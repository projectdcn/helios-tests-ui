"""pytest plugin: inventory loading, per-node parametrization, test-plan ID tracking, results export."""
from __future__ import annotations

import csv
import json
import os
import time
from collections import defaultdict
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
    g.addoption("--results-dir", default="results", help="where tc_results.csv / tc_summary.csv go")


def pytest_configure(config):
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


def pytest_collection_modifyitems(config, items):
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
def checks():
    return Checks()


# ----------------------------------------------------------------- results

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
        item.config._helios_reports.append({
            "tc_id": tc_id(item) or "",
            "nodeid": item.nodeid,
            "outcome": "error" if rep.when == "setup" and rep.failed else rep.outcome,
            "duration_s": round(rep.duration, 2),
            "properties": json.dumps(dict(item.user_properties)) if item.user_properties else "",
            "message": msg,
        })


def pytest_sessionfinish(session):
    cfg = session.config
    reports = getattr(cfg, "_helios_reports", None)
    if not reports or cfg._helios_inventory is None:
        return
    out = Path(cfg.getoption("--results-dir")) / f"{cfg._helios_inventory.rack}-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "tc_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(reports[0]))
        w.writeheader()
        w.writerows(reports)

    by_tc: dict[str, list[str]] = defaultdict(list)
    for r in reports:
        by_tc[r["tc_id"] or "(untagged)"].append(r["outcome"])
    with open(out / "tc_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tc_id", "verdict", "passed", "failed", "skipped", "error"])
        for tc, outs in sorted(by_tc.items()):
            n = {k: outs.count(k) for k in ("passed", "failed", "skipped", "error")}
            verdict = ("FAIL" if n["failed"] or n["error"] else
                       "PASS" if n["passed"] and not n["skipped"] else
                       "SKIP" if not n["passed"] else "PARTIAL")
            w.writerow([tc, verdict, n["passed"], n["failed"], n["skipped"], n["error"]])
    session.config._helios_results_dir = out


def pytest_terminal_summary(terminalreporter, config):
    out = getattr(config, "_helios_results_dir", None)
    if out:
        terminalreporter.write_sep("-", f"helios results: {out}/tc_summary.csv")
