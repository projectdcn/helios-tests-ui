"""Build a self-contained HTML test report (summary dashboard + per-test logs) from helios result runs.

    python tools/html_report.py results/mr3-20261013-002808 [more run dirs...] \
        --title "Helios MR3 Test Report" --subtitle "1 switch tray / 2 compute trays" \
        --notes results/findings.txt -o results/MR3-report.html

Each run dir is what one pytest session writes: tc_results.csv (+ logs/*.log per test item).
When several runs contain the same test item, an executed result (pass/fail/blocked) wins over a
skip, otherwise the later run wins. --notes: plain text, one finding per line; a line starting with
"!" is shown as a problem, "+" as good news, anything else neutral.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
import re
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SUITE_NAMES = OrderedDict([
    ("l1/test_switch_hw", "Switch hardware (TC-SWH)"),
    ("switch/test_switch_agent", "SONiC switch agent (TC-SWA)"),
    ("hw/test_hw", "Hardware (TC-HW)"),
    ("sideband/test_sideband_bringup", "Sideband bring-up (TC-BU / TC-SEQ)"),
    ("sideband/test_ifcp", "IFCP control plane (TC-BU / TC-SBM / TC-E2E)"),
    ("platform/test_node_platform", "Node platform (NTP, DNS, logs)"),
    ("telemetry/test_metrics_exporter", "GPU metrics exporter (TC-CTL)"),
    ("l1/test_rccl_smoke", "RCCL smoke"),
    ("l1/test_afm_datapath", "Scale-up datapath (TC-AFM-001..005A)"),
    ("ras/test_ras_reset", "RAS reset (TC-RAS-reset)"),
    ("ifoe/test_ifoe_driver", "IFoE driver (TC-AFM-LU / TC-HW-017)"),
    ("scaleout/test_rdma_perftest", "Scale-out RDMA (TC-IBS)"),
    ("l1/test_switch_health", "Switch health - added checks (TC-SWH-037..059)"),
    ("afm/test_afm_api", "AFM API"),
    ("afm/test_afm_api_more", "AFM API - more"),
    ("afm/test_afm_compute", "AFM compute nodes (TC-BU / TC-SEQ)"),
    ("afm/test_afm_rbac_audit", "AFM RBAC + audit"),
    ("afm/test_aggwatch", "AggWatch"),
    ("afm/test_afm_reconfigure", "AFM Reconfigure"),
    ("afm/test_afm_sideband_more", "Sideband placement (TC-SBM)"),
    ("afm/test_afm_switch_fabric", "Switch fabric vs AFM (TC-SWH-048 / 050)"),
    ("health/test_rack_health", "Rack health checks (HC-001..006)"),
    ("afm/test_afm_health", "AFM rack state + snapshot (HC-000)"),
])
STATUS = {"passed": ("PASS", "pass"), "failed": ("FAIL", "fail"), "error": ("ERROR", "fail"),
          "blocked": ("BLOCKED", "blocked"), "skipped": ("SKIP", "skip"), "partial": ("PARTIAL", "blocked")}


def to_utc(started: str, offset_h: float | None) -> str:
    """'YYYY-mm-dd HH:MM:SS' in the runner's clock -> '... UTC'. Already-UTC stamps (plugin >= 14 Oct) pass through;
    an unknown offset leaves the stamp as is (no ' UTC' suffix)."""
    if not started or started.endswith(" UTC") or offset_h is None:
        return started
    t = datetime.strptime(started, "%Y-%m-%d %H:%M:%S") - timedelta(hours=offset_h)
    return t.strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def load_runs(dirs: list[Path], tz: dict[str, float] | None = None) -> list[dict]:
    items: "OrderedDict[str, dict]" = OrderedDict()
    for d in dirs:
        rows = list(csv.DictReader(open(d / "tc_results.csv", newline="")))
        for r in rows:
            r["_run"] = d.name
            r["started"] = to_utc(r.get("started", ""), (tz or {}).get(d.name))
            log = d / r["log"] if r.get("log") else None
            r["_log"] = log.read_text(errors="replace") if log and log.exists() else ""
            prev = items.get(r["nodeid"])
            # an executed result beats a skip or a pre-flight block; a later run beats an earlier one otherwise
            # (an earlier BLOCKED must not hide a later run's skip reason)
            if prev is None or prev["outcome"] in ("skipped", "blocked") or r["outcome"] not in ("skipped", "blocked"):
                items[r["nodeid"]] = r
    return list(items.values())


LOG_KEEP = 75_000   # chars kept from each end of an embedded command log (the page must stay < 16 MB)


def clip_log(log: str, path: str) -> str:
    """Head + tail of a long command log, with a marker naming the full file under results/."""
    if len(log) <= 2 * LOG_KEEP:
        return log
    head, tail = log[:LOG_KEEP], log[-LOG_KEEP:]
    cut = log.count("\n", LOG_KEEP, len(log) - LOG_KEEP)
    return (f"{head.rsplit(chr(10), 1)[0]}\n... [{cut} lines omitted here - full log: results/{path}] ...\n"
            f"{tail.split(chr(10), 1)[-1]}")


def fmt_dur(s: float) -> str:
    s = float(s or 0)
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {sec}s" if h else f"{m}m {sec}s"


def esc(x) -> str:
    return html.escape(str(x or ""), quote=True)


def colour_log(text: str) -> str:
    out = []
    for line in text.splitlines():
        e = esc(line)
        if " WARNING " in line or "TIMED OUT" in line or "killed by remote timeout" in line:
            out.append(f'<span class="lvl-fail">{e}</span>')
        elif re.search(r"\] rc=(?!0 )\d+", line):
            out.append(f'<span class="lvl-warn">{e}</span>')
        elif re.search(r"\] \$ ", line):
            out.append(f'<span class="lvl-doc">{e}</span>')
        elif line.startswith("  --- "):
            out.append(f'<span class="lvl-note">{e}</span>')
        else:
            out.append(e)
    return "\n".join(out)


def plan_verdicts(items: list[dict]) -> dict[str, str]:
    by: dict[str, list[str]] = {}
    for r in items:
        by.setdefault(r["tc_id"] or r["nodeid"], []).append(r["outcome"])
    v = {}
    for tc, outs in by.items():
        v[tc] = ("FAIL" if any(o in ("failed", "error") for o in outs) else
                 "BLOCKED" if "blocked" in outs else
                 "PARTIAL" if "partial" in outs else
                 "PASS" if all(o == "passed" for o in outs) else
                 "SKIP" if all(o == "skipped" for o in outs) else "PASS")
    return v


WB_STATUS = {"PASS": "PASS", "FAIL": "FAIL", "PENDING": "PENDING", "SKIPPED": "SKIP", "PARTIAL": "PARTIAL", "N/A": "N/A"}


def workbook_status(wb_path, ids) -> dict[str, tuple[str, str, str]]:
    """{plan id: (reviewed status, latest note, title)} from the test-plan workbook's Testcases sheet (column F / H),
    so the report shows the reviewed status - e.g. 'Pending' for a case that failed only because of the test
    environment - instead of the raw test verdict."""
    if not wb_path:
        return {}
    import openpyxl
    from update_testplan import COL_NOTES, COL_STATUS, find_row

    ws = openpyxl.load_workbook(wb_path, read_only=False)["Testcases"]
    out = {}
    for tid in ids:
        r = find_row(ws, tid)
        st = str(ws.cell(r, COL_STATUS).value or "").strip().upper() if r else ""
        if st in WB_STATUS:
            note = str(ws.cell(r, COL_NOTES).value or "").split("\n\nEarlier: ", 1)[0]
            out[tid] = (WB_STATUS[st], note, str(ws.cell(r, 3).value or ""))
    return out


def workbook_all_rows(wb_path) -> list[dict]:
    """Every case row of the workbook's Testcases sheet (the whole test plan), as recorded there."""
    import openpyxl
    from update_testplan import ROW_ALIASES

    ws = openpyxl.load_workbook(wb_path, read_only=True)["Testcases"]
    out = []
    for r in ws.iter_rows(min_row=3, values_only=True):
        if r and not r[0] and "not allowed in minirack" in str(r[2] or "").lower():
            r = ("TC-AFM-035A",) + tuple(r[1:])      # the TC-AFM-035A row has no TID in the workbook
        if not r or not r[0] or not str(r[0]).strip().startswith("TC-"):
            continue
        r = list(r) + [None] * 12
        st = str(r[5] or "").strip()
        auto = str(r[3] or "").strip()
        shown = (WB_STATUS.get(st.upper(), st.upper()) if st else
                 "NOT RUN" if auto in ("Yes", "Inprogress") else "NOT AUTOMATED")
        tid, title = str(r[0]).strip(), str(r[2] or "")
        code = next((a for a, (t, frag) in ROW_ALIASES.items() if t == tid and frag and frag.lower() in title.lower()), tid)
        out.append({"id": tid, "code": code, "level": str(r[1] or ""), "title": title, "automation": auto,
                    "status": shown, "note": str(r[7] or "").split("\n\nEarlier: ", 1)[0], "scope": str(r[9] or ""),
                    "origin": str(r[10] or ""), "disruptive": str(r[11] or "")})
    return out


def earlier(r: dict) -> bool:
    """Not run with the current build, but the workbook keeps notes from the earlier (10-12 Oct) runs."""
    return r["status"] in ("NOT RUN", "NOT AUTOMATED") and bool(r["note"].strip())


def dim_matrix(rows: list[dict], key: str, label: str) -> str:
    """Status counts per value of a workbook column (Scope / Origin), one row per value plus a total."""
    order = [o for o in ("PASS", "FAIL", "PARTIAL", "PENDING", "SKIP", "N/A", "NOT RUN", "NOT AUTOMATED")
             if any(r["status"] == o for r in rows)]
    vals = sorted({r[key] or "(blank)" for r in rows}, key=lambda v: (-sum(1 for r in rows if (r[key] or "(blank)") == v), v))
    h = [f'<table><thead><tr><th>{esc(label)}</th>' + "".join(f'<th class="num">{esc(o)}</th>' for o in order) +
         '<th class="num">Total</th></tr></thead><tbody>']
    for v in vals + ["All"]:
        sel = [r for r in rows if v == "All" or (r[key] or "(blank)") == v]
        c = Counter(r["status"] for r in sel)
        b = "<b>" if v == "All" else ""
        h.append(f'<tr><td>{b}{esc(v)}</td>' + "".join(f'<td class="num">{b}{c.get(o, 0)}</td>' for o in order) +
                 f'<td class="num">{b}{len(sel)}</td></tr>')
    h.append("</tbody></table>")
    return "\n".join(h)


def all_cases_section(rows: list[dict], wb_name: str) -> str:
    """Whole-plan overview: counts, level x status matrix, and a filterable table of every case."""
    order = ["PASS", "FAIL", "PARTIAL", "PENDING", "SKIP", "N/A", "NOT RUN", "NOT AUTOMATED"]
    cls = {"PASS": "pass", "FAIL": "fail", "PENDING": "blocked", "PARTIAL": "blocked", "SKIP": "skip", "N/A": "skip"}
    levels = sorted({r["level"] for r in rows})
    n_earlier = sum(1 for r in rows if earlier(r))
    h = [f'<h2 id="plan">Test plan overview - every case in {esc(wb_name)} ({len(rows)})</h2>',
         '<p class="desc">Executed cases show the reviewed status from the workbook. <b>NOT RUN</b> = automated but not yet run on '
         'this rack with the current build (mostly disruptive cases awaiting approval, MCH-rack or equipment cases); '
         f'{n_earlier} of them carry notes from the 10-12 Oct runs on the earlier build (AFM 0.1.0-AICC-817) but no status in '
         'the workbook - marked "earlier notes only". <b>NOT AUTOMATED</b> = no test '
         'yet (needs AMD input, equipment or manual work).</p>',
         '<table><thead><tr><th>Level</th>' + "".join(f'<th class="num">{esc(o)}</th>' for o in order) +
         '<th class="num">Total</th></tr></thead><tbody>']
    for lv in levels + ["All"]:
        sel = [r for r in rows if lv == "All" or r["level"] == lv]
        c = Counter(r["status"] for r in sel)
        b = "<b>" if lv == "All" else ""
        h.append(f'<tr><td>{b}{esc(lv)}</td>' + "".join(f'<td class="num">{b}{c.get(o, 0)}</td>' for o in order) +
                 f'<td class="num">{b}{len(sel)}</td></tr>')
    h.append("</tbody></table>")
    h.append('<h3>By scope (where the case runs from)</h3>' + dim_matrix(rows, "scope", "Scope"))
    h.append('<h3>By origin</h3>' + dim_matrix(rows, "origin", "Origin"))
    h.append('<div class="toggles"><input id="pf" type="search" placeholder="Filter: ID, title, scope, notes..." '
             'style="padding:5px 8px;min-width:260px;font-size:12px"> <select id="ps" style="font-size:12px;padding:4px">'
             '<option value="">All statuses</option>' + "".join(f'<option>{esc(o)}</option>' for o in order) +
             '</select> <select id="pl" style="font-size:12px;padding:4px"><option value="">All levels</option>' +
             "".join(f'<option>{esc(l)}</option>' for l in levels) + '</select> <span id="pc" class="desc"></span></div>')
    h.append('<table id="plantable"><thead><tr><th>Plan ID</th><th>Level</th><th>Title</th><th>Scope</th><th>Disruptive</th>'
             '<th>Origin</th><th>Automation</th><th>Status</th><th>Latest execution note</th></tr></thead><tbody>')
    for r in rows:
        h.append(f'<tr data-s="{esc(r["status"])}" data-l="{esc(r["level"])}"><td class="mono">{esc(r["id"])}</td>'
                 f'<td>{esc(r["level"])}</td><td>{esc(r["title"])}</td><td>{esc(r["scope"])}</td>'
                 f'<td>{esc(r["disruptive"])}</td><td>{esc(r["origin"])}</td>'
                 f'<td>{esc(r["automation"])}</td><td><span class="{cls.get(r["status"], "")}">{esc(r["status"])}</span>'
                 + ('<br><span class="desc">earlier notes only</span>' if earlier(r) else '') + '</td>'
                 f'<td class="reason">{esc(r["note"] if len(r["note"]) <= 400 else r["note"][:400] + " ...")}</td></tr>')
    h.append("</tbody></table>")
    h.append("""<script>
(function () {
  var f = document.getElementById('pf'), s = document.getElementById('ps'), l = document.getElementById('pl'),
      c = document.getElementById('pc'), rows = document.querySelectorAll('#plantable tbody tr');
  function apply() {
    var q = f.value.toLowerCase(), n = 0;
    rows.forEach(function (tr) {
      var ok = (!q || tr.textContent.toLowerCase().indexOf(q) >= 0) && (!s.value || tr.dataset.s === s.value) &&
               (!l.value || tr.dataset.l === l.value);
      tr.style.display = ok ? '' : 'none'; if (ok) n++;
    });
    c.textContent = n + ' of ' + rows.length + ' cases';
  }
  [f, s, l].forEach(function (e) { e.addEventListener('input', apply); e.addEventListener('change', apply); });
  apply();
}());
</script>""")
    return "\n".join(h)


def coverage_section(plan_rows: list[dict] | None) -> str:
    """Automation coverage from tools/coverage.py (manifest + added cases), with what was executed on the rack and
    its result per category / level when the whole-plan rows are known."""
    from coverage import coverage
    from update_testplan import ROW_ALIASES

    cov = coverage()
    # status per plan ID from the workbook rows (a TID on several rows: the code IDs share it, e.g. TC-AFM-005/005A)
    st: dict[str, str] = {}
    for r in plan_rows or []:
        if r["status"] not in ("NOT RUN", "NOT AUTOMATED"):
            st[r["code"]] = r["status"]

    def stats(sel):
        ids = [c["id"] for c in sel]
        auto = sum(1 for i in ids if i in cov["automated"])
        ex = [x for x in (st.get(i) for i in ids) if x]
        return len(ids), auto, len(ex), Counter(ex)

    def row(name, sel, bold=False):
        n, a, e, c = stats(sel)
        pct = 100.0 * a / n if n else 0
        b, eb = ("<b>", "</b>") if bold else ("", "")
        bar = (f'<div style="background:#e5e7eb;border-radius:3px;height:8px;width:120px;display:inline-block;vertical-align:middle">'
               f'<div style="background:#2c7be5;height:8px;border-radius:3px;width:{pct * 1.2:.0f}px"></div></div>')
        return (f"<tr><td>{b}{esc(name)}{eb}</td><td class='num'>{b}{a}{eb}</td><td class='num'>{b}{n}{eb}</td>"
                f"<td class='num'>{b}{pct:.0f}%{eb} {bar}</td><td class='num'>{b}{e}{eb}</td>"
                f"<td class='num'>{c.get('PASS', 0)}</td><td class='num'>{c.get('FAIL', 0)}</td>"
                f"<td class='num'>{c.get('PENDING', 0)}</td><td class='num'>{c.get('SKIP', 0) + c.get('N/A', 0)}</td></tr>")
    hdr = ("<tr><th>{}</th><th class='num'>Automated</th><th class='num'>Total</th><th class='num'>Coverage</th>"
           "<th class='num'>Executed on rack</th><th class='num'>Pass</th><th class='num'>Fail</th>"
           "<th class='num'>Pending</th><th class='num'>Skip / N/A</th></tr>")
    cases = cov["cases"]
    a, t = cov["total"]
    row_ids = {r["code"] for r in plan_rows or []}
    shared = sorted(a for a, (t, _f) in ROW_ALIASES.items() if a != t and a in row_ids)
    other = sorted({c["id"] for c in cases} - row_ids - set(shared) - {c["id"] for c in cases if c["id"].startswith("TC-IBS-")})
    ibs = sorted(c["id"] for c in cases if c["id"].startswith("TC-IBS-") and c["id"] not in row_ids)
    recon = (f'The workbook\'s Testcases sheet has {len(plan_rows)} case rows, one per case ({", ".join(shared)} sit on a '
             f'row whose TID is shared with another case or blank; the report maps each row to its case). '
             f'{t - len(row_ids)} plan cases have no row there: {len(ibs)} TC-IBS-* (on the mortaro sheet)'
             + (f' and <b>{", ".join(other)}</b> (in the plan manifest, missing from the workbook)' if other else "")
             + '.') if plan_rows else ""
    h = [f'<h2 id="coverage">Automation coverage - {a} of {t} cases automated ({100.0 * a / t:.0f}%)</h2>',
         f'<p class="desc">From <span class="mono">tools/coverage.py</span>: a case is automated when a test carries its '
         f'plan ID. Totals count unique plan IDs ({t}: the BKC-26.12 manifest plus testplan/added.yaml). {recon} '
         f'"Executed on rack" = cases with a status in the workbook.</p>',
         "<table><thead>" + hdr.format("Level") + "</thead><tbody>"]
    for lv in cov["by_level"]:
        h.append(row(lv, [c for c in cases if c["level"] == lv]))
    h.append(row("All levels", cases, bold=True))
    h.append("</tbody></table><table style='margin-top:16px'><thead>" + hdr.format("Category") + "</thead><tbody>")
    for catn in cov["by_category"]:
        h.append(row(catn, [c for c in cases if c["category"] == catn]))
    h.append(row("All categories", cases, bold=True))
    h.append("</tbody></table>")
    if cov["extra"]:
        h.append(f'<p class="desc">Extra tests (not plan cases, used as evidence): {esc(", ".join(cov["extra"]))}</p>')
    return "\n".join(h)


def build(items: list[dict], args) -> str:
    verdicts = plan_verdicts(items)
    # plan cases (AMD plan + testplan/added.yaml) vs extra evidence tests (e.g. SMOKE-RCCL-*), counted separately
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from plan_cases import load_cases
        plan_ids = {c["id"] for c in load_cases()} | {"TC-AFM-005A", "TC-AFM-048A", "TC-AFM-035A"}
    except Exception:  # noqa: BLE001 - outside the repo: count everything
        plan_ids = set(verdicts)
    extra = sorted(k for k in verdicts if k not in plan_ids)
    reviewed = workbook_status(getattr(args, "workbook", None), [k for k in verdicts if k in plan_ids])
    for k, (st, _note, _title) in reviewed.items():
        verdicts[k] = st
    kp = Counter(v for k, v in verdicts.items() if k in plan_ids)
    wb_rows = {r["code"]: r for r in workbook_all_rows(args.workbook)} if getattr(args, "workbook", None) else {}
    plan_rows = list(wb_rows.values()) if getattr(args, "all_cases", False) and args.workbook else None
    if plan_rows is not None:      # whole test plan: counts follow the workbook rows, not only what ran
        kp = Counter(r["status"] for r in plan_rows)
    n_cases = len(plan_rows) if plan_rows is not None else len(verdicts) - len(extra)
    not_run_txt = (f" &middot; {kp['NOT RUN']} automated but not run &middot; {kp['NOT AUTOMATED']} not automated"
                   if plan_rows is not None else "")
    card_why = ""
    if plan_rows is not None:      # one line under the "not run" card: why those automated cases did not run
        _nr = [r for r in plan_rows if r["status"] == "NOT RUN"]
        _dis = sum(1 for r in _nr if r["disruptive"].lower().startswith("disruptive"))
        _mch = sum(1 for r in _nr if r["code"].startswith("TC-MCH-"))
        card_why = (("All disruptive" if _nr and _dis == len(_nr) else f"{_dis} disruptive")
                    + " - run only with per-batch approval on a healthy rack" + (f"; {_mch} MCH (not this rack)" if _mch else ""))
    not_run_card = (f'<div class="kpi-card"><div class="num" style="color:#94a3b8;">{kp["NOT RUN"]}</div>'
                    f'<div class="label">Automated, not run yet</div><div class="why">{esc(card_why)}</div></div>'
                    f'<div class="kpi-card"><div class="num" style="color:#64748b;">{kp["NOT AUTOMATED"]}</div>'
                    f'<div class="label">Not automated</div><div class="why">Needs AMD input, lab equipment or manual steps</div></div>'
                    if plan_rows is not None else "")
    executed = kp["PASS"] + kp["FAIL"]
    rate = (100.0 * kp["PASS"] / executed) if executed else 0.0
    auto_pill = auto_card = ""
    # pass / fail / pending / skip cards; the whole-plan view already shows them in the "Executed result" pill
    outcome_cards = "" if plan_rows is not None else (
        f'<div class="kpi-card"><div class="num" style="color:var(--pass);">{kp["PASS"]}</div><div class="label">Passed</div></div>'
        f'<div class="kpi-card"><div class="num" style="color:var(--fail);">{kp["FAIL"]}</div><div class="label">Failed</div></div>'
        + (f'<div class="kpi-card"><div class="num" style="color:#eab308;">{kp["PARTIAL"]}</div><div class="label">Partial</div></div>' if kp["PARTIAL"] else "")
        + f'<div class="kpi-card"><div class="num" style="color:var(--blocked);">{kp["PENDING"] + kp["BLOCKED"]}</div><div class="label">Pending / blocked</div></div>'
        f'<div class="kpi-card"><div class="num" style="color:var(--skip);">{kp["SKIP"] + kp["N/A"]}</div><div class="label">Skipped / N/A</div></div>')
    # cases that ran on the rack = any status other than not run / not automated (pass, fail, pending, skip, N/A)
    n_exec = n_cases - kp["NOT RUN"] - kp["NOT AUTOMATED"]
    detail = (f"{kp['PARTIAL']} partial &middot; " if kp["PARTIAL"] else "") + f"{kp['PENDING']} pending &middot; {kp['BLOCKED']} blocked &middot; {kp['SKIP']} skipped &middot; {kp['N/A']} N/A"
    if plan_rows is not None:   # whole plan: pending / skipped / N/A ran on the rack and count as executed
        not_exec_val = f"{kp['NOT RUN'] + kp['NOT AUTOMATED']}: {kp['NOT RUN']} automated, not run &middot; {kp['NOT AUTOMATED']} not automated"
        n_earlier = sum(1 for r in plan_rows if earlier(r))
        nr = [r for r in plan_rows if r["status"] == "NOT RUN"]
        nr_dis = sum(1 for r in nr if r["disruptive"].lower().startswith("disruptive"))
        nr_mch = sum(1 for r in nr if r["code"].startswith("TC-MCH-"))
        why = (f"all {len(nr)} automated-not-run cases are disruptive" if nr and nr_dis == len(nr)
               else f"{nr_dis} of the {len(nr)} automated-not-run cases are disruptive")
        not_exec_chips = (f'<span class="chip">Why not run: {why} (restarts, resets, link flaps, reboots) - they run only '
                          'with per-batch approval on a healthy rack'
                          + (f'; {nr_mch} are MCH cases (not applicable on a non-MCH rack)' if nr_mch else '') + '</span>'
                          '<span class="chip">Not automated: needs AMD input, lab equipment or manual steps</span>'
                          f'<span class="chip">{n_earlier} of the not-run cases have notes from the 10-12 Oct runs on the earlier build, no status</span>')
    else:                       # every case here ran: the third pill is the cases without a pass/fail verdict
        not_exec_val = detail
        not_exec_chips = '<span class="chip">blocked = datapath pre-flight refused traffic</span>'
    not_exec_cap = "Not executed" if plan_rows is not None else "No pass/fail verdict"
    # whole-plan view: not run / not automated (and why) are on their own KPI cards - no separate pill
    not_exec_pill = "" if plan_rows is not None else (
        f'<div class="pill"><div><div class="cap">{not_exec_cap}</div><div class="val">{not_exec_val}</div>'
        f'{not_exec_chips}</div></div>')
    no_verdict_chip = (f'<span class="chip">{n_exec - executed} without a pass/fail verdict: {kp["PARTIAL"]} partial (passed on the reachable trays), {kp["PENDING"]} pending, '
                       f'{kp["SKIP"]} skipped, {kp["N/A"]} N/A</span>' if plan_rows is not None else "")
    exec_txt = f" &middot; {n_exec} executed" if plan_rows is not None else ""
    exec_card = (f'<div class="kpi-card"><div class="num" style="color:var(--accent2);">{n_exec}</div>'
                 f'<div class="label">Executed ({100.0 * n_exec / n_cases:.0f}% of plan)</div></div>'
                 if plan_rows is not None and n_cases else "")
    if plan_rows is not None:      # automation coverage (tools/coverage.py) in the hero pills + KPI cards
        from coverage import coverage
        cov = coverage()
        ca, ct = cov["total"]
        lv_chips = "".join(f'<span class="chip">{esc(lv)} {a}/{t} ({100.0 * a / t:.0f}%)</span>'
                           for lv, (a, t) in cov["by_level"].items() if t)
        auto_pill = (f'<div class="pill"><div><div class="cap">Automated</div>'
                     f'<div class="val">{ca} / {ct} cases ({100.0 * ca / ct:.0f}%) &middot; {ct - ca} not automated</div>'
                     f'{lv_chips}<a class="chip" href="#coverage" style="color:#fff">coverage &rarr;</a></div></div>')
        auto_card = (f'<div class="kpi-card"><div class="num" style="color:var(--accent);">{100.0 * ca / ct:.0f}%</div>'
                     f'<div class="label">Automated ({ca} / {ct})</div></div>')
    suites: "OrderedDict[str, list[dict]]" = OrderedDict((k, []) for k in SUITE_NAMES)
    for r in items:
        suites.setdefault(r["suite"], []).append(r)
    suites = OrderedDict((k, v) for k, v in suites.items() if v)
    runs = sorted({r["_run"] for r in items})
    started = sorted(r["started"] for r in items if r.get("started"))
    if started and not all(x.endswith(" UTC") for x in started):   # mixed / unknown runner clocks: say so
        started = [x.replace(" UTC", "") for x in started]
        started[-1] += " (runner clocks, not normalised - see --tz)"

    def suite_counts(rows):
        c = Counter(STATUS.get(r["outcome"], ("?", ""))[0] for r in rows)
        return c["PASS"] + c["PARTIAL"], c["FAIL"] + c["ERROR"], c["BLOCKED"], c["SKIP"]   # PARTIAL = passed on the kept trays

    suite_labels = [SUITE_NAMES.get(k, k).split(" (")[0] for k in suites]
    bars = [suite_counts(v) for v in suites.values()]

    notes = []
    if args.notes:
        for line in Path(args.notes).read_text().splitlines():
            line = line.rstrip()
            if not line:
                continue
            kind = "bad" if line.startswith("!") else "good" if line.startswith("+") else "neutral"
            notes.append((kind, line.lstrip("!+ ").strip()))

    h = []
    h.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(args.title)}</title>
<style>
body {{ font-family: Arial, sans-serif; font-size: 13px; margin: 20px; color: #222; background: #fff; }}
h1 {{ color: #2c3e50; }}
h2 {{ color: #34495e; font-size: 15px; margin-top: 34px; border-bottom: 2px solid #2c3e50; padding-bottom: 4px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
th {{ background: #2c3e50; color: #fff; padding: 7px 10px; text-align: left; }}
td {{ padding: 6px 10px; border-bottom: 1px solid #dce1e7; vertical-align: top; }}
tr:hover td {{ background: #f0f4f8; }}
.pass {{ color: #27ae60; font-weight: bold; }}
.fail {{ color: #c0392b; font-weight: bold; }}
.blocked {{ color: #d35400; font-weight: bold; }}
.skip {{ color: #b8860b; font-weight: bold; }}
.info {{ margin-top: 10px; padding: 10px 16px; background: #f4f7fa; border: 1px solid #dce1e7; display: inline-block; }}
.mono {{ font-family: monospace; }}
.num {{ text-align: right; }}
.desc {{ color: #555; }}
.reason {{ color: #7a5a00; font-size: 12px; }}
tr.logrow:hover td {{ background: #fafbfc; }}
td.logcell {{ background: #fafbfc; padding-top: 0; }}
.toggles {{ margin: 12px 0 0; }}
.toggles button {{ font-size: 12px; padding: 5px 12px; margin-right: 8px; cursor: pointer; background: #2c3e50; color: #fff; border: none; border-radius: 3px; }}
.toggles button:hover {{ background: #34495e; }}
details.logbox summary {{ cursor: pointer; color: #1b4f72; font-weight: bold; padding: 6px 0; outline: none; }}
pre.logpre {{ white-space: pre-wrap; word-break: break-word; background: #0f1720; color: #d6deeb; padding: 12px; border-radius: 4px;
              max-height: 420px; overflow: auto; font-size: 12px; line-height: 1.45; margin: 0 0 8px; }}
pre.failpre {{ white-space: pre-wrap; word-break: break-word; background: #fff5f5; color: #7b1d1d; border: 1px solid #f5c2c2;
               padding: 10px 12px; border-radius: 4px; font-size: 12px; margin: 0 0 8px; max-height: 260px; overflow: auto; }}
pre.logpre .lvl-warn {{ color: #f1c40f; font-weight: bold; }}
pre.logpre .lvl-fail {{ color: #ff6b6b; font-weight: bold; }}
pre.logpre .lvl-doc {{ color: #7fd7ff; }}
pre.logpre .lvl-note {{ color: #9aa7b4; font-style: italic; }}
.viz {{ --pass:#22c55e; --fail:#ef4444; --skip:#f59e0b; --blocked:#f97316; --vbg:#0f172a; --card:#1e293b; --border:#334155;
        --vtext:#e2e8f0; --muted:#94a3b8; --accent:#38bdf8; --accent2:#818cf8; background:var(--vbg); color:var(--vtext);
        border-radius:16px; padding:26px 24px 30px; margin:24px 0 36px; font-family:'Segoe UI', system-ui, sans-serif;
        font-size:14px; line-height:1.6; }}
.viz *, .viz *::before, .viz *::after {{ box-sizing:border-box; }}
.viz .viz-title {{ font-size:22px; font-weight:800; color:#fff; margin:0 0 4px; }}
.viz h3 {{ color:var(--vtext); font-size:15px; font-weight:600; margin:0 0 10px; }}
.viz .hero {{ background:linear-gradient(135deg,#0ea5e9 0%,#6366f1 100%); border-radius:14px; padding:22px 24px; margin-bottom:24px; }}
.viz .hero-pills {{ display:flex; flex-wrap:wrap; gap:12px; margin-top:16px; }}
.viz .pill {{ display:flex; align-items:center; gap:14px; background:rgba(255,255,255,0.12); border:1px solid rgba(255,255,255,0.25);
              border-radius:14px; padding:14px 22px; }}
.viz .pill .cap {{ font-size:10px; font-weight:600; letter-spacing:.1em; text-transform:uppercase; color:rgba(255,255,255,0.65); }}
.viz .pill .val {{ font-size:17px; font-weight:800; color:#fff; }}
.viz .chip {{ display:inline-block; border-radius:20px; font-size:11px; padding:2px 10px; font-weight:600;
              background:rgba(255,255,255,0.15); color:#fff; border:1px solid rgba(255,255,255,0.3); margin:6px 6px 0 0; }}
.viz .kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:14px; margin-bottom:28px; }}
.viz .kpi-card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; }}
.viz .kpi-card .num {{ font-size:32px; font-weight:800; }}
.viz .kpi-card .label {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; margin-top:2px; }}
.viz .kpi-card .why {{ color:var(--muted); font-size:11px; line-height:1.35; margin-top:6px; }}
.viz .charts-row {{ display:grid; grid-template-columns:1fr 1.6fr; gap:20px; margin-bottom:24px; }}
.viz .chart-card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; }}
.viz .chart-wrap {{ position:relative; height:260px; }}
.viz .findings {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:18px 22px; }}
.viz .findings li {{ margin:6px 0; }}
.viz .findings .bad::marker {{ color:var(--fail); }} .viz .findings .good::marker {{ color:var(--pass); }}
.viz .findings .bad b {{ color:#fca5a5; }} .viz .findings .good b {{ color:#86efac; }}
@media (max-width:900px) {{ .viz .kpi-grid {{ grid-template-columns:repeat(3,1fr); }} .viz .charts-row {{ grid-template-columns:1fr; }} }}
@media (max-width:520px) {{ body {{ margin:12px; }} .viz .kpi-grid {{ grid-template-columns:1fr 1fr; }} }}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
</head>
<body>
<h1>{esc(args.title)}</h1>
<div class="viz">
  <div class="hero">
    <div class="viz-title">{esc(args.title)} &mdash; Visual Dashboard</div>
    <div style="color:rgba(255,255,255,0.85);font-size:13px;">{esc(args.subtitle)} &nbsp;|&nbsp; Runs: {esc(", ".join(runs))}
      &nbsp;|&nbsp; Executed {esc(started[0] if started else "")} &rarr; {esc(started[-1] if started else "")}
      &nbsp;|&nbsp; Generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC</div>
    <div class="hero-pills">
      <div class="pill"><div><div class="cap">Test cases</div><div class="val">{n_cases} plan cases{exec_txt} &middot; {len(items)} executions</div>
        {f'<span class="chip">+{len(extra)} extra evidence test(s): {esc(", ".join(extra))}</span>' if extra else ''}
        <span class="chip">{len(suites)} suites</span><span class="chip">{len(runs)} run(s)</span></div></div>
      <div class="pill"><div><div class="cap">Executed result</div>
        <div class="val" style="color:{'#86efac' if kp['FAIL'] == 0 else '#fecaca'};">{n_exec} executed &middot; {kp['PASS']} PASS &middot; {kp['FAIL']} FAIL{f" &middot; {kp['PARTIAL']} PARTIAL" if kp['PARTIAL'] else ""}</div>
        {no_verdict_chip}
        <span class="chip">{rate:.0f}% pass rate ({kp['PASS']} of {executed} with a pass/fail verdict)</span></div></div>
      {not_exec_pill}
      {auto_pill}
    </div>
  </div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="num" style="color:var(--accent);">{n_cases}</div><div class="label">Test cases</div></div>{auto_card}{exec_card}{not_run_card}
    {outcome_cards}
    <div class="kpi-card"><div class="num" style="color:var(--accent2);">{len(items)}</div><div class="label">Test items</div></div>
  </div>
  <div class="charts-row">
    <div class="chart-card"><h3>Test cases by result</h3><div class="chart-wrap"><canvas id="vizDoughnut"></canvas></div></div>
    <div class="chart-card"><h3>Test items per suite</h3><div class="chart-wrap"><canvas id="vizSuiteBar"></canvas></div></div>
  </div>""")
    if notes:
        h.append('  <div class="findings"><h3>Key findings</h3><ul>')
        for kind, text in notes:
            head, _, tail = text.partition(":")
            body = f"<b>{esc(head)}:</b>{esc(tail)}" if tail else esc(text)
            h.append(f'    <li class="{kind}">{body}</li>')
        h.append("  </ul></div>")
    h.append("</div>")
    if plan_rows is not None:
        h.append(coverage_section(plan_rows))
        h.append(all_cases_section(plan_rows, Path(args.workbook).name))
    elif reviewed:
        cls = {"PASS": "pass", "FAIL": "fail", "PENDING": "blocked", "SKIP": "skip", "PARTIAL": "blocked", "N/A": "skip"}
        here = [dict(r, status=reviewed[r["code"]][0]) for r in wb_rows.values() if r["code"] in reviewed]
        h.append(f'<h2>Test plan status (reviewed, from {esc(Path(args.workbook).name)})</h2>'
                 '<p class="desc">Status as recorded in the workbook. Where it differs from the raw test verdict in the '
                 'sections below (e.g. Pending for a failure caused by the test environment), the note says why. '
                 'Scope = where the case runs from; Origin = BKC-26.12 plan or added by helios-tests.</p>')
        if here:
            h.append('<h3>By scope</h3>' + dim_matrix(here, "scope", "Scope"))
            h.append('<h3>By origin</h3>' + dim_matrix(here, "origin", "Origin"))
        h.append('<table><thead><tr><th>Plan ID</th><th>Level</th><th>Title</th><th>Scope</th><th>Origin</th><th>Disruptive</th>'
                 '<th>Status</th><th>Execution notes</th></tr></thead><tbody>')
        for tid in sorted(reviewed, key=lambda t: (t.split("-")[1], t)):
            st, note, title = reviewed[tid]
            raw = plan_verdicts([r for r in items if r["tc_id"] == tid]).get(tid, "")
            diff = f' <span class="reason">(raw test verdict: {esc(raw)})</span>' if raw and raw != st else ""
            w = wb_rows.get(tid, {})
            h.append(f'<tr><td class="mono">{esc(tid)}</td><td>{esc(w.get("level", ""))}</td><td>{esc(title)}</td>'
                     f'<td>{esc(w.get("scope", ""))}</td><td>{esc(w.get("origin", ""))}</td><td>{esc(w.get("disruptive", ""))}</td>'
                     f'<td><span class="{cls.get(st, "")}">'
                     f'{esc("PENDING" if st == "PENDING" else st)}</span>{diff}</td><td class="reason">{esc(note[:700])}</td></tr>')
        h.append("</tbody></table>")

    h.append(f"""<script>
(function () {{
  if (typeof Chart === 'undefined') return;
  Chart.defaults.color = '#94a3b8'; Chart.defaults.borderColor = '#334155';
  Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";
  var P='#22c55e', F='#ef4444', B='#f97316', S='#f59e0b';
  new Chart(document.getElementById('vizDoughnut'), {{ type: 'doughnut',
    data: {{ labels: ['Pass','Fail','Partial','Pending / blocked','Skipped / N/A','Automated, not run','Not automated'], datasets: [{{ data: {json.dumps([kp['PASS'], kp['FAIL'], kp['PARTIAL'], kp['PENDING'] + kp['BLOCKED'], kp['SKIP'] + kp['N/A'], kp['NOT RUN'], kp['NOT AUTOMATED']])},
      backgroundColor: [P,F,'#eab308',B,S,'#94a3b8','#475569'], borderWidth: 0, hoverOffset: 8 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false, cutout: '70%', plugins: {{ legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }} }} }},
    plugins: [{{ id: 'ct', beforeDraw: function (c) {{ var a=c.chartArea, x=(a.left+a.right)/2, y=(a.top+a.bottom)/2, ctx=c.ctx;
      ctx.save(); ctx.font='bold 24px Segoe UI'; ctx.fillStyle='#e2e8f0'; ctx.textAlign='center'; ctx.textBaseline='middle';
      ctx.fillText('{rate:.0f}%', x, y-8); ctx.font='12px Segoe UI'; ctx.fillStyle='#94a3b8'; ctx.fillText('pass rate', x, y+14); ctx.restore(); }} }}] }});
  new Chart(document.getElementById('vizSuiteBar'), {{ type: 'bar',
    data: {{ labels: {json.dumps(suite_labels)}, datasets: [
      {{ label: 'Pass', data: {json.dumps([b[0] for b in bars])}, backgroundColor: P, borderRadius: 3 }},
      {{ label: 'Fail', data: {json.dumps([b[1] for b in bars])}, backgroundColor: F, borderRadius: 3 }},
      {{ label: 'Blocked', data: {json.dumps([b[2] for b in bars])}, backgroundColor: B, borderRadius: 3 }},
      {{ label: 'Skipped', data: {json.dumps([b[3] for b in bars])}, backgroundColor: S, borderRadius: 3 }} ] }},
    options: {{ responsive: true, maintainAspectRatio: false, indexAxis: 'y',
      plugins: {{ legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }} }},
      scales: {{ x: {{ stacked: true, beginAtZero: true }}, y: {{ stacked: true, grid: {{ display: false }}, ticks: {{ font: {{ size: 11 }} }} }} }} }} }});
}}());
</script>""")

    # run summary per suite
    h.append("<h2>Run Summary</h2><p class=\"desc\">Per test item (one per switch / tray / parameter) with the raw pytest "
             "verdict of the latest run. The reviewed status of each plan case (e.g. N/A for a skipped PSU-module item, Pending "
             "for an environment failure) is the workbook's, shown in the overview above. Pass includes PARTIAL items (passed on every tray the pre-flight kept).</p><table><thead><tr><th>#</th><th>Suite</th><th class=\"num\">Items</th><th class=\"num\">Pass</th>"
             "<th class=\"num\">Fail</th><th class=\"num\">Blocked</th><th class=\"num\">Skip</th><th class=\"num\">Duration</th><th>Result</th></tr></thead><tbody>")
    for i, (key, rows) in enumerate(suites.items(), 1):
        p, f, b, s = suite_counts(rows)
        res = ("FAIL", "fail") if f else ("BLOCKED", "blocked") if b else ("PASS", "pass") if p else ("SKIP", "skip")
        dur = sum(float(r["duration_s"] or 0) for r in rows)
        h.append(f'<tr><td>{i}</td><td><a href="#s{i}">{esc(SUITE_NAMES.get(key, key))}</a></td><td class="num">{len(rows)}</td>'
                 f'<td class="num">{p}</td><td class="num">{f}</td><td class="num">{b}</td><td class="num">{s}</td>'
                 f'<td class="num">{fmt_dur(dur)}</td><td><span class="{res[1]}">{res[0]}</span></td></tr>')
    h.append("</tbody></table>")
    h.append('<div class="toggles"><button onclick="document.querySelectorAll(\'details.logbox\').forEach(function(d){d.open=true;});">'
             'Expand all logs</button><button onclick="document.querySelectorAll(\'details.logbox\').forEach(function(d){d.open=false;});">'
             'Collapse all logs</button></div>')

    for i, (key, rows) in enumerate(suites.items(), 1):
        p, f, b, s = suite_counts(rows)
        res = ("FAIL", "fail") if f else ("BLOCKED", "blocked") if b else ("PASS", "pass") if p else ("SKIP", "skip")
        dur = sum(float(r["duration_s"] or 0) for r in rows)
        h.append(f'<h2 id="s{i}">{esc(SUITE_NAMES.get(key, key))} &nbsp;[<span class="{res[1]}">{res[0]}</span>]</h2>')
        h.append(f'<div class="info"><strong>Result:</strong> {p} passed, {f} failed, {b} blocked, {s} skipped of {len(rows)} &nbsp;|&nbsp; '
                 f'<strong>Duration:</strong> {fmt_dur(dur)} &nbsp;|&nbsp; <strong>Module:</strong> <span class="mono">tests/{esc(key)}.py</span></div>')
        h.append('<table><thead><tr><th>#</th><th>TC ID</th><th>Test</th><th>Description</th><th>Scope</th><th>Origin</th><th class="num">Duration</th><th>Status</th></tr></thead><tbody>')
        for j, r in enumerate(rows, 1):
            label, cls = STATUS.get(r["outcome"], (r["outcome"], ""))
            reason = ""
            if r["outcome"] in ("skipped",) and r.get("message"):
                reason = f'<div class="reason">{esc(r["message"].removeprefix("Skipped: ")[:300])}</div>'
            h.append(f'<tr><td>{j}</td><td class="mono">{esc(r["tc_id"])}</td><td class="mono">{esc(r["test"])}</td>'
                     f'<td class="desc">{esc(r.get("description"))}{reason}</td>'
                     f'<td>{esc(wb_rows.get(r["tc_id"], {}).get("scope", ""))}</td><td>{esc(wb_rows.get(r["tc_id"], {}).get("origin", ""))}</td>'
                     f'<td class="num">{fmt_dur(r["duration_s"])}</td>'
                     f'<td><span class="{cls}">{label}</span></td></tr>')
            log = clip_log(r.get("_log", "").rstrip(), f'{r["_run"]}/{r.get("log", "")}')
            nlines = log.count("\n") + 1 if log else 0
            fail = ""
            if r["outcome"] in ("failed", "error", "blocked", "partial") and r.get("message"):
                fail = f'<pre class="failpre">{esc(r["message"])}</pre>'
            props = ""
            if r.get("properties"):
                try:
                    props = f'<pre class="failpre" style="background:#f4f7fa;color:#234;border-color:#dce1e7;">measurements: ' \
                            f'{esc(json.dumps(json.loads(r["properties"]), indent=1)[:4000])}</pre>'
                except ValueError:
                    pass
            h.append(f'<tr class="logrow"><td class="logcell" colspan="8"><details class="logbox"><summary>Command log '
                     f'({nlines} lines, run {esc(r["_run"])}, started {esc(r.get("started"))})</summary>{fail}{props}'
                     f'<pre class="logpre">{colour_log(log) or "(no remote commands)"}</pre></details></td></tr>')
        h.append("</tbody></table>")
    h.append("</body></html>")
    return "\n".join(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--title", default="Helios Test Report")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--notes", type=Path)
    ap.add_argument("--tz", action="append", default=[], metavar="RUN=HOURS",
                    help="UTC offset of the runner clock of a run dir whose 'started' stamps have no ' UTC' suffix, "
                         "e.g. --tz g02-g17-20261014-092828=-7 (Mac, PDT); runs on the jump host are UTC (=0)")
    ap.add_argument("--all-cases", action="store_true", help="with --workbook: cover EVERY case of the test plan "
                    "(overview counts, level x status, filterable table incl. NOT RUN / NOT AUTOMATED), not only executed ones")
    ap.add_argument("--workbook", type=Path, help="show the reviewed status (Testcases column F + notes) of each plan case "
                                                "from this test-plan workbook instead of the raw test verdict")
    args = ap.parse_args()
    items = load_runs(args.runs, {k: float(v) for k, v in (x.split("=", 1) for x in args.tz)})
    args.output.write_text(build(items, args))
    v = Counter(plan_verdicts(items).values())
    if args.workbook:
        v = Counter(st for st, _, _ in workbook_status(args.workbook, list(plan_verdicts(items))).values())
    print(f"{args.output}: {len(items)} items, {dict(v)}, {args.output.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
