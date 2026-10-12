"""TC-CTL-139..150 — AMD device-metrics-exporter on every compute tray.

Scraped with curl *on the compute* (localhost), so the runner needs no route to
port 5000. Metric names vary between exporter releases, so each family is a
regex (``thresholds.metrics.families`` overrides the defaults below).
"""
from __future__ import annotations

import math
import re
import shlex

import pytest

from helios.parsers import parse_prometheus, split_http_response
from helios.rack import fan_out
from helios.wait import wait_until

pytestmark = pytest.mark.l3

FAMILIES = {
    "power": r"^gpu_(package_)?power(_usage)?$",
    "temperature": r"^gpu_\w*temperature$",
    "utilization": r"^gpu_(gfx_)?(activity|utilization|busy\w*)$",
    "vram_total": r"^gpu_total_vram$",
    "vram_used": r"^gpu_used_vram$",
    "vram_free": r"^gpu_free_vram$",
    "ecc_total": r"^gpu_ecc_(un)?correct_total$",
    "ecc_uncorrectable": r"^gpu_ecc_uncorrect_total$",
    "ecc_block": r"^gpu_ecc_(un)?correct_(?!total$)\w+$",
    "pcie_bandwidth": r"pcie\w*bandwidth",
    "pcie_speed": r"pcie\w*speed",
    "pcie_width": r"pcie\w*width",
    "pcie_errors": r"pcie\w*(error|replay|nak|nack|recover)\w*",
    "xgmi_throughput": r"xgmi\w*(throughput|bandwidth)",
    "xgmi_beats": r"xgmi\w*beats?",
    "nic_frames": r"^(nic|eth)_\w*(frames|pkts|packets)\w*",
    "nic_bytes": r"^(nic|eth)_\w*bytes\w*",
    "nic_lif": r"lif",
    "rdma_req": r"rdma\w*req",
    "rdma_resp": r"rdma\w*resp",
    "rdma_cnp": r"cnp",
    "rdma_ecn": r"ecn",
    "rdma_qp": r"(^|_)qps?(_|$)",
}
PER_GPU = {"power", "temperature", "utilization", "vram_total", "vram_used", "vram_free", "ecc_total"}


def _cfg(inventory, key, default):
    v = inventory.cmd(f"metrics_exporter.{key}")
    return default if v is None else v


def _families(inventory) -> dict[str, re.Pattern]:
    merged = {**FAMILIES, **(inventory.th("metrics.families") or {})}
    return {k: re.compile(v) for k, v in merged.items()}


def _gpu_label(inventory) -> str:
    return inventory.th("metrics.gpu_label", "gpu_id")


def _scrape(c, inventory):
    port = _cfg(inventory, "port", 5000)
    url = _cfg(inventory, "url", "http://localhost:{port}/metrics").format(port=port)
    r = c.run(f"curl -s -m 15 -D - {shlex.quote(url)}", timeout=30)
    if not r.ok:
        return None, {}, [], r
    status, headers, body = split_http_response(r.stdout)
    return status, headers, parse_prometheus(body), r


def _per_compute(computes, inventory, checks, fn):
    """Scrape every compute in parallel and hand (compute, samples) to ``fn``."""
    for c, res, exc in fan_out(lambda c: _scrape(c, inventory), computes):
        if exc:
            checks.fail(f"{c.name}: scrape failed: {exc}")
            continue
        status, _, samples, r = res
        if not checks.check(status == 200, f"{c.name}: metrics endpoint status {status} rc={r.rc} {r.tail(200)!r}"):
            continue
        fn(c, samples)


def _require_families(names, computes, inventory, checks):
    fam = _families(inventory)
    gl = _gpu_label(inventory)

    def check(c, samples):
        for name in names:
            hits = [s for s in samples if fam[name].search(s.name)]
            if not checks.check(hits, f"{c.name}: no '{name}' metrics (/{fam[name].pattern}/)"):
                continue
            bad = sorted({s.name for s in hits if math.isnan(s.value)})
            checks.check(not bad, f"{c.name}: '{name}' metrics report NaN: {bad[:5]}")
            if name in PER_GPU:
                have = {s.labels.get(gl) for s in hits}
                missing = sorted(set(map(str, range(c.gpus))) - have)
                checks.check(not missing, f"{c.name}: '{name}' missing for {gl} {missing}")

    _per_compute(computes, inventory, checks, check)


def _container_up(c, inventory) -> bool:
    name = _cfg(inventory, "container", "device-metrics-exporter")
    port = _cfg(inventory, "port", 5000)
    running = c.run(f"docker ps --filter name=^{name}$ --format '{{{{.Names}}}}'", sudo=True)
    listening = c.run(f"ss -ltnH 'sport = :{port}'")
    return running.ok and name in running.stdout.split() and listening.ok and bool(listening.stdout.strip())


@pytest.mark.tc("TC-CTL-139")
def test_exporter_container_running_port_bound(computes, inventory, checks):
    """Start the exporter with ``commands.metrics_exporter.run`` where it isn't running, then
    require the container up and port 5000 listening on every compute."""
    run_tmpl = _cfg(inventory, "run", None)

    def ensure(c):
        if _container_up(c, inventory):
            return True
        if not run_tmpl:
            return False
        c.run(run_tmpl, sudo=True, timeout=300).check()
        return wait_until(lambda: _container_up(c, inventory), float(inventory.th("metrics.start_timeout_s", 60)),
                          float(inventory.th("metrics.poll_interval_s", 5)))[0]

    for c, ok, exc in fan_out(ensure, computes):
        checks.check(ok and not exc, f"{c.name}: exporter not running / port not bound"
                                     f"{'' if run_tmpl else ' (commands.metrics_exporter.run not set)'}{f': {exc}' if exc else ''}")
    checks.assert_all()


@pytest.mark.tc("TC-CTL-140")
def test_metrics_http_200_prometheus_format(computes, inventory, checks):
    for c, res, exc in fan_out(lambda c: _scrape(c, inventory), computes):
        if exc:
            checks.fail(f"{c.name}: {exc}")
            continue
        status, headers, samples, r = res
        checks.check(status == 200, f"{c.name}: HTTP {status}")
        ctype = headers.get("content-type", "")
        checks.check(ctype.startswith("text/plain") or "openmetrics" in ctype,
                     f"{c.name}: Content-Type {ctype!r} is not Prometheus text")
        checks.check("# TYPE " in r.stdout and samples, f"{c.name}: body has no # TYPE lines / samples")
    checks.assert_all()


@pytest.mark.tc("TC-CTL-141")
def test_gpu_power_temperature_utilization(computes, inventory, checks):
    _require_families(["power", "temperature", "utilization"], computes, inventory, checks)
    fam, gl = _families(inventory), _gpu_label(inventory)
    tmax = float(inventory.th("metrics.max_temperature_c", 110))

    def ranges(c, samples):
        for s in samples:
            g = s.labels.get(gl, "?")
            if fam["power"].search(s.name):
                checks.check(s.value > 0, f"{c.name}: gpu {g} {s.name}={s.value}")
            elif fam["temperature"].search(s.name):
                checks.check(0 < s.value < tmax, f"{c.name}: gpu {g} {s.name}={s.value} outside (0, {tmax})")
            elif fam["utilization"].search(s.name):
                checks.check(0 <= s.value <= 100, f"{c.name}: gpu {g} {s.name}={s.value} outside [0, 100]")

    _per_compute(computes, inventory, checks, ranges)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-142")
def test_vram_total_used_free(computes, inventory, checks):
    _require_families(["vram_total", "vram_used", "vram_free"], computes, inventory, checks)
    fam, gl = _families(inventory), _gpu_label(inventory)
    tol = float(inventory.th("metrics.vram_sum_tolerance_pct", 5))

    def sums(c, samples):
        by = {}
        for s in samples:
            for k in ("vram_total", "vram_used", "vram_free"):
                if fam[k].search(s.name):
                    by.setdefault(s.labels.get(gl), {})[k] = s.value
        for g, v in sorted(by.items(), key=lambda kv: str(kv[0])):
            if len(v) < 3:
                continue
            checks.check(v["vram_total"] > 0, f"{c.name}: gpu {g} total VRAM {v['vram_total']}")
            drift = abs(v["vram_used"] + v["vram_free"] - v["vram_total"]) / max(v["vram_total"], 1) * 100
            checks.check(drift <= tol, f"{c.name}: gpu {g} used+free off total by {drift:.1f}%")

    _per_compute(computes, inventory, checks, sums)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-143")
def test_ecc_counters(computes, inventory, checks):
    _require_families(["ecc_total", "ecc_block"], computes, inventory, checks)
    fam, gl = _families(inventory), _gpu_label(inventory)
    limit = float(inventory.th("metrics.max_ecc_uncorrectable", 0))

    def unc(c, samples):
        for s in samples:
            if fam["ecc_uncorrectable"].search(s.name):
                checks.check(s.value <= limit, f"{c.name}: gpu {s.labels.get(gl)} {s.name}={s.value:.0f} (> {limit:.0f})")

    _per_compute(computes, inventory, checks, unc)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-144")
def test_pcie_metrics(computes, inventory, checks):
    _require_families(["pcie_bandwidth", "pcie_speed", "pcie_width", "pcie_errors"], computes, inventory, checks)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-145")
def test_xgmi_metrics(computes, inventory, checks):
    _require_families(["xgmi_throughput", "xgmi_beats"], computes, inventory, checks)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-146")
def test_nic_metrics(computes, inventory, checks):
    _require_families(["nic_frames", "nic_bytes", "nic_lif"], computes, inventory, checks)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-147")
def test_rdma_metrics(computes, inventory, checks):
    _require_families(["rdma_req", "rdma_resp", "rdma_cnp", "rdma_ecn", "rdma_qp"], computes, inventory, checks)
    checks.assert_all()


@pytest.mark.tc("TC-CTL-148")
def test_gpu_labels(computes, inventory, checks):
    """gpu ids are 0..n-1, hostname label is the node's hostname, one GPU model label per node."""
    gl = _gpu_label(inventory)
    host_label = inventory.th("metrics.hostname_label", "hostname")
    model_labels = inventory.th("metrics.model_labels") or ["card_model", "card_series", "gpu_model"]

    def labels(c, samples):
        ids = sorted({s.labels[gl] for s in samples if gl in s.labels}, key=lambda x: (len(x), x))
        checks.check(ids == [str(i) for i in range(c.gpus)], f"{c.name}: {gl} values {ids}, expected 0..{c.gpus - 1}")
        names = {c.sh("hostname").strip(), c.sh("hostname -s").strip()}
        seen = {s.labels[host_label] for s in samples if host_label in s.labels}
        checks.check(seen and seen <= names, f"{c.name}: {host_label} labels {sorted(seen)} not in {sorted(names)}")
        models = {s.labels[k] for s in samples for k in model_labels if s.labels.get(k)}
        checks.check(len(models) == 1, f"{c.name}: GPU model labels {sorted(models)} (expected exactly one; keys {model_labels})")

    _per_compute(computes, inventory, checks, labels)
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-CTL-149")
def test_exporter_restart(computes, inventory, checks):
    """docker restart: port re-binds, metrics come back (no fewer samples), no errors in the new logs."""
    name = _cfg(inventory, "container", "device-metrics-exporter")
    timeout = float(inventory.th("metrics.start_timeout_s", 60))
    interval = float(inventory.th("metrics.poll_interval_s", 5))
    err_re = re.compile(inventory.th("metrics.log_error_regex", r"(?i)\b(error|panic|fatal)\b"))

    def one(c):
        before = len(_scrape(c, inventory)[2])
        c.run(f"docker restart {name}", sudo=True, timeout=120).check()
        ok, res = wait_until(lambda: (lambda r: r if r[0] == 200 and r[2] else None)(_scrape(c, inventory)), timeout, interval)
        if not ok:
            return f"metrics not back within {timeout:.0f}s"
        if len(res[2]) < 0.9 * before:
            return f"only {len(res[2])} samples after restart (was {before})"
        logs = c.run(f"docker logs --since {int(timeout) + 120}s {name} 2>&1", sudo=True).stdout
        errs = [ln for ln in logs.splitlines() if err_re.search(ln)]
        return f"errors in exporter logs: {errs[:3]}" if errs else ""

    for c, msg, exc in fan_out(one, computes):
        checks.check(not exc and not msg, f"{c.name}: {exc or msg}")
    checks.assert_all()


@pytest.mark.disruptive
@pytest.mark.tc("TC-CTL-150")
def test_exporter_upgrade(computes, inventory, checks):
    """Run ``commands.metrics_exporter.upgrade`` and require the container on ``upgrade_image``
    with metrics still served."""
    tmpl, image = _cfg(inventory, "upgrade", None), _cfg(inventory, "upgrade_image", None)
    if not tmpl or not image:
        pytest.skip("commands.metrics_exporter.upgrade / upgrade_image not set")
    name = _cfg(inventory, "container", "device-metrics-exporter")
    timeout = float(inventory.th("metrics.start_timeout_s", 60))

    def one(c):
        c.run(tmpl.format(image=image, container=name), sudo=True, timeout=600).check()
        ok, _ = wait_until(lambda: _scrape(c, inventory)[0] == 200, timeout, float(inventory.th("metrics.poll_interval_s", 5)))
        running = c.sh(f"docker inspect -f '{{{{.Config.Image}}}}' {name}", sudo=True).strip()
        if running != image:
            return f"container runs {running!r}, expected {image!r}"
        return "" if ok else f"metrics not served within {timeout:.0f}s after upgrade"

    for c, msg, exc in fan_out(one, computes):
        checks.check(not exc and not msg, f"{c.name}: {exc or msg}")
    checks.assert_all()
