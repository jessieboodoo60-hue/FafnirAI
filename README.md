# Fafnir

A voice-first assistant harness, built tier by tier. See [AGENT.md](AGENT.md) for the spec.

## Setup (Windows, PowerShell)

```
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Open `.env` and set `ANTHROPIC_API_KEY`.

## The app

```
.venv\Scripts\python main.py --app
```

Opens Fafnir fullscreen on your right-hand monitor (change `screen` under `[app]` in `config.toml`). It also starts by itself when you log in to Windows, and it's in the Start menu as **Fafnir**. Hold M5 to talk or type in the box at the bottom; confirmations show Yes/No buttons. **Esc** stops Fafnir talking, **F11** toggles fullscreen, **Ctrl+Q** quits. Launching it again just brings the open one to the front.

To stop it starting with Windows: `.venv\Scripts\python main.py --remove-startup` (and `--install-startup` to turn it back on). If the app misbehaves, errors are in `logs\app.log`.

## Your phone (Discord)

Fafnir DMs you on Discord and you can DM it back from anywhere (the PC must be on with the app running).
1. Go to discord.com/developers/applications → **New Application** (name it Fafnir) → **Bot** → **Reset Token**, copy it into `.env` as `DISCORD_BOT_TOKEN=...`.
2. **OAuth2 → URL Generator**: tick `bot`, open the generated link, and add the bot to a small private server of your own (Discord only lets bots DM people they share a server with).
3. Restart the app; it shows `DM the bot ... /pair 123456`. Send exactly that to the bot in a DM. Only your account can use it from then on.

Risky actions you ask for from the phone come with Yes / No buttons. Important notices are DMed to you (`send` under `[phone]` in `config.toml`). `/pause` and `/resume` control notices.

## Your phone (WhatsApp) — waiting on Meta business verification

Fafnir can message you on WhatsApp and you can message it back from anywhere (the PC must be on with the app running). It uses Meta's official WhatsApp Business Cloud API, so Fafnir writes from its own number (Meta's free test number), not yours. Setup steps are in the chat where this was built; in short: a Meta developer app with WhatsApp, a permanent system-user token, your number added as a test recipient, a free ngrok static domain, and the webhook pointed at `https://<your-domain>/webhook` with the verify token the app shows. Secrets go in `.env` (see `.env.example`), the rest under `[phone]` in `config.toml`.

Message it like you'd talk to it. Risky actions you ask for from the phone come with Yes / No buttons. Important notices are sent to WhatsApp; WhatsApp only allows free-form messages within 24 hours of your last message, so for alerts after that, set `alert_template` to an approved template (otherwise they wait in the Notices panel). `/pause` and `/resume` control notices.

## Conversations and backups

Fafnir remembers recent conversations across restarts: the app reopens with today's chat, and new conversations start knowing the last day or so. Ask "what did we talk about on Monday?" for older ones. The log is in `data\conversations\` (one file per day; delete any you like).

Your personal data (`data\`, `.env`, `config.toml`) is zipped to `D:\FafnirBackups` once a day while the app runs, keeping the last 14 copies; you get a notice only if a backup fails. Run one by hand with `.venv\Scripts\python main.py --backup`. To restore, unzip the newest file into the project folder. The code itself is on GitHub.

## Heartbeat (Fafnir reaching out)

While the app is open, Fafnir checks in the background: meetings starting soon, tasks due or overdue, new important email, low disk space, and new YouTube comments. Findings show in the **Notices** panel (✕ to dismiss, CLEAR for all) and stay there until you dismiss them, even across restarts. Important ones are also spoken. Click **PAUSE** or say "don't disturb me for an hour" to silence it. Tune what it checks and how often under `[heartbeat]` in `config.toml`, e.g. add your key clients to `vip_senders` so their emails are announced.

## API credit or Claude subscription

`provider` under `[model]` in `config.toml` picks where Fafnir's brain runs:
- `"claude_code"`: through the Claude Code app on this PC, using your Claude subscription (its usage limits apply; costs shown are estimates, not charges).
- `"api"`: through Anthropic API credit (pay per token, with prompt caching).

Restart Fafnir after changing it.

## Terminal mode (for debugging)

```
.venv\Scripts\python main.py
```

Type to chat. `/reset` clears the conversation, `/tools` lists what Fafnir can use, `/quit` exits, Ctrl+C stops a reply mid-stream.

Fafnir has full control of the PC. If you directly ask for something ("delete that file", "install Spotify"), it just does it. It only stops to ask `Allow? (yes/no)` when a risky action was its own idea or came from something it read, or for system-wrecking actions like formatting a disk or shutting down. In voice mode it asks out loud: hold the key and say yes or no. Everything it does is logged in `logs/actions.log`.

Each tool call is shown on its own line (`[tool] name(...) -> ok`) so you can see what Fafnir did. Business items are stored in `data/business.json`, which you can open and edit by hand.

Settings (model, effort, tone, notes folder, web access, voice) live in `config.toml`.

## Memory

Fafnir remembers lasting things you tell it (preferences, people, business details) in `data/memory.md`, and knows them the next time you start it. Say "remember..." to save something on purpose, or "forget..." to remove it. Type `/memory` to see everything stored. You can also open `data/memory.md` and edit or delete lines yourself; Fafnir uses your version from then on.

## Google (Calendar, Gmail, Drive, Docs, Sheets, Contacts, Tasks, YouTube)

In Google Cloud Console, in your Fafnir project: enable **Google Calendar API**, **Gmail API**, **Google Drive API**, **People API**, **Google Tasks API** and **YouTube Data API v3**, add your Gmail as a **test user** on the consent screen, and create a **Desktop app** OAuth client. Save its JSON as `data\google_credentials.json`. The first time you ask about your calendar or email, a browser opens: pick your account and allow access. Google asks again about once a week while the app is in testing mode.

## Voice mode

Add `DEEPGRAM_API_KEY` and `ELEVENLABS_API_KEY` to `.env`, then:

```
.venv\Scripts\python main.py --voice
```

Hold the talk key, speak, release. Press it again while Fafnir is talking to cut it off. Typing still works in voice mode, and typed replies are spoken too. `/stop` silences it, `/reset` clears the conversation.

**Wake word:** say "Fafnir" instead of holding the key, then talk; Fafnir stops listening when you pause for a second. Say it while Fafnir is talking to cut it off. It runs entirely on this PC by comparing what you say with recordings of your own voice. Record them once (close the app first so the mic is free):

```
.venv\Scripts\python main.py --train-wake
```

It asks you to say "Fafnir" 6 times. The recordings stay in `data\wake\` (not uploaded anywhere). If it triggers by mistake, lower `sensitivity` under `[voice.wake]` in `config.toml`; if it misses you, raise it or re-record.

To set the talk key: in Razer Synapse, assign the M5 macro key to F13 (or any unused key), then run `.venv\Scripts\python main.py --learn-key`, press it, and copy the `ptt_key` line it prints into `config.toml`.
