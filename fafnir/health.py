"""Self-diagnostics: is everything Fafnir depends on working, and fix what is safe to fix.

Every `interval` seconds each check reports ok / warn / fail with a plain sentence and a number.
State changes are written to logs/health.log (one JSON object per line), the owner gets a notice
when something breaks and when it recovers, and `health_status` / `python main.py --health` /
Settings > Health show the current picture.

Recovery is limited to this allowlist, each logged when used:
  - start Ollama again if the local model server has gone,
  - reopen the microphone and speaker streams if the wake-word listener has stopped hearing,
  - step the performance profile down if the local model has become slow (out of graphics memory).
Nothing here can change a security control: not the kill switch, the confirmation gate, the
firewall, or encryption. If the kill switch is on, no recovery is attempted at all.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import ROOT

LOG = ROOT / "logs" / "health.log"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
OK, WARN, FAIL = "ok", "warn", "fail"
SLOW_TPS = 15.0            # tokens/s below this = the model has spilled out of graphics memory
WAKE_STALL_SECONDS = 20    # no audio frames for this long = the listener is stuck


def _row(name: str, status: str, detail: str, value=None) -> dict:
    return {"check": name, "status": status, "detail": detail, "value": value}


# ---- individual checks (each returns one row; none of them changes anything) ----------------------
def check_system() -> list[dict]:
    rows = []
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.3)
        rows.append(_row("Processor", WARN if cpu > 95 else OK, f"{cpu:.0f}% busy", cpu))
        m = psutil.virtual_memory()
        rows.append(_row("Memory", FAIL if m.percent > 97 else WARN if m.percent > 90 else OK,
                         f"{m.percent:.0f}% used ({m.available / 2**30:.1f} GB free)", m.percent))
        for drive in sorted({ROOT.anchor, "D:\\"}):
            if os.path.exists(drive):
                d = psutil.disk_usage(drive)
                free = d.free / 2**30
                rows.append(_row(f"Disk {drive[:2]}", FAIL if free < 5 else WARN if free < 20 else OK,
                                 f"{free:.0f} GB free", round(free)))
    except Exception as e:
        rows.append(_row("System", WARN, f"couldn't read ({type(e).__name__})"))
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout.strip().splitlines()[0]
        used, total, util, temp = (int(x) for x in out.split(","))
        rows.append(_row("Graphics card", WARN if temp >= 85 or used / total > 0.97 else OK,
                         f"{used / 1024:.1f} of {total / 1024:.1f} GB used, {util}% busy, {temp} C", used))
    except Exception:
        rows.append(_row("Graphics card", WARN, "couldn't read"))
    return rows


def check_network() -> dict:
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        return _row("Internet", OK, "connected")
    except OSError:
        return _row("Internet", WARN, "offline: local features still work; Google, Discord and cloud hand-offs won't")


def check_ollama() -> dict:
    try:
        import httpx
        v = httpx.get("http://127.0.0.1:11434/api/version", timeout=3).json().get("version", "?")
        return _row("Local model server (Ollama)", OK, f"running (v{v})")
    except Exception:
        return _row("Local model server (Ollama)", FAIL, "not responding")


def check_backup() -> dict:
    try:
        from .heartbeat.runner import load_settings
        folder = Path(load_settings().get("backup", {}).get("folder", "D:/FafnirBackups"))
        files = sorted(folder.glob("fafnir-*.zip.enc"))
        if not files:
            return _row("Backups", WARN, f"no encrypted backup in {folder}")
        age = (time.time() - files[-1].stat().st_mtime) / 3600
        return _row("Backups", WARN if age > 36 else OK, f"newest is {age:.0f} hours old", round(age))
    except Exception as e:
        return _row("Backups", WARN, f"couldn't check ({type(e).__name__})")


def check_security(hours: float = 1) -> list[dict]:
    """Signals from the action log: tampering, refusals, blocked loops, planted instructions."""
    from . import audit, killswitch
    rows = [_row("Kill switch", WARN if killswitch.engaged() else OK, "ENGAGED: Fafnir is stopped" if killswitch.engaged() else "ready")]
    v = audit.verify(audit.ACTIONS)
    rows.append(_row("Action log", OK if v["ok"] else FAIL,
                     f"intact ({v['entries']} entries)" if v["ok"] else f"has been altered near line {v['first_problem_line']}"))
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    counts = {"rejected": 0, "rate": 0, "inject": 0, "errors": 0, "calls": 0}
    try:
        with open(audit.ACTIONS, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            for line in f.read().decode("utf-8", errors="replace").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("time", "") < cutoff:
                    continue
                tool, res = str(e.get("tool", "")), str(e.get("result", ""))
                counts["calls"] += 1
                counts["rejected"] += tool == "AUTH_REJECTED"
                counts["inject"] += tool == "INJECTION_FLAG"
                counts["rate"] += res.startswith("Rate limit")
                counts["errors"] += bool(e.get("error")) and not res.startswith(("Rate limit", "declined", "Blocked"))
    except OSError:
        pass
    rows.append(_row("Messages from strangers", WARN if counts["rejected"] >= 5 else OK,
                     f"{counts['rejected']} rejected in the last hour", counts["rejected"]))
    rows.append(_row("Planted instructions", WARN if counts["inject"] else OK,
                     f"{counts['inject']} flagged in the last hour", counts["inject"]))
    rows.append(_row("Runaway loops", WARN if counts["rate"] else OK,
                     f"{counts['rate']} calls stopped by the rate limit in the last hour", counts["rate"]))
    rate = counts["errors"] / counts["calls"] if counts["calls"] >= 6 else 0
    rows.append(_row("Failed actions", WARN if rate > 0.4 else OK,
                     f"{counts['errors']} of {counts['calls']} tool calls failed in the last hour", counts["errors"]))
    return rows


def static_report() -> list[dict]:
    """Everything that can be checked without the running app (for `python main.py --health`)."""
    return check_system() + [check_network(), check_ollama(), check_backup()] + check_security()


# ---- the monitor inside the running app -----------------------------------------------------------
class HealthMonitor:
    def __init__(self, engine, interval: float = 30.0, notify=None):
        self.engine, self.interval, self.notify = engine, interval, notify
        self.extras: dict = {}            # {"discord": link, "whatsapp": link, "heartbeat": hb}, set by the app
        self.rows: list[dict] = []
        self.recoveries: list[dict] = []
        self._state: dict[str, str] = {}
        self._last_recovery: dict[str, float] = {}
        self._stop = threading.Event()
        self._ticks = 0
        self._slow_cache: list[dict] = []

    # ---- checks that need the live engine ----
    def _live_checks(self) -> list[dict]:
        e, rows = self.engine, []
        agent = e.agent
        if hasattr(agent, "use_model"):
            rows.append(check_ollama())
            tps = getattr(agent, "last_tps", 0.0)
            model = agent.active_model
            if not model:
                rows.append(_row("Local brain", OK, "unloaded (Gaming profile)"))
            elif tps and tps < SLOW_TPS:
                rows.append(_row("Local brain", WARN, f"{model} is slow: {tps:.0f} tokens/s (out of graphics memory?)", round(tps)))
            else:
                rows.append(_row("Local brain", OK, f"{model}" + (f", {tps:.0f} tokens/s" if tps else ", not used yet"), round(tps) or None))
        else:
            rows.append(_row("Brain", OK, f"cloud ({e.cfg.model.provider})"))
        if e.voice:
            w = e.wake
            if w is None:
                rows.append(_row("Wake word", WARN if e.cfg.voice.wake_enabled else OK,
                                 e.wake_problem or "off"))
            else:
                age = time.monotonic() - getattr(w, "_last_frame", time.monotonic())
                stuck = w._stream is None or age > WAKE_STALL_SECONDS
                rows.append(_row("Wake word listener", FAIL if stuck else OK,
                                 "not receiving sound from the microphone" if stuck else f"listening on {e.mic_name or 'the default mic'}"))
            sp = e.speaker
            rows.append(_row("Voice output", OK if sp and sp._out is not None else FAIL,
                             "ready" if sp and sp._out is not None else "the speaker stream is closed"))
            stt = e.stt
            rows.append(_row("Speech recognition", OK, "local (Whisper)" if hasattr(stt, "transcribe_pcm") else "cloud (Deepgram)"))
        res = getattr(e, "resources", None)
        if res is not None:
            rows.append(_row("Performance profile", OK, f"{res.profile.replace('_', ' ') or 'starting'} ({res.why})"))
        d = self.extras.get("discord")
        if d is not None:
            try:
                up = d.client.is_ready() and not d.client.is_closed()
            except Exception:
                up = False
            rows.append(_row("Discord link", OK if up else WARN, "connected" if up else "not connected (it reconnects by itself)"))
        hb = self.extras.get("heartbeat")
        if hb is not None:
            rows.append(_row("Heartbeat", OK, "paused" if hb.paused else "running"))
        pv = [n for n in e.agent.registry.names() if n == "private_files_status"]
        if pv:
            try:
                st = e.agent.registry.read("private_files_status")
                rows.append(_row("Private documents vault", OK if st.get("state") != "unlocked" else WARN,
                                 st.get("state", "?") + (f", locks in {st.get('locks_in_seconds')} s" if st.get("state") == "unlocked" else "")))
            except Exception:
                pass
        return rows

    def check(self) -> list[dict]:
        self._ticks += 1
        if self._ticks % 10 == 1 or not self._slow_cache:     # the heavier checks, every ~5 minutes
            self._slow_cache = [check_network(), check_backup()] + check_security()
        self.rows = check_system() + self._live_checks() + self._slow_cache
        return self.rows

    # ---- recovery: only these three things, only when the kill switch is off ----
    def _recover(self, row: dict) -> str | None:
        from . import killswitch
        if killswitch.engaged():
            return None
        name, now = row["check"], time.monotonic()
        if now - self._last_recovery.get(name, -1e9) < 120:     # don't hammer: one try every two minutes
            return None
        e = self.engine
        done = None
        if name == "Local model server (Ollama)" and row["status"] == FAIL:
            app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
            try:
                if app.exists():
                    subprocess.Popen([str(app)], creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0))
                else:
                    subprocess.Popen(["ollama", "serve"], creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                done = "started Ollama again"
            except Exception as ex:
                done = f"tried to start Ollama, but couldn't ({type(ex).__name__})"
        elif name in ("Wake word listener", "Voice output") and row["status"] == FAIL:
            try:
                e._switch_mic(e._mic_want)
                done = "reopened the microphone and speaker"
            except Exception as ex:
                done = f"tried to reopen the microphone, but couldn't ({type(ex).__name__})"
        elif name == "Local brain" and row["status"] == WARN and "slow" in row["detail"]:
            res = getattr(e, "resources", None)
            if res is not None and res.profile == "ai_performance" and res.mode == "auto":
                res.apply("balanced", "the 8B model had become slow")
                e.agent.last_tps = 0.0
                done = "switched to the Balanced profile (smaller model)"
        if done:
            self._last_recovery[name] = now
            entry = {"time": datetime.now().isoformat(timespec="seconds"), "event": "recovery", "check": name, "action": done}
            self.recoveries = (self.recoveries + [entry])[-20:]
            _write(entry)
            print(f"[health] {name}: {done}", flush=True)
        return done

    def tick(self) -> None:
        rows = self.check()
        for r in rows:
            prev = self._state.get(r["check"])
            if prev != r["status"]:
                self._state[r["check"]] = r["status"]
                if prev is not None or r["status"] != OK:        # log changes (and anything wrong at start-up)
                    _write({"time": datetime.now().isoformat(timespec="seconds"), "event": "change", **r, "was": prev})
                if self.notify and prev is not None:
                    if r["status"] == FAIL:
                        self.notify(f"Problem: {r['check']}: {r['detail']}.")
                    elif prev == FAIL and r["status"] == OK:
                        self.notify(f"Recovered: {r['check']} is working again.")
            if r["status"] != OK:
                self._recover(r)

    def start(self) -> None:
        def loop() -> None:
            self._stop.wait(20)             # let everything finish starting first
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:
                    print(f"[health] check failed: {type(e).__name__}: {e}", flush=True)
                self._stop.wait(self.interval)
        threading.Thread(target=loop, daemon=True, name="health").start()

    def stop(self) -> None:
        self._stop.set()

    def summary(self) -> dict:
        rows = self.rows or self.check()
        bad = [r for r in rows if r["status"] != OK]
        return {"overall": "all good" if not bad else f"{len(bad)} thing(s) need attention",
                "problems": [{"what": r["check"], "status": r["status"], "detail": r["detail"]} for r in bad],
                "everything": [{"what": r["check"], "status": r["status"], "detail": r["detail"]} for r in rows],
                "recent_recoveries": self.recoveries[-5:]}


def _write(entry: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def main() -> int:
    rows = static_report()
    print("\nFafnir health (what can be checked without the app running)\n")
    for r in rows:
        print(f"  [{r['status'].upper():4}] {r['check']}: {r['detail']}")
    bad = [r for r in rows if r["status"] != OK]
    print(f"\n{len(rows) - len(bad)} fine, {len(bad)} need attention. Live details: Settings > Health, or ask Fafnir \"how are you doing?\"\n")
    return 0
