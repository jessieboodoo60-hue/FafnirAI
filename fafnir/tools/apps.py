"""Launch installed apps by name (anything in the Windows Start menu).

Only apps Windows lists as installed can be started, with no arguments. Arbitrary
scripts, commands, and uninstall/repair entries are not allowed.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time

from .registry import Registry, Tool, ToolError

BLOCKED = re.compile(r"uninstall|remove|repair|reset|installer|setup|update|recovery|regedit", re.I)
CACHE_SECONDS = 600


class AppIndex:
    def __init__(self):
        self._apps: list[dict] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def apps(self) -> list[dict]:
        with self._lock:
            if not self._apps or time.monotonic() - self._loaded_at > CACHE_SECONDS:
                self._apps = self._load()
                self._loaded_at = time.monotonic()
            return self._apps

    @staticmethod
    def _load() -> list[dict]:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            data = json.loads(out.stdout or "[]")
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
            raise ToolError(f"Couldn't get the list of installed apps: {e}")
        if isinstance(data, dict):
            data = [data]
        return [a for a in data if a.get("Name") and a.get("AppID") and not BLOCKED.search(a["Name"])]

    def find(self, name: str) -> list[dict]:
        q = name.strip().lower()
        apps = self.apps()
        exact = [a for a in apps if a["Name"].lower() == q]
        if exact:
            return exact
        starts = [a for a in apps if a["Name"].lower().startswith(q)]
        if starts:
            return starts
        words = q.split()
        return [a for a in apps if all(w in a["Name"].lower() for w in words)]


def register(reg: Registry) -> None:
    index = AppIndex()

    def launch_app(name: str) -> dict:
        matches = index.find(name)
        if not matches:
            raise ToolError(f"No installed app matches '{name}'. Try computer_list_apps to see names.")
        if len(matches) > 1:
            names = [m["Name"] for m in matches[:10]]
            exact_short = min(matches, key=lambda m: len(m["Name"]))
            if len(matches) > 3:
                return {"launched": None, "ambiguous": names,
                        "note": "Several apps match; ask which one or use the exact name."}
            matches = [exact_short]
        app = matches[0]
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['AppID']}"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"launched": app["Name"]}

    def list_apps(filter: str = "") -> dict:
        names = sorted({a["Name"] for a in index.apps()}, key=str.lower)
        if filter:
            names = [n for n in names if filter.lower() in n.lower()]
        return {"count": len(names), "apps": names[:150]}

    reg.register(Tool(
        name="computer_launch_app",
        description=(
            "Start an installed program on the owner's computer by name, e.g. 'Google Chrome', "
            "'Task Manager', 'Discord', 'OBS Studio', 'Calculator', 'Settings'. Use whenever they ask "
            "to open or start an app. Partial names work. Only installed apps can be started; to open a "
            "website use computer_open with the URL instead."
        ),
        properties={"name": {"type": "string", "description": "App name as shown in the Start menu."}},
        required=["name"],
        handler=launch_app,
    ))
    reg.register(Tool(
        name="computer_list_apps",
        description="List installed apps that can be launched, optionally filtered by a name fragment.",
        properties={"filter": {"type": "string", "description": "Optional name fragment."}},
        handler=list_apps,
    ))
