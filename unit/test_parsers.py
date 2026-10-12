from __future__ import annotations

import pytest

from helios.parsers import (parse_bandwidth, parse_db_hash, parse_kv, parse_rccl, parse_size,
                            parse_speed_gbps, parse_table, to_float)
from helios.transport import build_command

# Verbatim-style SONiC output: right-aligned cells, a cell with spaces, preamble + trailer.
LLDP = """\
Capability codes: (R) Router, (B) Bridge, (O) Other
LocalPort    RemoteDevice    RemotePortID    Capability    RemotePortDescr
-----------  --------------  --------------  ------------  -----------------
Ethernet0    compute-01      ifoe0           R             gpu0 port 0
Ethernet8                    ifoe1           R
--------------------------------------------------
Total entries displayed:  2
"""

COUNTERS = """\
      IFACE    STATE    RX_OK      RX_BPS    RX_UTIL    RX_ERR
-----------  -------  -------  ----------  ---------  --------
  Ethernet0        U    1,234  0.00 B/s        0.00%         5
  Ethernet8        D      N/A         N/A        N/A       N/A
"""


def test_parse_table_handles_blank_cells_preamble_and_trailer():
    rows = parse_table(LLDP)
    assert [r["LocalPort"] for r in rows] == ["Ethernet0", "Ethernet8"]
    assert rows[0]["RemotePortDescr"] == "gpu0 port 0"
    assert rows[1]["RemoteDevice"] == ""


def test_parse_table_cells_with_spaces_and_commas():
    rows = parse_table(COUNTERS)
    assert rows[0]["RX_BPS"] == "0.00 B/s"
    assert to_float(rows[0]["RX_OK"]) == 1234
    assert to_float(rows[1]["RX_ERR"]) is None


def test_parse_table_without_separator_is_empty():
    assert parse_table("no table here\njust text\n") == []


def test_parse_kv_and_db_hash():
    assert parse_kv("Platform: x86\nASIC Count: 2\n")["ASIC Count"] == "2"
    assert parse_db_hash("{'speed': '42', 'presence': 'True'}") == {"speed": "42", "presence": "True"}
    assert parse_db_hash("speed\n42\npresence\nTrue\n") == {"speed": "42", "presence": "True"}
    assert parse_db_hash("") == {}


@pytest.mark.parametrize("raw,gbps", [("400G", 400), ("100G", 100), ("400000", 400), ("N/A", None), ("25000M", 25)])
def test_parse_speed(raw, gbps):
    assert parse_speed_gbps(raw) == gbps


def test_parse_size():
    assert parse_size("1K") == 1024 and parse_size("1G") == 1 << 30 and parse_size(64) == 64


RCCL = """\
#       size         count      type   redop    root     time   algbw   busbw #wrong     time   algbw   busbw #wrong
        1024           256     float     sum      -1    25.12    0.04    0.07      0    24.98    0.04    0.07      0
  1073741824     268435456     float     sum      -1  9876.54  108.71  163.07      0  9870.11  108.78  163.17      0
        1024           256     float    none       0    11.00    0.09    0.09    N/A    11.00    0.09    0.09    N/A
# Out of bounds values : 0 OK
# Avg bus bandwidth    : 54.32
"""


def test_parse_rccl():
    r = parse_rccl(RCCL)
    assert len(r.rows) == 3 and r.max_size == 1 << 30
    assert r.avg_busbw == 54.32 and r.out_of_bounds == 0 and not r.errors
    assert r.rows[1].oop_busbw == 163.07 and r.rows[1].ip_wrong == 0
    assert r.rows[2].root == 0 and r.rows[2].oop_wrong is None
    assert not r.wrong_rows


def test_parse_rccl_flags_errors():
    r = parse_rccl("rank 3 NCCL WARN Call to ibv_create_cq failed\nTest failure common.cu:390\n")
    assert len(r.errors) == 2 and not r.rows


def test_parse_bandwidth_takes_last_summary():
    text = "Executor GPU 00 : 50.1 GB/s\nAggregate bandwidth (GPU Timed): 10.0 GB/s\nAggregate bandwidth (GPU Timed): 612.48 GB/s\n"
    assert parse_bandwidth(text) == 612.48
    assert parse_bandwidth("nothing") is None
    assert parse_bandwidth("BW=77.5GB/s", [r"BW=([\d.]+)GB/s"]) == 77.5


def test_build_command_wraps_compound_commands():
    assert build_command("show x") == "show x"
    assert build_command("a | b", sudo=True) == "sudo -n bash -c 'a | b'"
    assert build_command("run", env={"HIP_VISIBLE_DEVICES": "0,1"}) == "env HIP_VISIBLE_DEVICES=0,1 bash -c run"


PERF = """\
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 4096       1000000          0.00               98.50		   3.006
 65536      7442117          0.00               390.12		   0.744097
---------------------------------------------------------------------------------------
"""


def test_parse_perftest():
    from helios.parsers import parse_perftest

    r = parse_perftest(PERF)
    assert [row.size for row in r.rows] == [4096, 65536]
    assert r.bw_avg == 390.12 and not r.errors
    bad = parse_perftest(" Completion with error at client\n Failed status 12: wr_id 0 syndrom 0x81\n")
    assert len(bad.errors) == 2 and bad.bw_avg is None
