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


def _register_calculator(reg: Registry) -> None:
    import ast
    import math
    import operator as op
    from .registry import Tool, ToolError

    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.FloorDiv: op.floordiv,
           ast.Mod: op.mod, ast.Pow: op.pow, ast.USub: op.neg, ast.UAdd: op.pos}
    funcs = {"sqrt": math.sqrt, "round": round, "abs": abs, "min": min, "max": max, "floor": math.floor, "ceil": math.ceil}

    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(b) > 200 or abs(a) > 1e9):
                raise ToolError("That power is too large.")
            return ops[type(node.op)](a, b)
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in funcs:
            return funcs[node.func.id](*[ev(a) for a in node.args])
        raise ToolError("Only numbers and + - * / % ** ( ) sqrt round abs min max floor ceil are allowed.")

    def calculate(expression: str) -> dict:
        expr = expression.replace("^", "**").replace("x", "*").replace("×", "*").replace("÷", "/").replace(",", "")
        try:
            value = ev(ast.parse(expr, mode="eval").body)
        except ToolError:
            raise
        except ZeroDivisionError:
            raise ToolError("Division by zero.")
        except Exception:
            raise ToolError(f"Couldn't read '{expression}' as arithmetic. Write it like 17*23 or (120+35)/5.")
        return {"expression": expression, "result": round(value, 10) if isinstance(value, float) else value}

    reg.register(Tool(
        name="calculate", handler=calculate, required=["expression"],
        description="Exact arithmetic. Use for ANY calculation (multiplying, dividing, percentages, totals) instead of "
                    "working it out yourself. Give a plain expression like 17*23 or 1250*0.15 or (120+35)/5.",
        properties={"expression": {"type": "string"}}))


def _register_health_tool(reg: Registry) -> None:
    from .registry import Tool

    def health() -> dict:
        m = RESOURCES.get("health")
        if m is None:
            from .. import health as h
            rows = h.static_report()
            return {"overall": "checked without the live app", "everything": [{"what": r["check"], "status": r["status"], "detail": r["detail"]} for r in rows]}
        m.check()
        return m.summary()

    reg.register(Tool(
        name="health_status", handler=health, properties={},
        description="Fafnir's self-diagnostics: processor, memory, disks, graphics card, the local model and its "
                    "speed, microphone and voice, phone links, backups, the kill switch and security signals, plus "
                    "anything it recently repaired by itself. Use for 'how are you doing', 'run diagnostics', "
                    "'is everything working', 'why are you slow'."))


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
    _register_calculator(reg)
    _register_profile_tool(reg)
    _register_privacy_tool(reg)
    _register_health_tool(reg)
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
