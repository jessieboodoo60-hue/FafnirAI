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
        self.voice = voice and not cfg.voice.missing_keys()
        self.speaker = self.recorder = self.stt = self.ptt = None
        if self.voice:
            from .voice.ptt import PushToTalk
            from .voice.recorder import Recorder
            from .voice.stt import DeepgramSTT
            from .voice.tts import ElevenLabsTTS, Speaker
            v = cfg.voice
            keyterms = [self.name] + [p.strip() for p in cfg.assistant.home_location.split(",") if p.strip()]
            self.stt = DeepgramSTT(v.deepgram_api_key, v.stt_model, v.stt_language, keyterms)
            self.speaker = Speaker(ElevenLabsTTS(v.elevenlabs_api_key, v.tts_voice_id, v.tts_model),
                                   on_error=lambda m: self.events.error(m))
            self.recorder = Recorder()
            self.ptt = PushToTalk(v.ptt_key, self._ptt_down, self._ptt_up)
        self.wake = None
        self.wake_problem = ""           # why the wake word is off, for displays
        self._wake_live = None
        if self.voice and cfg.voice.wake_enabled:
            self._setup_wake()

    def _setup_wake(self) -> None:
        v = self.cfg.voice
        try:
            from .config import ROOT
            from .voice.wake import WakeWord
            self.wake = WakeWord(ROOT / "data" / "wake", v.wake_sensitivity,
                                 on_wake=self._wake_detected, on_chunk=None, on_done=self._wake_done,
                                 end_silence=v.wake_end_silence, name=self.name)
        except Exception as e:  # no recordings yet, or no mic
            self.wake = None
            self.wake_problem = f"Wake word off: {e}"

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._worker, daemon=True).start()
        if self.ptt:
            self.ptt.start()
        if self.wake:
            try:
                self.wake.start()
            except Exception as e:
                self.wake_problem = f"Wake word off: couldn't open the microphone ({e})."
                self.wake = None
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
    def submit_text(self, text: str) -> None:
        """A typed message, or a typed answer to a pending yes/no."""
        text = text.strip()
        if not text:
            return
        if self._confirm_answer is not None:
            self._confirm_answer.put(text)
            return
        self.interrupt()
        self.turns.put(("text", text, None, time.monotonic()))

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
        if self.cfg.voice.cue_sounds:
            _beep(520, 120)
        if self.speaker:
            self.speaker.say(f"I need your OK. {action.split(CONFIRM_MARK)[0]} Yes or no?")
        timed_out = False
        try:
            answer = answers.get(timeout=CONFIRM_TIMEOUT)
        except queue.Empty:
            answer, timed_out = "", True
        finally:
            self._confirm_answer = None
            self.pending_confirm = None
        ok = is_yes(answer)
        self.events.confirm_done(ok, timed_out)
        return ok

    # ---- push-to-talk --------------------------------------------------------
    def _ptt_down(self) -> None:
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
        self.turns.put((kind, wav, live, time.monotonic()))

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

    def _wake_done(self, wav: bytes, seconds: float) -> None:
        self.wake.set_on_chunk(None)
        live, self._wake_live = self._wake_live, None
        if seconds < MIN_SECONDS:
            if live:
                live.abort()
            self.events.notice("Didn't hear anything after the wake word.")
            return
        self._utterance_ready(wav, live, kind="wake_audio")

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
            kind, payload, live, started = self.turns.get()
            self.cancel.clear()
            self.busy = True
            try:
                stats = TurnStats()
                if kind in ("audio", "wake_audio"):
                    try:
                        text, how = self._transcribe(payload, live)
                    except STTError as e:
                        self.events.error(str(e))
                        continue
                    stats.transcribe = f"{time.monotonic() - started:.1f}s ({how})"
                    if not text:
                        self.events.notice("Didn't catch that.")
                        continue
                    if kind == "wake_audio" and not mentions_name(text, self.name):
                        # the local matcher fired but Deepgram didn't hear the name: a false alarm
                        self.events.notice("Ignored: that didn't sound like my name.")
                        continue
                    self.events.heard(text)
                else:
                    text = payload
                if self.cancel.is_set():
                    continue
                self._run(text, started, stats)
            except Exception as e:  # never let the worker die
                self.events.error(f"Something went wrong: {type(e).__name__}: {e}")
            finally:
                self.busy = False

    def _run(self, text: str, started: float, stats: TurnStats) -> None:
        chunker = SentenceChunker()
        if self.speaker:
            self.speaker.first_audio_at = None
        brain_start = time.monotonic()
        usage_before = self.agent.llm.usage.copy()
        first_word: list[float] = []

        def say(sentences) -> None:
            if self.speaker:
                for s in sentences:
                    self.speaker.say(s)

        def on_text(t: str) -> None:
            if not first_word and t.strip():
                first_word.append(time.monotonic())
            self.events.reply_text(t)
            say(chunker.feed(t))

        def on_tool(name, args, result, is_error) -> None:
            say(chunker.flush())  # speak any lead-in ("Checking your list.") now
            self.events.tool(name, args, result, is_error)

        self.events.reply_start()
        try:
            reply = self.agent.run_turn(
                text, on_text=on_text, cancel=self.cancel, on_tool=on_tool, confirm=self.confirm
            )
        except LLMError as e:
            self.events.error(str(e))
            return
        interrupted = reply.stop_reason == "interrupted"
        if not interrupted:
            say(chunker.flush())
        if first_word:
            stats.first_word = first_word[0] - brain_start
        if self.speaker and not interrupted:
            deadline = time.monotonic() + 15
            while self.speaker.first_audio_at is None and self.speaker.speaking and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.speaker.first_audio_at is not None:
                stats.first_audio = self.speaker.first_audio_at - started
        stats.cost = cost_line(self.agent.llm.usage, usage_before)[1:-1]
        self.events.reply_end(interrupted, stats)
