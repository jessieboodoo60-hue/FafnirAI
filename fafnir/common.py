"""Small helpers shared by every front end (terminal, voice console, desktop app)."""
from __future__ import annotations

import re

from .agent import Agent
from .config import ROOT, Config
from .llm import LLM
from .memory import MemoryStore
from .prompts import build_system_prompt, turn_context
from .tools import build_registry

YES_WORDS = {"y", "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
             "approve", "approved", "go", "do", "proceed", "affirmative", "please"}
NO_WORDS = {"no", "nope", "nah", "don't", "dont", "stop", "cancel", "not", "wait", "never"}


def is_yes(answer: str) -> bool:
    """Only a clear yes counts. Anything unclear or containing a no is a no."""
    words = set(re.findall(r"[a-z']+", answer.lower()))
    return bool(words & YES_WORDS) and not (words & NO_WORDS)


def cost_line(total, before) -> str:
    return f"[cost {total.minus(before).summary()} · session ${total.dollars:.2f}]"


def describe_tool_call(name: str, args, result: str | None, is_error: bool) -> str:
    """One-line summary of a tool call for the screen."""
    if result is None:
        return f"\n  [tool] {name} (run by Anthropic)\n"
    shown = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    if len(shown) > 100:
        shown = shown[:97] + "..."
    status = "FAILED: " + result[:150] if is_error else f"ok ({len(result)} chars)"
    return f"\n  [tool] {name}({shown}) -> {status}\n"


def build_agent(cfg: Config) -> Agent:
    """The one agent core every front end uses."""
    memory = MemoryStore(ROOT / "data" / "memory.md")
    if cfg.model.provider == "claude_code":
        from .claude_code import ClaudeCodeAgent
        agent = ClaudeCodeAgent(
            lambda: build_system_prompt(cfg.assistant, memory.prompt_block()),
            build_registry(cfg, memory),
            turn_context,
            model=cfg.model.name,
            effort=cfg.model.effort,
        )
        agent.registry.server_tools = []  # Claude Code brings its own web search/fetch
        agent.memory = memory
        return agent
    agent = Agent(
        LLM(cfg.model, cfg.anthropic_api_key),
        lambda: build_system_prompt(cfg.assistant, memory.prompt_block()),
        build_registry(cfg, memory),
        turn_context=turn_context,
    )
    agent.memory = memory
    return agent
