"""Score a brain on Fafnir's fixed test set (tests/brain_cases.json).

For each request: which tool does the brain reach for first (or does it correctly answer
without one), and how long until its first word or tool call. NOTHING IS EXECUTED: every tool
call is recorded and answered with a stub, and the turn is stopped there. Nothing is written
to memory or the conversation log.

    .venv\\Scripts\\python tests\\brain_eval.py                 # the brain in config.toml
    .venv\\Scripts\\python tests\\brain_eval.py --out docs/benchmarks/reference.json

A case passes when the first tool is one of `expect` (or no tool is used when `expect` is
empty), and the answer contains `answer_has` when that is given.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fafnir.common import build_agent  # noqa: E402
from fafnir.config import load_config  # noqa: E402


def main() -> int:
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    only = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    cases = json.loads((ROOT / "tests" / "brain_cases.json").read_text(encoding="utf-8"))[:only]
    cfg = load_config()
    agent = build_agent(cfg)
    label = f"{cfg.model.provider}:{cfg.model.name}"
    print(f"Brain under test: {label} | {len(cases)} cases | tools are recorded, never run\n")

    state = {"tools": [], "first": None, "t0": 0.0, "cancel": None}

    def mark() -> None:
        if state["first"] is None:
            state["first"] = time.perf_counter() - state["t0"]

    def fake_run(name, args, confirm=None, owner_present=True):
        mark()
        state["tools"].append(name)
        state["cancel"].set()   # one tool choice is all we score; stop the turn here
        return "Test mode: recorded, not carried out. Stop now.", False

    agent.registry.run = fake_run

    def on_tool(name, args, result, is_error) -> None:   # built-in web tools arrive here
        mark()
        if name not in state["tools"]:
            state["tools"].append(name)
        state["cancel"].set()

    def on_text(t: str) -> None:
        if t.strip():
            mark()

    rows = []
    for i, c in enumerate(cases, 1):
        try:
            agent.reset()                       # every case starts a fresh conversation
            if hasattr(agent, "warm_up"):
                agent.warm_up()                 # connection time isn't part of the score
        except Exception:
            pass
        state.update(tools=[], first=None, cancel=threading.Event(), t0=time.perf_counter())
        err = ""
        try:
            r = agent.run_turn(c["say"], on_text=on_text, cancel=state["cancel"], on_tool=on_tool)
            text = (r.text or "").strip()
        except Exception as e:  # a failed turn is a failed case, not a crashed run
            text, err = "", f"{type(e).__name__}: {e}"
        total = time.perf_counter() - state["t0"]
        first_tool = state["tools"][0] if state["tools"] else None
        ok = (first_tool in c["expect"]) if c["expect"] else (first_tool is None and bool(text))
        if ok and c.get("answer_has"):
            ok = c["answer_has"].lower() in text.lower()
        rows.append({"say": c["say"], "expect": c["expect"], "first_tool": first_tool, "ok": ok,
                     "first_s": round(state["first"] if state["first"] is not None else total, 2),
                     "total_s": round(total, 2), "answer": text[:120], "error": err})
        got = first_tool or repr(text[:40]) or "(nothing)"
        print(f"{i:2} {'ok ' if ok else 'BAD'} {rows[-1]['first_s']:5.1f}s  {c['say'][:46]:46} -> {got}{'  ' + err if err else ''}")

    firsts = sorted(r["first_s"] for r in rows)
    passed = sum(r["ok"] for r in rows)
    summary = {"brain": label, "date": time.strftime("%Y-%m-%d %H:%M"), "cases": len(rows), "passed": passed,
               "first_response_median_s": firsts[len(firsts) // 2], "first_response_p90_s": firsts[int(len(firsts) * 0.9)],
               "first_response_worst_s": firsts[-1]}
    print(f"\n{label}: {passed}/{len(rows)} passed | first response: median {summary['first_response_median_s']}s, "
          f"90% under {summary['first_response_p90_s']}s, worst {summary['first_response_worst_s']}s")
    if out:
        p = ROOT / out
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
        print(f"saved to {out}")
    try:
        agent.reset()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
