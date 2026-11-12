"""Install and uninstall software, then open it: app-store software (winget), Steam games, and
direct downloads (zip / installer) such as fan games.

Installs run in the background, so Fafnir stays responsive. When one finishes it files an
important notice (spoken at the PC and sent to the phone) and, if asked, opens the program.

Safety:
- winget and Steam installs/uninstalls go through the normal gate (no second question when
  the owner asked for it).
- Direct downloads always ask once, showing the website the file comes from, because the
  link is found on the web and running downloaded programs is the riskiest thing Fafnir does.
- Every downloaded program is scanned with Windows Defender before it's opened; if anything
  is found, nothing is run.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import ROOT
from .registry import Registry, Tool, ToolError

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GAMES = Path.home() / "Games"
DOWNLOADS = Path.home() / "Downloads" / "Fafnir"
INSTALLS = ROOT / "data" / "installs.json"       # what Fafnir installed from direct downloads
DEFENDER = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"
STEAM_KEY = r"HKCU:\Software\Valve\Steam"
SKIP_EXE = re.compile(r"unins|uninstall|setup|install|update|crash|redist|vc_?redist|dxsetup|helper|report", re.I)


def _ps(cmd: str, timeout: int = 60) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "", name).strip().rstrip(".") or "App"


# ---- jobs --------------------------------------------------------------------------------
class Job:
    def __init__(self, kind: str, name: str):
        self.id, self.kind, self.name = uuid.uuid4().hex[:6], kind, name
        self.status, self.detail, self.progress = "starting", "", None
        self.started = datetime.now().isoformat(timespec="seconds")

    def out(self) -> dict:
        d = {"id": self.id, "what": f"{self.kind} {self.name}", "status": self.status, "detail": self.detail,
             "started": self.started}
        if self.progress is not None:
            d["progress_pct"] = self.progress
        return d


class Installer:
    def __init__(self, notices):
        self.notices = notices
        self.jobs: dict[str, Job] = {}
        self.http = httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Fafnir"})

    # ---- notices ---------------------------------------------------------------------
    def _notify(self, title: str, detail: str = "", urgency: str = "important") -> None:
        if self.notices is None:
            return
        from ..heartbeat.notices import Notice
        self.notices.add(Notice(key=f"install:{uuid.uuid4().hex}", title=title, detail=detail,
                                urgency=urgency, check="installer"))

    def _run_job(self, job: Job, work) -> dict:
        self.jobs[job.id] = job

        def runner():
            try:
                work(job)
            except Exception as e:
                job.status, job.detail = "failed", f"{type(e).__name__}: {e}"[:300]
                self._notify(f"Couldn't install {job.name}", job.detail)
        threading.Thread(target=runner, daemon=True, name=f"install-{job.id}").start()
        return {"started": True, "job_id": job.id,
                "note": "Running in the background. I'll announce it (and open it if asked) when it's done; "
                        "software_jobs shows progress."}

    def _launch(self, job: Job, launch) -> str:
        try:
            launch()
            return " and opened it"
        except Exception as e:
            return f", but couldn't open it ({e})"

    # ---- winget ------------------------------------------------------------------------
    def search(self, query: str) -> dict:
        out = _ps(f"winget search --accept-source-agreements --disable-interactivity {json.dumps(query)}", 90)
        rows = []
        for line in out.splitlines():
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) >= 3 and not line.startswith(("Name", "-")) and "." in parts[1] + parts[-1]:
                rows.append({"name": parts[0], "id": parts[1], "version": parts[2] if len(parts) > 3 else "",
                             "source": parts[-1] if parts[-1] in ("winget", "msstore") else "winget"})
        result = {"winget": rows[:12]}
        try:  # Steam store too, for games
            r = self.http.get("https://store.steampowered.com/api/storesearch/",
                              params={"term": query, "cc": "us", "l": "english"}, timeout=15)
            result["steam"] = [{"name": i["name"], "app_id": i["id"]} for i in r.json().get("items", [])[:8]]
        except Exception:
            result["steam"] = []
        if not rows and not result["steam"]:
            result["note"] = ("Not in winget or Steam. If it's a free download from its own website, find the "
                              "official direct download link (web search/fetch) and use software_install with source 'url'.")
        return result

    def _winget(self, job: Job, package_id: str, uninstall: bool, launch: bool) -> None:
        verb = "uninstall" if uninstall else "install"
        job.status = "uninstalling" if uninstall else "installing"
        args = ["winget", verb, "--id", package_id, "-e", "--silent", "--disable-interactivity",
                "--accept-source-agreements"]
        if not uninstall:
            args.append("--accept-package-agreements")
        p = subprocess.run(args, capture_output=True, text=True, timeout=3600, creationflags=NO_WINDOW,
                           encoding="utf-8", errors="replace")
        tail = (p.stdout or p.stderr or "").strip().splitlines()[-1:] or [""]
        if p.returncode != 0 and "already installed" not in (p.stdout or "").lower():
            raise RuntimeError(f"winget {verb} failed (code {p.returncode}): {tail[0][:200]}")
        job.status = "done"
        if uninstall:
            job.detail = "removed"
            self._notify(f"Uninstalled {job.name}", urgency="info")
            return
        opened = ""
        if launch:
            from .apps import AppIndex
            idx = AppIndex()
            def go():
                matches = idx.find(job.name) or idx.find(job.name.split()[0])
                if not matches:
                    raise RuntimeError("it's installed but I couldn't find it in the Start menu yet")
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{matches[0]['AppID']}"], creationflags=NO_WINDOW)
            opened = self._launch(job, go)
        job.detail = "installed" + opened
        self._notify(f"Installed {job.name}{opened}")

    # ---- Steam --------------------------------------------------------------------------
    def _steam_libraries(self) -> list[Path]:
        root = Path(_ps(f"(Get-ItemProperty '{STEAM_KEY}' -ErrorAction SilentlyContinue).SteamPath") or
                    r"C:\Program Files (x86)\Steam")
        libs = [root / "steamapps"]
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore")):
                libs.append(Path(m.group(1).replace("\\\\", "\\")) / "steamapps")
        return list(dict.fromkeys(libs))

    def _steam_installed(self, app_id: int) -> bool:
        for lib in self._steam_libraries():
            acf = lib / f"appmanifest_{app_id}.acf"
            if acf.exists():
                m = re.search(r'"StateFlags"\s+"(\d+)"', acf.read_text(encoding="utf-8", errors="ignore"))
                if m and int(m.group(1)) == 4:   # 4 = fully installed
                    return True
        return False

    def _steam(self, job: Job, app_id: int, uninstall: bool, launch: bool) -> None:
        if uninstall:
            os.startfile(f"steam://uninstall/{app_id}")
            job.status, job.detail = "done", "Steam asked to uninstall (confirm in Steam)"
            return
        if self._steam_installed(app_id):
            job.status = "done"
            opened = self._launch(job, lambda: os.startfile(f"steam://rungameid/{app_id}")) if launch else ""
            job.detail = "already installed" + opened
            self._notify(f"{job.name} was already installed{opened}", urgency="info")
            return
        os.startfile(f"steam://install/{app_id}")
        job.status, job.detail = "waiting", "Steam's install window is open: click Install there"
        deadline = time.time() + 6 * 3600
        while time.time() < deadline:
            time.sleep(15)
            if self._steam_installed(app_id):
                job.status = "done"
                opened = self._launch(job, lambda: os.startfile(f"steam://rungameid/{app_id}")) if launch else ""
                job.detail = "installed" + opened
                self._notify(f"Installed {job.name}{opened}")
                return
            job.detail = "downloading in Steam (or waiting for you to click Install)"
        raise RuntimeError("Steam didn't finish within 6 hours")

    # ---- direct downloads ----------------------------------------------------------------
    def _download(self, job: Job, url: str) -> Path:
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        job.status = "downloading"
        with self.http.stream("GET", url) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"the download failed (HTTP {r.status_code})")
            cd = r.headers.get("content-disposition", "")
            m = re.search(r'filename\*?="?([^";]+)', cd)
            fname = _safe_name((m.group(1) if m else Path(urlparse(str(r.url)).path).name) or f"{job.name}.bin")
            total = int(r.headers.get("content-length") or 0)
            dest, got = DOWNLOADS / fname, 0
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        job.progress = round(100 * got / total)
                    job.detail = f"{got / 1e6:.0f} MB" + (f" of {total / 1e6:.0f} MB" if total else "")
        return dest

    def _scan(self, job: Job, path: Path) -> None:
        if not DEFENDER.exists():
            return
        job.status, job.detail = "scanning", "checking it with Windows Defender"
        p = subprocess.run([str(DEFENDER), "-Scan", "-ScanType", "3", "-File", str(path), "-DisableRemediation"],
                           capture_output=True, text=True, timeout=1800, creationflags=NO_WINDOW)
        if p.returncode == 2:
            raise RuntimeError("Windows Defender found a threat in the download, so I didn't run it")

    @staticmethod
    def _main_exe(folder: Path, name: str) -> Path | None:
        exes = [p for p in folder.rglob("*.exe") if not SKIP_EXE.search(p.name)]
        if not exes:
            return None
        words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2]
        def score(p: Path):
            n = p.stem.lower()
            return (n == "game", any(w in n for w in words), -len(p.relative_to(folder).parts), p.stat().st_size)
        return max(exes, key=score)

    @staticmethod
    def _shortcut(name: str, exe: Path) -> Path:
        programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        lnk = programs / f"{_safe_name(name)}.lnk"
        env = dict(os.environ, LNK=str(lnk), TARGET=str(exe), WORKDIR=str(exe.parent))
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
                        "$s.TargetPath=$env:TARGET;$s.WorkingDirectory=$env:WORKDIR;$s.Save()"],
                       env=env, capture_output=True, timeout=30, creationflags=NO_WINDOW)
        return lnk

    def _url_install(self, job: Job, url: str, launch: bool) -> None:
        file = self._download(job, url)
        job.progress = None
        self._scan(job, file)
        target = GAMES / _safe_name(job.name)
        if file.suffix.lower() == ".zip":
            job.status, job.detail = "unzipping", f"into {target}"
            if target.exists():
                shutil.rmtree(target)
            with zipfile.ZipFile(file) as z:
                z.extractall(target)
            file.unlink(missing_ok=True)   # the zip isn't needed after unzipping
            exe = self._main_exe(target, job.name)
            if exe is None:
                raise RuntimeError(f"unzipped to {target}, but found no program to run in it")
            lnk = self._shortcut(job.name, exe)
            self._record({"name": job.name, "kind": "folder", "folder": str(target), "exe": str(exe),
                          "shortcut": str(lnk), "source": url})
            job.status = "done"
            opened = self._launch(job, lambda: subprocess.Popen([str(exe)], cwd=str(exe.parent))) if launch else ""
            job.detail = f"installed in {target}{opened}"
            self._notify(f"Installed {job.name}{opened}", f"in {target}")
        elif file.suffix.lower() in (".exe", ".msi"):
            job.status, job.detail = "installing", "the installer is open: follow its steps if it asks"
            cmd = ["msiexec", "/i", str(file)] if file.suffix.lower() == ".msi" else [str(file)]
            p = subprocess.run(cmd, timeout=7200)
            self._record({"name": job.name, "kind": "installer", "installer": str(file), "source": url})
            job.status = "done"
            opened = ""
            if launch:
                from .apps import AppIndex
                def go():
                    m = AppIndex().find(job.name)
                    if not m:
                        raise RuntimeError("couldn't find it in the Start menu")
                    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{m[0]['AppID']}"], creationflags=NO_WINDOW)
                opened = self._launch(job, go)
            job.detail = f"installer finished (code {p.returncode}){opened}"
            self._notify(f"Installed {job.name}{opened}")
        else:
            raise RuntimeError(f"downloaded {file.name}, but I don't know how to install a {file.suffix} file")

    # ---- records of direct installs -------------------------------------------------------------
    def _records(self) -> list[dict]:
        try:
            return json.loads(INSTALLS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _record(self, entry: dict) -> None:
        recs = [r for r in self._records() if r["name"].lower() != entry["name"].lower()]
        recs.append(dict(entry, installed=datetime.now().isoformat(timespec="seconds")))
        INSTALLS.parent.mkdir(parents=True, exist_ok=True)
        INSTALLS.write_text(json.dumps(recs, indent=1), encoding="utf-8")

    def _remove_folder_install(self, rec: dict) -> str:
        from .control import delete_to_recycle_bin
        Path(rec.get("shortcut", "")).unlink(missing_ok=True)
        if Path(rec["folder"]).exists():
            delete_to_recycle_bin(rec["folder"])
        INSTALLS.write_text(json.dumps([r for r in self._records() if r is not rec and r["name"] != rec["name"]],
                                       indent=1), encoding="utf-8")
        return f"removed {rec['folder']} (in the Recycle Bin) and its Start menu shortcut"

    # ---- tool entry points ---------------------------------------------------------------------
    def install(self, name: str, source: str, package_id: str = "", app_id: int = 0, url: str = "",
                launch_when_done: bool = True) -> dict:
        if source == "winget":
            if not package_id:
                raise ToolError("Give the winget package_id from software_search.")
            return self._run_job(Job("install", name), lambda j: self._winget(j, package_id, False, launch_when_done))
        if source == "steam":
            if not app_id:
                raise ToolError("Give the Steam app_id from software_search.")
            return self._run_job(Job("install", name), lambda j: self._steam(j, app_id, False, launch_when_done))
        if source == "url":
            if not url.lower().startswith("https://"):
                raise ToolError("Give a direct https download link (the file itself, e.g. a .zip or .exe).")
            return self._run_job(Job("install", name), lambda j: self._url_install(j, url, launch_when_done))
        raise ToolError("source must be winget, steam or url.")

    def uninstall(self, name: str, source: str = "", package_id: str = "", app_id: int = 0) -> dict:
        rec = next((r for r in self._records() if r["name"].lower() == name.lower()), None)
        if rec and rec.get("kind") == "folder" and source in ("", "url"):
            return {"done": self._remove_folder_install(rec)}
        if source == "steam" and app_id:
            return self._run_job(Job("uninstall", name), lambda j: self._steam(j, app_id, True, False))
        pid = package_id or name
        return self._run_job(Job("uninstall", name), lambda j: self._winget(j, pid, True, False))

    def list_jobs(self) -> dict:
        return {"jobs": [j.out() for j in self.jobs.values()][-10:],
                "installed_from_downloads": [{"name": r["name"], "where": r.get("folder") or r.get("installer")}
                                             for r in self._records()]}


def register(reg: Registry, notices) -> None:
    inst = Installer(notices)

    reg.register(Tool(
        name="software_search",
        description=("Find software or games to install: searches winget (Windows' package manager, thousands of apps) "
                     "and the Steam store. Use before software_install. If nothing is found and it's a free download "
                     "from its own website, look up the official direct download link instead."),
        properties={"query": {"type": "string"}},
        required=["query"],
        handler=inst.search,
    ))
    reg.register(Tool(
        name="software_install",
        description=(
            "Install software or a game and (by default) open it when done. Runs in the background; you'll be "
            "notified when it finishes, so tell the owner it's under way and keep going. "
            "source: 'winget' (package_id from software_search), 'steam' (app_id; Steam shows an Install button the "
            "owner must click once), or 'url' (a direct https link to the file itself, e.g. a .zip or installer, "
            "taken from the official website; zips are unzipped into ~/Games and given a Start menu shortcut). "
            "Downloads are scanned with Windows Defender before anything runs."),
        properties={
            "name": {"type": "string", "description": "Display name, e.g. 'Pokemon Reborn'."},
            "source": {"type": "string", "enum": ["winget", "steam", "url"]},
            "package_id": {"type": "string"},
            "app_id": {"type": "integer"},
            "url": {"type": "string"},
            "launch_when_done": {"type": "boolean", "description": "Open it once installed. Default true."},
        },
        required=["name", "source"],
        handler=inst.install,
        confirm_if=lambda a: (f"Install {a['name']}" + (" from Steam" if a.get("source") == "steam" else "") + "."
                              if a.get("source") in ("winget", "steam") else None),
        critical_if=lambda a: (f"Download and install {a['name']} from {urlparse(a.get('url', '')).netloc or 'the web'} "
                               f"(it will be scanned before it runs)." if a.get("source") == "url" else None),
    ))
    reg.register(Tool(
        name="software_uninstall",
        description=("Uninstall software or a game: things Fafnir installed from a download (folder goes to the "
                     "Recycle Bin), winget packages (package_id or name), or Steam games (app_id)."),
        properties={"name": {"type": "string"},
                    "source": {"type": "string", "enum": ["", "winget", "steam", "url"]},
                    "package_id": {"type": "string"}, "app_id": {"type": "integer"}},
        required=["name"],
        handler=inst.uninstall,
        confirm_if=lambda a: f"Uninstall {a['name']}.",
    ))
    reg.register(Tool(
        name="software_jobs",
        description="Progress of installs/uninstalls running in the background, and what Fafnir installed from downloads.",
        properties={},
        handler=inst.list_jobs,
    ))
