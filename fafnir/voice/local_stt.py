"""Local speech-to-text: Whisper on this PC's processor (faster-whisper). Nothing leaves the PC.

Same interface as DeepgramSTT (transcribe / start_live), so the engine doesn't change.

Measured 2026-11-13 on 20 test clips (tests/stt_eval.py): small.en 3.9% word errors in ~1.1 s
per request; tiny.en 7.1% in 0.2 s; Deepgram (cloud) 2.4% in 0.13 s. The graphics card is left
alone on purpose: it's full of the brain and the voice, and Gaming needs it empty.

Hiding the wait. Fafnir already waits ~0.8 s of silence to decide you've finished talking. A live
session starts transcribing as soon as you pause (0.3 s of quiet); if you say nothing more, that
transcript is the answer and it's ready when the pause ends; if you carry on, it's thrown away.

Silero voice-activity detection (built into faster-whisper) drops clips with no speech in them
before any decoding, so noises come back empty quickly.
"""
from __future__ import annotations

import io
import threading
import time

import numpy as np

from .stt import STTError

RATE = 16000
PAUSE_SECONDS = 0.3      # quiet this long -> start transcribing speculatively
QUIET_PEAK = 600         # int16 peak below this counts as quiet (after noise suppression)


class LocalSTT:
    _models: dict = {}
    _lock = threading.Lock()

    def __init__(self, model: str = "small.en", keyterms: list[str] | None = None, threads: int = 6):
        self.model_name, self.threads = model, threads
        self.hotwords = " ".join(keyterms[:6]) if keyterms else None
        threading.Thread(target=self._model, daemon=True, name="stt-load").start()

    def _model(self):
        with self._lock:
            key = (self.model_name, self.threads)
            if key not in self._models:
                from faster_whisper import WhisperModel
                self._models[key] = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                                 cpu_threads=self.threads)
            return self._models[key]

    def warm_up(self) -> None:
        self.transcribe_pcm(np.zeros(RATE, np.float32))

    def transcribe_pcm(self, audio: np.ndarray) -> str:
        """16 kHz mono float32 in, text out ('' if there's no speech)."""
        try:
            segs, _ = self._model().transcribe(audio, language="en", beam_size=1, hotwords=self.hotwords,
                                               without_timestamps=True, condition_on_previous_text=False,
                                               vad_filter=True, vad_parameters={"min_silence_duration_ms": 400})
            return " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.6).strip()
        except Exception as e:
            raise STTError(f"Local speech recognition failed: {type(e).__name__}: {e}")

    def transcribe(self, audio_bytes: bytes, content_type: str = "audio/wav") -> str:
        """A finished recording in any common format (wav, ogg/opus voice notes, mp3...)."""
        try:
            import soundfile as sf   # reads wav, ogg/opus (Discord voice notes), flac, mp3
            data, rate = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=True)
            audio = data.mean(axis=1)
            if rate != RATE:
                x = np.arange(int(len(audio) * RATE / rate)) * rate / RATE
                audio = np.interp(x, np.arange(len(audio)), audio).astype(np.float32)
        except Exception as e:
            raise STTError(f"Couldn't read that audio: {type(e).__name__}: {e}")
        return self.transcribe_pcm(audio)

    def start_live(self, sample_rate: int) -> "LocalLive":
        return LocalLive(self, sample_rate)


class LocalLive:
    """One utterance as it's spoken. Transcribes during your closing pause (see module notes)."""

    connected = True

    def __init__(self, stt: LocalSTT, sample_rate: int):
        self.stt, self.rate = stt, sample_rate
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._done = threading.Event()
        self._text, self._error = "", None
        self._quiet_since: float | None = None
        self._heard_speech = False
        self.speculative_hits = 0
        threading.Thread(target=self._run, daemon=True, name="stt-live").start()

    # ---- called from the audio thread ---------------------------------------------------------
    def send(self, pcm: bytes) -> None:
        loud = np.abs(np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)).max(initial=0) > QUIET_PEAK
        with self._lock:
            self._chunks.append(pcm)
            if loud:
                self._heard_speech, self._quiet_since = True, None
                self._last_loud_n = len(self._chunks)
            elif self._quiet_since is None:
                self._quiet_since = time.monotonic()

    def close(self) -> None:
        self._closed.set()

    def abort(self) -> None:
        self._aborted = True
        self._closed.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float) -> str:
        """The transcript. The engine's short live timeout doesn't apply: the work is local, so
        wait for it rather than starting a second transcription of the same audio."""
        if not self._done.wait(max(timeout, 20.0)):
            raise STTError("Local speech recognition took too long.")
        if self._error:
            raise STTError(self._error)
        return self._text

    def finish(self, timeout: float = 20.0) -> str:
        self.close()
        return self.wait(timeout)

    # ---- worker -----------------------------------------------------------------------------
    def _audio(self, n: int) -> np.ndarray:
        with self._lock:
            pcm = b"".join(self._chunks[:n])
        a = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768
        if self.rate != RATE:   # simple resample (push-to-talk already records at 16 kHz)
            idx = (np.arange(int(len(a) * RATE / self.rate)) * self.rate / RATE).astype(int)
            a = a[idx]
        return a

    def _run(self) -> None:
        guess: tuple[int, str] | None = None        # (chunks covered, text)
        try:
            while not self._closed.is_set():
                with self._lock:
                    n, quiet, heard = len(self._chunks), self._quiet_since, self._heard_speech
                paused = heard and quiet is not None and time.monotonic() - quiet >= PAUSE_SECONDS
                if paused and (guess is None or guess[0] < self._last_loud_index(n)):
                    guess = (n, self.stt.transcribe_pcm(self._audio(n)))
                self._closed.wait(0.05)
            if getattr(self, "_aborted", False):
                return
            with self._lock:
                n = len(self._chunks)
            if guess is not None and guess[0] >= self._last_loud_index(n):
                self._text = guess[1]               # nothing was said after the guess: use it
                self.speculative_hits = 1
            else:
                self._text = self.stt.transcribe_pcm(self._audio(n)) if n else ""
        except STTError as e:
            self._error = str(e)
        except Exception as e:
            self._error = f"Local speech recognition failed: {type(e).__name__}: {e}"
        finally:
            self._done.set()

    def _last_loud_index(self, n: int) -> int:
        """Number of chunks up to and including the last one that had sound in it."""
        return min(getattr(self, "_last_loud_n", 0), n)


def make_stt(v, keyterms: list[str] | None = None):
    """The speech-to-text engine chosen in config ([voice.stt] engine = "local" | "deepgram")."""
    if v.stt_engine == "deepgram":
        from .stt import DeepgramSTT
        return DeepgramSTT(v.deepgram_api_key, v.stt_model, v.stt_language, keyterms)
    return LocalSTT(v.stt_local_model, keyterms)
