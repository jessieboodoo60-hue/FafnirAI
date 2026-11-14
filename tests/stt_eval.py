"""Compare speech-to-text engines on the same audio: accuracy and speed.

Test audio is made locally by Kokoro in several voices and accents (no real recordings of the
owner are kept anywhere, so synthetic speech is the fair, private option). Synthetic speech is
cleaner than a real voice in a real room, so treat the scores as a comparison between engines,
not as what you'd get live.

    .venv\\Scripts\\python tests\\stt_eval.py              # local Whisper models only
    .venv\\Scripts\\python tests\\stt_eval.py --deepgram   # also Deepgram (sends the synthetic clips)
"""
from __future__ import annotations

import io
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SENTENCES = [
    "Fafnir, what's on my calendar tomorrow?", "Hey Fafnir, open Notepad.", "Turn the volume down a bit.",
    "What cars are in the driveway right now?", "Send an email to my accountant saying I'll be late tomorrow.",
    "Remember that my favourite colour is red.", "How much free space is on my C drive?", "Add a meeting with Sam on Friday at three p m.",
    "Fafnir, switch to gaming mode.", "What's the weather like in Petit Valley today?", "Find my resume in Documents.",
    "Hey Fafnir, pause the music.", "Do I have any unread emails?", "Close Chrome and open Spotify.",
    "Show me the pool cameras.", "Fafnir, what did we talk about yesterday?", "Install V L C for me.",
    "Thanks, that's all.", "Unlock my private files so you can read my passport.", "Fafnir, kill switch.",
]
VOICES = ["am_michael", "af_heart", "bm_george", "bf_emma"]


def norm(s: str) -> list[str]:
    s = s.lower().replace("p m", "pm").replace("p.m.", "pm").replace("v l c", "vlc").replace("3", "three")
    return re.findall(r"[a-z0-9']+", s.replace("favorite", "favourite").replace("color", "colour"))


def word_errors(ref: list[str], hyp: list[str]) -> int:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[-1]


def make_clips() -> list[tuple[str, np.ndarray]]:
    from fafnir.voice.tts import KokoroTTS
    clips = []
    rng = np.random.default_rng(1)
    for i, text in enumerate(SENTENCES):
        k = KokoroTTS(voice=VOICES[i % len(VOICES)], pitch=0.0)
        a = np.frombuffer(b"".join(k.stream_pcm(text)), np.int16).astype(np.float32) / 32768
        a = a[(np.arange(int(len(a) * 2 / 3)) * 1.5).astype(int)]            # 24 kHz -> 16 kHz
        a = np.concatenate([np.zeros(3200, np.float32), a * 0.5, np.zeros(3200, np.float32)])
        a = a + rng.normal(0, 0.004, len(a)).astype(np.float32)               # a little room hiss
        clips.append((text, a))
    return clips


def to_wav(a: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def score(name: str, fn, clips) -> None:
    errs = words = name_ok = name_total = 0
    times = []
    for text, a in clips:
        t = time.perf_counter()
        out = fn(a)
        times.append(time.perf_counter() - t)
        ref, hyp = norm(text), norm(out)
        errs += word_errors(ref, hyp)
        words += len(ref)
        if "fafnir" in ref:
            name_total += 1
            name_ok += "fafnir" in hyp
    times.sort()
    print(f"{name:26} word errors {100 * errs / words:4.1f}% | name heard {name_ok}/{name_total} | "
          f"time median {times[len(times) // 2]:.2f}s, slowest {times[-1]:.2f}s")


def main() -> int:
    clips = make_clips()
    secs = sum(len(a) for _, a in clips) / 16000 / len(clips)
    print(f"{len(clips)} clips, {secs:.1f} s each on average, {len(VOICES)} voices\n")
    from faster_whisper import WhisperModel
    for model in ("tiny.en", "base.en", "small.en"):
        m = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=6)

        def run(a, m=m):
            segs, _ = m.transcribe(a, language="en", beam_size=1, hotwords="Fafnir", without_timestamps=True,
                                   condition_on_previous_text=False, vad_filter=True)
            return " ".join(s.text for s in segs)
        run(clips[0][1])
        score(f"whisper {model} (CPU)", run, clips)
    if "--deepgram" in sys.argv:
        from fafnir.config import load_config
        from fafnir.voice.stt import DeepgramSTT
        v = load_config().voice
        dg = DeepgramSTT(v.deepgram_api_key, v.stt_model, v.stt_language, ["Fafnir"])
        score("Deepgram nova-3 (cloud)", lambda a: dg.transcribe(to_wav(a)), clips)
    return 0


if __name__ == "__main__":
    sys.exit(main())
