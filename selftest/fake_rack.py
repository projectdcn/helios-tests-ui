"""Rack simulation behind selftest/fake_afm.py: switch agents, controllers, sb-node-agents, switch/compute
admission, vPOD membership and switching planes, plus ``FakeSwitchTransport`` - the switch "SSH" the
disruptive AFM tests drive (systemctl/docker/kill/config interface/show mac/...), bound to the same state.

Transitions are immediate, so the lifecycle tests run in seconds. Behaviour follows the g02-g17 full-rack
observations (results/extract-procedures.md); ``faults`` switch individual product behaviours to their
broken form so the self-test proves each test catches it.
"""
from __future__ import annotations

import itertools
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
        self.ack: dict[str, set] = {}          # vPOD -> failed links acknowledged by ReconfigureDataFabric
        self.n_ct = n_computes
        self.events: list[dict] = []
        self.gpus: dict[int, list[str]] = {}  # slot -> gpu uuids
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

    def set_link(self, i, ns, port, up: bool):
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
                if d is not None:
                    self.event("link_up", f"PortName {k[2]} asic {k[1]} switch {self.sname(i)} is UP")
                    had_failed = k in self.failed_evt
                    if ("flap_counts_as_failure" in self.f and not had_failed) or \
                            ("no_auto_restore" in self.f and had_failed):
                        self.sticky.add(k)
                self.failed_evt.discard(k)
            elif k not in self.links_down:
                self.links_down[k] = {"since": time.monotonic(), "known": self.agent_up[i], "admin": k == key}
                if self.agent_up[i]:
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
                    o["status"]["admission-phase"] in ("admitted", "drained")
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
            manual = (pol.get("spec") or {}).get("datafabric-reconfig") == "manual"
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
            self.agent_up[i] = False
            if "no_unreachable_event" not in self.f:
                self.event("switchnode_unreachable", f"switch {self.sname(i)} unreachable")
            return
        self.agent_up[i] = True
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
                    gi = {u: {"index": g, "acc-id": slot * 4 + g, "oper-state": "Ready", "vpod-membership": "",
                              "ports": [{"id": p, "oper-state": "PortReady"} for p in range(36)],
                              "ifoe-stations": list(range(18))} for g, u in enumerate(self.gpus[slot])}
                    if "no_readmit" not in self.f:
                        self.computes[self.cname(slot)] = self._compute_obj(slot, gi)

    def restart_controller(self, i):
        ip = self.switch_ips[i]
        if ip == self.leader and len(self.quorum) > 1:
            self.leader = next(q for q in self.quorum if q != ip)
        if "vlan_wipe" in self.f:
            self.wiped |= {j for j in range(self.n_sw) if self.phase_sw(j) == "drained"}

    def vlans(self, i) -> list[str]:
        if self.phase_sw(i) not in ("admitted", "drained") or i in self.wiped:
            return []
        users = [] if "no_vlan_for_vpod" in self.f else sorted(
            v["meta"]["name"] for v in self.st.objs["VPod"].values()
            if not v["meta"]["name"].endswith(".system") and v["spec"].get("GPUs"))
        return ["Vlan100", "Vlan200", "Vlan300"] + [f"Vlan{1000 + n}" for n in range(len(users))]

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
                          for s in sorted(sim.sb_pid) if sim.slot_host(s) == i]
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
            elif m := re.fullmatch(r"docker exec afm-agent kill -(STOP|CONT) (\d+)", cmd):
                slot = next((s for s, p in sim.sb_pid.items() if p == int(m.group(2))), None)
                if slot is None:
                    res = self._res(cmd, rc=1)
                else:
                    if m.group(1) == "STOP":
                        sim.stopped.add(slot)
                    elif slot in sim.stopped:
                        sim.stopped.discard(slot)
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
