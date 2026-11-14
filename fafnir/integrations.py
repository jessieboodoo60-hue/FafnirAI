"""Outside services Fafnir can use once the owner turns them on.

Each one is OFF until the owner flips its switch (Settings > Integrations, the API, or
`[integrations]` in config.toml). A switch can only be turned on once the service's key is in the
encrypted vault (`python main.py --secret-set NAME`); keys are never typed into the app or sent to
the phone. Code that wants a service asks `enabled("higgsfield")` and gets False until both are true.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass

from . import secretstore


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    purpose: str
    secrets: tuple[str, ...] = ()
    how: str = ""                 # how to get it ready


PROVIDERS: tuple[Provider, ...] = (
    Provider("higgsfield", "Higgsfield", "Image and video generation for adverts and social posts",
             ("HIGGSFIELD_API_KEY",), "Create a Higgsfield account and an API key, then: python main.py --secret-set HIGGSFIELD_API_KEY"),
    Provider("gemini", "Google Gemini", "Research with grounded web search",
             ("GEMINI_API_KEY",), "Get a key at aistudio.google.com, then: python main.py --secret-set GEMINI_API_KEY"),
    Provider("openai", "OpenAI", "A low-cost model for sales replies and classification",
             ("OPENAI_API_KEY",), "Get a key at platform.openai.com, then: python main.py --secret-set OPENAI_API_KEY"),
    Provider("anthropic_api", "Anthropic API", "Claude billed per use (separate from your Claude subscription)",
             ("ANTHROPIC_API_KEY",), "Already in the vault if you added it; python main.py --secret-set ANTHROPIC_API_KEY to change it"),
    Provider("stripe", "Stripe", "Invoices and tracking real payments (Fafnir never moves money)",
             ("STRIPE_SECRET_KEY",), "Use a restricted or test-mode key first, then: python main.py --secret-set STRIPE_SECRET_KEY"),
    Provider("firebase", "Firebase Cloud Messaging", "Push notifications to the phone app",
             ("FIREBASE_SERVICE_ACCOUNT",), "Create a Firebase project and a service-account key, then: python main.py --secret-set FIREBASE_SERVICE_ACCOUNT"),
    Provider("tailscale", "Tailscale", "Reach Fafnir from your phone anywhere, privately (no open ports)",
             (), "Install Tailscale on this PC and your phone and sign in to the same account"),
)
BY_ID = {p.id: p for p in PROVIDERS}


def _flags() -> dict:
    from . import config_edit
    try:
        return dict(config_edit.read_all().get("integrations", {}))
    except Exception:
        return {}


def ready(p: Provider) -> tuple[bool, str]:
    """Is everything the service needs present? (key in the vault / program installed)"""
    missing = [s for s in p.secrets if not secretstore.get(s)]
    if missing:
        return False, f"needs {', '.join(missing)} in the vault"
    if p.id == "tailscale" and not (shutil.which("tailscale") or shutil.which("tailscale.exe")):
        return False, "Tailscale isn't installed on this PC"
    return True, "ready"


def enabled(pid: str) -> bool:
    p = BY_ID.get(pid)
    return bool(p and _flags().get(pid) is True and ready(p)[0])


def status() -> list[dict]:
    flags = _flags()
    out = []
    for p in PROVIDERS:
        ok, why = ready(p)
        out.append({"id": p.id, "name": p.name, "purpose": p.purpose, "switch": flags.get(p.id) is True,
                    "ready": ok, "active": flags.get(p.id) is True and ok, "detail": why, "how": p.how})
    return out


def check_can_enable(pid: str, on: bool) -> None:
    """Raises ValueError with a plain reason when a switch can't be turned on yet."""
    p = BY_ID.get(pid)
    if p is None:
        raise ValueError(f"Unknown integration '{pid}'.")
    if on:
        ok, why = ready(p)
        if not ok:
            raise ValueError(f"{p.name} can't be turned on yet: {why}. {p.how}")


def set_enabled(pid: str, on: bool) -> dict:
    from . import config_edit
    check_can_enable(pid, on)
    config_edit.set_value("integrations", pid, bool(on))
    return next(s for s in status() if s["id"] == pid)
