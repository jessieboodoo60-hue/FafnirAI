"""Voice mode in a terminal: push-to-talk in, speech out, typed input still works.

A thin printer around the shared Engine (fafnir/engine.py).
"""
from __future__ import annotations

from ..agent import Agent
from ..common import describe_tool_call
from ..config import Config
from ..engine import Engine, Events, TurnStats


def _out(text: str) -> None:
    print(text, end="", flush=True)


class _ConsoleEvents(Events):
    def __init__(self, cfg: Config):
        self.cfg, self.prefix = cfg, f"{cfg.assistant.name.lower()}> "

    def heard(self, text):
        _out(f"you (heard)> {text}\n")

    def reply_start(self):
        _out(self.prefix)

    def reply_text(self, delta):
        _out(delta)

    def tool(self, name, args, result, is_error):
        _out(describe_tool_call(name, args, result, is_error) + self.prefix)

    def reply_end(self, interrupted, stats: TurnStats):
        _out(" [interrupted]\n" if interrupted else "\n")
        if interrupted or not self.cfg.voice.show_timings:
            return
        parts = []
        if stats.transcribe:
            parts.append(f"transcribe {stats.transcribe}")
        if stats.first_word is not None:
            parts.append(f"first word {stats.first_word:.1f}s")
        if stats.first_audio is not None:
            parts.append(f"first audio {stats.first_audio:.1f}s after release")
        if self.cfg.model.show_cost and stats.cost:
            parts.append(stats.cost)
        if parts:
            _out(f"[{' · '.join(parts)}]\n")

    def notice(self, text):
        _out(f"[{text}]\n")

    def error(self, text):
        _out(f"\n[{text}]\n")

    def confirm_request(self, action):
        _out(f"\n  [confirm] {action}\n  Hold the key and say yes or no (or type it).\n")

    def confirm_done(self, approved, timed_out):
        if timed_out:
            _out("  [no answer in time, so I didn't do it]\n")
        _out("  -> approved\n" if approved else "  -> declined\n")


class ConsoleVoice:
    def __init__(self, cfg: Config, agent: Agent):
        self.cfg, self.agent = cfg, agent
        self.engine = Engine(cfg, agent, _ConsoleEvents(cfg))

    def run(self) -> int:
        from ..cli import show_memory
        e = self.engine
        e.start()
        print(f"{self.cfg.assistant.name} voice mode. Hold [{self.cfg.voice.ptt_key}] to talk, "
              "or type a message and press Enter. /quit to exit.\n")
        try:
            while True:
                line = input().strip().lstrip("﻿")
                if not line:
                    continue
                if line in ("/quit", "/exit"):
                    e.answer_confirm(False)
                    break
                if line == "/reset":
                    e.reset()
                    print("(new conversation; memory reloaded)")
                elif line == "/memory":
                    show_memory(self.agent.memory)
                elif line == "/stop":
                    e.answer_confirm(False)
                    e.interrupt()
                else:
                    e.submit_text(line)
        except (EOFError, KeyboardInterrupt):
            pass
        e.stop()
        print(f"\n{self.cfg.assistant.name} signing off.")
        return 0
