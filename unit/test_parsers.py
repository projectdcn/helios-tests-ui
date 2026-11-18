from __future__ import annotations

import pytest

from helios.parsers import (parse_bandwidth, parse_db_hash, parse_kv, parse_rccl, parse_size,
                            parse_speed_gbps, parse_table, parse_tb_poda2a, parse_tb_podp2p, parse_tb_smoketest,
                            parse_tb_transfers, parse_tb_wallclock, to_float)
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


def test_parse_prometheus_and_http():
    from helios.parsers import parse_prometheus, split_http_response

    raw = ("HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\n\r\n"
           "# HELP gpu_power_usage W\n# TYPE gpu_power_usage gauge\n"
           'gpu_power_usage{gpu_id="0",card_model="MI455X"} 812.5\n'
           'odd{msg="say \\"hi\\""} NaN\nup 1 1760000000000\n')
    status, headers, body = split_http_response(raw)
    assert status == 200 and headers["content-type"].startswith("text/plain")
    s = parse_prometheus(body)
    assert [x.name for x in s] == ["gpu_power_usage", "odd", "up"]
    assert s[0].labels == {"gpu_id": "0", "card_model": "MI455X"} and s[0].value == 812.5
    assert s[1].labels["msg"] == 'say "hi"' and s[1].value != s[1].value  # NaN kept
    assert s[2].value == 1.0  # trailing timestamp ignored
    assert split_http_response("curl: (7) Failed to connect")[0] is None


# TransferBench v1.70.02 smoketest across 18 trays (g02-g17, 17 Nov 2026), trimmed
TB_SMOKE = """\
Running tests on 72 GPUs total across 18 rank(s)
Legend: *=Pass .=Skip F=Fail
                                                      | GFX | GFX |
| Name                      | GPU | Test | DMA | Test | 001 | 256 |
|---------------------------|-----|------|-----|------|-----|-----|
| Copy (H2D)                | ALL |  01  | *** |  09  | **. | .** |
|---------------------------|-----|------|-----|------|-----|-----|
| Copy (D2D) (Remote Read ) | ALL |  04  | *** |  12  | **. | .** |
| Broadcast  (One to All)   | ALL |  05  | ... |  13  | ... | ... |
All tests passed
"""


def test_parse_tb_smoketest():
    r = parse_tb_smoketest(TB_SMOKE)
    assert (r.gpus, r.ranks, r.passed, r.failed(), r.errors) == (72, 18, True, [], [])
    assert list(r.rows) == ["Copy (H2D)", "Copy (D2D) (Remote Read )", "Broadcast  (One to All)"]
    assert r.ran() == 14                       # '*' cells; skipped '.' not counted
    bad = parse_tb_smoketest(TB_SMOKE.replace("|  09  | **. |", "|  09  | *F. |").replace(
        "All tests passed", "  ERROR: cfg.gfx.unrollFactor) must be consistent across all ranks (Rank 15)"))
    assert bad.failed() == ["Copy (H2D)"] and not bad.passed
    assert bad.errors == ["ERROR: cfg.gfx.unrollFactor) must be consistent across all ranks (Rank 15)"]


TB_POD = """\
[268435456 bytes per Transfer] [GFX:8] [1 Read(s) 1 Write(s)] [MemType:default GPU] [NIC QueuePairs:0] [#Ranks:2]
\u2502 SRC+EXE\\DST \u2502            \u2502 Rank 00         \u2502 Rank 01         \u2502  STotal  \u2502  Actual  \u2502
\u2502     Rank 00 \u2502     GPU 00 \u2502     N/A    3.62 \u2502    3.64    3.65 \u2502    10.91 \u2502    10.80 \u2502
\u2502             \u2502     GPU 01 \u2502    4.48     N/A \u2502    4.50    4.50 \u2502    13.48 \u2502    13.30 \u2502
\u2502     Rank 01 \u2502     GPU 00 \u2502    7.66    7.67 \u2502     N/A    7.75 \u2502    23.08 \u2502    22.90 \u2502
\u2502             \u2502     GPU 01 \u2502    8.99    8.89 \u2502    9.15     N/A \u2502    27.03 \u2502    26.80 \u2502
\u2502             \u2502   RTotal   \u2502   21.13   20.18 \u2502   21.29   15.90 \u2502    74.50 \u2502    73.80 \u2502
"""


def test_parse_tb_poda2a():
    p = parse_tb_poda2a(TB_POD)
    assert p.ranks == 2 and (p.stotal, p.actual) == (74.50, 73.80)
    assert p.src == [(0, 0, 10.91), (0, 1, 13.48), (1, 0, 23.08), (1, 1, 27.03)]
    assert p.rx == [21.13, 20.18, 21.29, 15.90]
    assert parse_tb_poda2a("ERROR: rank 3 failed").errors == ["ERROR: rank 3 failed"]


def test_parse_tb_podp2p():
    text = ("│ SRC Rank │ SRC MEM │ DST Rank │ DST MEM │ bw (GB/s) │\n"
            "│  Rank 00 │  GPU 00 │  Rank 00 │  GPU 01 │       N/A │\n"
            "│  Rank 00 │  GPU 00 │  Rank 01 │  GPU 02 │     48.21 │\n"
            "|  Rank 01 |  GPU 03 |  Rank 00 |  GPU 00 |      7.50 |\n"
            "│  Rank 00 │  GPU 00 │  Rank 01 │  GPU 02 │     96.40 │\n"    # bidirectional table later: ignored
            "│  Rank 17    GPU 03 │  Rank 00    GPU 01 │    224.82 │\n")   # v1.70.02 layout (g02-g17, 17 Nov)
    assert parse_tb_podp2p(text) == {(0, 0, 0, 1): None, (0, 0, 1, 2): 48.21, (1, 3, 0, 0): 7.5, (17, 3, 0, 1): 224.82}


def test_parse_tb_transfers():
    # TransferBench cmdline bisection, 18 ranks (g02-g17, 18 Nov 2026), trimmed
    text = """\
   Executor: Rank 0 GPU 00 │  63.171 GB/s │  16.997 ms │  1073741824 bytes │  45.052 GB/s (sum) [ctheliosr-rck-g02-g17-01]
             Transfer 0    │  45.052 GB/s │  23.833 ms │  1073741824 bytes │ R0G0 -> R0G0:8 -> R9G0
             Transfer 71   │  51.824 GB/s │  20.719 ms │  1073741824 bytes │ R17G3 -> R17G3:8 -> R8G3
           Aggregate (CPU) │4458.837 GB/s │  17.338 ms │ 77309411328 bytes │ Overhead 0.138 ms
"""
    r = parse_tb_transfers(text)
    assert r.transfers == {0: (0, 0, 9, 0, 45.052), 71: (17, 3, 8, 3, 51.824)} and r.aggregate_cpu == 4458.837


def test_parse_tb_wallclock():
    assert parse_tb_wallclock("Minimum Delta detected: 275399.60 cycles (2754.00 usec)\n"
                              "Maximum Delta detected: 686845.80 cycles (6868.46 usec)\n") == 6868.46   # ct01, 18 Nov
    assert parse_tb_wallclock("no figure") is None
