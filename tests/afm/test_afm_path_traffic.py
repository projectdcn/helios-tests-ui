"""Bring-up validation: IFoE station path-to-port map and TransferBench traffic after link triggers (L1, DISRUPTIVE):

  TC-AFM-L1-6  Path-to-Port Mapping - In-Domain TxB Traffic After Trigger: on the target tray, after each trigger the
               ``afmctl show station`` path-to-port map of the triggered port's station is right (port out of the
               map while down / drained, back once up / activated) and in-domain TransferBench then runs on the tray.
  TC-AFM-L1-7  Path-to-Port Map - Cross-Domain TxB Validation: the same, with cross-domain TransferBench between the
               target tray and a second tray (both directions).

Triggers (one switch-side port cabled to GPU 0 of the target tray, as TC-AFM-058): Link Down (shut; traffic with the
port down, then restored; traffic again), Link Flap (shut + startup; traffic), Link Drain (AFM DrainGPUPort; traffic
with the port drained), Link Activate (AFM ActivateGPUPort; traffic). Traffic always runs after the trigger has taken
effect in the map, never during it. A baseline map check and traffic run come first. The inb-node-agent restart
trigger of the related AMD case is NOT included: the repo has no helper to restart a compute's node agent.

Bandwidth per step is recorded (bw_gbps); a run fails below the floors the datapath helpers apply
(thresholds.datapath.transferbench.in_domain_min_gbps / cross_domain_min_gbps; unset = only a bandwidth figure is
required). Map waits: thresholds.afm.path_map_wait_s (120) and path_map_activate_wait_s (180), scaled by
thresholds.afm.time_scale. The target is thresholds.afm.target_compute, else the highest Ready slot among the trays
that passed the datapath pre-flight. Shut ports are restored (links fixture) and the drained port activated in finally blocks.
"""
from __future__ import annotations

import pytest

from helios import datapath
from helios.afm_ops import CN

pytestmark = [pytest.mark.l1, pytest.mark.disruptive]


def _target(ops, computes):
    """(AFM name, tray) of a Ready compute whose tray passed the datapath pre-flight; thresholds.afm.target_compute
    or the highest slot first."""
    names = {c.name: c for c in computes}
    first = ops.pick_compute()
    nodes = sorted(ops.afm.computes(), key=lambda c: -(c.get("spec") or {}).get("slot-id", 0))
    for n in [first] + [c["meta"]["name"] for c in nodes if c["meta"]["name"] != first]:
        try:
            t = ops.tray(n)
        except RuntimeError:
            continue
        if t.name in names and ops.compute_ready(n):
            return n, names[t.name]
    pytest.fail(f"no Ready AFM compute among the trays that passed the datapath pre-flight: {sorted(names)}")


def _triggers_with_traffic(ops, links, computes, inventory, checks, record_property, cross: bool):
    name, tray = _target(ops, computes)
    trays = [tray]
    if cross:
        peer = next((c for c in computes if c.name != tray.name), None)
        if peer is None:
            pytest.skip("cross-domain traffic needs a second tray that passed the datapath pre-flight")
        datapath.require_transferbench_cross(inventory)
        trays.append(peer)
    slot = ops.compute(name)["spec"]["slot-id"]
    link = next((x for x in ops.gpu_links(slot) if x[3][1] == 0), None)
    if link is None:
        pytest.fail(f"{ops.label(name)}: no up fabric port cabled to GPU 0")
    sw, ns, swport, cab = link
    gpu, port = cab[1], cab[3]
    bdf = f"000{gpu + 1}:01:00.1"               # AFM GPU index order = BDF order
    st = ops.station(cab)
    wait_s = float(ops.th("path_map_wait_s", 120))
    record_property("target", {"compute": ops.label(name), "trays": [t.name for t in trays], "gpu": gpu, "port": port,
                               "station": st, "switch_port": f"{sw.name} {ns} {swport}"})

    def in_map():
        return port in ops.station_map(tray, bdf).get(st, set())
    maps, bw = {}, {}

    def step(label, want_in: bool, timeout: float = wait_s):
        """The map must reach the expected state, then traffic runs (also when the map is wrong: its bandwidth is
        evidence)."""
        ok = bool(ops.wait(in_map if want_in else (lambda: not in_map()), timeout, 5, f"station {st} map {label}"))
        maps[label] = ok
        checks.check(ok, f"{tray.name} {bdf} station {st}: port {port} {'not back in' if want_in else 'still in'} the "
                         f"path-to-port map after {label}")
        tb = datapath.transferbench_cross_domain(trays, inventory, checks) if cross else \
            datapath.transferbench_in_domain(trays, inventory, checks)
        bw[label] = tb
        checks.check(tb and all(v is not None for v in tb.values()), f"TransferBench after {label}: no bandwidth {tb}")

    body = {"ports": [{"gpu-id": gpu, "port-id": port}]}
    try:
        step("baseline", True)
        r = links.shut(sw, ns, swport)
        checks.check(r.ok, f"{sw.name} {ns} {swport}: shutdown failed rc {r.rc}")
        step("link_down", False)
        links.restore(sw, ns, swport, checks)
        step("link_down_restored", True)
        links.shut(sw, ns, swport)
        links.restore(sw, ns, swport, checks)
        step("link_flap", True)
        try:
            ops.afm.post(f"{CN}/{name}/DrainGPUPort", body)
            step("link_drain", False)
        finally:
            ops.afm.post(f"{CN}/{name}/ActivateGPUPort", body)
        step("link_activate", True, float(ops.th("path_map_activate_wait_s", 180)))
    finally:
        links.restore_all()
        record_property("station_map", maps)
        record_property("bw_gbps", bw)
    checks.assert_all()


@pytest.mark.datapath(tools=["transferbench"])
@pytest.mark.tc("TC-AFM-L1-6")
def test_path_map_in_domain_traffic_after_triggers(ops, afm_ready, datapath_ready, links, computes, inventory, checks,
                                                   record_property):
    """Link down / flap / drain / activate on a GPU 0 port of the target tray: after each, the station's
    path-to-port map is right and in-domain TransferBench on the tray meets the in-domain floor."""
    _triggers_with_traffic(ops, links, computes, inventory, checks, record_property, cross=False)


@pytest.mark.datapath(tools=["transferbench"], min_computes=2)
@pytest.mark.tc("TC-AFM-L1-7")
def test_path_map_cross_domain_traffic_after_triggers(ops, afm_ready, datapath_ready, links, computes, inventory, checks,
                                                      record_property):
    """Link down / flap / drain / activate on a GPU 0 port of the target tray: after each, the station's
    path-to-port map is right and cross-domain TransferBench between it and a second tray meets the cross-domain
    floor."""
    _triggers_with_traffic(ops, links, computes, inventory, checks, record_property, cross=True)
