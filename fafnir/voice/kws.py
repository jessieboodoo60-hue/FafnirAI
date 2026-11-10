"""Local keyword spotting by example: matches what you say against recordings of you
saying the wake word. Runs entirely on this PC with numpy; no account, no model download.

How it works: each recording is turned into MFCCs (a standard compact description of
the sound's shape over time), and incoming speech is compared to every recording with
dynamic time warping, which lines up the two even if you say it faster or slower. If the
best match is close enough, it's the wake word. The "close enough" threshold is worked
out from how similar your own recordings are to each other.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

RATE = 16000
WIN, HOP, NFFT, NMELS, NCEPS = 400, 160, 512, 26, 13   # 25 ms windows every 10 ms


def _mel_bank() -> np.ndarray:
    def hz2mel(h): return 2595 * np.log10(1 + h / 700)
    def mel2hz(m): return 700 * (10 ** (m / 2595) - 1)
    pts = mel2hz(np.linspace(hz2mel(80), hz2mel(7600), NMELS + 2))
    bins = np.floor((NFFT + 1) * pts / RATE).astype(int)
    fb = np.zeros((NMELS, NFFT // 2 + 1))
    for m in range(1, NMELS + 1):
        a, b, c = bins[m - 1], bins[m], bins[m + 1]
        if b > a:
            fb[m - 1, a:b] = (np.arange(a, b) - a) / (b - a)
        if c > b:
            fb[m - 1, b:c] = (c - np.arange(b, c)) / (c - b)
    return fb


_FB = _mel_bank()
_DCT = np.cos(np.pi / NMELS * (np.arange(NMELS) + 0.5)[None, :] * np.arange(NCEPS)[:, None])
_WINDOW = np.hamming(WIN)


def trim(x: np.ndarray, keep_end: bool = False) -> np.ndarray:
    """Cut quiet audio from the start (and the end, unless keep_end) so recordings and live
    speech line up the same way."""
    n = len(x) // HOP
    if n < 3:
        return x
    rms = np.sqrt((x[: n * HOP].reshape(n, HOP) ** 2).mean(axis=1))
    loud = np.where(rms > max(0.1 * rms.max(), 0.004))[0]
    if not len(loud):
        return x
    start = max(0, loud[0] - 3) * HOP
    end = len(x) if keep_end else min(n, loud[-1] + 4) * HOP
    return x[start:end]


def features(pcm: bytes, keep_end: bool = False) -> np.ndarray:
    """16 kHz 16-bit mono PCM -> (frames, 12) MFCCs, silence-trimmed and mean-normalised."""
    x = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768
    x = trim(x, keep_end)
    if len(x) < WIN:
        return np.zeros((0, NCEPS - 1), dtype=np.float32)
    x = np.append(x[0], x[1:] - 0.97 * x[:-1])
    n = 1 + (len(x) - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    frames = x[idx] * _WINDOW
    power = np.abs(np.fft.rfft(frames, NFFT)) ** 2 / NFFT
    mel = np.log(power @ _FB.T + 1e-10)
    cep = (mel @ _DCT.T)[:, 1:]               # drop c0 (overall loudness)
    return (cep - cep.mean(axis=0)).astype(np.float32)   # removes mic/room colouring


def dtw_distance(template: np.ndarray, seq: np.ndarray, open_end: bool = True) -> float:
    """Average per-step distance of the best alignment of `template` to the start of `seq`.
    open_end lets the match finish anywhere in `seq` (so "Fafnir, what's..." still matches),
    and the start may slip by a few frames."""
    T, N = len(template), len(seq)
    if T == 0 or N == 0:
        return float("inf")
    cost = np.sqrt(((template[:, None, :] - seq[None, :, :]) ** 2).sum(-1))
    D = np.full((T, N), np.inf)
    steps = np.ones((T, N))
    start_slack = min(N, 15)
    D[0, :start_slack] = cost[0, :start_slack]
    for i in range(1, T):
        prev, prev_steps = D[i - 1], steps[i - 1]
        row, row_steps = np.full(N, np.inf), np.ones(N)
        # vertical move (i-1, j) and diagonal (i-1, j-1)
        diag = np.concatenate(([np.inf], prev[:-1]))
        diag_steps = np.concatenate(([1], prev_steps[:-1]))
        use_diag = diag <= prev
        best = np.where(use_diag, diag, prev)
        best_steps = np.where(use_diag, diag_steps, prev_steps)
        for j in range(N):  # horizontal move (i, j-1) depends on this row
            b, s = best[j], best_steps[j]
            if j and row[j - 1] < b:
                b, s = row[j - 1], row_steps[j - 1]
            row[j] = cost[i, j] + b
            row_steps[j] = s + 1
        D[i], steps[i] = row, row_steps
    if not open_end:  # whole-to-whole comparison (used to compare the owner's own samples)
        return float(D[T - 1, N - 1] / steps[T - 1, N - 1])
    lo = max(1, int(T * 0.6))   # the match must cover a sensible length of the input
    hi = min(N, int(T * 1.6) + start_slack)
    if lo > hi:
        return float("inf")
    end = D[T - 1, lo - 1:hi] / steps[T - 1, lo - 1:hi]
    return float(end.min())


class Spotter:
    """Matches speech against the owner's recordings of the wake word."""

    def __init__(self, folder: Path, sensitivity: float = 0.6):
        self.folder = folder
        self.templates: list[np.ndarray] = []
        for p in sorted(folder.glob("sample_*.wav")):
            with wave.open(str(p), "rb") as w:
                f = features(w.readframes(w.getnframes()))
                if len(f) >= 15:
                    self.templates.append(f)
        if len(self.templates) < 3:
            raise ValueError(
                f"Not enough wake word recordings in {folder}. Run: python main.py --train-wake")
        cal = json.loads((folder / "calibration.json").read_text()) if (folder / "calibration.json").exists() else {}
        base = cal.get("threshold")
        if not isinstance(base, (int, float)) or not np.isfinite(base) or base <= 0:
            base = calibrate(self.templates)  # missing or broken calibration: redo it
        # sensitivity 0..1 scales the threshold: 0.5 = as calibrated, higher = easier to trigger
        self.threshold = base * (0.8 + 0.4 * sensitivity)
        self.max_len = max(len(t) for t in self.templates)

    def score(self, pcm: bytes) -> float:
        seq = features(pcm, keep_end=True)  # live speech may run on past the wake word
        if len(seq) < 10:
            return float("inf")
        seq = seq[: int(self.max_len * 1.8) + 15]
        return min(dtw_distance(t, seq) for t in self.templates)

    def matches(self, pcm: bytes) -> bool:
        return self.score(pcm) <= self.threshold


def calibrate(templates: list[np.ndarray]) -> float:
    """Threshold from how alike the owner's own recordings are to each other."""
    d = [dtw_distance(a, b, open_end=False) for i, a in enumerate(templates)
         for j, b in enumerate(templates) if i != j]
    d = [x for x in d if np.isfinite(x)]
    if not d:
        raise ValueError("Couldn't compare the wake word recordings; please record them again.")
    return float(np.mean(d) + 1.0 * np.std(d)) * 1.08
