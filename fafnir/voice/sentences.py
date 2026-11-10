"""Split streaming text into speakable sentences so TTS can start early."""
from __future__ import annotations

import re

# End of sentence: . ! ? or : followed by whitespace, or a newline.
_BOUNDARY = re.compile(r"(?<=[.!?:])\s+|\n+")
_MIN_CHARS = 20  # avoid sending tiny fragments like "Yes." alone when more follows


def clean_for_speech(text: str) -> str:
    """Strip markdown that sounds bad read aloud."""
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return text.strip()


class SentenceChunker:
    def __init__(self):
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        while True:
            m = _BOUNDARY.search(self._buf)
            if not m:
                break
            head = self._buf[: m.start()]
            # keep short heads buffered so they merge with the next sentence,
            # unless the boundary was a newline
            if len(head.strip()) < _MIN_CHARS and "\n" not in m.group():
                nxt = _BOUNDARY.search(self._buf, m.end())
                if not nxt:
                    break
                head = self._buf[: nxt.start()]
                self._buf = self._buf[nxt.end():]
            else:
                self._buf = self._buf[m.end():]
            s = clean_for_speech(head)
            if s:
                out.append(s)
        return out

    def flush(self) -> list[str]:
        s = clean_for_speech(self._buf)
        self._buf = ""
        return [s] if s else []
