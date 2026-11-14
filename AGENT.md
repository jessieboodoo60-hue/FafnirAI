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
- **Voice (Tier 3):** Deepgram (speech-to-text) and, since 2026-11-12, Kokoro text-to-speech running offline (kokoro-onnx, model in `models/kokoro/`, not backed up or committed; voice am_onyx lowered 3 semitones, owner's pick 'A'), each behind its own seam (`make_tts` in `fafnir/voice/tts.py`; ElevenLabs remains selectable with `[voice.tts] engine = "elevenlabs"`). Voice chosen at Tier 3 and stored in config.
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

### Installing software (`fafnir/tools/installer.py`, 2026-11-12)
- `software_search` (winget + Steam store), `software_install` (winget / steam / url), `software_uninstall`, `software_jobs`. Background jobs; completion files an important notice (spoken, pushed to phone) and opens the program if asked.
- url installs: download to ~/Downloads/Fafnir, Windows Defender scan (exit code 2 = threat, abort), zips unzipped to ~/Games/<name>, main exe picked (Game.exe or name match, skipping uninstallers), Start menu shortcut, recorded in data/installs.json for uninstall. Always confirmed once (critical_if) because the link comes from the web; winget/steam follow the normal owner_asked rule.

### Screen, mouse and keyboard (`fafnir/tools/desktop_control.py`, owner granted full permission 2026-11-12)
- `screen_look` (mss screenshot of a named monitor, scaled to 1280 wide, returned as an image), `mouse_click/move/drag/scroll`, `keyboard_type/keys` (pynput), all taking coordinates in the latest screenshot's pixels; actions return a fresh screenshot by default.
- Tools can now return images: a handler returns `_images` (PNG bytes); `Registry.take_images()` hands them to the API agent (image blocks in tool_result) and the Claude Code agent (MCP image content).
- Take-over: if the cursor moved >40 px from where Fafnir left it within 90 s, the next action stops with an error. `confirm_with_owner` routes risky on-screen actions through the normal gate. Max tool steps raised (API 40, Claude Code 60 turns).

### Camera security system (`fafnir/tools/cameras.py`, owner granted full control 2026-11-12)
- Hikvision DVR at 192.168.100.191 over ISAPI with HTTP Digest auth; login in .env (HIKVISION_HOST/USERNAME/PASSWORD).
- camera_status (deviceInfo, channels + online state, HDD), camera_snapshot (/Streaming/channels/<n>01/picture, returned as images), camera_recordings (/ContentMgmt/search), camera_ptz (continuous move then stop, presets), camera_open_live, camera_api (any ISAPI path; non-GET gated, format/factoryReset/firmware/users/password/reboot always ask).
- Wake word diagnostics: every burst the listener checks is logged to logs/wake.log with its score and the cutoff.

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
- **Local upgrade, stage 2 (2026-11-13, `fafnir/resources.py`):** profiles gaming / balanced / ai_performance / auto, checked every 5 s (game = process under a games library using >= 700 MB RAM, or listed in `[performance] games`; steps down at once, up after two calm readings). Measured: to Balanced 5-6 s, to AI Performance 5.5-6.5 s, to Gaming 1.3 s; Kokoro on CPU frees 640 MB (first sound 0.42/1.17 s vs 0.16/0.26 s on GPU); 4B on CPU reads prompts at ~45 tok/s so CPU is not a brain fallback (Gaming hands requests to the cloud per `gaming_brain`); 8B + voice sits at 7.5 of 8 GB, so auto mostly picks Balanced. Tool `performance_profile`. Also fixed: two Fafnir copies could start after boot (now a Windows mutex).
- **Private vault (2026-11-13, `fafnir/tools/private_vault.py`):** owner's rule: Fafnir may use C:\Personal Information (passport, IDs) but must ask every time. One AES-256-GCM file, names hidden; `private_files_unlock` is critical (always asks, owner_asked never skips it); auto-lock 5 min, kill switch and start-up re-lock. Tested on dummy files (12 checks) before sealing the real folder.
- **Local upgrade, stage 1 (2026-11-13):** `provider = "ollama"` -> `fafnir/local_brain.py` (compact prompt, `fafnir/router.py` shortlist of ~10 tools via `fafnir/embed.py` bge-small on CPU, relevant memory only, rolling history, cloud hand-off via `cloud_assist`/vision tools with the owner's yes, logged to logs/outbound.log). Scores on tests/brain_cases.json: Claude Sonnet 40/41 at 1.58 s median; Qwen3 8B 38/41 at 1.88 s; Qwen3 4B-instruct 34/41 at 0.75 s. 8B only fits the 8 GB card at 4k context with <2.5 GB already in use. Live provider NOT switched yet.
- **Local upgrade, stage 0 (2026-11-13):** plan in docs/LOCAL_UPGRADE_PLAN.md (owner: cloud fallback allowed, asking each time). Backups now AES-256-GCM encrypted (`fafnir/vault.py`: DPAPI-wrapped key in data/vault.key, one-time RECOVERY_KEY.txt; `--restore-backup`). Git tag `pre-local` is the rollback point. `tests/brain_eval.py` + `tests/brain_cases.json` score any brain on 41 requests without executing tools; reference scores in docs/benchmarks/.
- **Conversation mode (2026-11-13):** follow-up window 12 s (15 after a question), renewed every exchange; empty/noise/echo captures reopen it instead of ending it; open-mic turns carry a system note telling the brain to answer exactly "(no reply)" if the speech wasn't meant for it (engine holds streamed text until it can tell, then stays silent and keeps listening; tested: 2 background lines ignored, follow-up and thanks answered); short goodbyes/thanks end it after the reply; a falling 520/390 Hz tone marks the end.
- **Human-like mouse/keyboard (2026-11-13):** pointer glides on a curved, eased path (0.2-0.9 s by distance, 120 updates/s, jitter, overshoot on long moves), clicks hold the button briefly, scroll goes notch by notch, typing is key by key at ~14 chars/s (40 for long text); kill switch or the owner grabbing the mouse stops a glide mid-way. Constants at the top of fafnir/tools/desktop_control.py.
- **Kill switch (2026-11-13, `fafnir/killswitch.py`):** persisted in data/killswitch.json. Engaged: Registry.run refuses every tool, engine interrupts/drops turns/closes follow-up/answers pending confirmation "no", desktop control lets go of held buttons and modifiers and stops mid-action, heartbeat ticks skip, Discord and typed/spoken requests get a short refusal. On: Ctrl+Alt+K (own combo detector; pynput GlobalHotKeys never matched because Ctrl+Alt makes K arrive as vk 75), kill phrases in typed/spoken/phone text (checked before the AI), Discord /kill, `--kill`. Off: Ctrl+Alt+Shift+K, typed /release in the app, Discord /release, `--release` (other-process changes picked up within 1 s). Never by voice.
- **Echo cancellation tried (2026-11-13):** WebRTC AEC (livekit) only removed ~50% of the JBL echo: the soundbar adds ~510 ms delay and overloads the camera mic (clipping), so muting while speaking stays the guard. WebRTC noise suppression + high-pass is applied to the wake listener (raised the owner's weakest sample from -0.49 to -0.19; noise still 0/21).
- **Wake accuracy + echo (2026-11-12):** hotwords made tiny.en write "Fafnir" for look-alikes and noise (owner saw false wakes). A wake now needs the exact name in the first 3 words, once, avg log-prob >= -0.10-0.25*sensitivity and no-speech <= 0.15+0.3*sensitivity (test: 8/9 real, 1/27 look-alike/noise). base.en/small.en were no better. On outputs not in `[voice] headphones`, the wake check is skipped while Fafnir talks (+0.8 s), the follow-up window opens 0.8 s after speech, and transcripts mostly made of its last reply are ignored. Cameras: DVR chanStatus `signal=1` marks empty inputs (13-16); 14 connected; camera 0 snapshots all connected cameras as JPEG.
- **Speed (2026-11-12):** Kokoro runs on the RTX 5060 via onnxruntime-gpu CUDA (pip CUDA/cuDNN libs, `preload_dlls`; DirectML failed on a ConvTranspose layer): first audio 0.2-0.4 s vs 1-4 s on CPU, falls back to CPU automatically. Voice model and Claude Code connection are warmed up at app start (first request was 4.8 s). Brain first word ~1 s. Wake end-of-speech pause 0.8 s.
- **Microphone choice (2026-11-12):** First `[voice.mic_for_speaker]` rules map the Windows default speaker (read live via winmm DRVM_MAPPER_PREFERRED_GET) to a mic: Primus speaker → Primus mic, JBL soundbar → camera mic (owner's request); a speaker change also reopens Fafnir's output on the new default. Otherwise `[voice] mic_preference` lists mics in order (headset "Primus Headset", then "USB 2.0 Camera"). `fafnir/voice/devices.py` polls Windows' live mic list (winmm, every 2 s) because PortAudio caches its device list; on a change the engine closes the wake stream, recorder and speaker output, re-reads PortAudio, sets the default input, and reopens. A listed mic that sends digital silence (peak <= 4 for 4 s; the owner's wireless Primus keeps its dongle plugged in when switched off) is treated as absent and probed every 10 s. Switching to a better mic waits until no exchange is in progress; losing the mic in use switches at once.
- **Wake word update (2026-11-12):** the voice-sample matcher missed the owner live (live scores 19-23 vs cutoff 14.6), so the default detector is now an offline recognizer (`fafnir/voice/whisper_wake.py`, faster-whisper tiny.en, int8 CPU, `hotwords` bias, no prompt since a prompt made it hallucinate the name in silence). Each speech burst (first 1.6 s) is transcribed locally and checked for the name; `[voice.wake] engine = "samples"` switches back. Every check is logged to logs/wake.log.
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

## Desktop app ("a face", built 2026-11-10; liquid-glass redesign 2026-11-11; desktop environment 2026-11-11)
### Fafnir's desktop (default, `[app] ui = "desktop"`)
- `fafnir/ui/desktop.py`: frameless fullscreen `QWebEngineView` hosting `fafnir/ui/web/` (index.html, desktop.css, icons.js, desktop.js = window manager/dock/menus/search/control centre/banners, apps.js = the apps). Page <-> Python via QWebChannel object `fafnir` (`DesktopBridge` slots; everything to the page is JSON on `DesktopBridge.event`). `DesktopEvents` is the Engine's Events, marshalled to the UI thread.
- All actions go through the registry: reads via `registry.read`, risky ones (open program, read secret file, delete, Wi-Fi on/off via `computer_run_command`) via `registry.run(..., confirm=engine.confirm)` so the normal confirmation sheet appears. Brightness uses WMI; volume/media use `computer_media`.
- Offline: no CDN, original inline-SVG icons, system font stack, no Apple names or assets. Window positions, desktop icon positions and wallpaper live in the page's localStorage.
- `screen_open(app, path)` opens an app window; `screen_show` opens a Viewer window; `screen_layout` maps the old panel names to apps (today->calendar, email->mail, activity->fafnir) and tiles them.
- `[app] ui = "glass"` falls back to the Qt panel screen below.
### Panel screen (`[app] ui = "glass"`)
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
