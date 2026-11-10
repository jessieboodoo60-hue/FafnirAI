"""Let Fafnir read and clear its own notices, and pause or resume the heartbeat by voice."""
from __future__ import annotations

from ..heartbeat.notices import NoticeStore
from .registry import Registry, Tool, ToolError


def register(reg: Registry, store: NoticeStore, heartbeat_ref: dict) -> None:
    """`heartbeat_ref["hb"]` is filled in once the heartbeat starts (it may not run in every mode)."""

    def hb():
        h = heartbeat_ref.get("hb")
        if h is None:
            raise ToolError("The heartbeat isn't running in this mode (it runs with the desktop app).")
        return h

    def notices_list() -> dict:
        return {"notices": [{"id": n.id, "title": n.title, "detail": n.detail, "urgency": n.urgency,
                             "when": n.created[:16].replace("T", " ")} for n in store.active()]}

    def notices_dismiss(id: str) -> dict:
        n = store.dismiss(id)
        return {"dismissed": n}

    def heartbeat_pause(minutes: int = 0) -> dict:
        until = hb().pause(minutes)
        return {"paused_until": "until resumed" if until == "forever" else until}

    def heartbeat_resume() -> dict:
        hb().resume()
        return {"resumed": True}

    def heartbeat_status() -> dict:
        return hb().status()

    reg.register(Tool(name="notices_list", handler=notices_list, properties={},
                      description="List the notices the heartbeat has held for the owner (reminders, due tasks, "
                                  "new email, low disk, new comments). Use for 'what did I miss?' or 'any notices?'."))
    reg.register(Tool(name="notices_dismiss", handler=notices_dismiss, required=["id"],
                      properties={"id": {"type": "string", "description": "A notice id, or 'all'."}},
                      description="Dismiss a notice (or 'all') once the owner has seen or dealt with it."))
    reg.register(Tool(name="heartbeat_pause", handler=heartbeat_pause,
                      properties={"minutes": {"type": "integer", "description": "0 = until resumed."}},
                      description="Pause all proactive checks and announcements (do-not-disturb / kill switch)."))
    reg.register(Tool(name="heartbeat_resume", handler=heartbeat_resume, properties={},
                      description="Turn proactive checks and announcements back on."))
    reg.register(Tool(name="heartbeat_status", handler=heartbeat_status, properties={},
                      description="Show whether the heartbeat is paused and when each check last ran or failed."))
