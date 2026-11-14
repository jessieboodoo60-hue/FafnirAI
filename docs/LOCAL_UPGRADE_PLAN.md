# FafnirAI local upgrade: audit, baseline and staged plan

Written 2026-11-13. Nothing in Fafnir's code was changed to produce this. Every number below was
measured on this PC today unless marked "estimate".

## 1. What this PC actually is

| Part | Found | Note |
|---|---|---|
| CPU | Intel Core Ultra 5 **225F** (10 cores, 10 threads) | You wrote 225K; Windows reports 225F. Same class, no difference for this plan. |
| RAM | 31.7 GB (about 10 GB free in normal use) | Fine for CPU-side models (speech, embeddings). |
| GPU | NVIDIA RTX 5060, 8 GB | **The bottleneck.** 2.0 GB is in use by Windows and apps with nothing heavy open; 4.8 GB with OBS open. |
| Disk | C: 122 GB free, D: 65 GB free | Enough. Models so far: Qwen3 8B 5.2 GB, Qwen3 4B 2.6 GB. |
| Ollama | Already installed, v0.40.16 | Had mistral:7b and dolphin-llama3:8b. I added qwen3:8b and qwen3:4b for the tests. |
| TPM / BitLocker / Secure Boot | Could not be read without administrator rights | You'd need to check these yourself (section 7). |

## 2. Fafnir today (what must keep working)

About 10,800 lines of Python in separate modules already: brain (`llm.py`, `agent.py`,
`claude_code.py`), tools (`fafnir/tools/`, 87 tools behind one registry and one confirmation gate),
voice (`fafnir/voice/`), memory (`memory.py`, one Markdown file), heartbeat, phone links (Discord,
WhatsApp), the desktop app (`fafnir/ui/`), kill switch, backups, action log.

- **Brain:** Claude Sonnet through your Claude subscription. One line in `config.toml`
  (`provider`) already switches between two brains, so a third (local) one fits the same seam.
- **Already local:** wake word (Whisper tiny), voice (Kokoro on the GPU), noise suppression,
  memory file, logs, backups.
- **Not local today:** the brain (Anthropic), speech-to-text (Deepgram), Google, Discord, WhatsApp, web search.

## 3. Baseline measurements

| What | Today (cloud brain) | Qwen3 8B local | Qwen3 4B local |
|---|---|---|---|
| Brain: time to first word | ~1.0 s | 0.1–0.2 s bare; 2–5 s with a tool list | ~0.1 s bare |
| Brain: speed | n/a | **52–73 tokens/s** at 4k context; **7–8 tokens/s** at 8k+ context or with OBS open | **87–96 tokens/s** at 8k context |
| Graphics memory needed | 0 | 5.5 GB at 4k context, 6.2 GB at 8k | 3.8 GB at 8k |
| Cold start | ~3 s (connect) | 3–23 s (load) | ~3 s |
| Right tool chosen, 8 test requests, all 87 tools | not re-run | **7 of 8**, but 3–18 s each (one took 212 s) | not run |
| Voice: first sound | 0.2–0.4 s | same | same |
| Wake check | ~0.25 s | same | same |

Three findings decide the design:

1. **Fafnir's prompt is far too big for a local model.** System prompt plus 87 tool descriptions is
   **13,742 tokens**. Qwen3 8B at full speed fits about 4,000. The prompt has to shrink by roughly
   five times: a short core prompt, and only the 8–15 tools relevant to each request.
2. **8 GB of graphics memory holds one heavy thing at a time.** Qwen3 8B fills it. With OBS or a
   game open it overflows into ordinary RAM and drops to 7 tokens/s, which is unusable for
   conversation. The 4B model is the one that coexists with other things.
3. **Qwen3 8B is text-only.** "What cars are in the driveway" and controlling the screen by looking
   at it need a model that sees pictures. A local vision model (about 6 GB) cannot be loaded next to
   the 8B; it would have to be swapped in (several seconds), or those requests go to the cloud with
   your consent.

Also seen: in one run the 8B answered 17 × 23 as 431 (it's 391). Expect more small errors than now.

## 4. Honest expectations

- Simple requests (open an app, a timer, a quick fact, calendar lookups) should be **as fast or
  faster** than today once the prompt is small, and fully private.
- Long multi-step work (the OBS alert test you ran today, installing software, anything driven by
  screenshots) will be **noticeably worse** on an 8B model than on Claude. That is the limit of
  local models this size, not something tuning removes.
- So the plan is **local first, with an optional cloud fallback you control**, not local only.
  Section 8 asks how you want that set.

## 5. Target architecture

```
 mic -> noise suppression -> wake word "Hey Fafnir" -> local speech-to-text
                                                             |
                                    +------------------------v-----------------------+
                                    |  ROUTER (tiny, local): which tools? which brain? |
                                    +---+---------------+----------------+-----------+
                                        |               |                |
                                  Qwen3 4B         Qwen3 8B        cloud (Claude)
                                  quick/gaming     default         only with consent:
                                                                   pictures, long tasks
                                        +---------------+----------------+
                                                        |
        memory service  <----->  AGENT CORE (one loop, unchanged gate, kill switch)  <-----> tools
        (encrypted SQLite,                              |                                   (allowlist,
         local embeddings)                              v                                    timeouts)
                                              Kokoro voice -> speaker

 RESOURCE MANAGER watches games/OBS/GPU memory and picks the profile:
   Gaming: nothing heavy on the GPU    Balanced: 4B + voice    AI Performance: 8B + voice
 SECURITY + HEALTH: secrets vault, encrypted backups, audit log, outbound-request log, health checks
```

Each box is its own module behind a small interface, so any one can be replaced.

## 6. Staged roadmap

Every stage: built behind a switch in `config.toml`, measured before and after, committed
separately, and reversible by flipping the switch back (the current Claude setup stays intact
throughout). I'll report real numbers at the end of each stage, including the bad ones.

| Stage | What | Done when (measured) | Rough size |
|---|---|---|---|
| **0. Safety net** | Encrypted, restorable backup of `data/`, `.env`, config; git tag `pre-local`; a fixed test set of ~40 real requests with expected tool/answer | Backup restores on a scratch folder; test set runs against today's brain as the reference score | Small |
| **1. Local brain** | `provider = "ollama"`; compact core prompt; tool router (pick 8–15 tools per request); streaming; non-thinking by default, thinking on request; context trimming | Test-set score and latency vs the Claude reference; first word under 1.5 s for simple requests; prompt under 3,000 tokens | Large |
| **2. Profiles and gaming awareness** | Resource manager; Gaming / Balanced / AI Performance; unload models when a game or OBS needs the GPU; reload after | FPS and GPU memory measured in one of your games with each profile; model reload time | Medium |
| **3. Memory v2** | Encrypted SQLite store, local embeddings on the CPU, relevance scoring, dedupe, summaries of old conversations, view/correct/export/delete; your current `memory.md` imported | Recall test (facts found / missed), tokens used per request, survives restart, export and delete work | Medium |
| **4. Local voice** | Local speech-to-text (replaces Deepgram), "Hey Fafnir", voice-activity detection, interrupt by talking on the headset | Word accuracy vs Deepgram on your recordings; wake hits/false wakes; delay to first transcript | Medium |
| **5. Security hardening** | Secrets out of plain `.env` into Windows' protected store (DPAPI, TPM-backed where Windows provides it); encrypted memory and backups; tamper-evident action log; outbound-request log and dashboard panel; tool allowlists, timeouts and limits per tool; retention rules | Each control tested (for example: memory file unreadable outside Fafnir; every outbound request appears in the log); written threat model with what is still exposed | Medium |
| **6. Health and recovery** | Monitor CPU/GPU/RAM/disk, model speed, services, failed automations; restart a crashed part; alerts | Kill each service on purpose and watch it recover; alerts fire | Small |
| **7. Smarter features** | Document/PDF question answering (RAG), optional local vision model swapped in on demand, uncertainty and "fact vs guess" wording | Answers on a test document set; picture questions answered locally and how long the swap takes | Medium |
| **8. Smart-home interfaces** | Interfaces only (Home Assistant, lights, sensors, alarm). Nothing activated. Separate approval rules written down | Design reviewed by you | Small |

## 7. Security: what I will and won't claim

**Will build and test:** secrets vault, encryption at rest for memory/backups/config, the existing
confirmation gate and kill switch kept as the last line, "everything read is data, not instructions"
kept and extended to retrieved documents, per-tool limits, audit log, outbound-request log.

**Yours to do (I can't change system security settings):** turn on BitLocker, confirm Secure Boot
and TPM in Windows Security, firewall rules, router/network separation. I'll give exact steps and a
check script that reports their state.

**Not worth building for a one-person PC assistant, and why:** role-based permissions (one user),
a hardware security module (the TPM via Windows covers the same job), passkeys (no web login exists;
the phone link is already tied to your one Discord account).

**Residual risks that remain whatever we do:** Fafnir runs as your Windows user, so anything that
runs as you can read what Fafnir can; a local model is easier to fool with injected text than
Claude, so the gate matters more, not less; Discord, WhatsApp and Google stay outside your control.

## 8. Decisions I need from you

1. **Cloud fallback.** Allowed for (a) pictures and (b) long multi-step tasks, asking you each time,
   or never (those features then get weaker or stop)?
2. **Cameras.** Your brief says not to activate security-system integration yet, but Fafnir already
   controls the Hikvision DVR. Keep it working as now, or switch it off until stage 8?
3. **Wake word.** Change from "Fafnir" to "Hey Fafnir" only, or accept both?

## 9. Rollback

One line (`provider` in `config.toml`) returns to today's brain at any point. Each stage is its own
commit on GitHub; `git checkout pre-local` returns the code to today's state; stage 0's backup
restores the data.

## 10. Progress log

- **Stage 0 done (2026-11-13):** encrypted backups (restore tested), tag `pre-local`, test set; Claude reference 40/41, 1.58 s median.
- **Stage 1 done (2026-11-13):** local brain behind `provider = "ollama"`. Qwen3 8B 38/41 at 1.88 s; Qwen3 4B 34/41 at 0.75 s. Not switched on.
- **Stage 2 done (2026-11-13):** profiles and gaming awareness. Switch times 1.3-6.5 s. Findings: the CPU is not a usable brain fallback (4B reads prompts at ~45 tokens/s); the 8B plus voice uses 7.5 of 8 GB, so `auto` mostly lands on Balanced. FPS impact in a real game has NOT been measured yet (no game was running during the tests).
- **Stage 3 done (2026-11-13):** encrypted semantic memory. Recall 16/16 (top-8) on questions sharing no keywords with the fact, against 9/16 for the old keyword search; 5 ms per lookup; relevant-only prompt ~80 tokens instead of ~300 for the whole memory. Day summaries made by the local 4B (3 days in 12 s); their quality is modest (they lean on the start and end of a long day).
- **Brain switched to local Qwen (2026-11-13)** at the owner's request, with the controls in Settings.
- **Stage 4 done (2026-11-13):** local speech-to-text (Whisper small.en on the CPU: 3.9% word errors against Deepgram's 2.4%; wait after speech 0.28 s thanks to transcribing during the closing pause), "Hey Fafnir", talk-over interrupt on headphones. Tested on synthetic clips and simulated audio, not yet on the owner's live voice.
