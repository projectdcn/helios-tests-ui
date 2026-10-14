"""Automation coverage of the test plan: which manifest cases have a @pytest.mark.tc test.

    python tools/coverage.py            # per-category table
    python tools/coverage.py --missing  # also list unautomated IDs
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from plan_cases import load_cases  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def automated_ids() -> dict[str, str]:
    out = {}
    for f in sorted((ROOT / "tests").rglob("test_*.py")):
        for tc in re.findall(r"""mark\.tc\(\s*["']([^"']+)["']""", f.read_text()):
            out[tc] = str(f.relative_to(ROOT))
    return out


def coverage() -> dict:
    """{"by_category": {cat: (automated, total)}, "by_level": {...}, "total": (automated, total), "extra": [...],
    "automated": {id}, "cases": [...]} - the same numbers main() prints."""
    cases = load_cases()
    done = automated_ids()
    cat, lvl = Counter(), Counter()
    cat_a, lvl_a = Counter(), Counter()
    for c in cases:
        cat[c["category"]] += 1
        lvl[c["level"]] += 1
        cat_a[c["category"]] += c["id"] in done
        lvl_a[c["level"]] += c["id"] in done
    return {"by_category": {k: (cat_a[k], cat[k]) for k in sorted(cat, key=lambda k: (-cat[k], k))},
            "by_level": {k: (lvl_a[k], lvl[k]) for k in sorted(lvl)},
            "total": (sum(cat_a.values()), sum(cat.values())),
            "extra": sorted(set(done) - {c["id"] for c in cases}),
            "automated": set(done), "cases": cases}


def main() -> None:
    cases = load_cases()
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
        print(f"\nextra tests (IDs not in the test plan): {stray}")
    if "--missing" in sys.argv:
        for c in cases:
            if c["id"] not in done:
                print(f"  {c['id']:<18} {c['level']}  {c['title']}")


if __name__ == "__main__":
    main()
