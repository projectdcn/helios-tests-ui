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
from datetime import datetime
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
])
STATUS = {"passed": ("PASS", "pass"), "failed": ("FAIL", "fail"), "error": ("ERROR", "fail"),
          "blocked": ("BLOCKED", "blocked"), "skipped": ("SKIP", "skip"), "partial": ("PARTIAL", "blocked")}


def load_runs(dirs: list[Path]) -> list[dict]:
    items: "OrderedDict[str, dict]" = OrderedDict()
    for d in dirs:
        rows = list(csv.DictReader(open(d / "tc_results.csv", newline="")))
        for r in rows:
            r["_run"] = d.name
            log = d / r["log"] if r.get("log") else None
            r["_log"] = log.read_text(errors="replace") if log and log.exists() else ""
            prev = items.get(r["nodeid"])
            # an executed result beats a skip or a pre-flight block; a later run beats an earlier one otherwise
            # (an earlier BLOCKED must not hide a later run's skip reason)
            if prev is None or prev["outcome"] in ("skipped", "blocked") or r["outcome"] not in ("skipped", "blocked"):
                items[r["nodeid"]] = r
    return list(items.values())


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
    kp = Counter(v for k, v in verdicts.items() if k in plan_ids)
    executed = kp["PASS"] + kp["FAIL"]
    rate = (100.0 * kp["PASS"] / executed) if executed else 0.0
    suites: "OrderedDict[str, list[dict]]" = OrderedDict((k, []) for k in SUITE_NAMES)
    for r in items:
        suites.setdefault(r["suite"], []).append(r)
    suites = OrderedDict((k, v) for k, v in suites.items() if v)
    runs = sorted({r["_run"] for r in items})
    started = sorted(r["started"] for r in items if r.get("started"))

    def suite_counts(rows):
        c = Counter(STATUS.get(r["outcome"], ("?", ""))[0] for r in rows)
        return c["PASS"], c["FAIL"] + c["ERROR"], c["BLOCKED"], c["SKIP"]

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
.viz .kpi-grid {{ display:grid; grid-template-columns:repeat(6,1fr); gap:14px; margin-bottom:28px; }}
.viz .kpi-card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; }}
.viz .kpi-card .num {{ font-size:32px; font-weight:800; }}
.viz .kpi-card .label {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; margin-top:2px; }}
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
      &nbsp;|&nbsp; Generated {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>
    <div class="hero-pills">
      <div class="pill"><div><div class="cap">Test cases</div><div class="val">{len(verdicts) - len(extra)} plan cases &middot; {len(items)} executions</div>
        {f'<span class="chip">+{len(extra)} extra evidence test(s): {esc(", ".join(extra))}</span>' if extra else ''}
        <span class="chip">{len(suites)} suites</span><span class="chip">{len(runs)} run(s)</span></div></div>
      <div class="pill"><div><div class="cap">Executed result</div>
        <div class="val" style="color:{'#86efac' if kp['FAIL'] == 0 else '#fecaca'};">{kp['PASS']} / {executed} PASS</div>
        <span class="chip">{rate:.0f}% pass rate (executed)</span></div></div>
      <div class="pill"><div><div class="cap">Not executed</div><div class="val">{kp['BLOCKED']} blocked &middot; {kp['SKIP']} skipped</div>
        <span class="chip">blocked = datapath pre-flight refused traffic</span></div></div>
    </div>
  </div>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="num" style="color:var(--accent);">{len(verdicts)}</div><div class="label">Test cases</div></div>
    <div class="kpi-card"><div class="num" style="color:var(--pass);">{kp['PASS']}</div><div class="label">Passed</div></div>
    <div class="kpi-card"><div class="num" style="color:var(--fail);">{kp['FAIL']}</div><div class="label">Failed</div></div>
    <div class="kpi-card"><div class="num" style="color:var(--blocked);">{kp['BLOCKED']}</div><div class="label">Blocked</div></div>
    <div class="kpi-card"><div class="num" style="color:var(--skip);">{kp['SKIP']}</div><div class="label">Skipped</div></div>
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

    h.append(f"""<script>
(function () {{
  if (typeof Chart === 'undefined') return;
  Chart.defaults.color = '#94a3b8'; Chart.defaults.borderColor = '#334155';
  Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";
  var P='#22c55e', F='#ef4444', B='#f97316', S='#f59e0b';
  new Chart(document.getElementById('vizDoughnut'), {{ type: 'doughnut',
    data: {{ labels: ['Pass','Fail','Blocked','Skipped'], datasets: [{{ data: {json.dumps([kp['PASS'], kp['FAIL'], kp['BLOCKED'], kp['SKIP']])},
      backgroundColor: [P,F,B,S], borderWidth: 0, hoverOffset: 8 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false, cutout: '70%', plugins: {{ legend: {{ position: 'bottom', labels: {{ boxWidth: 12 }} }} }} }},
    plugins: [{{ id: 'ct', beforeDraw: function (c) {{ var a=c.chartArea, x=(a.left+a.right)/2, y=(a.top+a.bottom)/2, ctx=c.ctx;
      ctx.save(); ctx.font='bold 24px Segoe UI'; ctx.fillStyle='#e2e8f0'; ctx.textAlign='center'; ctx.textBaseline='middle';
      ctx.fillText('{rate:.0f}%', x, y-8); ctx.font='12px Segoe UI'; ctx.fillStyle='#94a3b8'; ctx.fillText('executed pass', x, y+14); ctx.restore(); }} }}] }});
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
    h.append("<h2>Run Summary</h2><table><thead><tr><th>#</th><th>Suite</th><th class=\"num\">Items</th><th class=\"num\">Pass</th>"
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
        h.append('<table><thead><tr><th>#</th><th>TC ID</th><th>Test</th><th>Description</th><th class="num">Duration</th><th>Status</th></tr></thead><tbody>')
        for j, r in enumerate(rows, 1):
            label, cls = STATUS.get(r["outcome"], (r["outcome"], ""))
            reason = ""
            if r["outcome"] in ("skipped",) and r.get("message"):
                reason = f'<div class="reason">{esc(r["message"].removeprefix("Skipped: ")[:300])}</div>'
            h.append(f'<tr><td>{j}</td><td class="mono">{esc(r["tc_id"])}</td><td class="mono">{esc(r["test"])}</td>'
                     f'<td class="desc">{esc(r.get("description"))}{reason}</td><td class="num">{fmt_dur(r["duration_s"])}</td>'
                     f'<td><span class="{cls}">{label}</span></td></tr>')
            log = r.get("_log", "").rstrip()
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
            h.append(f'<tr class="logrow"><td class="logcell" colspan="6"><details class="logbox"><summary>Command log '
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
    args = ap.parse_args()
    items = load_runs(args.runs)
    args.output.write_text(build(items, args))
    v = Counter(plan_verdicts(items).values())
    print(f"{args.output}: {len(items)} items, {dict(v)}, {args.output.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
