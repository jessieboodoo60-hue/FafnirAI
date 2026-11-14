# FafnirAI app API (stage 1)

The back end the FafnirAI phone app will talk to. It starts with the desktop app and listens on
`127.0.0.1:8790` only: nothing on your home network or the internet can reach it. Reaching it from your
phone outside the house will go through Tailscale (stage 6), which forwards to this address privately.

## Set up your account (on this PC)

```
.venv\Scripts\python main.py --app-user-add YOURNAME
```

You choose a password (at least 10 characters). Owner accounts also need a 6-digit code from an
authenticator app (Google Authenticator, Microsoft Authenticator, Authy): the command prints a setup key
to type into that app. Other commands:

```
.venv\Scripts\python main.py --app-user-list
.venv\Scripts\python main.py --app-user-passwd YOURNAME     (also signs every device out)
.venv\Scripts\python main.py --app-user-add NAME --viewer    (read-only account, no code needed)
.venv\Scripts\python main.py --app-user-remove NAME
```

Accounts can't be created over the network. Password reset is done here on the PC; email verification and
email-based reset come when outbound email for the business is set up.

## Security

| Control | Detail |
|---|---|
| Passwords | scrypt, salted; weak ones refused |
| Second factor | Required for owner accounts (TOTP, 30 s, one step of clock drift allowed); secret stored encrypted |
| Tokens | Access token 15 min; refresh token 30 days, replaced on every use; an old refresh token coming back ends the whole session (it was copied). Only hashes are stored |
| Lockout | 5 wrong passwords or codes per account or address in 15 min: locked 15 min |
| Permissions | owner: read, chat, control, admin. viewer: read only. Checked on every route |
| Rate limit | 120 requests a minute per signed-in device |
| Live feed | WebSocket; the token is sent as the first message (never in the URL, so never in a log); closed when the session ends |
| Logging | Sign-ins, failures, refresh-token reuse, approvals, kill switch and integration changes go into the tamper-evident action log |
| Exposure | 127.0.0.1 only; docs pages switched off; `no-store`, `nosniff`, `DENY` framing headers |
| Secrets | API keys stay in the vault on the PC; the API never returns them and can't set them |

## Routes

| Route | Needs | What |
|---|---|---|
| `POST /auth/login` | - | username, password, code, device -> tokens |
| `POST /auth/refresh` | - | refresh token -> new tokens |
| `POST /auth/logout`, `/auth/logout-all` | signed in | end this device / every device |
| `GET /auth/sessions`, `DELETE /auth/sessions/{id}` | signed in | your signed-in devices |
| `GET /api/status` | read | state, brain, profile, kill switch, health problems, pending approval |
| `GET /api/now` | read | the Right Now briefing and items |
| `GET /api/health` | read | every health check |
| `GET /api/events/recent?after=N` | read | events since N |
| `WS /api/events` | read | live: heard, reply text, tools, notices, errors, approvals, Right Now |
| `POST /api/chat` | chat | talk to Fafnir; waits up to 3 minutes for the reply |
| `GET /api/approvals`, `POST /api/approvals/current` | read / control | what Fafnir is waiting on; yes or no |
| `POST /api/killswitch`, `POST /api/interrupt` | control | stop everything / stop talking |
| `GET /api/integrations`, `PUT /api/integrations/{id}` | read / admin | outside services and their switches |

## Integrations

Higgsfield, Gemini, OpenAI, the Anthropic API, Stripe, Firebase and Tailscale each have a switch (Settings >
Integrations, `[integrations]` in `config.toml`, or the API). All start off. A switch can only be turned
on once the service is ready: its key in the vault (`python main.py --secret-set NAME`), or for Tailscale,
the program installed. Nothing uses a service until its switch is on.

## Tested

`.venv\Scripts\python -m pytest tests\test_api.py -q`: 15 tests (hashing, weak passwords, lockout, MFA,
viewer limits, every route refusing a missing or made-up token, refresh rotation and reuse detection,
expiry, logout and logout-all, password change signing everyone out, chat, approvals, kill switch,
integration switches, the live feed, rate limiting). Also checked against the running Fafnir with a
temporary read-only account: sign-in, status, Right Now, refusals, live feed, and that the port is not
reachable from the home network.

## Stage 2: jobs, approvals, budget, autonomy (`fafnir/ops.py`)

Everything the future agents do will run as **jobs** in `data/ops.db`:

- **Event-driven:** workers sleep until a job is due or one is added; no loop calls a model when there's nothing to do.
- **No double actions:** a job can carry an idempotency key (for example `invoice-42`); the same key is never queued twice, and scheduled jobs use the time slot as their key.
- **Crash-safe:** a job that was running when Fafnir stopped goes back in the queue at the next start; long jobs save checkpoints and resume from them.
- **Retries:** network-type failures retry after about 30 s, 60 s, 120 s... (up to 1 h), then the job is marked failed and you get a notice. Errors retrying won't fix fail at once.
- **Approval queue:** a job that reaches a sensitive step waits in the queue (app, desktop or phone) and carries on only after your yes; a no cancels it; unanswered requests expire after 7 days.
- **Autonomy levels** (Settings, `[autonomy]`, or the API): 1 research and recommend (the default), 2 prepare and ask, 3 run approved work within limits, 4 low-risk reversible actions alone. Spending, paid campaigns, bulk outreach, contracts, moving money, changing financial accounts, publishing, deploying, deleting data and legal matters **always** ask, at every level.
- **Budget:** US$50 a month by default (Settings, `[budget]`, or the API). Checked **before** each paid call; refused once reached; you're told at 80% and 100%. Tracked per provider, role, client and project. Only pay-per-use services count; your Claude subscription and the local models don't.
- **Automatic suspension:** a role whose jobs fail 3 times within an hour, or that hits its own budget, is paused until you resume it. The kill switch pauses every worker.
- **Daily report** at 08:00: jobs done and failed, what's waiting for you, spending, the autonomy level. Built from the records, no AI call, no cost.

| Route | Needs | What |
|---|---|---|
| `GET /api/ops` | read | level, job counts, pending approvals, budget, paused roles |
| `GET /api/jobs`, `GET /api/jobs/{id}` | read | jobs and their results, errors, checkpoints |
| `POST /api/jobs/{id}/cancel` | control | cancel a waiting job |
| `GET /api/approvals`, `POST /api/approvals/{id}` | read / control | Fafnir's current question (`current`) and queued requests (numbers) |
| `GET /api/budget`, `PUT /api/budget` | read / admin | spending this month; change the cap |
| `GET /api/autonomy`, `PUT /api/autonomy` | read / admin | the level and what always needs approval |
| `GET /api/roles`, `POST /api/roles/{role}/resume` | read / control | roles, their spend, paused ones |
| `GET /api/schedules` | read | scheduled jobs |

Tests: `.venv\Scripts\python -m pytest tests -q` (18 job-system tests plus the 15 API tests). They cover results,
duplicates, backoff, giving up, permanent errors, crash recovery with checkpoints, approval wait and
resume, rejection, always-approve at level 4, autonomy gating, budget refusal before the call, what
counts as billable, role budgets and failure suspension, the kill switch, schedules firing once per
slot, cancelling, expiry, and the free daily report.

## Not yet built

A persistent approval queue (today only the one question Fafnir is currently asking), agents, business
data, push notifications, and remote access. Those are stages 2 to 6.
