"""Fafnir's desktop: a liquid-glass desktop environment (menu bar, dock, windows, search,
control centre, notifications, files) running in a fullscreen web view, wired to Fafnir.

The interface itself is HTML/CSS/JS in fafnir/ui/web/ (CSS backdrop-filter gives real
blur; animations run at display rate). This module hosts it and is the only bridge
between the page and Python: the page calls slots on `DesktopBridge` through QWebChannel,
and everything Python wants to tell the page goes out as JSON through `DesktopBridge.event`.

All actions go through Fafnir's normal tools and confirmation gate: opening programs,
deleting files, reading secret files and changing system settings still ask first.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView

from ..config import ROOT
from ..engine import Events
from ..screen import SCREEN
from .priorities import rank

WEB = ROOT / "fafnir" / "ui" / "web"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STATUS = {"idle": "Ready", "listening": "Listening", "thinking": "Thinking", "speaking": "Speaking",
          "confirming": "Needs your OK"}


def _ps(script: str, timeout: int = 15) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return (r.stdout or "").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


class DesktopEvents(QObject, Events):
    """Engine events (background threads) -> UI thread -> the page."""
    post = Signal(str)
    screen = Signal(str, object)   # Fafnir's screen tools, marshalled to the UI thread

    def _send(self, kind: str, **data) -> None:
        self.post.emit(json.dumps({"type": kind, **data}, default=str))

    def heard(self, text): self._send("heard", text=text)
    def reply_start(self): self._send("reply_start")
    def reply_text(self, delta): self._send("reply_text", text=delta)

    def tool(self, name, args, result, is_error):
        first = next((str(v) for v in (args or {}).values() if isinstance(v, (str, list)) and v), "")
        self._send("tool", name=name, detail=first[:60], error=bool(is_error), web=result is None)

    def reply_end(self, interrupted, stats): self._send("reply_end", interrupted=interrupted, cost=stats.cost)
    def notice(self, text): self._send("caption", text=text)
    def error(self, text): self._send("error", text=text)
    def confirm_request(self, action): self._send("confirm", action=action)
    def confirm_done(self, approved, timed_out): self._send("confirm_done", approved=approved, timed_out=timed_out)
    def info(self, text): self._send("info", text=str(text))   # phone-link status lines etc.


class DesktopBridge(QObject):
    """Exposed to the page as `fafnir`. Slots are called from JS; `event` goes to JS."""
    event = Signal(str)

    def __init__(self, window: "DesktopWindow"):
        super().__init__()
        self.w = window

    def emit(self, kind: str, **data) -> None:
        self.event.emit(json.dumps({"type": kind, **data}, default=str))

    def _bg(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    # ---- conversation ----------------------------------------------------------------
    @Slot()
    def ready(self) -> None:
        self.w.page_ready()

    @Slot(str)
    def submitText(self, text: str) -> None:
        text = text.strip()
        if text == "/reset":
            self.w.engine.reset()
            self.emit("caption", text="New conversation. Memory reloaded.")
            return
        if text:
            self.w.engine.submit_text(text)

    @Slot(bool)
    def answerConfirm(self, approved: bool) -> None:
        self.w.engine.answer_confirm(approved)

    @Slot()
    def interrupt(self) -> None:
        self.w.engine.interrupt()

    # ---- notices ------------------------------------------------------------------------
    @Slot(str)
    def dismissNotice(self, notice_id: str) -> None:
        if self.w.notices is not None:
            self.w.notices.dismiss(notice_id)
        self.w.push_notices(force=True)

    @Slot()
    def togglePause(self) -> None:
        hb = self.w.heartbeat
        if hb is None:
            return
        hb.resume() if hb.paused else hb.pause(0)
        self.w.push_notices(force=True)

    @Slot()
    def refresh(self) -> None:
        self.w.refresh_data()

    # ---- settings (saved in config.toml) ------------------------------------------------
    @Slot(str)
    def getSettings(self, req: str) -> None:
        from .. import config_edit
        try:
            when = {f"{s}.{k}": spec[2] for (s, k), spec in config_edit.EDITABLE.items()}
            self.emit("result", id=req, ok=True, data={"values": config_edit.current(), "when": when,
                                                      "restartNeeded": self.w.restart_needed})
        except Exception as e:
            self.emit("result", id=req, ok=False, error=f"Couldn't read config.toml: {e}")

    @Slot(str, str, str)
    def saveSetting(self, name: str, value_json: str, req: str) -> None:
        from .. import config_edit
        try:
            section, key = name.rsplit(".", 1)
            stored = config_edit.set_value(section, key, json.loads(value_json))
            when = config_edit.EDITABLE[(section, key)][2]
            self.w.apply_setting(section, key, stored, when)
            self.emit("result", id=req, ok=True, data={"value": stored, "when": when,
                                                      "restartNeeded": self.w.restart_needed})
        except Exception as e:
            self.emit("result", id=req, ok=False, error=str(e))

    @Slot()
    def restartApp(self) -> None:
        self.w.restart()

    # ---- windows on other screens ------------------------------------------------------
    @Slot(str, str)
    def popOut(self, app: str, opts_json: str) -> None:
        try:
            self.w.pop_out(app, json.loads(opts_json or "{}"))
        except Exception as e:
            self.emit("error", text=f"Couldn't move that window: {e}")

    @Slot(str)
    def soloReady(self, page: str) -> None:
        self.w.send_state(page)

    @Slot(str, str, int)
    def soloAction(self, page: str, action: str, arg: int) -> None:
        pop = self.w.pops.get(page)
        if pop is None:
            return
        if action == "dock":   # back onto Fafnir's own screen
            app, args = pop.app, pop.args
            pop.close()
            self.emit("screen", to="main", action="open", payload={"app": app, "path": args.get("path")}
                      if app != "viewer" else {})
            if app == "viewer" and args.get("viewer"):
                self.emit("screen", to="main", action="show", payload=args["viewer"])
            self.w.activateWindow()
        else:
            pop.action(action, arg)

    @Slot(str)
    def soloFocus(self, app: str) -> None:
        for pop in self.w.pops.values():
            if pop.app == app:
                if pop.isMinimized():
                    pop.showNormal()
                pop.raise_()
                pop.activateWindow()
                return

    # ---- files ---------------------------------------------------------------------------
    @Slot(str, str)
    def listFolder(self, path: str, req: str) -> None:
        def run():
            try:
                res = self.w.registry.read("computer_list_folder", path=path or "~")
                self.emit("result", id=req, ok=True, data=res)
            except Exception as e:
                self.emit("result", id=req, ok=False, error=str(e))
        self._bg(run)

    @Slot(str, str)
    def readFile(self, path: str, req: str) -> None:
        def run():  # through the gate: files that hold secrets ask first
            text, err = self.w.registry.run("computer_read_text_file", {"path": path, "max_chars": 20000},
                                            confirm=self.w.engine.confirm)
            self.emit("result", id=req, ok=not err, data=None if err else json.loads(text), error=text if err else "")
        self._bg(run)

    @Slot(str)
    def openPath(self, path: str) -> None:
        def run():  # programs and scripts ask first
            text, err = self.w.registry.run("computer_open", {"target": path, "owner_asked": True},
                                            confirm=self.w.engine.confirm)
            if err:
                self.emit("error", text=text)
        self._bg(run)

    @Slot(str, str)
    def deletePath(self, path: str, req: str) -> None:
        def run():  # always confirmed: deleting is on the owner's ask-first list
            text, err = self.w.registry.run("computer_delete", {"path": path}, confirm=self.w.engine.confirm)
            self.emit("result", id=req, ok=not err, error=text if err else "")
        self._bg(run)

    # ---- search -----------------------------------------------------------------------------
    @Slot(str, str)
    def search(self, query: str, req: str) -> None:
        q = query.strip()
        if len(q) < 2:
            return
        reg = self.w.registry

        def section(name, tool, convert, **args):
            def run():
                try:
                    if tool not in reg.names():
                        return
                    items = convert(reg.read(tool, **args))
                    self.emit("search", id=req, section=name, items=items[:6])
                except Exception:
                    pass
            self._bg(run)

        section("Files", "computer_find_files",
                lambda r: [{"title": os.path.basename(p), "subtitle": p, "path": p} for p in r.get("matches", [])],
                name_pattern=q, folder="~", max_results=8)
        section("Memory", "memory_search",
                lambda r: [{"title": f["fact"], "subtitle": f["category"]} for f in r.get("results", [])], query=q)
        section("Business", "business_list_items",
                lambda r: [{"title": t["title"], "subtitle": t["kind"] + (f" · due {t['due']}" if t.get("due") else "")}
                           for t in r.get("items", [])], status="all", search=q)
        section("Notes", "notes_search",
                lambda r: [{"title": x["note"], "subtitle": (x.get("snippets") or [""])[0][:90],
                            "path": os.path.join(r.get("notes_folder", ""), x["note"])} for x in r.get("results", [])],
                query=q, max_results=5)

    # ---- control centre ----------------------------------------------------------------------
    @Slot(str)
    def controlStatus(self, req: str) -> None:
        def run():
            wifi = _ps("netsh wlan show interfaces")
            state = re.search(r"^\s*State\s*:\s*(.+)$", wifi, re.M)
            ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", wifi, re.M)
            signal = re.search(r"^\s*Signal\s*:\s*(\d+)%", wifi, re.M)
            bt = _ps("(Get-PnpDevice -Class Bluetooth -PresentOnly -ErrorAction SilentlyContinue | "
                     "Where-Object { $_.FriendlyName -match 'Radio|Adapter|Bluetooth' -and $_.Status -eq 'OK' } | "
                     "Measure-Object).Count")
            bright = _ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction "
                         "SilentlyContinue | Select-Object -First 1).CurrentBrightness")
            self.emit("result", id=req, ok=True, data={
                "wifi": {"available": bool(wifi and "There is no wireless" not in wifi),
                         "on": bool(state and state.group(1).strip().lower() == "connected"),
                         "name": ssid.group(1).strip() if ssid else "", "signal": int(signal.group(1)) if signal else None},
                "bluetooth": {"on": bt.isdigit() and int(bt) > 0},
                "brightness": int(bright) if bright.isdigit() else None,
                "dnd": bool(self.w.heartbeat and self.w.heartbeat.paused),
                "voice": self.w.engine.voice, "wake": bool(self.w.engine.wake),
            })
        self._bg(run)

    @Slot(str, int)
    def control(self, action: str, value: int) -> None:
        reg = self.w.registry

        def media(act, times=1):
            try:
                reg.read("computer_media", action=act, times=times)
            except Exception as e:
                self.emit("error", text=f"Sound control failed: {e}")

        if action in ("volume_up", "volume_down"):
            self._bg(media, action, max(1, min(value or 1, 50)))
        elif action in ("mute", "play_pause", "next", "previous"):
            self._bg(media, action)
        elif action == "brightness":
            v = max(0, min(int(value), 100))
            self._bg(lambda: _ps("$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
                                 f"-ErrorAction SilentlyContinue | Select-Object -First 1; if ($m) {{ "
                                 f"Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
                                 f"-Arguments @{{Timeout=1; Brightness={v}}} | Out-Null }}"))
        elif action == "dnd":
            self.togglePause()
        elif action in ("wifi_on", "wifi_off"):
            on = action == "wifi_on"

            def run():  # a system setting: goes through the gate, so the owner is asked
                text, err = reg.run("computer_run_command", {
                    "command": f'netsh interface set interface name="Wi-Fi" admin={"enabled" if on else "disabled"}',
                    "reason": f"Turn Wi-Fi {'on' if on else 'off'}"}, confirm=self.w.engine.confirm)
                if err:
                    self.emit("error", text=text)
                elif '"exit_code": 0' not in text:
                    self.emit("error", text="Windows refused to change Wi-Fi (it may need admin rights).")
            self._bg(run)
        elif action == "bluetooth_settings":
            os.startfile("ms-settings:bluetooth")
        elif action == "wifi_settings":
            os.startfile("ms-settings:network-wifi")
        elif action == "display_settings":
            os.startfile("ms-settings:display")

    @Slot(str)
    def openUrl(self, url: str) -> None:
        if url.startswith(("http://", "https://")):
            os.startfile(url)

    @Slot(str)
    def log(self, text: str) -> None:
        print(f"[desktop] {text}", flush=True)


class DesktopWindow(QWebEngineView):
    def __init__(self, cfg, app_cfg: dict, engine, events: DesktopEvents, heartbeat=None, notices=None):
        super().__init__()
        self.cfg, self.app_cfg, self.engine, self.events = cfg, app_cfg, engine, events
        self.heartbeat, self.notices = heartbeat, notices
        self.registry = engine.agent.registry
        self.setWindowTitle(cfg.assistant.name)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        # A saved (not private) browser profile, so wallpaper, window and icon positions
        # survive restarts. It belongs to the app so it outlives the page.
        self.profile = QWebEngineProfile("fafnir", QApplication.instance())
        self.profile.setPersistentStoragePath(str(ROOT / "data" / "web"))
        self.profile.setCachePath(str(ROOT / "data" / "web" / "cache"))
        self.setPage(QWebEnginePage(self.profile, self))
        self.restart_needed = False
        self.restart_hook = None       # set by run_app: stops everything and relaunches
        self.pops: dict = {}           # page id -> PopOut (apps moved onto other screens)
        self.bridge = DesktopBridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject("fafnir", self.bridge)
        self.page().setWebChannel(self.channel)
        self.page().setBackgroundColor(Qt.black)
        s = self.settings()
        s.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, True)
        self.setContextMenuPolicy(Qt.NoContextMenu)  # the page draws its own menus
        events.post.connect(self.bridge.event.emit)
        events.screen.connect(self.on_screen)
        self._ready = False
        self._data: dict = {}
        self._briefing_override: tuple[str, float] | None = None
        self._last_notices: str = ""
        self._last_state = None
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)
        self.load(QUrl.fromLocalFile(str(WEB / "index.html")))

    # ---- page lifecycle -------------------------------------------------------------------
    def _init_payload(self) -> dict:
        from ..tools import CONVERSATIONS
        bg = str(self.app_cfg.get("background", "")).strip()
        return dict(name=self.cfg.assistant.name, model=self.cfg.model.name,
                    talkKey=self.app_cfg.get("talk_key_label", "M5"),
                    voice=self.engine.voice, wake=self.engine.wake.name if self.engine.wake else "",
                    wallpaper=QUrl.fromLocalFile(os.path.expanduser(bg)).toString() if bg else "",
                    home=os.path.expanduser("~"), conversation=CONVERSATIONS.today()[-40:],
                    activity=self._recent_activity())

    def page_ready(self) -> None:
        self._ready = True
        self.bridge.emit("init", to="main", **self._init_payload())
        if not self.engine.voice:
            missing = self.cfg.voice.missing_keys()
            self.bridge.emit("caption", text=("Voice is off: add " + ", ".join(missing) + " to .env.") if missing
                             else "Voice is off (microphone, speaker or talk key unavailable). Typing still works.")
        self.push_notices(force=True)
        self.refresh_data()
        if not hasattr(self, "tick"):
            self.tick = QTimer(self)
            self.tick.timeout.connect(self.on_tick)
            self.tick.start(66)  # orb + status at ~15 fps; the page animates smoothly in between
            self.slow = QTimer(self)
            self.slow.timeout.connect(self.on_slow)
            self.slow.start(3000)
            self.data_timer = QTimer(self)
            self.data_timer.timeout.connect(self.refresh_data)
            self.data_timer.start(5 * 60 * 1000)
            self.events.post.connect(self._watch_reply_end)

    def _watch_reply_end(self, msg: str) -> None:
        if '"type": "reply_end"' in msg:
            self.refresh_data()

    def on_tick(self) -> None:
        if not self._ready:
            return
        st = self.engine.state
        self.bridge.emit("tick", state=st, level=round(float(self.engine.level or 0), 3),
                         status=STATUS.get(st, "Ready"))

    def on_slow(self) -> None:
        self.push_notices()
        self._announce()

    # ---- notices ---------------------------------------------------------------------------
    def push_notices(self, force: bool = False) -> None:
        if not self._ready:
            return
        items = self.notices.active() if self.notices is not None else []
        payload = [{"id": n.id, "title": n.title, "detail": n.detail, "urgency": n.urgency,
                    "time": n.created[11:16], "check": n.check} for n in items]
        paused = bool(self.heartbeat and self.heartbeat.paused)
        sig = json.dumps([payload, paused])
        if force or sig != self._last_notices:
            self._last_notices = sig
            self.bridge.emit("notices", items=payload, paused=paused, enabled=self.heartbeat is not None)
            self.push_briefing()

    def _announce(self) -> None:
        """Banner for every new important notice; speak it when Fafnir is free (as before)."""
        if self.notices is None or (self.heartbeat and self.heartbeat.paused):
            return
        from .app import _in_quiet_hours
        for n in self.notices.active():
            if n.announced or n.urgency not in ("important", "urgent"):
                continue
            if self.engine.state != "idle":
                return
            quiet = _in_quiet_hours(self.heartbeat.settings.get("quiet_hours", "") if self.heartbeat else "")
            self.notices.mark_announced(n.id)
            self.bridge.emit("banner", id=n.id, title=n.title, detail=n.detail, urgency=n.urgency)
            if n.urgency == "urgent" or not quiet:
                self.engine.announce(f"Heads up. {n.title}.")
            return  # one at a time

    # ---- data for the apps and the Right-now widget ---------------------------------------
    def refresh_data(self) -> None:
        threading.Thread(target=self._load_data, daemon=True).start()

    def _load_data(self) -> None:
        reg, out = self.registry, {}

        def get(key, tool, **args):
            if tool not in reg.names():
                return None
            try:
                return reg.read(tool, **args)
            except Exception as e:
                out[f"{key}_error"] = str(e)[:140]
                return None

        r = get("tasks", "business_list_items", status="open")
        if r is not None:
            out["tasks"] = r.get("items", [])
        if (ROOT / "data" / "google_token.json").exists():
            today = date.today()
            r = get("events", "calendar_list_events", start=today.isoformat(),
                    end=(today + timedelta(days=6)).isoformat(), max_results=40)
            if r is not None:
                out["events"] = r.get("events", [])
            r = get("gtasks", "google_tasks_list")
            if r is not None:
                out["gtasks"] = r.get("tasks", [])
            r = get("emails", "gmail_search", query="in:inbox newer_than:7d", max_results=25)
            if r is not None:
                out["emails"] = r.get("emails", [])
            r = get("youtube", "youtube_channel")
            if r is not None and r.get("channel"):
                out["youtube"] = r["channel"]
            r = get("videos", "youtube_my_videos", max_results=6)
            if r is not None:
                out["videos"] = r.get("videos", [])
        else:
            out["google_missing"] = True
        try:
            out["system"] = reg.read("computer_system_info")
        except Exception:
            pass
        self._data = out
        self.events.post.emit(json.dumps({"type": "data", **out}, default=str))
        QTimer.singleShot(0, self.push_briefing)

    def push_briefing(self) -> None:
        if not self._ready:
            return
        d = dict(self._data)
        d["emails"] = [m for m in d.get("emails", []) if m.get("unread")][:8]
        d["notices"] = self.notices.active() if self.notices is not None else []
        vips = self.heartbeat.settings.get("email", {}).get("vip_senders", []) if self.heartbeat else []
        briefing, items = rank(d, vips)
        if self._briefing_override and time.monotonic() < self._briefing_override[1]:
            briefing = self._briefing_override[0]
        self.bridge.emit("briefing", text=briefing, items=[{k: v for k, v in i.items() if k != "score"} for i in items])

    def _recent_activity(self) -> list:
        out = []
        try:
            for raw in (ROOT / "logs" / "actions.log").read_text(encoding="utf-8").splitlines()[-10:]:
                e = json.loads(raw)
                out.append({"name": e.get("tool", ""), "time": e.get("time", "")[11:16], "error": bool(e.get("error"))})
        except (OSError, ValueError):
            pass
        return out[::-1]

    # ---- Fafnir's screen tools -----------------------------------------------------------------
    def on_screen(self, action: str, payload: dict) -> None:
        if action == "briefing":
            self._briefing_override = (payload.get("text", ""), time.monotonic() + 30 * 60)
            self.push_briefing()
            return
        self.bridge.emit("screen", action=action, payload=payload)

    # ---- window --------------------------------------------------------------------------------
    # ---- windows on other screens ------------------------------------------------------------
    def pop_out(self, app: str, opts: dict) -> None:
        from .popout import PopOut, place
        pop = PopOut(self, app, opts.get("args") or {}, WEB / "index.html")
        pop.args = opts.get("args") or {}
        self.pops[pop.pid] = pop
        place(self, pop, opts)
        self._send_popped()

    def send_state(self, page: str) -> None:
        """Everything a popped-out window needs to draw its app."""
        self.bridge.emit("init", to=page, **self._init_payload())
        if self._data:
            self.bridge.emit("data", to=page, **self._data)
        items = self.notices.active() if self.notices is not None else []
        self.bridge.emit("notices", to=page, paused=bool(self.heartbeat and self.heartbeat.paused),
                         enabled=self.heartbeat is not None,
                         items=[{"id": n.id, "title": n.title, "detail": n.detail, "urgency": n.urgency,
                                 "time": n.created[11:16], "check": n.check} for n in items])

    def pop_closed(self, pop) -> None:
        self.pops.pop(pop.pid, None)
        self._send_popped()

    def _send_popped(self) -> None:
        self.bridge.emit("popped", to="main", apps=sorted({p.app for p in self.pops.values()}))

    def toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def apply_setting(self, section: str, key: str, value, when: str) -> None:
        """Use a just-saved setting straight away where that's possible."""
        import dataclasses
        if when == "restart":
            self.restart_needed = True
            return
        if section in ("voice", "voice.wake"):
            field = {"cue_sounds": "cue_sounds", "follow_up_seconds": "follow_up_seconds",
                     "while_speaking": "wake_while_speaking"}[key]
            cfg = self.engine.cfg
            self.engine.cfg = self.cfg = dataclasses.replace(cfg, voice=dataclasses.replace(cfg.voice, **{field: value}))
        elif section == "performance" and key == "profile":
            res = getattr(self.engine, "resources", None)
            if res is not None:
                res.set_mode(value)
        elif section == "model":
            agent = self.engine.agent
            if hasattr(agent, "use_model"):       # local brain in charge: these two belong to its cloud helper
                agent = getattr(agent, "_cloud", None)
                if agent is None:
                    self.restart_needed = True
                    return
            if hasattr(agent, key if key == "effort" else "model"):
                setattr(agent, "model" if key == "name" else "effort", value)   # Claude Code brain
            llm = getattr(agent, "llm", None)
            if llm is not None and hasattr(llm, "cfg"):                          # API brain
                llm.cfg = dataclasses.replace(llm.cfg, **{key: value})
        elif section == "app" and key == "background":
            self.app_cfg["background"] = value
            url = QUrl.fromLocalFile(os.path.expanduser(value)).toString() if value else ""
            self.bridge.emit("wallpaper", url=url)

    def restart(self) -> None:
        if self.restart_hook:
            self.restart_hook()

    def closeEvent(self, event) -> None:
        for pop in list(self.pops.values()):
            pop.close()
        SCREEN.attach(None)
        self.engine.stop()
        super().closeEvent(event)
