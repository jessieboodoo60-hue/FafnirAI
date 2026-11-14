# FafnirAI business engine and Android app: what exists, what's blocked, staged plan

Written 2026-11-14 after inspecting the repository and this PC. Phase 1 (the Right Now overflow) is
done and verified at 1440, 1280, 1024, 768 and 375 px (commit abbc9fa). Nothing below is built yet.

## What already exists and will be reused

| Need in the brief | Already in FafnirAI |
|---|---|
| Agent core, tools, approvals | One agent loop, 100 tools behind one confirmation gate, critical actions always ask (`fafnir/tools/registry.py`) |
| Emergency shutdown | Kill switch: hotkey, voice, Discord, command line; survives restart |
| Audit logs | Hash-chained `logs/actions.log`, outbound log |
| Spending/loop protection | Per-tool rate limits; no money-moving tools exist |
| Health, recovery | `fafnir/health.py`: 20 checks, three safe self-repairs |
| Secrets | Encrypted vault, never in the environment or the mobile app |
| Local model routing | Qwen 4B/8B via Ollama, cloud hand-off with consent, gaming-aware profiles |
| A crew of agents | **StarNet** (installed in `%LOCALAPPDATA%\StarNet`) already holds a main agent, a business chief, a researcher and a marketer. An integration (`fafnir/tools/starnet.py`) is being written in another session right now. |
| Remote control | Discord bot (paired to one account); WhatsApp blocked by Meta |
| Business records | A small JSON business tracker (`data/business.json`) |

**Recommendation:** build the agent workforce on top of the existing gate, audit log and StarNet crew,
not a second, parallel agent system. Fourteen agents become roles with a job queue and a budget each,
coordinated by one orchestrator; most of them only wake up when there is work.

## Blockers (nothing can be faked around these)

| Blocker | Needed for | What resolves it |
|---|---|---|
| Flutter SDK, Android SDK and a JDK 17/21 not installed (Java 25 is present; the Android build tools don't support it yet) | The Android app, APK, App Bundle | Your OK to install them (about 10 GB), or building the app on another machine |
| No secure way in from outside your home network | The app working "from anywhere" | A private network such as Tailscale (free for personal use; you create the account), or Cloudflare Tunnel with a login in front. Fafnir's admin services stay off the public internet either way. |
| No Higgsfield API key | Image and video generation | Your Higgsfield account and key (`--secret-set HIGGSFIELD_API_KEY`) |
| No Gemini or OpenAI key | The suggested research and sales models | Keys, or route those roles to Claude and local Qwen instead |
| No payment provider | Invoices, tracking payments, real revenue figures | A Stripe (or PayPal) account in test mode first; Fafnir will never move funds itself |
| No sending domain for business email | Compliant outreach that won't land in spam | A domain (Fafnir Enterprise already has a site on GitHub Pages) with SPF/DKIM set up, and an opt-out mechanism |
| Firebase project | Push notifications on the phone | A free Firebase project, or skip push and use the live connection only |
| PostgreSQL, Docker not installed | The brief's preferred database | Not needed to start: an encrypted SQLite database covers one owner; PostgreSQL can come with a server move |

## Staged plan (each stage: built behind a switch, tested, committed, reversible)

1. **Backend API (no new installs).** A FastAPI service inside Fafnir: login with hashed passwords,
   short-lived tokens with refresh rotation, rate-limited, every endpoint checked; WebSocket for live
   events; listening on this PC only. Exposes chat, the Right Now feed, approvals, health, agents,
   kill switch. Tests for every endpoint and for unauthenticated access being refused.
2. **Approval queue and job system.** Persistent jobs (SQLite), retries with backoff, no double
   execution, checkpoints, schedules, per-role budgets with the US$50/month cap enforced before a
   model call is made, not after. Autonomy levels 1–4 as a setting; level 1 to start.
3. **Agent workforce on StarNet + Fafnir.** Orchestrator, research, lead scoring (local Qwen),
   outreach drafting, sales, development, finance (read-only), QA. Each role has a model, a budget, a
   tool allowlist and an approval rule. Starts at level 1: research and recommendations only.
4. **Business discovery.** Opportunity research with sources recorded, scoring, business plans
   presented for approval. No launch without your yes.
5. **Android app (after the SDK install).** Flutter, dark charcoal/crimson. Login, dashboard,
   chat, Right Now, agents, approvals, settings first; leads, creative studio and finance once their
   back ends exist. Tokens in Android's secure storage. Debug APK first; a signed release needs a
   signing key you keep.
6. **Remote access** through the private network chosen above.
7. **Leads and outreach.** Lead database with sources, dedupe, scoring explained, suppression list,
   opt-out handling. Every outbound campaign needs approval.
8. **Creative pipeline** (Higgsfield once a key exists; FFmpeg is installed; Remotion via Node,
   which is installed).
9. **Finance** from real integrations only. No simulated revenue is ever shown as earnings.

## What will not happen

- No guaranteed revenue, and no figures shown as earnings that didn't come from a real payment.
- No unsolicited bulk email, no spending, no contracts, no money movement, no publishing without
  your approval, at every autonomy level.
- No public exposure of Ollama, the database or Fafnir's internal services.
- Nothing is claimed to work until it has been run and tested.

## Progress

- **Stage 1 done (2026-11-14):** the app API (accounts with scrypt passwords and a required authenticator code for owners, rotating refresh tokens with reuse detection, lockout, roles, rate limits, live WebSocket feed, chat, approvals, kill switch, health, Right Now) on 127.0.0.1:8790, plus integration switches that stay off until a key exists. 15 automated tests and a live check. Details: docs/APP_API.md.
