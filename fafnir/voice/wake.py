"""Wake word: say "Fafnir" instead of holding the talk key. Fully local.

An always-open microphone stream watches for the start of speech. Each time you start
talking, the first second or so is compared on this PC with your own recordings of the
wake word (see kws.py). Nothing leaves the computer unless it matches. On a match, the
same stream carries on capturing what you say next (including anything you said straight
after "Fafnir" in one breath) and stops by itself once you've been quiet for a moment.
"""
from __future__ import annotations

import collections
import io
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import sounddevice as sd

from .kws import RATE, Spotter
from .recorder import pcm_level

FRAME = 512                      # 32 ms
PRE_ROLL = 8                     # frames kept from just before speech starts (~0.25 s)
SEGMENT_GAP = 0.3                # silence that ends a burst of speech during detection
MAX_UTTERANCE_SECONDS = 20
NO_SPEECH_TIMEOUT = 5.0          # wake word heard but nothing after it -> give up


class WakeWord:
    def __init__(self, samples_folder: Path, sensitivity: float,
                 on_wake: Callable[[], bool], on_chunk: Callable[[bytes], None] | None,
                 on_done: Callable[[bytes, float], None], end_silence: float = 1.0,
                 name: str = "Fafnir"):
        """`on_wake()` returns False to ignore a detection (e.g. already recording).
        `on_chunk` gets live audio while capturing; `on_done(wav, seconds)` gets the result."""
        self.spotter = Spotter(samples_folder, sensitivity)
        self.name = name
        self.rate = RATE
        self.on_wake, self.on_chunk, self.on_done = on_wake, on_chunk, on_done
        self.end_silence = end_silence
        self.capturing = False
        self.level = 0.0
        self._noise = 0.02
        self._pre = collections.deque(maxlen=PRE_ROLL)
        self._seg: list[bytes] | None = None    # speech burst being considered
        self._seg_quiet_since = None
        self._ignore_until_quiet = False         # not the wake word: skip the rest of this sentence
        self._frames: list[bytes] = []
        self._buf = b""
        self._stream: sd.RawInputStream | None = None
        self._lock = threading.Lock()
        self._check_len = int(self.spotter.max_len * 1.5 * 160 / FRAME) + PRE_ROLL  # frames
        # The audio callback only queues frames; a worker thread does the (slower) matching.
        # Time is measured in audio, not wall clock, so a slow match can't distort silences.
        self._queue: queue.Queue = queue.Queue()
        self._clock = 0.0
        self._running = False

    # ---- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._process, daemon=True, name="wake-word").start()
        self._stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                                         blocksize=FRAME, callback=self._callback)
        self._stream.start()

    def stop(self) -> None:
        self._running = False
        self._queue.put(None)
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def set_on_chunk(self, fn) -> None:
        self.on_chunk = fn

    # ---- audio -------------------------------------------------------------------
    def _callback(self, indata, frames, time_info, status) -> None:
        data = self._buf + bytes(indata)
        step = FRAME * 2
        while len(data) >= step:
            self._queue.put(data[:step])
            data = data[step:]
        self._buf = data

    def _process(self) -> None:
        while self._running:
            chunk = self._queue.get()
            if chunk is None:
                return
            self._clock += FRAME / RATE
            try:
                self._handle(chunk)
            except Exception:
                pass  # never let a bad frame stop the listener

    def _is_speech(self, lvl: float) -> bool:
        return lvl > max(0.05, self._noise * 3)

    def _handle(self, chunk: bytes) -> None:
        if self.capturing:
            self._capture(chunk)
            return
        lvl = pcm_level(chunk)
        speech = self._is_speech(lvl)
        if not speech:  # track the room's background level
            self._noise = 0.95 * self._noise + 0.05 * lvl
        now = self._clock

        if self._ignore_until_quiet:
            if speech:
                self._seg_quiet_since = None
            elif self._seg_quiet_since is None:
                self._seg_quiet_since = now
            elif now - self._seg_quiet_since > SEGMENT_GAP:
                self._ignore_until_quiet = False
            self._pre.append(chunk)
            return

        if self._seg is None:
            if speech:
                self._seg = list(self._pre) + [chunk]
                self._seg_quiet_since = None
            else:
                self._pre.append(chunk)
            return

        self._seg.append(chunk)
        if speech:
            self._seg_quiet_since = None
        elif self._seg_quiet_since is None:
            self._seg_quiet_since = now
        burst_over = self._seg_quiet_since is not None and now - self._seg_quiet_since > SEGMENT_GAP
        if burst_over or len(self._seg) >= self._check_len:
            seg, self._seg = self._seg, None
            self._pre.clear()
            if self.spotter.matches(b"".join(seg)) and self.on_wake():
                self._begin(seg, still_talking=not burst_over)
            elif not burst_over:
                self._ignore_until_quiet = True  # ordinary speech: wait for the next sentence
                self._seg_quiet_since = None

    # ---- capturing what's said after the wake word -------------------------------------
    def _begin(self, already: list[bytes], still_talking: bool) -> None:
        with self._lock:
            self.capturing = True
            self._frames = list(already)
            self._started = self._clock
            # if "Fafnir" was said on its own, wait for the request; if they carried on
            # talking ("Fafnir, what's..."), that speech counts already
            self._last_voice = self._started if still_talking else None
        if self.on_chunk:
            for ch in already:
                self.on_chunk(ch)

    def _capture(self, chunk: bytes) -> None:
        lvl = pcm_level(chunk)
        self.level = lvl
        self._frames.append(chunk)
        if self.on_chunk:
            self.on_chunk(chunk)
        now = self._clock
        elapsed = now - self._started
        if self._is_speech(lvl):
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
            w.setframerate(RATE)
            w.writeframes(pcm)
        seconds = len(pcm) / (2 * RATE) if heard_speech else 0.0
        threading.Thread(target=self.on_done, args=(buf.getvalue(), seconds), daemon=True).start()


# ---- recording the owner's samples ---------------------------------------------------
def record_samples(folder: Path, count: int = 6, word: str = "Fafnir") -> int:
    """Interactive: record the owner saying the wake word several times, then calibrate."""
    import json
    from .kws import calibrate, features
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("sample_*.wav"):
        old.unlink()
    (folder / "calibration.json").unlink(missing_ok=True)

    print(f"\nLet's teach me your voice saying \"{word}\".")
    print("Use the same mic you normally use. Say it naturally, once each time, then stop.\n")
    noise = _measure_noise()
    saved = 0
    while saved < count:
        input(f"  [{saved + 1}/{count}] Press Enter, then say \"{word}\"...")
        pcm = _record_one(noise)
        secs = len(pcm) / (2 * RATE)
        if secs < 0.25 or secs > 2.0:
            print(f"     Didn't catch a clear word ({secs:.1f}s). Let's try that one again.")
            continue
        with wave.open(str(folder / f"sample_{saved + 1}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        saved += 1
        print(f"     Got it ({secs:.2f}s).")

    templates = []
    for p in sorted(folder.glob("sample_*.wav")):
        with wave.open(str(p), "rb") as w:
            templates.append(features(w.readframes(w.getnframes())))
    threshold = calibrate(templates)
    (folder / "calibration.json").write_text(json.dumps({"threshold": threshold, "word": word}))
    print(f"\nDone. Saved {saved} samples in {folder}. Restart the app and say \"{word}\".")
    return 0


def _measure_noise() -> float:
    print("  Measuring background noise; stay quiet for a second...")
    rec = sd.rec(int(RATE * 1.0), samplerate=RATE, channels=1, dtype="int16")
    sd.wait()
    return pcm_level(rec.tobytes())


def _record_one(noise: float) -> bytes:
    """Record until speech has started and then stopped; trim the silence around it."""
    threshold = max(0.05, noise * 3)
    frames, started, quiet, t0 = [], None, 0, time.monotonic()
    with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME) as s:
        while time.monotonic() - t0 < 6:
            data, _ = s.read(FRAME)
            data = bytes(data)
            loud = pcm_level(data) > threshold
            if started is None:
                frames = (frames + [data])[-PRE_ROLL // 2:]
                if loud:
                    started = len(frames)
            else:
                frames.append(data)
                quiet = 0 if loud else quiet + 1
                if quiet * FRAME / RATE > 0.35:
                    break
    if started is None:
        return b""
    return b"".join(frames[: len(frames) - max(0, quiet - 3)])
