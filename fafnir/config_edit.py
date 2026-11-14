"""Change single settings in config.toml from the app, keeping its comments and layout.

Only the settings listed in EDITABLE can be changed this way; each says how it's checked
and whether it applies straight away or after a restart."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .config import ROOT

CONFIG = ROOT / "config.toml"

# (section, key) -> (kind, allowed, when): kind is choice | bool | number | text;
# when is "now", "conversation" (next conversation) or "restart".
EDITABLE: dict[tuple[str, str], tuple[str, object, str]] = {
    ("model", "provider"): ("choice", ["ollama", "claude_code"], "restart"),   # local Qwen or cloud Claude
    ("model.local", "cloud_fallback"): ("choice", ["ask", "never"], "restart"),
    ("performance", "profile"): ("choice", ["auto", "gaming", "balanced", "ai_performance"], "now"),
    ("performance", "gaming_brain"): ("choice", ["ask", "cloud", "off"], "restart"),
    ("model", "name"): ("choice", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"], "conversation"),
    ("model", "effort"): ("choice", ["low", "medium", "high", "xhigh"], "conversation"),
    ("voice", "cue_sounds"): ("bool", None, "now"),
    ("voice", "barge_in"): ("bool", None, "restart"),
    ("voice.stt", "engine"): ("choice", ["local", "deepgram"], "restart"),
    ("voice.wake", "enabled"): ("bool", None, "restart"),
    ("voice.wake", "sensitivity"): ("number", (0.05, 0.95), "restart"),
    ("voice.wake", "follow_up_seconds"): ("number", (0, 60), "now"),
    ("voice.wake", "while_speaking"): ("bool", None, "now"),
    **{("integrations", p): ("bool", None, "now")
       for p in ("higgsfield", "gemini", "openai", "anthropic_api", "stripe", "firebase", "tailscale")},
    ("app", "screen"): ("choice", ["right", "left", "above", "primary"], "restart"),
    ("app", "background"): ("text", None, "now"),
}


def read_all() -> dict:
    with open(CONFIG, "rb") as f:
        return tomllib.load(f)


def current() -> dict:
    """The editable settings' values as {"section.key": value}."""
    data = read_all()
    out = {}
    for (section, key) in EDITABLE:
        node = data
        for part in section.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        out[f"{section}.{key}"] = node.get(key) if isinstance(node, dict) else None
    return out


def check(section: str, key: str, value):
    """Validate and normalise a value; raises ValueError with a plain message."""
    spec = EDITABLE.get((section, key))
    if spec is None:
        raise ValueError(f"{section}.{key} can't be changed from the app.")
    kind, allowed, _ = spec
    if section == "integrations":
        from . import integrations
        integrations.check_can_enable(key, bool(value))
    if kind == "choice":
        if value not in allowed:
            raise ValueError(f"{key} must be one of: {', '.join(allowed)}.")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be on or off.")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number.")
        lo, hi = allowed
        value = max(lo, min(hi, value))
        value = int(value) if float(value).is_integer() and key.endswith("seconds") else round(float(value), 2)
    elif kind == "text":
        value = str(value).strip()
        if "\n" in value or '"' in value:
            raise ValueError(f"{key} can't contain quotes or line breaks.")
    return value


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return '"' + str(value).replace("\\", "/") + '"'


def set_value(section: str, key: str, value) -> object:
    """Write one setting into config.toml (comments kept). Returns the stored value."""
    value = check(section, key, value)
    text = CONFIG.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    header = re.compile(r"^\s*\[([^\]]+)\]\s*(#.*)?$")
    start = next((i for i, ln in enumerate(lines) if (m := header.match(ln)) and m.group(1).strip() == section), None)
    if start is None:
        lines.append(f"\n[{section}]\n{key} = {_toml(value)}\n")
    else:
        end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
        pat = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s#]+)(.*)$", re.S)
        for i in range(start + 1, end):
            m = pat.match(lines[i])
            if m:
                lines[i] = m.group(1) + _toml(value) + m.group(3)
                break
        else:  # not there yet: add it after the section's last setting
            last = max((i for i in range(start, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")),
                       default=start)
            lines.insert(last + 1, f"{key} = {_toml(value)}\n")
    new = "".join(lines)
    tomllib.loads(new)  # never write a file that won't load
    CONFIG.write_text(new, encoding="utf-8", newline="")
    return value
