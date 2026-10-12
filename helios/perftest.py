"""perftest (ib_*_bw) runner for scale-out NIC ports: server/client pairs, run in parallel."""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from .compute import ComputeTray, render
from .parsers import PerftestResult, parse_perftest
from .rack import Checks, fan_out
from .transport import CmdResult

VERBS = {
    "write": "ib_write_bw",
    "write_imm": "ib_write_bw --write_with_imm",
    "send": "ib_send_bw",
    "send_imm": "ib_send_bw --send_with_imm",
    "read": "ib_read_bw",
}
BASE = ("{bin} -d {dev} -x {gid_index} {af_flag} {mem_flag} -q {qps} -D {duration} -p {tcp_port} "
        "--report_gbits -F {dir_flag} {extra}")


@dataclass
class Endpoint:
    node: ComputeTray
    idx: int  # 0-based NIC port index (P1 = 0)
    port: dict

    @property
    def label(self) -> str:
        return f"{self.node.name}/P{self.idx + 1}({self.port.get('dev', '?')})"

    def ip(self, af: str) -> str:
        key = "ip6" if af == "ipv6" else "ip4"
        if not self.port.get(key):
            pytest.skip(f"{self.node.name} rdma port P{self.idx + 1} has no {key} in inventory")
        return str(self.port[key])


@dataclass
class PairResult:
    server: Endpoint
    client: Endpoint
    client_res: CmdResult | None
    server_res: CmdResult | None
    parsed: PerftestResult | None
    error: str = ""

    @property
    def tag(self) -> str:
        return f"{self.server.label} <-> {self.client.label}"


def scaleout_nodes(rack, inventory) -> tuple[ComputeTray, ComputeTray]:
    names = inventory.cmd("perftest.nodes") or list(rack.computes)[:2]
    if len(names) < 2:
        pytest.skip("scale-out perftest needs 2 nodes (commands.perftest.nodes)")
    a, b = (rack.computes[n] for n in names[:2])
    for n in (a, b):
        if not n.rdma_ports():
            pytest.skip(f"{n.name}: no 'rdma:' ports in inventory")
    return a, b


def cross_pairs(a: ComputeTray, b: ComputeTray, only: list[int] | None = None) -> list[tuple[Endpoint, Endpoint]]:
    """Port pair i = (a.Pi, b.Pi). ``only`` = 1-based port numbers to keep."""
    n = min(len(a.rdma_ports()), len(b.rdma_ports()))
    idxs = [i for i in range(n) if not only or i + 1 in only]
    return [(Endpoint(a, i, a.rdma_ports()[i]), Endpoint(b, i, b.rdma_ports()[i])) for i in idxs]


def loopback_pairs(a: ComputeTray) -> list[tuple[Endpoint, Endpoint]]:
    """Same-node pairs P1-P2, P3-P4, ..."""
    ports = a.rdma_ports()
    if len(ports) < 2:
        pytest.skip(f"{a.name}: loopback needs >= 2 rdma ports")
    return [(Endpoint(a, i, ports[i]), Endpoint(a, i + 1, ports[i + 1])) for i in range(0, len(ports) - 1, 2)]


def _cfg(inventory, key, default):
    v = inventory.cmd(f"perftest.{key}")
    return default if v is None else v


def build(ep: Endpoint, inventory, *, verb: str, mem: str, af: str, bidir: bool, qps: int,
          tcp_port: int, server_ip: str | None) -> str:
    verbs = _cfg(inventory, "verbs", VERBS)
    if verb not in verbs:
        raise ValueError(f"unknown perftest verb {verb!r}; configured: {sorted(verbs)}")
    gid = _cfg(inventory, "gid_index", {"ipv4": 3, "ipv6": 1})
    af_flag = _cfg(inventory, "af_flag", {"ipv4": "", "ipv6": "--ipv6"})
    mem_flag = render(_cfg(inventory, "gpu_mem_flag", "--use_rocm={gpu}"), gpu=ep.port.get("gpu", 0)) if mem == "gpu" else ""
    kw = dict(bin=verbs[verb], dev=ep.port.get("dev", ""), gid_index=ep.port.get(f"gid_index_{af}", gid[af]),
              af_flag=af_flag[af], mem_flag=mem_flag, qps=qps, duration=_cfg(inventory, "duration_s", 10),
              tcp_port=tcp_port, dir_flag="-b" if bidir else "", extra=_cfg(inventory, "extra", ""))
    if server_ip is None:
        cmd = render(_cfg(inventory, "server", BASE), **kw)
    else:
        cmd = render(_cfg(inventory, "client", BASE + " {server_ip}"), server_ip=server_ip, **kw)
    return re.sub(r"\s+", " ", cmd).strip()


def run_pairs(pairs, inventory, *, verb="write", mem="gpu", af="ipv6", bidir=True, qps=None) -> list[PairResult]:
    """Run every (server, client) pair in parallel on distinct TCP ports."""
    qps = qps or int(_cfg(inventory, "qps", 1))
    base_port = int(_cfg(inventory, "base_port", 18515))
    duration = float(_cfg(inventory, "duration_s", 10))
    start_delay = float(_cfg(inventory, "server_start_s", 2))
    timeout = duration + float(_cfg(inventory, "timeout_margin_s", 60))

    def one(item):
        i, (srv, cli) = item
        port = base_port + i
        s_cmd = build(srv, inventory, verb=verb, mem=mem, af=af, bidir=bidir, qps=qps, tcp_port=port, server_ip=None)
        c_cmd = build(cli, inventory, verb=verb, mem=mem, af=af, bidir=bidir, qps=qps, tcp_port=port,
                      server_ip=srv.ip(af))
        with ThreadPoolExecutor(1) as ex:
            s_fut = ex.submit(srv.node.run, s_cmd, timeout=timeout)
            time.sleep(start_delay)
            c_res = cli.node.run(c_cmd, timeout=timeout)
            try:
                s_res = s_fut.result(timeout=30 if c_res.ok else 5)
            except Exception as e:  # noqa: BLE001 — server hung after a failed client; reap it
                srv.node.run(f"pkill -f -- '-p {port}'", timeout=15)
                return PairResult(srv, cli, c_res, None, parse_perftest(c_res.stdout), f"server did not exit: {e}")
        return PairResult(srv, cli, c_res, s_res, parse_perftest(c_res.stdout))

    results = []
    for (i, (srv, cli)), res, exc in fan_out(one, list(enumerate(pairs))):
        results.append(res if res else PairResult(srv, cli, None, None, None, f"{exc}"))
    return results


def check_pairs(results: list[PairResult], inventory, checks: Checks, *, verb: str, what: str = "") -> dict:
    floor = inventory.th(f"datapath.perftest.{verb}_min_gbps", inventory.th("datapath.perftest.min_gbps"))
    bws = {}
    for r in results:
        tag = f"{r.tag}{' ' + what if what else ''}"
        if r.error:
            checks.fail(f"{tag}: {r.error}")
        if r.client_res is None:
            continue
        if not checks.check(r.client_res.ok, f"{tag}: client rc={r.client_res.rc} {r.client_res.tail(300)!r}"):
            continue
        if r.server_res is not None:
            checks.check(r.server_res.ok, f"{tag}: server rc={r.server_res.rc} {r.server_res.tail(300)!r}")
        p = r.parsed
        checks.check(not p.errors, f"{tag}: errors {p.errors[:3]}")
        if checks.check(p.bw_avg is not None, f"{tag}: no BW average row in output"):
            bws[tag] = p.bw_avg
            if floor is not None:
                checks.check(p.bw_avg >= floor, f"{tag}: {p.bw_avg:.1f} Gb/s < {floor} Gb/s")
    return bws
