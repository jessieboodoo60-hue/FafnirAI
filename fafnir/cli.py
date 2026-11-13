"""Entry point and text interface. The text mode is kept forever as the debug path."""
from __future__ import annotations

import sys

from .agent import Agent
from .common import build_agent, cost_line, describe_tool_call, is_yes
from .config import ConfigError, load_config
from .llm import LLMError
from .memory import MemoryStore

HELP = "Commands: /reset (new conversation), /memory (what I remember), /tools, /quit, /help"


def show_memory(memory: MemoryStore) -> None:
    facts = memory.all()
    print(f"Memory file: {memory.path}")
    if not facts:
        print("  (nothing remembered yet)\n")
        return
    for f in facts:
        print(f"  ({f.id}) [{f.category}] {f.text}")
    print()


def _print(text: str) -> None:
    print(text, end="", flush=True)


def confirm_in_terminal(action: str) -> bool:
    print(f"\n  [confirm] Fafnir wants to: {action}")
    try:
        answer = input("  Allow? (yes/no) ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    ok = is_yes(answer)
    print("  -> approved\n" if ok else "  -> declined\n")
    return ok


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if "--learn-key" in argv:
        from .voice.ptt import learn_key
        return learn_key()
    if "--kill" in argv:
        from . import killswitch
        killswitch.engage("command line")
        print("Kill switch ON. Release with: python main.py --release")
        return
    if "--release" in argv:
        from . import killswitch
        print("Kill switch released." if killswitch.release("command line") else "The kill switch wasn't on.")
        return
    if "--backup" in argv:
        from .backup import main as backup_main
        return backup_main()
    if "--train-wake" in argv:
        from .config import ROOT
        from .voice.wake import record_samples
        return record_samples(ROOT / "data" / "wake")
    if "--install-startup" in argv or "--remove-startup" in argv:
        from .ui.install import install, remove
        return install() if "--install-startup" in argv else remove()
    if "--app" in argv:
        from .ui.app import run_app
        return run_app(argv)

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Setup problem: {e}")
        return 1
    agent = build_agent(cfg)

    if "--voice" in argv:
        missing = cfg.voice.missing_keys()
        if missing:
            print(f"Setup problem: voice mode needs {', '.join(missing)} in .env.")
            return 1
        from .voice.app import ConsoleVoice
        try:
            app = ConsoleVoice(cfg, agent)
        except ValueError as e:  # bad ptt_key
            print(f"Setup problem: {e}")
            return 1
        except Exception as e:  # audio device problems
            print(f"Setup problem: couldn't open audio: {e}")
            return 1
        return app.run()

    return text_loop(cfg, agent)


def text_loop(cfg, agent: Agent) -> int:
    name = cfg.assistant.name
    print(f"{name} is ready ({cfg.model.name}). {HELP}\n")

    while True:
        try:
            user = input("you> ").strip().lstrip("﻿")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user in ("/quit", "/exit"):
            break
        if user == "/reset":
            agent.reset()
            print("(new conversation; memory reloaded)\n")
            continue
        if user == "/help":
            print(HELP + "\n")
            continue
        if user == "/memory":
            show_memory(agent.memory)
            continue
        if user == "/tools":
            extra = [t["name"] for t in agent.registry.server_tools]
            print("Tools: " + ", ".join(agent.registry.names() + extra) + "\n")
            continue

        print(f"{name.lower()}> ", end="", flush=True)

        def on_tool(tname, args, result, is_error):
            _print(describe_tool_call(tname, args, result, is_error) + f"{name.lower()}> ")

        from .tools import CONVERSATIONS
        CONVERSATIONS.append("user", user, "typed")
        said: list[str] = []

        def on_text(t: str) -> None:
            said.append(t)
            _print(t)

        before = agent.llm.usage.copy()
        try:
            agent.run_turn(user, on_text=on_text, on_tool=on_tool, confirm=confirm_in_terminal)
            CONVERSATIONS.append("assistant", "".join(said))
            print("\n")
            if cfg.model.show_cost:
                print(cost_line(agent.llm.usage, before) + "\n")
        except LLMError as e:
            print(f"\n[{e}]\n")
        except KeyboardInterrupt:
            print("\n[interrupted]\n")

    print(f"{name} signing off.")
    return 0
