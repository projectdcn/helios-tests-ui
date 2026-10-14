"""Shared helpers for the disruptive AFM suites (test_afm_links.py, test_afm_failover.py)."""
from __future__ import annotations

import pytest

from helios.afm import CLUSTER, wait_for
from helios.afm_ops import TERMINAL

FP = f"{CLUSTER}/failoverpolicies"


def _scale(ops) -> float:
    return float(ops.th("time_scale", 1))


def _baseline(ops, vpod, record_property):
    base = ops.settled_planes(vpod, interval=float(ops.th("plane_settle_interval_s", 10)))
    record_property("baseline_planes", base)
    return base


class Links:
    """Shut ports and always bring them back."""

    def __init__(self, ops):
        self.ops, self.down = ops, []

    def shut(self, sw, ns, port):
        r = self.ops.port_admin(sw, ns, port, up=False)
        if r.ok:
            self.down.append((sw, ns, port))
        return r

    def restore(self, sw, ns, port, checks=None):
        self.ops.port_admin(sw, ns, port, up=True)
        if not self.ops.wait_oper(sw, ns, port, "up", 60):   # full rack: startup did not always stick -> re-flap
            self.ops.port_admin(sw, ns, port, up=False)
            self.ops.port_admin(sw, ns, port, up=True)
            ok = self.ops.wait_oper(sw, ns, port, "up", 60)
            if checks is not None:
                checks.check(ok, f"{sw.name} {ns} {port} not oper up after startup (re-flapped once)")
        if (sw, ns, port) in self.down:
            self.down.remove((sw, ns, port))

    def restore_all(self):
        for sw, ns, port in list(self.down):
            self.restore(sw, ns, port)


def _target_switch(ops):
    t = ops.pick_switch(controller=None if len(ops.afm.switches()) == 1 else False)
    return t["meta"]["name"], ops.ssh_switch(t)


def _pick(ops, sw, slots=None, asics=None):
    nss = sw.namespaces()
    c = ops.link_candidates(sw, slots, [nss[a] for a in asics] if asics else None)
    if not c:
        pytest.fail(f"{sw.name}: no up trunk port cabled to slots {slots} (asics {asics})")
    return c[0]


def _restored(ops, checks, vpod, base, what="after the link came back"):
    t = float(ops.th("plane_restore_s", 300))
    ok = wait_for(lambda: (ops.planes(vpod)[1] or 0) >= (base[1] or 0) and ops.vpod_state(vpod) in TERMINAL, t, 10)
    checks.check(ok, f"{vpod} planes {ops.planes(vpod)} / {ops.vpod_state(vpod)} {what}: baseline {base} not "
                     f"restored within {t:.0f}s")


def _fix_manual(ops, vpod):
    if ops.vpod_state(vpod) == "needdatafabricreconfiguration":
        ops.afm.raw("POST", f"{CLUSTER}/vpods/{vpod}/ReconfigureDataFabric", {})


def _user_vpod_ctx(ops, uniq, cleanup, policy_spec):
    sysv = ops.system_vpod_name()
    names = [c["meta"]["name"] for c in sorted(ops.afm.computes(), key=lambda c: c["spec"].get("slot-id", 0))
             if ops.compute_ready(c["meta"]["name"])][-int(ops.th("vpod_test_computes", 1)):]
    pol, user = uniq("fp"), uniq("vp")
    cleanup(f"{FP}/{pol}")
    ops.afm.post(FP, {"meta": {"name": pol}, "spec": policy_spec})
    moved = []
    for n in names:
        moved += ops.remove_compute(sysv, n)
    ops.create_vpod(user, moved, policy=pol)
    return sysv, user, pol, names, moved


def _user_vpod_restore(ops, sysv, user, moved):
    _fix_manual(ops, user)
    ops.delete_vpod(user)
    if moved:
        ops.add_gpus(sysv, moved)


