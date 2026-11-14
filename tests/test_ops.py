"""Tests for the job system, approval queue, budget, autonomy and schedules (fafnir/ops.py).

    .venv\\Scripts\\python -m pytest tests\\test_ops.py -q

Each test uses a throwaway database; time is moved forward by hand instead of waiting.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fafnir import ops as O  # noqa: E402


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(O, "_now", clock)
    settings = {"autonomy": {"level": 1}, "budget": {"monthly_usd": 50}}
    notes, events, killed = [], [], {"on": False}
    o = O.Ops(tmp_path / "ops.db", notify=lambda t, u: notes.append((t, u)),
              publish=lambda kind, **d: events.append((kind, d)), settings=lambda: settings,
              killswitch_engaged=lambda: killed["on"])
    return o, clock, settings, notes, events, killed, tmp_path


def run_all(o, limit=50):
    n = 0
    while n < limit and o.run_once():
        n += 1
    return n


def test_job_runs_and_records_result(env):
    o, *_ = env
    o.register(O.JobKind("add", lambda c: {"sum": c.payload["a"] + c.payload["b"]}))
    j = o.enqueue("add", {"a": 2, "b": 3})
    assert run_all(o) == 1
    assert o.job(j["id"])["status"] == "done" and o.job(j["id"])["result"] == {"sum": 5}


def test_idempotency_key_prevents_duplicates(env):
    o, *_ = env
    ran = []
    o.register(O.JobKind("send", lambda c: ran.append(1)))
    a = o.enqueue("send", {}, idem="invoice-42")
    b = o.enqueue("send", {}, idem="invoice-42")
    assert b["duplicate"] and a["id"] == b["id"]
    run_all(o)
    assert ran == [1]


def test_transient_failure_retries_with_backoff_then_succeeds(env):
    o, clock, *_ = env
    tries = []

    def flaky(c):
        tries.append(c.attempt)
        if len(tries) < 3:
            raise ConnectionError("network blip")
        return "ok"
    o.register(O.JobKind("flaky", flaky, max_attempts=5))
    j = o.enqueue("flaky")
    run_all(o)
    assert o.job(j["id"])["status"] == "retrying"
    assert run_all(o) == 0, "not due yet: backing off"
    clock.t += 40
    run_all(o)
    clock.t += 80
    run_all(o)
    assert o.job(j["id"])["status"] == "done" and tries == [1, 2, 3]


def test_gives_up_after_max_attempts_and_tells_owner(env):
    o, clock, _, notes, *_ = env
    o.register(O.JobKind("broken", lambda c: 1 / 0, max_attempts=3))
    j = o.enqueue("broken")
    for _ in range(4):
        run_all(o)
        clock.t += 4000
    assert o.job(j["id"])["status"] == "failed" and "gave up after 3" in o.job(j["id"])["error"]
    assert any("failed" in t for t, _ in notes)


def test_permanent_errors_are_not_retried(env):
    o, *_ = env

    def bad(c):
        raise O.Permanent("the client has no email address")
    o.register(O.JobKind("bad", bad))
    j = o.enqueue("bad")
    run_all(o)
    assert o.job(j["id"])["status"] == "failed" and o.job(j["id"])["attempts"] == 1


def test_crash_recovery_and_checkpoints(env):
    o, clock, settings, notes, events, killed, tmp = env
    seen = []

    def long_job(c):
        seen.append(dict(c.checkpoint_data))
        if not c.checkpoint_data:
            c.checkpoint(step=1)
            raise SystemExit("power cut")   # simulates the process dying mid-job
        return c.checkpoint_data["step"]
    o.register(O.JobKind("long", long_job))
    j = o.enqueue("long")
    with pytest.raises(SystemExit):
        o.run_once()
    assert o.job(j["id"])["status"] == "running"
    o._db.execute("UPDATE jobs SET owner_pid=999999999 WHERE id=?", (j["id"],))   # that program is gone
    o2 = O.Ops(tmp / "ops.db", settings=lambda: settings)      # Fafnir starts again
    o2.register(O.JobKind("long", long_job))
    assert o2.job(j["id"])["status"] == "queued"
    run_all(o2)
    assert o2.job(j["id"])["status"] == "done" and seen[-1] == {"step": 1}


def test_approval_parks_the_job_then_resumes(env):
    o, _, _, notes, events, *_ = env
    steps = []

    def outreach(c):
        steps.append("prepared")
        c.require_approval("bulk_outreach", "Send 20 emails to Port of Spain bakeries", "draft attached")
        steps.append("sent")
        return "sent"
    o.register(O.JobKind("outreach", outreach))
    j = o.enqueue("outreach")
    run_all(o)
    assert o.job(j["id"])["status"] == "waiting_approval" and steps == ["prepared"]
    pending = o.approvals()
    assert len(pending) == 1 and pending[0]["title"].startswith("Send 20 emails")
    assert any("Needs your OK" in t for t, _ in notes)
    assert run_all(o) == 0, "nothing runs while waiting"
    o.decide(pending[0]["id"], True)
    run_all(o)
    assert o.job(j["id"])["status"] == "done" and steps == ["prepared", "prepared", "sent"]


def test_rejection_cancels_the_job(env):
    o, *_ = env

    def pay(c):
        c.require_approval("spend", "Buy a domain for US$12")
        return "bought"
    o.register(O.JobKind("pay", pay))
    j = o.enqueue("pay")
    run_all(o)
    o.decide(o.approvals()[0]["id"], False)
    run_all(o)
    assert o.job(j["id"])["status"] == "cancelled"


def test_always_approve_actions_ask_even_at_level_4(env):
    o, _, settings, *_ = env
    settings["autonomy"]["level"] = 4

    def act(c):
        c.require_approval("reschedule_post", "Move a draft post to Friday")    # low-risk: allowed at level 4
        c.require_approval("move_funds", "Transfer US$100")                    # never automatic
        return "done"
    o.register(O.JobKind("act", act, level=4))
    j = o.enqueue("act")
    run_all(o)
    assert o.job(j["id"])["status"] == "waiting_approval"
    assert [a["kind"] for a in o.approvals()] == ["move_funds"]


def test_autonomy_level_blocks_queueing(env):
    o, _, settings, *_ = env
    o.register(O.JobKind("launch_site", lambda c: 1, level=3))
    with pytest.raises(O.Refused, match="level 3"):
        o.enqueue("launch_site")
    settings["autonomy"]["level"] = 3
    assert o.enqueue("launch_site")["status"] == "queued"


def test_budget_refuses_before_the_call(env):
    o, _, _, notes, *_ = env
    calls = []

    def paid(c):
        c.spend("openai", 2.0)            # checked BEFORE the request
        calls.append(1)
        c.spent("openai", 2.0)
        return "ok"
    o.register(O.JobKind("paid", paid, role="sales"))
    o.record_spend("anthropic", 49.0, role="research")
    j = o.enqueue("paid")
    run_all(o)
    assert calls == [] and o.job(j["id"])["status"] == "failed" and "budget" in o.job(j["id"])["error"].lower()
    assert any("80%" in t for t, _ in notes)


def test_subscription_and_local_use_do_not_count(env):
    o, *_ = env
    o.record_spend("claude_code", 3.0, billable=False)
    o.record_spend("ollama (local)", 0.0, billable=False)
    o.record_spend("api", 1.25)
    b = o.budget()
    assert b["spent_usd"] == 1.25 and b["by_provider"] == {"api": 1.25}


def test_role_budget_suspends_the_role(env):
    o, *_ = env
    o.set_role_budget("marketing", 1.0)
    o.register(O.JobKind("ad", lambda c: (c.spend("higgsfield", 2.0), "ok")[1], role="marketing"))
    o.enqueue("ad")
    run_all(o)
    assert o.role_suspended("marketing")
    with pytest.raises(O.Refused, match="suspended"):
        o.enqueue("ad")
    o.resume_role("marketing")
    assert not o.role_suspended("marketing")


def test_repeated_failures_suspend_the_role(env):
    o, clock, _, notes, *_ = env

    def bad(c):
        raise O.Permanent("nope")
    o.register(O.JobKind("scrape", bad, role="research"))
    for i in range(3):
        o.enqueue("scrape", idem=f"s{i}")
        run_all(o)
    assert o.role_suspended("research") and any("paused" in t for t, _ in notes)


def test_kill_switch_pauses_workers(env):
    o, _, _, _, _, killed, _ = env
    ran = []
    o.register(O.JobKind("x", lambda c: ran.append(1)))
    o.enqueue("x")
    killed["on"] = True
    assert run_all(o) == 0 and ran == []
    killed["on"] = False
    assert run_all(o) == 1 and ran == [1]


def test_schedule_fires_once_per_slot(env):
    o, clock, *_ = env
    ran = []
    o.register(O.JobKind("tick", lambda c: ran.append(c.id)))
    o.schedule("every-10", "tick", every_minutes=10)
    assert run_all(o) == 0
    clock.t += 601
    run_all(o)
    run_all(o)
    assert len(ran) == 1
    clock.t += 600
    run_all(o)
    assert len(ran) == 2


def test_cancel_and_expired_approvals(env):
    o, clock, *_ = env
    o.register(O.JobKind("later", lambda c: 1))
    j = o.enqueue("later", delay=3600)
    assert o.cancel(j["id"]) and o.job(j["id"])["status"] == "cancelled"
    o.register(O.JobKind("ask", lambda c: c.require_approval("publish", "Post the reel")))
    k = o.enqueue("ask")
    run_all(o)
    clock.t += 8 * 86400
    assert o.approvals() == [] and o.job(k["id"])["status"] == "cancelled"


def test_daily_report_costs_nothing(env):
    o, clock, _, notes, events, *_ = env
    O.register_builtin(o)
    clock.t = O.Ops._next_slot(None, "08:00", clock.t) + 1
    run_all(o)
    assert any(k == "report" for k, _ in events)
    assert o.budget()["spent_usd"] == 0


def test_a_second_program_does_not_steal_running_jobs(env):
    o, clock, settings, notes, events, killed, tmp = env
    o.register(O.JobKind("slow", lambda c: 1))
    j = o.enqueue("slow")
    o._claim()                                                   # this program is running it
    other = O.Ops(tmp / "ops.db", settings=lambda: settings)    # a command or check opens the database
    assert other.job(j["id"])["status"] == "running", "left alone: its program is alive"
    o._db.execute("UPDATE jobs SET owner_pid=999999999 WHERE id=?", (j["id"],))
    third = O.Ops(tmp / "ops.db", settings=lambda: settings)
    assert third.job(j["id"])["status"] == "queued", "recovered once its program has gone"
