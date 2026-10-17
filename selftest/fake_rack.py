"""Rack simulation behind selftest/fake_afm.py: switch agents, controllers, sb-node-agents, switch/compute
admission, vPOD membership and switching planes, plus ``FakeSwitchTransport`` - the switch "SSH" the
disruptive AFM tests drive (systemctl/docker/kill/config interface/show mac/...), bound to the same state.

Transitions are immediate, so the lifecycle tests run in seconds. Behaviour follows the g02-g17 full-rack
observations (results/extract-procedures.md); ``faults`` switch individual product behaviours to their
broken form so the self-test proves each test catches it.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
import time
import uuid as uuidlib
from datetime import datetime, timezone

from helios.transport import CmdResult, build_command

log = logging.getLogger("helios.cmd")
PLANES = 36
MCH_MASK = "000fff333000fff333"   # plan lane mask, station 0 first: 6 two-port, 6 one-port, 6 empty
FRU = "/etc/sonic/fru.json"   # selftest inventory thresholds.afm.fru_json_path
IFCP_MTU = 4096           # negotiated IFCP MTU (peer_mtu on MR3)
BASELINE_DOWN = 3          # system vPOD baseline 36/33/33 (degraded), as on the full rack
_pids = itertools.count(4000)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _obj(kind, name, spec, status):
    return {"kind": kind, "api-version": "v1",
            "meta": {"name": name, "tenant": "default", "uuid": str(uuidlib.uuid4()), "creation-time": now(),
                     "mod-time": now(), "generation-id": "1"}, "spec": spec, "status": status}


def _selects(reqs, ev: dict) -> bool:
    """AFM fields selector: requirements are ANDed and 'a selector with no Requirements does not select anything'."""
    if not reqs:
        return False
    for r in reqs:
        v, vals, op = str(ev.get(r.get("key"), "")), [str(x) for x in r.get("values") or []], r.get("operator", "equals")
        ok = {"equals": v in vals[:1], "notequals": v not in vals[:1], "in": v in vals, "notin": v not in vals}.get(op, False)
        if not ok:
            return False
    return True


def mch_used(mask: str = MCH_MASK) -> set[int]:
    return {2 * s + b for s, d in enumerate(mask) for b, bits in ((0, 0x3), (1, 0xC)) if int(d, 16) & bits}


class Sim:
    def __init__(self, st, n_switches: int = 1, n_computes: int = 2, mch: str | None = None):
        self.st, self.f = st, st.f
        self.n_sw = n_switches
        # MCH (mch: full | mini): 36 IFoE ports per GPU, all cabled, only the lane mask's 18 used; 18 planes
        self.mch = mch
        self.used = mch_used() if mch else set(range(2 * n_switches))
        self.planes = 18 if mch else PLANES
        self.switch_ips = [f"10.0.0.{83 - n_switches + 1 + i}" for i in range(n_switches)]
        self.quorum = self.switch_ips[:3]
        self.leader = self.quorum[-1]
        self.agent_up = {i: True for i in range(n_switches)}
        self.identity_reset: set[int] = set()
        self.stale: set[int] = set()          # readmitted switches whose planes AFM never restored (fault)
        self.wiped: set[int] = set()          # switches whose VLANs were wiped (fault vlan_wipe)
        self.sb_pid = {s: next(_pids) for s in range(n_computes)}
        self.stopped: set[int] = set()
        self.removed_at: dict[str, float] = {}
        self.reconf_at: dict[str, float] = {}
        self.configfailed: set[str] = set()
        self.stale_macs = 0
        # links: (sw, ns, port) -> {"since", "known", "coupled"}; a link fails after the vPOD policy's threshold
        self.links_down: dict[tuple[int, str, str], dict] = {}
        self.time_scale = 1.0                  # fake seconds per policy second (selftest shrinks timers)
        self.failed_evt: set[tuple] = set()
        self.sticky: set[tuple] = set()       # links AFM keeps counting as failed after they came back (faults)
        self.last_avail: dict[str, int] = {}
        # controllers: quorum member index -> up; container start time / restart count / last log write
        self.ctrl_up = {i: True for i in range(len(self.quorum))}
        self.ctrl_started = {i: time.time() - 3600 for i in range(n_switches)}
        self.ctrl_restarts = {i: 0 for i in range(n_switches)}
        self.ctrl_log = {i: time.time() - 600 for i in range(n_switches)}
        self.sw_stopped: set[int] = set()      # switch-agent process SIGSTOPped
        self.stops = {i: 0 for i in range(n_switches)}
        self.configdone: set[str] = set()
        self.zero_reads: dict[str, int] = {}
        self.ws_refuse = False
        self.cmp_admin_down: set[tuple] = set()   # (slot, gpu, port) set idle with afmctl update port
        self.flapped: set[tuple] = set()
        self.ifcp_stuck: set[tuple] = set()
        self.journal: dict[int, list[float]] = {}  # slot -> afmctl reset times
        self.user_vlans: dict[int, set[int]] = {}
        self.readmit_at: dict[int, float] = {}
        self.ack: dict[str, set] = {}          # vPOD -> failed links acknowledged by ReconfigureDataFabric
        self.back: dict[str, dict] = {}        # manual vPOD -> {acknowledged link that came back: since} (asks again)
        self.n_ct = n_computes
        self.homed: dict[int, int] = {}       # slot -> switch it was re-homed to (fault no_rebalance keeps it)
        self.flaps = 0
        self.last_ctrl_restart = self.last_agent_restart = -99.0
        self.wedged: set[int] = set()         # switches stuck unhealthy (fault pairwise_wedge)
        self.disconnected = False             # node agents never reconnected (fault agents_no_reconnect)
        self.cf_perm: set[str] = set()        # vPODs stuck configurationfailed (fault vpod_fails_with_cp_switch_down)
        self.images = {i: {"current": "SONiC-OS-helios.1", "available": ["SONiC-OS-helios.1"]} for i in range(n_switches)}
        self.mgmt_down_ct: set[int] = set()
        self.layout_lost = False              # MCH unused ports re-enabled (faults mch_layout_lost_on_*)
        self.map_stuck: set[tuple] = set()    # (slot, gpu, port) left out of the live path map (fault mch_flap_sticky)
        self.reset_broken: set[int] = set()   # slots whose live maps stay empty after afmctl reset (fault)   # computes whose management network is cut
        self.fru_bad: set[int] = set()        # switches whose fru.json was overwritten with invalid JSON
        self.mgmt_eth1: set[int] = set()      # switches with the management IP moved to eth1
        self.mtu: dict[tuple, int] = {}       # (sw, ns, port) -> MTU set with config interface mtu (default 9100)
        self.events: list[dict] = []
        self.gpus: dict[int, list[str]] = {}  # slot -> gpu uuids
        self.st = st
        gpu_ready = "Failed" if "gpu_not_ready" in self.f else "Ready"
        computes, switches = {}, {}
        for slot in range(n_computes):
            gi = {}
            for g in range(4):
                u = f"0500{slot:02d}{g}c1-0000-1000-80f7-ca5e4f760e70"
                gi[u] = {"index": g, "acc-id": slot * 4 + g + (0 if "dup_acc" not in self.f else -g),
                         "oper-state": gpu_ready if (g == 0 and slot == 0) else "Ready", "vpod-membership": "",
                         "ports": [{"id": p, "oper-state": "PortReady"} for p in range(36)],
                         "ifoe-stations": list(range(18))}
                self.gpus.setdefault(slot, []).append(u)
            computes[self.cname(slot)] = self._compute_obj(slot, gi)
        for i, ip in enumerate(self.switch_ips):
            switches[self.sname(i)] = _obj("SwitchNode", self.sname(i), {"ip-address": ip, "slot-id": self.sw_slot(i)},
                                           {"admission-phase": "admitted", "conditions": []})
        early = (datetime.now(timezone.utc) - __import__("datetime").timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if "computes_before_switches" not in self.f:
            for o in switches.values():          # bring-up order: switches are admitted first
                o["meta"]["creation-time"] = early
        self.computes, self.switches = computes, switches
        if mch and "mch_alarm_unused" in self.f:
            unused = min(set(range(36)) - self.used)
            self.events.append({"kind": "Event", "meta": {"name": "mch-alarm", "creation-time": now(), "mod-time": now()},
                                "type": "ifoe_netport_link_down", "message": f"Port (acc-id: 0, port-id: {unused}) link down"})

    # ------------------------------------------------------------ naming / topology
    @staticmethod
    def cname(slot):
        return f"pod-1-{slot}-ComputeTray"

    @staticmethod
    def sname(i):
        return f"pod-1-{i}-SwitchTray"

    def _compute_obj(self, slot, gi):
        return _obj("ComputeNode", self.cname(slot), {"admit": True, "slot-id": slot, "scaleup-pod": "pod-1"},
                    {"admission-phase": "admitted", "conditions": [],
                     "cpus": [{"num-cores": 8, "model-name": "AMD Ryzen Embedded V3C48"}],
                     "scale-up-info": {"gpu-info": gi}})

    def sw_slot(self, i):
        """heliosmch full rack: switch slot ids 1..3; elsewhere the index."""
        return i + 1 if self.mch == "full" and "mch_slot_ids_wrong" not in self.f else i

    def gpu_pids(self):
        """IFoE port ids a GPU reports: all 36 on MCH, else the cabled 2 per switch."""
        return range(36) if self.mch else range(2 * self.n_sw)

    def pid_link(self, pid):
        """(switch index, asic) a GPU port is cabled to, or None."""
        if self.mch:
            return divmod(pid % (2 * self.n_sw), 2)
        return divmod(pid, 2) if pid < 2 * self.n_sw else None

    def sw_index(self, name):
        return int(name.split("-")[2])

    def in_service(self):
        return [i for i in range(self.n_sw) if self.phase_sw(i) == "admitted"]

    def phase_sw(self, i):
        o = self.switches.get(self.sname(i))
        return o["status"]["admission-phase"] if o else "absent"

    def slot_host(self, slot) -> int | None:
        """Switch index running the slot's sb-node-agent: slot % n, re-homed to the next live switch."""
        phases = ("admitted", "drained") + (("absent", "decommissioned") if "no_rehome_on_delete" in self.f else ())
        live = [i for i in range(self.n_sw) if self.phase_sw(i) in phases and self.agent_up[i]]
        if "no_rehome" in self.f and not self.agent_up[slot % self.n_sw]:
            return None
        if "agents_unbalanced" in self.f and live:
            return live[0]
        if "thrash" in self.f and live and self.flaps:
            return live[(slot + int(time.monotonic() * 20)) % len(live)]
        if "no_rebalance" in self.f and slot in self.homed and self.homed[slot] in live:
            return self.homed[slot]
        pref = slot % self.n_sw
        if pref in live or not live:
            return pref if self.agent_up[pref] else None
        self.homed[slot] = live[slot % len(live)]
        return self.homed[slot]

    # ------------------------------------------------------------ cabling
    def ports(self, i, ns):
        """{port: (slot, gpu, acc, gpu_port)}: one port per GPU per ASIC; plane/GPU port id = 2*switch + asic.
        MCH: every GPU port id p with p % (2*switches) == 2*switch + asic (unused lanes cabled too)."""
        a = int(ns[4:])
        if not self.mch:
            return {f"Ethernet{a * 256 + (s * 4 + g) * 8}": (s, g, s * 4 + g, 2 * i + a)
                    for s in range(self.n_ct) for g in range(4)}
        pids = [p for p in range(36) if self.pid_link(p) == (i, a)]
        return {f"Ethernet{a * 256 + ((s * 4 + g) * len(pids) + j) * 2}": (s, g, s * 4 + g, p)
                for s in range(self.n_ct) for g in range(4) for j, p in enumerate(pids)}

    def port_vlan(self, i, ns, port) -> str:
        """'show interfaces status' Vlan column: 'trunk' while the cabled GPU is in a vPOD, else 'routed' - g02-g17
        17 Oct: AFM takes the switch ports of GPUs that left every vPOD out of the VLAN."""
        cab = self.ports(i, ns).get(port)
        if cab is None:
            return "trunk"
        return "trunk" if any((g.get("slot-id"), g.get("index")) == (cab[0], cab[1])
                              for v in self.st.objs["VPod"].values() for g in v["spec"].get("GPUs") or []) else "routed"

    def lane_used(self, i, ns, port) -> bool:
        cab = self.ports(i, ns).get(port)
        return cab is not None and cab[3] in self.used

    def link_state(self, i, ns, port):
        if not self.lane_used(i, ns, port):
            return "down"                     # MCH: the GPU side of an unused lane is disabled
        return "down" if (i, ns, port) in self.links_down else "up"

    def admin(self, i, ns, port):
        return "down" if (self.links_down.get((i, ns, port)) or {}).get("admin") else "up"

    def stick_slot_ports(self, slots):
        """Fault ifcp_no_recover: IFCP sessions of these slots' ports never come back."""
        if "ifcp_no_recover" in self.f:
            for s in slots:
                for g in range(4):
                    for pid in self.gpu_pids():
                        self.ifcp_stuck.add((s, g, pid))

    def set_link(self, i, ns, port, up: bool, quiet: bool = False):
        key = (i, ns, port)
        ports = self.ports(i, ns)
        if port not in ports:
            return False
        if not self.lane_used(i, ns, port) and "events_on_unused_lanes" not in self.f:
            quiet = True
        keys = [key]
        if "coupled_ports" in self.f:   # full rack: shutting .48 Eth243 also dropped Eth251
            names = sorted(ports, key=lambda x: int(x[8:]))
            keys.append((i, ns, names[(names.index(port) + 1) % len(names)]))
        for k in ([key] if up else keys):   # a coupled neighbour does not come back with the target
            if up:
                d = self.links_down.pop(k, None)
                if k[2] in self.ports(i, k[1]):
                    s_, g_, _, pid_ = self.ports(i, k[1])[k[2]]
                    self.flapped.add((s_, g_, pid_))
                    if self.mch and "mch_flap_sticky" in self.f:
                        self.map_stuck.add((s_, g_, pid_))
                    if "ifcp_no_recover" in self.f:
                        self.ifcp_stuck.add((s_, g_, pid_))
                if d is not None and not quiet:
                    self.event("link_up", f"PortName {k[2]} asic {k[1]} switch {self.sname(i)} is UP{self._pid_tag(k)}")
                    had_failed = k in self.failed_evt
                    if ("flap_counts_as_failure" in self.f and not had_failed) or \
                            ("no_auto_restore" in self.f and had_failed):
                        self.sticky.add(k)
                self.failed_evt.discard(k)
            elif k not in self.links_down:
                if "cp_lost_on_link_down" in self.f:
                    self.stopped |= set(self.sb_pid)
                self.links_down[k] = {"since": time.monotonic(), "known": self.agent_up[i], "admin": k == key}
                if self.agent_up[i] and not quiet:
                    self.event("link_down", f"PortName {k[2]} asic {k[1]} switch {self.sname(i)} is DOWN{self._pid_tag(k)}")
        return True

    def _pid_tag(self, k) -> str:
        s, g, acc, pid = self.ports(k[0], k[1])[k[2]]
        if "mch_event_wrong_pid" in self.f:
            pid += 1
        return f" (acc-id: {acc}, port-id: {pid})"

    def failed_links(self, objs, vpod) -> list[tuple]:
        """Down links affecting a vPOD's GPUs whose down time exceeded its failover policy threshold."""
        pol = objs["FailoverPolicy"].get(vpod["spec"].get("failover-policy") or "system-failover-policy") or {}
        th = float((pol.get("spec") or {}).get("link-fail-threshold", 10)) * float(self.time_scale)
        if "timer_ignored" in self.f:
            th = 0.0
        gpus = {(g.get("slot-id"), g.get("index")) for g in vpod["spec"].get("GPUs") or []}
        out, seen = [], set()
        for k, d in list(self.links_down.items()) + [(k, None) for k in self.sticky]:
            s, g, _, pid = self.ports(k[0], k[1])[k[2]]
            if (s, g) not in gpus or pid not in self.used or (self.mch and "mch_failed_links_ignored" in self.f):
                continue
            if d is not None and not (d["known"] and time.monotonic() - d["since"] >= th):
                continue
            if "one_failure_per_gpu" in self.f and (s, g) in seen:
                continue
            seen.add((s, g))
            out.append(k)
        return out

    def event(self, etype, msg):
        sev = "warn" if etype in ("link_down", "link_failed", "ifoe_netport_link_down", "switchnode_unreachable") else "info"
        ev = {"kind": "Event", "meta": {"name": str(uuidlib.uuid4()), "creation-time": now(), "mod-time": now()},
              "severity": sev, "type": etype, "message": msg}
        self.events.append(ev)
        if "no_alerts" in self.f:
            return
        for pol in list(self.st.objs["AlertPolicy"].values()):
            sp = pol.get("spec") or {}
            if sp.get("enable") and sp.get("resource", "Event") == "Event" and _selects(sp.get("requirements"), ev):
                self.st.alerts.append({"kind": "Alert", "meta": {"name": str(uuidlib.uuid4()), "creation-time": now()},
                                       "spec": {"alert-policy-id": pol["meta"]["name"], "state": "open"},
                                       "status": {"severity": sp.get("severity", "info"), "message": msg,
                                                  "event-type": etype}})

    # ------------------------------------------------------------ derived state
    def refresh(self, objs):
        """Recompute conditions, membership and vPOD status from the simulated state."""
        if "stale_switch_events" in self.f:
            for i in range(self.n_sw):
                if self.phase_sw(i) == "absent" and i not in self.identity_reset and self.agent_up[i]:
                    self.event("switchnode_unreachable", f"switch {self.sname(i)} unreachable")
        member = {}
        for vp in objs["VPod"].values():
            for g in (vp["spec"].get("GPUs") or []):
                member[(g.get("slot-id"), g.get("index"))] = vp["meta"]["name"]
        for i in range(self.n_sw):
            o = self.switches.get(self.sname(i))
            if o:
                ok = (self.agent_up[i] or "agent_stop_ignored" in self.f) and \
                    o["status"]["admission-phase"] in ("admitted", "drained") and i not in self.sw_stopped \
                    and not ("switch_stuck_unknown" in self.f and self.stops[i] >= 2) and i not in self.wedged
                o["status"]["conditions"] = [{"type": "healthy", "status": "true" if ok else "unknown"}]
                o["status"]["asic-info"] = {"asics": [{"name": ns, "ports": [
                    {"name": p, "oper-state": "PortReady" if self._sw_port_ready(i, ns, p, cab[0]) else "PortDown"}
                    for p, cab in self.ports(i, ns).items()]} for ns in ("asic0", "asic1")]}
        for name, o in self.computes.items():
            slot = o["spec"]["slot-id"]
            host = self.slot_host(slot)
            ok = host is not None and (slot not in self.stopped or "sigstop_ignored" in self.f) and \
                o["status"]["admission-phase"] == "admitted" and \
                (slot not in self.mgmt_down_ct or "mgmt_loss_unnoticed" in self.f) and \
                not ("last_compute_unhealthy" in self.f and slot == len(self.computes) - 1)   # quarantine tests
            o["status"]["conditions"] = [{"type": "healthy", "status": "true" if ok else "unknown"}]
            for u, g in o["status"]["scale-up-info"]["gpu-info"].items():
                g["vpod-membership"] = member.get((slot, g["index"]), "")
                for n, prt in enumerate(g["ports"]):
                    prt["oper-state"] = "PortReady" if not self.mch or n in self.used or "mch_unused_ready" in self.f \
                        else "PortDisabled"
                    if self.mch and n == min(set(range(36)) - self.used) and "mch_unused_failed" in self.f:
                        prt["oper-state"] = "PortFailed"
        lost = sum(self.planes // self.n_sw for i in range(self.n_sw) if self.phase_sw(i) != "admitted"
                   and not (self.phase_sw(i) in ("drained", "decommissioned") and "planes_no_drop" in self.f))
        lost += sum(self.planes // self.n_sw for i in self.stale)
        all_down = not any(self.agent_up.values())
        for vp in objs["VPod"].values():
            name = vp["meta"]["name"]
            sysv = name.endswith(".system")
            failed = self.failed_links(objs, vp)
            pol = objs["FailoverPolicy"].get(vp["spec"].get("failover-policy") or "system-failover-policy") or {}
            manual = (pol.get("spec") or {}).get("datafabric-reconfig") == "manual" or "auto_needs_manual" in self.f
            for k in failed:
                if k not in self.failed_evt:
                    self.failed_evt.add(k)
                    self.event("link_failed", f"PortName {k[2]} asic {k[1]} switch {self.sname(k[0])} is FAILED")
                s_, g_, _, pid = self.ports(k[0], k[1])[k[2]]
                c = self.computes.get(self.cname(s_))
                if c:
                    for gi in c["status"]["scale-up-info"]["gpu-info"].values():
                        if gi["index"] == g_:
                            gi["ports"][pid]["oper-state"] = "PortFailed"
            acked = self.ack.setdefault(name, set())
            back = self.back.setdefault(name, {})
            if manual:   # g02-g17 17 Oct: a link back up -> needdatafabricreconfiguration again after ~the threshold
                for k in acked - set(failed):
                    back.setdefault(k, time.monotonic())
            th = float((pol.get("spec") or {}).get("link-fail-threshold", 10)) * float(self.time_scale)
            asked = [k for k, t in back.items() if time.monotonic() - t >= th]
            acked &= set(failed)
            counted = [k for k in failed if (not manual) or k in acked] + [k for k in back if k not in asked]
            pending = manual and (any(k not in acked for k in failed) or asked) and "manual_auto" not in self.f
            avail = max(0, self.planes - (BASELINE_DOWN if sysv else 0) - lost - len(counted))
            if self.zero_reads.get(name):
                self.zero_reads[name] -= 1
                avail = 0
            if self.last_avail.get(name) not in (None, avail):
                self.vpod_event("vpod_bandwidth_change", name,
                                f"available/effective/provisioned switch planes changed to {avail}/{avail}/{self.planes}")
            self.last_avail[name] = avail
            st = vp["status"]
            st.update({"provisioned-switch-planes": PLANES if "mch_planes_36" in self.f else self.planes, "available-switch-planes": avail,
                       "effective-switch-planes": avail, "pending-ops": st.get("pending-ops") or []})
            st["gpus-status"] = [{"gpu": {"id": self.gpus.get(g.get("slot-id"), [None] * 4)[g.get("index", 0)],
                                          "gpu-index": {"slot-id": g.get("slot-id"), "index": g.get("index")}},
                                  "state": "ready"} for g in vp["spec"].get("GPUs") or []]
            if name in self.cf_perm:
                st["vpod-state"] = "configurationfailed"
                continue
            if name in self.configfailed:
                if not all_down and "configfailed_sticky" not in self.f:
                    self.configfailed.discard(name)
                else:
                    st["vpod-state"] = "configurationfailed"
                    continue
            if pending:
                st["vpod-state"] = "needdatafabricreconfiguration"
                continue
            if name in self.configdone:
                st["vpod-state"] = "configDone"
                continue
            st["vpod-state"] = "online" if avail == self.planes else "degraded"

    def _sw_port_ready(self, i, ns, port, slot) -> bool:
        c = self.computes.get(self.cname(slot))
        present = (c is not None and c["status"]["admission-phase"] == "admitted") or \
            "switch_ports_ready_after_delete" in self.f
        return present and (i, ns, port) not in self.links_down

    # ------------------------------------------------------------ vPOD membership
    def validate_gpus(self, gpus) -> str | None:
        """400 text when a GPU just left a vPOD (once per GPU, exercising the client's retry)."""
        for g in gpus:
            u = self.gpus.get(g.get("slot-id"), [None] * 4)[g.get("index", 0)]
            t = self.removed_at.pop(u, None)
            if t is not None:
                return (f"gpu validation failed (1 errors): gpu {g.get('slot-id') * 4 + g.get('index')} "
                        f"(slot:{g.get('slot-id')}, index:{g.get('index')}) is in configuration failed state. "
                        f"Reconfigure compute tray {g.get('slot-id')}")
        return None

    def gpus_removed(self, before, after):
        keep = {(g.get("slot-id"), g.get("index")) for g in after}
        for g in before:
            if (g.get("slot-id"), g.get("index")) not in keep:
                self.removed_at[self.gpus.get(g.get("slot-id"), [None] * 4)[g.get("index", 0)]] = time.monotonic()

    def vpod_written(self, name):
        if not any(self.agent_up.values()) and "configfailed_never" not in self.f:
            self.configfailed.add(name)
        if "vpod_fails_with_cp_switch_down" in self.f and not all(self.agent_up.values()):
            self.cf_perm.add(name)
        if "cp_lost_on_vpod_create" in self.f and not name.endswith(".system"):
            self.stopped |= set(self.sb_pid)

    # ------------------------------------------------------------ switch "SSH"
    def agent(self, i, action):
        if action == "stop":
            self.flaps += 1
            self.stops[i] += 1
            self.agent_up[i] = False
            if "no_unreachable_event" not in self.f:
                self.event("switchnode_unreachable", f"switch {self.sname(i)} unreachable")
            return
        if (i in self.fru_bad and "fru_ignored" not in self.f) or (i in self.mgmt_eth1 and "agent_fails_on_eth1" in self.f):
            self.agent(i, "stop")       # the switch agent refuses to start
            return
        was_down = not self.agent_up[i] or i in self.sw_stopped
        if action == "restart":
            self.last_agent_restart = time.monotonic()
            if "mch_layout_lost_on_agent_restart" in self.f:
                self.layout_lost = True
            if "pairwise_wedge" in self.f and time.monotonic() - self.last_ctrl_restart < 2:
                self.wedged.add(i)
        self.agent_up[i] = True
        self.stick_slot_ports([s for s in self.sb_pid if self.slot_host(s) == i])
        self.sw_stopped.discard(i)
        if "controller_unstable" in self.f:
            j = self.switch_ips.index(self.leader)
            self.ctrl_restarts[j] += 1
            self.ctrl_started[j] = time.time()
        if "ports_not_recovered" in self.f and was_down:
            self.stale.add(i)
        if self.sname(i) in self.switches:
            self.event("switchnode_healthy", f"switch {self.sname(i)} healthy")
        if "gpu_dup_after_restart" in self.f:
            sysv = next((v for v in self.st.objs["VPod"].values() if v["meta"]["name"].endswith(".system")), None)
            for v in self.st.objs["VPod"].values():
                if sysv is None or v is sysv:
                    continue
                for g in v["spec"].get("GPUs") or []:
                    if self.slot_host(g.get("slot-id")) == i and g not in sysv["spec"]["GPUs"]:
                        sysv["spec"]["GPUs"].append(dict(g))
        if "no_reconcile" not in self.f:
            for k, d in self.links_down.items():
                if k[0] == i and not d["known"]:
                    d.update(known=True, since=time.monotonic())
                    self.event("link_down", f"PortName {k[2]} asic {k[1]} switch {self.sname(i)} is DOWN (reconciled)")
        if self.sname(i) not in self.switches and i in self.identity_reset:
            self.switches[self.sname(i)] = _obj("SwitchNode", self.sname(i),
                                                {"ip-address": self.switch_ips[i], "slot-id": self.sw_slot(i)},
                                                {"admission-phase": "admitted", "conditions": []})
            if "stale_planes" in self.f:
                self.stale.add(i)
            self.identity_reset.discard(i)
            self.event("switchnode_admitted", f"switch {self.sname(i)} admitted")
        for slot in self.sb_pid:
            if self.slot_host(slot) == i:
                if "no_respawn" not in self.f:
                    self.sb_pid[slot] = next(_pids)
                self.stopped.discard(slot)
                if self.cname(slot) not in self.computes:   # readmit a deleted compute
                    gi = {u: {"index": g, "acc-id": slot * 4 + g + (200 if "accid_new_on_readmit" in self.f else 0),
                              "oper-state": "Ready", "vpod-membership": "",
                              "ports": [{"id": p, "oper-state": "PortReady"} for p in range(36)],
                              "ifoe-stations": list(range(18))} for g, u in enumerate(self.gpus[slot])}
                    if "no_readmit" not in self.f:
                        self.computes[self.cname(slot)] = self._compute_obj(slot, gi)
                        self.readmit_at[slot] = time.monotonic()
                        self.lifecycle_event("admitted", slot)
                        if "readmit_forgets_down_ports" in self.f:
                            for k, d in self.links_down.items():
                                if self.ports(k[0], k[1])[k[2]][0] == slot:
                                    d["known"] = False

    def controller(self, i, action):
        """docker stop|start|restart afm-controller on switch i."""
        ip = self.switch_ips[i]
        q = self.quorum.index(ip) if ip in self.quorum else None
        if action in ("stop", "restart"):
            if q is not None:
                self.ctrl_up[q] = False
            was_leader = ip == self.leader
            if was_leader:
                up = [self.quorum[k] for k, u in self.ctrl_up.items() if u]
                if up:
                    self.leader = up[0]
            for w in list(self.st.watchers):
                if was_leader or "watch_breaks_on_nonleader" in self.f:
                    w["close"] = True
            if was_leader and "ws_no_reconnect_after_failover" in self.f:
                self.ws_refuse = True
        if action in ("start", "restart"):
            if "mch_layout_lost_on_ctrl_restart" in self.f:
                self.layout_lost = True
            if q is not None and "no_rejoin" not in self.f:
                self.ctrl_up[q] = True
            self.ctrl_started[i] = time.time()
            self.ctrl_restarts[i] += 1
            if "no_logs_after_restart" not in self.f:
                self.ctrl_log[i] = time.time()

    def restart_controller(self, i):
        if "pairwise_wedge" in self.f and time.monotonic() - self.last_agent_restart < 2:
            self.wedged.add(i)
        self.last_ctrl_restart = time.monotonic()
        if "agents_no_reconnect" in self.f and self.switch_ips[i] == self.leader:
            self.disconnected = True
        self.controller(i, "restart")
        self.stick_slot_ports(list(self.sb_pid))
        if "vlan_wipe" in self.f:
            self.wiped |= {j for j in range(self.n_sw) if self.phase_sw(j) == "drained"}

    def vlans(self, i) -> list[str]:
        user = [f"Vlan{v}" for v in sorted(self.user_vlans.get(i, set()))]
        if self.phase_sw(i) not in ("admitted", "drained") or i in self.wiped:
            return [] if "decommission_wipes_user_vlans" in self.f else user
        users = [] if "no_vlan_for_vpod" in self.f else sorted(
            v["meta"]["name"] for v in self.st.objs["VPod"].values()
            if not v["meta"]["name"].endswith(".system") and v["spec"].get("GPUs"))
        return ["Vlan100", "Vlan200", "Vlan300"] + [f"Vlan{1000 + n}" for n in range(len(users))] + user

    # ------------------------------------------------------------ lab equipment (runner-side commands)
    def mgmt_compute(self, slot, down: bool):
        if down:
            self.mgmt_down_ct.add(slot)
            if "mgmt_loss_unnoticed" not in self.f and "no_mgmt_loss_event" not in self.f:
                self.event("computenode_unreachable", f"compute {self.cname(slot)} unreachable")
        elif slot in self.mgmt_down_ct:
            self.mgmt_down_ct.discard(slot)
            self.event("computenode_healthy", f"compute {self.cname(slot)} healthy")

    def mgmt_switch(self, i, down: bool):
        """A switch's management network cut / restored: its controller member drops out (leader re-elected)."""
        ip = self.switch_ips[i]
        if ip not in self.quorum:
            return
        q = self.quorum.index(ip)
        if down:
            self.ctrl_up[q] = False
            if ip == self.leader and "leader_stuck" not in self.f:
                up = [self.quorum[k] for k, u in self.ctrl_up.items() if u]
                if up:
                    self.leader = up[0]
            if "mgmt_flap_restarts_controllers" in self.f:
                for j, other in enumerate(self.switch_ips):
                    if other in self.quorum and j != i:
                        self.ctrl_restarts[j] += 1
                        self.ctrl_started[j] = time.time()
        elif "no_rejoin" not in self.f:
            self.ctrl_up[q] = True

    def cluster_move(self, target) -> bool:
        """Move a non-leader controller member to ``target`` (a switch IP outside the quorum)."""
        if target not in self.switch_ips or target in self.quorum:
            return False
        if "cluster_move_noop" not in self.f:
            q = next(k for k, ip in enumerate(self.quorum) if ip != self.leader)
            self.quorum[q] = target
        return True

    def reboot_switch(self, i):
        if self.switch_ips[i] in self.quorum:
            self.controller(i, "restart")
        if "upgrade_not_readmitted" in self.f:
            self.agent(i, "stop")
        else:
            self.agent(i, "restart")

    def lifecycle_event(self, what, slot):
        if "no_compute_lifecycle_events" not in self.f:
            self.event(f"computenode_{what}", f"compute {self.cname(slot)} {what}")

    def vpod_event(self, etype, name, msg):
        if "no_vpod_events" not in self.f:
            self.event(etype, f"vPod {name} {msg}")

    def macs(self, i, objs) -> int:
        if self.phase_sw(i) not in ("admitted", "drained"):
            return 0
        return sum(len(v["spec"].get("GPUs") or []) for v in objs["VPod"].values()) + self.stale_macs


class FakeSwitchTransport:
    """Switch 'SSH' bound to the simulated rack; anything it does not model goes to the replay fallback."""

    def __init__(self, host, st, index: int, fallback):
        self.host, self.st, self.i, self.fallback = host, st, index, fallback

    def _res(self, cmd, out="", rc=0):
        return CmdResult(self.host.name, cmd, rc, out, "")

    def run(self, cmd: str, *, timeout: float = 120, sudo: bool = False, env=None) -> CmdResult:
        st, sim, i = self.st, self.st.sim, self.i
        res = None
        with st.lock:
            if cmd == "docker exec afm-agent ps -eo pid,args":
                lines = ["    1 /usr/bin/afm_agent.sh"] + ([] if "no_switch_agent_process" in st.f else ["   20 /switch-agent"]) \
                    if sim.agent_up[i] else []
                if "split_gpu_set" in st.f and not all(sim.agent_up.values()) and sim.agent_up[i]:
                    lines += [f" {sim.sb_pid[s] + 1} /sb-node-agent -slot-id {s} -controllers {sim.leader}"
                              for s in sorted(sim.sb_pid) if sim.slot_host(s) not in (i, None)]
                lines += [f" {sim.sb_pid[s]} /sb-node-agent -slot-id {s} -controllers {sim.leader}"
                          for s in sorted(sim.sb_pid) if sim.slot_host(s) == i
                          or ("duplicate_node_agent" in st.f and s == 0 and i == (sim.slot_host(0) or 0) + 1)]
                res = self._res(cmd, "\n".join(lines) + "\n")
            elif m := re.fullmatch(r"systemctl (stop|start|restart) afm-agent", cmd):
                sim.agent(i, m.group(1))
                res = self._res(cmd)
            elif cmd == "docker restart afm-agent":
                sim.agent(i, "restart")
                res = self._res(cmd, "afm-agent\n")
            elif cmd == "docker restart afm-controller":
                sim.restart_controller(i)
                res = self._res(cmd, "afm-controller\n")
            elif m := re.fullmatch(r"docker (stop|start) afm-controller", cmd):
                sim.controller(i, m.group(1))
                res = self._res(cmd, "afm-controller\n")
            elif cmd.startswith("docker inspect -f") and cmd.endswith("afm-controller"):
                t = datetime.fromtimestamp(sim.ctrl_started[i], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                res = self._res(cmd, f"{t} {sim.ctrl_restarts[i]}\n")
            elif cmd == "date +%s":
                res = self._res(cmd, f"{int(time.time())}\n")
            elif cmd == "date -u +%Y-%m-%dT%H:%M:%S":
                res = self._res(cmd, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "\n")
            elif m := re.fullmatch(r"find /opt/amd/afm/controller/logs -type f -newermt @(\d+)( -print)?", cmd):
                newer = sim.ctrl_log[i] > int(m.group(1))
                res = self._res(cmd, "/opt/amd/afm/controller/logs/afm_controller.log\n" if newer else "")
            elif m := re.fullmatch(r"docker exec afm-agent kill -(STOP|CONT) (\d+)", cmd):
                slot = next((s for s, p in sim.sb_pid.items() if p == int(m.group(2))), None)
                if int(m.group(2)) == 20:          # the switch-agent process
                    (sim.sw_stopped.add if m.group(1) == "STOP" else sim.sw_stopped.discard)(i)
                    res = self._res(cmd)
                elif slot is None:
                    res = self._res(cmd, rc=1)
                else:
                    if m.group(1) == "STOP":
                        sim.stopped.add(slot)
                    elif slot in sim.stopped:
                        sim.stopped.discard(slot)
                        sim.stick_slot_ports([slot])
                        if "no_healthy_event" not in st.f:
                            sim.event("computenode_healthy", f"compute {sim.cname(slot)} healthy")
                    res = self._res(cmd)
            elif cmd == "true":
                res = self._res(cmd)
            elif cmd == "sonic-installer list":
                im = sim.images[i]
                res = self._res(cmd, f"Current: {im['current']}\nNext: {im['current']}\nAvailable:\n"
                                     + "".join(f"{a}\n" for a in im["available"]))
            elif m := re.fullmatch(r"helios-fake (sonic-upgrade|onie-install) (\S+)", cmd):
                im = sim.images[i]
                im["current"] = m.group(2)
                im["available"] = sorted(set(im["available"]) | {m.group(2)})
                sim.reboot_switch(i)
                res = self._res(cmd, f"Installed {m.group(2)}, rebooting\n")
            elif cmd == "helios-fake fw-upgrade":
                sim.reboot_switch(i)
                res = self._res(cmd, "firmware updated, rebooting\n")
            elif m := re.fullmatch(r"sonic-installer install -y (\S+)", cmd):
                im, new = sim.images[i], "SONiC-OS-" + m.group(1).rsplit("/", 1)[-1].removesuffix(".bin")
                im["available"] = sorted(set(im["available"]) | {new})
                if "local_install_switches_image" in st.f:
                    im["current"] = new
                res = self._res(cmd, f"Installed {new}\n")
            elif m := re.fullmatch(r"sonic-installer remove -y (\S+)", cmd):
                im = sim.images[i]
                im["available"] = [a for a in im["available"] if a != m.group(1) or a == im["current"]]
                res = self._res(cmd)
            elif cmd == "helios-fake bad-upgrade":
                if "no_rollback" in st.f:
                    sim.images[i]["current"] = "SONiC-OS-broken"
                sim.reboot_switch(i)
                res = self._res(cmd, "image verification failed, rolled back\n", rc=1)
            elif m := re.fullmatch(r"helios-mgmt move (eth0|eth1)", cmd):
                (sim.mgmt_eth1.add if m.group(1) == "eth1" else sim.mgmt_eth1.discard)(i)
                res = self._res(cmd)
            elif re.fullmatch(rf"cp -p {FRU} {FRU}\.helios-bak", cmd):
                res = self._res(cmd)
            elif cmd == f"sh -c 'echo {{not-json > {FRU}'":
                sim.fru_bad.add(i)
                res = self._res(cmd)
            elif cmd == f"mv -f {FRU}.helios-bak {FRU}":
                sim.fru_bad.discard(i)
                res = self._res(cmd)
            elif cmd.startswith("rm -f /opt/amd/afm/agent/sysconfig/"):
                sim.identity_reset.add(i)
                res = self._res(cmd)
            elif m := re.fullmatch(r"sonic-db-cli(?: -n (asic\d+))? CONFIG_DB keys 'VLAN_MEMBER\|\*'", cmd):
                ns_ = m.group(1) or "asic0"
                res = self._res(cmd, "".join(f"VLAN_MEMBER|Vlan100|{p}\n" for p in sim.ports(i, ns_)
                                             if sim.lane_used(i, ns_, p) or "mch_vlan_on_unused" in st.f))
            elif m := re.fullmatch(r"sonic-db-cli(?: -n (asic\d+))? CONFIG_DB hgetall 'VLAN_MEMBER\|Vlan\d+\|Ethernet\d+'", cmd):
                res = self._res(cmd, repr({"tagging_mode": "tagged" if "mch_tagged" in st.f else
                                           "priority_tagged" if sim.mch else "untagged"}) + "\n")
            elif m := re.fullmatch(r"sonic-db-cli(?: -n (asic\d+))? CONFIG_DB keys 'VLAN\|\*'", cmd):
                res = self._res(cmd, "".join(f"VLAN|{v}\n" for v in sim.vlans(i)))
            elif m := re.fullmatch(r"show mac(?: -n (asic\d+))?", cmd):
                n = sim.macs(i, st.objs)
                rows = "".join(f"  {k + 1}  Vlan100  02:00:00:00:{k:02x}:00  Ethernet{k}  Static\n" for k in range(n))
                res = self._res(cmd, "  No.  Vlan  MacAddress  Port  Type\n-----  ----  ----  ----  ----\n" + rows)
            elif m := re.fullmatch(r"show interfaces status -n (asic\d+) -d all", cmd):
                hdr = ("   Interface  Lanes            Speed   MTU  FEC  Alias   Vlan    Oper  Admin  Type  Asym PFC\n"
                       "------------  ---------------  -----  ----  ---  ------  ------  ----  -----  ----  --------\n")
                ns_ = m.group(1)
                rows = "".join("  ".join([f"{p:>12}", f"{'0,1,2,3,4,5,6,7':<15}", f"{'400G':>5}", f"{sim.mtu.get((i, ns_, p), 9100):>4}",
                                          f"{'rs':>3}", f"{'etp' + str(n):<6}", f"{sim.port_vlan(i, ns_, p):<6}",
                                          f"{sim.link_state(i, ns_, p):<4}", f"{sim.admin(i, ns_, p):<5}",
                                          f"{'OSFP':<4}", f"{'N/A':<8}"]) + "\n"
                               for n, p in enumerate(sorted(sim.ports(i, ns_), key=lambda x: int(x[8:]))))
                res = self._res(cmd, hdr + rows)
            elif m := re.fullmatch(r"config interface -n (asic\d+) mtu (Ethernet\d+) (\d+)", cmd):
                key, mtu = (i, m.group(1), m.group(2)), int(m.group(3))
                if key[2] in sim.ports(i, key[1]):
                    if mtu >= IFCP_MTU and sim.mtu.get(key, 9100) < IFCP_MTU and "mtu_sticky" in st.f:
                        s_, g_, _, pid_ = sim.ports(i, key[1])[key[2]]
                        sim.ifcp_stuck.add((s_, g_, pid_))
                    sim.mtu[key] = mtu
                res = self._res(cmd, rc=0 if key[2] in sim.ports(i, key[1]) else 1)
            elif m := re.fullmatch(r"config interface -n (asic\d+) (shutdown|startup) (Ethernet\d+)", cmd):
                ok = sim.set_link(i, m.group(1), m.group(3), m.group(2) == "startup")
                res = self._res(cmd, rc=0 if ok else 1)
            elif "CONFIG_DB keys 'PORT|Ethernet*'" in cmd and cmd.startswith("for k in"):
                ns = re.search(r"-n (asic\d+)", cmd).group(1)
                off = 1 if "desc_acc_off_by_one" in st.f else 0
                res = self._res(cmd, "".join(f"{p}|To ComputeTray Slot{s}/GPU{g}[{acc + off}]/Port{pid}\n"
                                             for p, (s, g, acc, pid) in sim.ports(i, ns).items()))
            elif "CONFIG_DB keys 'FDB|*'" in cmd and cmd.startswith("for k in"):
                # static FDB: one entry per VLAN-member port of an admitted compute (fault stale_fdb: + one in Vlan2)
                ns = re.search(r"-n (asic\d+)", cmd).group(1)
                lines = []
                for p, (s, g, acc, pid) in sim.ports(i, ns).items():
                    c = sim.computes.get(sim.cname(s))
                    if c and c["status"]["admission-phase"] == "admitted" and sim.lane_used(i, ns, p):
                        lines.append(f"FDB|Vlan100|02:00:00:{acc:02x}:{pid:02x}:01|{p}")
                if "stale_fdb" in st.f and lines:
                    lines.append(f"FDB|Vlan2|02:00:00:ff:00:01|{lines[0].rsplit('|', 1)[1]}")
                res = self._res(cmd, "\n".join(lines) + "\n")
            elif m := re.fullmatch(r"docker exec afm-agent curl -s -m 5 localhost:(\d+)/api/runtime/requests", cmd):
                slot = int(m.group(1)) - 13000
                ok = sim.agent_up[i] and sim.slot_host(slot) == i and "no_agent_requests" not in st.f
                res = self._res(cmd, json.dumps([{"id": f"req-{slot}", "created-at": now(), "status": "applied"}]) if ok else "",
                                0 if ok else 7)
            elif m := re.fullmatch(r"config vlan (add|del) -n (asic\d+) (\d+)", cmd):
                (sim.user_vlans.setdefault(i, set()).add if m.group(1) == "add" else sim.user_vlans.setdefault(i, set()).discard)(int(m.group(3)))
                res = self._res(cmd)
            elif m := re.fullmatch(r"docker exec afm-agent curl -s -m 5 localhost:(\d+)/api/health", cmd):
                slot = int(m.group(1)) - 13000
                c = sim.computes.get(sim.cname(slot))
                conn = (c is not None and c["status"]["admission-phase"] == "admitted") or slot not in sim.sb_pid \
                    or "control_path_kept" in st.f
                conn = conn and not sim.disconnected
                res = self._res(cmd, json.dumps({"is-connected": conn, "healthy": True}) if sim.agent_up[i] else "",
                                0 if sim.agent_up[i] else 7)
        if res is None:
            return self.fallback.run(cmd, timeout=timeout, sudo=sudo, env=env)
        log.debug("[%s] $ %s", self.host.name, build_command(cmd, sudo, env))
        log.debug("[%s] rc=%s 0.00s (sim)%s", self.host.name, res.rc, ("\n" + res.stdout.rstrip()) if res.stdout else "")
        return res

    def close(self):
        pass

    reset = close


class FakeComputeTransport:
    """Compute-tray 'SSH' bound to the simulated rack: afmctl (devices, ports + IFCP, accelerator-addr,
    stations, update port, reset), clock and journal. Traffic tools fall back to the replay rules."""

    def __init__(self, host, st, slot: int, fallback):
        self.host, self.st, self.slot, self.fallback = host, st, slot, fallback

    # ---------------------------------------------------------------- model
    def _gpu(self, g):
        sim = self.st.sim
        c = sim.computes.get(sim.cname(self.slot))
        gi = next((x for x in ((c or {}).get("status", {}).get("scale-up-info", {}).get("gpu-info") or {}).values()
                   if x["index"] == g), None)
        return gi

    def _acc(self, g) -> int:
        gi = self._gpu(g)
        return gi["acc-id"] if gi else self.slot * 4 + g

    def _vpod_of(self, g):
        for v in self.st.objs["VPod"].values():
            if any(x.get("slot-id") == self.slot and x.get("index") == g for x in v["spec"].get("GPUs") or []):
                return v
        return None

    def _peers(self, g) -> list[int]:
        v = self._vpod_of(g)
        if not v:
            return []
        out = []
        for x in v["spec"].get("GPUs") or []:
            if (x.get("slot-id"), x.get("index")) != (self.slot, g):
                c = self.st.sim.computes.get(self.st.sim.cname(x.get("slot-id")))
                gi = next((y for y in ((c or {}).get("status", {}).get("scale-up-info", {}).get("gpu-info") or {}).values()
                           if y["index"] == x.get("index")), None)
                out.append(gi["acc-id"] if gi else x.get("slot-id") * 4 + x.get("index"))
        return sorted(out)

    def _phase(self, g) -> str:
        v = self._vpod_of(g)
        if not v:
            return "PROVIDER"
        if v["meta"]["name"] in self.st.sim.configfailed and "phase_active_when_failed" not in self.st.f:
            return "CONFIG_FAILED"
        return "ACTIVE"

    def _link(self, g, pid):
        """(switch idx, ns, switch port) cabled to GPU g port pid, or None."""
        sim = self.st.sim
        ia = sim.pid_link(pid)
        if ia is None:
            return None
        i, ns = ia[0], f"asic{ia[1]}"
        port = next((p for p, (s, gg, _, q) in sim.ports(i, ns).items() if s == self.slot and gg == g and q == pid), None)
        return (i, ns, port) if port else None

    def _port_up(self, g, pid) -> bool:
        k = self._link(g, pid)
        return bool(k) and pid in self.st.sim.used and k not in self.st.sim.links_down \
            and (self.slot, g, pid) not in self.st.sim.cmp_admin_down

    def _ports(self, g) -> list[dict]:
        sim = self.st.sim
        out = []
        for pid in sim.gpu_pids():
            k = self._link(g, pid)
            up = self._port_up(g, pid)
            dis = pid not in sim.used and "mch_unused_enabled" not in self.st.f and not sim.layout_lost
            ll = up and not ((self.slot, g, pid) in sim.ifcp_stuck) and sim.mtu.get(k, 9100) >= IFCP_MTU and \
                not ("ifcp_baseline_down" in self.st.f and (self.slot, g, pid) == (0, 0, min(sim.used)))
            mac = pid in sim.used or "mch_mac_on_unused" in self.st.f
            out.append({"id": str(pid), "spec": {"station_id": str(pid // 2 + (1 if "mch_station_ids_wrong" in self.st.f
                                                                               and pid == 7 else 0)),
                                                  "nw_mac_addr": f"02:00:{self.slot:02x}:{g:02x}:{pid:02x}:01" if mac else "",
                                                  "admin_state": "disabled" if dis else
                                                  "idle" if (self.slot, g, pid) in sim.cmp_admin_down else "enabled"},
                        "status": {"name": f"netport{pid}",
                                   "link_status": "LINK_DISABLED" if dis else "LINK_UP" if up else "LINK_DOWN",
                                   "ifcp": {"port_id": str(pid), "link_up": "yes" if up else "no",
                                            "ll_up": "yes" if ll else "no", "in_error": "no",
                                            "peer_device_id": str(k[0]) if k else "", "peer_port_id": k[2][8:] if k else "",
                                            "peer_enabled": "yes" if ll else "no", "peer_in_error": "no"}}})
        return out

    @staticmethod
    def bdf(g):
        return f"000{g + 1}:01:00.1"

    def _gpus(self, bdfs=None):
        gs = range(4)
        if bdfs:
            want = set(bdfs.split(","))
            gs = [g for g in gs if self.bdf(g) in want]
        return gs

    # ---------------------------------------------------------------- commands
    def run(self, cmd: str, *, timeout: float = 120, sudo: bool = False, env=None) -> CmdResult:
        st, sim = self.st, self.st.sim
        c = re.sub(r"\s+2>(/dev/null|&1)$", "", cmd.strip())
        out, rc = None, 0
        with st.lock:
            st.sim.refresh(st.objs)
            if c == "afmctl show device --json":
                local = [str(self._acc(g)) for g in range(4)]
                out = json.dumps({"device": [{"bdf": self.bdf(g), "spec": {
                    "accelerator_id": str(self._acc(g)), "config_phase": self._phase(g), "local_accelerators": local,
                    "vpod_accelerators": sorted({str(self._acc(g))} | {str(p) for p in self._peers(g)}, key=int)}}
                    for g in range(4)]})
            elif m := re.fullmatch(r"afmctl show port -b (\S+) --json", c):
                out = json.dumps({"device": [{"bdf": self.bdf(g), "port": self._ports(g)} for g in self._gpus(m.group(1))]})
            elif c == "afmctl show port accelerator-addr --json":
                devs = []
                for g in range(4):
                    ports = []
                    for pid in sim.gpu_pids():
                        stale = (self.slot, g, pid) in sim.flapped and "stale_fwd_table" in st.f
                        peers = [] if (not self._port_up(g, pid) or stale) else self._peers(g)
                        if "mch_dest_on_unused" in st.f and pid not in sim.used:
                            peers = self._peers(g)
                        if "fwd_table_incomplete" in st.f and peers:
                            peers = peers[1:]
                        ports.append({"port_id": str(pid), "accelerator_addr": [
                            {"accelerator_id": str(a), "mac_addr": f"02:00:00:{a:02x}:{pid:02x}:00"} for a in peers]})
                    devs.append({"bdf": self.bdf(g), "port": ports})
                out = json.dumps({"device": devs})
            elif m := re.fullmatch(r"afmctl show station(?: -b (\S+))? --json", c):
                devs = []
                for g in self._gpus(m.group(1)):
                    stations = []
                    for k in (range(18) if sim.mch else range(sim.n_sw)):
                        ports = [p for p in (2 * k, 2 * k + 1) if p in sim.used or "mch_unused_mapped" in st.f]
                        c = sim.computes.get(sim.cname(self.slot)) or {}
                        drained = {(x.get("gpu-id"), x.get("port-id")) for x in (c.get("spec") or {}).get("drained-ports") or []}
                        live = ports if "station_map_stale" in st.f else \
                            [p for p in ports if self._port_up(g, p) and (g, p) not in drained
                             and (self.slot, g, p) not in sim.map_stuck]
                        if self.slot in sim.reset_broken:
                            live = []
                        pm = lambda ps: [{"accelerator_id": str(a), "path_to_port_map": [  # noqa: E731
                            {"path": str((p - 2 * k) ^ (1 if "mch_path_bits_swapped" in st.f else 0)), "port": str(p)}
                            for p in ps]} for a in self._peers(g)]
                        stations.append({"id": str(k), "spec": {"device_bdf": self.bdf(g), "admin_state": "active",
                                                                "accel_path_map": pm(ports)},
                                         "status": {"oper_state": "up" if live else "down",
                                                    "accel_path_map": pm(ports if "mch_map_static" in st.f else live)}})
                    devs.append({"bdf": self.bdf(g), "station": stations})
                out = json.dumps({"device": devs})
            elif (m := re.fullmatch(r"env AFMCTL_FORCE_UPDATE=1 afmctl update port -b (\S+) -s (\d+) -p (\S+) --admin-state (\S+)", c)) \
                    and m.group(4) not in ("idle", "enable", "power-off"):
                # real afmctl: shows "enabled" but only accepts "enable" (MR3, 16 Oct)
                out, rc = f"Invalid admin state : {m.group(4)} (valid: power-off, idle, enable)\n", 255
            elif m := re.fullmatch(r"env AFMCTL_FORCE_UPDATE=1 afmctl update port -b (\S+) -s (\d+) -p (\S+) --admin-state (\S+)", c):
                g = [g for g in range(4) if self.bdf(g) == m.group(1)][0]
                for pid in map(int, m.group(3).split(",")):
                    key = (self.slot, g, pid)
                    k = self._link(g, pid)
                    if m.group(4) == "idle":
                        sim.cmp_admin_down.add(key)
                        quiet = "no_compute_port_events" in st.f
                        if k:
                            sim.set_link(k[0], k[1], k[2], up=False, quiet=quiet)
                            if not quiet:
                                sim.event("ifoe_netport_link_down", f"Port (acc-id: {self._acc(g)}, port-id: {pid}) link down")
                    else:
                        sim.cmp_admin_down.discard(key)
                        sim.flapped.add(key)
                        if "ifcp_no_recover" in st.f:
                            sim.ifcp_stuck.add(key)
                        quiet = "no_compute_port_events" in st.f
                        if k:
                            sim.set_link(k[0], k[1], k[2], up=True, quiet=quiet)
                            if not quiet:
                                sim.event("ifoe_netport_link_up", f"Port (acc-id: {self._acc(g)}, port-id: {pid}) link up")
                out = f"({m.group(1)}) : port admin state updated\n"
            elif m := re.fullmatch(r"afmctl reset device (?:-b (\S+)|--datapath)", c):
                gs = self._gpus(m.group(1)) if m.group(1) else range(4)
                if "reset_not_logged" not in st.f:
                    sim.journal.setdefault(self.slot, []).append(time.time())
                if sim.mch and "mch_reset_loses_layout" in st.f:
                    sim.reset_broken.add(self.slot)
                out = "".join(f"Device {self.bdf(g)} reset successfully\n" for g in gs)
            elif m := re.fullmatch(r"afmctl show port statistics mac(?: -b (\S+))? --json", c):
                sim.stat_reads = getattr(sim, "stat_reads", 0) + (0 if "stats_frozen" in st.f else 1)
                busy = lambda pid: "mch_stats_on_unused" in st.f and pid not in sim.used  # noqa: E731
                n = sim.stat_reads * 1000
                # the real afmctl layout (g02-g17, 16 Oct): "ports", tx/rx_total_packets (+ many more counters)
                out = json.dumps({"devices": [{"bdf": self.bdf(g), "ports": [
                    {"station_id": str(pid // 2), "port_id": str(pid),
                     "statistics": {"tx_total_packets": str(n if self._port_up(g, pid) or busy(pid) else 0),
                                    "rx_total_packets": str(n if self._port_up(g, pid) or busy(pid) else 0),
                                    "rx_bad_fcs": "0"}}
                    for pid in sim.gpu_pids()]} for g in self._gpus(m.group(1))]})
            elif m := re.fullmatch(r"cat /sys/bus/pci/devices/(\S+)/ualink/station_lane_en_bitmap", c):
                g = next((g for g in range(4) if self.bdf(g) == m.group(1)), None)
                if g is None or not sim.mch:
                    out, rc = "", 1
                else:
                    live = sim.used if "mch_mask_static" in st.f else {p for p in sim.used if self._port_up(g, p)}
                    if "mch_wrong_strategy" in st.f:
                        live = {p for p in range(36) if (p // 2) % 2 == 0}      # 9x2 / 9x0
                    out = "0x" + "".join("%x" % ((0x3 if 2 * k in live else 0) | (0xC if 2 * k + 1 in live else 0))
                                         for k in range(18)) + "\n"
            elif c == "lsmod | grep -q '^ifoe_mock '":
                out, rc = "", 1 if "mch_mock_driver_missing" in st.f else 0
            elif c == "date -u '+%Y-%m-%d %H:%M:%S'":
                out = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "\n"
            elif m := re.fullmatch(r"journalctl -t afmctl --since '([^']+) UTC' --no-pager", c):
                since = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                out = "".join(f"afmctl[{4242 + n}]: phase=end sub_cmd=reset status=0\n"
                              for n, t in enumerate(sim.journal.get(self.slot, [])) if t >= since - 1)
        if out is None:
            return self.fallback.run(cmd, timeout=timeout, sudo=sudo, env=env)
        res = CmdResult(self.host.name, cmd, rc, out, "")
        log.debug("[%s] $ %s", self.host.name, build_command(cmd, sudo, env))
        log.debug("[%s] rc=%s 0.00s (sim)%s", self.host.name, rc, ("\n" + out.rstrip()[:2000]) if out else "")
        return res

    def close(self):
        pass

    reset = close


class FakeRunnerTransport:
    """The test runner's local shell for the lab-equipment commands of the self-test inventories (management
    network cuts, syslog server check, cluster move, bootstrap, AFM upgrade); anything else goes to the fallback."""

    def __init__(self, st, fallback, computes: dict[str, int], switches: dict[str, int]):
        self.st, self.fallback, self.computes, self.switches = st, fallback, computes, switches

    def run(self, cmd: str, *, timeout: float = 120, sudo: bool = False, env=None) -> CmdResult:
        st, sim = self.st, self.st.sim
        out, rc = None, 0
        with st.lock:
            if m := re.fullmatch(r"helios-fake mgmtnet (down|up) (\S+) \S*", cmd):
                name, down = m.group(2), m.group(1) == "down"
                if name in self.computes:
                    sim.mgmt_compute(self.computes[name], down)
                elif name in self.switches:
                    sim.mgmt_switch(self.switches[name], down)
                else:
                    rc = 1
                out = ""
            elif m := re.fullmatch(r"helios-fake syslog-grep (\S+)", cmd):
                exported = bool(st.objs["SyslogExport"]) and "syslog_export_dropped" not in st.f
                hit = any(m.group(1) in json.dumps(a) for a in st.audit)
                out, rc = "", 0 if exported and hit else 1
            elif m := re.fullmatch(r"helios-fake cluster-move (\S+)", cmd):
                out, rc = "", 0 if sim.cluster_move(m.group(1)) else 1
            elif m := re.fullmatch(r"helios-fake bootstrap --rack-type (\S+)", cmd):
                ok = m.group(1) in ("heliosmch", "heliosmchmini", "minirack", "fullrack") or \
                    "bootstrap_accepts_invalid" in st.f
                out, rc = ("bootstrap ok\n", 0) if ok else (f"unsupported rack type {m.group(1)}\n", 2)
            elif cmd == "helios-fake afm-upgrade":
                for i in range(sim.n_sw):
                    sim.controller(i, "restart")
                if "upgrade_resets_rack_type" in st.f:
                    for pod in st.objs["Pod"].values():
                        pod["spec"]["rack-type"] = "minirack"
                out = "upgraded\n"
        if out is None:
            return self.fallback.run(cmd, timeout=timeout, sudo=sudo, env=env)
        log.debug("[runner] $ %s\n[runner] rc=%s (sim)", cmd, rc)
        return CmdResult("runner", cmd, rc, out, "")

    def close(self):
        pass

    reset = close
