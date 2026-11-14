# The AI workforce (stage 3)

Fourteen roles, coordinated through the job system (`fafnir/ops.py`) rather than agents chatting with
each other. Code: `fafnir/workforce.py`.

## Who does what

| Role | Runs on | Autonomy level | QA review | Waiting on |
|---|---|---|---|---|
| CEO / Orchestrator | Fafnir's local model | 1 | | |
| Research | StarNet: FAFNIR RESEARCH | 1 | yes | |
| Lead Scoring | Fafnir's local model | 1 | | the lead database (stage 7) |
| Outreach | StarNet: COPYWRITER | 2 | yes | sending needs email setup and per-campaign approval (stage 7) |
| Sales | StarNet: INBOX MANAGER | 2 | yes | |
| Development | Fafnir (Claude Code later) | 3 | | |
| Marketing | StarNet: FAFNIR MARKETING | 1 | yes | |
| Creative Director | StarNet: SCRIPTWRITER | 1 | yes | |
| Image Generation | Higgsfield | 2 | | a Higgsfield key |
| Video Generation | Higgsfield | 2 | | a Higgsfield key |
| Video Editor | FFmpeg | 2 | | the editing pipeline (stage 8) |
| Customer Support | StarNet: INBOX MANAGER | 2 | yes | customers and an approved knowledge base |
| Finance | Fafnir's local model | 1 | | a payment provider for real revenue |
| Quality Assurance | Fafnir's local model | 1 | | |

All StarNet crew agents currently run on the local Qwen 4B, so their work costs nothing. Change which
crew agent serves a role under `[workforce.crew]` in `config.toml` (for example `research = "researcher"`).

## How work flows

- **One task:** "have the researcher find X" (to Fafnir, or `POST /api/agents/research/tasks`) becomes
  a job. The role's worker does it; roles marked QA get a second check (missing requirements, claims
  without a source); the result arrives as a notice and stays on the job.
- **A goal:** "look into starting a website service" (`workforce_goal`, or `POST /api/goals`). The
  orchestrator splits it into up to six tasks for roles allowed at the current autonomy level, queues
  them, waits without spending anything, then writes an executive summary with next steps for your approval.
- **Every brief** tells the worker to separate facts (with sources), estimates and assumptions, and
  never to contact anyone, publish, buy or sign. StarNet runs are non-interactive, so anything a crew
  agent would need consent for is refused inside the run anyway.
- **Gaming-aware:** work on Fafnir's local model waits while a game is running.
- **Safety from stage 2 applies:** budget checked before paid work, autonomy gating, approvals,
  retries, crash recovery (finished work isn't redone), automatic pause after repeated failures, kill switch.

## Controls

| Where | What |
|---|---|
| Talk to Fafnir | "have the team research...", "give the team a goal...", "how is my team doing?" |
| `GET /api/agents` | every role: state (idle, busy, paused, unavailable and why), worker, model, spend, recent jobs |
| `POST /api/agents/{role}/tasks` | give a role a task (control) |
| `POST /api/agents/{role}/pause`, `/resume` | stop or restart a role (control) |
| `POST /api/goals`, `GET /api/goals` | give the team a goal; see goals with their tasks |

## Known limit: research on the small local model

In the first live run (2026-11-14) the StarNet researcher, running on Qwen 4B, tried to search the web
and ran out of time twice without an answer (StarNet exit 4, after 15 minutes each). Now a run is capped
at 10 minutes, and if it times out the task is answered by Fafnir's local model **without web search**,
labelled as such at the top, so it doesn't retry for an hour. Proper web research needs a stronger model
for the researcher (switch its model in StarNet) or the Gemini integration once you have a key.

The same run found a real bug: a second program opening the job database (a command or a check)
re-queued jobs the running Fafnir was still working on, which could have made a task run twice. Running
jobs are now tagged with the program running them and only recovered once that program has gone.

## Tested

`tests/test_workforce.py`, 9 tests with stand-ins for StarNet and the local model: the roster,
a research task with QA, autonomy gating, missing integrations, a goal split, run and summarised
(with a not-allowed task dropped), no repeated paid work after a crash, deferral while gaming,
failures pausing a role, paid crew runs counted and refused at the budget, and the labelled fallback when
StarNet times out (10 tests; plus a job-system test that a second program can't take over running jobs).
