"""Operations core: the approval queue, persistent jobs, schedules, the AI budget and autonomy levels.

Everything the future agent workforce does goes through here, so it is safe by construction:

  Jobs        A job is a named piece of work with a JSON payload, stored in data/ops.db. Workers pick
              jobs up only when one is due (they sleep on a condition, not a polling loop that calls
              models). Each job may carry an idempotency key: the same key is never queued twice, so a
              retried trigger can't send an email or create an invoice twice. A running job holds a
              lease; if Fafnir crashes, the job is put back in the queue at the next start. Failures
              retry with exponential backoff (30 s, 60 s, 120 s ... up to 1 h) until max_attempts, then
              the job is marked failed and the owner is told. Long jobs save checkpoints and resume.
  Approvals   A job that reaches a sensitive step calls ctx.require_approval(); it parks in
              "waiting_approval" (nothing is held in memory) and continues only after the owner says yes
              in the app, on the desktop or from the phone. A no cancels it. Unanswered requests expire.
  Budget      A monthly AI budget (default US$50). ctx.spend() is called BEFORE a paid call with an
              estimate; if it would exceed the month's cap or the role's own cap, the call is refused.
              Actual costs are recorded per provider, role, client and project.
  Autonomy    Level 1 research, 2 assisted, 3 supervised, 4 controlled full operations. Each job kind
              declares the level it needs; below that level it can't be queued. Some actions ALWAYS need
              approval whatever the level (see ALWAYS_APPROVE).
  Safety      The kill switch pauses every worker. A role whose jobs fail 3 times in an hour, or that
              hits its budget, is suspended automatically until the owner resumes it.
"""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

LEVELS = {1: "Research: agents research and recommend", 2: "Assisted: agents prepare work and ask before acting",
          3: "Supervised: agents run approved workflows within limits", 4: "Controlled full operations: low-risk, reversible actions run on their own"}
# Actions that need the owner's yes at EVERY autonomy level.
ALWAYS_APPROVE = {"spend", "paid_campaign", "bulk_outreach", "contract", "move_funds", "change_financial_account",
                  "publish", "deploy", "delete_data", "legal"}
LEASE_SECONDS = 15 * 60
BACKOFF_BASE, BACKOFF_CAP = 30, 3600
SUSPEND_AFTER_FAILURES, SUSPEND_WINDOW = 3, 3600
APPROVAL_DAYS = 7


class WaitingApproval(Exception):
    """Raised inside a job to park it until the owner answers."""


class Refused(Exception):
    """Budget, autonomy or suspension said no. Not retried."""


class Permanent(Exception):
    """A failure retrying won't fix (bad input, missing account). Not retried."""


@dataclass
class JobKind:
    name: str
    handler: Callable
    role: str = "system"
    level: int = 1                 # autonomy level needed to queue it
    max_attempts: int = 5
    description: str = ""


def _now() -> float:
    return time.time()


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t).isoformat(timespec="seconds") if t else None


class Ops:
    def __init__(self, path: Path, notify: Callable[[str, str], None] | None = None,
                 publish: Callable[..., None] | None = None, settings: Callable[[], dict] | None = None,
                 killswitch_engaged: Callable[[], bool] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.notify = notify or (lambda title, urgency: None)
        self.publish = publish or (lambda kind, **d: None)
        self.settings = settings or (lambda: {})
        self.killed = killswitch_engaged or (lambda: False)
        self.kinds: dict[str, JobKind] = {}
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, role TEXT, payload TEXT,
                status TEXT NOT NULL, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 5, next_run REAL,
                idem TEXT UNIQUE, checkpoint TEXT, result TEXT, error TEXT, approval_id INTEGER, client TEXT,
                project TEXT, lease_until REAL, created REAL, updated REAL);
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, next_run);
            CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY, kind TEXT, title TEXT, detail TEXT,
                requested_by TEXT, status TEXT NOT NULL, decided_by TEXT, decided_at REAL, payload TEXT,
                job_id INTEGER, created REAL, expires REAL);
            CREATE TABLE IF NOT EXISTS schedules (name TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT,
                every_minutes REAL, daily_at TEXT, next_run REAL, enabled INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS spend (id INTEGER PRIMARY KEY, time REAL, month TEXT, provider TEXT, role TEXT,
                client TEXT, project TEXT, usd REAL, billable INTEGER, note TEXT);
            CREATE INDEX IF NOT EXISTS spend_month ON spend(month);
            CREATE TABLE IF NOT EXISTS roles (name TEXT PRIMARY KEY, suspended INTEGER DEFAULT 0, why TEXT,
                monthly_usd REAL);
        """)
        self._recover()

    # ---- settings ----------------------------------------------------------------------------
    @property
    def level(self) -> int:
        try:
            return max(1, min(4, int(self.settings().get("autonomy", {}).get("level", 1))))
        except (TypeError, ValueError):
            return 1

    @property
    def monthly_cap(self) -> float:
        try:
            return float(self.settings().get("budget", {}).get("monthly_usd", 50))
        except (TypeError, ValueError):
            return 50.0

    # ---- start-up recovery ----------------------------------------------------------------------
    def _recover(self) -> None:
        """Jobs left 'running' when Fafnir stopped (crash, restart) go back in the queue. This runs once,
        before any worker starts, so nothing 'running' can belong to this process."""
        with self._lock:
            n = self._db.execute("UPDATE jobs SET status='queued', lease_until=NULL, next_run=?, updated=? "
                                 "WHERE status='running'", (_now(), _now())).rowcount
        if n:
            print(f"[ops] {n} interrupted job(s) put back in the queue", flush=True)

    # ---- job kinds --------------------------------------------------------------------------------
    def register(self, kind: JobKind) -> None:
        self.kinds[kind.name] = kind

    # ---- queueing -----------------------------------------------------------------------------------
    def enqueue(self, kind: str, payload: dict | None = None, idem: str | None = None, delay: float = 0,
                client: str = "", project: str = "") -> dict:
        k = self.kinds.get(kind)
        if k is None:
            raise Refused(f"Unknown job kind '{kind}'.")
        if k.level > self.level:
            raise Refused(f"'{kind}' needs autonomy level {k.level}; the current level is {self.level}.")
        if self.role_suspended(k.role):
            raise Refused(f"The {k.role} role is suspended.")
        with self._lock:
            if idem:
                row = self._db.execute("SELECT id, status FROM jobs WHERE idem=?", (idem,)).fetchone()
                if row:
                    return {"id": row["id"], "status": row["status"], "duplicate": True}
            cur = self._db.execute(
                "INSERT INTO jobs(kind, role, payload, status, max_attempts, next_run, idem, client, project, created, updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (kind, k.role, json.dumps(payload or {}), "queued", k.max_attempts,
                                                 _now() + delay, idem, client, project, _now(), _now()))
            self._wake.notify_all()
        self.publish("job", id=cur.lastrowid, job_kind=kind, status="queued")
        return {"id": cur.lastrowid, "status": "queued", "duplicate": False}

    def cancel(self, job_id: int, who: str = "owner") -> bool:
        with self._lock:
            n = self._db.execute("UPDATE jobs SET status='cancelled', error=?, updated=? WHERE id=? AND status IN "
                                 "('queued','waiting_approval','retrying')", (f"cancelled by {who}", _now(), job_id)).rowcount
            self._db.execute("UPDATE approvals SET status='cancelled' WHERE job_id=? AND status='pending'", (job_id,))
        if n:
            self.publish("job", id=job_id, status="cancelled")
        return bool(n)

    def jobs(self, status: str | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT * FROM jobs" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT ?"
        rows = self._db.execute(q, ((status, limit) if status else (limit,))).fetchall()
        return [self._job_dict(r) for r in rows]

    def job(self, job_id: int) -> dict | None:
        r = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_dict(r) if r else None

    @staticmethod
    def _job_dict(r) -> dict:
        d = dict(r)
        for k in ("payload", "checkpoint", "result"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        for k in ("next_run", "created", "updated", "lease_until"):
            d[k] = _iso(d[k])
        return d

    # ---- approvals ----------------------------------------------------------------------------------
    def request_approval(self, kind: str, title: str, detail: str = "", by: str = "", payload: dict | None = None,
                         job_id: int | None = None, days: float = APPROVAL_DAYS) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO approvals(kind, title, detail, requested_by, status, payload, job_id, created, expires) "
                                   "VALUES(?,?,?,?,?,?,?,?,?)", (kind, title, detail, by, "pending", json.dumps(payload or {}),
                                                                job_id, _now(), _now() + days * 86400))
        aid = cur.lastrowid
        self.notify(f"Needs your OK: {title}", "important")
        self.publish("approval_new", id=aid, action=kind, title=title, by=by)
        return aid

    def approvals(self, status: str = "pending") -> list[dict]:
        self._expire()
        rows = self._db.execute("SELECT * FROM approvals WHERE status=? ORDER BY id DESC LIMIT 100", (status,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            for k in ("created", "expires", "decided_at"):
                d[k] = _iso(d[k])
            out.append(d)
        return out

    def decide(self, approval_id: int, approve: bool, who: str = "owner") -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise Refused("That request isn't waiting for an answer.")
            status = "approved" if approve else "rejected"
            self._db.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                             (status, who, _now(), approval_id))
            if row["job_id"]:
                if approve:   # the job picks up where it stopped
                    self._db.execute("UPDATE jobs SET status='queued', next_run=?, updated=? WHERE id=? AND status='waiting_approval'",
                                     (_now(), _now(), row["job_id"]))
                else:
                    self._db.execute("UPDATE jobs SET status='cancelled', error=?, updated=? WHERE id=?",
                                     (f"rejected by {who}", _now(), row["job_id"]))
            self._wake.notify_all()
        self.publish("approval_decided", id=approval_id, approved=approve)
        return {"id": approval_id, "status": status}

    def _expire(self) -> None:
        with self._lock:
            for r in self._db.execute("SELECT id, job_id FROM approvals WHERE status='pending' AND expires<?", (_now(),)).fetchall():
                self._db.execute("UPDATE approvals SET status='expired' WHERE id=?", (r["id"],))
                if r["job_id"]:
                    self._db.execute("UPDATE jobs SET status='cancelled', error='approval request expired', updated=? WHERE id=?",
                                     (_now(), r["job_id"]))

    # ---- budget -------------------------------------------------------------------------------------
    @staticmethod
    def _month(t: float | None = None) -> str:
        return datetime.fromtimestamp(t or _now()).strftime("%Y-%m")

    def spent(self, role: str | None = None, month: str | None = None) -> float:
        q, a = "SELECT COALESCE(SUM(usd),0) FROM spend WHERE billable=1 AND month=?", [month or self._month()]
        if role:
            q, a = q + " AND role=?", a + [role]
        return float(self._db.execute(q, a).fetchone()[0])

    def check_budget(self, role: str, estimate_usd: float) -> None:
        """Call BEFORE a paid request. Raises Refused if it would go over a cap."""
        if estimate_usd <= 0:
            return
        if self.spent() + estimate_usd > self.monthly_cap:
            raise Refused(f"Monthly AI budget reached (${self.spent():.2f} of ${self.monthly_cap:.2f}).")
        row = self._db.execute("SELECT monthly_usd FROM roles WHERE name=?", (role,)).fetchone()
        if row and row["monthly_usd"] is not None and self.spent(role) + estimate_usd > row["monthly_usd"]:
            self.suspend(role, f"reached its own budget of ${row['monthly_usd']:.2f}")
            raise Refused(f"The {role} role reached its budget of ${row['monthly_usd']:.2f}.")

    def record_spend(self, provider: str, usd: float, role: str = "assistant", client: str = "", project: str = "",
                     billable: bool = True, note: str = "") -> None:
        if usd <= 0 and billable:
            return
        with self._lock:
            self._db.execute("INSERT INTO spend(time, month, provider, role, client, project, usd, billable, note) "
                             "VALUES(?,?,?,?,?,?,?,?,?)", (_now(), self._month(), provider, role, client, project,
                                                           round(usd, 6), 1 if billable else 0, note[:200]))
        if billable:
            used = self.spent()
            for mark in (0.8, 1.0):
                before = used - usd
                if before < self.monthly_cap * mark <= used:
                    self.notify(f"AI spending is at {int(mark * 100)}% of this month's ${self.monthly_cap:.0f} budget.",
                                "urgent" if mark >= 1 else "important")

    def budget(self) -> dict:
        m = self._month()
        by = lambda col: {r[0] or "-": round(r[1], 4) for r in self._db.execute(   # noqa: E731
            f"SELECT {col}, SUM(usd) FROM spend WHERE month=? AND billable=1 GROUP BY {col}", (m,))}
        return {"month": m, "cap_usd": self.monthly_cap, "spent_usd": round(self.spent(), 4),
                "left_usd": round(max(0.0, self.monthly_cap - self.spent()), 4),
                "by_provider": by("provider"), "by_role": by("role"), "by_client": by("client"), "by_project": by("project"),
                "note": "Only pay-per-use services count (Anthropic API, OpenAI, Gemini, Higgsfield...). Your Claude "
                        "subscription and the local models cost nothing extra and aren't counted."}

    # ---- roles ----------------------------------------------------------------------------------------
    def role_suspended(self, role: str) -> bool:
        r = self._db.execute("SELECT suspended FROM roles WHERE name=?", (role,)).fetchone()
        return bool(r and r["suspended"])

    def suspend(self, role: str, why: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO roles(name, suspended, why) VALUES(?,1,?) ON CONFLICT(name) DO UPDATE SET "
                             "suspended=1, why=excluded.why", (role, why))
        self.notify(f"The {role} role was paused: {why}. Resume it when you've looked.", "important")
        self.publish("role", name=role, suspended=True, why=why)

    def resume_role(self, role: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO roles(name, suspended, why) VALUES(?,0,'') ON CONFLICT(name) DO UPDATE SET "
                             "suspended=0, why=''", (role,))
            self._wake.notify_all()
        self.publish("role", name=role, suspended=False)

    def set_role_budget(self, role: str, usd: float | None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO roles(name, monthly_usd) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET "
                             "monthly_usd=excluded.monthly_usd", (role, usd))

    def roles(self) -> list[dict]:
        names = sorted({k.role for k in self.kinds.values()} |
                       {r[0] for r in self._db.execute("SELECT name FROM roles")})
        out = []
        for n in names:
            r = self._db.execute("SELECT suspended, why, monthly_usd FROM roles WHERE name=?", (n,)).fetchone()
            out.append({"role": n, "suspended": bool(r and r["suspended"]), "why": (r["why"] if r else "") or "",
                        "monthly_usd": r["monthly_usd"] if r else None, "spent_usd": round(self.spent(n), 4),
                        "queued": self._db.execute("SELECT COUNT(*) FROM jobs WHERE role=? AND status IN ('queued','retrying','running')", (n,)).fetchone()[0]})
        return out

    # ---- schedules ------------------------------------------------------------------------------------
    def schedule(self, name: str, kind: str, payload: dict | None = None, every_minutes: float | None = None,
                 daily_at: str | None = None) -> None:
        with self._lock:
            existing = self._db.execute("SELECT next_run FROM schedules WHERE name=?", (name,)).fetchone()
            nxt = existing["next_run"] if existing else self._next_slot(every_minutes, daily_at, _now())
            self._db.execute("INSERT INTO schedules(name, kind, payload, every_minutes, daily_at, next_run, enabled) "
                             "VALUES(?,?,?,?,?,?,1) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, payload=excluded.payload, "
                             "every_minutes=excluded.every_minutes, daily_at=excluded.daily_at",
                             (name, kind, json.dumps(payload or {}), every_minutes, daily_at, nxt))
            self._wake.notify_all()

    @staticmethod
    def _next_slot(every: float | None, daily_at: str | None, after: float) -> float:
        if daily_at:
            h, m = (int(x) for x in daily_at.split(":"))
            d = datetime.fromtimestamp(after).replace(hour=h, minute=m, second=0, microsecond=0)
            if d.timestamp() <= after:
                d += timedelta(days=1)
            return d.timestamp()
        return after + float(every or 60) * 60

    def schedules(self) -> list[dict]:
        return [{**dict(r), "next_run": _iso(r["next_run"]), "payload": json.loads(r["payload"] or "{}")}
                for r in self._db.execute("SELECT * FROM schedules ORDER BY name")]

    def _fire_schedules(self) -> None:
        now = _now()
        for s in self._db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (now,)).fetchall():
            slot = s["next_run"]
            try:   # the slot time is part of the key, so a schedule fires once per slot even after a crash
                self.enqueue(s["kind"], json.loads(s["payload"] or "{}"), idem=f"schedule:{s['name']}:{int(slot)}")
            except Refused as e:
                print(f"[ops] schedule {s['name']} skipped: {e}", flush=True)
            nxt = self._next_slot(s["every_minutes"], s["daily_at"], max(now, slot))
            with self._lock:
                self._db.execute("UPDATE schedules SET next_run=? WHERE name=?", (nxt, s["name"]))

    # ---- running ----------------------------------------------------------------------------------------
    def _claim(self) -> sqlite3.Row | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE status IN ('queued','retrying') AND next_run<=? "
                                   "ORDER BY next_run, id LIMIT 1", (_now(),)).fetchone()
            if not row:
                return None
            got = self._db.execute("UPDATE jobs SET status='running', lease_until=?, attempts=attempts+1, updated=? "
                                   "WHERE id=? AND status IN ('queued','retrying')",
                                   (_now() + LEASE_SECONDS, _now(), row["id"])).rowcount
            return self._db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone() if got else None

    def _seconds_until_next(self) -> float:
        r1 = self._db.execute("SELECT MIN(next_run) FROM jobs WHERE status IN ('queued','retrying')").fetchone()[0]
        r2 = self._db.execute("SELECT MIN(next_run) FROM schedules WHERE enabled=1").fetchone()[0]
        nxt = min([t for t in (r1, r2) if t is not None], default=_now() + 300)
        return max(0.05, min(300.0, nxt - _now()))

    def run_once(self) -> bool:
        """Run one due job if there is one. Returns True if something ran."""
        if self.killed():
            return False
        self._fire_schedules()
        row = self._claim()
        if row is None:
            return False
        kind = self.kinds.get(row["kind"])
        ctx = JobContext(self, row)
        try:
            if kind is None:
                raise Permanent(f"No handler for '{row['kind']}' (the feature may have been removed).")
            if self.role_suspended(kind.role):
                raise WaitingApproval()   # parked; resumes when the role is resumed
            result = kind.handler(ctx)
            self._finish(row["id"], "done", result=result)
        except WaitingApproval:
            with self._lock:
                status = "waiting_approval" if ctx.approval_id else "queued"
                self._db.execute("UPDATE jobs SET status=?, approval_id=?, attempts=attempts-1, lease_until=NULL, next_run=?, "
                                 "updated=? WHERE id=?", (status, ctx.approval_id, _now() + (0 if ctx.approval_id else 300),
                                                          _now(), row["id"]))
            self.publish("job", id=row["id"], status=status)
        except (Refused, Permanent) as e:
            self._finish(row["id"], "failed", error=str(e))
            self._count_failure(kind.role if kind else "system", row, str(e))
        except Exception as e:  # transient: retry with backoff
            err = f"{type(e).__name__}: {e}"
            if row["attempts"] >= row["max_attempts"]:
                self._finish(row["id"], "failed", error=err + f" (gave up after {row['attempts']} tries)")
                self._count_failure(kind.role if kind else "system", row, err)
            else:
                delay = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (row["attempts"] - 1)) * random.uniform(0.8, 1.2)
                with self._lock:
                    self._db.execute("UPDATE jobs SET status='retrying', error=?, next_run=?, lease_until=NULL, updated=? "
                                     "WHERE id=?", (err[:500], _now() + delay, _now(), row["id"]))
                self.publish("job", id=row["id"], status="retrying", in_seconds=round(delay))
                print(f"[ops] job {row['id']} ({row['kind']}) failed, retrying in {delay:.0f}s: {err[:120]}", flush=True)
        return True

    def _finish(self, job_id: int, status: str, result=None, error: str = "") -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET status=?, result=?, error=?, lease_until=NULL, updated=? WHERE id=?",
                             (status, json.dumps(result, default=str) if result is not None else None, error[:500] or None,
                              _now(), job_id))
        self.publish("job", id=job_id, status=status, error=error[:200] or None)

    def _count_failure(self, role: str, row, err: str) -> None:
        self.notify(f"A {row['kind']} job failed: {err[:120]}", "important")
        n = self._db.execute("SELECT COUNT(*) FROM jobs WHERE role=? AND status='failed' AND updated>?",
                             (role, _now() - SUSPEND_WINDOW)).fetchone()[0]
        if n >= SUSPEND_AFTER_FAILURES and role != "system" and not self.role_suspended(role):
            self.suspend(role, f"{n} jobs failed within an hour")

    # ---- worker threads -----------------------------------------------------------------------------------
    def start(self, workers: int = 2) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    if self.run_once():
                        continue
                except Exception:
                    traceback.print_exc()
                with self._lock:
                    self._wake.wait(timeout=5 if self.killed() else self._seconds_until_next())
        for i in range(workers):
            threading.Thread(target=loop, daemon=True, name=f"ops-worker-{i}").start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._wake.notify_all()

    def summary(self) -> dict:
        counts = {r[0]: r[1] for r in self._db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}
        return {"autonomy_level": self.level, "autonomy": LEVELS[self.level], "jobs": counts,
                "pending_approvals": len(self.approvals()), "budget": {k: v for k, v in self.budget().items()
                                                                       if k in ("cap_usd", "spent_usd", "left_usd")},
                "suspended_roles": [r["role"] for r in self.roles() if r["suspended"]]}


class JobContext:
    """What a job handler gets: its payload, checkpointing, approvals and the budget."""

    def __init__(self, ops: Ops, row):
        self.ops, self.id, self.kind = ops, row["id"], row["kind"]
        self.payload = json.loads(row["payload"] or "{}")
        self.checkpoint_data = json.loads(row["checkpoint"]) if row["checkpoint"] else {}
        self.attempt, self.role = row["attempts"], row["role"]
        self.client, self.project = row["client"] or "", row["project"] or ""
        self._approval_row = row["approval_id"]
        self.approval_id: int | None = None

    def checkpoint(self, **data) -> None:
        """Save progress; a retried or resumed job starts with ctx.checkpoint_data."""
        self.checkpoint_data.update(data)
        with self.ops._lock:
            self.ops._db.execute("UPDATE jobs SET checkpoint=?, lease_until=?, updated=? WHERE id=?",
                                 (json.dumps(self.checkpoint_data, default=str), _now() + LEASE_SECONDS, _now(), self.id))

    def require_approval(self, action: str, title: str, detail: str = "", payload: dict | None = None) -> None:
        """Stop here until the owner approves. `action` from ALWAYS_APPROVE always asks; anything else asks
        below autonomy level 4. Returns normally once approved; raises WaitingApproval while waiting."""
        if action not in ALWAYS_APPROVE and self.ops.level >= 4:
            return
        if self._approval_row:
            r = self.ops._db.execute("SELECT status FROM approvals WHERE id=?", (self._approval_row,)).fetchone()
            if r and r["status"] == "approved":
                return
            if r and r["status"] in ("rejected", "expired", "cancelled"):
                raise Permanent(f"Not approved ({r['status']}).")
        self.approval_id = self.ops.request_approval(action, title, detail, by=self.role, payload=payload, job_id=self.id)
        raise WaitingApproval()

    def spend(self, provider: str, estimate_usd: float) -> None:
        """Check the budget before a paid call; record the actual cost with ctx.spent()."""
        self.ops.check_budget(self.role, estimate_usd)

    def spent(self, provider: str, usd: float, note: str = "") -> None:
        self.ops.record_spend(provider, usd, role=self.role, client=self.client, project=self.project, note=note)


# ---- built-in job: the daily report (no AI call, costs nothing) ------------------------------------
def daily_report(ctx: JobContext) -> dict:
    ops = ctx.ops
    since = _now() - 86400
    done = ops._db.execute("SELECT kind, COUNT(*) FROM jobs WHERE status='done' AND updated>? GROUP BY kind", (since,)).fetchall()
    failed = ops._db.execute("SELECT COUNT(*) FROM jobs WHERE status='failed' AND updated>?", (since,)).fetchone()[0]
    day_spend = ops._db.execute("SELECT COALESCE(SUM(usd),0) FROM spend WHERE billable=1 AND time>?", (since,)).fetchone()[0]
    b = ops.budget()
    lines = [f"Jobs done: {sum(c for _, c in done)}" + (f" ({', '.join(f'{k} {c}' for k, c in done)})" if done else ""),
             f"Jobs failed: {failed}", f"Waiting for your OK: {len(ops.approvals())}",
             f"AI spend in the last day: ${day_spend:.2f}; this month ${b['spent_usd']:.2f} of ${b['cap_usd']:.2f}",
             f"Autonomy level: {ops.level} ({LEVELS[ops.level].split(':')[0]})"]
    suspended = [r["role"] for r in ops.roles() if r["suspended"]]
    if suspended:
        lines.append("Paused roles: " + ", ".join(suspended))
    text = "\n".join(lines)
    ops.publish("report", title="Daily report", text=text)
    ops.notify("Daily report: " + "; ".join(lines[:4]), "info")
    return {"report": text}


def register_builtin(ops: Ops) -> None:
    ops.register(JobKind("daily_report", daily_report, role="system", level=1, max_attempts=3,
                         description="A summary of yesterday's work, approvals and spending (no AI, no cost)."))
    ops.schedule("daily-report", "daily_report", daily_at="08:00")
