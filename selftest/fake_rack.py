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
BASELINE_DOWN = 3          # system vPOD baseline 36/33/33 (degraded), as on the full rack
_pids = itertools.count(4000)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _obj(kind, name, spec, status):
    return {"kind": kind, "api-version": "v1",
            "meta": {"name": name, "tenant": "default", "uuid": str(uuidlib.uuid4()), "creation-time": now(),
                     "mod-time": now(), "generation-id": "1"}, "spec": spec, "status": status}


class Sim:
    def __init__(self, st, n_switches: int = 1, n_computes: int = 2):
        self.st, self.f = st, st.f
        self.n_sw = n_switches
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
        self.n_ct = n_computes
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
            switches[self.sname(i)] = _obj("SwitchNode", self.sname(i), {"ip-address": ip, "slot-id": i},
                                           {"admission-phase": "admitted", "conditions": []})
        early = (datetime.now(timezone.utc) - __import__("datetime").timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if "computes_before_switches" not in self.f:
            for o in switches.values():          # bring-up order: switches are admitted first
                o["meta"]["creation-time"] = early
        self.computes, self.switches = computes, switches

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

    def sw_index(self, name):
        return int(name.split("-")[2])

    def in_service(self):
        return [i for i in range(self.n_sw) if self.phase_sw(i) == "admitted"]

    def phase_sw(self, i):
        o = self.switches.get(self.sname(i))
        return o["status"]["admission-phase"] if o else "absent"

    def slot_host(self, slot) -> int | None:
        """Switch index running the slot's sb-node-agent: slot % n, re-homed to the next live switch."""
        live = [i for i in range(self.n_sw) if self.phase_sw(i) in ("admitted", "drained") and self.agent_up[i]]
        pref = slot % self.n_sw
        if pref in live or not live:
            return pref if self.agent_up[pref] else None
        return live[slot % len(live)]

    # ------------------------------------------------------------ cabling
    def ports(self, i, ns):
        """{port: (slot, gpu, acc, gpu_port)}: one port per GPU per ASIC; plane/GPU port id = 2*switch + asic."""
        a = int(ns[4:])
        return {f"Ethernet{a * 256 + (s * 4 + g) * 8}": (s, g, s * 4 + g, 2 * i + a)
                for s in range(self.n_ct) for g in range(4)}

    def link_state(self, i, ns, port):
        return "down" if (i, ns, port) in self.links_down else "up"

    def admin(self, i, ns, port):
        return "down" if (self.links_down.get((i, ns, port)) or {}).get("admin") else "up"

    def stick_slot_ports(self, slots):
        """Fault ifcp_no_recover: IFCP sessions of these slots' ports never come back."""
        if "ifcp_no_recover" in self.f:
            for s in slots:
                for g in range(4):
                    for pid in range(2 * self.n_sw):
                        self.ifcp_stuck.add((s, g, pid))

    def set_link(self, i, ns, port, up: bool, quiet: bool = False):
        key = (i, ns, port)
        ports = self.ports(i, ns)
        if port not in ports:
            return False
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
                    if "ifcp_no_recover" in self.f:
                        self.ifcp_stuck.add((s_, g_, pid_))
                if d is not None and not quiet:
                    self.event("link_up", f"PortName {k[2]} asic {k[1]} switch {self.sname(i)} is UP")
                    had_failed = k in self.failed_evt
                    if ("flap_counts_as_failure" in self.f and not had_failed) or \
                            ("no_auto_restore" in self.f and had_failed):
                        self.sticky.add(k)
                self.failed_evt.discard(k)
            elif k not in self.links_down:
                self.links_down[k] = {"since": time.monotonic(), "known": self.agent_up[i], "admin": k == key}
                if self.agent_up[i] and not quiet:
                    self.event("link_down", f"PortName {k[2]} asic {k[1]} switch {self.sname(i)} is DOWN")
        return True

    def failed_links(self, objs, vpod) -> list[tuple]:
        """Down links affecting a vPOD's GPUs whose down time exceeded its failover policy threshold."""
        pol = objs["FailoverPolicy"].get(vpod["spec"].get("failover-policy") or "system-failover-policy") or {}
        th = float((pol.get("spec") or {}).get("link-fail-threshold", 10)) * float(self.time_scale)
        if "timer_ignored" in self.f:
            th = 0.0
        gpus = {(g.get("slot-id"), g.get("index")) for g in vpod["spec"].get("GPUs") or []}
        out, seen = [], set()
        for k, d in list(self.links_down.items()) + [(k, None) for k in self.sticky]:
            s, g, _, _ = self.ports(k[0], k[1])[k[2]]
            if (s, g) not in gpus:
                continue
            if d is not None and not (d["known"] and time.monotonic() - d["since"] >= th):
                continue
            if "one_failure_per_gpu" in self.f and (s, g) in seen:
                continue
            seen.add((s, g))
            out.append(k)
        return out

    def event(self, etype, msg):
        self.events.append({"kind": "Event", "meta": {"name": str(uuidlib.uuid4()), "creation-time": now(),
                                                       "mod-time": now()}, "type": etype, "message": msg})
        if "no_alerts" in self.f:
            return
        for pol in list(self.st.objs["AlertPolicy"].values()):
            sp = pol.get("spec") or {}
            if sp.get("enable") and sp.get("resource", "Event") == "Event":
                self.st.alerts.append({"kind": "Alert", "meta": {"name": str(uuidlib.uuid4()), "creation-time": now()},
                                       "spec": {"alert-policy-id": pol["meta"]["name"], "state": "open"},
                                       "status": {"severity": sp.get("severity", "info"), "message": msg,
                                                  "event-type": etype}})

    # ------------------------------------------------------------ derived state
    def refresh(self, objs):
        """Recompute conditions, membership and vPOD status from the simulated state."""
        member = {}
        for vp in objs["VPod"].values():
            for g in (vp["spec"].get("GPUs") or []):
                member[(g.get("slot-id"), g.get("index"))] = vp["meta"]["name"]
        for i in range(self.n_sw):
            o = self.switches.get(self.sname(i))
            if o:
                ok = (self.agent_up[i] or "agent_stop_ignored" in self.f) and \
                    o["status"]["admission-phase"] in ("admitted", "drained") and i not in self.sw_stopped \
                    and not ("switch_stuck_unknown" in self.f and self.stops[i] >= 2)
                o["status"]["conditions"] = [{"type": "healthy", "status": "true" if ok else "unknown"}]
        for name, o in self.computes.items():
            slot = o["spec"]["slot-id"]
            host = self.slot_host(slot)
            ok = host is not None and (slot not in self.stopped or "sigstop_ignored" in self.f) and \
                o["status"]["admission-phase"] == "admitted"
            o["status"]["conditions"] = [{"type": "healthy", "status": "true" if ok else "unknown"}]
            for u, g in o["status"]["scale-up-info"]["gpu-info"].items():
                g["vpod-membership"] = member.get((slot, g["index"]), "")
                for prt in g["ports"]:
                    prt["oper-state"] = "PortReady"
        lost = sum(PLANES // self.n_sw for i in range(self.n_sw) if self.phase_sw(i) != "admitted"
                   and not (self.phase_sw(i) in ("drained", "decommissioned") and "planes_no_drop" in self.f))
        lost += sum(PLANES // self.n_sw for i in self.stale)
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
            acked &= set(failed)
            counted = [k for k in failed if (not manual) or k in acked]
            pending = manual and any(k not in acked for k in failed) and "manual_auto" not in self.f
            avail = max(0, PLANES - (BASELINE_DOWN if sysv else 0) - lost - len(counted))
            if self.zero_reads.get(name):
                self.zero_reads[name] -= 1
                avail = 0
            if self.last_avail.get(name) not in (None, avail):
                self.vpod_event("vpod_bandwidth_change", name,
                                f"available/effective/provisioned switch planes changed to {avail}/{avail}/{PLANES}")
            self.last_avail[name] = avail
            st = vp["status"]
            st.update({"provisioned-switch-planes": PLANES, "available-switch-planes": avail,
                       "effective-switch-planes": avail, "pending-ops": st.get("pending-ops") or []})
            st["gpus-status"] = [{"gpu": {"id": self.gpus.get(g.get("slot-id"), [None] * 4)[g.get("index", 0)],
                                          "gpu-index": {"slot-id": g.get("slot-id"), "index": g.get("index")}},
                                  "state": "ready"} for g in vp["spec"].get("GPUs") or []]
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
            st["vpod-state"] = "online" if avail == PLANES else "degraded"

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

    # ------------------------------------------------------------ switch "SSH"
    def agent(self, i, action):
        if action == "stop":
            self.stops[i] += 1
            self.agent_up[i] = False
            if "no_unreachable_event" not in self.f:
                self.event("switchnode_unreachable", f"switch {self.sname(i)} unreachable")
            return
        was_down = not self.agent_up[i] or i in self.sw_stopped
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
                                                {"ip-address": self.switch_ips[i], "slot-id": i},
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
            if q is not None and "no_rejoin" not in self.f:
                self.ctrl_up[q] = True
            self.ctrl_started[i] = time.time()
            self.ctrl_restarts[i] += 1
            if "no_logs_after_restart" not in self.f:
                self.ctrl_log[i] = time.time()

    def restart_controller(self, i):
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
                lines = ["    1 /usr/bin/afm_agent.sh", "   20 /switch-agent"] if sim.agent_up[i] else []
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
            elif cmd.startswith("rm -f /opt/amd/afm/agent/sysconfig/"):
                sim.identity_reset.add(i)
                res = self._res(cmd)
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
                rows = "".join("  ".join([f"{p:>12}", f"{'0,1,2,3,4,5,6,7':<15}", f"{'400G':>5}", f"{'9100':>4}",
                                          f"{'rs':>3}", f"{'etp' + str(n):<6}", f"{'trunk':<6}",
                                          f"{sim.link_state(i, ns_, p):<4}", f"{sim.admin(i, ns_, p):<5}",
                                          f"{'OSFP':<4}", f"{'N/A':<8}"]) + "\n"
                               for n, p in enumerate(sorted(sim.ports(i, ns_), key=lambda x: int(x[8:]))))
                res = self._res(cmd, hdr + rows)
            elif m := re.fullmatch(r"config interface -n (asic\d+) (shutdown|startup) (Ethernet\d+)", cmd):
                ok = sim.set_link(i, m.group(1), m.group(3), m.group(2) == "startup")
                res = self._res(cmd, rc=0 if ok else 1)
            elif "CONFIG_DB keys 'PORT|Ethernet*'" in cmd and cmd.startswith("for k in"):
                ns = re.search(r"-n (asic\d+)", cmd).group(1)
                res = self._res(cmd, "".join(f"{p}|To ComputeTray Slot{s}/GPU{g}[{acc}]/Port{pid}\n"
                                             for p, (s, g, acc, pid) in sim.ports(i, ns).items()))
            elif m := re.fullmatch(r"docker exec afm-agent curl -s -m 5 localhost:(\d+)/api/runtime/requests", cmd):
                slot = int(m.group(1)) - 13000
                ok = sim.agent_up[i] and sim.slot_host(slot) == i and "no_agent_requests" not in st.f
                res = self._res(cmd, json.dumps([{"id": f"req-{slot}", "created-at": now(), "status": "applied"}]) if ok else "",
                                0 if ok else 7)
            elif m := re.fullmatch(r"config vlan (add|del) -n (asic\d+) (\d+)", cmd):
                (sim.user_vlans.setdefault(i, set()).add if m.group(1) == "add" else sim.user_vlans.setdefault(i, set()).discard)(int(m.group(3)))
                res = self._res(cmd)
            elif m := re.fullmatch(r"docker exec afm-agent curl -s -m 5 localhost:(\d+)/api/health", cmd):
                res = self._res(cmd, '{"is-connected": true, "healthy": true}' if sim.agent_up[i] else "", 0 if sim.agent_up[i] else 7)
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
        i, a = divmod(pid, 2)
        if i >= sim.n_sw:
            return None
        ns = f"asic{a}"
        port = next((p for p, (s, gg, _, q) in sim.ports(i, ns).items() if s == self.slot and gg == g and q == pid), None)
        return (i, ns, port) if port else None

    def _port_up(self, g, pid) -> bool:
        k = self._link(g, pid)
        return bool(k) and k not in self.st.sim.links_down and (self.slot, g, pid) not in self.st.sim.cmp_admin_down

    def _ports(self, g) -> list[dict]:
        sim = self.st.sim
        out = []
        for pid in range(2 * sim.n_sw):
            k = self._link(g, pid)
            up = self._port_up(g, pid)
            ll = up and not ((self.slot, g, pid) in sim.ifcp_stuck) and \
                not ("ifcp_baseline_down" in self.st.f and (self.slot, g, pid) == (0, 0, 0))
            out.append({"id": str(pid), "spec": {"station_id": str(pid // 2),
                                                  "admin_state": "idle" if (self.slot, g, pid) in sim.cmp_admin_down else "enabled"},
                        "status": {"name": f"netport{pid}", "link_status": "LINK_UP" if up else "LINK_DOWN",
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
                    for pid in range(2 * sim.n_sw):
                        stale = (self.slot, g, pid) in sim.flapped and "stale_fwd_table" in st.f
                        peers = [] if (not self._port_up(g, pid) or stale) else self._peers(g)
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
                    for k in range(sim.n_sw):
                        ports = [p for p in (2 * k, 2 * k + 1)]
                        c = sim.computes.get(sim.cname(self.slot)) or {}
                        drained = {(x.get("gpu-id"), x.get("port-id")) for x in (c.get("spec") or {}).get("drained-ports") or []}
                        live = ports if "station_map_stale" in st.f else \
                            [p for p in ports if self._port_up(g, p) and (g, p) not in drained]
                        pm = lambda ps: [{"accelerator_id": str(a), "path_to_port_map": [  # noqa: E731
                            {"path": str(n), "port": str(p)} for n, p in enumerate(ps)]} for a in self._peers(g)]
                        stations.append({"id": str(k), "spec": {"device_bdf": self.bdf(g), "admin_state": "active",
                                                                "accel_path_map": pm(ports)},
                                         "status": {"oper_state": "up" if live else "down", "accel_path_map": pm(live)}})
                    devs.append({"bdf": self.bdf(g), "station": stations})
                out = json.dumps({"device": devs})
            elif m := re.fullmatch(r"env AFMCTL_FORCE_UPDATE=1 afmctl update port -b (\S+) -s (\d+) -p (\S+) --admin-state (idle|enabled)", c):
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
                out = "".join(f"Device {self.bdf(g)} reset successfully\n" for g in gs)
            elif m := re.fullmatch(r"afmctl show port statistics mac(?: -b (\S+))? --json", c):
                sim.stat_reads = getattr(sim, "stat_reads", 0) + (0 if "stats_frozen" in st.f else 1)
                n = sim.stat_reads * 1000
                out = json.dumps({"devices": [{"bdf": self.bdf(g), "port": [
                    {"station_id": str(pid // 2), "port_id": str(pid),
                     "statistics": {"rx_frames": str(n if self._port_up(g, pid) else 0),
                                    "tx_frames": str(n if self._port_up(g, pid) else 0)}}
                    for pid in range(2 * sim.n_sw)]} for g in self._gpus(m.group(1))]})
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
