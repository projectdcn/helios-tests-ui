from types import SimpleNamespace

from helios import datapath
from helios.rack import Checks

OUT = """RcclSmokeTest v1.0.0 (42d8776)
Running single-node smoke test
  [1/2] ReduceScatter (float32)    ... PASS
  [2/2] ReduceScatter (bfloat16)   ... PASS

  Legend: P=Pass  F=Fail  S=Skip(OOM)  -=Not run

  Collective                 |   1 KB |   4 MB |   1 GB |
  --------------------------+--------+--------+--------+
  ReduceScatter (float32)    |    P   |    P   |    P   |
  ReduceScatter (bfloat16)   |    P   |    P   |    P   |

  RESULT: ALL TESTS PASSED!
"""


def _check(stdout, ok=True):
    checks = Checks()
    r = SimpleNamespace(ok=ok, rc=0 if ok else 1, stdout=stdout, tail=lambda *a: stdout[-80:])
    bw = datapath.check_rccl("ct02", r, "reduce_scatter", SimpleNamespace(cmd=lambda k, d=None: d, th=lambda k, d=None: d),
                             checks, "rccl")
    return bw, checks


def test_smoke_all_pass():
    bw, checks = _check(OUT)
    assert bw is None and not checks.failures


def test_smoke_fail_cell_and_result():
    bad = OUT.replace("|    P   |    P   |    P   |\n  ReduceScatter (bfloat16)", "|    P   |    F   |    P   |\n  ReduceScatter (bfloat16)")
    bad = bad.replace("ALL TESTS PASSED!", "1 TEST(S) FAILED")
    _, checks = _check(bad)
    msgs = " ".join(checks.failures)
    assert "not passed" in msgs and "4 MB" in msgs and "RESULT" in msgs


def test_smoke_no_table():
    _, checks = _check("RcclSmokeTest v1.0.0\nsegfault\n")
    assert "no result table" in " ".join(checks.failures)
