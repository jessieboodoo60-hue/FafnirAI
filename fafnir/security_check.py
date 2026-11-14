"""`python main.py --security-check`: what's protected, what isn't, and what only the owner can fix.

Read-only. It changes nothing: Fafnir never alters Windows security settings (BitLocker, Secure
Boot, the firewall); it reports their state and says what to do.
"""
from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

from .config import ROOT

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
OK, WARN, TODO = "OK  ", "WARN", "YOU "


def _ps(cmd: str, timeout: int = 20) -> str:
    try:
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                              text=True, timeout=timeout, creationflags=NO_WINDOW).stdout.strip()
    except Exception:
        return ""


def _port_open_to_network(port: int) -> bool | None:
    """Is something listening on this port on a non-loopback address?"""
    out = _ps(f"Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | "
              "Select-Object -ExpandProperty LocalAddress")
    if not out:
        return None
    return any(a.strip() not in ("127.0.0.1", "::1") for a in out.splitlines())


def run() -> list[tuple[str, str, str]]:
    """[(status, what, detail)]"""
    from . import audit, vault
    rows: list[tuple[str, str, str]] = []
    data = ROOT / "data"

    # ---- Fafnir's own data ----
    try:
        vault.get_key(create=False)
        rows.append((OK, "Encryption key", "present and locked to this Windows account (DPAPI)"))
    except Exception as e:
        rows.append((WARN, "Encryption key", f"not usable: {e}"))
    for name, path in (("Secrets (API keys, tokens, camera password)", data / "secrets.vault"),
                       ("Memory", data / "memory.vault"), ("Google login token", data / "google_token.json"),
                       ("Google app credentials", data / "google_credentials.json")):
        if not path.exists():
            rows.append((WARN if "Secrets" in name or "Memory" in name else OK, name, "no file" if "Google" in name else "not created yet"))
        elif vault.is_encrypted(path):
            try:
                vault.decrypt(path.read_bytes())
                rows.append((OK, name, "encrypted, and opens with the key"))
            except Exception:
                rows.append((WARN, name, "encrypted but does NOT open with this key"))
        else:
            rows.append((WARN, name, "stored as plain text"))
    plain_days = [p.name for p in (data / "conversations").glob("*.jsonl")
                  if any(ln.strip() and not ln.startswith(vault.LINE_PREFIX) for ln in p.read_text(encoding="utf-8").splitlines())]
    rows.append((WARN, "Conversation logs", f"{len(plain_days)} day(s) still contain plain-text lines") if plain_days
                else (OK, "Conversation logs", "every line encrypted"))
    env = ROOT / ".env"
    rows.append((WARN, "Leftover .env file", "plain-text secrets on disk; run --secrets-import, then delete it") if env.exists()
                else (OK, "Leftover .env file", "none"))
    rec = ROOT / "RECOVERY_KEY.txt"
    rows.append((TODO, "Recovery key file", "still in the project folder: move the key somewhere safe and delete the file")
                if rec.exists() else (OK, "Recovery key file", "not lying around"))
    try:
        from .heartbeat.runner import load_settings
        folder = Path(load_settings().get("backup", {}).get("folder", "D:/FafnirBackups"))
        plain = list(folder.glob("fafnir-*.zip")) if folder.exists() else []
        enc = list(folder.glob("fafnir-*.zip.enc")) if folder.exists() else []
        rows.append((WARN, "Backups", f"{len(plain)} old UNENCRYPTED backup(s) in {folder} (they contain your keys)") if plain
                    else (OK, "Backups", f"{len(enc)} encrypted backup(s) in {folder}"))
    except Exception:
        pass
    v = audit.verify()
    rows.append((OK, "Action log", f"chain intact ({v['entries']} hashed entries)") if v["ok"]
                else (WARN, "Action log", f"chain BROKEN at line {v['first_problem_line']} ({v['problem_lines']} problem lines)"))
    from . import killswitch
    rows.append((WARN, "Kill switch", "ENGAGED") if killswitch.engaged() else (OK, "Kill switch", "ready (Ctrl+Alt+K)"))

    # ---- what listens on the network ----
    o = _port_open_to_network(11434)
    rows.append((OK, "Ollama (local model server)", "not running" if o is None else "listening on this PC only")
                if not o else (WARN, "Ollama (local model server)", "reachable from the network: unset OLLAMA_HOST or set it to 127.0.0.1"))
    w = _port_open_to_network(8787)
    rows.append((OK, "WhatsApp webhook port 8787", "closed" if w is None else "this PC only (reached through ngrok)")
                if not w else (WARN, "WhatsApp webhook port 8787", "open to the local network"))

    # ---- Windows settings: reported, never changed ----
    fw = _ps("Get-NetFirewallProfile | ForEach-Object { $_.Name + '=' + $_.Enabled }")
    off = [ln.split("=")[0] for ln in fw.splitlines() if ln.endswith("False")]
    rows.append((TODO, "Windows Firewall", f"OFF for: {', '.join(off)}. Turn it on in Windows Security.") if off
                else ((OK, "Windows Firewall", "on for every network type") if fw else (TODO, "Windows Firewall", "couldn't read; check Windows Security")))
    sb = _ps("try { Confirm-SecureBootUEFI } catch { 'unknown' }")
    rows.append((OK, "Secure Boot", "on") if sb == "True" else
                (TODO, "Secure Boot", "OFF: enable it in the PC's UEFI/BIOS setup" if sb == "False" else
                 "needs an administrator window to read: run `Confirm-SecureBootUEFI` in PowerShell (Admin)"))
    bl = _ps("try { (Get-BitLockerVolume -MountPoint C: -ErrorAction Stop).ProtectionStatus } catch { 'unknown' }")
    rows.append((OK, "BitLocker on C:", "on") if bl in ("On", "1") else
                (TODO, "BitLocker on C:", "OFF: without it, deleted files and the Windows key store can be read from the raw disk. "
                                           "Settings > Privacy & security > Device encryption" if bl in ("Off", "0") else
                 "needs an administrator window to read: run `manage-bde -status C:` in PowerShell (Admin)"))
    tpm = _ps("try { $t = Get-Tpm -ErrorAction Stop; $t.TpmPresent.ToString() + ',' + $t.TpmReady.ToString() } catch { 'unknown' }")
    rows.append((OK, "TPM", "present and ready") if tpm == "True,True" else
                (TODO, "TPM", "needs an administrator window to read: run `Get-Tpm` in PowerShell (Admin)" if tpm in ("", "unknown")
                 else f"present,ready = {tpm}"))
    return rows


def main() -> int:
    rows = run()
    print("\nFafnir security check (read-only; nothing was changed)\n")
    for status, what, detail in rows:
        print(f"  [{status}] {what}: {detail}")
    n_warn = sum(1 for r in rows if r[0] == WARN)
    n_you = sum(1 for r in rows if r[0] == TODO)
    print(f"\n{len(rows) - n_warn - n_you} fine, {n_warn} to fix, {n_you} for you to check or do in Windows.")
    print("Details and what is still exposed whatever we do: docs/SECURITY.md\n")
    return 0
