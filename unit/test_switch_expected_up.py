"""TC-SWH-030 expected_up_ports: "described" means a compute cabling description, not any label (SONiC BD.9 labels
the empty front-panel cages)."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "switch_hw_mod", Path(__file__).resolve().parents[1] / "tests" / "l1" / "test_switch_hw.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class _Sw:
    host = SimpleNamespace(extra={})

    def namespaces(self):
        return ["asic0"]

    def port_descriptions(self, ns):
        return {"Ethernet1": "To ComputeTray Slot0/GPU0[0]/Port6", "Ethernet513": "Front-panel 100G SW0 QSFP port",
                "Ethernet2": ""}


def test_described_counts_only_cabling_descriptions():
    inv = SimpleNamespace(th=lambda key, default=None: "described" if key == "switch.expected_up_ports" else default)
    up = mod._expected_up(inv, _Sw())
    assert [p for p in ("Ethernet1", "Ethernet513", "Ethernet2") if up(p)] == ["Ethernet1"]
