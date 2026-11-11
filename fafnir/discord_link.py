"""Reach the owner on their phone through a private Discord bot.

- Two-way chat: DMs from the owner go to Fafnir like typed messages; the answer comes back
  as a DM.
- Alerts: important heartbeat notices are DMed to the owner (once each).
- Confirmations for phone requests arrive with Yes / No buttons.

Security: only the paired Discord user is listened to; everyone else is ignored. Pairing
happens once: the app shows a 6-digit code, and the owner DMs `/pair <code>` to the bot.
The bot token lives in .env (DISCORD_BOT_TOKEN); the paired user id in data/discord.json.

The bot connects out to Discord, so no router changes or tunnels are needed. Discord only
lets a bot DM people it shares a server with, so the bot sits in a small private server.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import tomllib
from datetime import datetime, timedelta
from typing import Callable

import discord

from .config import ROOT
from .tools.control import CONFIRM_MARK

STATE = ROOT / "data" / "discord.json"
SOURCE = "phone:discord"
MAX_LEN = 1900   # Discord's limit is 2000 characters per message


class _ConfirmView(discord.ui.View):
    def __init__(self, engine):
        super().__init__(timeout=65)
        self.engine = engine

    async def _answer(self, interaction: discord.Interaction, approved: bool) -> None:
        self.engine.answer_confirm(approved)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Approved." if approved else "Declined.")
        self.stop()

    @discord.ui.button(label="Yes, do it", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._answer(interaction, True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._answer(interaction, False)


class DiscordBridge:
    def __init__(self, token: str, engine, notices, push_levels=("important", "urgent"),
                 on_status: Callable[[str], None] = lambda s: None, name: str = "Fafnir"):
        self.token, self.engine, self.notices = token, engine, notices
        self.push_levels, self.on_status, self.name = set(push_levels), on_status, name
        self.state = self._load()
        self.pair_code = None if self.owner_id else f"{random.randint(0, 999999):06d}"
        self.loop: asyncio.AbstractEventLoop | None = None
        intents = discord.Intents.default()
        intents.dm_messages = True          # DMs carry their text without the privileged intent
        self.client = discord.Client(intents=intents)
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        engine.confirm_notifiers.append(self._confirm_on_phone)

    # ---- state -------------------------------------------------------------------
    @property
    def owner_id(self) -> int | None:
        return self.state.get("owner_id")

    def _load(self) -> dict:
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(self.state), encoding="utf-8")

    # ---- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="discord").start()

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.client.start(self.token))
        except discord.LoginFailure:
            self.on_status("Discord link off: the bot token was rejected. Check DISCORD_BOT_TOKEN in .env.")
        except discord.PrivilegedIntentsRequired:
            self.on_status("Discord link off: an intent isn't enabled in the Developer Portal.")
        except Exception as e:
            self.on_status(f"Discord link stopped: {type(e).__name__}: {e}")

    def stop(self) -> None:
        if self.loop and self.client:
            asyncio.run_coroutine_threadsafe(self.client.close(), self.loop)

    async def on_ready(self) -> None:
        me = self.client.user
        if self.owner_id:
            self.on_status(f"Discord linked: DM {me} to talk to {self.name}.")
        else:
            self.on_status(f"Discord not paired yet: DM the bot {me} the message  /pair {self.pair_code}")
        self.client.loop.create_task(self._push_loop())

    # ---- sending (safe from any thread) -----------------------------------------------
    async def _owner(self) -> discord.User | None:
        if not self.owner_id:
            return None
        return self.client.get_user(self.owner_id) or await self.client.fetch_user(self.owner_id)

    async def _send(self, text: str, view: discord.ui.View | None = None) -> bool:
        user = await self._owner()
        if user is None:
            return False
        try:
            chunks = [text[i:i + MAX_LEN] for i in range(0, max(1, len(text)), MAX_LEN)] or ["…"]
            for i, chunk in enumerate(chunks):
                await user.send(chunk or "…", view=view if i == len(chunks) - 1 else None)
            return True
        except discord.HTTPException as e:
            self.on_status(f"Couldn't DM you on Discord: {e}")
            return False

    def send(self, text: str, view=None) -> None:
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._send(text, view), self.loop)

    # ---- incoming --------------------------------------------------------------------------
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return  # only DMs, never server channels
        text = (message.content or "").strip()
        if not text:
            return
        if not self.owner_id:
            if self.pair_code and text in (f"/pair {self.pair_code}", self.pair_code):
                self.state["owner_id"] = message.author.id
                self._save()
                self.pair_code = None
                await message.channel.send(f"Paired. This DM now talks to {self.name}. Message me anything, "
                                           "and I'll send important notices here.")
                self.on_status(f"Discord paired with {message.author}.")
            return
        if message.author.id != self.owner_id:
            return  # a stranger: ignore completely
        low = text.lower()
        if low in ("/help", "help"):
            await message.channel.send("Message me anything, like you'd say it at the PC. "
                                       "/pause and /resume control notifications.")
            return
        if low in ("/pause", "/resume"):
            from .tools import HEARTBEAT
            hb = HEARTBEAT.get("hb")
            if hb:
                hb.pause(0) if low == "/pause" else hb.resume()
            await message.channel.send("Paused. No notices until /resume." if low == "/pause" else "Notices are back on.")
            return
        self.on_status(f"From your phone: {text}")
        async with message.channel.typing():
            pass  # show "typing…" briefly; the reply arrives when Fafnir finishes
        self.engine.submit_text(text, source=SOURCE, reply=lambda answer: self.send(answer))

    # ---- confirmations -----------------------------------------------------------------------
    def _confirm_on_phone(self, action: str, source: str) -> None:
        if source != SOURCE:
            return  # confirmed wherever the request came from
        main, _, cmd = action.partition(CONFIRM_MARK)
        text = f"**I need your OK:**\n{main}" + (f"\n```{cmd}```" if cmd else "") + "\n(No answer in 60 seconds means no.)"
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._send(text, _ConfirmView(self.engine)), self.loop)

    # ---- alerts --------------------------------------------------------------------------------
    async def _push_loop(self) -> None:
        while not self.client.is_closed():
            await asyncio.sleep(10)
            if not self.owner_id:
                continue
            from .tools import HEARTBEAT
            hb = HEARTBEAT.get("hb")
            if hb is not None and hb.paused:
                continue
            stale = (datetime.now() - timedelta(hours=6)).isoformat()
            for n in self.notices.active():
                if n.pushed or n.urgency not in self.push_levels:
                    continue
                if n.created < stale:
                    self.notices.mark_pushed(n.id)
                    continue
                icon = {"urgent": "🚨", "important": "🔔"}.get(n.urgency, "•")
                if await self._send(f"{icon} **{n.title}**" + (f"\n{n.detail}" if n.detail else "")):
                    self.notices.mark_pushed(n.id)


def make_bridge(engine, notices, on_status, name: str):
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return None
    try:
        with open(ROOT / "config.toml", "rb") as f:
            phone = tomllib.load(f).get("phone", {})
    except (OSError, tomllib.TOMLDecodeError):
        phone = {}
    if not phone.get("enabled", True):
        return None
    levels = {"important": ("important", "urgent"), "all": ("info", "important", "urgent"),
              "urgent": ("urgent",)}.get(phone.get("send", "important"), ("important", "urgent"))
    return DiscordBridge(token, engine, notices, levels, on_status, name)
