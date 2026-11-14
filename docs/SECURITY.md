# Fafnir security: what is protected, how, and what is still exposed

Written 2026-11-13. No system is impossible to break into. This lists what was built, how each
control was tested, and the risks that remain. Check the current state any time with:

```
.venv\Scripts\python main.py --security-check
```

## What Fafnir holds that is worth protecting

| Asset | Where | Protection |
|---|---|---|
| API keys, bot tokens, camera password | `data/secrets.vault` | AES-256-GCM; never in the process environment, logs or prompts |
| Passport, IDs | `C:\Personal Information\personal-information.vault` | AES-256-GCM; Fafnir must ask before every unlock; auto-locks |
| Memory (facts about you, day summaries) | `data/memory.vault` | AES-256-GCM; refuses to store secrets |
| Conversations | `data/conversations/*.jsonl` | every line AES-256-GCM; deleted after 365 days |
| Google login | `data/google_token.json`, `google_credentials.json` | AES-256-GCM |
| Backups | `D:\FafnirBackups\*.zip.enc` | AES-256-GCM |
| Record of what Fafnir did | `logs/actions.log` | hash-chained: edits and deletions show |

One 256-bit key protects all of these. It is stored wrapped by Windows DPAPI (tied to your Windows
account on this PC; Windows uses the TPM for that where the account is set up with one) and exists
unwrapped only in Fafnir's memory while it runs. The recovery key is the same key, for use on
another PC. `python main.py --rotate-key` replaces it.

## Threats and what stands in the way

| Threat | Controls | Tested |
|---|---|---|
| Someone copies files off the PC or a backup drive | Everything above is encrypted at rest | Files checked to contain no readable text; wrong key rejected |
| A program Fafnir launches reads its secrets | Secrets are no longer environment variables | A launched process was checked: none visible |
| Text in an email, web page or file tells Fafnir to do something ("prompt injection") | Read content is data, not instructions (prompt rule); risky tools go through the confirmation gate; critical ones always ask; instruction-like text in tool results is flagged to the local model and logged | 4 of 4 planted instructions flagged, 0 of 4 normal messages |
| The AI gets stuck in a loop sending or deleting | Rate limit: 8 calls a minute for tools that change things, 40 for read-only, 120 overall | A looping send tool was stopped after 8 |
| The AI misbehaves in some other way | Kill switch (Ctrl+Alt+K, voice, Discord, command line): blocks every tool, survives restart, checked before the AI sees a request | Every trigger tested |
| Someone else messages the bot | Discord: only your paired account is answered; strangers are ignored. WhatsApp: only your number, and the webhook checks Meta's signature | Pairing tested when built |
| Someone edits the action log to hide something | Hash chain; `--verify-log` | Editing or deleting one line is detected at that line |
| Private documents read without you knowing | The unlock tool always asks you, aloud and on screen; locks after 5 minutes, on kill switch, and at start-up | 12 checks on dummy files |
| Something leaves the PC without you knowing | `logs/outbound.log` and the Privacy panel in Settings list every outside service contacted, how often and roughly how much | Summary tested |
| Network attack on Fafnir's own services | Ollama and the WhatsApp webhook listen on this PC only (127.0.0.1); the webhook is reached only through the ngrok tunnel | Checked by `--security-check` |

## What is still exposed, whatever we do

1. **Fafnir runs as you.** Malware running under your Windows account can ask Windows to unwrap
   the same key, read Fafnir's memory while it runs, or drive Fafnir's tools. The fix for that is
   keeping the PC itself clean, not anything inside Fafnir. Running Fafnir under a separate,
   limited Windows account would narrow this; it has not been done.
2. **Fafnir can change itself.** It has full control of the PC, including its own code and the
   kill switch, while the switch is off. The gate and the critical-action list make that require
   your yes, but a determined or badly confused model with a cleverly disguised command could get
   around pattern checks.
3. **The local model is easier to fool than Claude.** Injection flagging is a pattern match; it
   will miss rewordings. The confirmation gate is the real protection, so don't approve an action
   you didn't expect.
4. **Deleted files may survive on the disk.** Files are overwritten before deletion, but SSDs and
   Windows keep old copies in places software can't reach. Only full-disk encryption (BitLocker)
   closes that, and it also protects the DPAPI key store if the disk is removed.
5. **Outside services see what you send them.** Google, Discord, WhatsApp/Meta, and Anthropic when
   you approve a cloud hand-off, each see the content of that request. The cloud brain is given
   documents and camera pictures when it handles those requests.
6. **The hash chain doesn't stop a full rewrite.** Someone with access to the PC could rebuild the
   whole log with a fresh, consistent chain. It catches quiet edits, not a wholesale replacement.
7. **One key for everything.** Simpler to keep safe, but if the recovery key leaks along with a
   backup, everything in that backup is readable. Rotate the key if you think it has leaked.
8. **Voice is not identity.** Anyone in the room can talk to Fafnir. Risky actions still need a
   spoken yes, but that yes isn't tied to your voice. Speaker verification has not been built.

## Yours to do (Fafnir never changes Windows security settings)

- **Turn on BitLocker / device encryption** for C: and D:. Check: `manage-bde -status C:` in an
  administrator PowerShell.
- **Confirm Secure Boot and the TPM**: `Confirm-SecureBootUEFI` and `Get-Tpm` in an administrator
  PowerShell.
- **Keep the recovery key off this PC** (password manager or USB stick).
- **Delete the old plain backups** in `D:\FafnirBackups` (`fafnir-*.zip` without `.enc`): they
  contain your keys and Google login in the clear.
- **Rotate the API keys that were in plain text**: the old project folder was synced to OneDrive
  for a while, and the keys sat in a plain `.env` until today. New Anthropic, Deepgram, Discord,
  WhatsApp and camera credentials would make those old copies worthless.
- **Home network**: put the DVR and cameras on a guest or separate network if your router allows,
  and change the DVR's password if it is still a default.

## Left out on purpose

- **Roles and permissions**: there is one user.
- **A hardware security module**: Windows DPAPI with the TPM does the same job for one PC.
- **Passkeys / web login**: Fafnir has no web login. The phone link is tied to one Discord account.
- **TLS between Fafnir's own parts**: they talk inside one process or over 127.0.0.1. Every
  connection to an outside service already uses HTTPS.
