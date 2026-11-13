"""Wake word detection with a small offline speech recognizer (Whisper tiny.en, faster-whisper).

Each short burst of speech is transcribed on this PC and checked for the assistant's name in
its first few words. Unlike matching against voice recordings, this doesn't depend on the
owner's mic level or room noise, so it works for anyone saying the name. Nothing leaves the
computer. The model (~75 MB) downloads once on first use, then loads from the cache.

`hotwords` nudges the recognizer toward spelling the name correctly, but it also makes it
write the name for look-alikes ("Fifty", "Daphne", "Fast now") and for noise. Those come out
with low confidence, so a wake needs: the exact name in the first three words, said once, a
confident transcript (avg log-prob above a bar set by `sensitivity`) and a low "no speech"
score. Measured 2026-11-12 on the owner's six recordings: real ones -0.05..-0.13 (one -0.53),
look-alikes -0.26..-0.67, room noise no-speech 0.6-0.9.
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


def says_name(text: str, name: str) -> bool:
    """Strict: the name itself, spelled right, once, in the first three words."""
    words = re.findall(r"[a-z]+", text.lower())
    target = name.lower()
    return target in words[:3] and words.count(target) == 1


class WhisperSpotter:
    def __init__(self, name: str = "Fafnir", model: str = "tiny.en", sensitivity: float = 0.5):
        self.name, self.model_name = name, model
        s = min(1.0, max(0.0, sensitivity))
        self.min_logprob = -0.10 - 0.25 * s     # 0.5 -> -0.225; higher sensitivity = easier to wake
        self.max_no_speech = 0.15 + 0.3 * s     # 0.5 -> 0.30
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

    def hear(self, pcm: bytes) -> tuple[str, float, float]:
        """Transcribe 16 kHz 16-bit mono PCM -> (text, worst avg log-prob, worst no-speech prob)."""
        if not self._ready.wait(0) or self._model is None:
            return "", -9.0, 1.0
        audio = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768
        segs, _ = self._model.transcribe(audio, language="en", beam_size=1, hotwords=self.name,
                                         without_timestamps=True, condition_on_previous_text=False,
                                         vad_filter=False)
        segs = list(segs)
        text = " ".join(s.text for s in segs).strip()
        return (text, min((s.avg_logprob for s in segs), default=-9.0),
                max((s.no_speech_prob for s in segs), default=1.0))

    def check(self, pcm: bytes) -> tuple[bool, str]:
        text, lp, ns = self.hear(pcm)
        ok = bool(text) and says_name(text, self.name) and lp >= self.min_logprob and ns <= self.max_no_speech
        return ok, (f"{text!r} conf {lp:.2f} (need {self.min_logprob:.2f}) nospeech {ns:.2f}" if text else "")
