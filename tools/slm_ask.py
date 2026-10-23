"""Ask the trained model about one run: each case result with evidence in the run folder is turned into the model's
input (tools/dataset_export.py, the same as in training), the model answers with its JSON diagnosis, and the answer
is shown next to what the rules say. Read-only: it only reads the run folder.

    python tools/slm_ask.py ~/dev/helios-tests/results/runs/g02-g17/g02-g17-20261023-064053 \\
        --model mlx-community/Qwen3-4B-Instruct-2507-4bit --adapter dataset/mlx-blind-4/adapters
    python tools/slm_ask.py RUN_DIR --tc TC-SWH-060 --show-input          # one case, with what the model was given
    python tools/slm_ask.py RUN_DIR --backend openai --url http://127.0.0.1:11434/v1 --model helios-diag

    python tools/slm_ask.py RUN_DIR --question "why was TC-SWH-060 blocked, which trays should I look at?" \
        --model mlx-community/Qwen3-4B-Instruct-2507-4bit --inventory inventory/fullrack-jump-afm.local.yaml

    python tools/slm_ask.py RUN_DIR --chat --model mlx-community/Qwen3-4B-Instruct-2507-4bit \
        --inventory inventory/fullrack-jump-afm.local.yaml          # several questions, follow-ups work

--question: a free-text answer to your question about this run, from its evidence only (all cases, or --tc ones,
with everything they recorded); the model is told to say when the evidence does not show something. Without
--adapter (the untrained model) answers questions in prose; the trained adapter leans to its JSON diagnosis format.

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
import slm_ground as ground  # noqa: E402
import slm_eval as ev  # noqa: E402
import slm_prep as prep  # noqa: E402


QA_SYSTEM = ("You answer questions about AMD Helios rack test results (SONiC switch trays, AFM fabric manager, compute "
             "trays) using ONLY the evidence given. If the evidence does not show the answer, say so plainly - do not "
             "guess or invent names, files or settings. Copy switch, tray, port and pod names exactly as written. Be "
             "brief. Never suggest disruptive actions (flapping or shutting ports, rebooting or power-cycling, config "
             "reload); say what to check and who should act. Give commands ONLY from the command reference you are "
             "given (exactly as written, asicN = asic0 / asic1); if none fits, say it is not in your reference. "
             "'Port 22' in an SSH error is the SSH port of the tray, not a switch port. A tray's cabling, when given, "
             "is from a dated link scan: say it may have changed since.")


def inventory_names(path: Path | None) -> dict[str, str]:
    """{AFM name: 'tray, address'} from a rack inventory's afm_name keys - the run itself does not say which tray
    pod-1-12-ComputeTray is (slots count from 0: it is ct13)."""
    if not path:
        return {}
    import yaml
    data = yaml.safe_load(Path(path).expanduser().read_text()) or {}
    return {str(e["afm_name"]): f"{e.get('name')}, {e.get('host', '?')}" for k in ("switches", "computes")
            for e in data.get(k) or [] if isinstance(e, dict) and e.get("afm_name")}


def annotate(text: str, names: dict[str, str]) -> str:
    """Every AFM name in ``text`` followed by its tray: 'pod-1-12-ComputeTray [= ct13, 10.255.4.59]'. Done here, not
    by the model: given the table, the model read pod-1-12 as ct12 (22 Oct) - a lookup is code's job."""
    import re
    if not names:
        return text
    rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\b(?![-\w])")
    return rx.sub(lambda m: f"{m.group(1)} [= {names[m.group(1)]}]", text)


def question_input(run: Path, exs: list[dict], question: str, limit: int = 9000, inventory: Path | None = None) -> str:
    """The evidence of the run (evidence_context) followed by the question."""
    return evidence_context(run, exs, limit, inventory) + f"\n\nQuestion: {question}"


def evidence_context(run: Path, exs: list[dict], limit: int = 9000, inventory: Path | None = None) -> str:
    """The evidence of the run's cases (with the verdict and message), what else each recorded, AFM names annotated
    with their tray (--inventory)."""
    import csv
    csv.field_size_limit(10**9)
    with open(run / "tc_results.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    parts = [f"Run: {run.name}"]
    shown = set()
    for ex in exs:
        parts.append(ex["input"])
        shown.add(ex["meta"]["tc_id"])
        row = next((r for r in rows if r.get("tc_id") == ex["meta"]["tc_id"]), {})
        try:
            props = json.loads(row.get("properties") or "{}")
        except ValueError:
            props = {}
        extra = {k: v for k, v in props.items() if k not in ("evidence", "incast_evidence", "pfc_lossless")}
        if extra:
            parts.append("Also recorded by this case: " + json.dumps(extra, default=str)[:1500])
    for r in rows:                                  # cases the diagnosis export does not cover: verdict and message
        if r.get("tc_id") not in shown:
            parts.append(f"Case {r.get('tc_id')} ({r.get('test', '')}): verdict {str(r.get('outcome', '')).upper()}"
                         + (f"; message: {dx.message_excerpt(r['message'], 400)}" if r.get("message") else ""))
    text = annotate("\n\n".join(parts), inventory_names(inventory))
    text = text[:limit] + "\n[evidence cut]" if len(text) > limit else text
    cab = ground.tray_cabling(run.parent, ground.trays_in(text))        # trays named in the evidence
    return text + ("\n\n" + cab if cab else "") + "\n\n" + ground.command_reference()


def _clean(answer: str) -> str:
    return answer.replace("<think>", "").replace("</think>", "").strip()


def _warn_unsafe(answer: str) -> None:
    bad = ev.UNSAFE.search(answer)
    if bad:
        print(f"\n!! the answer mentions a disruptive action ({bad.group(0)}) - do not follow it without review")


CHAT_HELP = "q quit · r start over · e show the evidence the model sees · h help"


def chat(gen, context: str, run_name: str, read=input, keep_turns: int = 6, rack_results: Path | None = None) -> None:
    """Questions one after the other about one run: the evidence stays the first message, the last ``keep_turns``
    question / answer pairs are kept (follow-ups work), older ones are dropped to keep the prompt short."""
    print(f"Questions about {run_name} - answers come from its evidence only.  {CHAT_HELP}")
    turns: list[tuple[str, str]] = []
    told: set[str] = set()
    while True:
        try:
            q = read("› ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not q:
            continue
        if q.lower() in ("q", "quit", "exit"):
            return
        if q.lower() in ("r", "reset"):
            turns, told = [], set()
            print("(started over)")
            continue
        if q.lower() in ("e", "evidence"):
            print(textwrap.indent(context, "  | "))
            continue
        if q.lower() in ("h", "help", "?"):
            print(CHAT_HELP)
            continue
        new = [t for t in ground.trays_in(q) if f"Cabling of {t} " not in context and t not in told]
        if new and rack_results is not None:              # a tray first named in this question: its cabling too
            cab = ground.tray_cabling(rack_results, new)
            if cab:
                q = f"{q}\n\n{cab}"
            told.update(new)
        msgs = [{"role": "system", "content": QA_SYSTEM}]
        for k, (pq, pa) in enumerate(turns[-keep_turns:] + [(q, None)]):
            first = k == 0
            msgs.append({"role": "user", "content": (context + "\n\nQuestion: " + pq) if first else pq})
            if pa is not None:
                msgs.append({"role": "assistant", "content": pa})
        answer = _clean(gen(msgs, None))
        print(answer)
        _warn_unsafe(answer)
        turns.append((q, answer))


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
    ap.add_argument("--question", default="", help="a free-text question about this run, answered from its evidence")
    ap.add_argument("--chat", action="store_true",
                    help="ask questions one after the other about this run (the model and evidence loaded once)")
    ap.add_argument("--inventory", type=Path, default=None,
                    help="--question: the rack inventory, so AFM names (pod-1-12-ComputeTray) map to trays (ct13)")
    a = ap.parse_args(argv)
    if not (a.run / "tc_results.csv").is_file():
        raise SystemExit(f"{a.run}: no tc_results.csv")
    want = {t.strip() for t in a.tc.split(",") if t.strip()}
    exs = [e for e in dx.examples(a.run) if not want or e["meta"]["tc_id"] in want]
    if not exs and not (a.question or a.chat):
        raise SystemExit(f"{a.run}: no case with evidence the model knows ({', '.join(dx.CASES)})")
    gen = ev.Mlx(a.model, a.adapter, a.max_tokens) if a.backend == "mlx" else ev.OpenAI(a.url, a.model, a.max_tokens)
    if a.chat:
        chat(gen, evidence_context(a.run, exs, inventory=a.inventory), a.run.name, rack_results=a.run.parent)
        return 0
    if a.question:
        msgs = [{"role": "system", "content": QA_SYSTEM},
                {"role": "user", "content": question_input(a.run, exs, a.question, inventory=a.inventory)}]
        if a.show_input:
            print(textwrap.indent(msgs[1]["content"], "  | "))
        answer = _clean(gen(msgs, None))
        print(answer)
        _warn_unsafe(answer)
        return 0
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
