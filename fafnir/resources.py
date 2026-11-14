"""Gaming-aware resource manager: decides what Fafnir may keep on the graphics card.

The RTX 5060 has 8 GB. Measured 2026-11-13:
  - Qwen3 8B needs ~5.5 GB (52-73 tokens/s when it fits, ~7 when it overflows),
  - Qwen3 4B needs ~3.2 GB (~90 tokens/s),
  - Kokoro (the voice) holds ~0.9 GB on the card,
  - a 4B model on the CPU reads prompts at only ~45 tokens/s (a normal request would take about a
    minute), so the CPU is NOT a usable fallback for the brain.

Profiles:
  gaming          nothing of Fafnir's on the card: local models unloaded, voice made on the CPU.
                  Requests go to the cloud brain according to [performance] gaming_brain.
  balanced        Qwen3 4B + voice on the card.
  ai_performance  Qwen3 8B + voice on the card.
  auto (default)  gaming while a game runs; otherwise the best profile that fits in the graphics
                  memory other programs leave free.

Switching down (to free memory for a game) is immediate; switching up waits for two calm
readings in a row so it doesn't flap. Nothing here changes security or system settings.
"""
from __future__ import annotations

import subprocess
import threading
import time

import httpx

from .config import Config

OLLAMA = "http://127.0.0.1:11434"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROFILES = ("gaming", "balanced", "ai_performance")
NEED_MB = {"ai_performance": 5700, "balanced": 3400}   # model + context, measured
GAME_FOLDERS = ("\\steamapps\\common\\", "\\epic games\\", "\\riot games\\", "\\xboxgames\\", "\\ubisoft game launcher\\games\\",
                "\\gog galaxy\\games\\", "\\ea games\\", "\\battle.net\\", "\\rockstar games\\")
NOT_GAMES = ("wallpaper_engine", "steam controller configs", "steamworks shared", "\\launcher\\", "epic online services",
             "eosoverlay", "webhelper", "crashreport", "anticheat", "battle.net.exe", "launcher.exe")
GAME_MIN_RAM_MB = 700   # helpers and launchers under those folders are small; real games are not


def gpu_memory() -> tuple[int, int] | None:
    """(used MiB, total MiB) of the graphics card, or None if it can't be read."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
        used, total = (int(x) for x in out.strip().splitlines()[0].split(","))
        return used, total
    except Exception:
        return None


def running_game(extra: tuple[str, ...] = (), ignore: tuple[str, ...] = ()) -> str | None:
    """Name of a game that's running now, if any."""
    try:
        import psutil
    except ImportError:
        return None
    extra_l, ignore_l = [e.lower() for e in extra], [i.lower() for i in ignore]
    for p in psutil.process_iter(["name", "exe", "memory_info"]):
        try:
            name, exe = (p.info["name"] or ""), (p.info["exe"] or "").lower()
            if not exe or name.lower() in ignore_l:
                continue
            if name.lower() in extra_l:
                return name
            if any(f in exe for f in GAME_FOLDERS) and not any(n in exe for n in NOT_GAMES) \
                    and p.info["memory_info"].rss >= GAME_MIN_RAM_MB * 1024 * 1024:
                return name
        except Exception:
            continue
    return None


def ollama_loaded() -> dict[str, int]:
    """Models Ollama has loaded -> MiB each holds on the graphics card."""
    try:
        models = httpx.get(f"{OLLAMA}/api/ps", timeout=5).json().get("models", [])
        return {m["name"]: int(m.get("size_vram", 0) / 2**20) for m in models}
    except Exception:
        return {}


def ollama_unload(name: str) -> None:
    try:
        httpx.post(f"{OLLAMA}/api/generate", json={"model": name, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


class ResourceManager:
    def __init__(self, cfg: Config, agent, set_voice_device=None, on_change=None):
        self.cfg, self.agent = cfg, agent
        self.perf = cfg.performance
        self.set_voice_device = set_voice_device     # fn("gpu" | "cpu")
        self.on_change = on_change                   # fn(profile, why) for displays
        self.is_local = hasattr(agent, "use_model")  # the local (Ollama) brain is in charge
        self.mode = self.perf.profile if self.perf.profile in PROFILES else "auto"
        self.profile = ""
        self.why = ""
        self.game: str | None = None
        self._calm = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ---- deciding ---------------------------------------------------------------------
    def snapshot(self) -> dict:
        mem = gpu_memory()
        loaded = ollama_loaded() if self.is_local else {}
        used, total = mem if mem else (0, 0)
        others = max(0, used - sum(loaded.values()))   # what everything except our LLMs is using
        return {"gpu_used_mb": used, "gpu_total_mb": total, "others_mb": others, "free_for_models_mb": max(0, total - others),
                "models_loaded": loaded, "game": running_game(self.perf.games, self.perf.not_games)}

    def decide(self, snap: dict) -> tuple[str, str]:
        if self.mode != "auto":
            return self.mode, "chosen by you"
        if snap["game"]:
            return "gaming", f"{snap['game']} is running"
        if not self.is_local:   # cloud brain: only the voice is ours on the card; no reason to move it
            return "balanced", "no game running"
        if not snap["gpu_total_mb"]:
            return "balanced", "couldn't read the graphics card"
        room = snap["free_for_models_mb"] - self.perf.vram_reserve_mb
        if room >= NEED_MB["ai_performance"]:
            return "ai_performance", f"{room} MB of graphics memory free"
        if room >= NEED_MB["balanced"]:
            return "balanced", f"only {room} MB of graphics memory free"
        return "gaming", f"only {room} MB of graphics memory free"

    # ---- applying ---------------------------------------------------------------------
    def apply(self, profile: str, why: str) -> None:
        with self._lock:
            if profile == self.profile:
                self.why = why
                return
            old, self.profile, self.why = self.profile, profile, why
            lc = self.cfg.local
            if self.is_local:
                want = {"gaming": None, "balanced": lc.fast, "ai_performance": lc.name}[profile]
                for name in ollama_loaded():
                    if name != want:
                        ollama_unload(name)
                self.agent.use_model(want, gaming_brain=self.perf.gaming_brain)
                if want:
                    threading.Thread(target=self._preload, daemon=True, name="model-preload").start()
            if self.set_voice_device:
                try:
                    self.set_voice_device("cpu" if profile == "gaming" else "gpu")
                except Exception as e:
                    print(f"[profile] couldn't move the voice: {e}", flush=True)
            print(f"[profile] {old or 'start'} -> {profile} ({why})", flush=True)
            if self.on_change:
                try:
                    self.on_change(profile, why)
                except Exception:
                    pass

    def _preload(self) -> None:
        try:
            t = time.monotonic()
            self.agent.warm_up()
            print(f"[profile] local model ready in {time.monotonic() - t:.1f}s", flush=True)
        except Exception as e:
            print(f"[profile] couldn't load the local model: {e}", flush=True)

    def tick(self) -> None:
        snap = self.snapshot()
        self.game = snap["game"]
        want, why = self.decide(snap)
        rank = {p: i for i, p in enumerate(PROFILES)}
        if not self.profile or rank[want] < rank[self.profile] or self.mode != "auto":
            self._calm = 0
            self.apply(want, why)              # first decision, or stepping down: right away
        elif rank[want] > rank[self.profile]:
            self._calm += 1
            if self._calm >= 2:                # stepping up: only after two calm readings
                self._calm = 0
                self.apply(want, why)
        else:
            self._calm = 0
            self.why = why

    # ---- lifecycle / control ----------------------------------------------------------------
    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as e:
                    print(f"[profile] check failed: {type(e).__name__}: {e}", flush=True)
                self._stop.wait(self.perf.check_seconds)
        threading.Thread(target=loop, daemon=True, name="resources").start()

    def stop(self) -> None:
        self._stop.set()

    def set_mode(self, mode: str) -> dict:
        mode = (mode or "").lower().strip().replace(" ", "_")
        mode = {"ai": "ai_performance", "performance": "ai_performance", "game": "gaming"}.get(mode, mode)
        if mode not in PROFILES + ("auto",):
            raise ValueError("Profile must be auto, gaming, balanced or ai_performance.")
        self.mode, self._calm = mode, 0
        snap = self.snapshot()
        self.game = snap["game"]
        self.apply(*self.decide(snap))   # the owner asked: switch now, no waiting for calm readings
        return self.status()

    def status(self) -> dict:
        snap = self.snapshot()
        return {"mode": self.mode, "profile": self.profile, "why": self.why, "game_running": snap["game"],
                "graphics_memory_used_mb": snap["gpu_used_mb"], "graphics_memory_total_mb": snap["gpu_total_mb"],
                "local_models_loaded": snap["models_loaded"],
                "brain": ("local: " + str(getattr(self.agent, "active_model", None) or "none (cloud, per gaming_brain)"))
                if self.is_local else f"cloud ({self.cfg.model.provider})"}
