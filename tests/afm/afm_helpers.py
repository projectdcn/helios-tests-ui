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
    """Shut ports and always bring them back. Records which other ports of the same switch ASIC were up before the
    shut and reports any that are down after the restore (collateral): g02-g17 17-18 Oct, shutting st6 Ethernet257
    also took Ethernet261 down, st4 Ethernet70 took Ethernet66 - and the re-flap meant to recover a port that stayed
    down took more neighbours with it. thresholds.afm.reflap_on_restore (true) turns that re-flap off;
    thresholds.afm.collateral_wait_s (90) is how long a neighbour may take to come back."""

    def __init__(self, ops):
        self.ops, self.down, self.before, self.collateral, self.shut_ports = ops, [], {}, [], set()

    def _up(self, sw, ns) -> set[str]:
        try:
            return {r.get("Interface") for r in sw.interfaces_status(ns) if r.get("Oper") == "up"}
        except Exception:                     # noqa: BLE001 - the neighbour check must never break a restore
            return set()

    def shut(self, sw, ns, port):
        self.before.setdefault((sw.name, ns), (sw, self._up(sw, ns)))
        r = self.ops.port_admin(sw, ns, port, up=False)
        if r.ok:
            self.down.append((sw, ns, port))
            self.shut_ports.add((sw.name, ns, port))
        return r

    def restore(self, sw, ns, port, checks=None):
        self.ops.port_admin(sw, ns, port, up=True)
        if not self.ops.wait_oper(sw, ns, port, "up", 60):
            ok = False
            if self.ops.th("reflap_on_restore", True):   # MR3: startup did not always stick -> re-flap once
                self.ops.port_admin(sw, ns, port, up=False)
                self.ops.port_admin(sw, ns, port, up=True)
                ok = self.ops.wait_oper(sw, ns, port, "up", 60)
            if checks is not None:
                how = "re-flapped once" if self.ops.th("reflap_on_restore", True) else "not re-flapped: reflap_on_restore false"
                checks.check(ok, f"{sw.name} {ns} {port} not oper up after startup ({how})")
        if (sw, ns, port) in self.down:
            self.down.remove((sw, ns, port))
        self._neighbours(sw, ns, checks)

    def _neighbours(self, sw, ns, checks=None):
        """After the last shut port of this ASIC is back: ports up before the shut and down now (still shut ones
        excluded), given collateral_wait_s to recover."""
        if any(s.name == sw.name and n == ns for s, n, _ in self.down) or (sw.name, ns) not in self.before:
            return
        _, before = self.before.pop((sw.name, ns))
        mine = {p for s_, n_, p in self.shut_ports if s_ == sw.name and n_ == ns}   # reported by restore itself
        lost = lambda: sorted(before - mine - self._up(sw, ns))  # noqa: E731
        gone = lost()
        if gone:
            self.ops.wait(lambda: not lost(), float(self.ops.th("collateral_wait_s", 90)), 10)
            gone = lost()
        if gone:
            self.collateral.append((sw.name, ns, gone))
            if checks is not None:
                checks.check(False, f"{sw.name} {ns}: {', '.join(gone)} up before the shut, still down after the "
                                    f"restore (coupled ports? add them to thresholds.afm.avoid_ports)")

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


def _reconfigure_after_restore(ops, checks, vpod, base, threshold: float):
    """Manual policy, port back up: wait (threshold + 60 s) for AFM to ask again (needdatafabricreconfiguration) and
    answer with ReconfigureDataFabric - plan TC-AFM-016: 'bring interface back up, manually reconfigure again to
    online'. Returns at once if the planes are back without it. g02-g17 17 Oct (TC-AFM-016/041, TC-E2E-146/052):
    AFM asked ~20 s after the port was up (36,36,35); the old immediate _fix_manual saw 'degraded', did nothing,
    and _restored waited 900 s on a vPOD that was waiting for the user."""
    def asked_or_back():
        st = ops.vpod_state(vpod)
        return st == "needdatafabricreconfiguration" or (st in TERMINAL and (ops.planes(vpod)[1] or 0) >= (base[1] or 0))
    ops.wait(asked_or_back, threshold + 60, 2)
    if ops.vpod_state(vpod) == "needdatafabricreconfiguration":
        r = ops.afm.raw("POST", f"{CLUSTER}/vpods/{vpod}/ReconfigureDataFabric", {})
        checks.check(r.status_code == 200, f"ReconfigureDataFabric after the link came back -> HTTP {r.status_code}")


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


