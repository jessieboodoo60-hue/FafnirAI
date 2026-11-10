"""Wake word: say "Fafnir" instead of holding the talk key.

An always-open microphone stream is checked frame by frame by Picovoice Porcupine, which
runs entirely on this PC; no audio leaves the computer until the wake word is heard.
After it is heard, the same stream captures what you say next and stops by itself once
you've been quiet for a moment (there's no key to release).
"""
from __future__ import annotations

import array
import io
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import pvporcupine
import sounddevice as sd

from .recorder import pcm_level

MAX_UTTERANCE_SECONDS = 20
NO_SPEECH_TIMEOUT = 5.0   # heard the wake word but nothing after it -> give up


class WakeWord:
    def __init__(self, access_key: str, keyword: str, sensitivity: float,
                 on_wake: Callable[[], bool], on_chunk: Callable[[bytes], None] | None,
                 on_done: Callable[[bytes, float], None], end_silence: float = 1.0):
        """`keyword` is a path to a custom .ppn file or a built-in name ("jarvis").
        `on_wake()` returns False to ignore this detection (e.g. already recording).
        `on_chunk` gets live audio while capturing; `on_done(wav, seconds)` gets the result."""
        kw = Path(keyword)
        if kw.suffix.lower() == ".ppn":
            if not kw.exists():
                raise ValueError(f"Wake word file not found: {kw}")
            self.porcupine = pvporcupine.create(access_key=access_key, keyword_paths=[str(kw)],
                                                sensitivities=[sensitivity])
            self.name = kw.stem.split("_")[0]
        else:
            self.porcupine = pvporcupine.create(access_key=access_key, keywords=[keyword],
                                                sensitivities=[sensitivity])
            self.name = keyword
        self.on_wake, self.on_chunk, self.on_done = on_wake, on_chunk, on_done
        self.end_silence = end_silence
        self.rate = self.porcupine.sample_rate
        self.frame = self.porcupine.frame_length
        self.capturing = False
        self.muted = False           # e.g. while Fafnir is speaking, if configured
        self.level = 0.0
        self._frames: list[bytes] = []
        self._buf = b""
        self._stream: sd.RawInputStream | None = None
        self._lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._stream = sd.RawInputStream(samplerate=self.rate, channels=1, dtype="int16",
                                         blocksize=self.frame, callback=self._callback)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.porcupine.delete()

    def set_on_chunk(self, fn) -> None:
        self.on_chunk = fn

    # ---- audio -------------------------------------------------------------------
    def _callback(self, indata, frames, time_info, status) -> None:
        data = self._buf + bytes(indata)
        step = self.frame * 2
        while len(data) >= step:
            chunk, data = data[:step], data[step:]
            try:
                self._handle(chunk)
            except Exception:
                pass  # never let a bad frame kill the audio thread
        self._buf = data

    def _handle(self, chunk: bytes) -> None:
        if self.capturing:
            self._capture(chunk)
            return
        if self.muted:
            return
        pcm = array.array("h")
        pcm.frombytes(chunk)
        if self.porcupine.process(pcm) >= 0 and self.on_wake():
            self._begin()

    # ---- capturing what's said after the wake word -------------------------------------
    def _begin(self) -> None:
        with self._lock:
            self.capturing = True
            self._frames = []
            self._started = time.monotonic()
            self._last_voice = None
            self._noise = None

    def _capture(self, chunk: bytes) -> None:
        lvl = pcm_level(chunk)
        self.level = lvl
        self._frames.append(chunk)
        if self.on_chunk:
            self.on_chunk(chunk)
        now = time.monotonic()
        elapsed = now - self._started
        # learn the room's background level in the first 0.25s, then treat clearly louder as speech
        if self._noise is None:
            if elapsed < 0.25:
                return
            self._noise = min(lvl, 0.15)
        if lvl > max(0.06, self._noise * 2.5):
            self._last_voice = now
        ended = (
            (self._last_voice is not None and now - self._last_voice > self.end_silence)
            or (self._last_voice is None and elapsed > NO_SPEECH_TIMEOUT)
            or elapsed > MAX_UTTERANCE_SECONDS
        )
        if ended:
            self._finish(heard_speech=self._last_voice is not None)

    def _finish(self, heard_speech: bool) -> None:
        with self._lock:
            self.capturing = False
            frames, self._frames = self._frames, []
            self.level = 0.0
        pcm = b"".join(frames)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(pcm)
        seconds = len(pcm) / (2 * self.rate) if heard_speech else 0.0
        threading.Thread(target=self.on_done, args=(buf.getvalue(), seconds), daemon=True).start()
