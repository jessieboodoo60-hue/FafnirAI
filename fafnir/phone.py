"""Reach the owner on their phone through a private Telegram bot.

- Two-way chat: messages from the owner's Telegram go to Fafnir like typed messages, and
  the answer comes back as a Telegram message.
- Alerts: important heartbeat notices are pushed to the phone (once each).
- Confirmations for phone requests arrive with Yes/No buttons.

Security: only the paired Telegram chat is listened to; everyone else is ignored. Pairing
happens once: the app shows a 6-digit code, and the owner sends `/pair <code>` to the bot.
The bot token lives in .env (TELEGRAM_BOT_TOKEN); the paired chat id in data/phone.json.

Works from anywhere with no router changes: Fafnir polls Telegram for new messages.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from .config import ROOT
from .tools.control import CONFIRM_MARK

API = "https://api.telegram.org/bot{token}/{method}"
STATE = ROOT / "data" / "phone.json"
MAX_LEN = 4000  # Telegram's limit is 4096


class TelegramBridge:
    def __init__(self, token: str, engine, notices, push_levels=("important", "urgent"),
                 on_status: Callable[[str], None] = lambda s: None, name: str = "Fafnir"):
        self.token, self.engine, self.notices = token, engine, notices
        self.push_levels = set(push_levels)
        self.on_status, self.name = on_status, name
        self.http = httpx.Client(timeout=httpx.Timeout(10.0, read=65.0))
        self.state = self._load()
        self.pair_code = None if self.chat_id else f"{random.randint(0, 999999):06d}"
        self._offset = self.state.get("offset", 0)
        self._stop = threading.Event()
        self._confirm_msg: int | None = None
        engine.confirm_notifiers.append(self._confirm_on_phone)

    # ---- state -------------------------------------------------------------------
    @property
    def chat_id(self) -> int | None:
        return self.state.get("chat_id")

    def _load(self) -> dict:
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(self.state), encoding="utf-8")

    # ---- Telegram API ---------------------------------------------------------------
    def _call(self, method: str, **params):
        r = self.http.post(API.format(token=self.token, method=method), json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"Telegram error {r.status_code}"))
        return data["result"]

    def send(self, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        if not self.chat_id:
            return None
        msg_id = None
        for i in range(0, max(1, len(text)), MAX_LEN):
            params = {"chat_id": self.chat_id, "text": text[i:i + MAX_LEN] or "…"}
            if buttons and i + MAX_LEN >= len(text):
                params["reply_markup"] = {"inline_keyboard": buttons}
            try:
                msg_id = self._call("sendMessage", **params)["message_id"]
            except Exception as e:
                self.on_status(f"Couldn't send to your phone: {e}")
                return None
        return msg_id

    # ---- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        try:
            me = self._call("getMe")
            self.bot_name = me.get("username", "your bot")
        except Exception as e:
            self.on_status(f"Phone link off: the Telegram bot token didn't work ({e}).")
            return
        threading.Thread(target=self._poll_loop, daemon=True, name="telegram").start()
        threading.Thread(target=self._push_loop, daemon=True, name="telegram-push").start()
        if self.chat_id:
            self.on_status(f"Phone linked through Telegram (@{self.bot_name}).")
        else:
            self.on_status(f"Phone not paired yet: in Telegram, send  /pair {self.pair_code}  to @{self.bot_name}")

    def stop(self) -> None:
        self._stop.set()

    # ---- incoming messages ----------------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = self._call("getUpdates", offset=self._offset, timeout=50,
                                     allowed_updates=["message", "callback_query"])
            except Exception:
                time.sleep(5)  # network blip; try again
                continue
            for u in updates:
                self._offset = u["update_id"] + 1
                try:
                    self._handle(u)
                except Exception as e:
                    self.on_status(f"Phone message failed: {e}")
            if updates:
                self.state["offset"] = self._offset
                self._save()

    def _handle(self, u: dict) -> None:
        if "callback_query" in u:
            q = u["callback_query"]
            if q.get("message", {}).get("chat", {}).get("id") != self.chat_id:
                return
            approved = q.get("data") == "yes"
            self.engine.answer_confirm(approved)
            try:
                self._call("answerCallbackQuery", callback_query_id=q["id"],
                           text="Approved" if approved else "Declined")
                self._call("editMessageReplyMarkup", chat_id=self.chat_id,
                           message_id=q["message"]["message_id"], reply_markup={"inline_keyboard": []})
            except Exception:
                pass
            return
        msg = u.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat or not text:
            return
        if not self.chat_id:  # not paired: only accept the pairing code
            if self.pair_code and text in (f"/pair {self.pair_code}", self.pair_code):
                self.state["chat_id"] = chat
                self._save()
                self.pair_code = None
                self.send(f"Paired. This chat now talks to {self.name}. Message me anything, "
                          "and I'll send important notices here.")
                self.on_status("Phone paired through Telegram.")
            return
        if chat != self.chat_id:
            return  # a stranger: ignore completely
        if text in ("/start", "/help"):
            self.send("Message me anything, like you'd say it to me at the PC. "
                      "/pause and /resume control notifications.")
            return
        if text == "/pause":
            from .tools import HEARTBEAT
            if HEARTBEAT.get("hb"):
                HEARTBEAT["hb"].pause(0)
            self.send("Paused. No notices until you send /resume.")
            return
        if text == "/resume":
            from .tools import HEARTBEAT
            if HEARTBEAT.get("hb"):
                HEARTBEAT["hb"].resume()
            self.send("Notices are back on.")
            return
        try:
            self._call("sendChatAction", chat_id=self.chat_id, action="typing")
        except Exception:
            pass
        self.on_status(f"From your phone: {text}")
        self.engine.submit_text(text, source="phone", reply=lambda answer: self.send(answer))

    # ---- confirmations ---------------------------------------------------------------------
    def _confirm_on_phone(self, action: str, source: str) -> None:
        if source != "phone" or not self.chat_id:
            return  # requests made at the PC are confirmed at the PC
        main, _, cmd = action.partition(CONFIRM_MARK)
        text = f"I need your OK:\n{main}" + (f"\n\n{cmd}" if cmd else "") + "\n\n(No answer in 60 seconds means no.)"
        self.send(text, buttons=[[{"text": "Yes, do it", "callback_data": "yes"},
                                  {"text": "No", "callback_data": "no"}]])

    # ---- alerts ----------------------------------------------------------------------
    def _push_loop(self) -> None:
        while not self._stop.wait(10):
            if not self.chat_id:
                continue
            from .tools import HEARTBEAT
            hb = HEARTBEAT.get("hb")
            if hb is not None and hb.paused:
                continue
            for n in self.notices.active():
                if n.pushed or n.urgency not in self.push_levels:
                    continue
                icon = {"urgent": "🚨", "important": "🔔"}.get(n.urgency, "•")
                if self.send(f"{icon} {n.title}" + (f"\n{n.detail}" if n.detail else "")) is not None:
                    self.notices.mark_pushed(n.id)


def make_bridge(engine, notices, on_status, name: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    import tomllib
    try:
        with open(ROOT / "config.toml", "rb") as f:
            phone = tomllib.load(f).get("phone", {})
    except (OSError, tomllib.TOMLDecodeError):
        phone = {}
    if not phone.get("enabled", True):
        return None
    levels = {"important": ("important", "urgent"), "all": ("info", "important", "urgent"),
              "urgent": ("urgent",)}.get(phone.get("send", "important"), ("important", "urgent"))
    return TelegramBridge(token, engine, notices, levels, on_status, name)
