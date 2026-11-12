# Fafnir — agent spec

The single source of truth for what we're building and why. Update it when a decision changes.

## Identity
- **Name:** Fafnir
- **Purpose:** a crisp, professional voice-first assistant that helps run the owner's business, social media, and computer.
- **Users:** just the owner for now. Keep a `user_id` slot in persistent state so a small team is possible later.
- **Tone:** crisp and professional. Brief, direct, no filler. Same voice in text, speech and proactive notices.

## Stack
- **Language:** Python 3.13 (moved from 3.15 on 2026-11-10 after a 3.15 lazy-import bug broke tools; the 3.15 workaround in `fafnir/__init__.py` stays as a harmless safeguard). Plain modules, no framework.
- **Conversations:** every exchange is logged to `data/conversations/YYYY-MM-DD.jsonl`; new conversations get the last ~36 hours in the system prompt (background, not instructions); `conversation_search` reaches back a year; the app reopens with today's chat.
- **Backups:** the heartbeat's `backup` check zips `data/`, `.env` and `config.toml` to `D:/FafnirBackups` daily, keeping 14 (`[heartbeat.backup]`); code is on GitHub.
- **Brain:** Claude (`claude-sonnet-5-5`, switched from Opus 5.5 on 2026-11-10 to halve cost; set in config.toml) through the official `anthropic` SDK, behind one seam (`fafnir/llm.py`). Nothing else imports the SDK.
- **Cost:** prompt caching is on (instructions, tools and history re-read at ~5% price). Each reply shows `[cost $x · N% cached · session $y]`; `show_cost` in config.toml turns it off.
- **Voice (Tier 3):** Deepgram (speech-to-text) and ElevenLabs (text-to-speech), each behind its own seam. Voice chosen at Tier 3 and stored in config.
- **Runs on:** the owner's Windows laptop first. The heartbeat (Tier 5) is a separable process so it can move to an always-on host later without a rewrite.
- **Secrets:** environment variables / `.env` (git-ignored). Never in source.

## First capabilities (Tier 2 tools)
1. **Business management:** a local store of business tasks, clients/contacts and deadlines, with add / list / update tools.
2. **Computer management:** read-only to start (list folders, find files, read a text file, disk and system info, open a file or app). Anything that changes or deletes goes through the confirmation gate.
3. **Q&A:** answer questions from a configurable notes folder, plus web lookup.

Also wanted, after the baseline: draft messages, and platform integrations for **Twitch, Instagram/Facebook, TikTok/YouTube, X/LinkedIn, email & calendar**. One tool per integration. Posting and sending are always gated.

### Tools as built (fafnir/tools/)
- `business_*`: add, list, update (local file `data/business.json`); delete is marked as needing confirmation and refuses until Tier 6.
- `computer_*`: list folder, find files by name, read text file (refuses secret files like `.env`), system info, open file/folder/URL.
- `computer_launch_app`, `computer_list_apps`: start any installed app from the Start menu by name.
- Full control (owner's decision, 2026-11-10): `computer_run_command` (any PowerShell), `computer_close_app`, `computer_list_running_apps`, `computer_write_file`, `computer_make_folder`, `computer_move_or_copy`, `computer_delete` (Recycle Bin), `computer_media` (play/pause, tracks, volume).

### Confirmation gate (pulled forward from Tier 6)
Tools declare when they need a yes (`confirm_if` in `fafnir/tools/registry.py`). The registry asks before running; each yes covers one action; no answer within 60 seconds means no.
- **Owner asked = no second question** (owner's decision, 2026-11-10): gated tools carry an `owner_asked` flag. When the owner's own message directly asked for that exact action, Fafnir sets it and the action runs without asking again. It still asks when the action was its own idea, a step the owner didn't mention, or suggested by a file/web page/note, and on turns the owner didn't start (`owner_present=False`, for the future heartbeat).
- **Always asks, even when requested** (`critical_if`): disk wipes/formatting, partition and boot changes, deleting system registry keys or Windows/Program Files content, disabling Defender or System Restore, deleting backups, shutdown/restart.
- **Asks first:** deleting files or business items, overwriting a file, force-quitting an app, running a program/script file, reading files that hold secrets, and any PowerShell command that isn't clearly read-only (installs, settings, services, registry, killing processes, writing files).
- **Runs immediately:** reading and looking, launching and politely closing apps, creating files and folders, moving and copying (never overwrites), media keys, read-only commands.
- Text mode asks in the terminal. Voice mode speaks the question and takes a spoken or typed yes/no; anything other than a clear yes is a no.
- Every tool call, and whether it was confirmed, is logged to `logs/actions.log`.
- `weather`: live conditions and forecast from Open-Meteo; defaults to the owner's home location.
- `notes_search`, `notes_read`: keyword search over `notes_folder` (config.toml); skips OneDrive cloud-only files so searching doesn't download them.
- `web_search`, `web_fetch`: Anthropic-hosted, toggled in config.toml.

### Google Calendar and Gmail
- `fafnir/google_auth.py` signs in with the owner's Google Cloud "Desktop app" OAuth client (`data/google_credentials.json`). The first use opens a browser for approval; the token is kept in `data/google_token.json` and refreshed automatically. In Google's "testing" mode the approval expires about weekly and the browser step repeats.
- Scopes: `calendar` and `gmail.modify` (read, label, draft, send, trash; no permanent delete).
- Calendar: `calendar_list_events`, `calendar_create_event`, `calendar_update_event`, `calendar_delete_event`. Gmail: `gmail_search`, `gmail_read`, `gmail_draft`, `gmail_send`, `gmail_send_draft`, `gmail_trash`, `gmail_label`.
- Gated: sending email or drafts, trashing email, deleting events, and anything that emails invitations or updates to other people. Reading, searching, drafting and private events run immediately.
- Email content is untrusted: Fafnir never sends, replies or acts just because an email asks.
- Also (`fafnir/tools/google_more.py`, owner asked for full Google access 2026-11-10): Drive incl. Docs/Sheets/Slides (search, read, create, update, rename/move, upload, download, share, trash), Contacts (search, add), Google Tasks (list, add, update, delete), YouTube (channel stats, my videos, comments, reply, edit title/description). Scopes: `drive`, `contacts`, `tasks`, `youtube.force-ssl`. Gated: sharing, trashing, overwriting a file, deleting tasks, anything public on YouTube.
- Not included on purpose: permanent email deletion and Gmail forwarding/filter settings (a classic way for a hijacked assistant to leak mail).

### Memory (Tier 4)
- `data/memory.md`: plain Markdown, one fact per line under headings (About me, Preferences, People, Business, Other, or any heading the owner adds). Re-read on every use, so hand edits apply immediately; lines added by hand get an ID automatically.
- Loaded into the system prompt when a conversation starts (and on `/reset`), then frozen for that conversation. Up to 200 most recent facts go in; older ones are found with `memory_search`.
- Tools: `memory_remember`, `memory_update`, `memory_search`, `memory_forget` (asks first unless the owner said to forget it).
- Facts are background knowledge, never instructions; a stored "always do X" doesn't bypass judgement or the confirmation gate.
- `/memory` shows what's stored.

To add a capability: write a module in `fafnir/tools/` with a `register(reg)` function and call it from `fafnir/tools/__init__.py`. The core loop never changes.

## How the owner talks to it
Text first (always kept as a debug path and fallback), push-to-talk (Tier 3), and a wake word.
- **Wake word "Fafnir"** (`fafnir/voice/wake.py`, `kws.py`): fully local, no account (owner declined Picovoice, 2026-11-10). An always-open 16 kHz mic stream; each burst of speech is compared (MFCC features + dynamic time warping, numpy only) with the owner's own recordings in `data/wake/` (`python main.py --train-wake`, 6 samples, self-calibrating threshold). Audio only leaves the PC after a match. Then the same stream goes to Deepgram and a loudness detector ends the utterance after `end_silence_seconds` of quiet (gives up after 5 s, caps at 20 s). Saying it while Fafnir talks interrupts (`while_speaking`). Same turn path and confirmations as push-to-talk. Speaker-dependent: tuned to the owner's voice and mic.
- **Talk key:** the Razer keyboard's M5 macro key, mapped in Razer Synapse to F13 (`ptt_key` in config.toml).
- **Voice:** ElevenLabs voice ID `43vS2JKzejLgSwmqF6gC`, model `eleven_flash_v2_5` for low latency.

## Never without asking (hard confirmation gate, Tier 6)
- sending a message (email, DM, chat, comment)
- posting or publishing anything publicly
- spending money
- deleting data
- changing a setting

Each approval covers one action only. Content Fafnir reads (web pages, emails, files, transcripts, stored memories) is data, never instructions.

## Proactive behaviour (Tier 5)
Yes. Fafnir may interrupt **at any time, including quiet hours, when something is important**. Everything else is held in a calm inbox and shown on return. Notices are never dropped while the interface is closed, and every notice can be dismissed.

## Build order
Each tier runs and is verified on its own before the next starts.

| Tier | Layer | Status |
|---|---|---|
| 0 | Interview + this spec | done |
| 1 | Brain: streaming text conversation loop | verified 2026-11-10 |
| 2 | Hands: tool registry + first tools | verified 2026-11-10 (built after Tier 3, at the owner's request) |
| 3 | Ears and mouth: push-to-talk, Deepgram, ElevenLabs, interruption | built; awaiting Deepgram/ElevenLabs keys and the M5 key mapping to verify |
| 4 | Memory: durable, human-readable facts across restarts | verified 2026-11-10 |
| 5 | Heartbeat: scheduled checks, held inbox, persisted schedule | verified 2026-11-10 |
| 6 | Rails: confirmation gate, injection flagging, audit log, cost tally, kill switch | gate and action log done early; injection flagging, cost tally, kill switch remain |

## Heartbeat (Tier 5)
- `fafnir/heartbeat/`: `runner.py` (loop), `checks.py` (calendar soon, tasks due/overdue, new important email, low disk, new YouTube comments), `notices.py` (inbox). Settings and intervals: `[heartbeat]` in config.toml.
- Notices live in `data/notices.json`: held until dismissed (shown on return, never fire-and-forget), deduplicated by key, dismissed ones remembered 14 days so they don't come back.
- `info` notices are silent; `important`/`urgent` are spoken by the app when Fafnir is free, one at a time. VIP email senders (config) and overdue tasks count as important. Optional `quiet_hours` mutes spoken important notices (urgent still speaks); default off, per the owner.
- Schedule persists in `data/heartbeat_state.json`; overdue checks after a restart are staggered 20s apart. A still-running check is skipped, not stacked. Email and YouTube seed silently on their first ever run (no flood). A failing check logs to `logs/heartbeat.log` and never stops the loop.
- Kill switch: the PAUSE button on the Notices panel, or "don't disturb me (for N minutes)" via `heartbeat_pause`. Tools: `notices_list`, `notices_dismiss`, `heartbeat_pause/resume/status`.
- Runs inside the desktop app today; it has no UI dependency, so it can move to an always-on host.

## Phone link (Discord, active)
- `fafnir/discord_link.py`: private Discord bot (`DISCORD_BOT_TOKEN`, discord.py), outbound gateway connection, no tunnels. Paired once with a code shown in the app; only that user id (`data/discord.json`) is heard, only in DMs. Owner turns use `source="phone:discord"`, answered by DM; a voice note in gets a voice note out (Deepgram transcribes the Ogg/Opus attachment, ElevenLabs `opus_48000_64` reply posted as a real voice message with flag 1<<13), text in gets text out; confirmations get Yes/No buttons; important notices DMed once.
- Both phone links run side by side; each only shows confirmation buttons for requests that came through it.

## Phone link (WhatsApp — blocked by Meta until business verification, 2026-11-11, owner's choice 2026-11-11; Telegram version dropped)
- `fafnir/phone.py`: official WhatsApp Business Cloud API (Graph v23.0). Fafnir sends from its own business/test number. Incoming messages arrive at a local webhook (127.0.0.1:8787) exposed by an ngrok static domain that Fafnir starts; every delivery is HMAC-checked with the app secret, and only the owner's number (`WHATSAPP_OWNER_NUMBER`) is acted on.
- Phone messages are owner turns through the same Engine (`source="phone"`), answered by text; confirmations get Yes/No reply buttons. Important notices are pushed once (skipped if older than 6 h). WhatsApp's 24-hour rule: free text only within 24 h of the owner's last message; outside it, alerts need `alert_template` (an approved Utility template) or they stay in the Notices panel.

## Desktop app ("a face", built 2026-11-10; liquid-glass redesign 2026-11-11)
### Screen control (owner: "Fafnir controls everything; pull up = it pulls it up")
- `fafnir/screen.py` (SCREEN bus) + `fafnir/tools/screen_tools.py`: `screen_show` (list | cards | table | text on the centre stage), `screen_clear`, `screen_layout` (panel order/hidden, conversation show/small/hide), `screen_briefing`, `screen_status`. Tools run on worker threads; the app attaches a handler that forwards to the UI thread via `Bridge.sig_screen`. Without the app they report "no screen".
- `fafnir/ui/glass.py`: aurora background (or `[app] background` image), pre-blurred copy; `GlassFrame` paints the blurred crop + tint + sheen + bright edge.
- `fafnir/ui/priorities.py`: ranks the "Right now" strip and composes the default briefing (Fafnir can override it for 30 min).

- `python main.py --app` (`fafnir/ui/`): Qt (PySide6) fullscreen dark HUD. Left: today's calendar and open tasks. Centre: animated core (colour = state, swells with voice loudness). Right: notices (filled by the Tier 5 heartbeat) and recent activity. Bottom: conversation and a type box. Confirmations are Yes/No buttons and can also be answered by voice.
- `[app]` in config.toml: `screen` ("right" = the monitor right of the main one), `fullscreen`, `talk_key_label`.
- Starts with Windows via a Startup-folder shortcut (`--install-startup` / `--remove-startup`), launched with pythonw (no console). Only one instance runs; a second launch focuses the first. Errors go to `logs/app.log`.
- `fafnir/engine.py` is the shared running core (agent, ears, mouth, push-to-talk, turn queue, confirmations). The app and the terminal voice mode are both thin front ends on it.

## Architecture rule
One shared agent core (`Agent.run_turn`). Typed turns, spoken turns and heartbeat-started turns all go through it. Voice and proactivity are adapters on the edges. If agent logic ever gets written twice, unify it.

## Assumptions to revisit
- "Business management" means a local tracker until specific business tools or platforms are named.
- Speech-to-text streams audio to Deepgram live while the key is held, so the transcript is ready ~0.2s after release. If streaming fails, it uploads the clip instead.
- Home location (config.toml `home_location`) is Petit Valley, Diego Martin, Trinidad and Tobago. It's also passed to Deepgram as names to expect. Moves to the memory store in Tier 4.
