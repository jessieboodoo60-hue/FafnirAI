"""Builds Fafnir's tool registry. Add a capability: write a module, register it here."""
from __future__ import annotations

from ..config import ROOT, Config
from ..conversations import ConversationLog
from ..heartbeat.notices import NoticeStore
from ..memory import MemoryStore
from . import apps, business, cameras, computer, control, desktop_control, heartbeat_tools, installer, memory_tools, notes, private_vault, screen_tools, weather
from .registry import Registry

# Shared by the tools and the front ends: the notice inbox, and the running heartbeat
# (filled in by whichever front end starts it).
NOTICES = NoticeStore(ROOT / "data" / "notices.json")
HEARTBEAT: dict = {}
RESOURCES: dict = {}   # {"manager": ResourceManager} once the engine starts it
CONVERSATIONS = ConversationLog(ROOT / "data" / "conversations")


def _register_profile_tool(reg: Registry) -> None:
    from .registry import Tool, ToolError

    def profile(mode: str = "") -> dict:
        m = RESOURCES.get("manager")
        if m is None:
            raise ToolError("The performance manager isn't running (it starts with the app).")
        try:
            return m.set_mode(mode) if mode else m.status()
        except ValueError as e:
            raise ToolError(str(e))

    reg.register(Tool(
        name="performance_profile", handler=profile,
        description="Fafnir's performance profile: how much of the graphics card it uses. No mode = report the "
                    "current profile, graphics memory and whether a game is running. Modes: 'auto' (decides by "
                    "itself, gaming while a game runs), 'gaming' (frees the graphics card for the game), "
                    "'balanced', 'ai_performance' (best local brain). Use for 'gaming mode', 'free up my GPU', "
                    "'AI performance mode', 'what profile are you in'.",
        properties={"mode": {"type": "string", "enum": ["auto", "gaming", "balanced", "ai_performance"]}}))


def _register_privacy_tool(reg: Registry) -> None:
    from .registry import Tool

    def report(hours: int = 24) -> dict:
        from .. import audit
        hours = max(1, min(int(hours or 24), 24 * 30))
        sent = audit.outbound_summary(hours)
        return {"hours": hours, "sent_off_this_pc": sent or "nothing",
                "note": "Counts and sizes only; the content of what was sent is never logged. "
                        "Everything not listed stayed on this PC or the home network."}

    reg.register(Tool(
        name="privacy_report", handler=report,
        description="What Fafnir sent off this PC recently: which outside services were contacted, how many "
                    "times, roughly how much, and why. Use for 'what have you sent out', 'did anything leave my "
                    "PC', 'privacy report'.",
        properties={"hours": {"type": "integer", "description": "How far back; default 24."}}))


def build_registry(cfg: Config, memory: MemoryStore) -> Registry:
    t = cfg.tools
    reg = Registry(log_path=ROOT / "logs" / "actions.log")
    memory_tools.register(reg, memory, CONVERSATIONS)
    heartbeat_tools.register(reg, NOTICES, HEARTBEAT)
    screen_tools.register(reg)
    installer.register(reg, NOTICES)          # install / uninstall software and games, then open them
    desktop_control.register(reg)             # see the screen, use the mouse and keyboard
    cameras.register(reg)                     # Hikvision DVR / security cameras
    private_vault.register(reg, t.private_folder)   # encrypted passport/IDs: always asks before unlocking
    _register_profile_tool(reg)
    _register_privacy_tool(reg)
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
