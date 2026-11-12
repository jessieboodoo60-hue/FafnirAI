"""Decide what goes in the "Right now" strip: the most important things at this moment,
ranked, plus a one-line briefing. Pure functions over plain data, so it's easy to test.

Order (most important first): urgent notices, a meeting within the hour, overdue tasks,
important notices, VIP or important email, tasks due today, the next event later today,
then business & social.
"""
from __future__ import annotations

from datetime import date, datetime


def _when(start: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(start)
        return dt if dt.tzinfo else dt.astimezone()
    except (TypeError, ValueError):
        return None


def _minutes(dt: datetime) -> int:
    return round((dt - datetime.now().astimezone()).total_seconds() / 60)


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def rank(data: dict, vip_senders: list[str] | None = None, limit: int = 6) -> tuple[str, list[dict]]:
    """Returns (briefing, items). Each item: {score, icon, title, detail, tag}."""
    vips = [v.lower() for v in (vip_senders or [])]
    today = date.today().isoformat()
    items: list[dict] = []
    add = lambda score, icon, title, detail="", tag="info": items.append(  # noqa: E731
        {"score": score, "icon": icon, "title": title, "detail": detail, "tag": tag})

    # heartbeat notices (calendar/task notices are covered by the live data below)
    for n in data.get("notices", []):
        if getattr(n, "check", "") in ("calendar", "tasks"):
            continue
        if n.urgency == "urgent":
            add(0, "🚨", n.title, n.detail, "urgent")
        elif n.urgency == "important":
            add(3, "🔔", n.title, n.detail, "important")

    # calendar
    next_event = None
    for e in data.get("events", []):
        if e.get("all_day"):
            continue
        dt = _when(e.get("start", ""))
        if dt is None:
            continue
        mins = _minutes(dt)
        if mins < -5:
            continue  # already under way or over
        if next_event is None:
            next_event = (e, dt, mins)
        if mins <= 60:
            add(1, "📅", e.get("title", "Event"), "starting now" if mins <= 0 else f"in {mins} min · {_clock(dt)}",
                "urgent" if mins <= 15 else "important")
        elif dt.date().isoformat() == today:
            add(6, "📅", e.get("title", "Event"), f"today at {_clock(dt)}", "info")

    # tasks: the business tracker and Google Tasks
    overdue = due_today = 0
    for t in data.get("tasks", []) + data.get("gtasks", []):
        due = (t.get("due") or "")[:10]
        if not due or t.get("done"):
            continue
        if due < today:
            overdue += 1
            add(2, "⏰", t.get("title", "Task"), f"overdue · was due {due}", "urgent")
        elif due == today:
            due_today += 1
            add(5, "✅", t.get("title", "Task"), "due today", "important")

    # email
    important_mail = 0
    for m in data.get("emails", []):
        sender = (m.get("from") or "")
        name = sender.split("<")[0].strip().strip('"') or sender
        vip = any(v in sender.lower() for v in vips)
        important_mail += 1
        add(4 if vip else 7, "✉️", f"{name}: {m.get('subject') or '(no subject)'}",
            "VIP · unread" if vip else "unread", "important" if vip else "info")

    # business & social
    yt = data.get("youtube") or {}
    if yt.get("title"):
        bits = [f"{int(yt[k]):,} {label}" for k, label in (("subscribers", "subs"), ("views", "views"))
                if str(yt.get(k, "")).isdigit()]
        add(9, "▶️", yt["title"], " · ".join(bits) or "YouTube", "good")
    open_business = sum(1 for t in data.get("tasks", []) if not t.get("due"))
    if open_business:
        add(9.5, "💼", f"{open_business} open business item{'s' if open_business != 1 else ''}", "no due date", "info")

    items.sort(key=lambda i: i["score"])
    return _briefing(next_event, overdue, due_today, important_mail, data), items[:limit]


def _briefing(next_event, overdue: int, due_today: int, mail: int, data: dict) -> str:
    parts = []
    if any(getattr(n, "urgency", "") == "urgent" for n in data.get("notices", [])):
        parts.append("Something urgent needs you")
    if next_event:
        e, dt, mins = next_event
        if mins <= 60:
            parts.append(f"{e.get('title', 'Next event')} {'now' if mins <= 0 else f'in {mins} min'}")
        elif dt.date() == date.today():
            parts.append(f"next up: {e.get('title', 'an event')} at {_clock(dt)}")
    if overdue:
        parts.append(f"{overdue} overdue task{'s' if overdue != 1 else ''}")
    if due_today:
        parts.append(f"{due_today} due today")
    if mail:
        parts.append(f"{mail} important email{'s' if mail != 1 else ''}")
    if not parts:
        return "All clear. Nothing needs you right now."
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]
