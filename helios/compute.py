"""Compute-tray driver (MI-series GPUs on IFoE)."""
from __future__ import annotations

from .inventory import Host
from .transport import CmdResult, Transport


# PATH for amd-smi: g02-g17 trays all have /opt/rocm/bin/amd-smi, but only some have the /usr/bin/amd-smi link
# (14 Oct: missing on CT08, CT09, CT11-CT16), and sudo resets PATH to secure_path
ROCM_PATH = "/opt/rocm/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class ComputeTray:
    def __init__(self, host: Host, transport: Transport):
        self.host = host
        self.name = host.name
        self.t = transport

    @property
    def gpus(self) -> int:
        return int(self.host.extra.get("gpus", 4))

    def gpu_addr(self, idx: int) -> str:
        """Per-GPU accelerator address from inventory (``gpu_addrs: [...]``), '' if unset."""
        addrs = self.host.extra.get("gpu_addrs") or []
        return str(addrs[idx]) if idx < len(addrs) else ""

    def gpu_bdf(self, idx: int) -> str:
        """IFoE PCI function of GPU ``idx`` from inventory ``gpu_bdfs: [...]`` (e.g. 0001:01:00.1)."""
        bdfs = self.host.extra.get("gpu_bdfs") or []
        return str(bdfs[idx]) if idx < len(bdfs) else ""

    def accel_id(self, idx: int) -> str:
        """AFM accelerator ID of GPU ``idx`` from inventory ``accel_ids: [...]``."""
        ids = self.host.extra.get("accel_ids") or []
        return str(ids[idx]) if idx < len(ids) else ""

    def rdma_ports(self) -> list[dict]:
        """Scale-out NIC ports from inventory ``rdma: [{dev, ip4, ip6, gpu}, ...]`` (P1..Pn order)."""
        return list(self.host.extra.get("rdma") or [])

    def run(self, cmd: str, **kw) -> CmdResult:
        if cmd.lstrip().startswith("amd-smi") and not kw.get("env"):
            kw["env"] = {"PATH": ROCM_PATH}       # amd-smi lives in /opt/rocm/bin; not every tray links it into /usr/bin
        return self.t.run(cmd, **kw)

    def sh(self, cmd: str, **kw) -> str:
        return self.run(cmd, **kw).check().stdout

    def close(self) -> None:
        self.t.close()


def render(template: str, **kw) -> str:
    """Fill an inventory command template; a missing placeholder is a config error, not a test failure."""
    try:
        return template.format(**kw)
    except KeyError as e:
        raise ValueError(f"command template needs placeholder {e} (available: {sorted(kw)}): {template!r}") from None
