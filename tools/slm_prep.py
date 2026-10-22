"""Training files for LoRA fine-tuning of a small language model (MLX-LM on a Mac; the same JSONL works with TRL /
Unsloth) from the dataset of tools/vrack_dataset.py / tools/dataset_export.py.

    python tools/slm_prep.py dataset/vrack-1 -o dataset/mlx-1
    python tools/slm_prep.py dataset/vrack-1 --extra dataset/real-reviewed.jsonl --model mlx-community/<model>-4bit

Reads <dataset>/train.jsonl and <dataset>/eval.jsonl (chat format, with meta). Writes to <out>/:
  train.jsonl, valid.jsonl  - messages only (MLX-LM's chat format); valid = ~10% of the training runs (by run),
                              used for the loss during training
  test.jsonl                - the held-out eval set: score models on it with tools/slm_eval.py
  <name>.jsonl              - each --eval-extra file (e.g. real-rack examples), for scoring
  lora.yaml                 - an MLX-LM LoRA config (mlx_lm.lora --config lora.yaml); set model: first
  prep.json                 - counts per fault before / after balancing, length statistics

--blind removes what the test concluded (the verdict, its message where a timeline exists, the repaired / not
repaired markers), so the model has to read the FDB states, cleanup and counters itself - the realistic job, and a
test the untrained model cannot pass by reading the answer off the message.

Balancing (train only): healthy examples are capped at --max-healthy (default 40%) of the training set, and every
fault class with fewer than --min-per-class examples is repeated up to that count (a class with 9 examples would
otherwise barely register). ``--extra`` files (e.g. reviewed real-rack examples) go into training; examples still
marked needs_review are refused - review them first.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path


# --blind: what the test concluded is removed, the model must read the evidence itself
JUDGED_EVENTS = ("repaired_by_afm", "not_repaired_in_time")


def blind(rec: dict) -> dict:
    """The example without the test's own conclusions: no 'Test verdict' line; no 'Test message' when there is a
    timeline (the timeline is the evidence then); no repaired_by_afm / not_repaired_in_time timeline steps (the FDB
    states, the cleanup and the counters stay). An example without a timeline (an older run, a blocked case) keeps its
    message - it is the only evidence."""
    msgs = [dict(m) for m in rec["messages"]]
    for m in msgs:
        if m["role"] != "user":
            continue
        lines = m["content"].splitlines()
        has_tl = any(ln.startswith(("Timeline", "Evidence")) for ln in lines)
        m["content"] = "\n".join(
            ln for ln in lines
            if not ln.startswith("Test verdict:") and not (has_tl and ln.startswith("Test message:"))
            and not any(ln.lstrip().split(" ", 2)[1:2] == [k] for k in JUDGED_EVENTS))
    return {**rec, "messages": msgs}


def load(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()] if path.is_file() else []


def fault_of(rec: dict) -> str:
    try:
        return json.loads(rec["messages"][-1]["content"]).get("fault", "?")
    except (ValueError, KeyError, IndexError, AttributeError):
        return "?"


def balance(recs: list[dict], max_healthy: float, min_per_class: int, rng: random.Random) -> list[dict]:
    healthy = [r for r in recs if fault_of(r) == "none"]
    faults = [r for r in recs if fault_of(r) != "none"]
    by: dict[str, list] = {}
    for r in faults:
        by.setdefault(fault_of(r), []).append(r)
    out = list(faults)
    for _f, rs in by.items():
        k = 0
        while len(rs) + k < min_per_class:          # repeat a rare class (cycling through its examples)
            out.append(rs[k % len(rs)])
            k += 1
    if max_healthy < 1 and healthy:
        cap = int(max_healthy / (1 - max_healthy) * len(out)) if out else len(healthy)
        rng.shuffle(healthy)
        healthy = healthy[:max(1, cap)]
    out += healthy
    rng.shuffle(out)
    return out


def split_valid(recs: list[dict], frac: float, rng: random.Random) -> tuple[list, list]:
    runs = sorted({(r.get("meta") or {}).get("run", str(i)) for i, r in enumerate(recs)})
    rng.shuffle(runs)
    val = set(runs[:max(1, round(len(runs) * frac))]) if len(runs) > 1 else set()
    run = lambda i, r: (r.get("meta") or {}).get("run", str(i))  # noqa: E731
    return ([r for i, r in enumerate(recs) if run(i, r) not in val], [r for i, r in enumerate(recs) if run(i, r) in val])


def lengths(recs: list[dict]) -> dict:
    """Characters per example and a token estimate (~3.5 characters per token for this text)."""
    n = sorted(sum(len(m["content"]) for m in r["messages"]) for r in recs) or [0]
    q = lambda p: n[min(len(n) - 1, int(p * len(n)))]  # noqa: E731
    return {"chars_median": q(0.5), "chars_p95": q(0.95), "chars_max": n[-1], "tokens_est_max": int(n[-1] / 3.5) + 1}


LORA_YAML = """\
# MLX-LM LoRA fine-tuning (mlx_lm.lora --config {cfg}). Written by tools/slm_prep.py.
# model: a small instruct model, 4-bit MLX conversion. Default Qwen3-4B-Instruct-2507 (Apache-2.0, ~2.3 GB, no
# "thinking" mode - answers JSON directly); alternatives: mlx-community/Phi-4-mini-instruct-4bit (MIT),
# mlx-community/Llama-3.2-3B-Instruct-4bit (Llama 3.2 licence). A 4-bit 3-4B model with these settings fits in 24 GB.
# Environment: .venv-mlx (pip install mlx-lm).
model: {model}
train: true
fine_tune_type: lora
data: {data}
adapter_path: {adapters}
mask_prompt: true            # learn the answer only, not the evidence text
batch_size: {batch}
iters: {iters}               # ~{epochs} epochs over {n_train} training examples
learning_rate: 1.0e-4
num_layers: 16               # LoRA on the last 16 layers
max_seq_length: {max_seq}    # longest example ~{tok_max} tokens (estimate)
steps_per_report: 10
steps_per_eval: 50
val_batches: -1
save_every: 100
grad_checkpoint: true
seed: {seed}
lora_parameters:
  rank: 8
  scale: 20.0
  dropout: 0.05
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dataset", type=Path, help="folder with train.jsonl / eval.jsonl (tools/vrack_dataset.py --out)")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--extra", type=Path, action="append", default=[], help="more training examples (reviewed)")
    ap.add_argument("--max-healthy", type=float, default=0.4, help="largest share of healthy examples in training")
    ap.add_argument("--min-per-class", type=int, default=30, help="repeat a rarer fault class up to this many examples")
    ap.add_argument("--valid-fraction", type=float, default=0.1)
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--keep-unreviewed", action="store_true",
                    help="keep training examples marked needs_review (default: left out - their label may be wrong)")
    ap.add_argument("--blind", action="store_true",
                    help="remove the test's verdict / message / repaired markers: the model reads the evidence itself")
    ap.add_argument("--iters", type=int, default=0, help="training iterations (default: --epochs over the data)")
    ap.add_argument("--eval-extra", type=Path, action="append", default=[],
                    help="another scoring file (e.g. real-rack examples): written to <out>/<name>.jsonl, --blind applied")
    ap.add_argument("--model", default="mlx-community/Qwen3-4B-Instruct-2507-4bit")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    train, test = load(a.dataset / "train.jsonl"), load(a.dataset / "eval.jsonl")
    if not train:
        raise SystemExit(f"no {a.dataset / 'train.jsonl'}")
    for f in a.extra:
        rs = load(f)
        bad = [r for r in rs if (r.get("meta") or {}).get("needs_review")]
        if bad:
            raise SystemExit(f"{f}: {len(bad)} example(s) still marked needs_review - review them (set needs_review "
                             "false in meta, fix the answer if wrong) before training on them")
        train += rs
    unreviewed = [r for r in train if (r.get("meta") or {}).get("needs_review")]
    if unreviewed and not a.keep_unreviewed:      # rules and injected fault disagree: the label may be wrong
        train = [r for r in train if not (r.get("meta") or {}).get("needs_review")]
    if a.blind:
        train, test = [blind(r) for r in train], [blind(r) for r in test]
    tr, val = split_valid(train, a.valid_fraction, rng)
    tr_bal = balance(tr, a.max_healthy, a.min_per_class, rng)
    a.out.mkdir(parents=True, exist_ok=True)
    strip = lambda r: {"messages": r["messages"]}  # noqa: E731
    for name, rs in (("train", tr_bal), ("valid", val)):
        with open(a.out / f"{name}.jsonl", "w") as fh:
            fh.writelines(json.dumps(strip(r), ensure_ascii=False) + "\n" for r in rs)
    with open(a.out / "test.jsonl", "w") as fh:          # meta kept: tools/slm_eval.py reports by run / source
        fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in test)
    for f in a.eval_extra:                               # scoring only: unreviewed labels allowed, but counted
        rs = [blind(r) for r in load(f)] if a.blind else load(f)
        with open(a.out / f.name, "w") as fh:
            fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rs)
    ln = lengths(tr_bal + val + test)
    iters = a.iters or max(50, int(a.epochs * len(tr_bal) / a.batch_size))
    max_seq = 512 * -(-int(ln["tokens_est_max"] * 1.2) // 512)        # rounded up to a multiple of 512
    (a.out / "lora.yaml").write_text(LORA_YAML.format(
        cfg=a.out / "lora.yaml", model=a.model, data=a.out.resolve(), adapters=(a.out / "adapters").resolve(),
        batch=a.batch_size, iters=iters, epochs=round(iters * a.batch_size / max(1, len(tr_bal)), 1),
        n_train=len(tr_bal), max_seq=max_seq,
        tok_max=ln["tokens_est_max"], seed=a.seed))
    info = {"train_before": dict(Counter(fault_of(r) for r in tr)), "train": dict(Counter(fault_of(r) for r in tr_bal)),
            "valid": dict(Counter(fault_of(r) for r in val)), "test": dict(Counter(fault_of(r) for r in test)),
            "n": {"train": len(tr_bal), "valid": len(val), "test": len(test)}, "lengths": ln, "iters": iters,
            "max_seq_length": max_seq, "extra": [str(f) for f in a.extra], "blind": a.blind,
            "left_out_unreviewed": 0 if a.keep_unreviewed else len(unreviewed),
            "eval_extra": {f.name: {"n": len(load(f)), "needs_review": sum(1 for r in load(f)
                                                                          if (r.get("meta") or {}).get("needs_review"))}
                           for f in a.eval_extra}}
    (a.out / "prep.json").write_text(json.dumps(info, indent=1) + "\n")
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
