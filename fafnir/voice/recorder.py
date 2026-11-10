"""Microphone capture for push-to-talk."""
from __future__ import annotations

import array
import io
import threading
import wave

import sounddevice as sd

SAMPLE_RATE = 16000
MAX_SECONDS = 120


def pcm_level(pcm: bytes) -> float:
    """Rough 0..1 loudness of 16-bit PCM (RMS, scaled so normal speech is ~0.3-0.8)."""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not a:
        return 0.0
    step = max(1, len(a) // 256)  # sample a few hundred values; plenty for a meter
    vals = a[::step]
    rms = (sum(v * v for v in vals) / len(vals)) ** 0.5
    return min(1.0, rms / 6000)


class Recorder:
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._frames: list[bytes] = []
        self._lock = threading.Lock()
        self._stream: sd.RawInputStream | None = None
        self._on_chunk = None
        self.level = 0.0  # 0..1 loudness of the latest block, for displays

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, time_info, status) -> None:
        data = bytes(indata)
        self.level = pcm_level(data)
        with self._lock:
            if len(self._frames) * 1024 < self.sample_rate * 2 * MAX_SECONDS:
                self._frames.append(data)
        if self._on_chunk:
            self._on_chunk(data)

    def start(self, on_chunk=None) -> None:
        """Start capturing. `on_chunk(pcm_bytes)` is called live with each block."""
        if self._stream is not None:
            return
        self._on_chunk = on_chunk
        with self._lock:
            self._frames = []
        self._stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=512,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> tuple[bytes, float]:
        """Stop recording. Returns (wav_bytes, seconds)."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        with self._lock:
            pcm = b"".join(self._frames)
            self._frames = []
        seconds = len(pcm) / (2 * self.sample_rate)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm)
        return buf.getvalue(), seconds
