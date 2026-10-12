"""Compute-tray driver (MI-series GPUs on IFoE)."""
from __future__ import annotations

from .inventory import Host
from .transport import CmdResult, Transport


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

    def run(self, cmd: str, **kw) -> CmdResult:
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
