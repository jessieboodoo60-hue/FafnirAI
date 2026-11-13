"""Text-to-speech seam and the Speaker that plays it.

`ElevenLabsTTS` is the only code that knows about ElevenLabs. `Speaker` takes
sentences as they arrive, synthesizes the next one while the current one plays,
and can be stopped instantly for interruption.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Iterator

import httpx
import sounddevice as sd

from .recorder import pcm_level

ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
PCM_RATE = 24000  # matches output_format=pcm_24000 (16-bit mono little-endian)
WRITE_BYTES = 2400  # 50 ms per write, so stop() takes effect fast


class TTSError(Exception):
    """Synthesis failed. `str(err)` is safe to show the user."""


class ElevenLabsTTS:
    def __init__(self, api_key: str, voice_id: str, model: str):
        self.url = ELEVEN_URL.format(voice_id=voice_id)
        self.model = model
        self.http = httpx.Client(headers={"xi-api-key": api_key}, timeout=30.0)

    def stream_pcm(self, text: str) -> Iterator[bytes]:
        """Give me text, get back raw 24 kHz 16-bit mono PCM chunks as they're made."""
        try:
            with self.http.stream(
                "POST",
                self.url,
                params={"output_format": f"pcm_{PCM_RATE}"},
                json={"text": text, "model_id": self.model},
            ) as r:
                if r.status_code in (401, 403):
                    raise TTSError("ElevenLabs rejected the key. Check ELEVENLABS_API_KEY in .env.")
                if r.status_code == 404:
                    raise TTSError("ElevenLabs voice not found. Check voice_id in config.toml.")
                if r.status_code >= 400:
                    r.read()
                    raise TTSError(f"ElevenLabs error {r.status_code}: {r.text[:200]}")
                carry = b""
                for chunk in r.iter_bytes():
                    data = carry + chunk
                    cut = len(data) - (len(data) % 2)  # never split a 16-bit sample
                    carry = data[cut:]
                    if cut:
                        yield data[:cut]
        except httpx.TimeoutException:
            raise TTSError("Speech synthesis timed out.")
        except httpx.HTTPError:
            raise TTSError("Couldn't reach ElevenLabs. Check your internet connection.")


    def synth_ogg(self, text: str) -> bytes:
        """Whole utterance as Ogg/Opus (the format of Discord voice messages)."""
        try:
            r = self.http.post(self.url.removesuffix("/stream"), params={"output_format": "opus_48000_64"},
                               json={"text": text, "model_id": self.model}, timeout=60)
        except httpx.HTTPError as e:
            raise TTSError(f"Couldn't reach ElevenLabs ({type(e).__name__}).")
        if r.status_code in (401, 403):
            raise TTSError("ElevenLabs rejected the key, or you're out of characters this month.")
        if r.status_code >= 400:
            raise TTSError(f"ElevenLabs error {r.status_code}: {r.text[:200]}")
        if r.content[:4] != b"OggS":
            raise TTSError("ElevenLabs didn't return Ogg audio.")
        return r.content


class Speaker:
    """Queue sentences with say(); they play in order. stop() cuts everything off."""

    def __init__(self, tts: ElevenLabsTTS, on_error: Callable[[str], None] = print):
        self.tts = tts
        self.on_error = on_error
        self._gen = 0  # bumped by stop(); stale work is dropped
        self._lock = threading.Lock()
        self._pending = 0
        self._idle = threading.Event()
        self._idle.set()
        self._sentences: queue.Queue = queue.Queue()
        self._audio: queue.Queue = queue.Queue()
        self.first_audio_at: float | None = None
        self._level = 0.0
        self._out = sd.RawOutputStream(samplerate=PCM_RATE, channels=1, dtype="int16")
        self._out.start()
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    # public API -----------------------------------------------------------
    def say(self, sentence: str) -> None:
        with self._lock:
            self._pending += 1
            self._idle.clear()
            gen = self._gen
        self._sentences.put((gen, sentence))

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._pending = 0
            self._idle.set()
        _drain(self._sentences)
        _drain(self._audio)

    @property
    def speaking(self) -> bool:
        return not self._idle.is_set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    # workers --------------------------------------------------------------
    def _synth_loop(self) -> None:
        while True:
            gen, sentence = self._sentences.get()
            if gen != self._gen:
                continue
            try:
                for pcm in self.tts.stream_pcm(sentence):
                    if gen != self._gen:
                        break
                    self._audio.put((gen, pcm))
            except TTSError as e:
                if gen == self._gen:
                    self.on_error(f"[voice: {e}]")
            self._audio.put((gen, None))  # end-of-sentence marker

    def _play_loop(self) -> None:
        while True:
            gen, pcm = self._audio.get()
            if gen != self._gen:
                continue
            if pcm is None:
                with self._lock:
                    if gen == self._gen and self._pending > 0:
                        self._pending -= 1
                        if self._pending == 0:
                            self._idle.set()
                continue
            if self.first_audio_at is None:
                self.first_audio_at = time.monotonic()
            for i in range(0, len(pcm), WRITE_BYTES):
                if gen != self._gen:
                    break
                piece = pcm[i : i + WRITE_BYTES]
                self._level = pcm_level(piece)
                self._out.write(piece)
            self._level = 0.0

    @property
    def level(self) -> float:
        """0..1 loudness of what's playing now, for displays."""
        return self._level if self.speaking else 0.0


def _drain(q: queue.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


# ---- Kokoro: offline speech (no API key) ---------------------------------------------------
class KokoroTTS:
    """Speech made on this PC with the Kokoro model (models/kokoro). Same interface as
    ElevenLabsTTS: stream_pcm() for the speaker, synth_ogg() for Discord voice notes.

    The voice can be one Kokoro voice ("am_onyx") or a blend ("am_onyx+bm_george"), lowered
    by `pitch` semitones at the same speaking pace (bigger, deeper), with optional reverb."""

    _shared = None                      # one loaded model for the whole app
    _lock = threading.Lock()

    def __init__(self, voice: str = "am_onyx", pitch: float = -3.0, speed: float = 1.0, reverb: float = 0.0,
                 lang: str = "en-us"):
        self.voice, self.pitch, self.speed, self.reverb, self.lang = voice, pitch, speed, reverb, lang

    @classmethod
    def _model(cls):
        with cls._lock:
            if cls._shared is None:
                from pathlib import Path
                from kokoro_onnx import Kokoro
                root = Path(__file__).resolve().parents[2] / "models" / "kokoro"
                model, voices = root / "kokoro-v1.0.onnx", root / "voices-v1.0.bin"
                if not model.exists() or not voices.exists():
                    raise TTSError(f"Kokoro's model files are missing from {root}.")
                cls._shared = Kokoro(str(model), str(voices))
            return cls._shared

    def _style(self, k):
        names = [v.strip() for v in self.voice.split("+") if v.strip()]
        if len(names) == 1:
            return names[0]
        import numpy as np
        return np.mean([k.get_voice_style(n) for n in names], axis=0)

    def _speak(self, text: str):
        import numpy as np
        k = self._model()
        f = 2 ** (self.pitch / 12)       # e.g. -3 semitones: f = 0.84
        try:
            with self._lock:
                s, sr = k.create(text, voice=self._style(k), speed=self.speed / f, lang=self.lang)
        except Exception as e:
            raise TTSError(f"Kokoro couldn't speak that ({type(e).__name__}).")
        if f != 1:                       # stretch back to normal pace: same speed, lower pitch
            n = int(len(s) / f)
            s = np.interp(np.linspace(0, len(s) - 1, n), np.arange(len(s)), s).astype(np.float32)
        if self.reverb:
            out = s.copy()
            for delay_ms, gain in ((37, .35), (71, .25), (113, .18), (167, .12)):
                d = int(sr * delay_ms / 1000)
                out[d:] += self.reverb * gain * s[:-d]
            s = out
        peak = float(np.abs(s).max()) if len(s) else 0.0
        if peak > 0.98:
            s = s / peak * 0.95
        return s.astype(np.float32), sr

    def stream_pcm(self, text: str) -> Iterator[bytes]:
        import numpy as np
        s, sr = self._speak(text)
        if sr != PCM_RATE:
            n = int(len(s) * PCM_RATE / sr)
            s = np.interp(np.linspace(0, len(s) - 1, n), np.arange(len(s)), s).astype(np.float32)
        pcm = (np.clip(s, -1, 1) * 32767).astype("<i2").tobytes()
        for i in range(0, len(pcm), 9600):   # ~0.2 s pieces, so stop() still cuts in quickly
            yield pcm[i:i + 9600]

    def synth_ogg(self, text: str) -> bytes:
        import io
        import soundfile as sf
        s, sr = self._speak(text)
        buf = io.BytesIO()
        sf.write(buf, s, sr, format="OGG", subtype="OPUS")
        return buf.getvalue()


def make_tts(v):
    """The configured speech engine (VoiceConfig): Kokoro (default, offline) or ElevenLabs."""
    if getattr(v, "tts_engine", "kokoro") == "elevenlabs":
        return ElevenLabsTTS(v.elevenlabs_api_key, v.tts_voice_id, v.tts_model)
    return KokoroTTS(v.kokoro_voice, v.kokoro_pitch, v.kokoro_speed, v.kokoro_reverb, v.kokoro_lang)
