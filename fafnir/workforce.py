"""The agent workforce: fourteen roles, each a desk with a worker behind it, coordinated through the job
system (fafnir/ops.py) instead of agents chatting with each other.

A role is: a department, who does the work (a StarNet crew agent, Fafnir's local Qwen, or a service
that isn't connected yet), the autonomy level its tasks need, whether its output gets a QA review, and
an optional monthly budget. Work reaches a role as a job ("task:<role>") with an instruction; the
result is stored on the job, checked by QA where it matters, and filed as a notice.

The orchestrator ("goal" jobs) takes a goal from the owner, splits it into at most six role tasks with
the local model, queues them, waits for them (without polling a model), and writes an executive
summary. It only ever assigns roles allowed at the current autonomy level.

Safety comes from the layers underneath: StarNet runs are non-interactive, so anything a crew agent
would need consent for is refused inside the run; nothing here can send, post, pay or delete; the
budget is checked before paid work; the kill switch pauses everything; a role that keeps failing is
paused automatically. What any agent writes is data, never instructions.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .ops import Defer, JobContext, JobKind, Ops, Permanent, Refused

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STARNET_HOME = Path(os.environ.get("LOCALAPPDATA", "")) / "StarNet"
OLLAMA = "http://127.0.0.1:11434"
TASK_SECONDS = 600
MAX_GOAL_TASKS = 6
FREE_PROVIDERS = {"ollama", "local", "lmstudio", "claude_code", "subscription", ""}


class StarNetTimeout(RuntimeError):
    """The crew agent ran out of time without an answer (common on a small local model doing web research)."""


@dataclass
class Role:
    id: str
    name: str
    department: str
    does: str
    worker: str                   # "starnet:<agentId>", "local", or "integration:<id>"
    level: int = 1                # autonomy level its tasks need
    qa: bool = False              # output is reviewed by QA before it's reported
    needs: tuple[str, ...] = ()   # integrations that must be on
    later: str = ""               # what is still missing, shown to the owner
    examples: list[str] = field(default_factory=list)


ROLES: list[Role] = [
    Role("orchestrator", "CEO / Orchestrator", "Leadership", "Splits goals into tasks, assigns roles, writes executive summaries",
         "local", 1),
    Role("research", "Research", "Research", "Market and opportunity research with sources", "starnet:researcher", 1, qa=True,
         examples=["Which small businesses in Port of Spain are most likely to pay for a new website?"]),
    Role("lead_scoring", "Lead Scoring", "Sales", "Scores leads on fit, need, budget and relevance, explaining each score",
         "local", 1, later="Needs the lead database (stage 7)."),
    Role("outreach", "Outreach", "Sales", "Drafts personalised messages (never sends at this stage)", "starnet:copywriter", 2, qa=True,
         later="Sending needs a business email domain, a suppression list and your approval per campaign (stage 7)."),
    Role("sales", "Sales", "Sales", "Qualifies prospects, drafts quotes and replies", "starnet:envoy", 2, qa=True),
    Role("development", "Development", "Delivery", "Builds websites, apps and automations; prepares releases for approval",
         "local", 3, later="Code work through Claude Code with tests and an approval before anything is deployed."),
    Role("marketing", "Marketing", "Marketing", "Campaign plans, ad copy, content calendars, performance analysis",
         "starnet:marketer", 1, qa=True),
    Role("creative_director", "Creative Director", "Creative", "Ad concepts, video scripts, storyboards", "starnet:writer", 1, qa=True),
    Role("image", "Image Generation", "Creative", "Advert images, banners, social graphics", "integration:higgsfield", 2,
         needs=("higgsfield",), later="Turn on Higgsfield (Settings > Integrations) once you have a key."),
    Role("video", "Video Generation", "Creative", "Scenes and commercial clips", "integration:higgsfield", 2,
         needs=("higgsfield",), later="Turn on Higgsfield once you have a key."),
    Role("video_editor", "Video Editor", "Creative", "Cuts, captions, transitions and branding with FFmpeg",
         "integration:ffmpeg", 2, later="The editing pipeline is stage 8 (FFmpeg is already installed)."),
    Role("support", "Customer Support", "Service", "Answers routine customer questions from approved knowledge",
         "starnet:envoy", 2, qa=True, later="Needs customers and an approved knowledge base."),
    Role("finance", "Finance", "Finance", "Revenue, expenses, invoices and reports from real records; never moves money",
         "local", 1, later="Real revenue needs a payment provider (Stripe); until then it reports AI spending only."),
    Role("qa", "Quality Assurance", "Quality", "Reviews other roles' work for missing requirements and errors", "local", 1),
]
BY_ID = {r.id: r for r in ROLES}


# ---- workers ------------------------------------------------------------------------------------
def starnet_available() -> bool:
    return (STARNET_HOME / "node.exe").exists() and (STARNET_HOME / "bin" / "starnet.js").exists()


def starnet_crew() -> dict:
    out = _starnet(["ls", "--json"], timeout=30)[1]
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def _starnet(args: list[str], stdin: str | None = None, timeout: int = 30) -> tuple[int, str, str]:
    if not starnet_available():
        raise Permanent("StarNet isn't installed on this PC.")
    try:
        r = subprocess.run([str(STARNET_HOME / "node.exe"), str(STARNET_HOME / "bin" / "starnet.js"), *args, "--no-spawn"],
                           input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW, cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"StarNet didn't answer within {timeout} s")
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def run_starnet(agent_id: str, instruction: str, seconds: int = TASK_SECONDS) -> dict:
    """One non-interactive run on a crew agent. Returns {"text", "usd", "provider"}."""
    code, out, err = _starnet(["-p", "-", "--agent", agent_id, "--json", "--timeout", str(seconds)],
                              stdin=instruction, timeout=seconds + 60)
    try:
        res = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        res = {}
    if code == 3:
        raise Defer(300, "StarNet isn't running; trying again in 5 minutes")
    if code == 0 and res.get("ok"):
        return {"text": str(res.get("text") or "").strip(), "usd": float(res.get("usd") or 0)}
    why = "; ".join(str(e) for e in (res.get("errors") or []) if e) or str(res.get("error") or err[:200])
    if code == 4:
        raise StarNetTimeout(f"StarNet's {agent_id} ran out of time without an answer")
    if code in (2, 6):
        raise Permanent(f"StarNet couldn't take that task (exit {code}): {why}")
    raise RuntimeError(f"StarNet run didn't finish (exit {code}): {why}")


def run_local(prompt: str, model: str, schema: dict | None = None, max_tokens: int = 900) -> str:
    """Fafnir's own local model (free). With a schema, the reply is forced to that JSON shape."""
    body = {"model": model, "stream": False, "think": False, "messages": [{"role": "user", "content": prompt}],
            "options": {"num_ctx": 4096, "temperature": 0.2, "num_predict": max_tokens}, "keep_alive": "10m"}
    if schema:
        body["format"] = schema
    try:
        r = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=300)
        r.raise_for_status()
    except httpx.ConnectError:
        raise Defer(120, "the local model server isn't running")
    return (r.json().get("message", {}).get("content") or "").strip()


# ---- the workforce ------------------------------------------------------------------------------
class Workforce:
    def __init__(self, ops: Ops, local_model: str = "qwen3:4b-instruct", notify=None, gaming=None,
                 starnet=run_starnet, local=run_local, integration_on=None, mapping: dict | None = None):
        self.ops, self.model = ops, local_model
        self.notify = notify or (lambda title, detail, urgency: None)
        self.gaming = gaming or (lambda: False)            # True while a game needs the graphics card
        self._starnet, self._local = starnet, local
        self.integration_on = integration_on or (lambda pid: False)
        self.mapping = mapping or {}                         # role id -> StarNet agent id overrides
        for r in ROLES:
            ops.register(JobKind(f"task:{r.id}", self._task_handler(r), role=r.id, level=r.level, max_attempts=3,
                                 description=r.does))
        ops.register(JobKind("goal", self._goal, role="orchestrator", level=1, max_attempts=3,
                             description="Split a goal into role tasks, run them, summarise"))

    # ---- what each role runs on ----
    def worker(self, r: Role) -> str:
        if r.worker.startswith("starnet:") and r.id in self.mapping:
            return "starnet:" + self.mapping[r.id]
        return r.worker

    def available(self, r: Role) -> tuple[bool, str]:
        """(can it work now, everything that's stopping it)."""
        why = []
        if r.level > self.ops.level:
            why.append(f"needs autonomy level {r.level} (now {self.ops.level})")
        missing = [n for n in r.needs if not self.integration_on(n)]
        w = self.worker(r)
        if missing or (w.startswith("integration:") and not r.needs):
            why.append(r.later or f"needs {', '.join(missing)}")
        if w.startswith("starnet:") and not starnet_available():
            why.append("StarNet isn't installed")
        return (not why), ("; ".join(why) or "ready")

    # ---- one role task ----
    def _task_handler(self, r: Role):
        def handle(ctx: JobContext) -> dict:
            ok, why = self.available(r)
            if not ok:
                raise Refused(f"{r.name} can't work yet: {why}")
            instruction = str(ctx.payload.get("instruction", "")).strip()
            if not instruction:
                raise Permanent("No instruction given.")
            w = self.worker(r)
            if "output" not in ctx.checkpoint_data:      # resume after a crash without redoing paid work
                if w.startswith("starnet:"):
                    ctx.spend("starnet", 0.25)            # a small reserve: the run's real cost is recorded after
                    try:
                        res = self._starnet(w.split(":", 1)[1], self._brief(r, instruction))
                    except StarNetTimeout as e:
                        if self.gaming():
                            raise Defer(600, "a game is running; the local model waits")
                        # Better a clearly labelled answer than an endless retry loop.
                        res = {"text": "[Answered by Fafnir's local model WITHOUT web search, because " + str(e) +
                                       ". Treat facts and figures below as unverified.]\n\n" +
                                       self._local(self._brief(r, instruction), self.model), "usd": 0}
                    if res.get("usd"):
                        ctx.spent("starnet", float(res["usd"]), note=f"{r.id} via StarNet")
                    text = res.get("text", "")
                else:
                    if self.gaming():
                        raise Defer(600, "a game is running; the local model waits")
                    text = self._local(self._brief(r, instruction), self.model)
                if not text.strip():
                    raise RuntimeError("the worker returned an empty reply")
                ctx.checkpoint(output=text)
            out = {"role": r.id, "worker": w, "instruction": instruction, "output": ctx.checkpoint_data["output"]}
            if r.qa and r.id != "qa":
                out["qa"] = self._qa(instruction, out["output"])
            if not ctx.payload.get("parent"):
                verdict = f" QA: {out['qa']['verdict']}." if "qa" in out else ""
                self.notify(f"{r.name} finished: {instruction[:70]}", out["output"][:400] + verdict, "info")
            return out
        return handle

    @staticmethod
    def _brief(r: Role, instruction: str) -> str:
        return (f"You are the {r.name} desk of Fafnir Enterprise ({r.does.lower()}). Task:\n{instruction}\n\n"
                "Work only on this task. Say clearly what is a fact (with its source), what is an estimate, and what "
                "is an assumption. Don't contact anyone, publish, buy or sign anything; prepare it for the owner instead. "
                "Keep it concise and practical.")

    def _qa(self, instruction: str, output: str) -> dict:
        schema = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["pass", "needs work"]},
                                                   "missing": {"type": "array", "items": {"type": "string"}},
                                                   "unsupported_claims": {"type": "array", "items": {"type": "string"}}},
                  "required": ["verdict", "missing", "unsupported_claims"]}
        prompt = ("You are quality assurance. Check the work below against the task. List requirements of the task it "
                  "misses, and claims stated as fact without a source or that look invented. Be strict but fair.\n\n"
                  f"TASK:\n{instruction[:1500]}\n\nWORK:\n{output[:5000]}")
        try:
            if self.gaming():
                return {"verdict": "not checked", "missing": [], "unsupported_claims": [], "note": "skipped while gaming"}
            return json.loads(self._local(prompt, self.model, schema=schema, max_tokens=400))
        except (ValueError, Defer, httpx.HTTPError) as e:
            return {"verdict": "not checked", "missing": [], "unsupported_claims": [], "note": str(e)[:100]}

    # ---- goals (the orchestrator) ----
    def _goal(self, ctx: JobContext) -> dict:
        goal = str(ctx.payload.get("goal", "")).strip()
        if not goal:
            raise Permanent("No goal given.")
        if "children" not in ctx.checkpoint_data:
            if self.gaming():
                raise Defer(600, "a game is running; planning waits")
            usable = [r for r in ROLES if r.id not in ("orchestrator", "qa") and self.available(r)[0]]
            if not usable:
                raise Refused("No role can work at the current autonomy level.")
            ids = [r.id for r in usable]
            schema = {"type": "object", "properties": {"tasks": {"type": "array", "maxItems": MAX_GOAL_TASKS, "items": {
                "type": "object", "properties": {"role": {"type": "string", "enum": ids},
                                                 "instruction": {"type": "string"}}, "required": ["role", "instruction"]}}},
                      "required": ["tasks"]}
            prompt = (f"You coordinate the staff of a small business. Owner's goal:\n{goal}\n\nAvailable roles:\n"
                      + "\n".join(f"- {r.id}: {r.does}" for r in usable)
                      + f"\n\nSplit the goal into 1 to {MAX_GOAL_TASKS} concrete tasks, each for one role, each with a "
                        "self-contained instruction (the role sees nothing else). Use only the roles listed.")
            plan = json.loads(self._local(prompt, self.model, schema=schema, max_tokens=900)).get("tasks", [])
            plan = [t for t in plan if t.get("role") in ids and str(t.get("instruction", "")).strip()][:MAX_GOAL_TASKS]
            if not plan:
                raise RuntimeError("the planner produced no usable tasks")
            children = []
            for i, t in enumerate(plan):
                j = self.ops.enqueue(f"task:{t['role']}", {"instruction": t["instruction"], "parent": ctx.id},
                                     idem=f"goal:{ctx.id}:{i}", client=ctx.client, project=ctx.project)
                children.append(j["id"])
            ctx.checkpoint(children=children, plan=plan)
            raise Defer(30, f"waiting for {len(children)} task(s)")
        kids = [self.ops.job(i) for i in ctx.checkpoint_data["children"]]
        if any(k and k["status"] in ("queued", "running", "retrying", "waiting_approval") for k in kids):
            raise Defer(60, "waiting for tasks to finish")
        done = [k for k in kids if k and k["status"] == "done"]
        failed = [k for k in kids if k and k["status"] != "done"]
        if "summary" not in ctx.checkpoint_data:
            if self.gaming():
                raise Defer(600, "a game is running; the summary waits")
            parts = "\n\n".join(f"[{k['result']['role']}] {k['result']['instruction']}\n{k['result']['output'][:1800]}" for k in done)
            prompt = (f"Write a short executive summary for the owner. Goal: {goal}\n\nResults from the team:\n{parts}\n\n"
                      f"{len(failed)} task(s) failed. Give: what was found, recommended next steps (each needing the "
                      "owner's approval), and open questions. Mark estimates and assumptions as such.")
            ctx.checkpoint(summary=self._local(prompt, self.model, max_tokens=700) if done else "No task finished.")
        summary = ctx.checkpoint_data["summary"]
        self.notify(f"Goal finished: {goal[:70]}", summary[:600], "important")
        self.ops.publish("report", title=f"Goal: {goal[:80]}", text=summary)
        return {"goal": goal, "summary": summary, "tasks": [{"role": k["result"]["role"], "job": k["id"]} for k in done],
                "failed": [{"job": k["id"], "kind": k["kind"], "error": k["error"]} for k in failed]}

    # ---- for the owner ----
    def assign(self, role: str, instruction: str, client: str = "", project: str = "") -> dict:
        r = BY_ID.get(role)
        if r is None:
            raise Refused(f"No role called '{role}'. Roles: {', '.join(BY_ID)}")
        ok, why = self.available(r)
        if not ok:
            raise Refused(f"{r.name} can't take work yet: {why}")
        return self.ops.enqueue(f"task:{role}", {"instruction": instruction}, client=client, project=project)

    def give_goal(self, goal: str, client: str = "", project: str = "") -> dict:
        return self.ops.enqueue("goal", {"goal": goal}, client=client, project=project)

    def roster(self, live: bool = True) -> list[dict]:
        crew = {}
        if live and starnet_available():
            try:
                crew = {a.get("agentId"): a for a in starnet_crew().get("agents", [])}
            except Exception:
                crew = {}
        role_rows = {x["role"]: x for x in self.ops.roles()}
        out = []
        for r in ROLES:
            ok, why = self.available(r)
            w = self.worker(r)
            agent = crew.get(w.split(":", 1)[1]) if w.startswith("starnet:") else None
            row = role_rows.get(r.id, {})
            if row.get("suspended"):
                state = "paused"
            elif not ok:
                state = "unavailable"
            elif row.get("queued"):
                state = "busy"
            else:
                state = (agent or {}).get("status") or "idle"
            out.append({"id": r.id, "name": r.name, "department": r.department, "does": r.does, "state": state,
                        "why": row.get("why") if row.get("suspended") else ("" if ok else why),
                        "worker": ("StarNet: " + (agent or {}).get("name", w.split(':', 1)[1])) if w.startswith("starnet:")
                        else ("Fafnir local model" if w == "local" else w.split(":", 1)[1].title()),
                        "model": (agent or {}).get("model") if agent else (self.model if w == "local" else None),
                        "level": r.level, "qa": r.qa, "queued": row.get("queued", 0),
                        "spent_usd": row.get("spent_usd", 0.0), "monthly_usd": row.get("monthly_usd"),
                        "recent": [{"id": j["id"], "status": j["status"], "updated": j["updated"]}
                                   for j in self.ops.jobs() if j["kind"] == f"task:{r.id}"][:5]})
        return out
