"""The Results tab's Ask / Diagnose panel: questions about one run answered by a small language model from that run's
evidence (tools/slm_ask.py: the evidence, AFM names annotated with their trays from the rack's inventory, the trays'
cabling from the newest link scan, the read-only command reference), and the per-case diagnosis next to the rules.

The model runs elsewhere, behind an OpenAI-compatible endpoint the UI server calls - nothing is sent anywhere else:
    HELIOS_AI_URL    e.g. http://127.0.0.1:8090/v1   (mlx_lm.server on a Mac; Ollama / llama.cpp / vLLM on Linux)
    HELIOS_AI_MODEL  the model name the server expects (mlx_lm.server: the --model it was started with)
    HELIOS_AI_KEY    optional bearer key, if the endpoint wants one
in config/ui.env (installed UI) or the environment. Not set: the panel says so. One question at a time (a lock):
a small model server answers slowly and would only queue them anyway. Answers are advisory; an answer suggesting a
disruptive action is flagged.
"""
from __future__ import annotations

import os
import sys
import threading
import urllib.request
from pathlib import Path

from .config import ROOT
from .runner import NAME_RX, JobError

sys.path.insert(0, str(ROOT / "tools"))

MAX_QUESTION = 2000
MAX_TURNS = 6
_LOCK = threading.Lock()


def settings(environ=None) -> dict:
    env = os.environ if environ is None else environ
    return {"url": (env.get("HELIOS_AI_URL") or "").strip().rstrip("/"), "model": (env.get("HELIOS_AI_MODEL") or "").strip(),
            "key": bool((env.get("HELIOS_AI_KEY") or "").strip())}


def status(environ=None) -> dict:
    """{configured, url, model, reachable[, error]} - never the key."""
    s = settings(environ)
    out = {"configured": bool(s["url"] and s["model"]), "url": s["url"], "model": s["model"]}
    if not out["configured"]:
        out["error"] = "no model configured: set HELIOS_AI_URL and HELIOS_AI_MODEL (config/ui.env), then restart the UI"
        return out
    try:
        req = urllib.request.Request(s["url"] + "/models", headers=_auth(environ))
        with urllib.request.urlopen(req, timeout=3) as r:
            out["reachable"] = r.status == 200
    except Exception as e:  # noqa: BLE001 - down, refused, timeout
        out["reachable"] = False
        out["error"] = f"model server not answering at {s['url']}: {type(e).__name__}"
    return out


def _auth(environ=None) -> dict:
    env = os.environ if environ is None else environ
    key = (env.get("HELIOS_AI_KEY") or "").strip()
    return {"Authorization": "Bearer " + key} if key else {}


def _generator(environ=None, max_tokens: int = 700):
    import slm_eval
    s = settings(environ)
    if not (s["url"] and s["model"]):
        raise JobError("no model configured: set HELIOS_AI_URL and HELIOS_AI_MODEL (config/ui.env), then restart the UI")
    key = ((os.environ if environ is None else environ).get("HELIOS_AI_KEY") or "").strip()
    if key:                                   # slm_eval.OpenAI sends $SLM_API_KEY as the bearer key
        os.environ["SLM_API_KEY"] = key
    return slm_eval.OpenAI(s["url"], s["model"], max_tokens)


def run_dir(profile, name: str) -> Path:
    if not NAME_RX.match(name or "") or "-session-" in name:
        raise JobError(f"{name}: not a run folder")
    d = profile.results_dir / name
    if not (d / "tc_results.csv").is_file():
        raise KeyError(f"no run {name!r} with results")
    return d


def ask(profile, name: str, question: str, history=None, environ=None) -> dict:
    """{answer, unsafe}: the question about run ``name`` answered from its evidence; ``history`` [{q, a}] the earlier
    turns of this conversation (the last MAX_TURNS are sent)."""
    import dataset_export as dx
    import slm_ask
    import slm_eval
    import slm_ground
    q = str(question or "").strip()
    if not q:
        raise JobError("type a question")
    if len(q) > MAX_QUESTION:
        raise JobError(f"the question is longer than {MAX_QUESTION} characters")
    d = run_dir(profile, name)
    inv = profile.inventory if profile.inventory and Path(profile.inventory).is_file() else None
    context = slm_ask.evidence_context(d, dx.examples(d), inventory=inv)
    turns = [(str(t.get("q", ""))[:MAX_QUESTION], str(t.get("a", ""))[:4000]) for t in (history or []) if isinstance(t, dict)]
    told = " ".join(t for t, _a in turns)
    new = [t for t in slm_ground.trays_in(q) if f"Cabling of {t} " not in context and t not in told]
    cab = slm_ground.tray_cabling(d.parent, new) if new else ""
    msgs = [{"role": "system", "content": slm_ask.QA_SYSTEM}]
    for k, (pq, pa) in enumerate(turns[-MAX_TURNS:] + [(q + (f"\n\n{cab}" if cab else ""), None)]):
        msgs.append({"role": "user", "content": (context + "\n\nQuestion: " + pq) if k == 0 else pq})
        if pa is not None:
            msgs.append({"role": "assistant", "content": pa})
    gen = _generator(environ)
    with _LOCK:
        answer = slm_ask._clean(gen(msgs, None))
    bad = slm_eval.UNSAFE.search(answer)
    return {"answer": answer, "unsafe": bad.group(0) if bad else None}


def diagnose(profile, name: str, environ=None) -> dict:
    """{cases: [{tc_id, test, model, rules, agrees, unsafe}]} - the model's diagnosis of each case with evidence (blind,
    as it was trained) next to the rules'."""
    import dataset_export as dx
    import slm_eval
    import slm_prep
    d = run_dir(profile, name)
    gen = _generator(environ, max_tokens=512)
    out = []
    for ex in dx.examples(d):
        rec = slm_prep.blind(dx.as_record(ex, "chat"))
        with _LOCK:
            text = gen(rec["messages"][:-1], rec)
        ans = slm_eval.parse_answer(text)
        rules = ex["output"]
        out.append({"tc_id": ex["meta"]["tc_id"], "test": ex["meta"].get("test", ""), "model": ans,
                    "raw": None if ans else text[:500], "rules": rules,
                    "agrees": bool(ans) and (ans.get("fault"), ans.get("root_cause")) == (rules["fault"], rules["root_cause"]),
                    "unsafe": slm_eval.unsafe(ans)})
    if not out:
        raise JobError(f"{name}: no case the model knows ({', '.join(dx.CASES)})")
    return {"cases": out}
