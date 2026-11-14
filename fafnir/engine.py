"""The running assistant: one agent core plus ears, mouth, push-to-talk and the turn queue.

Front ends (the voice console, the desktop app) create an Engine, give it an `Events`
listener, and feed it typed text. Spoken and typed turns, confirmations and interruptions
all go through here, so no front end ever re-implements agent logic.

Events are called from background threads; front ends must be thread-safe (the desktop
app forwards them through Qt signals).
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

from .agent import Agent
from .common import cost_line, is_yes
from .config import Config
from .llm import LLMError
from .tools.control import CONFIRM_MARK
from .voice.sentences import SentenceChunker
from .voice.stt import STTError

MIN_SECONDS = 0.3
CONFIRM_TIMEOUT = 60  # seconds to wait for a yes/no before treating it as no


class Events:
    """Override what you need. All methods are called from background threads."""

    def heard(self, text: str) -> None: ...                 # transcript of a spoken turn
    def reply_start(self) -> None: ...
    def reply_text(self, delta: str) -> None: ...           # streamed reply text
    def tool(self, name: str, args, result: str | None, is_error: bool) -> None: ...
    def reply_end(self, interrupted: bool, stats: "TurnStats") -> None: ...
    def notice(self, text: str) -> None: ...                # small status ("didn't catch that")
    def error(self, text: str) -> None: ...
    def confirm_request(self, action: str) -> None: ...
    def confirm_done(self, approved: bool, timed_out: bool) -> None: ...


@dataclass
class TurnStats:
    transcribe: str = ""     # e.g. "0.2s (live)"
    first_word: float | None = None
    first_audio: float | None = None
    cost: str = ""


class _NeverDone:
    done = False


NO_REPLY = "(no reply)"   # what the brain says when open-mic speech wasn't meant for it
NOT_ADDRESSED_NOTE = (
    "\n\n[System note: heard through the open mic during an ongoing conversation, without your "
    "name. If it's clearly not meant for you (background talk, TV, someone else in the room, "
    "a half-sentence), reply with exactly " + NO_REPLY + " and nothing else. Otherwise answer normally.]")
GOODBYES = ("that's all", "thats all", "that is all", "that's it", "thats it", "goodbye", "good bye",
            "bye", "never mind", "nevermind", "stop listening", "go to sleep", "i'm done", "im done",
            "we're done", "were done", "no thanks", "nothing else", "that'll be all", "thatll be all")


def is_goodbye(text: str) -> bool:
    """Does the owner want to end the conversation? ("thanks, that's all", "goodbye")."""
    import re
    t = " ".join(re.findall(r"[a-z']+", text.lower()))
    words = t.split()
    if any(g in t for g in GOODBYES) and len(words) <= 8:
        return True
    return len(words) <= 4 and bool(re.search(r"\b(thanks|thank you|cheers)\b", t))


def _drain_queue(q: queue.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def mentions_name(text: str, name: str) -> bool:
    """Does a transcript start with something like the assistant's name? Speech recognition
    may spell an unusual name oddly ("Fafner", "Fav near"), so match loosely, and only in
    the first few words."""
    import difflib
    import re
    words = re.findall(r"[a-z']+", text.lower())[:4]
    target = name.lower()
    candidates = words + ["".join(words[i:i + 2]) for i in range(len(words) - 1)]
    return any(difflib.SequenceMatcher(None, c, target).ratio() >= 0.6 for c in candidates)


def _beep(freq: int, ms: int) -> None:
    try:
        import winsound
        threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()
    except Exception:
        pass


class Engine:
    def __init__(self, cfg: Config, agent: Agent, events: Events, voice: bool = True):
        self.cfg, self.agent, self.events = cfg, agent, events
        self.name = cfg.assistant.name
        self.turns: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False                # a turn is being worked on
        self._live = None                # Deepgram live session for the current utterance
        self._confirm_answer: queue.Queue | None = None
        self.pending_confirm: str | None = None
        self.confirm_notifiers: list = []   # extra places to ask for a yes/no (the phone)
        self.current_source = ""            # where the turn in progress came from
        self.voice = voice and not cfg.voice.missing_keys()
        self.speaker = self.recorder = self.stt = self.ptt = None
        self._mic_want: str | None = None   # preferred mic in use (None = Windows default)
        self._speaker_name = ""             # Windows' default speaker when the mic was chosen
        self._last_said: tuple[str, float] = ("", 0.0)   # Fafnir's last spoken reply, for echo checks
        self._off_mics: set[str] = set()    # present in Windows but silent (headset switched off)
        self.mic_name = ""                  # the mic being listened to, for displays
        self._audio_lock = threading.Lock()
        if self.voice:
            self._pick_mic()
            from .voice.ptt import PushToTalk
            from .voice.recorder import Recorder
            from .voice.local_stt import make_stt
            from .voice.tts import Speaker, make_tts
            v = cfg.voice
            keyterms = [self.name] + [p.strip() for p in cfg.assistant.home_location.split(",") if p.strip()]
            self.stt = make_stt(v, keyterms)   # local Whisper by default; Deepgram if chosen
            self.speaker = Speaker(make_tts(v),
                                   on_error=lambda m: self.events.error(m))
            self.recorder = Recorder()
            self.ptt = PushToTalk(v.ptt_key, self._ptt_down, self._ptt_up)
        self.wake = None
        self.wake_problem = ""           # why the wake word is off, for displays
        self._wake_live = None
        self._window_gen = 0             # bumps whenever a pending follow-up window is cancelled
        if self.voice and cfg.voice.wake_enabled:
            self._setup_wake()

    def _setup_wake(self) -> None:
        v = self.cfg.voice
        try:
            from .config import ROOT
            from .voice.wake import WakeWord
            self.wake = WakeWord(ROOT / "data" / "wake", v.wake_sensitivity,
                                 on_wake=self._wake_detected, on_chunk=None, on_done=self._wake_done,
                                 end_silence=v.wake_end_silence, name=self.name, engine=v.wake_engine,
                                 on_open=self._open_mic_heard)
            self.wake.is_muted = self._echo_risk
            self.wake.on_barge = self._barge_in
            self.wake.can_barge = lambda: bool(self.cfg.voice.barge_in and self.speaker and self.speaker.speaking
                                               and not self.on_loudspeaker())
            self.wake.clean_audio = v.noise_suppression
        except Exception as e:  # no recordings yet, or no mic
            self.wake = None
            self.wake_problem = f"Wake word off: {e}"

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._worker, daemon=True).start()
        self._start_killswitch()
        self._start_resources()
        threading.Thread(target=self._summarize_loop, daemon=True, name="summaries").start()
        threading.Thread(target=self._retention, daemon=True, name="retention").start()
        threading.Thread(target=self._warm_up, daemon=True, name="warm-up").start()
        if self.voice and (self.cfg.voice.mic_preference or self.cfg.voice.mic_for_speaker):
            threading.Thread(target=self._watch_mics, daemon=True, name="mic-watch").start()
        if self.ptt:
            self.ptt.start()
        if self.wake:
            try:
                self.wake.start()
            except Exception as e:
                self.wake_problem = f"Wake word off: couldn't open the microphone ({e})."
                self.wake = None
        print(f"[voice] wake word: {('on (' + self.wake.engine + ')') if self.wake else 'off'}"
              f"{' - ' + self.wake_problem if self.wake_problem else ''}", flush=True)
        if self.wake_problem:
            self.events.notice(self.wake_problem)

    def stop(self) -> None:
        self.interrupt()
        if self.ptt:
            self.ptt.stop()
        if self.wake:
            try:
                self.wake.stop()
            except Exception:
                pass

    # ---- Fafnir's own voice coming back through the mic (JBL soundbar + camera mic) ----
    def on_loudspeaker(self) -> bool:
        if not self._speaker_name:
            try:
                from .voice import devices
                self._speaker_name = devices.default_output_name()
            except Exception:
                return True
        return not any(h.lower() in self._speaker_name.lower() for h in self.cfg.voice.headphones)

    def _echo_risk(self) -> bool:
        """True while Fafnir is talking (or just stopped) through a speaker the mic can hear."""
        if not self.speaker or not self.on_loudspeaker():
            return False
        return self.speaker.speaking or time.monotonic() - self.speaker.last_played < 0.8

    def _is_own_echo(self, text: str) -> bool:
        """Heard text that's mostly words Fafnir itself just said."""
        said, when = self._last_said
        if not said or time.monotonic() - when > 20:
            return False
        import re
        heard = re.findall(r"[a-z']+", text.lower())
        mine = set(re.findall(r"[a-z']+", said.lower()))
        if len(heard) < 2:
            return False
        return sum(w in mine for w in heard) / len(heard) >= 0.7

    # ---- data retention: old conversations and logs are deleted on a schedule ----
    def _retention(self) -> None:
        import re
        from datetime import datetime, timedelta
        from .config import ROOT
        from .tools import CONVERSATIONS
        p = self.cfg.privacy
        try:
            gone = CONVERSATIONS.purge_older_than(p.keep_conversations_days)
            if gone:
                print(f"[privacy] deleted {gone} conversation file(s) older than {p.keep_conversations_days} days", flush=True)
            if p.keep_logs_days > 0:   # these logs start each line with a timestamp or carry a "time" field
                cutoff = (datetime.now() - timedelta(days=p.keep_logs_days)).isoformat()[:10]
                for name in ("outbound.log", "wake.log", "heartbeat.log"):
                    f = ROOT / "logs" / name
                    if not f.exists():
                        continue
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                    def recent(ln: str) -> bool:   # keep anything without a readable date
                        m = re.search(r"\d{4}-\d{2}-\d{2}", ln[:40])
                        return m is None or m.group(0) >= cutoff
                    keep = [ln for ln in lines if recent(ln)]
                    if len(keep) < len(lines):
                        f.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except Exception as e:
            print(f"[privacy] retention skipped: {type(e).__name__}: {e}", flush=True)

    # ---- summaries of older conversations (local model, background) ----
    def _summarize_loop(self) -> None:
        from . import summarizer
        from .config import ROOT
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "add_summary"):
            return
        time.sleep(90)
        while True:
            try:
                res = getattr(self, "resources", None)
                snap = res.snapshot() if res else {}
                idle = not self.busy and not self.listening
                room = snap.get("free_for_models_mb", 0) - sum(snap.get("models_loaded", {}).values()) >= 3600 \
                    or self.cfg.local.fast in snap.get("models_loaded", {})
                if idle and not snap.get("game") and room:
                    local = hasattr(self.agent, "use_model")
                    done = summarizer.run(memory, ROOT / "data" / "conversations", self.cfg.local.fast, unload=not local)
                    if done:
                        print(f"[memory] summarised conversations from {', '.join(done)}", flush=True)
            except Exception as e:
                print(f"[memory] summaries skipped: {type(e).__name__}: {e}", flush=True)
            time.sleep(6 * 3600)

    # ---- gaming-aware profiles (what may sit on the graphics card) ----
    def _start_resources(self) -> None:
        try:
            from .resources import ResourceManager
            from .tools import RESOURCES
            set_voice = None
            tts = self.speaker.tts if self.speaker else None
            if tts is not None and hasattr(type(tts), "set_device"):
                set_voice = type(tts).set_device
            self.resources = ResourceManager(
                self.cfg, self.agent, set_voice_device=set_voice,
                on_change=lambda profile, why: self.events.notice(f"Profile: {profile.replace('_', ' ')} ({why})"))
            RESOURCES["manager"] = self.resources
            self.resources.start()
        except Exception as e:
            print(f"[profile] not started: {type(e).__name__}: {e}", flush=True)

    # ---- emergency kill switch ----------------------------------------------------
    def _start_killswitch(self) -> None:
        from . import killswitch
        killswitch.on_change(self._kill_changed)
        try:
            self._hotkeys = killswitch.Hotkeys()
            self._hotkeys.start()
        except Exception as e:
            print(f"[kill switch] hotkeys unavailable: {e}", flush=True)

        def watch() -> None:  # changes made by `python main.py --kill/--release`
            while True:
                time.sleep(1)
                killswitch.sync()
        threading.Thread(target=watch, daemon=True, name="kill-switch").start()
        if killswitch.engaged():
            self.events.error("Kill switch is ON (from before the restart): I won't do anything. "
                              "Release with Ctrl+Alt+Shift+K or type /release.")

    def _kill_changed(self, on: bool) -> None:
        if on:
            self.interrupt()
            _drain_queue(self.turns)
            self._window_gen += 1
            if self.wake:
                self.wake.close_window()
            if self._confirm_answer is not None:
                self._confirm_answer.put("no")
            if self.recorder and self.recorder.recording:
                try:
                    self.recorder.stop()
                except Exception:
                    pass
            _beep(300, 400)
            self.events.error("KILL SWITCH ON: everything stopped. Release with Ctrl+Alt+Shift+K or type /release.")
        else:
            _beep(880, 120)
            self.events.notice("Kill switch released. Fafnir can act again.")

    def _warm_up(self) -> None:
        """Load the voice model and connect to the brain now, not on the first request."""
        for name, fn in (("voice", getattr(self.speaker and self.speaker.tts, "warm_up", None)),
                         ("speech recognition", getattr(self.stt, "warm_up", None)),
                         ("brain", getattr(self.agent, "warm_up", None))):
            if fn:
                t = time.monotonic()
                try:
                    fn()
                    print(f"[warm-up] {name} ready in {time.monotonic() - t:.1f}s", flush=True)
                except Exception as e:
                    print(f"[warm-up] {name}: {type(e).__name__}: {e}", flush=True)

    # ---- microphone: headset when plugged in, otherwise the next one on the list ----
    def _desired_mic(self) -> tuple[str | None, str, list[str]]:
        """(mic to use, speaker Windows plays through, mics present) right now."""
        from .voice import devices
        v = self.cfg.voice
        names, speaker = devices.live_input_names(), devices.default_output_name()
        names = [n for n in names if n not in self._off_mics]
        return devices.choose_for(speaker, list(v.mic_for_speaker), list(v.mic_preference), names), speaker, names

    def _pick_mic(self) -> None:
        """Choose the mic before any stream is open (startup)."""
        from .voice import devices
        try:
            want, self._speaker_name, _ = self._desired_mic()
            devices.refresh()
            self._mic_want, self.mic_name = want, devices.use_input(want)
            print(f"[voice] microphone: {self.mic_name} (speaker: {self._speaker_name})", flush=True)
        except Exception as e:
            print(f"[voice] couldn't choose a microphone: {e}", flush=True)

    def _check_off_mics(self, tick: int) -> None:
        """Spot a mic that's switched off but still listed (wireless headset, dongle plugged in),
        and notice when it comes back on."""
        from .voice import devices
        from .voice.wake import SILENT_PEAK
        cur = self._mic_want
        if (self.wake and cur and cur not in self._off_mics and not self.listening
                and self.wake.silent_for() > 4):
            self._off_mics.add(cur)
            print(f"[voice] {cur} gives no sound (switched off?) - using another mic", flush=True)
        if self._off_mics and tick % 5 == 0:  # every ~10 s
            present = devices.live_input_names()
            for name in list(self._off_mics):
                try:
                    if name not in present or devices.probe_peak(name) > SILENT_PEAK:
                        self._off_mics.discard(name)
                        print(f"[voice] {name} is giving sound again", flush=True)
                except Exception:
                    pass

    def _watch_mics(self) -> None:
        tick = 0
        while True:
            time.sleep(2)
            tick += 1
            try:
                self._check_off_mics(tick)
                want, speaker, names = self._desired_mic()
                if want == self._mic_want and speaker == self._speaker_name:
                    continue
                gone = self._mic_want is not None and (self._mic_want not in names
                                                       or self._mic_want in self._off_mics)
                busy = self.listening or (self.speaker and self.speaker.speaking)
                if busy and not gone:
                    continue  # switch once this exchange is over
                self._speaker_name = speaker
                self._switch_mic(want)  # also reopens the speaker stream on the new default
            except Exception as e:
                print(f"[voice] microphone check failed: {e}", flush=True)

    def _switch_mic(self, want: str | None) -> None:
        """Close every audio stream, re-read the device list, reopen on the new mic."""
        from .voice import devices
        with self._audio_lock:
            if self.recorder and self.recorder.recording:
                self.recorder.stop()
            if self.wake:
                self.wake.stop()
            if self.speaker:
                self.speaker.close_output()
            try:
                devices.refresh()
                self._mic_want, self.mic_name = want, devices.use_input(want)
            finally:
                if self.speaker:
                    self.speaker.open_output()
                if self.wake:
                    try:
                        self.wake.start()
                    except Exception as e:
                        self.events.error(f"Wake word paused: couldn't open the microphone ({e}).")
        print(f"[voice] microphone switched to: {self.mic_name} (speaker: {self._speaker_name})", flush=True)
        parts = list(self.cfg.voice.mic_preference) + [m for _, m in self.cfg.voice.mic_for_speaker]
        label = next((p for p in parts if want and p.lower() in want.lower()), "Windows default")
        self.events.notice(f"Microphone: {label}")

    # ---- state for displays ------------------------------------------------
    @property
    def listening(self) -> bool:
        return bool((self.recorder and self.recorder.recording) or (self.wake and self.wake.capturing))

    @property
    def state(self) -> str:
        """idle | listening | thinking | speaking | confirming"""
        if self.listening:
            return "listening"
        if self.pending_confirm:
            return "confirming"
        if self.wake and self.wake.window_open and not (self.speaker and self.speaker.speaking):
            return "listening"   # just answer, no wake word needed
        if self.speaker and self.speaker.speaking:
            return "speaking"
        if self.busy:
            return "thinking"
        return "idle"

    @property
    def level(self) -> float:
        """0..1 loudness of the mic (while listening) or of Fafnir's voice."""
        if self.recorder and self.recorder.recording:
            return self.recorder.level
        if self.wake and self.wake.capturing:
            return self.wake.level
        if self.speaker:
            return self.speaker.level
        return 0.0

    # ---- inputs --------------------------------------------------------------
    def submit_text(self, text: str, source: str = "typed", reply=None) -> None:
        """A typed message (or one from the phone), or a typed answer to a pending yes/no.
        `reply(text)` is called with Fafnir's answer, for front ends elsewhere (the phone)."""
        text = text.strip()
        if not text:
            return
        from . import killswitch
        if killswitch.is_kill_command(text):
            killswitch.engage(f"owner ({source})")
            if reply:
                reply("KILL SWITCH ON. Everything is stopped.")
            return
        if killswitch.is_release_command(text) and not source.startswith("phone"):
            ok = killswitch.release(f"owner ({source})")
            if not ok:
                self.events.notice("The kill switch wasn't on.")
            return
        if killswitch.engaged():
            self.events.error(killswitch.MESSAGE)
            if reply:
                reply(killswitch.MESSAGE)
            return
        if self._confirm_answer is not None:
            self._confirm_answer.put(text)
            return
        if not source.startswith("phone"):  # a phone message shouldn't cut off what's playing at the PC
            self.interrupt()
        self.turns.put(("text", text, None, time.monotonic(), {"source": source, "reply": reply}))

    def answer_confirm(self, approved: bool) -> None:
        if self._confirm_answer is not None:
            self._confirm_answer.put("yes" if approved else "no")

    def interrupt(self) -> None:
        """Stop whatever Fafnir is saying or generating."""
        self.cancel.set()
        if self.speaker:
            self.speaker.stop()

    def announce(self, text: str) -> None:
        """Say something unprompted (a heartbeat notice). Silent if voice is off."""
        if self.cfg.voice.cue_sounds:
            _beep(740, 90)
        if self.speaker:
            self.speaker.say(text)
            self._follow_up(self.cfg.voice.follow_up_seconds)

    # ---- follow-up listening: answer without the wake word -----------------------
    def _follow_up(self, seconds: float, tone: bool = True) -> None:
        """Conversation mode: once Fafnir has finished speaking, listen for `seconds` without the
        wake word. Every exchange opens a fresh window, so a conversation just carries on; when a
        window runs out unused, a soft falling tone says Fafnir has stopped listening."""
        if not self.wake or seconds <= 0:
            return
        self._window_gen += 1   # a newer window replaces any older pending one
        gen = self._window_gen

        def wait_then_open() -> None:
            time.sleep(0.2)
            deadline = time.monotonic() + 180
            while self.speaker and self.speaker.speaking and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.8 if self.on_loudspeaker() else 0.35)  # let the echo of Fafnir's voice die down
            if gen != self._window_gen or not self.wake or not self.turns.empty():
                return
            self.wake.open_window(seconds)
            end = time.monotonic() + seconds
            while time.monotonic() < end + 0.3:
                time.sleep(0.2)
                if gen != self._window_gen or self.wake.capturing or self.busy:
                    return  # the owner spoke (or something else took over)
            if gen == self._window_gen and self.turns.empty() and not self.busy:
                self._conversation_over()

        threading.Thread(target=wait_then_open, daemon=True).start()

    def _stray_speech(self) -> None:
        """Something was heard in the open-mic window that wasn't for Fafnir (noise, an echo, other
        people talking). Keep listening once; after two in a row, stop until the wake word, so room
        talk can't hold the mic open (and keep being sent out for transcription)."""
        self._stray = getattr(self, "_stray", 0) + 1
        if self._stray >= 2:
            self._stray = 0
            self._conversation_over()
        else:
            self._follow_up(self.cfg.voice.follow_up_seconds)

    def _conversation_over(self) -> None:
        """Stop listening without the wake word; a soft falling tone (if cue sounds are on)."""
        self._window_gen += 1
        if self.wake:
            self.wake.close_window()
        if self.cfg.voice.cue_sounds:
            def tone():
                time.sleep(0.1)
                while self.speaker and self.speaker.speaking:
                    time.sleep(0.05)
                import winsound  # one after the other (_beep plays in the background)
                winsound.Beep(520, 70)
                winsound.Beep(390, 110)
            threading.Thread(target=tone, daemon=True).start()

    def _close_follow_up(self) -> None:
        self._window_gen += 1
        if self.wake:
            self.wake.close_window()

    def _barge_in(self) -> bool:
        """The owner started talking while Fafnir was speaking. On headphones (where the mic can't
        hear Fafnir) that means 'stop and listen': cut the speech and capture what they say."""
        if not (self.cfg.voice.barge_in and self.speaker and self.speaker.speaking):
            return False
        if self.on_loudspeaker() or (self.recorder and self.recorder.recording) or self._confirm_answer is not None:
            return False
        self.interrupt()
        try:
            self._wake_live = self.stt.start_live(self.wake.rate)
            self.wake.set_on_chunk(self._wake_live.send)
        except Exception:
            self._wake_live = None
            self.wake.set_on_chunk(None)
        print("[voice] interrupted by the owner talking", flush=True)
        return True

    def _open_mic_heard(self) -> bool:
        """Speech started inside a follow-up window. Returns True to capture it."""
        if self.recorder and self.recorder.recording:
            return False
        if self.speaker and self.speaker.speaking:
            return False  # probably Fafnir's own voice
        try:
            self._wake_live = self.stt.start_live(self.wake.rate)
            self.wake.set_on_chunk(self._wake_live.send)
        except Exception:
            self._wake_live = None
            self.wake.set_on_chunk(None)
        if self.cfg.voice.cue_sounds:
            _beep(660, 60)
        return True

    def reset(self) -> None:
        self.interrupt()
        self.answer_confirm(False)
        self.agent.reset()

    # ---- confirmation gate ---------------------------------------------------
    def confirm(self, action: str) -> bool:
        """Ask the owner to approve one action. Called from the worker thread."""
        answers: queue.Queue = queue.Queue()
        self._confirm_answer = answers
        self.pending_confirm = action
        self.events.confirm_request(action)
        for notify in list(self.confirm_notifiers):  # e.g. Yes/No buttons on the phone
            try:
                notify(action, self.current_source)
            except Exception:
                pass
        if self.cfg.voice.cue_sounds and not self.current_source.startswith("phone"):
            _beep(520, 120)
        if self.speaker and not self.current_source.startswith("phone"):
            self.speaker.say(f"I need your OK. {action.split(CONFIRM_MARK)[0]} Yes or no?")
            self._follow_up(CONFIRM_TIMEOUT)   # just say yes or no, no wake word
        timed_out = False
        try:
            answer = answers.get(timeout=CONFIRM_TIMEOUT)
        except queue.Empty:
            answer, timed_out = "", True
        finally:
            self._confirm_answer = None
            self.pending_confirm = None
            self._close_follow_up()
        ok = is_yes(answer)
        self.events.confirm_done(ok, timed_out)
        return ok

    # ---- push-to-talk --------------------------------------------------------
    def _ptt_down(self) -> None:
        self._close_follow_up()
        if self._confirm_answer is not None:
            self.speaker.stop()  # answering a confirmation: don't cancel the turn
        else:
            self.interrupt()
        try:
            self._live = self.stt.start_live(self.recorder.sample_rate)
        except Exception:
            self._live = None  # fall back to uploading after release
        try:
            self.recorder.start(on_chunk=self._live.send if self._live else None)
        except Exception as e:
            self.events.error(f"Microphone error: {e}")
            if self._live:
                self._live.abort()
            return
        if self.cfg.voice.cue_sounds:
            _beep(660, 60)

    def _ptt_up(self) -> None:
        if not self.recorder.recording:
            return
        wav, seconds = self.recorder.stop()
        live, self._live = self._live, None
        if seconds < MIN_SECONDS:
            if live:
                live.abort()
            self.events.notice("Too short, ignored.")
            return
        self._utterance_ready(wav, live)

    def _utterance_ready(self, wav: bytes, live, kind: str = "audio") -> None:
        """Something was said (key released, or silence after the wake word).
        kind "wake_audio" means it began with the wake word, which gets double-checked."""
        if self.cfg.voice.cue_sounds:
            _beep(880, 60)
        if self._confirm_answer is not None:
            threading.Thread(target=self._answer_confirm_from_audio, args=(wav, live), daemon=True).start()
            return
        self._close_follow_up()
        self.turns.put((kind, wav, live, time.monotonic(), {"source": "spoken"}))

    # ---- wake word -------------------------------------------------------------
    def _wake_detected(self) -> bool:
        """Called from the audio thread when the wake word is heard. Returns True to start
        capturing what's said next."""
        if self.recorder and self.recorder.recording:
            return False  # already talking with the key held
        if self.speaker and self.speaker.speaking and not self.cfg.voice.wake_while_speaking:
            return False
        if self._confirm_answer is not None:
            self.speaker.stop()  # answering a confirmation: don't cancel the turn
        else:
            self.interrupt()
        try:
            self._wake_live = self.stt.start_live(self.wake.rate)
            self.wake.set_on_chunk(self._wake_live.send)
        except Exception:
            self._wake_live = None
            self.wake.set_on_chunk(None)
        if self.cfg.voice.cue_sounds:
            _beep(660, 60)
        return True

    def _wake_done(self, wav: bytes, seconds: float, open_mic: bool = False) -> None:
        self.wake.set_on_chunk(None)
        live, self._wake_live = self._wake_live, None
        if seconds < MIN_SECONDS:
            if live:
                live.abort()
            if not open_mic:
                self.events.notice("Didn't hear anything after the wake word.")
            return
        self._utterance_ready(wav, live, kind="open_audio" if open_mic else "wake_audio")

    def _answer_confirm_from_audio(self, wav: bytes, live) -> None:
        answers = self._confirm_answer
        try:
            text, _ = self._transcribe(wav, live)
        except STTError as e:
            self.events.error(f"{e} Say it again, or use the buttons.")
            return
        self.events.heard(text)
        if answers is not None and text:
            answers.put(text)

    def _transcribe(self, wav: bytes, live) -> tuple[str, str]:
        """Returns (text, how). Prefers the live stream; if it's slow, uploads the clip in
        parallel and takes whichever answer arrives first."""
        if live is None:
            return self.stt.transcribe(wav), "upload"
        live.close()
        try:
            return live.wait(1.5 if live.connected else 0.3), "live"
        except STTError:
            pass
        why = "live slow" if live.connected else "live not connected"
        upload: dict = {}

        def do_upload() -> None:
            try:
                upload["text"] = self.stt.transcribe(wav)
            except STTError as e:
                upload["error"] = e

        threading.Thread(target=do_upload, daemon=True).start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if live.done:
                try:
                    return live.wait(0), "live, late"
                except STTError:
                    live = _NeverDone()
            if "text" in upload:
                return upload["text"], f"upload, {why}"
            if "error" in upload and isinstance(live, _NeverDone):
                raise upload["error"]
            time.sleep(0.03)
        raise STTError("Transcription timed out.")

    # ---- the one worker that runs turns --------------------------------------
    def _worker(self) -> None:
        while True:
            kind, payload, live, started, extra = self.turns.get()
            self.cancel.clear()
            self.busy = True
            try:
                stats = TurnStats()
                if kind in ("audio", "wake_audio", "open_audio"):
                    try:
                        text, how = self._transcribe(payload, live)
                    except STTError as e:
                        self.events.error(str(e))
                        continue
                    stats.transcribe = f"{time.monotonic() - started:.1f}s ({how})"
                    if not text:
                        if kind == "open_audio":  # a noise during the conversation: stay quiet, keep listening
                            self._stray_speech()
                        else:
                            self.events.notice("Didn't catch that.")
                        continue
                    from . import killswitch
                    if killswitch.is_kill_command(text):
                        self.events.heard(text)
                        killswitch.engage("owner (voice)")
                        continue
                    if killswitch.engaged():
                        self.events.error(killswitch.MESSAGE)
                        continue
                    if kind in ("wake_audio", "open_audio") and self.on_loudspeaker() and self._is_own_echo(text):
                        print(f"[voice] ignored my own voice: {text!r}", flush=True)
                        if kind == "open_audio":
                            self._stray_speech()
                        continue
                    if kind == "wake_audio" and not mentions_name(text, self.name):
                        # the local matcher fired but Deepgram didn't hear the name: a false alarm
                        self.events.notice("Ignored: that didn't sound like my name.")
                        continue
                    self.events.heard(text)
                else:
                    text = payload
                from . import killswitch
                if self.cancel.is_set() or killswitch.engaged():
                    continue
                spoken = kind in ("audio", "wake_audio", "open_audio")
                self._run(text, started, stats, source=extra.get("source", "typed"), reply=extra.get("reply"),
                          open_mic=kind == "open_audio", ending=spoken and is_goodbye(text))
            except Exception as e:  # never let the worker die
                self.events.error(f"Something went wrong: {type(e).__name__}: {e}")
            finally:
                self.busy = False

    def _run(self, text: str, started: float, stats: TurnStats, source: str = "", reply=None,
             open_mic: bool = False, ending: bool = False) -> None:
        from .tools import CONVERSATIONS
        CONVERSATIONS.append("user", text, source)
        self.current_source = source
        said: list[str] = []
        chunker = SentenceChunker()
        if self.speaker:
            self.speaker.first_audio_at = None
        brain_start = time.monotonic()
        usage_before = self.agent.llm.usage.copy()
        first_word: list[float] = []

        def say(sentences) -> None:
            if self.speaker and not source.startswith("phone"):  # phone replies are sent as text, not spoken
                for s in sentences:
                    self.speaker.say(s)

        held = [""] if open_mic else None   # open-mic turns: hold text until it's clearly not "(no reply)"
        held_done = [False]

        def on_text(t: str) -> None:
            if held is not None and not held_done[0]:
                held[0] += t
                lead = held[0].lstrip()
                if NO_REPLY.startswith(lead) or lead.startswith(NO_REPLY):
                    return  # might be (or is) the "not for me" marker: say nothing yet
                t = held[0]
                held_done[0] = True
            if not first_word and t.strip():
                first_word.append(time.monotonic())
            said.append(t)
            self.events.reply_text(t)
            say(chunker.feed(t))

        def on_tool(name, args, result, is_error) -> None:
            say(chunker.flush())  # speak any lead-in ("Checking your list.") now
            self.events.tool(name, args, result, is_error)

        self.events.reply_start()
        try:
            turn = self.agent.run_turn(
                text + (NOT_ADDRESSED_NOTE if open_mic else ""), on_text=on_text, cancel=self.cancel,
                on_tool=on_tool, confirm=self.confirm
            )
        except LLMError as e:
            self.events.error(str(e))
            if reply:
                reply(f"Sorry, that didn't work: {e}")
            return
        finally:
            self.current_source = ""
        interrupted = turn.stop_reason == "interrupted"
        answer = "".join(said).strip()
        if held is not None and not held_done[0]:
            leftover = (held[0] or "").strip()
            if leftover and leftover != NO_REPLY and not leftover.startswith(NO_REPLY):
                said.append(leftover)            # a very short real answer, held back above
                self.events.reply_text(leftover)
                say(chunker.feed(leftover))
                answer = leftover
            elif not interrupted:
                # not meant for Fafnir: stay quiet and keep the conversation open
                print(f"[voice] not for me, ignored: {text!r}", flush=True)
                CONVERSATIONS.append("assistant", "(ignored: not addressed to Fafnir)")
                self.events.reply_end(False, stats)
                self._stray_speech()
                return
        CONVERSATIONS.append("assistant", answer + (" [interrupted]" if interrupted else ""))
        if reply:
            reply(answer + (" [interrupted]" if interrupted else "") if answer else "(Done.)")
        if not interrupted:
            say(chunker.flush())
        if first_word:
            stats.first_word = first_word[0] - brain_start
        if answer and not source.startswith("phone"):
            self._last_said = (answer, time.monotonic() + 15)  # counts from roughly when speech ends
        if self.speaker and not interrupted and not source.startswith("phone"):
            deadline = time.monotonic() + 15
            while self.speaker.first_audio_at is None and self.speaker.speaking and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.speaker.first_audio_at is not None:
                stats.first_audio = self.speaker.first_audio_at - started
            if answer and ending:
                self._conversation_over()      # "thanks, that's all": stop listening after the reply
            elif answer:  # conversation mode: keep listening so the owner can just carry on
                self._stray = 0
                secs = self.cfg.voice.follow_up_seconds
                self._follow_up(max(secs, 15) if secs > 0 and answer.endswith("?") else secs)
        stats.cost = cost_line(self.agent.llm.usage, usage_before)[1:-1]
        self.events.reply_end(interrupted, stats)
