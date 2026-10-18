"""pytest plugin: inventory loading, per-node parametrization, test-plan ID tracking, results export."""
from __future__ import annotations

import csv
import json
import logging
import sys
import os
import time
from collections import defaultdict, deque
from pathlib import Path

import pytest

from .inventory import load_inventory
from .rack import Checks, Rack

DEFAULT_COLLECTIVES = ["all_reduce", "all_gather", "reduce_scatter", "alltoall", "broadcast"]

MARKERS = [
    "tc(id): test-plan case ID from Helios_ScaleUp_TestPlan (e.g. TC-SWH-002)",
    "l1: level-1 (smoke) test",
    "l2: level-2 test",
    "l3: level-3 test",
    "disruptive: changes rack state (link/agent/controller); needs --allow-disruptive",
    "stress: loads the device under test but restores it afterwards",
    "manual: needs a physical/thermal stimulus; needs --run-manual",
    "datapath(tools=[], min_computes=1): what the datapath pre-flight must find on each tray "
    "(tools: transferbench, rccl_tests, rccl_smoke) and how many healthy trays the test needs",
    "healthcheck(gate): part of the full-rack health check (helios/healthcheck.py); gate = blocker | warn. "
    "Select with -m healthcheck or run tools/rack_health.py",
]


def pytest_addoption(parser):
    g = parser.getgroup("helios")
    g.addoption("--inventory", default=os.environ.get("HELIOS_INVENTORY"),
                help="rack inventory YAML (or $HELIOS_INVENTORY)")
    g.addoption("--tc", default=None, help="comma-separated test-plan IDs to run, e.g. TC-SWH-002,TC-AFM-001")
    g.addoption("--switches", default=None, help="comma-separated subset of switch names")
    g.addoption("--computes", default=None, help="comma-separated subset of compute names")
    g.addoption("--allow-disruptive", action="store_true", help="run tests marked disruptive")
    g.addoption("--run-manual", action="store_true", help="run tests marked manual")
    g.addoption("--results-dir", default=None,
                help="folder that gets this run's <rack>-<timestamp>/ results folder (default results/runs/<rack>)")


def pytest_configure(config):
    # paramiko's DEBUG handshake chatter drowns the command trace in --log-file
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    # the per-test command trace (logs/*.log) needs DEBUG records regardless of pytest's log levels
    logging.getLogger("helios.cmd").setLevel(logging.DEBUG)
    for m in MARKERS:
        config.addinivalue_line("markers", m)
    path = config.getoption("--inventory")
    config._helios_inventory = load_inventory(path) if path else None
    config._helios_reports = []


def _subset(names: list[str], opt: str | None) -> list[str]:
    if not opt:
        return names
    wanted = [n.strip() for n in opt.split(",") if n.strip()]
    unknown = sorted(set(wanted) - set(names))
    if unknown:
        raise pytest.UsageError(f"unknown node(s) {unknown}; inventory has {names}")
    return [n for n in names if n in wanted]


def pytest_generate_tests(metafunc):
    inv = metafunc.config._helios_inventory
    if "switch_name" in metafunc.fixturenames:
        names = [h.name for h in inv.switches] if inv else []
        metafunc.parametrize("switch_name", _subset(names, metafunc.config.getoption("--switches")))
    if "collective" in metafunc.fixturenames:
        colls = (inv.cmd("rccl.collectives") if inv else None) or DEFAULT_COLLECTIVES
        metafunc.parametrize("collective", colls)


def tc_id(item) -> str | None:
    m = item.get_closest_marker("tc")
    return m.args[0] if m else None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Plan cases that belong to the health check get the healthcheck marker before -m / --tc filtering."""
    from .healthcheck import CASES

    for item in items:
        tc = tc_id(item)
        if tc in CASES and not item.get_closest_marker("healthcheck"):
            item.add_marker(pytest.mark.healthcheck(CASES[tc][0]))
    _filter_items(config, items)


def _filter_items(config, items):
    wanted = config.getoption("--tc")
    if wanted:
        ids = {t.strip() for t in wanted.split(",") if t.strip()}
        keep = [i for i in items if tc_id(i) in ids]
        config.hook.pytest_deselected(items=[i for i in items if i not in keep])
        items[:] = keep
    for item in items:
        if item.get_closest_marker("disruptive") and not config.getoption("--allow-disruptive"):
            item.add_marker(pytest.mark.skip(reason="disruptive: pass --allow-disruptive"))
        if item.get_closest_marker("manual") and not config.getoption("--run-manual"):
            item.add_marker(pytest.mark.skip(reason="manual: needs a physical/thermal stimulus; pass --run-manual"))


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="session")
def inventory(pytestconfig):
    inv = pytestconfig._helios_inventory
    if inv is None:
        pytest.skip("no rack inventory: pass --inventory or set HELIOS_INVENTORY")
    return inv


@pytest.fixture(scope="session")
def rack(inventory):
    r = Rack(inventory)
    yield r
    r.close()


@pytest.fixture
def switch(rack, switch_name):
    return rack.switches[switch_name]


@pytest.fixture
def computes(rack, pytestconfig):
    names = _subset(list(rack.computes), pytestconfig.getoption("--computes"))
    if not names:
        pytest.skip("no computes in inventory")
    return [rack.computes[n] for n in names]


@pytest.fixture
def datapath_ready(rack, computes, inventory, pytestconfig, request):
    """Datapath pre-flight (helios/preflight.py) before any IFoE traffic. Unhealthy trays - unreachable, GPUs not
    ACTIVE / not openable / busy, a tool the test declares missing, a black-holed port - are REMOVED from this
    test's ``computes`` (the same list object the test receives) and recorded as ``excluded_trays``; the test is
    blocked ("pre-flight failed") only for rack-wide problems or when fewer than ``min_computes`` trays remain.
    Cached per (trays, tools) for the session. Disable: thresholds.datapath.preflight.enabled: false."""
    if inventory.th("datapath.preflight.enabled", True) is False:
        return
    from . import preflight

    m = request.node.get_closest_marker("datapath")
    tools = tuple(sorted((m.kwargs.get("tools") if m else None) or ()))
    need = int((m.kwargs.get("min_computes") if m else None) or 1)
    cache = pytestconfig.__dict__.setdefault("_helios_preflight", {})
    key = (tuple(sorted(c.name for c in computes)), tools, need)
    if key not in cache:
        cache[key] = preflight.assess(rack, computes, inventory, tools, need)
    a = cache[key]
    if a.evidence:      # failed probe pings (command, rc, afmctl output) - recorded even when the test is blocked
        request.node.user_properties.append(("probe_ping_evidence", a.evidence[:20]))
    if a.problems:
        pytest.fail("datapath pre-flight failed - not starting IFoE traffic (it would hang or be meaningless):\n"
                    + "\n".join(f"  - {p}" for p in a.problems), pytrace=False)
    if a.excluded:
        computes[:] = [c for c in computes if c.name not in a.excluded]
        request.node.user_properties.append(("excluded_trays", dict(sorted(a.excluded.items()))))
        logging.getLogger("helios").warning("datapath pre-flight: %d tray(s) excluded, running on %s:\n%s", len(a.excluded),
                    ",".join(c.name for c in computes), "\n".join(f"  - {v}" for v in a.excluded.values()))


@pytest.fixture
def checks():
    return Checks()


# ----------------------------------------------------------------- results

ITEM_LOG_LIMIT = 600_000  # chars per test log kept for the report: its first and its last half


class _ItemLog(logging.Handler):
    """Collects the helios.cmd command trace (commands, outputs, rc, timings) of one test item. Over ITEM_LOG_LIMIT it
    keeps the start and the END and drops the middle: g02-g17 17 Oct, 20 of 33 disruptive logs were cut while the
    pre-checks read the switch ports, before the test's own shuts and AFM calls (the evidence)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s", "%H:%M:%S"))
        self.lines: list[str] = []
        self.size = 0
        self.tail: deque[str] = deque()
        self.tail_size = self.dropped = 0

    def emit(self, record):
        text = self.format(record)
        if not self.tail and self.size + len(text) <= ITEM_LOG_LIMIT // 2:
            self.lines.append(text)
            self.size += len(text)
            return
        self.tail.append(text)
        self.tail_size += len(text)
        while self.tail_size > ITEM_LOG_LIMIT // 2 and len(self.tail) > 1:
            old = self.tail.popleft()
            self.tail_size -= len(old)
            self.dropped += len(old)

    def text(self) -> str:
        cut = [f"... [{self.dropped:,} chars dropped here: the log keeps its first and last {ITEM_LOG_LIMIT // 2:,} "
               f"chars; full trace in --log-file]"] if self.dropped else []
        return "\n".join(self.lines + cut + list(self.tail))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    cfg = item.config
    if cfg._helios_inventory is None:
        yield
        return
    handler = _ItemLog()
    cmd_log = logging.getLogger("helios.cmd")
    cmd_log.addHandler(handler)
    started = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())   # one clock for runs from the Mac and the jump host
    from .progress import Heartbeat
    try:
        with Heartbeat(item.nodeid):
            yield
    finally:
        cmd_log.removeHandler(handler)
        cfg.__dict__.setdefault("_helios_item_logs", {})[item.nodeid] = (started, handler.text())


def _describe(item) -> tuple[str, str, str]:
    """(suite, test name, first docstring line) for reports."""
    try:
        rel = Path(str(item.path)).resolve().relative_to(Path(str(item.config.rootpath)).resolve() / "tests")
        suite = str(rel.with_suffix("")).replace("\\", "/")
    except ValueError:
        suite = Path(str(item.path)).stem
    doc = (getattr(getattr(item, "obj", None), "__doc__", None) or "").strip().splitlines()
    return suite, item.name, (doc[0].strip() if doc else "")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """'start <test> [TC]' on the live log, under the test's own name (pytest has printed it by now)."""
    if item.config._helios_inventory is None:
        return
    from .progress import log as progress
    tc = next((m.args[0] for m in item.iter_markers("tc")), "")
    progress.info("start %s%s", item.nodeid, f" [{tc}]" if tc else "")


# fixtures through which a test reaches the rack's nodes: with quarantined nodes such a test ran on part of the rack
# (a pure AFM API test - only the 'afm' client - is not affected)
RACK_FIXTURES = {"rack", "computes", "switch", "switch_name", "ops", "afm_ready", "datapath_ready", "links"}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if item.config._helios_inventory is None:
        return
    if rep.when == "call" or (rep.when == "setup" and not rep.passed):
        msg = ""
        if rep.failed:
            msg = rep.longreprtext.strip()[:2000]
        elif rep.skipped and isinstance(rep.longrepr, tuple):
            msg = rep.longrepr[2]
        outcome = rep.outcome
        if rep.when == "setup" and rep.failed:
            # a pre-flight (datapath / RDMA / AFM rack health) refused to start: not a test failure
            outcome = "blocked" if "pre-flight failed" in (rep.longreprtext or "") else "error"
        excluded = dict(item.user_properties).get("excluded_trays")
        if outcome == "passed" and excluded:
            # passed, but only on the trays the pre-flight kept: not a full-rack PASS
            outcome = "partial"
            ran = [getattr(c, "name", str(c)) for c in getattr(item, "funcargs", {}).get("computes") or []]
            msg = partial_message(ran, excluded)
        ch = getattr(item, "funcargs", {}).get("checks")
        if rep.failed and ch is not None and ch.failures and "failures" not in dict(item.user_properties):
            # the full list (the message is cut at 2000 chars): used by the health check and the reports
            item.user_properties.append(("failures", ch.failures[:5000]))
        q = item.config._helios_inventory.quarantine_summary()
        applied = "quarantine_applied" in dict(item.user_properties)   # an AFM-only test that left quarantined nodes out
        if q and (applied or RACK_FIXTURES & set(getattr(item, "fixturenames", ()))):
            # quarantined nodes were left out of this test: record it, and a pass is not a full-rack PASS
            item.user_properties.append(("quarantined", {n: v.get("reason", "") for n, v in q.items()}))
            if outcome == "passed":
                outcome = "partial"
                msg = (msg + "\n" if msg else "PARTIAL - passed, ") + "NOT run on the quarantined node(s) " + \
                    "; ".join(f"{n} ({v.get('reason', '')})" for n, v in q.items()) + \
                    " - lift the quarantine and re-run for a full PASS"
        suite, test, desc = _describe(item)
        item.config._helios_reports.append({
            "tc_id": tc_id(item) or "",
            "nodeid": item.nodeid,
            "suite": suite,
            "test": test,
            "description": desc,
            "outcome": outcome,
            "duration_s": round(rep.duration, 2),
            "properties": json.dumps(dict(item.user_properties)) if item.user_properties else "",
            "message": msg,
        })
        if outcome != "passed":     # the reason on the live log, right after the test (pytest shows it only in -v / the summary)
            from .progress import log as progress
            why = "; ".join(x.strip() for x in (msg or "").splitlines() if x.strip()).replace(":; ", ": ")[:300]
            tc = tc_id(item)
            (progress.warning if outcome == "partial" else progress.info)(
                "%s %s%s%s", outcome, test, f" [{tc}]" if tc else "", f": {why}" if why else "")
        rep.helios_outcome = outcome            # pytest's own status word follows it (pytest_report_teststatus)


def partial_message(ran: list[str], excluded: dict[str, str]) -> str:
    """Why a test is PARTIAL, readable on its own: where it passed, which trays it did not run on and why, what a
    full PASS needs. 17 Oct (MR3 TC-AFM-001): 'passed on the remaining trays; excluded: ping probe: c2/0001...'
    named neither the trays it ran on nor that c2 was never tested."""
    out = sorted(excluded)
    head = (f"PARTIAL - passed on {', '.join(ran)}" if ran else "PARTIAL - passed on the remaining trays") + \
        f"; NOT run on {', '.join(out)}: left out by the datapath pre-flight before the test started"
    why = [f"  {n}: {excluded[n].removeprefix(f'{n}: ')}" for n in out]   # reasons may already start 'c2: '

    return "\n".join([head + ":", *why, f"A full PASS needs {', '.join(out)} fixed and the case re-run."])


@pytest.hookimpl(tryfirst=True)
def pytest_report_teststatus(report, config):
    """The console says PARTIAL (yellow) instead of PASSED for a test that passed only on the trays it kept."""
    if report.when == "call" and getattr(report, "helios_outcome", None) == "partial":
        return "partial", "P", ("PARTIAL", {"yellow": True})


SUMMARY_FIELDS = ["tc_id", "verdict", "passed", "failed", "skipped", "error", "blocked", "partial"]


def tc_verdict(outs: list[str]) -> str:
    """The verdict of one plan case from the outcomes of its test items (tc_summary.csv)."""
    n = {k: outs.count(k) for k in ("passed", "failed", "skipped", "error", "blocked", "partial")}
    return ("FAIL" if n["failed"] or n["error"] else
            "BLOCKED" if n["blocked"] else
            "PARTIAL" if n["partial"] else
            "PASS" if n["passed"] and not n["skipped"] else
            "SKIP" if not n["passed"] else "PARTIAL")


def summary_rows(rows) -> list[list]:
    """tc_summary.csv rows (after the SUMMARY_FIELDS header) from tc_results.csv rows (tc_id, outcome)."""
    by_tc: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        by_tc[r["tc_id"] or "(untagged)"].append(r["outcome"])
    out = []
    for tc, outs in sorted(by_tc.items()):
        out.append([tc, tc_verdict(outs), *(outs.count(k) for k in ("passed", "failed", "skipped", "error", "blocked",
                                                                     "partial"))])
    return out


def write_summary(path: Path, rows) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SUMMARY_FIELDS)
        w.writerows(summary_rows(rows))


def pytest_sessionfinish(session):
    cfg = session.config
    reports = getattr(cfg, "_helios_reports", None)
    if not reports or cfg._helios_inventory is None:
        return
    rack = cfg._helios_inventory.rack
    res = Path(os.environ["HELIOS_RESULTS_ROOT"]).expanduser() if os.environ.get("HELIOS_RESULTS_ROOT") \
        else Path("results")                    # an installed UI keeps its results outside the code
    base = Path(cfg.getoption("--results-dir") or res / "runs" / rack)
    out = base / f"{rack}-{time.strftime('%Y%m%d-%H%M%S')}"
    n = 1
    while out.exists():   # two runs ending in the same second (g02-g17 18 Oct 04:47: one overwrote the other's results)
        n += 1
        out = base / f"{rack}-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
    out.mkdir(parents=True)
    # one command-trace file per test item, referenced from tc_results.csv (used by tools/html_report.py)
    logs = cfg.__dict__.get("_helios_item_logs", {})
    (out / "logs").mkdir(exist_ok=True)
    for i, r in enumerate(reports, start=1):
        started, text = logs.get(r["nodeid"], ("", ""))
        name = f"{i:03d}_" + "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in r["nodeid"].split("::")[-1])[:80] + ".log"
        (out / "logs" / name).write_text(text + "\n" if text else "(no remote commands)\n")
        r["started"], r["log"] = started, f"logs/{name}"
    with open(out / "tc_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(reports[0]))
        w.writeheader()
        w.writerows(reports)

    write_summary(out / "tc_summary.csv", reports)
    session.config._helios_results_dir = out
    try:        # results/index.csv (tools/results_index.py) - only when the run sits in <results>/runs/<rack>/
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from results_index import update_run
        update_run(out)
    except Exception as e:  # noqa: BLE001 - the index must never break a run
        logging.getLogger("helios").warning("results index not updated: %s", e)


def pytest_terminal_summary(terminalreporter, config):
    out = getattr(config, "_helios_results_dir", None)
    if out:
        terminalreporter.write_sep("-", f"helios results: {out}/tc_summary.csv")
