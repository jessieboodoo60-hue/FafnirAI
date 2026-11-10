"""The heartbeat's checks. Each looks at one thing and returns notices worth filing.

Every notice has a stable `key`, so the same thing is reported once. Settings come from
[heartbeat.<check>] in config.toml. All checks only read; none change anything.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .notices import Notice
from .runner import Check, CheckContext


def _time(s: str) -> str:
    try:
        return datetime.fromisoformat(s).strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return s


def calendar_soon(ctx: CheckContext) -> list[Notice]:
    """Events starting within lead_minutes."""
    if not ctx.google_ready:
        return []
    lead = float(ctx.settings.get("lead_minutes", 15))
    now = datetime.now().astimezone()
    res = ctx.registry.read("calendar_list_events", start=now.isoformat(timespec="minutes"),
                            end=(now + timedelta(minutes=lead)).isoformat(timespec="minutes"), max_results=10)
    out = []
    for e in res.get("events", []):
        if e.get("all_day"):
            continue
        start = datetime.fromisoformat(e["start"])
        if start < now - timedelta(minutes=1):
            continue  # already started
        mins = max(0, round((start - now).total_seconds() / 60))
        where = f" · {e['location']}" if e.get("location") else ""
        out.append(Notice(key=f"cal:{e['id']}:{e['start']}", check="calendar",
                          title=f"{e['title']} in {mins} minutes" if mins else f"{e['title']} is starting now",
                          detail=f"{_time(e['start'])}{where}", urgency="important"))
    return out


def tasks_due(ctx: CheckContext) -> list[Notice]:
    """Business-tracker items and Google Tasks due today or overdue (once per item per day)."""
    today = date.today().isoformat()
    out = []
    items = ctx.registry.read("business_list_items", status="open", due_on_or_before=today).get("items", [])
    for t in items:
        overdue = t["due"] < today
        out.append(Notice(key=f"task:{t['id']}:{today}", check="tasks",
                          title=f"Overdue: {t['title']}" if overdue else f"Due today: {t['title']}",
                          detail=f"business {t['kind']}" + (f" · was due {t['due']}" if overdue else ""),
                          urgency="important" if overdue else "info"))
    if ctx.google_ready and "google_tasks_list" in ctx.registry.names():
        for t in ctx.registry.read("google_tasks_list").get("tasks", []):
            if t.get("due") and t["due"] <= today and not t.get("done"):
                overdue = t["due"] < today
                out.append(Notice(key=f"gtask:{t['id']}:{today}", check="tasks",
                                  title=f"Overdue: {t['title']}" if overdue else f"Due today: {t['title']}",
                                  detail="Google Tasks", urgency="important" if overdue else "info"))
    return out


def email_new(ctx: CheckContext) -> list[Notice]:
    """New email matching the configured search. VIP senders are spoken; the rest are quiet."""
    if not ctx.google_ready:
        return []
    query = ctx.settings.get("query", "is:unread is:important newer_than:2d")
    vips = [v.lower() for v in ctx.settings.get("vip_senders", [])]
    res = ctx.registry.read("gmail_search", query=query, max_results=15)
    out = []
    for m in res.get("emails", []):
        sender = (m.get("from") or "").strip()
        name = sender.split("<")[0].strip().strip('"') or sender
        vip = any(v in sender.lower() for v in vips)
        out.append(Notice(key=f"mail:{m['id']}", check="email",
                          title=f"Email from {name}: {m.get('subject') or '(no subject)'}",
                          detail=(m.get("snippet") or "")[:140], urgency="important" if vip else "info"))
    return out


def disk_space(ctx: CheckContext) -> list[Notice]:
    """Any drive with less free space than min_free_gb (once a day)."""
    floor = float(ctx.settings.get("min_free_gb", 20))
    info = ctx.registry.read("computer_system_info")
    today = date.today().isoformat()
    return [Notice(key=f"disk:{d['drive']}:{today}", check="disk",
                   title=f"Drive {d['drive'][0]} is low on space",
                   detail=f"{d['free_gb']} GB free of {d['total_gb']} GB",
                   urgency="important" if d["free_gb"] < floor / 2 else "info")
            for d in info.get("disks", []) if d.get("free_gb", floor) < floor]


def youtube_comments(ctx: CheckContext) -> list[Notice]:
    """New comments on the owner's latest videos."""
    if not ctx.google_ready or "youtube_my_videos" not in ctx.registry.names():
        return []
    videos = ctx.registry.read("youtube_my_videos", max_results=int(ctx.settings.get("videos", 3))).get("videos", [])
    out = []
    for v in videos:
        for c in ctx.registry.read("youtube_comments", video_id=v["id"], max_results=20).get("comments", []):
            out.append(Notice(key=f"yt:{c['thread_id']}", check="youtube",
                              title=f"New comment from {c.get('author')} on “{v['title']}”",
                              detail=(c.get("text") or "")[:140], urgency="info"))
    return out


def default_checks(settings: dict) -> list[Check]:
    def every(name: str, default: float) -> float:
        return float(settings.get(name, {}).get("every_minutes", default))
    return [
        Check("calendar", every("calendar", 5), calendar_soon),
        Check("tasks", every("tasks", 30), tasks_due),
        Check("email", every("email", 10), email_new, seed_first_run=True),
        Check("disk", every("disk", 360), disk_space),
        Check("youtube", every("youtube", 60), youtube_comments, seed_first_run=True),
    ]
