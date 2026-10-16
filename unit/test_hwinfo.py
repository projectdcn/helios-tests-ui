"""HC-011 hardware inventory parsers (helios/hwinfo.py), on outputs captured from g02-g17 (14-15 Oct 2026)."""
from __future__ import annotations

import json

from helios.hwinfo import cpu_busy_pct, gpu_info, node_info, parse_df, parse_free_b, parse_lsblk, parse_platform_summary, sections

SCRIPT_OUT = """@@lscpu
Architecture:                         x86_64
CPU(s):                               16
Model name:                           AMD Ryzen Embedded V3C48 8-Core Processor
Core(s) per socket:                   8
Socket(s):                            1
@@dmi
sys_vendor=HPE
product_name=QFX5252
product_serial=
board_name=
@@stat1
cpu  5474827 340 8702905 3182661203 4428 0 934258 0 0 0
@@stat2
cpu  5474927 340 8703005 3182662003 4428 0 934258 0 0 0
@@loadavg
1.20 1.33 1.34 2/2921 2581241
@@free
               total        used        free      shared  buff/cache   available
Mem:     33386127360 20251525120  3559145472   876785664 10931908608 13134602240
Swap:              0           0           0
@@lsblk
loop0  70049792 loop
nvme0n1 2048408248320 disk TS2TMTE712P-VS2
@@df
Filesystem         1-blocks        Used    Available Capacity Mounted on
/dev/nvme0n1p3 470223544320 27635859456 442570907648       6% /host
/dev/loop1      67049664512  1115484160  62481432576       2% /var/log
"""


def test_node_info_switch():
    n = node_info(sections(SCRIPT_OUT))
    assert n["vendor"] == "HPE" and n["product"] == "QFX5252"
    assert n["cpu"] == {"model": "AMD Ryzen Embedded V3C48 8-Core Processor", "cpus": 16, "sockets": 1, "cores_per_socket": 8}
    assert n["cpu_busy_pct"] == 20.0            # 200 busy jiffies of 1000
    assert n["load"] == [1.2, 1.33, 1.34]
    assert n["memory"] == {"total": 33386127360, "used": 20251525120, "free": 3559145472, "available": 13134602240}
    assert n["disks"] == [{"name": "nvme0n1", "size": 2048408248320, "model": "TS2TMTE712P-VS2"}]
    assert [f["mount"] for f in n["filesystems"]] == ["/host", "/var/log"]


def test_parsers_tolerate_garbage():
    assert cpu_busy_pct("", "") is None and cpu_busy_pct("cpu 1 1 1 1", "cpu 1 1 1 1") is None
    assert parse_free_b("nonsense") == {} and parse_lsblk("") == [] and parse_df("") == []
    assert node_info({})["memory"] == {}


def test_platform_summary():
    p = parse_platform_summary("Platform: x86_64-juniper_qfx5252-r0\nHwSKU: QFX5252\nASIC: broadcom\nASIC Count: 2\n"
                               "Serial Number: LH3326AN0003\nModel Number: 650-198880\n")
    assert p == {"platform": "x86_64-juniper_qfx5252-r0", "hwsku": "QFX5252", "asic_count": "2",
                 "serial": "LH3326AN0003", "model": "650-198880"}


def test_gpu_info_real_shapes():
    static = {"nic_data": [{"ai_nic": 0}], "gpu_data": [{"gpu": 0, "asic": {"market_name": "AMD Radeon Graphics",
              "asic_serial": "0x579A9FE36ECA56B1", "physical_acc_id": 3}, "ifwi": {"name": "AMD MI450X_GENERIC",
              "version": "00205629"}, "vram": {"size": {"value": 442368, "unit": "MB"}}}]}
    metric = {"gpu_data": [{"gpu": 0, "usage": {"gfx_activity": {"value": 38, "unit": "%"}},
              "power": {"socket_power": {"value": 1204, "unit": "W"}, "gfx_voltage": "N/A"},
              "temperature": {"edge": "N/A", "hotspot": {"value": 67, "unit": "C"}},
              "mem_usage": {"total_vram": {"value": 442368, "unit": "MB"}, "used_vram": {"value": 170, "unit": "MB"}}}]}
    assert gpu_info(json.dumps(static), json.dumps(metric)) == [{
        "gpu": 0, "model": "AMD MI450X_GENERIC", "ifwi": "00205629", "serial": "0x579A9FE36ECA56B1", "physical_acc_id": 3,
        "vram_total_mb": 442368.0, "vram_used_mb": 170.0, "gfx_busy_pct": 38.0, "power_w": 1204.0, "hotspot_c": 67.0}]
    assert gpu_info("", "not json") == []


def test_amd_smi_runs_with_rocm_path():
    """amd-smi gets /opt/rocm/bin on PATH (not every tray links /usr/bin/amd-smi; sudo resets PATH); other commands
    and explicit env are left alone."""
    from helios.compute import ROCM_PATH, ComputeTray

    calls = []

    class T:
        def run(self, cmd, **kw):
            calls.append((cmd, kw))

    c = ComputeTray.__new__(ComputeTray)
    c.t = T()
    c.run("amd-smi process --json", sudo=True)
    c.run("afmctl show device --json")
    c.run("amd-smi list", env={"X": "1"})
    assert calls[0][1] == {"sudo": True, "env": {"PATH": ROCM_PATH}}
    assert "env" not in calls[1][1] and calls[2][1] == {"env": {"X": "1"}}


def test_classify_kernel_errors():
    from helios.hwinfo import classify_kernel_errors
    ce = ["k: [Hardware Error]: Corrected error, no action required.", "k: [Hardware Error]: CPU:152 (1a:c0:0) MC60_STATUS[Over|CE|MiscV]",
          "k: [Hardware Error]: cache level: L3/GEN, mem/io: IO", "k: [Hardware Error]: FRU Text: PPlErr: G4G0 "]
    ue = ["k: [Hardware Error]: Uncorrected, software restartable error.", "k: [Hardware Error]: CPU:5 MC26_STATUS[-|UE|PCC]"]
    r = classify_kernel_errors(ce * 3 + ["k: amdgpu 0001:01:00.0: ring gfx timeout"] + ue + ce)
    assert r["corrected_events"] == 4
    assert r["uncorrected"] == ue
    assert r["other"] == ["k: amdgpu 0001:01:00.0: ring gfx timeout"]
    assert classify_kernel_errors(["k: [Hardware Error]: CPU:1 MC2_STATUS[-|CE|]"])["corrected_events"] == 0   # no header: detail only
    assert classify_kernel_errors(["k: pcieport 0000:00:01.1: AER: Corrected error received"])["other"]


def test_checks_summarise_failures_by_node():
    import pytest
    from helios.rack import Checks
    c = Checks()
    for m in ("st1/asic0: Ethernet9 down", "st1/asic0: Ethernet10 down", "ct05: unreachable", "pod-1-4-ComputeTray: unhealthy"):
        c.fail(m)
    assert c.by_node() == {"st1": 2, "ct05": 1, "pod-1-4-ComputeTray": 1}
    with pytest.raises(pytest.fail.Exception) as e:
        c.assert_all()
    assert "by node: st1 2, ct05 1, pod-1-4-ComputeTray 1" in str(e.value)
