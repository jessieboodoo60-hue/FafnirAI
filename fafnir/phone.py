"""Reach the owner on WhatsApp through the official WhatsApp Business Cloud API (Meta).

- Two-way chat: the owner's WhatsApp messages go to Fafnir like typed messages, and the
  answer comes back as a WhatsApp message (from Fafnir's own business/test number).
- Alerts: important heartbeat notices are sent to the owner's WhatsApp (once each).
- Confirmations for phone requests arrive with Yes / No buttons.

How messages get in: Meta delivers incoming messages to a webhook. Fafnir runs a tiny web
server on this PC (127.0.0.1 only) and an ngrok tunnel gives it a fixed public https
address that Meta can reach. Every delivery is checked against the app secret
(X-Hub-Signature-256), and only messages from the owner's number are acted on.

WhatsApp's rule: free-form messages may only be sent within 24 hours of the owner's last
message. Outside that window, alerts use an approved message template (config.toml
[phone] alert_template); without one, they wait in the Notices panel as usual.

Secrets in .env: WHATSAPP_TOKEN (permanent system-user token), WHATSAPP_PHONE_NUMBER_ID,
WHATSAPP_APP_SECRET, WHATSAPP_OWNER_NUMBER (yours, digits with country code).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import tomllib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import httpx

from .config import ROOT
from .tools.control import CONFIRM_MARK

STATE = ROOT / "data" / "phone.json"
MAX_LEN = 4000          # WhatsApp text limit is 4096
WINDOW = timedelta(hours=23, minutes=30)   # stay safely inside the 24-hour window
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _digits(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())


class WhatsAppBridge:
    def __init__(self, engine, notices, settings: dict, on_status: Callable[[str], None] = lambda s: None,
                 name: str = "Fafnir"):
        self.engine, self.notices, self.on_status, self.name = engine, notices, on_status, name
        self.token = os.environ.get("WHATSAPP_TOKEN", "").strip()
        self.number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        self.app_secret = os.environ.get("WHATSAPP_APP_SECRET", "").strip()
        self.owner = _digits(os.environ.get("WHATSAPP_OWNER_NUMBER", ""))
        self.version = settings.get("graph_version", "v23.0")
        self.port = int(settings.get("port", 8787))
        self.domain = str(settings.get("ngrok_domain", "")).strip().removeprefix("https://").strip("/")
        self.start_tunnel = bool(settings.get("start_tunnel", True))
        self.template = str(settings.get("alert_template", "")).strip()
        self.template_lang = str(settings.get("template_language", "en_US"))
        levels = {"important": ("important", "urgent"), "all": ("info", "important", "urgent"),
                  "urgent": ("urgent",)}
        self.push_levels = set(levels.get(settings.get("send", "important"), levels["important"]))
        self.http = httpx.Client(timeout=20.0)
        self.state = self._load()
        if "verify_token" not in self.state:  # shared secret for Meta's webhook check
            self.state["verify_token"] = secrets.token_urlsafe(18)
            self._save()
        self._stop = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._tunnel: subprocess.Popen | None = None
        self._seen: set[str] = set()   # message ids already handled (Meta may redeliver)
        engine.confirm_notifiers.append(self._confirm_on_phone)

    def missing(self) -> list[str]:
        need = {"WHATSAPP_TOKEN": self.token, "WHATSAPP_PHONE_NUMBER_ID": self.number_id,
                "WHATSAPP_APP_SECRET": self.app_secret, "WHATSAPP_OWNER_NUMBER": self.owner}
        return [k for k, v in need.items() if not v]

    # ---- state -------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(self.state), encoding="utf-8")

    def _in_window(self) -> bool:
        last = self.state.get("last_inbound")
        return bool(last) and datetime.now() - datetime.fromisoformat(last) < WINDOW

    # ---- sending -------------------------------------------------------------------
    def _post(self, payload: dict) -> bool:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": self.owner, **payload}
        try:
            r = self.http.post(f"https://graph.facebook.com/{self.version}/{self.number_id}/messages",
                               headers={"Authorization": f"Bearer {self.token}"}, json=payload)
        except httpx.HTTPError as e:
            self.on_status(f"Couldn't reach WhatsApp: {e}")
            return False
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                msg = err.get("error_user_msg") or err.get("message") or r.text[:200]
            except ValueError:
                msg = r.text[:200]
            if r.status_code == 401:
                msg = "the WhatsApp token was rejected or has expired; make a permanent system-user token"
            self.on_status(f"WhatsApp send failed: {msg}")
            return False
        return True

    def send(self, text: str) -> bool:
        ok = True
        for i in range(0, max(1, len(text)), MAX_LEN):
            ok = self._post({"type": "text", "text": {"preview_url": False, "body": text[i:i + MAX_LEN] or "…"}}) and ok
        return ok

    def send_buttons(self, text: str, buttons: list[tuple[str, str]]) -> bool:
        return self._post({"type": "interactive", "interactive": {
            "type": "button", "body": {"text": text[:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": bid, "title": title[:20]}}
                                   for bid, title in buttons[:3]]}}})

    def send_alert(self, text: str) -> bool:
        """Free text inside the 24-hour window; the approved template outside it."""
        if self._in_window():
            return self.send(text)
        if not self.template:
            return False  # no template: it stays in the Notices panel until the owner writes
        return self._post({"type": "template", "template": {
            "name": self.template, "language": {"code": self.template_lang},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": text[:1000]}]}]}})

    # ---- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        missing = self.missing()
        if missing:
            self.on_status("WhatsApp link off: add " + ", ".join(missing) + " to .env.")
            return
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler_class())
        except OSError as e:
            self.on_status(f"WhatsApp link off: port {self.port} is busy ({e}).")
            return
        threading.Thread(target=self._server.serve_forever, daemon=True, name="whatsapp-webhook").start()
        if self.start_tunnel and self.domain:
            self._open_tunnel()
        threading.Thread(target=self._push_loop, daemon=True, name="whatsapp-push").start()
        where = f"https://{self.domain}/webhook" if self.domain else f"http://127.0.0.1:{self.port}/webhook (no tunnel set)"
        self.on_status(f"WhatsApp link on. Webhook: {where}  ·  verify token: {self.state['verify_token']}")

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            self._server.shutdown()
        if self._tunnel and self._tunnel.poll() is None:
            self._tunnel.terminate()

    def _open_tunnel(self) -> None:
        exe = shutil.which("ngrok") or next((str(p) for p in Path(os.environ.get("LOCALAPPDATA", "")).glob(
            "Microsoft/WinGet/Packages/Ngrok.Ngrok*/ngrok.exe")), None)
        if not exe:
            self.on_status("WhatsApp: ngrok isn't installed, so Meta can't reach Fafnir yet.")
            return
        try:
            self._tunnel = subprocess.Popen(
                [exe, "http", f"--url={self.domain}", str(self.port), "--log=stdout"],
                stdout=open(ROOT / "logs" / "ngrok.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW)
        except OSError as e:
            self.on_status(f"WhatsApp: couldn't start the ngrok tunnel ({e}).")
            return
        time.sleep(3)
        if self._tunnel.poll() is not None:
            self.on_status("WhatsApp: the ngrok tunnel stopped. Check logs\\ngrok.log (is your ngrok authtoken set?).")

    # ---- webhook (incoming) -----------------------------------------------------------
    def _handler_class(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep quiet
                pass

            def do_GET(self):  # Meta's one-time verification
                q = parse_qs(urlparse(self.path).query)
                ok = (q.get("hub.mode", [""])[0] == "subscribe"
                      and hmac.compare_digest(q.get("hub.verify_token", [""])[0], bridge.state["verify_token"]))
                body = q.get("hub.challenge", [""])[0].encode() if ok else b"forbidden"
                self.send_response(200 if ok else 403)
                self.end_headers()
                self.wfile.write(body)
                if ok:
                    bridge.on_status("WhatsApp webhook verified by Meta.")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                sig = self.headers.get("X-Hub-Signature-256", "")
                good = "sha256=" + hmac.new(bridge.app_secret.encode(), raw, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig, good):
                    self.send_response(403)
                    self.end_headers()
                    return
                self.send_response(200)  # acknowledge fast; work happens in the background
                self.end_headers()
                try:
                    data = json.loads(raw)
                except ValueError:
                    return
                threading.Thread(target=bridge._handle, args=(data,), daemon=True).start()

        return Handler

    def _handle(self, data: dict) -> None:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                for m in change.get("value", {}).get("messages", []) or []:
                    try:
                        self._handle_message(m)
                    except Exception as e:
                        self.on_status(f"WhatsApp message failed: {e}")

    def _handle_message(self, m: dict) -> None:
        mid = m.get("id", "")
        if mid in self._seen:
            return
        self._seen.add(mid)
        if _digits(m.get("from", "")) != self.owner:
            return  # not the owner: ignore completely
        self.state["last_inbound"] = datetime.now().isoformat(timespec="seconds")
        self._save()
        kind = m.get("type")
        if kind == "interactive":
            reply_id = m.get("interactive", {}).get("button_reply", {}).get("id", "")
            if reply_id in ("yes", "no"):
                self.engine.answer_confirm(reply_id == "yes")
            return
        if kind == "button":  # a quick-reply button on a template
            text = m.get("button", {}).get("text", "")
        elif kind == "text":
            text = m.get("text", {}).get("body", "")
        else:
            self.send("I can only read text messages for now.")
            return
        text = text.strip()
        if not text:
            return
        low = text.lower()
        if low in ("/pause", "pause notices"):
            from .tools import HEARTBEAT
            if HEARTBEAT.get("hb"):
                HEARTBEAT["hb"].pause(0)
            self.send("Paused. No notices until you send /resume.")
            return
        if low in ("/resume", "resume notices"):
            from .tools import HEARTBEAT
            if HEARTBEAT.get("hb"):
                HEARTBEAT["hb"].resume()
            self.send("Notices are back on.")
            return
        self.on_status(f"From your phone: {text}")
        self.engine.submit_text(text, source="phone", reply=lambda answer: self.send(answer))

    # ---- confirmations ---------------------------------------------------------------------
    def _confirm_on_phone(self, action: str, source: str) -> None:
        if source != "phone":
            return  # requests made at the PC are confirmed at the PC
        main, _, cmd = action.partition(CONFIRM_MARK)
        text = f"I need your OK:\n{main}" + (f"\n\n{cmd}" if cmd else "") + "\n\n(No answer in 60 seconds means no.)"
        self.send_buttons(text, [("yes", "Yes, do it"), ("no", "No")])

    # ---- alerts ----------------------------------------------------------------------------
    def _push_loop(self) -> None:
        while not self._stop.wait(10):
            from .tools import HEARTBEAT
            hb = HEARTBEAT.get("hb")
            if hb is not None and hb.paused:
                continue
            stale = (datetime.now() - timedelta(hours=6)).isoformat()
            for n in self.notices.active():
                if n.pushed or n.urgency not in self.push_levels:
                    continue
                if n.created < stale:  # too old to be worth a phone alert (still in the panel)
                    self.notices.mark_pushed(n.id)
                    continue
                icon = {"urgent": "🚨", "important": "🔔"}.get(n.urgency, "•")
                if self.send_alert(f"{icon} {n.title}" + (f" — {n.detail}" if n.detail else "")):
                    self.notices.mark_pushed(n.id)


def make_bridge(engine, notices, on_status, name: str):
    try:
        with open(ROOT / "config.toml", "rb") as f:
            settings = tomllib.load(f).get("phone", {})
    except (OSError, tomllib.TOMLDecodeError):
        settings = {}
    if not settings.get("enabled", True):
        return None
    if not any(os.environ.get(k) for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")):
        return None  # not set up yet: stay quiet
    return WhatsAppBridge(engine, notices, settings, on_status, name)
