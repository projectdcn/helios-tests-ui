"""Hardware inventory + usage of a switch or compute tray (HC-011, full-rack health check).

One SSH round trip per node (``HW_SCRIPT``, sections marked ``@@name``) plus, on computes, amd-smi static / metric
for the GPUs and afmctl for the GPU ports; on switches ``show platform summary`` and the compute-facing ports.
"""
from __future__ import annotations

import json
import re

HW_SCRIPT = ('echo "@@lscpu"; lscpu; echo "@@dmi"; for f in sys_vendor product_name product_serial board_name; do '
             'echo "$f=$(cat /sys/class/dmi/id/$f 2>/dev/null)"; done; echo "@@stat1"; head -1 /proc/stat; sleep 1; '
             'echo "@@stat2"; head -1 /proc/stat; echo "@@loadavg"; cat /proc/loadavg; echo "@@free"; free -b; '
             'echo "@@lsblk"; lsblk -b -d -n -o NAME,SIZE,TYPE,MODEL; echo "@@df"; '
             'df -P -B1 -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs')
GPU_STATIC = "amd-smi static --asic --vbios --vram --json"
GPU_METRIC = "amd-smi metric --usage --power --temperature --mem-usage --json"


def sections(text: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        m = re.fullmatch(r"@@(\w+)", line.strip())
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def parse_lscpu(text: str) -> dict:
    kv = {k.strip(): v.strip() for k, _, v in (ln.partition(":") for ln in text.splitlines()) if v}
    n = lambda k: int(kv[k]) if kv.get(k, "").isdigit() else None  # noqa: E731
    return {"model": kv.get("Model name", ""), "cpus": n("CPU(s)"), "sockets": n("Socket(s)"),
            "cores_per_socket": n("Core(s) per socket")}


def parse_kv(text: str) -> dict:
    return {k.strip(): v.strip() for k, _, v in (ln.partition("=") for ln in text.splitlines()) if k.strip()}


def cpu_busy_pct(stat1: str, stat2: str) -> float | None:
    """CPU busy % between two '/proc/stat' 'cpu' lines (idle + iowait count as idle)."""
    try:
        a = [int(x) for x in stat1.split()[1:]]
        b = [int(x) for x in stat2.split()[1:]]
    except ValueError:
        return None
    d = [y - x for x, y in zip(a, b)]
    if len(d) < 4:
        return None
    total = sum(d)
    idle = d[3] + (d[4] if len(d) > 4 else 0)
    return round(100.0 * (total - idle) / total, 1) if total > 0 else None


def parse_free_b(text: str) -> dict:
    for ln in text.splitlines():
        f = ln.split()
        if f and f[0] == "Mem:" and len(f) >= 7 and all(x.isdigit() for x in f[1:7]):
            total, used, free, _sh, _bc, avail = (int(x) for x in f[1:7])
            return {"total": total, "used": used, "free": free, "available": avail}
    return {}


def parse_lsblk(text: str) -> list[dict]:
    out = []
    for ln in text.splitlines():
        f = ln.split(None, 3)
        if len(f) >= 3 and f[2] == "disk" and f[1].isdigit():
            out.append({"name": f[0], "size": int(f[1]), "model": f[3].strip() if len(f) > 3 else ""})
    return out


def parse_df(text: str) -> list[dict]:
    out = []
    for ln in text.splitlines()[1:]:
        f = ln.split()
        if len(f) >= 6 and f[1].isdigit() and f[2].isdigit() and f[3].isdigit():
            out.append({"fs": f[0], "mount": f[5], "size": int(f[1]), "used": int(f[2]), "avail": int(f[3])})
    return out


def parse_platform_summary(text: str) -> dict:
    kv = {k.strip(): v.strip() for k, _, v in (ln.partition(":") for ln in text.splitlines()) if v}
    return {"platform": kv.get("Platform", ""), "hwsku": kv.get("HwSKU", ""), "asic_count": kv.get("ASIC Count", ""),
            "serial": kv.get("Serial Number", ""), "model": kv.get("Model Number", "")}


def node_info(sec: dict[str, str]) -> dict:
    """Common inventory of one node from HW_SCRIPT output sections."""
    dmi = parse_kv(sec.get("dmi", ""))
    load = (sec.get("loadavg", "").split() or [""])[:3]
    return {"vendor": dmi.get("sys_vendor", ""), "product": dmi.get("product_name", ""),
            "board": dmi.get("board_name", ""), "serial": dmi.get("product_serial", ""),
            "cpu": parse_lscpu(sec.get("lscpu", "")), "cpu_busy_pct": cpu_busy_pct(sec.get("stat1", ""), sec.get("stat2", "")),
            "load": [float(x) for x in load if re.fullmatch(r"[\d.]+", x)],
            "memory": parse_free_b(sec.get("free", "")), "disks": parse_lsblk(sec.get("lsblk", "")),
            "filesystems": parse_df(sec.get("df", ""))}


def _val(v):
    """amd-smi value: {"value": x, "unit": u} | "N/A" | x -> number or None."""
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def gpu_info(static_text: str, metric_text: str) -> list[dict]:
    """Per GPU: model, IFWI, serial, physical accelerator id, VRAM total / used (MB), gfx busy %, power W, hotspot C."""
    def load(t):
        try:
            d = json.loads(t)
        except ValueError:
            return {}
        return {g.get("gpu"): g for g in (d.get("gpu_data") if isinstance(d, dict) else d) or []}
    st, mt = load(static_text), load(metric_text)
    out = []
    for gpu in sorted(set(st) | set(mt), key=lambda x: (x is None, x)):
        s, m = st.get(gpu, {}), mt.get(gpu, {})
        asic, ifwi, mem = s.get("asic") or {}, s.get("ifwi") or {}, m.get("mem_usage") or {}
        out.append({"gpu": gpu, "model": ifwi.get("name") or asic.get("market_name", ""),
                    "ifwi": ifwi.get("version", ""), "serial": asic.get("asic_serial", ""),
                    "physical_acc_id": asic.get("physical_acc_id"),
                    "vram_total_mb": _val(mem.get("total_vram")) or _val((s.get("vram") or {}).get("size")),
                    "vram_used_mb": _val(mem.get("used_vram")),
                    "gfx_busy_pct": _val((m.get("usage") or {}).get("gfx_activity")),
                    "power_w": _val((m.get("power") or {}).get("socket_power")),
                    "hotspot_c": _val((m.get("temperature") or {}).get("hotspot"))})
    return out


_HW = re.compile(r"\[Hardware Error\]", re.I)
_UE = re.compile(r"Uncorrected|Deferred error|Fatal|panic|\|UE\||\bPCC\b", re.I)
_CE_HDR = re.compile(r"Corrected error", re.I)
_CE_STATUS = re.compile(r"MC\d+_STATUS\[[^\]]*\|CE\|")
_OTHER = re.compile(r"Machine Check|\bMCE\b|AER:|amdgpu.*(error|fail|timeout|hang|reset)|ifoe.*(error|fail)", re.I)


def classify_kernel_errors(lines: list[str]) -> dict:
    """Kernel error lines -> corrected machine-check events (count), uncorrected / deferred machine-check lines, and
    other hardware lines (AER, amdgpu, IFoE, unclassified MCE). A machine-check dump is a header ('Corrected error, no
    action required.' / 'Uncorrected ...' / 'Deferred error ...') followed by '[Hardware Error]:' detail lines that
    belong to it."""
    corrected, uncorrected, other = 0, [], []
    state = None
    for ln in lines:
        if _HW.search(ln):
            if _CE_HDR.search(ln) and not _UE.search(ln):
                corrected += 1
                state = "ce"
            elif _UE.search(ln):
                state = "ue"
                uncorrected.append(ln)
            elif state == "ue":
                uncorrected.append(ln)
            elif state == "ce" or _CE_STATUS.search(ln):
                pass                      # detail line of a corrected event
            else:
                other.append(ln)
        elif _OTHER.search(ln):
            state = None
            other.append(ln)
    return {"corrected_events": corrected, "uncorrected": uncorrected, "other": other}
