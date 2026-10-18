"""vPOD GPU list read-modify-write keeps every member (g02-g17 18 Oct: status.gpus-status lags a PUT by ~10 s;
TC-NA-016's clean-up re-added slot 8, then 6 ms later slot 9 from a status without slot 8 -> slot 8 in no vPOD,
and TC-E2E-033 / TC-E2E-040 dropped slots 8 / 9 the same way)."""
from __future__ import annotations

import json

from helios.afm_ops import CN, VP, AfmOps


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body
        self.text = json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return self._body


class _Afm:
    """One system vPOD whose status lists the GPUs of the PUT before the last one (AFM's lag); ``drop_once`` makes
    the next PUT lose that GPU once (a spec that does not hold what was PUT)."""

    def __init__(self, slots):
        self.spec = [{"slot-id": s, "index": i} for s in slots for i in range(2)]
        self.status = list(self.spec)
        self.puts, self.drop_once = [], None
        self.nodes = {f"c{s}": {"meta": {"name": f"c{s}"}, "spec": {"slot-id": s}, "status": {"scale-up-info": {
            "gpu-info": {f"g{s}{i}": {"index": i, "vpod-membership": "sys" if s in slots else ""}
                         for i in range(2)}}}} for s in range(10)}

    def _vpod(self):
        return {"meta": {"name": "sys"}, "spec": {"GPUs": [dict(g) for g in self.spec]},
                "status": {"gpus-status": [{"gpu": {"id": f"g{g['slot-id']}{g['index']}", "gpu-index": dict(g)}}
                                           for g in self.status]}}

    def get(self, path):
        if path.startswith(CN):
            return self.nodes[path.rsplit("/", 1)[1]]
        assert path == f"{VP}/sys", path
        return self._vpod()

    def computes(self):
        return list(self.nodes.values())

    def raw(self, method, path, body=None):
        assert method == "PUT" and path == f"{VP}/sys"
        gpus = [{"slot-id": g["slot-id"], "index": g["index"]} for g in body["spec"]["GPUs"]]
        self.puts.append(gpus)
        if self.drop_once in gpus:
            gpus.remove(self.drop_once)
            self.drop_once = None
        self.status, self.spec = self.spec, gpus          # status = the previous spec
        members = {(g["slot-id"], g["index"]) for g in gpus}
        for n in self.nodes.values():
            s = n["spec"]["slot-id"]
            for g in n["status"]["scale-up-info"]["gpu-info"].values():
                g["vpod-membership"] = "sys" if (s, g["index"]) in members else ""
        return _Resp(200, self._vpod())


def _slots(gpus):
    return sorted({g["slot-id"] for g in gpus})


def _entries(slot):
    return [{"id": f"g{slot}{i}", "slot-id": slot, "index": i} for i in range(2)]


def test_back_to_back_adds_keep_the_first():
    afm = _Afm([0, 1])
    ops = AfmOps(afm)
    assert ops.add_gpus("sys", _entries(8))
    assert ops.add_gpus("sys", _entries(9))        # status still lacks slot 8 here
    assert _slots(afm.spec) == [0, 1, 8, 9]


def test_remove_right_after_add_keeps_the_added():
    afm = _Afm([0, 1, 7])
    ops = AfmOps(afm)
    ops.add_gpus("sys", _entries(8))
    ops.remove_compute("sys", "c7")
    assert _slots(afm.spec) == [0, 1, 8]


def test_spec_gpus_carries_ids_from_status():
    ops = AfmOps(_Afm([0]))
    assert ops.spec_gpus("sys") == [{"slot-id": 0, "index": 0, "id": "g00"}, {"slot-id": 0, "index": 1, "id": "g01"}]


def test_put_again_when_the_spec_lost_a_gpu():
    afm = _Afm([0])
    afm.drop_once = {"slot-id": 8, "index": 1}
    ops = AfmOps(afm)
    assert ops.add_gpus("sys", _entries(8))
    assert len(afm.puts) == 2
    assert _slots(afm.spec) == [0, 8] and len(afm.spec) == 4
