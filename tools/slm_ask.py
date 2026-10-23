"""Ask the trained model about one run: each case result with evidence in the run folder is turned into the model's
input (tools/dataset_export.py, the same as in training), the model answers with its JSON diagnosis, and the answer
is shown next to what the rules say. Read-only: it only reads the run folder.

    python tools/slm_ask.py ~/dev/helios-tests/results/runs/g02-g17/g02-g17-20261023-064053 \\
        --model mlx-community/Qwen3-4B-Instruct-2507-4bit --adapter dataset/mlx-blind-4/adapters
    python tools/slm_ask.py RUN_DIR --tc TC-SWH-060 --show-input          # one case, with what the model was given
    python tools/slm_ask.py RUN_DIR --backend openai --url http://127.0.0.1:11434/v1 --model helios-diag

--blind (default; --no-blind to turn it off) gives the model what it was trained on: the evidence without the test's
own verdict / message where a timeline or evidence block exists. Run it with the MLX environment (.venv-mlx) for
--backend mlx. The model is advisory: check its summary against the evidence before acting (it can copy long IDs
slightly wrong).
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import dataset_export as dx  # noqa: E402
import slm_eval as ev  # noqa: E402
import slm_prep as prep  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run", type=Path, help="a run folder (with tc_results.csv)")
    ap.add_argument("--tc", default="", help="only these case ids (comma separated)")
    ap.add_argument("--backend", choices=("mlx", "openai"), default="mlx")
    ap.add_argument("--model", required=True)
    ap.add_argument("--adapter", default=None, help="mlx: the LoRA adapter folder (e.g. dataset/mlx-blind-4/adapters)")
    ap.add_argument("--url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--no-blind", action="store_true", help="also give the model the test's verdict and message")
    ap.add_argument("--show-input", action="store_true", help="print what the model was given")
    ap.add_argument("--json", action="store_true", help="one JSON line per case instead of the text view")
    a = ap.parse_args(argv)
    if not (a.run / "tc_results.csv").is_file():
        raise SystemExit(f"{a.run}: no tc_results.csv")
    want = {t.strip() for t in a.tc.split(",") if t.strip()}
    exs = [e for e in dx.examples(a.run) if not want or e["meta"]["tc_id"] in want]
    if not exs:
        raise SystemExit(f"{a.run}: no case with evidence the model knows ({', '.join(dx.CASES)})")
    gen = ev.Mlx(a.model, a.adapter, a.max_tokens) if a.backend == "mlx" else ev.OpenAI(a.url, a.model, a.max_tokens)
    for ex in exs:
        rec = dx.as_record(ex, "chat")
        if not a.no_blind:
            rec = prep.blind(rec)
        text = gen(rec["messages"][:-1], rec)
        ans = ev.parse_answer(text)
        rules = ex["output"]
        if a.json:
            print(json.dumps({"tc_id": ex["meta"]["tc_id"], "model": ans, "rules": rules, "raw": None if ans else text}))
            continue
        print(f"\n=== {ex['meta']['tc_id']}  ({ex['meta'].get('test') or ''})")
        if a.show_input:
            print(textwrap.indent(rec["messages"][1]["content"], "  | "))
        if not ans:
            print("  model: no JSON answer -", text[:300].replace("\n", " "))
            continue
        agree = "agrees with the rules" if (ans.get("fault"), ans.get("root_cause")) == (rules["fault"], rules["root_cause"]) \
            else f"rules say {rules['fault']} / {rules['root_cause']}"
        print(f"  fault       {ans.get('fault')} / {ans.get('root_cause')}   ({ans.get('category')}; {agree})")
        print(textwrap.fill(str(ans.get("summary", "")), 116, initial_indent="  summary     ", subsequent_indent=" " * 14))
        print(textwrap.fill(str(ans.get("next_step", "")), 116, initial_indent="  next step   ", subsequent_indent=" " * 14))
        if ev.unsafe(ans):
            print(f"  !! the next step suggests a disruptive action ({ev.unsafe(ans)}) - do not follow it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
