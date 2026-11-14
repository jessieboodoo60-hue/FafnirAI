# Fafnir

A voice-first assistant harness, built tier by tier. See [AGENT.md](AGENT.md) for the spec.

## Setup (Windows, PowerShell)

```
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Open `.env` and set `ANTHROPIC_API_KEY`.

## The screen (Fafnir runs it)

The app is Fafnir's own desktop: quiet frosted-glass windows over a painted dusk wallpaper (or your own image: `background` under `[app]` in `config.toml`, or pick a wallpaper in Settings).
- **Menu bar** (top): Fafnir's menu, File / Window / Help, Fafnir's status, Search, Control Centre, notices (with a badge) and the clock.
- **Dock** (bottom): Fafnir, Mail, Calendar, Tasks, Notices, Files, Business, Settings. Icons grow under the mouse; a dot marks open apps.
- **Windows**: drag by the title bar, resize from any edge, the three buttons close / minimise (into the dock) / maximise; double-click the title bar to maximise. Right-click almost anything for more options.
- **Other monitors**: drag a window past the edge of Fafnir's screen and it carries on as a normal window on your other monitor (or right-click its title bar → *Move to another screen*). Right-click it there → *Move back to Fafnir's screen*.
- **Fafnir** app: the orb (colour = state, swells with your voice) and today's conversation, with a type box.
- **Files**: browse your folders, quick-look text files, open things, "ask Fafnir about this". Opening programs, reading secret files and moving to the Recycle Bin ask for your OK first, as always.
- **Search** (`Ctrl+Space` or the magnifier): apps, files, notes, memory and business items, or send what you typed straight to Fafnir.
- **Control Centre**: Wi-Fi and Bluetooth status, do not disturb, display brightness (where the monitor allows it), volume and media keys. Turning Wi-Fi on or off asks first; Bluetooth opens Windows settings.
- **Right now** (top left): a one-line briefing and the most important things at this moment.
- **Notifications** slide in at the top right; the Notices app lists them (dismiss, clear all, pause).
- Say or type "pull up my emails", "show me today", "open my calendar", "open files in Downloads" and Fafnir opens the window; "clear the screen" closes what it put up.

The older panel screen is still there: set `ui = "glass"` under `[app]`.

## The app

```
.venv\Scripts\python main.py --app
```

Opens Fafnir fullscreen on your right-hand monitor (change `screen` under `[app]` in `config.toml`). It also starts by itself when you log in to Windows, and it's in the Start menu as **Fafnir**. Hold M5 to talk or type in Fafnir's window; confirmations show Yes/No buttons. **Esc** stops Fafnir talking, **Ctrl+Space** opens Search, **Ctrl+W** closes a window, **Ctrl+M** minimises it, **F11** toggles fullscreen, **Ctrl+Q** quits. Launching it again just brings the open one to the front.

To stop it starting with Windows: `.venv\Scripts\python main.py --remove-startup` (and `--install-startup` to turn it back on). If the app misbehaves, errors are in `logs\app.log`.

## Your phone (Discord)

Fafnir DMs you on Discord and you can DM it back from anywhere (the PC must be on with the app running).
1. Go to discord.com/developers/applications → **New Application** (name it Fafnir) → **Bot** → **Reset Token**, copy it into `.env` as `DISCORD_BOT_TOKEN=...`.
2. **OAuth2 → URL Generator**: tick `bot`, open the generated link, and add the bot to a small private server of your own (Discord only lets bots DM people they share a server with).
3. Restart the app; it shows `DM the bot ... /pair 123456`. Send exactly that to the bot in a DM. Only your account can use it from then on.

**Voice notes:** send Fafnir a voice note and it answers with a spoken voice note (Deepgram listens, ElevenLabs speaks); send text and it answers in text. Risky actions you ask for from the phone come with Yes / No buttons. Important notices are DMed to you (`send` under `[phone]` in `config.toml`). `/pause` and `/resume` control notices.

## Your phone (WhatsApp) — waiting on Meta business verification

Fafnir can message you on WhatsApp and you can message it back from anywhere (the PC must be on with the app running). It uses Meta's official WhatsApp Business Cloud API, so Fafnir writes from its own number (Meta's free test number), not yours. Setup steps are in the chat where this was built; in short: a Meta developer app with WhatsApp, a permanent system-user token, your number added as a test recipient, a free ngrok static domain, and the webhook pointed at `https://<your-domain>/webhook` with the verify token the app shows. Secrets go in `.env` (see `.env.example`), the rest under `[phone]` in `config.toml`.

Message it like you'd talk to it. Risky actions you ask for from the phone come with Yes / No buttons. Important notices are sent to WhatsApp; WhatsApp only allows free-form messages within 24 hours of your last message, so for alerts after that, set `alert_template` to an approved template (otherwise they wait in the Notices panel). `/pause` and `/resume` control notices.

## Conversations and backups

Fafnir remembers recent conversations across restarts: the app reopens with today's chat, and new conversations start knowing the last day or so. Ask "what did we talk about on Monday?" for older ones. The log is in `data\conversations\` (one file per day; delete any you like).

Your personal data (`data\`, `.env`, `config.toml`) is zipped to `D:\FafnirBackups` once a day while the app runs, keeping the last 14 copies; you get a notice only if a backup fails. Run one by hand with `.venv\Scripts\python main.py --backup`. The code itself is on GitHub.

Backups are **encrypted** (AES-256; they hold your API keys and Google login) and saved as `fafnir-<date>.zip.enc`. The key lives in `dataault.key`, locked to your Windows account on this PC. A one-time copy is in `RECOVERY_KEY.txt`: move that key into a password manager or onto a USB stick, then delete the file. Without it, a backup can't be opened on another PC or after reinstalling Windows.

Restore: `.venv\Scripts\python main.py --restore-backup D:\FafnirBackups\<file>` unpacks into a new `FafnirAI-restored-...` folder next to the project (it never overwrites live data); copy what you need back. Add `--key <recovery key>` on another PC, or `--to <folder>` to choose where.

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

Fafnir remembers lasting things you tell it (preferences, people, business details) and knows them the next time you start it. Say "remember..." to save something on purpose, or "forget..." to remove it. Type `/memory` to see everything stored.

- **Encrypted:** memory lives in `data/memory.vault` (AES-256, same key as backups); nothing in it is readable on disk.
- **Found by meaning:** "who does my taxes" finds "Accountant is Mr. Ramdeen" even with no shared words. The local brain is given only the few facts relevant to each request.
- **No duplicates, no secrets:** a fact that means the same as a stored one is refused, and passwords, keys, and card, ID, passport or account numbers are never stored.
- **Conversation summaries:** each past day is condensed into a few lines by the small local model (on this PC) so Fafnir can recall "what did we do on Tuesday".
- **Your controls:** `python main.py --memory-export memory.md` writes everything as readable text; edit or delete lines, then `python main.py --memory-import memory.md` to load your version (delete the text file afterwards).

## Google (Calendar, Gmail, Drive, Docs, Sheets, Contacts, Tasks, YouTube)

In Google Cloud Console, in your Fafnir project: enable **Google Calendar API**, **Gmail API**, **Google Drive API**, **People API**, **Google Tasks API** and **YouTube Data API v3**, add your Gmail as a **test user** on the consent screen, and create a **Desktop app** OAuth client. Save its JSON as `data\google_credentials.json`. The first time you ask about your calendar or email, a browser opens: pick your account and allow access. Google asks again about once a week while the app is in testing mode.

## Voice mode

Add `DEEPGRAM_API_KEY` and `ELEVENLABS_API_KEY` to `.env`, then:

```
.venv\Scripts\python main.py --voice
```

Hold the talk key, speak, release. Press it again while Fafnir is talking to cut it off. Typing still works in voice mode, and typed replies are spoken too. `/stop` silences it, `/reset` clears the conversation.

**Wake word:** say "Fafnir" instead of holding the key, then talk; Fafnir stops listening when you pause for a second. Say it while Fafnir is talking to cut it off. It runs entirely on this PC: a small offline speech recognizer listens for the name (no voice recordings needed). Record them once (close the app first so the mic is free):

```
.venv\Scripts\python main.py --train-wake
```

It asks you to say "Fafnir" 6 times. The recordings stay in `data\wake\` (not uploaded anywhere). If it triggers by mistake, lower `sensitivity` under `[voice.wake]` in `config.toml`; if it misses you, raise it or re-record.

**Speech recognition is local:** what you say is turned into text by Whisper on this PC (`[voice.stt] engine = "local"`); nothing you say leaves the computer for that step. It makes a few more mistakes than Deepgram (3.9% of words against 2.4% on the test clips) and starts transcribing during your closing pause so the wait stays short. Switch back in Settings > Voice or with `engine = "deepgram"`. Say "Fafnir" or "Hey Fafnir". **On headphones you can just start talking to cut Fafnir off** (`barge_in`); on the JBL you still need the talk key.

**Conversation mode:** after you say "Fafnir" once, just keep talking: after every reply Fafnir listens for 12 seconds more (15 after a question) without the wake word, and each exchange renews it. Noise and its own echo don't end the conversation, talk that isn't meant for it (TV, someone else) is quietly ignored, and "thanks, that's all" / "goodbye" / "never mind" ends it. A soft falling two-note tone means it has stopped listening; say "Fafnir" to start again. Length: `follow_up_seconds` under `[voice.wake]`.

**Security and privacy (details: `docs/SECURITY.md`):**
- **Secrets** (API keys, bot tokens, the camera password) live in an encrypted vault, not `.env`. Add or change one with `python main.py --secret-set NAME` (hidden input), or put them in a `.env` and run `python main.py --secrets-import`, then delete the `.env`. `--secrets-list` shows names only.
- **Encrypted at rest:** secrets, memory, conversations, the Google login, private documents and backups.
- **Action log you can check:** `python main.py --verify-log` reports whether any entry in `logs/actions.log` was edited or removed.
- **What left the PC:** Settings > Privacy, or ask "what have you sent out?", or read `logs/outbound.log`.
- **Runaway protection:** a tool that changes things can run 8 times a minute at most.
- **Retention:** conversations older than 365 days are deleted (`[privacy]` in `config.toml`). `python main.py --conversations-export <folder>` writes readable copies.
- **Health check:** `python main.py --security-check`. **New key:** `python main.py --rotate-key` (with Fafnir closed).

**Health and self-repair:** every 30 seconds Fafnir checks the processor, memory, disks, graphics card, the local model server and the model's speed, the microphone and voice, the phone links, backups, the kill switch and the action log, and counts security signals (messages from strangers, planted instructions, loops stopped). You get a notice when something breaks and when it recovers. It repairs only three things by itself: restarts Ollama, reopens the microphone and speaker, and steps down to the smaller model if the big one has become slow; never anything to do with security, and nothing at all while the kill switch is on. If the local model is down, a request offers the cloud brain instead of failing. See it in Settings > Health, ask "how are you doing?", run `python main.py --health`, or read `logs/health.log`.

**Performance profiles (gaming-aware):** Fafnir decides what it keeps on the graphics card. `auto` (default) switches to **Gaming** while a game runs (local models unloaded, voice made on the processor, about 0.3-0.9 s slower to start speaking), otherwise **Balanced** (the 4B local model + voice) or **AI Performance** (the 8B model, only when about 6 GB of graphics memory is free). Say "gaming mode", "balanced mode", "AI performance mode" or "back to auto", or set `profile` under `[performance]` in `config.toml`. With the cloud brain (today's setting) only the voice moves. With the local brain, requests made in Gaming go to the cloud brain according to `gaming_brain` (`ask` each time, `cloud`, or `off`).

**Private documents vault:** `C:\Personal Information` (passport, IDs) is kept as one encrypted file, `personal-information.vault`; file names are hidden too. Fafnir can use the documents only after asking you, every single time, even if you just asked for the thing that needs them. On your yes they're decrypted into `_unlocked` inside that folder; they lock again (re-encrypted, open copies wiped) when Fafnir is done, after 5 minutes at most, when the kill switch is hit, and at start-up if a crash left them open. Add or remove documents by asking ("add the file on my desktop called permit.pdf to my vault", "take my old permit out of the vault", "delete the tax scan from my vault", "what's in my vault"); Fafnir asks before each one. Adding deletes the unencrypted original once the vault copy is verified, unless you say to keep it. Or do it yourself in a terminal: `python main.py --vault-list`, `--vault-add <file or folder> [--keep-original] [--replace]`, `--vault-take-out <name> <folder>` (saves an unencrypted copy), `--vault-remove <name>` (deletes for good). The vault uses the same key as backups, so keep the recovery key safe.

**Emergency kill switch:** stops everything at once and keeps Fafnir stopped (even after a restart) until you release it. While it's on, every action is blocked (mouse/keyboard, programs, files, email, cameras...), speech and the current reply are cut off, queued requests are dropped, heartbeat notices stop, and any held mouse button or key is let go.
- Turn ON: **Ctrl+Alt+K**, or say/type "kill switch", "emergency stop" or "stop everything", or send `/kill` to the Discord bot, or run `python main.py --kill`.
- Turn OFF: **Ctrl+Alt+Shift+K**, or type `/release` in the app, or send `/release` on Discord, or run `python main.py --release`. (Not by voice, since anyone in the room could say it.)

**Wake word accuracy:** only a confident, clearly spelled "Fafnir" in the first three words wakes it; look-alikes ("Fifty", "Daphne") and noise are rejected. `sensitivity` under `[voice.wake]` (0-1, default 0.4) trades missed wakes for false ones; every check is in `logs/wake.log` with its confidence. **On the JBL (any output not listed in `headphones`):** Fafnir doesn't listen for its name while it's talking, waits a bit longer before its follow-up listening, and ignores anything it hears that's mostly its own last reply. On speakers you can't interrupt it by voice; use the talk key. The mic is also cleaned with WebRTC noise suppression before listening (`noise_suppression` under `[voice]`).

**Microphone:** the mic follows the speaker Windows is playing through: Primus headset as the output → Primus mic; JBL soundbar → camera mic. Fafnir's voice also moves to the new speaker. Rules are under `[voice.mic_for_speaker]` in `config.toml`. If no rule matches (or that mic isn't plugged in), `mic_preference` under `[voice]` decides: headset first, then camera. A mic that's listed but gives pure silence (headset switched off, dongle still plugged in) counts as unplugged after 4 seconds; Fafnir checks it again every 10 seconds and goes back to it when it gives sound. Checked every 2 seconds; a switch waits until the current exchange is over, unless the mic in use disappears.

To set the talk key: in Razer Synapse, assign the M5 macro key to F13 (or any unused key), then run `.venv\Scripts\python main.py --learn-key`, press it, and copy the `ptt_key` line it prints into `config.toml`.

## Installing and removing software

Ask Fafnir to install or uninstall an app or game ("install Pokemon Reborn", "install Spotify", "uninstall 7-Zip"). It uses winget (Windows' package manager), Steam (you click Steam's Install button once), or the official direct download (unzipped into `C:\Users\jessi\Games` with a Start menu shortcut). Installs run in the background; Fafnir tells you when it's done and opens it. Direct downloads ask once (showing the website) and are scanned with Windows Defender before anything runs.

## Screen, mouse and keyboard

Fafnir can see your monitors (main, right, above) and use the mouse and keyboard to do things that only work on screen: games, app menus, dialogs, websites and settings pages. It looks, acts, then checks the result. You can keep using your mouse while it works: if you move it during one of Fafnir's movements, Fafnir pauses until you stop and then carries on. **Press Esc to stop it** (or use the kill switch). Risky on-screen actions you didn't directly ask for (deleting, sending, buying, uninstalling, changing settings) ask for your OK first, and it won't type passwords or card numbers unless you give them to it for that purpose.

## Camera security system (Hikvision DVR)

Fafnir can check your cameras ("show me the cameras", "what's happening at the gate?"), list cameras and disk health, search recordings, move pan-tilt-zoom cameras, open the DVR's live view, and change DVR settings. Add the DVR login to `.env` (`HIKVISION_HOST`, `HIKVISION_USERNAME`, `HIKVISION_PASSWORD`) and restart. Settings changes follow the normal confirmation rules; formatting disks, factory resets, firmware, users/passwords and reboots always ask first.

## Fafnir's voice (Kokoro)

Fafnir speaks with Kokoro, a free speech model that runs on this PC: no API key and no character limits (Discord voice notes use it too). The voice is set under `[voice.tts]` in `config.toml`: `kokoro_voice` (e.g. `am_onyx`, `bm_george`, `am_fenrir`, or a blend like `am_onyx+bm_george`), `kokoro_pitch` (semitones; negative is deeper), `kokoro_speed` and `kokoro_reverb`. The model files live in `models\kokoro\` (about 350 MB). Set `engine = "elevenlabs"` to go back to ElevenLabs.
