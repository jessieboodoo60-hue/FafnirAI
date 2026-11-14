"""Tests for the agent workforce (fafnir/workforce.py) with stand-ins for StarNet and the local model.

    .venv\\Scripts\\python -m pytest tests\\test_workforce.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fafnir import ops as O, workforce as W  # noqa: E402


class Fakes:
    def __init__(self):
        self.starnet_calls, self.local_calls = [], []
        self.starnet_usd = 0.0
        self.fail_starnet = 0

    def starnet(self, agent, instruction, seconds=900):
        self.starnet_calls.append((agent, instruction))
        if self.fail_starnet:
            self.fail_starnet -= 1
            raise RuntimeError("run didn't finish")
        return {"text": f"[{agent}] findings for: {instruction[:40]}", "usd": self.starnet_usd}

    def local(self, prompt, model, schema=None, max_tokens=900):
        self.local_calls.append(prompt[:60])
        if schema and "verdict" in json.dumps(schema):
            return json.dumps({"verdict": "pass", "missing": [], "unsupported_claims": []})
        if schema and "tasks" in json.dumps(schema):
            return json.dumps({"tasks": [{"role": "research", "instruction": "Find website agencies in Trinidad"},
                                         {"role": "marketing", "instruction": "Draft a positioning statement"},
                                         {"role": "outreach", "instruction": "Email 50 businesses"}]})   # not allowed at level 1
        return "Executive summary: research and marketing done."


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "starnet_available", lambda: True)
    settings = {"autonomy": {"level": 1}, "budget": {"monthly_usd": 50}}
    notes, gaming = [], {"on": False}
    o = O.Ops(tmp_path / "ops.db", settings=lambda: settings, notify=lambda t, u: notes.append(t))
    f = Fakes()
    wf = W.Workforce(o, notify=lambda t, d, u: notes.append(t), gaming=lambda: gaming["on"],
                     starnet=f.starnet, local=f.local)
    return o, wf, f, settings, notes, gaming


def run_all(o, limit=100):
    n = 0
    while n < limit and o.run_once():
        n += 1
    return n


def test_fourteen_roles_with_owners(env):
    _, wf, *_ = env
    roster = {a["id"]: a for a in wf.roster(live=False)}
    assert len(roster) == 14
    assert roster["research"]["worker"].startswith("StarNet") and roster["lead_scoring"]["worker"] == "Fafnir local model"
    assert roster["image"]["state"] == "unavailable" and "Higgsfield" in roster["image"]["why"]
    assert roster["outreach"]["state"] == "unavailable" and "level 2" in roster["outreach"]["why"]


def test_research_task_runs_on_starnet_with_qa(env):
    o, wf, f, _, notes, _ = env
    j = wf.assign("research", "Which Port of Spain businesses lack websites?")
    run_all(o)
    job = o.job(j["id"])
    assert job["status"] == "done" and f.starnet_calls[0][0] == "researcher"
    assert "Don't contact anyone" in f.starnet_calls[0][1], "every brief forbids acting on its own"
    assert job["result"]["qa"]["verdict"] == "pass"
    assert any("Research finished" in n for n in notes)


def test_autonomy_level_gates_roles(env):
    o, wf, f, settings, *_ = env
    with pytest.raises(O.Refused, match="level 2"):
        wf.assign("outreach", "Draft an intro email")
    settings["autonomy"]["level"] = 2
    j = wf.assign("outreach", "Draft an intro email")
    run_all(o)
    assert o.job(j["id"])["status"] == "done" and f.starnet_calls[-1][0] == "copywriter"


def test_roles_needing_a_missing_integration_refuse(env):
    _, wf, _, settings, *_ = env
    settings["autonomy"]["level"] = 4
    with pytest.raises(O.Refused, match="Higgsfield"):
        wf.assign("image", "A crimson dragon banner")


def test_goal_is_split_run_and_summarised(env):
    o, wf, f, _, notes, _ = env
    g = wf.give_goal("Look into starting a website agency")
    for _ in range(4):
        run_all(o)
        for j in o.jobs():
            if j["status"] == "queued":   # skip the 'waiting for tasks' pause instead of sleeping
                o._db.execute("UPDATE jobs SET next_run=0 WHERE id=?", (j["id"],))
    goal = o.job(g["id"])
    assert goal["status"] == "done", goal
    kids = o.children(g["id"])
    assert sorted(k["kind"] for k in kids) == ["task:marketing", "task:research"], "the level-2 outreach task was dropped"
    assert goal["result"]["summary"].startswith("Executive summary")
    assert any("Goal finished" in n for n in notes)
    assert not any("Research finished" in n for n in notes), "sub-tasks report through the goal, not one by one"


def test_crash_after_paid_work_does_not_redo_it(env):
    o, wf, f, *_ = env
    j = wf.assign("research", "One question")
    original_qa = wf._qa
    wf._qa = lambda *a: (_ for _ in ()).throw(SystemExit("power cut"))
    with pytest.raises(SystemExit):
        o.run_once()
    wf._qa = original_qa
    o._db.execute("UPDATE jobs SET owner_pid=999999999 WHERE id=?", (j["id"],))   # that program is gone
    o2 = O.Ops(o.path, settings=o.settings)
    wf2 = W.Workforce(o2, starnet=f.starnet, local=f.local)
    run_all(o2)
    assert o2.job(j["id"])["status"] == "done" and len(f.starnet_calls) == 1, "the StarNet run wasn't repeated"


def test_gaming_defers_local_work(env):
    o, wf, f, _, _, gaming = env
    gaming["on"] = True
    j = wf.assign("finance", "Summarise this month's AI spending")
    run_all(o)
    assert o.job(j["id"])["status"] == "queued" and f.local_calls == [] and "game" in o.job(j["id"])["error"]


def test_failures_retry_then_pause_the_role(env):
    o, wf, f, _, notes, _ = env
    f.fail_starnet = 99
    for i in range(3):
        wf.assign("marketing", f"Plan {i}")
    for _ in range(12):
        run_all(o)
        o._db.execute("UPDATE jobs SET next_run=0 WHERE status='retrying'")
    assert o.role_suspended("marketing")
    with pytest.raises(O.Refused, match="suspended"):
        wf.assign("marketing", "One more")


def test_paid_crew_runs_count_against_the_budget(env):
    o, wf, f, settings, *_ = env
    f.starnet_usd = 0.4
    wf.assign("research", "Paid question")
    run_all(o)
    assert o.budget()["by_provider"] == {"starnet": 0.4}
    settings["budget"]["monthly_usd"] = 0.5
    j = wf.assign("research", "Another paid question")
    run_all(o)
    assert o.job(j["id"])["status"] == "failed" and "budget" in o.job(j["id"])["error"].lower()
    assert len(f.starnet_calls) == 1, "refused before StarNet was called"


def test_starnet_timeout_falls_back_to_a_labelled_local_answer(env):
    o, wf, f, *_ = env

    def timeout(agent, instruction, seconds=600):
        raise W.StarNetTimeout("StarNet's researcher ran out of time without an answer")
    wf._starnet = timeout
    j = wf.assign("research", "What does incorporation cost?")
    run_all(o)
    out = o.job(j["id"])["result"]["output"]
    assert o.job(j["id"])["status"] == "done" and out.startswith("[Answered by Fafnir's local model WITHOUT web search")
