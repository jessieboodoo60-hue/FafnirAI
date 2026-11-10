"""Run Fafnir's brain through Claude Code (your Claude subscription) instead of API credit.

Select it with `provider = "claude_code"` under [model] in config.toml. It is a drop-in
replacement for Agent: same run_turn()/reset() interface, same tools, same confirmation
gate and memory. Claude Code's own built-in tools (files, shell, etc.) are switched off;
only Fafnir's tools plus web search/fetch are available.

Uses the Claude Agent SDK, which drives the locally installed and logged-in `claude` CLI.
Subscription usage limits apply instead of per-token billing.
"""
from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures.thread import ThreadPoolExecutor  # imported eagerly on purpose, see below
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, StreamEvent,
    ToolUseBlock, create_sdk_mcp_server, tool,
)

from .llm import LLMError, Reply, Usage
from .tools.registry import Registry

SERVER = "fafnir"
WEB_TOOLS = ["WebSearch", "WebFetch"]


class EstimatedUsage(Usage):
    """Token counts are real; dollars are Claude Code's estimate (not billed per token)."""

    def summary(self) -> str:
        return super().summary() + " (est., subscription)"


class _UsageHolder:
    def __init__(self):
        self.usage = EstimatedUsage()


class ClaudeCodeAgent:
    def __init__(
        self,
        system_prompt: Callable[[], str],
        registry: Registry,
        turn_context: Callable[[], str] | None,
        model: str,
        effort: str,
    ):
        self._make_system = system_prompt
        self.registry = registry
        self.turn_context = turn_context
        self.model, self.effort = model, effort
        self.llm = _UsageHolder()
        self.history: list = []          # kept by Claude Code itself; here for interface parity
        self._client: ClaudeSDKClient | None = None
        self._turn: dict[str, Any] = {}  # callbacks for the turn in progress
        # Claude Code must use its own login (the subscription), not the API key Fafnir loaded.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        # Our own thread pool for running tools. Python 3.15's lazy imports can leave asyncio's
        # default pool (concurrent.futures.ThreadPoolExecutor) as an unloaded 'lazy_import'
        # placeholder when it's first needed from a background thread, which makes every tool
        # fail with "'lazy_import' object is not callable". Creating it here avoids that.
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fafnir-tool")
        self._loop = asyncio.new_event_loop()
        self._loop.set_default_executor(self._pool)
        threading.Thread(target=self._loop.run_forever, daemon=True, name="claude-code").start()

    # ---- tools: wrap every registry tool so it runs through the same gate -----------
    def _mcp_server(self):
        wrapped = []
        for name in self.registry.names():
            schema = self.registry._tools[name].schema()
            wrapped.append(tool(name, schema["description"], schema["input_schema"])(self._handler(name)))
        return create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=wrapped)

    def _handler(self, name: str):
        async def handle(args: dict[str, Any]) -> dict[str, Any]:
            t = self._turn
            cancel = t.get("cancel")
            if cancel is not None and cancel.is_set():
                text, is_error = "Not run: the turn was stopped.", True
            else:
                # run in a worker thread: tools may block (confirmations wait for the owner)
                text, is_error = await asyncio.get_running_loop().run_in_executor(
                    self._pool, lambda: self.registry.run(name, args, confirm=t.get("confirm"),
                                                    owner_present=t.get("owner_present", True)))
            if t.get("on_tool"):
                t["on_tool"](name, args, text, is_error)
            return {"content": [{"type": "text", "text": text}], "is_error": is_error}
        return handle

    def _options(self) -> ClaudeAgentOptions:
        mcp_names = [f"mcp__{SERVER}__{n}" for n in self.registry.names()]
        return ClaudeAgentOptions(
            system_prompt=self._make_system(),
            tools=WEB_TOOLS,                       # no files/shell/etc. from Claude Code itself
            allowed_tools=mcp_names + WEB_TOOLS,   # Fafnir's own gate handles confirmations
            mcp_servers={SERVER: self._mcp_server()},
            model=self.model,
            effort=self.effort,
            include_partial_messages=True,
            setting_sources=[],                    # ignore CLAUDE.md / user settings
            max_turns=12,
            cwd=os.path.expanduser("~"),
        )

    # ---- turns ------------------------------------------------------------------------
    def run_turn(self, user_text: str, on_text=None, cancel: threading.Event | None = None,
                 on_tool=None, confirm=None, owner_present: bool = True) -> Reply:
        fut = asyncio.run_coroutine_threadsafe(
            self._run(user_text, on_text, cancel, on_tool, confirm, owner_present), self._loop)
        try:
            return fut.result()
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(_friendly(e))

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self._client is None:
            client = ClaudeSDKClient(options=self._options())
            await client.connect()
            self._client = client
        return self._client

    async def _run(self, user_text, on_text, cancel, on_tool, confirm, owner_present) -> Reply:
        client = await self._ensure_client()
        self._turn = {"on_tool": on_tool, "confirm": confirm, "cancel": cancel, "owner_present": owner_present}
        prompt = f"[{self.turn_context()}]\n\n{user_text}" if self.turn_context else user_text
        await client.query(prompt)

        async def watch_cancel():
            while cancel is not None and not cancel.is_set():
                await asyncio.sleep(0.05)
            if cancel is not None:
                await client.interrupt()

        watcher = asyncio.create_task(watch_cancel())
        parts: list[str] = []
        result: ResultMessage | None = None

        def emit(s: str) -> None:
            parts.append(s)
            if on_text:
                on_text(s)

        try:
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    ev = msg.event or {}
                    if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                        emit(ev["delta"]["text"])
                    elif (ev.get("type") == "content_block_start"
                          and ev.get("content_block", {}).get("type") == "text"
                          and parts and not parts[-1].endswith("\n")):
                        emit("\n")  # keep separate text blocks apart
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock) and block.name in WEB_TOOLS and on_tool:
                            on_tool(block.name, None, None, False)
                elif isinstance(msg, ResultMessage):
                    result = msg
        finally:
            watcher.cancel()
            self._turn = {}

        if result is not None:
            self._count(result)
        if cancel is not None and cancel.is_set():
            return Reply(text="".join(parts), content=[], stop_reason="interrupted")
        if result is not None and result.is_error and result.subtype != "success":
            raise LLMError(_friendly_result(result))
        return Reply(text="".join(parts), content=[], stop_reason="end_turn")

    def _count(self, r: ResultMessage) -> None:
        u, us = r.usage or {}, self.llm.usage
        us.input += int(u.get("input_tokens", 0) or 0)
        us.cache_read += int(u.get("cache_read_input_tokens", 0) or 0)
        us.cache_write += int(u.get("cache_creation_input_tokens", 0) or 0)
        us.output += int(u.get("output_tokens", 0) or 0)
        us.dollars += float(r.total_cost_usd or 0.0)

    def reset(self) -> None:
        """New conversation: drop Claude Code's session and reload memory into the prompt."""
        async def close():
            if self._client is not None:
                try:
                    await self._client.disconnect()
                finally:
                    self._client = None
        asyncio.run_coroutine_threadsafe(close(), self._loop).result(timeout=15)


def _friendly(e: Exception) -> str:
    name = type(e).__name__
    if name == "CLINotFoundError":
        return "Claude Code isn't installed or can't be found. Install it, or set provider = \"api\" in config.toml."
    if name in ("CLIConnectionError", "ProcessError"):
        return f"Couldn't talk to Claude Code ({e}). Make sure you're logged in: run `claude` once in a terminal."
    return f"Claude Code error: {name}: {e}"


def _friendly_result(r: ResultMessage) -> str:
    text = (r.result or r.subtype or "").strip()
    low = text.lower()
    if "limit" in low or "usage" in low:
        return f"Your Claude subscription hit a usage limit: {text}"
    if "login" in low or "auth" in low:
        return "Claude Code isn't logged in. Run `claude` once in a terminal and sign in."
    return f"Claude Code couldn't finish: {text}"
