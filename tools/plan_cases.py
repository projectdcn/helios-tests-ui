"""All test-plan cases: AMD's plan (testplan/manifest.yaml, generated) + cases added by helios-tests
(testplan/added.yaml, hand-maintained, each with ``origin``)."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_cases(include_added: bool = True) -> list[dict]:
    cases = yaml.safe_load((ROOT / "testplan" / "manifest.yaml").read_text())["cases"]
    added = ROOT / "testplan" / "added.yaml"
    if include_added and added.exists():
        cases = cases + (yaml.safe_load(added.read_text()) or {}).get("cases", [])
    return cases
