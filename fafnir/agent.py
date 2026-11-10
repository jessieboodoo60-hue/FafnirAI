"""The shared agent core. Every channel (typed, spoken, heartbeat) calls `run_turn`."""
from __future__ import annotations

import threading
from typing import Any, Callable

from .llm import LLM, Reply
from .tools.registry import Confirm, Registry

MAX_STEPS = 12  # model calls per turn; stops runaway tool loops

# on_tool(name, input, result_text, is_error). For Anthropic-hosted tools (web search)
# it's called once when the call starts, with input and result_text set to None.
ToolCallback = Callable[[str, Any, str, bool], None]


class Agent:
    def __init__(
        self,
        llm: LLM,
        system_prompt: str | Callable[[], str],
        registry: Registry | None = None,
        turn_context: Callable[[], str] | None = None,
    ):
        self.llm = llm
        # Built once per conversation (at start and on reset) and then frozen; history is
        # append-only. Editing either mid-conversation invalidates the model's saved
        # reasoning (see prompts.py). A callable lets reset() reload memory.
        self._make_system = system_prompt if callable(system_prompt) else (lambda: system_prompt)
        self.system_prompt = self._make_system()
        self.turn_context = turn_context
        self.registry = registry or Registry()
        self.history: list[dict] = []

    def run_turn(
        self,
        user_text: str,
        on_text: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
        on_tool: ToolCallback | None = None,
        confirm: Confirm | None = None,
        owner_present: bool = True,
    ) -> Reply:
        """Run one user turn to completion, including any tool calls the model makes.

        On failure, the whole turn is removed from history so it never ends on an
        unanswered message, and the error propagates. If `cancel` fires, what was
        said so far is kept, marked as interrupted.
        """
        start = len(self.history)
        self.history.append({"role": "user", "content": user_text})
        if self.turn_context:
            # mid-conversation system note (e.g. the current time), right after the user turn
            self.history.append({"role": "system", "content": self.turn_context()})
        tools = self.registry.schemas() or None
        system = self.system_prompt
        def on_server_tool(name: str) -> None:
            # web search runs code internally to filter results; only show the web calls
            if on_tool and name.startswith("web_"):
                on_tool(name, None, None, False)
        try:
            for step in range(MAX_STEPS):
                reply = self.llm.stream_reply(
                    system, self.history, on_text=on_text, tools=tools,
                    cancel=cancel, on_server_tool=on_server_tool,
                )
                self.history.append({"role": "assistant", "content": reply.content})

                if reply.stop_reason == "pause_turn" and step < MAX_STEPS - 1:
                    continue  # a server tool (web) needs another pass
                if not reply.tool_uses or reply.stop_reason not in ("tool_use", "max_tokens"):
                    return reply

                last_step = step == MAX_STEPS - 1
                stop_now = last_step or reply.stop_reason == "max_tokens" or (
                    cancel is not None and cancel.is_set()
                )
                results: list[dict] = []
                for block in reply.tool_uses:
                    if stop_now or (cancel is not None and cancel.is_set()):
                        text, is_error = "Not run: the turn was stopped.", True
                    else:
                        text, is_error = self.registry.run(
                            block.name, block.input, confirm=confirm, owner_present=owner_present
                        )
                        if on_tool:
                            on_tool(block.name, block.input, text, is_error)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": text, "is_error": is_error})
                if step == MAX_STEPS - 2:
                    results.append({"type": "text", "text": "Tool step limit reached. Answer now without more tools."})
                # all results for one assistant message go back in a single user message
                self.history.append({"role": "user", "content": results})

                if cancel is not None and cancel.is_set():
                    self._close("[interrupted by the user]")
                    return Reply(text="", content=[], stop_reason="interrupted")
                if stop_now:
                    note = "[stopped: too many tool steps]" if last_step else "[stopped: reply was cut off]"
                    self._close(note)
                    return Reply(text=note, content=[], stop_reason="max_tokens")
            raise AssertionError("unreachable")
        except BaseException:
            del self.history[start:]
            raise

    def _close(self, note: str) -> None:
        """End a turn that stopped after tool results, so history stays valid."""
        self.history.append({"role": "assistant", "content": [{"type": "text", "text": note}]})

    def reset(self) -> None:
        """Start a fresh conversation (reloads long-term memory into the prompt)."""
        self.history.clear()
        self.system_prompt = self._make_system()
