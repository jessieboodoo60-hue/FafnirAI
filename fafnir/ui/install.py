"""Start Fafnir with Windows, and add it to the Start menu.

`python main.py --install-startup` creates two shortcuts that launch the desktop app with
no console window (pythonw.exe):
  - in the Startup folder, so it opens fullscreen on the chosen monitor when you log in
  - in the Start menu, so you can open it any time
`python main.py --remove-startup` removes both.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..config import ROOT

ICON = ROOT / "assets" / "fafnir.ico"


def _folders() -> tuple[Path, Path]:
    appdata = Path(os.environ["APPDATA"])
    programs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return programs / "Startup", programs


def _make_icon() -> None:
    """Render the orb into an .ico for the shortcuts (needs Qt, which the app uses anyway)."""
    if ICON.exists():
        return
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap, QRadialGradient, Qt
    _app = QGuiApplication.instance() or QGuiApplication([])
    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QPointF(128, 128)
    g = QRadialGradient(c, 124)
    g.setColorAt(0.0, QColor(255, 255, 255))
    g.setColorAt(0.3, QColor(63, 208, 255))
    g.setColorAt(0.75, QColor(20, 90, 140, 200))
    g.setColorAt(1.0, QColor(4, 7, 12, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(c, 124, 124)
    p.end()
    ICON.parent.mkdir(exist_ok=True)
    pm.save(str(ICON), "ICO")


def _shortcut(path: Path) -> None:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); "
        "$s.TargetPath = $env:TARGET; $s.Arguments = 'main.py --app'; "
        "$s.WorkingDirectory = $env:WORKDIR; $s.Description = 'Fafnir assistant'; "
        "if (Test-Path $env:ICON) { $s.IconLocation = $env:ICON }; $s.Save()"
    )
    env = dict(os.environ, LNK=str(path), TARGET=str(pythonw), WORKDIR=str(ROOT), ICON=str(ICON))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())


def install() -> int:
    startup, programs = _folders()
    try:
        _make_icon()
    except Exception as e:
        print(f"(Couldn't make an icon, using the default: {e})")
    for folder in (startup, programs):
        link = folder / "Fafnir.lnk"
        _shortcut(link)
        print(f"Created {link}")
    print("Fafnir will now open on your chosen monitor when you log in to Windows.")
    return 0


def remove() -> int:
    for folder in _folders():
        link = folder / "Fafnir.lnk"
        if link.exists():
            link.unlink()
            print(f"Removed {link}")
    print("Fafnir will no longer start with Windows.")
    return 0
