"""Backups of the owner's personal data (what GitHub deliberately doesn't hold).

Zips data/ (memory, business tracker, notices, conversations, Google login, wake word
recordings), .env (API keys) and config.toml into a dated file in the backup folder,
keeping the newest `keep` copies. Run daily by the heartbeat, or by hand with
`python main.py --backup`. To restore, unzip the newest file into the project folder.
"""
from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

from .config import ROOT

DEFAULT_FOLDER = "D:/FafnirBackups"
INCLUDE = ["data", ".env", "config.toml"]


def make_backup(folder: str | Path = DEFAULT_FOLDER, keep: int = 14) -> dict:
    dest = Path(folder).expanduser()
    if not dest.anchor or not Path(dest.anchor).exists():
        raise OSError(f"Backup drive {dest.anchor or dest} isn't available.")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"fafnir-{datetime.now():%Y-%m-%d_%H%M}.zip"
    files = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name in INCLUDE:
            src = ROOT / name
            if src.is_file():
                z.write(src, name)
                files += 1
            elif src.is_dir():
                for p in src.rglob("*"):
                    if p.is_file() and p.suffix != ".tmp":
                        z.write(p, p.relative_to(ROOT).as_posix())
                        files += 1
    # sanity check: the archive must open and list every file
    with zipfile.ZipFile(target) as z:
        if z.testzip() is not None or len(z.namelist()) != files:
            raise OSError(f"Backup {target.name} failed its check.")
    old = sorted(dest.glob("fafnir-*.zip"))[:-keep] if keep > 0 else []
    for p in old:
        p.unlink()
    return {"file": str(target), "files": files, "size_kb": round(target.stat().st_size / 1024, 1),
            "removed_old": len(old)}


def main() -> int:
    from .heartbeat.runner import load_settings
    s = load_settings().get("backup", {})
    r = make_backup(s.get("folder", DEFAULT_FOLDER), int(s.get("keep", 14)))
    print(f"Backed up {r['files']} files ({r['size_kb']} KB) to {r['file']}")
    return 0
