"""Metrics validation with metrics charts (L2, non-disruptive):

  TC-AFM-L2-57  Create Metrics and validate the data: one chart per metric category (kind) - Cluster,
                SwitchPortStats and every kind of thresholds.afm.metrics.extra_kinds ("Kind.Field") - and the data
                of each chart's query matches the rack: Cluster Admitted/Healthy Compute/Switch node counts = the AFM
                REST node lists and their status (admission-phase admitted, healthy condition true); SwitchPortStats
                of one switch: every field has series (KI-F07-005 fields excluded) and the Octets/Packets counters do
                not decrease between two reads; extra kinds: series present.
  TC-AFM-L2-58  Edit the existing chart: the Cluster chart AdmittedComputeNodes/last edited to HealthyComputeNodes/max;
                the read-back holds the edit, last(HealthyComputeNodes) = healthy computes and max >= last.
  TC-AFM-L2-59  Edit the chart's start/end time: the same field over the last 1 h, the last 15 min and an absolute
                RFC3339 window (35 to 5 min ago): every sample is inside its window, 15 min has <= the samples of 1 h,
                and an end-time before the start-time (relative and absolute) is rejected (4xx) or returns no data.
  TC-AFM-L2-60  Delete operation: DELETE the chart -> 2xx, then GET 404, gone from the list, a second DELETE 404;
                the metrics data itself is still there.

Charts are an AFM UI feature whose REST collection is not documented (API groups: preferences or objstore?). Set
thresholds.afm.metrics.chart_api: {path: <collection path>, kind: <object kind, default MetricsChart>,
api_version: <default v1>} to exercise create / read / edit / delete. A chart's spec is assumed to be the telemetry
query it plots ({kind, fields, function, start-time, end-time, sort-order, pagination[, selectors, group-by-field]});
the read-back compares only those keys. Without chart_api, L2-57..59 run the same telemetry validation and record
"chart_api not configured: chart CRUD not exercised"; L2-60 skips.

Thresholds (thresholds.afm.metrics.*): chart_api (unset), known_no_series (the KI-F07-005 list), extra_kinds ([]),
settle_s (120: how long the Cluster metrics may lag the node lists), counter_interval_s (30: between the two counter
reads), window_field ("Cluster.AdmittedComputeNodes"), window_function ("none": every sample of the window),
window_max_points (500), clock_skew_s (60: runner vs controller clock for the relative windows); the times are
scaled by thresholds.afm.time_scale. Charts are throw-away helios-* objects deleted on teardown.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from afm_helpers import PORT_FIELDS, PORT_KNOWN_NO_SERIES, _metric_raw, _metric_series, _metric_spec
from helios.afm import conditions_ok, wait_for

TRUTH_FIELDS = ["AdmittedComputeNodes", "AdmittedSwitchNodes", "HealthyComputeNodes", "HealthySwitchNodes"]
COUNTER_FIELDS = ["InOctets", "OutOctets", "InPackets", "OutPackets"]
QUERY_KEYS = ("kind", "fields", "function", "start-time", "end-time", "sort-order", "pagination", "selectors",
              "group-by-field")
NO_CHART_API = "chart_api not configured: chart CRUD not exercised"


def _th(inventory, key: str, default):
    v = inventory.th(f"afm.metrics.{key}", default)
    return default if v is None else v


def _scale(inventory) -> float:
    return float(inventory.th("afm.time_scale", 1) or 1)


def _ts(v) -> datetime | None:
    """A sample timestamp: RFC3339 (any fraction digits) or epoch s / ms / us / ns."""
    if isinstance(v, (int, float)):
        x = float(v)
        while x > 1e11:
            x /= 1000
        return datetime.fromtimestamp(x, timezone.utc)
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", str(v or ""))
    if not m:
        return None
    tz = m.group(3) or "Z"
    return datetime.fromisoformat(f"{m.group(1)}.{(m.group(2) or '')[:6].ljust(6, '0')}{'+00:00' if tz == 'Z' else tz}")


def _rfc3339(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(expr: str) -> timedelta | None:
    """'now() - 15m' -> 15 min, 'now()' -> 0; None for an absolute time."""
    m = re.fullmatch(r"now\(\)\s*(?:-\s*(\d+)\s*([smhd]))?", str(expr).strip())
    if not m:
        return None
    return timedelta(seconds=int(m.group(1) or 0) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2) or "s"])


def _rows(series: list[dict]) -> list[tuple]:
    """[(timestamp, value)] of every sample of every series."""
    return [(_ts(r[0]), r[1]) for s in series for r in s.get("values") or [] if r]


def _newest(series: list[dict]):
    rows = [r for r in _rows(series) if r[0] is not None]
    return max(rows, key=lambda r: r[0])[1] if rows else None


def _by_series(series: list[dict]) -> dict:
    """{series tags: newest value}"""
    return {tuple(sorted((s.get("tags") or {}).items())): _newest([s]) for s in series}


def _one(spec: dict, field: str, **kw) -> dict:
    return {**{k: v for k, v in spec.items() if k in QUERY_KEYS}, "fields": [field], **kw}


# ------------------------------------------------------------------ ground truth

def _truth(afm) -> dict:
    """{Admitted|Healthy Compute|Switch Nodes: count} from the AFM REST node lists."""
    out = {}
    for what, nodes in (("Compute", afm.computes()), ("Switch", afm.switches())):
        adm = [n for n in nodes if (n.get("status") or {}).get("admission-phase") == "admitted"]
        out[f"Admitted{what}Nodes"] = len(adm)
        out[f"Healthy{what}Nodes"] = sum(1 for n in adm if conditions_ok(n))
    return out


def _check_cluster(afm, inventory, spec, checks) -> dict:
    """Newest value of each field (function last); the TRUTH_FIELDS must equal the node lists within settle_s
    (metrics are sampled, so a node change shows up late)."""
    got: dict = {}

    def matches():
        truth = _truth(afm)
        got.clear()
        for f in spec["fields"]:
            got[f] = _newest(_metric_series(afm, _one(spec, f, function="last")))
        return all(got[f] == truth[f] for f in spec["fields"] if f in truth)
    settle = float(_th(inventory, "settle_s", 120)) * _scale(inventory)
    wait_for(matches, settle, max(0.2, 10 * _scale(inventory)), "Cluster metrics = AFM node lists")
    truth = _truth(afm)
    for f in spec["fields"]:
        if not checks.check(got.get(f) is not None, f"Cluster.{f}: no series"):
            continue
        if f in truth:
            checks.check(got[f] == truth[f], f"Cluster.{f} = {got[f]}, the AFM node lists say {truth[f]} "
                                             f"(still after {settle:.0f}s)")
    return {"metrics": dict(got), "afm_lists": truth}


def _check_ports(afm, inventory, spec, checks) -> dict:
    """Every field has series; the counters do not decrease between two reads counter_interval_s apart."""
    rep = ",".join(r.get("values", [""])[0] for s in spec.get("selectors") or [] for r in s.get("requirements") or [])
    first = {f: _metric_series(afm, _one(spec, f)) for f in spec["fields"]}
    out = {f: f"{len(s)} series" for f, s in first.items()}
    for f, s in first.items():
        checks.check(s, f"SwitchPortStats.{f} ({rep}): no series")
    time.sleep(float(_th(inventory, "counter_interval_s", 30)) * _scale(inventory))
    for f in [f for f in COUNTER_FIELDS if f in spec["fields"] and first[f]]:
        a, b = _by_series(first[f]), _by_series(_metric_series(afm, _one(spec, f)))
        gone = sorted(set(a) - set(b))
        dec = [f"{dict(k).get('name', k)}: {a[k]} -> {b[k]}" for k in a if k in b and None not in (a[k], b[k])
               and b[k] < a[k]]
        checks.check(not gone, f"SwitchPortStats.{f} ({rep}): {len(gone)} series gone in the second read: {gone[:4]}")
        checks.check(not dec, f"SwitchPortStats.{f} ({rep}): counter decreased: {dec[:4]}")
        out[f] += f", {len(dec)} decreasing"
    return out


def _check_other(afm, spec, checks) -> dict:
    out = {}
    for f in spec["fields"]:
        s = _metric_series(afm, _one(spec, f))
        out[f] = f"{len(s)} series"
        checks.check(s, f"{spec['kind']}.{f}: no series")
    return out


def _check_data(afm, inventory, spec, checks) -> dict:
    if spec["kind"] == "Cluster":
        return _check_cluster(afm, inventory, spec, checks)
    if spec["kind"] == "SwitchPortStats":
        return _check_ports(afm, inventory, spec, checks)
    return _check_other(afm, spec, checks)


def _categories(afm, inventory) -> dict[str, dict]:
    """{category: chart query} - one per metric kind."""
    known = set(_th(inventory, "known_no_series", PORT_KNOWN_NO_SERIES) or [])
    out = {"cluster": _metric_spec("Cluster", TRUTH_FIELDS)}
    sw = afm.switches()
    if sw:
        out["switchportstats"] = _metric_spec(
            "SwitchPortStats", [f for f in PORT_FIELDS if f not in known],
            [{"key": "reporterID", "operator": "in", "values": [sw[0]["meta"]["name"]]}],
            group_by_field="reporterID,name", count=1)
    for kf in _th(inventory, "extra_kinds", []) or []:
        k, f = kf.split(".", 1)
        q = out.setdefault(k.lower(), _metric_spec(k, []))
        q["fields"].append(f)
    return out


# ------------------------------------------------------------------ chart objects

def _chart_api(inventory) -> dict | None:
    c = _th(inventory, "chart_api", None)
    if isinstance(c, str):
        c = {"path": c}
    if not c or not c.get("path"):
        return None
    return {"path": c["path"].rstrip("/"), "kind": c.get("kind") or "MetricsChart",
            "api-version": c.get("api_version") or "v1"}


def _chart_names(afm, api) -> list[str]:
    return [(o.get("meta") or {}).get("name") for o in afm.items(api["path"])]


def _read_chart(afm, api, name, want, checks, when) -> dict | None:
    """The stored chart when its spec holds ``want``'s query keys, else None (reported)."""
    r = afm.raw("GET", f"{api['path']}/{name}")
    if not checks.check(r.status_code == 200, f"GET chart {name} {when} -> HTTP {r.status_code}: {r.text[:200]}"):
        return None
    got = r.json() or {}
    spec = got.get("spec") or {}
    diff = {k: {"want": v, "got": spec.get(k)} for k, v in want.items() if spec.get(k) != v}
    checks.check((got.get("meta") or {}).get("name") == name, f"GET chart {name} {when}: meta.name "
                                                              f"{(got.get('meta') or {}).get('name')!r}")
    return got if checks.check(not diff, f"chart {name} {when}: spec does not round-trip: {diff}") else None


def _create_chart(afm, api, name, spec, cleanup, checks) -> dict | None:
    cleanup(f"{api['path']}/{name}")
    r = afm.raw("POST", api["path"], {"kind": api["kind"], "api-version": api["api-version"], "meta": {"name": name},
                                      "spec": spec})
    if not checks.check(r.status_code < 300, f"create chart {name} -> HTTP {r.status_code}: {r.text[:200]}"):
        return None
    got = _read_chart(afm, api, name, spec, checks, "after create")
    checks.check(name in _chart_names(afm, api), f"chart {name} created but not in the list {api['path']}")
    return got


def _edit_chart(afm, api, name, obj, spec, checks, what) -> dict | None:
    r = afm.raw("PUT", f"{api['path']}/{name}", {"kind": obj.get("kind") or api["kind"],
                                                 "api-version": obj.get("api-version") or api["api-version"],
                                                 "meta": obj.get("meta") or {"name": name}, "spec": spec})
    if not checks.check(r.status_code < 300, f"edit chart {name} ({what}) -> HTTP {r.status_code}: {r.text[:200]}"):
        return None
    got = _read_chart(afm, api, name, spec, checks, f"after the {what} edit")
    n = _chart_names(afm, api).count(name)
    checks.check(n == 1, f"chart {name} listed {n} times after the {what} edit")
    return got


# ------------------------------------------------------------------ tests

@pytest.mark.l2
@pytest.mark.tc("TC-AFM-L2-57")
def test_metrics_chart_create(afm, inventory, uniq, cleanup, checks, record_property):
    """One chart per metric category (Cluster, SwitchPortStats, extra kinds); each round-trips and its query's data
    matches the rack (node lists / status, port counters with series and not decreasing)."""
    api = _chart_api(inventory)
    seen = {}
    for cat, spec in _categories(afm, inventory).items():
        if api:
            name = uniq(f"chart-{cat}")
            obj = _create_chart(afm, api, name, spec, cleanup, checks)
            if obj is None:
                continue
            spec = obj["spec"]
            seen[f"{cat} chart"] = name
        seen[cat] = _check_data(afm, inventory, spec, checks)
    if not api:
        record_property("note", NO_CHART_API)
    record_property("categories", seen)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-L2-58")
def test_metrics_chart_edit(afm, inventory, uniq, cleanup, checks, record_property):
    """Cluster chart AdmittedComputeNodes/last edited to HealthyComputeNodes/max: the read-back holds the edit and
    its data is right (last = healthy computes in the node lists, max >= last)."""
    api = _chart_api(inventory)
    spec = _metric_spec("Cluster", "AdmittedComputeNodes")
    edited = {**spec, "fields": ["HealthyComputeNodes"], "function": "max"}
    ev = {}
    if api:
        name = uniq("chart-cluster")
        obj = _create_chart(afm, api, name, spec, cleanup, checks)
        if obj is None:
            checks.assert_all()
        ev["before_edit"] = _check_data(afm, inventory, obj["spec"], checks)
        new = _edit_chart(afm, api, name, obj, edited, checks, "field/function")
        if new is None:
            checks.assert_all()
        edited = new["spec"]
        ev["chart"] = name
    else:
        record_property("note", NO_CHART_API)
    ev["after_edit_last"] = _check_cluster(afm, inventory, {**edited, "function": "last"}, checks)
    mx = _newest(_metric_series(afm, _one(edited, "HealthyComputeNodes")))
    last = ev["after_edit_last"]["metrics"].get("HealthyComputeNodes")
    ev["after_edit_max"] = mx
    if checks.check(mx is not None, "edited chart (HealthyComputeNodes, max): no data") and last is not None:
        checks.check(mx >= last, f"edited chart: max(HealthyComputeNodes) {mx} < last {last}")
    record_property("edit", ev)
    checks.assert_all()


def _window_check(afm, inventory, label, q, checks) -> dict:
    """Samples of q; every timestamp must be inside q's window (relative windows on the runner clock +- clock_skew_s,
    absolute ones +- 1 s)."""
    t0 = datetime.now(timezone.utc)
    rows = _rows(_metric_series(afm, q))
    t1 = datetime.now(timezone.utc)
    skew = timedelta(seconds=float(_th(inventory, "clock_skew_s", 60)))
    rs, re_ = _rel(q["start-time"]), _rel(q["end-time"])
    lo = t0 - rs - skew if rs is not None else _ts(q["start-time"]) - timedelta(seconds=1)
    hi = t1 - re_ + skew if re_ is not None else _ts(q["end-time"]) + timedelta(seconds=1)
    bad_ts = [r[0] for r in rows if r[0] is None]
    out = [_rfc3339(r[0]) for r in rows if r[0] is not None and not lo <= r[0] <= hi]
    checks.check(not bad_ts, f"{label}: {len(bad_ts)} samples without a parseable timestamp")
    checks.check(not out, f"{label} [{q['start-time']} .. {q['end-time']}]: {len(out)} of {len(rows)} samples outside "
                          f"the window ({_rfc3339(lo)} .. {_rfc3339(hi)}): {out[:4]}")
    ts = sorted(r[0] for r in rows if r[0] is not None)
    return {"window": [q["start-time"], q["end-time"]], "samples": len(rows), "outside": len(out),
            "first": _rfc3339(ts[0]) if ts else None, "last": _rfc3339(ts[-1]) if ts else None}


def _reversed_rejected(afm, label, q, checks) -> str:
    """end-time before start-time: 4xx, or 200 with no samples."""
    r = _metric_raw(afm, [q])
    rows = []
    if r.status_code == 200:
        rows = _rows(((r.json() or {}).get("results") or [{}])[0].get("series") or [])
    checks.check(400 <= r.status_code < 500 or (r.status_code == 200 and not rows),
                 f"{label} end-time {q['end-time']} before start-time {q['start-time']} -> HTTP {r.status_code}, "
                 f"{len(rows)} samples (expected 4xx or no data)")
    return f"HTTP {r.status_code}, {len(rows)} samples"


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-L2-59")
def test_metrics_chart_time_range(afm, inventory, uniq, cleanup, checks, record_property):
    """The chart's start/end time edited (1 h -> 15 min -> an absolute RFC3339 window): every sample inside the window,
    15 min <= 1 h samples, end-time before start-time rejected (4xx) or empty."""
    api = _chart_api(inventory)
    kind, field = str(_th(inventory, "window_field", "Cluster.AdmittedComputeNodes")).split(".", 1)
    base = _metric_spec(kind, field, window="1h", function=_th(inventory, "window_function", "none"),
                        count=int(_th(inventory, "window_max_points", 500)))
    end = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    windows = {"1h": base, "15m": {**base, "start-time": "now() - 15m"},
               "absolute": {**base, "start-time": _rfc3339(end - timedelta(minutes=30)), "end-time": _rfc3339(end)}}
    ev = {}
    if api:
        name = uniq("chart-window")
        obj = _create_chart(afm, api, name, windows["1h"], cleanup, checks)
        if obj is None:
            checks.assert_all()
        for w in ("15m", "absolute"):
            new = _edit_chart(afm, api, name, obj, windows[w], checks, f"start/end time -> {w}")
            if new is not None:
                obj = new
                windows[w] = new["spec"]
        ev["chart"] = name
    else:
        record_property("note", NO_CHART_API)
    for w, q in windows.items():
        ev[w] = _window_check(afm, inventory, f"{kind}.{field} {w}", q, checks)
    checks.check(ev["1h"]["samples"] > 0, f"{kind}.{field}: no samples in the last hour - nothing to validate")
    checks.check(ev["15m"]["samples"] <= ev["1h"]["samples"],
                 f"{kind}.{field}: last 15 min has {ev['15m']['samples']} samples, more than the last hour's "
                 f"{ev['1h']['samples']}")
    ev["reversed_relative"] = _reversed_rejected(afm, f"{kind}.{field}",
                                                 {**base, "start-time": "now()", "end-time": "now() - 1h"}, checks)
    ev["reversed_absolute"] = _reversed_rejected(
        afm, f"{kind}.{field}", {**base, "start-time": windows["absolute"]["end-time"],
                                 "end-time": windows["absolute"]["start-time"]}, checks)
    record_property("windows", ev)
    checks.assert_all()


@pytest.mark.l2
@pytest.mark.tc("TC-AFM-L2-60")
def test_metrics_chart_delete(afm, inventory, uniq, cleanup, checks, record_property):
    """A chart deleted: DELETE 2xx, GET 404, not in the list, second DELETE 404; the metrics data stays."""
    api = _chart_api(inventory)
    if not api:
        pytest.skip("thresholds.afm.metrics.chart_api not configured ({path: <chart collection>, kind: <object kind>}):"
                    " chart delete not exercised")
    name, spec = uniq("chart-cluster"), _metric_spec("Cluster", TRUTH_FIELDS)
    if _create_chart(afm, api, name, spec, cleanup, checks) is None:
        checks.assert_all()
    path = f"{api['path']}/{name}"
    codes = {"delete": afm.raw("DELETE", path).status_code}
    checks.check(codes["delete"] < 300, f"DELETE chart {name} -> HTTP {codes['delete']}")
    codes["get_after"] = afm.raw("GET", path).status_code
    checks.check(codes["get_after"] == 404, f"GET chart {name} after delete -> HTTP {codes['get_after']}, expected 404")
    checks.check(name not in _chart_names(afm, api), f"chart {name} still listed in {api['path']} after delete")
    codes["delete_again"] = afm.raw("DELETE", path).status_code
    checks.check(codes["delete_again"] == 404, f"second DELETE of chart {name} -> HTTP {codes['delete_again']}, "
                                               f"expected 404")
    record_property("codes", codes)
    record_property("data_after_delete", _check_cluster(afm, inventory, spec, checks))
    checks.assert_all()
