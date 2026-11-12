"""Builds Fafnir's tool registry. Add a capability: write a module, register it here."""
from __future__ import annotations

from ..config import ROOT, Config
from ..conversations import ConversationLog
from ..heartbeat.notices import NoticeStore
from ..memory import MemoryStore
from . import apps, business, computer, control, heartbeat_tools, memory_tools, notes, screen_tools, weather
from .registry import Registry

# Shared by the tools and the front ends: the notice inbox, and the running heartbeat
# (filled in by whichever front end starts it).
NOTICES = NoticeStore(ROOT / "data" / "notices.json")
HEARTBEAT: dict = {}
CONVERSATIONS = ConversationLog(ROOT / "data" / "conversations")


def build_registry(cfg: Config, memory: MemoryStore) -> Registry:
    t = cfg.tools
    reg = Registry(log_path=ROOT / "logs" / "actions.log")
    memory_tools.register(reg, memory, CONVERSATIONS)
    heartbeat_tools.register(reg, NOTICES, HEARTBEAT)
    screen_tools.register(reg)
    business.register(reg, ROOT / "data")
    computer.register(reg)
    apps.register(reg)
    control.register(reg)
    notes.register(reg, t.notes_folder)
    weather.register(reg, cfg.assistant.home_location)
    if t.google:
        from ..google_auth import GoogleAuth
        from . import google_more, google_tools
        gauth = GoogleAuth(ROOT / "data" / "google_credentials.json", ROOT / "data" / "google_token.json")
        google_tools.register(reg, gauth)   # Calendar, Gmail
        google_more.register(reg, gauth)    # Drive/Docs/Sheets, Contacts, Tasks, YouTube
    if t.web:
        # Anthropic-hosted: run on Anthropic's side, results come back in the reply.
        reg.server_tools += [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": t.web_max_uses},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": t.web_max_uses},
        ]
    return reg
