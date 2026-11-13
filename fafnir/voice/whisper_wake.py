"""Wake word detection with a small offline speech recognizer (Whisper tiny.en, faster-whisper).

Each short burst of speech is transcribed on this PC and checked for the assistant's name in
its first few words. Unlike matching against voice recordings, this doesn't depend on the
owner's mic level or room noise, so it works for anyone saying the name. Nothing leaves the
computer. The model (~75 MB) downloads once on first use, then loads from the cache.

`hotwords` nudges the recognizer toward spelling the name correctly; unlike a prompt, it
doesn't make it "hear" the name in silence (tested: silence and fan noise -> nothing).
"""
from __future__ import annotations

import difflib
import os
import re
import threading

import numpy as np

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

CHECK_SECONDS = 1.6   # look at the first ~1.6 s of a burst ("Fafnir, what's the..." is enough)


def mentions(text: str, name: str) -> bool:
    words = re.findall(r"[a-z']+", text.lower())[:4]
    cands = words + ["".join(words[i:i + 2]) for i in range(len(words) - 1)]
    target = name.lower()
    return any(difflib.SequenceMatcher(None, c, target).ratio() >= 0.6 for c in cands)


class WhisperSpotter:
    def __init__(self, name: str = "Fafnir", model: str = "tiny.en"):
        self.name, self.model_name = name, model
        self._model = None
        self._ready = threading.Event()
        self.error = ""
        threading.Thread(target=self._load, daemon=True, name="wake-model").start()

    def _load(self) -> None:
        try:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8", cpu_threads=4)
        except Exception as e:  # missing package / no network on first download
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self._ready.set()

    @property
    def ready(self) -> bool:
        return self._model is not None

    def hear(self, pcm: bytes) -> str:
        """Transcribe 16 kHz 16-bit mono PCM. Returns '' if not ready or nothing was said."""
        if not self._ready.wait(0) or self._model is None:
            return ""
        audio = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768
        segs, _ = self._model.transcribe(audio, language="en", beam_size=1, hotwords=self.name,
                                         without_timestamps=True, condition_on_previous_text=False,
                                         vad_filter=False)
        return " ".join(s.text for s in segs if s.no_speech_prob < 0.5).strip()

    def check(self, pcm: bytes) -> tuple[bool, str]:
        text = self.hear(pcm)
        return (bool(text) and mentions(text, self.name)), text
