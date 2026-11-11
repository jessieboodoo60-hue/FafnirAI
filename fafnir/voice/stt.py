"""Speech-to-text seam. Only this module knows about Deepgram.

Two paths: `start_live()` streams audio over a websocket while the key is held, so the
transcript is ready almost as soon as the key is released. `transcribe()` uploads a
finished recording, and is the fallback if live streaming fails.
"""
from __future__ import annotations

import json
import queue
import threading
from urllib.parse import urlencode

import httpx
from websockets.sync.client import connect

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_WSS = "wss://api.deepgram.com/v1/listen"


class STTError(Exception):
    """Transcription failed. `str(err)` is safe to show the user."""


class DeepgramSTT:
    def __init__(self, api_key: str, model: str = "nova-3", language: str = "en", keyterms: list[str] | None = None):
        self.api_key = api_key
        self.params = {"model": model, "language": language, "smart_format": "true"}
        if keyterms:  # names Deepgram should expect (e.g. "Fafnir"); nova-3 keyterm prompting
            self.params["keyterm"] = keyterms
        self.http = httpx.Client(
            headers={"Authorization": f"Token {api_key}"}, timeout=30.0
        )

    def transcribe(self, wav_bytes: bytes, content_type: str = "audio/wav") -> str:
        """Give me audio, get back text ("" if nothing was said)."""
        for attempt in (1, 2):  # one retry: home connections drop the odd request
            try:
                r = self.http.post(
                    DEEPGRAM_URL,
                    params=self.params,
                    content=wav_bytes,
                    headers={"Content-Type": content_type},
                )
                break
            except httpx.TimeoutException:
                if attempt == 2:
                    raise STTError("Transcription timed out.")
            except httpx.HTTPError:
                if attempt == 2:
                    raise STTError("Couldn't reach Deepgram. Check your internet connection.")
        if r.status_code in (401, 403):
            raise STTError("Deepgram rejected the key. Check DEEPGRAM_API_KEY in .env.")
        if r.status_code >= 400:
            raise STTError(f"Deepgram error {r.status_code}: {r.text[:200]}")
        try:
            alt = r.json()["results"]["channels"][0]["alternatives"][0]
            return alt.get("transcript", "").strip()
        except (KeyError, IndexError, ValueError):
            raise STTError("Deepgram returned an unexpected response.")

    def start_live(self, sample_rate: int) -> "LiveSession":
        params = dict(self.params, encoding="linear16", sample_rate=sample_rate, channels=1)
        return LiveSession(f"{DEEPGRAM_WSS}?{urlencode(params, doseq=True)}", self.api_key)


class LiveSession:
    """One push-to-talk utterance streamed to Deepgram as it's spoken."""

    def __init__(self, url: str, api_key: str):
        self._audio: queue.Queue = queue.Queue()
        self._finals: list[str] = []
        self._error: str | None = None
        self._done = threading.Event()
        self._closed = False
        self.connected = False
        threading.Thread(target=self._run, args=(url, api_key), daemon=True).start()

    def send(self, pcm: bytes) -> None:
        """Called from the audio thread; never blocks."""
        self._audio.put(pcm)

    def close(self) -> None:
        """No more audio is coming (key released). Safe to call twice."""
        if not self._closed:
            self._closed = True
            self._audio.put(None)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float) -> str:
        """Wait for the final transcript. Raises STTError on failure or timeout."""
        if not self._done.wait(timeout):
            raise STTError("Live transcription didn't finish in time.")
        if self._error:
            raise STTError(self._error)
        return " ".join(self._finals).strip()

    def finish(self, timeout: float = 6.0) -> str:
        self.close()
        return self.wait(timeout)

    def abort(self) -> None:
        self.close()

    def _run(self, url: str, api_key: str) -> None:
        try:
            with connect(url, additional_headers={"Authorization": f"Token {api_key}"},
                         open_timeout=10, close_timeout=5) as ws:
                self.connected = True
                receiver = threading.Thread(target=self._receive, args=(ws,), daemon=True)
                receiver.start()
                while True:
                    pcm = self._audio.get()
                    if pcm is None:
                        ws.send(json.dumps({"type": "CloseStream"}))
                        break
                    ws.send(pcm)
                receiver.join(8)
        except Exception as e:  # connection refused, auth, network drop
            self._error = f"Live transcription failed ({type(e).__name__})."
        finally:
            self._done.set()

    def _receive(self, ws) -> None:
        try:
            for msg in ws:
                if isinstance(msg, bytes):
                    continue
                data = json.loads(msg)
                if data.get("type") == "Results" and data.get("is_final"):
                    text = data["channel"]["alternatives"][0].get("transcript", "").strip()
                    if text:
                        self._finals.append(text)
        except Exception:
            pass  # closed by server after CloseStream, or dropped; finish() reports via _done
