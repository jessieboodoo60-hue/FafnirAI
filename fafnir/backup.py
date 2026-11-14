"""Backups of the owner's personal data (what GitHub deliberately doesn't hold).

Zips data/ (memory, business tracker, notices, conversations, Google login, wake word
recordings), .env (API keys) and config.toml into a dated file in the backup folder,
keeping the newest `keep` copies. Run daily by the heartbeat, or by hand with
`python main.py --backup`.

Since 2026-11-13 the zip is encrypted (AES-256-GCM, key in fafnir/vault.py) and saved as
`fafnir-<date>.zip.enc`, because it holds API keys and the Google login. Restore with
`python main.py --restore-backup <file> [--to <folder>] [--key <recovery key>]`; without
`--to` it restores into a new folder next to the project and never overwrites live data.
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

from .config import ROOT

DEFAULT_FOLDER = "D:/FafnirBackups"
INCLUDE = ["data", ".env", "config.toml"]
SKIP = {"data/vault.key"}          # the key must never travel inside the backup it locks
SKIP_DIRS = ()                     # folders to leave out (none: data/ is small)


def make_backup(folder: str | Path = DEFAULT_FOLDER, keep: int = 14) -> dict:
    dest = Path(folder).expanduser()
    if not dest.anchor or not Path(dest.anchor).exists():
        raise OSError(f"Backup drive {dest.anchor or dest} isn't available.")
    dest.mkdir(parents=True, exist_ok=True)
    from . import vault
    target = dest / f"fafnir-{datetime.now():%Y-%m-%d_%H%M}.zip.enc"
    files = 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in INCLUDE:
            src = ROOT / name
            if src.is_file():
                z.write(src, name)
                files += 1
            elif src.is_dir():
                for p in src.rglob("*"):
                    rel = p.relative_to(ROOT).as_posix()
                    if p.is_file() and p.suffix != ".tmp" and rel not in SKIP and not rel.startswith(SKIP_DIRS):
                        try:
                            z.write(p, rel)
                            files += 1
                        except OSError:
                            pass  # a file locked by the running app
    target.write_bytes(vault.encrypt(buf.getvalue()))
    # sanity check: the saved file must decrypt, pass the zip integrity test and hold the essentials
    with zipfile.ZipFile(io.BytesIO(vault.decrypt(target.read_bytes()))) as z:
        names = set(z.namelist())
        need = {n for n in (".env", "config.toml", "data/memory.vault") if (ROOT / n).exists()}
        if z.testzip() is not None or not need <= names:
            target.unlink()
            raise OSError(f"Backup {target.name} failed its check.")
        files = len(names)
    old = sorted(dest.glob("fafnir-*.zip.enc"))[:-keep] if keep > 0 else []
    for p in old:
        p.unlink()
    return {"file": str(target), "files": files, "size_kb": round(target.stat().st_size / 1024, 1),
            "removed_old": len(old), "encrypted": True}


def restore_backup(file: str | Path, to: str | Path | None = None, recovery_key: str | None = None) -> dict:
    """Decrypt a backup and unpack it into `to` (default: a new folder beside the project).
    Never writes into the live project folder unless `to` points there."""
    from . import vault
    src = Path(file)
    key = vault.key_from_recovery(recovery_key) if recovery_key else None
    data = vault.decrypt(src.read_bytes(), key) if src.suffix == ".enc" else src.read_bytes()
    dest = Path(to) if to else ROOT.parent / f"FafnirAI-restored-{datetime.now():%Y%m%d-%H%M%S}"
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():   # refuse paths that would escape the folder
            if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                raise OSError(f"Backup contains an unsafe path: {n}")
        z.extractall(dest)
        return {"restored_to": str(dest), "files": len(z.namelist())}


def main() -> int:
    from .heartbeat.runner import load_settings
    s = load_settings().get("backup", {})
    r = make_backup(s.get("folder", DEFAULT_FOLDER), int(s.get("keep", 14)))
    print(f"Backed up {r['files']} files ({r['size_kb']} KB) to {r['file']}")
    return 0
