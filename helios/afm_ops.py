"""AFM lifecycle operations shared by the disruptive AFM tests: vPOD membership moves, compute/switch
lifecycle, and the switch-side actions (afm-agent / afm-controller / sb-node-agent / link shut) over SSH.

Built from the g02-g17 full-rack procedures (results/extract-procedures.md), including the fixes for the
script bugs the manual runs hit:
  * vPOD membership is per GPU (``scale-up-info.gpu-info.*.vpod-membership``), never ``status.vpod-membership``
  * GPU re-add right after removal/reconfigure answers 400 "configuration failed state" for ~2 min -> retry
  * DELETE vPOD with GPUs still assigned -> 412; Reconfigure while one is in flight -> 412 -> retry
  * a slot's sb-node-agent can move between switches after an afm-agent restart -> re-locate on every poll
  * ``pgrep -f`` self-matches; read ``ps -eo pid,args`` and parse instead
  * PortFailed / membership checks look at every compute, not ``items[0]``
  * the runner clock may be skewed: event filters use controller timestamps, never the runner's
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .afm import CLUSTER, AfmClient, AfmError, conditions_ok, wait_for

from .progress import action  # noqa: E402

log = logging.getLogger("helios.cmd")
VP = f"{CLUSTER}/vpods"
CN = f"{CLUSTER}/computenodes"
SN = f"{CLUSTER}/switchnodes"
TERMINAL = ("online", "degraded")


class AfmOps:
    def __init__(self, afm: AfmClient, rack=None, inventory=None):
        self.afm, self.rack, self.inv = afm, rack, inventory

    def th(self, key: str, default):
        v = self.inv.th(f"afm.{key}", default) if self.inv is not None else default
        return default if v is None else v

    def wait(self, fn, timeout: float, interval: float = 10, what: str = ""):
        """wait_for with rack seconds scaled by thresholds.afm.time_scale (1 on a rack; small in the self-test)."""
        k = float(self.th("time_scale", 1))
        return wait_for(fn, timeout * k, max(0.05, interval * k), what)

    # ================================================================ read helpers
    def compute(self, name: str) -> dict:
        return self.afm.get(f"{CN}/{name}")

    def switch(self, name: str) -> dict:
        return self.afm.get(f"{SN}/{name}")

    def vpod(self, name: str) -> dict:
        return self.afm.get(f"{VP}/{name}")

    def system_vpod_name(self) -> str:
        v = self.afm.system_vpod()
        if not v:
            raise AfmError("GET", VP, 0, "no system vPOD")
        return v["meta"]["name"]

    @staticmethod
    def gpu_info(node: dict) -> dict:
        return ((node.get("status") or {}).get("scale-up-info") or {}).get("gpu-info") or {}

    def gpu_entries(self, compute: str) -> list[dict]:
        """vPOD spec.GPUs entries for a compute's GPUs: {id, slot-id, index}."""
        n = self.compute(compute)
        slot = (n.get("spec") or {}).get("slot-id")
        return [{"id": uuid, "slot-id": slot, "index": g.get("index")}
                for uuid, g in sorted(self.gpu_info(n).items(), key=lambda kv: kv[1].get("index", 0))]

    def membership(self, compute: str) -> set[str]:
        return {g.get("vpod-membership") or "" for g in self.gpu_info(self.compute(compute)).values()}

    def planes(self, vpod: str) -> tuple:
        st = self.vpod(vpod).get("status") or {}
        return tuple(st.get(k) for k in ("provisioned-switch-planes", "available-switch-planes",
                                         "effective-switch-planes"))

    def settled_planes(self, vpod: str, reads: int = 3, interval: float = 10, max_reads: int = 12):
        """Available planes once the same value is read ``reads`` times in a row (full-rack baseline rule)."""
        last, same = None, 0
        for _ in range(max_reads):
            p = self.planes(vpod)
            same = same + 1 if p == last else 1
            last = p
            if same >= reads:
                break
            time.sleep(interval)
        return last

    def vpod_state(self, name: str) -> str | None:
        r = self.afm.raw("GET", f"{VP}/{name}")
        return ((r.json().get("status") or {}).get("vpod-state")) if r.status_code == 200 else None

    def wait_vpod(self, name: str, states=TERMINAL, timeout: float | None = None, fail_states=("operation-failed",)):
        """Wait for vpod-state in ``states``; returns the last state seen."""
        timeout = timeout or float(self.th("vpod_timeout_s", 300))
        seen = {"s": None}

        def ok():
            seen["s"] = self.vpod_state(name)
            return seen["s"] in states or seen["s"] in fail_states
        self.wait(ok, timeout, float(self.th("poll_s", 10)), f"vPOD {name} in {states}")
        return seen["s"]

    def wait_pending_clear(self, name: str, timeout: float = 300) -> bool:
        return bool(self.wait(lambda: not (self.vpod(name).get("status") or {}).get("pending-ops"), timeout,
                             float(self.th("poll_s", 10)), f"{name} pending-ops empty"))

    # ================================================================ vPOD membership
    def _put_vpod_gpus(self, name: str, gpus: list[dict]) -> Any:
        v = self.vpod(name)
        spec = dict(v.get("spec") or {})
        spec["GPUs"] = [{k: g[k] for k in ("slot-id", "index") if k in g} | ({"id": g["id"]} if g.get("id") else {})
                        for g in gpus]
        return self._retry_400(lambda: self.afm.raw("PUT", f"{VP}/{name}", {"kind": "VPod", "api-version": "v1",
                                                                            "meta": v["meta"], "spec": spec}))

    def _retry_400(self, call, timeout: float | None = None):
        """Retry while AFM answers 400 '... configuration failed state' (GPU just left a vPOD / reconfigured)."""
        end = time.monotonic() + (timeout or float(self.th("readd_retry_s", 180)))
        while True:
            r = call()
            if r.status_code != 400 or "configuration failed" not in r.text or time.monotonic() >= end:
                if r.status_code >= 400:
                    raise AfmError("PUT/POST", "vpods", r.status_code, r.text)
                return r.json() if r.content else None
            log.debug("AFM: 400 configuration-failed state, retrying")
            time.sleep(float(self.th("retry_interval_s", 10)))

    def vpod_gpus(self, name: str) -> list[dict]:
        """Current members as spec entries {id, slot-id, index} (from status.gpus-status, which has the ids)."""
        out = []
        for gs in (self.vpod(name).get("status") or {}).get("gpus-status") or []:
            g = gs.get("gpu") or {}
            idx = g.get("gpu-index") or {}
            out.append({"id": g.get("id"), "slot-id": idx.get("slot-id"), "index": idx.get("index")})
        if not out:  # fall back to spec (no ids)
            out = list((self.vpod(name).get("spec") or {}).get("GPUs") or [])
        return out

    def remove_compute(self, vpod: str, compute: str) -> list[dict]:
        """Take a compute's GPUs out of ``vpod``; waits until every GPU reports no membership. Returns its entries."""
        mine = self.gpu_entries(compute)
        slot = mine[0]["slot-id"] if mine else None
        keep = [g for g in self.vpod_gpus(vpod) if g.get("slot-id") != slot]
        self._put_vpod_gpus(vpod, keep)
        ok = self.wait(lambda: self.membership(compute) <= {""}, float(self.th("membership_timeout_s", 120)),
                      float(self.th("poll_s", 10)),
                      f"{compute} GPUs out of {vpod}")
        if not ok:
            raise AfmError("PUT", vpod, 0, f"{compute} GPUs still members: {self.membership(compute)}")
        return mine

    def add_gpus(self, vpod: str, entries: list[dict]) -> None:
        have = self.vpod_gpus(vpod)
        keys = {(g.get("slot-id"), g.get("index")) for g in have}
        self._put_vpod_gpus(vpod, have + [e for e in entries if (e["slot-id"], e["index"]) not in keys])

    def create_vpod(self, name: str, entries: list[dict], policy: str = "system-failover-policy") -> Any:
        pod = (self.vpod(self.system_vpod_name()).get("spec") or {}).get("parent-pod", "pod-1")
        body = {"kind": "VPod", "api-version": "v1", "meta": {"name": name},
                "spec": {"GPUs": entries, "parent-pod": pod, "failover-policy": policy}}
        return self._retry_400(lambda: self.afm.raw("POST", VP, body))

    def delete_vpod(self, name: str, timeout: float = 120) -> bool:
        """Empty the vPOD, DELETE it (retrying 412 while GPUs drain out), wait for 404."""
        if self.afm.raw("GET", f"{VP}/{name}").status_code == 404:
            return True
        if self.vpod_gpus(name):
            self._put_vpod_gpus(name, [])
        end = time.monotonic() + timeout
        while True:
            r = self.afm.raw("DELETE", f"{VP}/{name}")
            if r.status_code in (200, 204, 404) or time.monotonic() >= end:
                break
            time.sleep(float(self.th("retry_interval_s", 10)))
        return bool(self.wait(lambda: self.afm.raw("GET", f"{VP}/{name}").status_code == 404, 60, 5, f"{name} gone"))

    # ================================================================ compute lifecycle
    def compute_phase(self, name: str) -> str | None:
        r = self.afm.raw("GET", f"{CN}/{name}")
        return (r.json().get("status") or {}).get("admission-phase") if r.status_code == 200 else "absent"

    def compute_ready(self, name: str) -> bool:
        r = self.afm.raw("GET", f"{CN}/{name}")
        if r.status_code != 200:
            return False
        n = r.json()
        gi = self.gpu_info(n)
        return ((n.get("status") or {}).get("admission-phase") == "admitted" and conditions_ok(n)
                and bool(gi) and all(g.get("oper-state") == "Ready" for g in gi.values()))

    def wait_compute_ready(self, name: str, timeout: float | None = None) -> bool:
        return bool(self.wait(lambda: self.compute_ready(name), timeout or float(self.th("readmit_timeout_s", 300)),
                             10, f"{name} admitted+healthy+GPUs Ready"))

    def reconfigure(self, name: str, retry_412_s: float = 180):
        end = time.monotonic() + retry_412_s
        while True:
            r = self.afm.raw("POST", f"{CN}/{name}/Reconfigure", {})
            final = any(k in r.text.lower() for k in ("decommission", "scaleup info", "failed to find"))
            if r.status_code != 412 or final or time.monotonic() >= end:  # 412 = another Reconfigure in flight
                return r
            time.sleep(float(self.th("retry_interval_s", 10)))

    def pick_compute(self, prefer_last: bool = True) -> str:
        """A ready compute to act on: thresholds.afm.target_compute, else the highest (or lowest) slot."""
        want = self.th("target_compute", None)
        if want:
            return want
        nodes = sorted((c for c in self.afm.computes() if self.compute_ready(c["meta"]["name"])),
                       key=lambda c: (c.get("spec") or {}).get("slot-id", 0))
        if not nodes:
            raise AfmError("GET", CN, 0, "no ready compute")
        return nodes[-1 if prefer_last else 0]["meta"]["name"]

    # ================================================================ switch lifecycle
    def switch_phase(self, name: str) -> str | None:
        r = self.afm.raw("GET", f"{SN}/{name}")
        return (r.json().get("status") or {}).get("admission-phase") if r.status_code == 200 else "absent"

    def switch_ready(self, name: str) -> bool:
        r = self.afm.raw("GET", f"{SN}/{name}")
        return r.status_code == 200 and (r.json().get("status") or {}).get("admission-phase") == "admitted" \
            and conditions_ok(r.json())

    def wait_switch_phase(self, name: str, phase: str, timeout: float = 300) -> bool:
        return bool(self.wait(lambda: self.switch_phase(name) == phase, timeout, 10, f"{name} {phase}"))

    def leader_ip(self) -> str | None:
        return (self.afm.cluster().get("status") or {}).get("leader")

    def quorum_ips(self) -> list[str]:
        return [m.get("name") for m in ((self.afm.cluster().get("status") or {}).get("quorum-status") or {})
                .get("members") or []]

    def pick_switch(self, *, controller: bool | None = False) -> dict:
        """Target switch: admitted + healthy; controller=False -> not a controller node, True -> a non-leader
        controller, None -> any. Highest slot-id first (the full-rack team's rule)."""
        leader, quorum = self.leader_ip(), set(self.quorum_ips())
        sw = [s for s in self.afm.switches() if self.switch_ready(s["meta"]["name"])]
        if controller is False:
            sw = [s for s in sw if (s.get("spec") or {}).get("ip-address") not in quorum] or \
                 [s for s in sw if (s.get("spec") or {}).get("ip-address") != leader]
        elif controller is True:
            sw = [s for s in sw if (s.get("spec") or {}).get("ip-address") in quorum - {leader}]
        if not sw:
            raise AfmError("GET", SN, 0, f"no eligible switch (controller={controller})")
        return sorted(sw, key=lambda s: (s.get("spec") or {}).get("slot-id", 0))[-1]

    # ================================================================ switch-side (SSH)
    def ssh_switch(self, afm_switch: dict | str):
        """Inventory switch for an AFM SwitchNode: by host.extra.afm_name, then by management IP, then the
        only switch when both sides have exactly one."""
        if self.rack is None:
            raise RuntimeError("rack (SSH) not available")
        obj = afm_switch if isinstance(afm_switch, dict) else self.switch(afm_switch)
        name, ip = obj["meta"]["name"], (obj.get("spec") or {}).get("ip-address")
        sws = list(self.rack.switches.values())
        for s in sws:
            if s.host.extra.get("afm_name") == name or (ip and s.host.host == ip):
                return s
        if len(sws) == 1 and len(self.afm.switches()) == 1:
            return sws[0]
        raise RuntimeError(f"no inventory switch for AFM {name} ({ip}); set afm_name: {name} on its entry")

    def ssh_by_ip(self, ip: str):
        for s in self.rack.switches.values():
            if s.host.host == ip:
                return s
        return self.ssh_switch(next(x for x in self.afm.switches() if (x.get("spec") or {}).get("ip-address") == ip))

    @staticmethod
    def sb_agents(sw) -> dict[int, int]:
        """{slot: pid} of sb-node-agent processes in this switch's afm-agent container (no pgrep self-match)."""
        r = sw.run("docker exec afm-agent ps -eo pid,args", sudo=True, timeout=60)
        return {int(m.group(2)): int(m.group(1))
                for m in re.finditer(r"^\s*(\d+)\s+\S*sb-node-agent\s+-slot-id\s+(\d+)(?:\s|$)", r.stdout, re.M)}

    def slot_host(self, slot: int):
        """(switch, pid) currently running the sb-node-agent for ``slot``; re-located on every call."""
        for sw in self.rack.switches.values():
            pid = self.sb_agents(sw).get(slot)
            if pid:
                return sw, pid
        return None, None

    @staticmethod
    def afm_agent(sw, what: str):
        action("%s: systemctl %s afm-agent", sw.name, what)
        return sw.run(f"systemctl {what} afm-agent", sudo=True, timeout=300)

    @staticmethod
    def restart_controller(sw):
        action("%s: docker restart afm-controller", sw.name)
        return sw.run("docker restart afm-controller", sudo=True, timeout=300)

    @staticmethod
    def signal_sb_agent(sw, pid: int, sig: str):
        action("%s: kill -%s sb-node-agent pid %s", sw.name, sig, pid)
        return sw.run(f"docker exec afm-agent kill -{sig} {pid}", sudo=True, timeout=60)

    @staticmethod
    def controller_action(sw, action: str):
        """docker stop|start|restart afm-controller."""
        return sw.run(f"docker {action} afm-controller", sudo=True, timeout=300)

    @staticmethod
    def controller_state(sw) -> tuple[str, int]:
        """(StartedAt, RestartCount) of the afm-controller container - a crash/restart changes either."""
        out = sw.run("docker inspect -f '{{.State.StartedAt}} {{.RestartCount}}' afm-controller",
                     sudo=True, timeout=60).stdout.split()
        return (out[0], int(out[1])) if len(out) >= 2 and out[1].isdigit() else ("", -1)

    def controllers(self) -> dict:
        """{quorum member IP: inventory switch} for every controller node."""
        return {ip: self.ssh_by_ip(ip) for ip in self.quorum_ips()}

    @staticmethod
    def switch_agent_pid(sw) -> int | None:
        r = sw.run("docker exec afm-agent ps -eo pid,args", sudo=True, timeout=60)
        m = re.search(r"^\s*(\d+)\s+\S*switch-agent(?:\s|$)", r.stdout, re.M)
        return int(m.group(1)) if m else None

    @staticmethod
    def epoch(sw) -> int:
        return int(sw.run("date +%s", timeout=30).stdout.strip() or 0)

    @staticmethod
    def controller_logs_since(sw, epoch: int) -> list[str]:
        """Controller log files written after ``epoch`` (switch clock)."""
        r = sw.run(f"find /opt/amd/afm/controller/logs -type f -newermt @{epoch}", sudo=True, timeout=60)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    @staticmethod
    def reset_switch_identity(sw):
        """Re-admission after delete: drop the agent's TLS identity and restart it."""
        sw.run("rm -f /opt/amd/afm/agent/sysconfig/config0/*.pem /opt/amd/afm/agent/sysconfig/config1/*.pem",
               sudo=True, timeout=60)
        return sw.run("systemctl restart afm-agent", sudo=True, timeout=300)

    @staticmethod
    def static_macs(sw) -> int:
        n = 0
        for ns in sw.namespaces():
            out = sw.run(f"show mac{' -n ' + ns if ns else ''}", sudo=True, timeout=120).stdout
            n += sum(1 for ln in out.splitlines() if re.search(r"\bstatic\b", ln, re.I))
        return n

    @staticmethod
    def vlan_count(sw) -> int:
        return sum(len(sw.vlans(ns)) for ns in sw.namespaces())

    @staticmethod
    def up_trunk_port(sw, ns: str | None, avoid=()) -> str | None:
        rows = [r for r in sw.interfaces_status(ns) if r.get("Vlan") == "trunk" and r.get("Oper") == "up"
                and r.get("Interface") not in set(avoid)]
        return rows[0]["Interface"] if rows else None

    @staticmethod
    def port_admin(sw, ns: str | None, port: str, up: bool):
        flag = f" -n {ns}" if ns else ""
        return sw.run(f"config interface{flag} {'startup' if up else 'shutdown'} {port}", sudo=True, timeout=120)

    @staticmethod
    def port_oper(sw, ns: str | None, port: str) -> str | None:
        return next((r.get("Oper") for r in sw.interfaces_status(ns) if r.get("Interface") == port), None)

    # ================================================================ links (switch fabric ports)
    def link_candidates(self, sw, slots: set[int] | None = None, ns_list=None) -> list[tuple]:
        """Up trunk ports cabled to a GPU: [(ns, port, (slot, gpu, acc, gpu_port))], skipping
        thresholds.afm.avoid_ports ("<switch>:<port>", e.g. the full rack's coupled .48 Eth243/251)."""
        from .afmctl import parse_port_description

        avoid = set(self.th("avoid_ports", []) or [])
        out = []
        for ns in ns_list or sw.namespaces():
            desc = sw.port_descriptions(ns)
            for r in sw.interfaces_status(ns):
                port = r.get("Interface")
                cab = parse_port_description(desc.get(port, ""))
                if not cab or r.get("Vlan") != "trunk" or r.get("Oper") != "up" or f"{sw.name}:{port}" in avoid:
                    continue
                if slots is None or cab[0] in slots:
                    out.append((ns, port, cab))
        return out

    def gpu_links(self, slot: int, gpu: int | None = None) -> list[tuple]:
        """Every up fabric port cabled to compute ``slot`` (optionally one GPU), across all switches:
        [(sw, ns, port, (slot, gpu, acc, gpu_port))], ordered by GPU then GPU port id."""
        out = []
        for sw in self.rack.switches.values():
            for ns, port, cab in self.link_candidates(sw, {slot}):
                if gpu is None or cab[1] == gpu:
                    out.append((sw, ns, port, cab))
        return sorted(out, key=lambda x: (x[3][1], x[3][3]))

    def station(self, cab) -> int:
        """IFoE station of a GPU port: gpu_port // thresholds.afm.ports_per_station (2; ``afmctl show station`` maps
        station k to ports 2k, 2k+1 - verified on MR3 c1, 2026-10-13)."""
        return cab[3] // int(self.th("ports_per_station", 2))

    def stations(self, slot: int, gpu: int) -> dict[int, list[tuple]]:
        """{station: [(sw, ns, port, cab), ...]} for one GPU (up ports only)."""
        out: dict[int, list[tuple]] = {}
        for link in self.gpu_links(slot, gpu):
            out.setdefault(self.station(link[3]), []).append(link)
        return out

    def wait_oper(self, sw, ns, port, state: str, timeout: float = 60) -> bool:
        return bool(self.wait(lambda: self.port_oper(sw, ns, port) == state, timeout, 2, f"{port} {state}"))

    def oper_down(self, sw) -> set[tuple]:
        return {(ns, r.get("Interface")) for ns in sw.namespaces() for r in sw.interfaces_status(ns)
                if r.get("Vlan") == "trunk" and r.get("Oper") != "up"}

    def failed_gpu_ports(self) -> set[tuple]:
        """(compute, gpu index, port id) of every PortFailed GPU port on EVERY compute (not just items[0])."""
        out = set()
        for c in self.afm.computes():
            for g in self.gpu_info(c).values():
                for i, p in enumerate(g.get("ports") or []):
                    if p.get("oper-state") == "PortFailed":
                        out.add((c["meta"]["name"], g.get("index"), p.get("id", i)))
        return out

    def link_fail_threshold(self, vpod: str) -> float:
        pol = (self.vpod(vpod).get("spec") or {}).get("failover-policy") or "system-failover-policy"
        return float((self.afm.get(f"{CLUSTER}/failoverpolicies/{pol}").get("spec") or {}).get("link-fail-threshold", 10))

    # ================================================================ AFM node <-> inventory tray names
    def tray_map(self) -> dict[str, str]:
        """{AFM ComputeNode name: inventory tray name}, learned once: inventory afm_name / slot, else the tray whose
        afmctl accelerator ids match the node's GPU acc-ids; a single AFM node and a single tray left over (e.g. the
        tray is unreachable, so its acc-ids cannot be read) are paired too. Used to name both in messages."""
        if "_tray_map" in self.__dict__:
            return self._tray_map
        from .afmctl import Afmctl
        from .rack import fan_out
        nodes = {c["meta"]["name"]: c for c in self.afm.computes()}
        trays = list(self.rack.computes.values()) if self.rack is not None else []
        out: dict[str, str] = {}
        for name, n in nodes.items():
            slot = (n.get("spec") or {}).get("slot-id")
            for c in trays:
                if c.host.extra.get("afm_name") == name or (slot is not None and c.host.extra.get("slot") == slot):
                    out[name] = c.name
        have = {c.name: {d.accel_id for d in r} for c, r, exc in
                fan_out(lambda c: Afmctl(c, timeout=60).devices(), [c for c in trays if c.name not in out.values()])
                if not exc}
        for name, n in nodes.items():
            if name in out:
                continue
            want = {str(g.get("acc-id")) for g in self.gpu_info(n).values()}
            hit = [t for t, ids in have.items() if want and want <= ids and t not in out.values()]
            if len(hit) == 1:
                out[name] = hit[0]
        left_n = [n for n in nodes if n not in out]
        left_t = [c.name for c in trays if c.name not in out.values()]
        if len(left_n) == 1 and len(left_t) == 1:
            out[left_n[0]] = left_t[0]
        self._tray_map = out
        return out

    def label(self, afm_name: str) -> str:
        """'pod-1-4-ComputeTray (ct05)' when the tray is known, else the AFM name."""
        t = self.tray_map().get(afm_name) if self.rack is not None else None
        return f"{afm_name} ({t})" if t else afm_name

    def slot_label(self, slot) -> str:
        """'slot 4 (ct05)'"""
        for n in self.afm.computes():
            if (n.get("spec") or {}).get("slot-id") == slot:
                t = self.tray_map().get(n["meta"]["name"]) if self.rack is not None else None
                return f"slot {slot} ({t})" if t else f"slot {slot}"
        return f"slot {slot}"

    # ================================================================ compute side (afmctl over SSH)
    def tray(self, afm_name: str):
        """Inventory compute tray for an AFM ComputeNode: host.extra.afm_name, else the tray whose afmctl
        accelerator ids match the AFM GPU acc-ids."""
        from .afmctl import Afmctl

        cache = self.__dict__.setdefault("_trays", {})
        if afm_name in cache:
            return cache[afm_name]
        for c in self.rack.computes.values():
            if c.host.extra.get("afm_name") == afm_name:
                cache[afm_name] = c
                return c
        want = {str(g.get("acc-id")) for g in self.gpu_info(self.compute(afm_name)).values()}
        for c in self.rack.computes.values():
            try:
                have = {d.accel_id for d in Afmctl(c).devices()}
            except Exception:  # noqa: BLE001 - tray down / no afmctl
                continue
            if want and want <= have:
                cache[afm_name] = c
                return c
        t = self.tray_map().get(afm_name)
        if t:                     # paired by elimination: the tray exists but its afmctl cannot be read
            raise RuntimeError(f"{afm_name} ({t}): tray unreachable or afmctl not answering")
        raise RuntimeError(f"no inventory compute matches AFM {afm_name} (acc-ids {sorted(want)}); "
                           f"set afm_name: {afm_name} on its inventory entry")

    @staticmethod
    def afmctl(tray):
        from .afmctl import Afmctl

        return Afmctl(tray)

    def ifcp(self, tray) -> dict[tuple, dict]:
        """{(bdf, port): {link, admin, ll_up, peer_enabled, error}} for every GPU port of the tray."""
        a = self.afmctl(tray)
        out = {}
        for d in a.devices():
            for p in a.ports_raw(d.bdf):
                st, sp = p.get("status") or {}, p.get("spec") or {}
                i = st.get("ifcp") or {}
                out[(d.bdf, int(p.get("id")))] = {
                    "link": st.get("link_status"), "admin": sp.get("admin_state"), "ll_up": i.get("ll_up") == "yes",
                    "peer_enabled": i.get("peer_enabled") == "yes",
                    "error": i.get("in_error") == "yes" or i.get("peer_in_error") == "yes"}
        return out

    def ifcp_up(self, tray) -> set[tuple]:
        """Ports with a healthy IFCP link-layer session (LINK_UP, ll_up, peer enabled, no error)."""
        return {k for k, v in self.ifcp(tray).items()
                if v["link"] == "LINK_UP" and v["ll_up"] and v["peer_enabled"] and not v["error"]}

    def compute_port_admin(self, tray, bdf: str, port: int, up: bool):
        """afmctl update port admin-state on the compute side (station = port // ports_per_station). afmctl SHOWS the
        state as "enabled" but SETS it as "enable" (MR3, 16 Oct: "Invalid admin state : enabled (valid: power-off,
        idle, enable)"); if a version refuses the value, the one it lists as valid is used instead."""
        st = port // int(self.th("ports_per_station", 2))
        action("%s %s port %s -> %s", tray.name, bdf, port, "enable" if up else "idle")
        cmd = f"env AFMCTL_FORCE_UPDATE=1 afmctl update port -b {bdf} -s {st} -p {port} --admin-state "
        want = "enable" if up else "idle"
        r = tray.run(cmd + want, sudo=True, timeout=120)
        if not r.ok:
            m = re.search(r"valid:\s*([^)]*)\)", r.stdout + r.stderr)
            valid = [v.strip() for v in m.group(1).split(",")] if m else []
            alt = next((v for v in valid if v != want and v.startswith(want[:5])), None)
            if alt:
                r = tray.run(cmd + alt, sudo=True, timeout=120)
        return r

    def station_map(self, tray, bdf: str, which: str = "status") -> dict[int, set[int]]:
        """{station: ports in its path-to-port map} from afmctl show station (spec or status)."""
        doc = json.loads(tray.run(f"afmctl show station -b {bdf} --json 2>/dev/null", sudo=True, timeout=120).check().stdout)
        out: dict[int, set[int]] = {}
        for d in doc.get("device") or doc.get("devices") or []:
            for s in d.get("station") or []:
                ports = {int(m["port"]) for a in ((s.get(which) or {}).get("accel_path_map") or [])
                         for m in a.get("path_to_port_map") or []}
                out[int(s["id"])] = ports
        return out

    @staticmethod
    def tray_time(tray) -> str:
        return tray.run("date -u '+%Y-%m-%d %H:%M:%S'", timeout=30).stdout.strip()

    @staticmethod
    def reset_logged_since(tray, since: str) -> bool:
        """journalctl confirms an afmctl reset completed after ``since`` (tray UTC time, 'YYYY-mm-dd HH:MM:SS')."""
        r = tray.run(f"journalctl -t afmctl --since '{since} UTC' --no-pager", sudo=True, timeout=60)
        return bool(re.search(r"phase=end.*sub_cmd=reset.*status=0", r.stdout))

    def phases(self, tray) -> dict[str, str]:
        return {d.bdf: d.config_phase for d in self.afmctl(tray).devices()}

    # ================================================================ events
    def events(self, etype: str | None = None, since: str | None = None, page: int = 300) -> list[dict]:
        q = f"/events/v1/events?sort-by=meta.creation-time&sort-order=desc&page-size={page}"
        evs = self.afm.items(q)
        out = []
        for e in evs:
            t = e.get("type") or (e.get("spec") or {}).get("event-type") or (e.get("spec") or {}).get("type")
            if etype and t != etype:
                continue
            if since and ((e.get("meta") or {}).get("creation-time") or "") <= since:  # strictly newer
                continue
            out.append(e)
        return out

    def controller_now(self) -> str:
        """Controller-side 'now' for event filters: newest event's creation-time (runner clock may be skewed)."""
        evs = self.afm.items("/events/v1/events?sort-by=meta.creation-time&sort-order=desc&page-size=1")
        return ((evs[0].get("meta") or {}).get("creation-time") if evs else "") or ""
