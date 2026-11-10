"""Full control of the PC: run commands, close apps, manage files, media keys.

Read-only and harmless actions run immediately. Deleting, overwriting, force-closing,
installing, changing settings and any non-read-only command go through the
confirmation gate (see registry.py).
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .computer import resolve
from .registry import Registry, Tool, ToolError

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CONFIRM_MARK = " Command: "  # voice mode speaks only the text before this

# ---- read-only PowerShell classifier -------------------------------------------
SAFE_CMDLET_VERBS = {"get", "test", "measure", "select", "where", "sort", "format", "group",
                     "convertto", "convertfrom", "resolve", "compare", "join", "split", "write"}
SAFE_COMMANDS = {
    "ipconfig", "ping", "whoami", "hostname", "systeminfo", "tasklist", "nslookup", "tracert",
    "netstat", "dir", "ls", "cat", "type", "echo", "where.exe", "driverquery", "ver", "nvidia-smi",
    "gci", "gc", "gps", "gsv", "gi", "gp", "select", "where", "sort", "ft", "fl", "fw", "measure",
    "?", "out-string", "pwd", "date", "uptime", "getmac", "route", "arp", "powercfg",
}
SAFE_SUBCOMMANDS = {"winget": {"list", "search", "show", "--version"}, "powercfg": {"/list", "/query", "/batteryreport"}}
SAFE_CMDLETS = {"foreach-object", "out-string", "out-null", "out-host"}
SAFE_KEYWORDS = {"if", "else", "elseif", "foreach", "return", "%", "?", "-not", "!"}
# redirection, backtick escapes, call operator, encoded commands, string-to-code
DANGER = re.compile(r"(?<![-\w])>|`|&|\b(iex|-encodedcommand|-command|scriptblock)\b", re.I)
# method or static calls that change things: .Delete(), .Kill(), [IO.File]::WriteAllText(...)
DANGER_METHODS = re.compile(
    r"\.(delete|kill|remove|move|copy|write|set\w*|start|stop|close|create|save|clear|exec\w*|"
    r"download\w*|upload\w*|invoke\w*|replace|rename|append\w*|open\w*|shutdown|restart)\s*\(", re.I)
DANGER_STATIC = re.compile(r"\[\s*(system\.)?(io\.|diagnostics|microsoft\.win32|net\.|environment|activator|reflection|runtime)", re.I)
VERB_NOUN = re.compile(r"\b([a-z]+)-([a-z]+)\b", re.I)
PS_VERBS = {"get", "set", "new", "remove", "add", "clear", "copy", "move", "rename", "start", "stop",
            "restart", "suspend", "resume", "invoke", "enable", "disable", "install", "uninstall",
            "update", "register", "unregister", "import", "export", "out", "write", "read", "test",
            "measure", "select", "where", "sort", "format", "group", "convertto", "convertfrom",
            "resolve", "compare", "join", "split", "foreach", "tee", "send", "push", "pop", "mount",
            "dismount", "connect", "disconnect", "save", "edit", "reset", "grant", "revoke", "block",
            "unblock", "protect", "unprotect", "lock", "unlock", "wait", "use", "find", "publish", "approve"}
# aliases/programs that change things, wherever they appear (e.g. "$x = rm file")
DANGER_WORDS = re.compile(
    r"(?<![\w$.-])(rm|del|erase|rd|rmdir|ri|mv|move|mi|cp|copy|cpi|kill|spps|saps|start|ni|md|mkdir|"
    r"sc|ac|sp|clc|clv|iwr|irm|curl|wget|cmd|powershell|pwsh|sv|set|reg|regedit|net|netsh|sc\.exe|"
    r"shutdown|format|diskpart|bcdedit|takeown|icacls|schtasks|wmic|msiexec|setx)(?![\w-])", re.I)
# Could wreck the PC or lose data for good: always confirm, even when the owner asked.
CATASTROPHIC = re.compile(
    r"\b(format(-volume)?\s+[a-z]:|format-volume|clear-disk|initialize-disk|remove-partition|diskpart|"
    r"bcdedit|cipher\s+/w|reg(\.exe)?\s+delete\s+hk(lm|ey_local_machine)|remove-item\s+.*(hklm:|c:\\windows|"
    r"c:\\program files)|takeown\s+/f\s+c:\\windows|vssadmin\s+delete|wbadmin\s+delete|"
    r"disable-computerrestore|set-mppreference\s+.*-disable|uninstall-windowsfeature|"
    r"stop-computer|restart-computer|shutdown(\.exe)?\s+/[srp])\b", re.I)
SECRETS = re.compile(r"\.env\b|\.pem\b|\.key\b|\.kdbx\b|id_rsa|id_ed25519|\.ssh|\.aws|credential|password", re.I)


def is_read_only(command: str) -> bool:
    """True only for commands that clearly just look at things. Anything unsure -> ask."""
    if DANGER.search(command) or SECRETS.search(command):
        return False
    if DANGER_METHODS.search(command) or DANGER_STATIC.search(command):
        return False
    # ignore quoted strings when looking for dangerous words ('Format-Table' etc. handled below)
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", "''", command)
    if DANGER_WORDS.search(re.sub(VERB_NOUN, "", unquoted)):
        return False
    # every cmdlet mentioned anywhere (including inside { } blocks) must be a read-only one
    for verb, noun in VERB_NOUN.findall(command):
        name = f"{verb}-{noun}".lower()
        if verb.lower() in PS_VERBS and verb.lower() not in SAFE_CMDLET_VERBS and name not in SAFE_CMDLETS:
            return False
    for segment in re.split(r"[|;\n{}]", command):
        words = segment.strip().split()
        if not words:
            continue
        first = words[0].lower()
        # expressions, variables, literals, hashtable keys and keywords
        if first[0] in "$[(@'\"0123456789)" or re.match(r"^\w+\s*=", segment.strip()) or first in SAFE_KEYWORDS:
            continue
        if first.startswith("-"):  # operators like -and / -gt continuing an expression
            continue
        if first in SAFE_SUBCOMMANDS:
            if len(words) < 2 or words[1].lower() not in SAFE_SUBCOMMANDS[first]:
                return False
            if first == "powercfg" and any(w.lower().startswith(("/set", "/change", "/delete", "/h")) for w in words[1:]):
                return False
            continue
        if first in SAFE_COMMANDS:
            continue
        if "-" in first and (first.split("-", 1)[0] in SAFE_CMDLET_VERBS or first in SAFE_CMDLETS):
            continue
        return False
    return True


def run_command(command: str, reason: str, timeout_seconds: int = 60) -> dict:
    timeout = max(5, min(timeout_seconds or 60, 900))
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout, cwd=str(Path.home()),
            creationflags=NO_WINDOW, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"The command didn't finish within {timeout}s and was stopped.")
    out, err = p.stdout or "", p.stderr or ""
    return {
        "exit_code": p.returncode,
        "output": out[-8000:] if len(out) > 8000 else out,
        "errors": err[-3000:] if err else "",
        "truncated": len(out) > 8000,
    }


# ---- processes ------------------------------------------------------------------
def _ps_json(script: str) -> list[dict]:
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
    data = json.loads(p.stdout or "[]") if (p.stdout or "").strip() else []
    return [data] if isinstance(data, dict) else data


def _processes() -> list[dict]:
    return _ps_json(
        "Get-Process | Select-Object Id, ProcessName, MainWindowTitle, Description | ConvertTo-Json -Compress"
    )


def _match(name: str, procs: list[dict]) -> list[dict]:
    q = re.sub(r"\.exe$", "", name.strip().lower())
    words = q.split()
    def hay(p):
        return " ".join(str(p.get(k) or "") for k in ("ProcessName", "Description", "MainWindowTitle")).lower()
    exact = [p for p in procs if p["ProcessName"].lower() == q.replace(" ", "")]
    return exact or [p for p in procs if all(w in hay(p) for w in words)]


def list_running_apps() -> dict:
    procs = [p for p in _processes() if p.get("MainWindowTitle")]
    return {"apps": sorted({f"{p.get('Description') or p['ProcessName']} — {p['MainWindowTitle']}" for p in procs})}


def close_app(name: str, force: bool = False) -> dict:
    procs = _match(name, _processes())
    if not procs:
        return {"closed": [], "note": f"Nothing called '{name}' is running."}
    names = sorted({p["ProcessName"] for p in procs})
    if len(names) > 3:
        return {"closed": [], "ambiguous": names[:15], "note": "Several programs match; use a more exact name."}
    ids = ",".join(str(p["Id"]) for p in procs)
    if force:
        subprocess.run(["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {ids} -Force"],
                       capture_output=True, timeout=20, creationflags=NO_WINDOW)
    else:
        # polite close: ask each window to close (lets apps save / prompt)
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Get-Process -Id {ids} -ErrorAction SilentlyContinue | ForEach-Object {{ [void]$_.CloseMainWindow() }}"],
                       capture_output=True, timeout=20, creationflags=NO_WINDOW)
        for n in names:  # apps that live in the tray (Steam, Discord) ignore window close
            subprocess.run(["taskkill", "/IM", f"{n}.exe"], capture_output=True, timeout=20, creationflags=NO_WINDOW)
        if any(n.lower() == "steam" for n in names):
            os.startfile("steam://exit")
    time.sleep(2.5)
    still = [n for n in names if _match(n, _processes())]
    result = {"asked_to_close": names, "still_running": still}
    if still and not force:
        result["note"] = "Some didn't close politely. Use force=true to force-close (unsaved work may be lost)."
    return result


# ---- files ----------------------------------------------------------------------
def write_file(path: str, content: str, mode: str = "create") -> dict:
    p = resolve(path)
    if mode == "create" and p.exists():
        raise ToolError(f"{p} already exists. Use mode 'overwrite' or 'append'.")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if mode == "append" else "w", encoding="utf-8") as f:
        f.write(content)
    return {"written": str(p), "mode": mode, "chars": len(content)}


def make_folder(path: str) -> dict:
    p = resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return {"folder": str(p)}


def move_or_copy(source: str, destination: str, copy: bool = False) -> dict:
    src, dst = resolve(source), resolve(destination)
    if not src.exists():
        raise ToolError(f"{src} doesn't exist.")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"{dst} already exists; delete or rename it first.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
    else:
        shutil.move(str(src), str(dst))
    return {"copied" if copy else "moved": str(src), "to": str(dst)}


def delete_to_recycle_bin(path: str) -> dict:
    p = resolve(path)
    if not p.exists():
        raise ToolError(f"{p} doesn't exist.")
    if len(p.parts) <= 2 or p in (Path.home(), Path(os.environ.get("SystemRoot", r"C:\Windows"))):
        raise ToolError("Refusing to delete a drive root, the home folder or the Windows folder.")
    method = "DeleteDirectory" if p.is_dir() else "DeleteFile"
    script = (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        f"[Microsoft.VisualBasic.FileIO.FileSystem]::{method}("
        f"'{str(p).replace(chr(39), chr(39) * 2)}', 'OnlyErrorDialogs', 'SendToRecycleBin')"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    if r.returncode != 0 or p.exists():
        raise ToolError(f"Couldn't delete {p}: {(r.stderr or '').strip()[:300]}")
    return {"deleted_to_recycle_bin": str(p)}


# ---- media keys -----------------------------------------------------------------
MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
              "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}


def media(action: str, times: int = 1) -> dict:
    vk = MEDIA_KEYS[action]
    times = max(1, min(times or 1, 50))
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
        time.sleep(0.02)
    return {"pressed": action, "times": times}


def register(reg: Registry) -> None:
    reg.register(Tool(
        name="computer_run_command",
        description=(
            "Run a Windows PowerShell command on the owner's PC and get its output. Use for anything "
            "the other tools don't cover: system details, network checks, installing software with "
            "winget, changing settings, managing services, batch file work, and so on. Read-only "
            "commands run straight away; anything that changes the system pauses for the owner's "
            "spoken yes automatically, so don't ask them yourself first. Prefer one clear command; "
            "avoid interactive commands that wait for input."
        ),
        properties={
            "command": {"type": "string", "description": "PowerShell command to run."},
            "reason": {"type": "string", "description": "Plain-English description of what it does, e.g. 'Install Spotify'. Read aloud when asking for approval."},
            "timeout_seconds": {"type": "integer", "description": "Default 60, up to 900 for installs."},
        },
        required=["command", "reason"],
        handler=run_command,
        confirm_if=lambda a: None if is_read_only(a["command"]) else f"{a['reason'].rstrip('.')}.{CONFIRM_MARK}{a['command']}",
        critical_if=lambda a: (
            f"{a['reason'].rstrip('.')}. This one can seriously change or damage the system.{CONFIRM_MARK}{a['command']}"
            if CATASTROPHIC.search(a["command"]) else None
        ),
    ))
    reg.register(Tool(
        name="computer_list_running_apps",
        description="List the apps currently open on screen (window titles).",
        properties={},
        handler=list_running_apps,
    ))
    reg.register(Tool(
        name="computer_close_app",
        description=(
            "Close a running app by name (e.g. 'Steam', 'Opera', 'Discord'). First try without force; "
            "if it reports the app is still running, call again with force=true."
        ),
        properties={
            "name": {"type": "string"},
            "force": {"type": "boolean", "description": "Force-quit (may lose unsaved work). Default false."},
        },
        required=["name"],
        handler=close_app,
        confirm_if=lambda a: f"Force-quit {a['name']}; any unsaved work in it will be lost." if a.get("force") else None,
    ))
    reg.register(Tool(
        name="computer_write_file",
        description="Create a text file, or overwrite or append to one. Use for notes, lists, scripts, documents.",
        properties={
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["create", "overwrite", "append"], "description": "Default 'create' (fails if it exists)."},
        },
        required=["path", "content"],
        handler=write_file,
        confirm_if=lambda a: f"Overwrite {a['path']}, replacing what's in it." if a.get("mode") == "overwrite" and resolve(a["path"]).exists() else None,
    ))
    reg.register(Tool(
        name="computer_make_folder",
        description="Create a folder (and any missing parent folders).",
        properties={"path": {"type": "string"}},
        required=["path"],
        handler=make_folder,
    ))
    reg.register(Tool(
        name="computer_move_or_copy",
        description="Move/rename or copy a file or folder. Won't overwrite an existing destination.",
        properties={
            "source": {"type": "string"},
            "destination": {"type": "string", "description": "New path, or an existing folder to put it in."},
            "copy": {"type": "boolean", "description": "true = copy, false = move/rename. Default false."},
        },
        required=["source", "destination"],
        handler=move_or_copy,
    ))
    reg.register(Tool(
        name="computer_delete",
        description="Delete a file or folder by sending it to the Recycle Bin (recoverable).",
        properties={"path": {"type": "string"}},
        required=["path"],
        handler=delete_to_recycle_bin,
        confirm_if=lambda a: f"Delete {resolve(a['path'])} (it goes to the Recycle Bin).",
    ))
    reg.register(Tool(
        name="computer_media",
        description="Press media keys: play/pause, next or previous track, stop, volume up/down (repeat with times), mute.",
        properties={
            "action": {"type": "string", "enum": list(MEDIA_KEYS)},
            "times": {"type": "integer", "description": "How many presses; each volume step is about 2%. Default 1."},
        },
        required=["action"],
        handler=media,
    ))
