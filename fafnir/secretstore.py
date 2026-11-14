"""Where Fafnir's secrets live: API keys, bot tokens, the camera password.

They used to sit in a plain-text `.env` file, readable by anything that could open the folder, and
were loaded into the process environment, where every program Fafnir launched could read them.
Now they are in data/secrets.vault (AES-256-GCM, key in fafnir/vault.py, locked to the owner's
Windows account), are read only when needed, and are never placed in the environment.

For the owner:
    python main.py --secrets-import        move everything from .env into the vault
    python main.py --secret-set NAME       type one secret in (hidden input)
    python main.py --secrets-list          names only, never values

Values are never printed, logged or given to the model. `.env` still works as a fallback so a
half-finished move can't lock Fafnir out; --security-check reports if one is lying around.
"""
from __future__ import annotations

import json
import os
import threading

from . import vault
from .config import ROOT

FILE = ROOT / "data" / "secrets.vault"
ENV_FILE = ROOT / ".env"
_lock = threading.RLock()
_cache: dict[str, str] | None = None
_mtime = -1.0


def _load() -> dict[str, str]:
    global _cache, _mtime
    with _lock:
        try:
            m = FILE.stat().st_mtime
        except OSError:
            _cache, _mtime = {}, -1.0
            return _cache
        if _cache is None or m != _mtime:
            _cache = json.loads(vault.decrypt(FILE.read_bytes()).decode("utf-8"))
            _mtime = m
        return _cache


def _legacy_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(ENV_FILE).items() if v}


def get(name: str, default: str = "") -> str:
    """A secret's value: the vault first, then a leftover .env, then the real environment."""
    try:
        value = _load().get(name)
    except Exception:
        value = None
    if not value:
        value = _legacy_env().get(name) or os.environ.get(name)
    return (value or default).strip()


def names() -> list[str]:
    return sorted(_load())


def set_value(name: str, value: str) -> None:
    global _cache, _mtime
    with _lock:
        data = dict(_load())
        if value:
            data[name] = value.strip()
        else:
            data.pop(name, None)
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_bytes(vault.encrypt(json.dumps(data).encode("utf-8")))
        os.replace(tmp, FILE)
        _cache, _mtime = data, FILE.stat().st_mtime


def import_env(path=None) -> dict:
    """Copy every KEY=value from a .env file into the vault and check each one reads back."""
    from dotenv import dotenv_values
    src = {k: v for k, v in dotenv_values(path or ENV_FILE).items() if v}
    for k, v in src.items():
        set_value(k, v)
    stored = _load()
    ok = all(stored.get(k) == v.strip() for k, v in src.items())
    return {"imported": sorted(src), "verified": ok}
