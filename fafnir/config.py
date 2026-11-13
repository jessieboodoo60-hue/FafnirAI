"""Load config.toml and secrets from the environment / .env."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """A setup problem the user has to fix (missing key, bad config)."""


@dataclass(frozen=True)
class AssistantConfig:
    name: str
    purpose: str
    tone: str
    home_location: str = ""


@dataclass(frozen=True)
class ModelConfig:
    name: str
    max_tokens: int
    effort: str
    fallbacks: bool
    max_retries: int
    timeout_seconds: float
    show_cost: bool = True
    provider: str = "api"  # "api" (Anthropic API credit) or "claude_code" (Claude subscription)

    @property
    def prices(self) -> dict:
        """Dollars per million tokens (web_search: per 1000 searches)."""
        return PRICES.get(self.name, PRICES["claude-opus-5-5"])


# Anthropic list prices. cache_write = 1.25x input (5-minute cache); cache_read ~5%.
PRICES = {
    "claude-opus-5-5":   {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0, "web_search": 10.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.5, "web_search": 10.0},
    "claude-haiku-5-5":  {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125, "web_search": 10.0},
}


@dataclass(frozen=True)
class VoiceConfig:
    ptt_key: str
    cue_sounds: bool
    show_timings: bool
    stt_model: str
    stt_language: str
    tts_voice_id: str
    tts_model: str
    deepgram_api_key: str
    elevenlabs_api_key: str
    tts_engine: str = "kokoro"     # "kokoro" (offline, no key) or "elevenlabs"
    kokoro_voice: str = "am_onyx"
    kokoro_pitch: float = -3.0
    kokoro_speed: float = 1.0
    kokoro_reverb: float = 0.0
    kokoro_lang: str = "en-us"
    # wake word ("Fafnir"): matched locally against the owner's recordings in data/wake/
    wake_enabled: bool = False
    wake_sensitivity: float = 0.5
    wake_engine: str = "whisper"  # "whisper" (offline recognizer) or "samples" (voice recordings)
    wake_end_silence: float = 1.0   # seconds of quiet that end what you're saying
    wake_while_speaking: bool = True  # say the wake word to interrupt Fafnir mid-sentence
    follow_up_seconds: float = 6.0    # after Fafnir speaks, just talk (no wake word) for this long; 0 = off
    mic_preference: tuple[str, ...] = ()  # mic names (or parts) in order of preference; first plugged-in wins

    def missing_keys(self) -> list[str]:
        missing = []
        if not self.deepgram_api_key:
            missing.append("DEEPGRAM_API_KEY")
        if self.tts_engine == "elevenlabs" and not self.elevenlabs_api_key:
            missing.append("ELEVENLABS_API_KEY")
        return missing


@dataclass(frozen=True)
class ToolsConfig:
    notes_folder: Path | None
    web: bool
    web_max_uses: int
    google: bool = True


@dataclass(frozen=True)
class Config:
    assistant: AssistantConfig
    model: ModelConfig
    voice: VoiceConfig
    tools: ToolsConfig
    anthropic_api_key: str


def _tools(raw: dict) -> ToolsConfig:
    t = raw.get("tools", {})
    folder = str(t.get("notes_folder", "")).strip()
    return ToolsConfig(
        notes_folder=Path(os.path.expandvars(os.path.expanduser(folder))) if folder else None,
        web=bool(t.get("web", True)),
        web_max_uses=int(t.get("web_max_uses", 5)),
        google=bool(t.get("google", True)),
    )


def _voice(raw: dict) -> VoiceConfig:
    v = raw.get("voice", {})
    stt = v.get("stt", {})
    tts = v.get("tts", {})
    return VoiceConfig(
        ptt_key=str(v.get("ptt_key", "f13")),
        mic_preference=tuple(str(m) for m in v.get("mic_preference", [])),
        cue_sounds=bool(v.get("cue_sounds", True)),
        show_timings=bool(v.get("show_timings", True)),
        stt_model=stt.get("model", "nova-3"),
        stt_language=stt.get("language", "en"),
        tts_voice_id=tts.get("voice_id", ""),
        tts_model=tts.get("model", "eleven_flash_v2_5"),
        deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY", "").strip(),
        elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY", "").strip(),
        tts_engine=str(tts.get("engine", "kokoro")),
        kokoro_voice=str(tts.get("kokoro_voice", "am_onyx")),
        kokoro_pitch=float(tts.get("kokoro_pitch", -3.0)),
        kokoro_speed=float(tts.get("kokoro_speed", 1.0)),
        kokoro_reverb=float(tts.get("kokoro_reverb", 0.0)),
        kokoro_lang=str(tts.get("kokoro_lang", "en-us")),
        wake_enabled=bool(v.get("wake", {}).get("enabled", False)),
        wake_sensitivity=float(v.get("wake", {}).get("sensitivity", 0.5)),
        wake_engine=str(v.get("wake", {}).get("engine", "whisper")),
        wake_end_silence=float(v.get("wake", {}).get("end_silence_seconds", 1.0)),
        wake_while_speaking=bool(v.get("wake", {}).get("while_speaking", True)),
        follow_up_seconds=float(v.get("wake", {}).get("follow_up_seconds", 6.0)),
    )


def load_config(path: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = path or ROOT / "config.toml"
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml is not valid TOML: {e}")

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    provider = str(raw.get("model", {}).get("provider", "api")).strip()
    if not key and provider != "claude_code":
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    try:
        a, m = raw["assistant"], raw["model"]
        return Config(
            assistant=AssistantConfig(
                name=a["name"], purpose=a["purpose"], tone=a["tone"],
                home_location=str(a.get("home_location", "")).strip(),
            ),
            model=ModelConfig(
                name=m["name"],
                max_tokens=int(m.get("max_tokens", 16000)),
                effort=m.get("effort", "medium"),
                fallbacks=bool(m.get("fallbacks", True)),
                max_retries=int(m.get("max_retries", 3)),
                timeout_seconds=float(m.get("timeout_seconds", 120)),
                show_cost=bool(m.get("show_cost", True)),
                provider=provider,
            ),
            voice=_voice(raw),
            tools=_tools(raw),
            anthropic_api_key=key,
        )
    except KeyError as e:
        raise ConfigError(f"config.toml is missing the setting {e}")
