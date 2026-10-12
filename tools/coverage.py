"""Automation coverage of the test plan: which manifest cases have a @pytest.mark.tc test.

    python tools/coverage.py            # per-category table
    python tools/coverage.py --missing  # also list unautomated IDs
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def automated_ids() -> dict[str, str]:
    out = {}
    for f in sorted((ROOT / "tests").rglob("test_*.py")):
        for tc in re.findall(r"""mark\.tc\(\s*["']([^"']+)["']""", f.read_text()):
            out[tc] = str(f.relative_to(ROOT))
    return out


def main() -> None:
    cases = yaml.safe_load((ROOT / "testplan" / "manifest.yaml").read_text())["cases"]
    done = automated_ids()
    known = {c["id"] for c in cases}
    total, auto = Counter(), Counter()
    for c in cases:
        total[c["category"]] += 1
        auto[c["category"]] += c["id"] in done
    print(f"{'category':<20} {'automated':>9} {'total':>6}")
    for cat in sorted(total, key=lambda k: (-total[k], k)):
        print(f"{cat:<20} {auto[cat]:>9} {total[cat]:>6}")
    print(f"{'TOTAL':<20} {sum(auto.values()):>9} {sum(total.values()):>6}")
    stray = sorted(set(done) - known)
    if stray:
        print(f"\ntests tagged with IDs not in the manifest: {stray}")
    if "--missing" in sys.argv:
        for c in cases:
            if c["id"] not in done:
                print(f"  {c['id']:<18} {c['level']}  {c['title']}")


if __name__ == "__main__":
    main()
