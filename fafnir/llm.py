"""The model seam. The only module that touches the Anthropic SDK.

Everything else calls `LLM.stream_reply` and gets back a `Reply`. Swapping providers,
adding logging or cost tracking happens here and nowhere else.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from .config import ModelConfig

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Safety net: if history ever stops matching what a saved reasoning block was made with,
# drop that block instead of failing the whole request.
BINDING_BETA = "thinking-binding-controls-2026-08-01"


class LLMError(Exception):
    """A model call failed. `str(err)` is a plain-language message safe to show the user."""


@dataclass
class Reply:
    text: str
    content: list[Any]          # full content blocks, appended to history as-is
    stop_reason: str | None
    tool_uses: list[Any] = field(default_factory=list)  # used from Tier 2
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Usage:
    """Running totals, so each turn's cost can be shown and a runaway loop is visible."""
    input: int = 0          # uncached input tokens (full price)
    cache_read: int = 0     # input tokens served from cache (~5% of price)
    cache_write: int = 0    # input tokens written to cache (125% of price)
    output: int = 0         # includes thinking
    web_searches: int = 0
    dollars: float = 0.0

    def copy(self) -> "Usage":
        return type(self)(**vars(self))

    def minus(self, other: "Usage") -> "Usage":
        return type(self)(**{k: getattr(self, k) - getattr(other, k) for k in vars(self)})

    def summary(self) -> str:
        total_in = self.input + self.cache_read + self.cache_write
        pct = f" · {100 * self.cache_read / total_in:.0f}% cached" if total_in else ""
        return f"${self.dollars:.3f}{pct}"


class LLM:
    def __init__(self, cfg: ModelConfig, api_key: str):
        self.cfg = cfg
        self.client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=cfg.max_retries,
            timeout=cfg.timeout_seconds,
        )
        self.usage = Usage()

    def _count(self, u) -> None:
        p = self.cfg.prices  # dollars per million tokens
        inp = getattr(u, "input_tokens", 0) or 0
        read = getattr(u, "cache_read_input_tokens", 0) or 0
        write = getattr(u, "cache_creation_input_tokens", 0) or 0
        out = getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        self.usage.input += inp
        self.usage.cache_read += read
        self.usage.cache_write += write
        self.usage.output += out
        self.usage.web_searches += searches
        self.usage.dollars += (
            inp * p["input"] + read * p["cache_read"] + write * p["cache_write"] + out * p["output"]
        ) / 1e6 + searches * p["web_search"] / 1000

    def stream_reply(
        self,
        system: str,
        messages: list[dict],
        on_text: Callable[[str], None] | None = None,
        tools: list[dict] | None = None,
        cancel: threading.Event | None = None,
        on_server_tool: Callable[[str], None] | None = None,
    ) -> Reply:
        """Send the conversation, stream text to `on_text`, return the final reply.

        If `cancel` is set mid-stream, generation stops and a Reply with the partial
        text and stop_reason "interrupted" is returned.
        """
        kwargs: dict[str, Any] = dict(
            model=self.cfg.name,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": self.cfg.effort},
            thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
            betas=[BINDING_BETA],
            # Prompt caching: the instructions, tools and conversation so far are identical
            # from one request to the next, so Anthropic caches them and re-reads them at
            # ~5% of the normal price. The system prompt is frozen and history is append-only
            # precisely so this keeps hitting.
            cache_control={"type": "ephemeral"},
        )
        if tools:
            kwargs["tools"] = tools
        if self.cfg.fallbacks:
            kwargs["betas"].append(FALLBACK_BETA)
            kwargs["fallbacks"] = "default"

        partial: list[str] = []
        try:
            with self.client.beta.messages.stream(**kwargs) as stream:
                for event in stream:
                    if cancel is not None and cancel.is_set():
                        break
                    if event.type == "content_block_start":
                        kind = event.content_block.type
                        if kind == "server_tool_use" and on_server_tool:
                            on_server_tool(event.content_block.name)
                        elif kind == "text" and partial and not partial[-1].endswith("\n"):
                            partial.append("\n")  # keep separate text blocks apart
                            if on_text:
                                on_text("\n")
                    elif event.type == "text":
                        partial.append(event.text)
                        if on_text:
                            on_text(event.text)
                if cancel is not None and cancel.is_set():
                    said = "".join(partial).strip()
                    note = (said + " " if said else "") + "[interrupted by the user]"
                    return Reply(
                        text="".join(partial),
                        content=[{"type": "text", "text": note}],
                        stop_reason="interrupted",
                    )
                final = stream.get_final_message()
        except anthropic.AuthenticationError:
            raise LLMError("The API key was rejected. Check ANTHROPIC_API_KEY in .env.")
        except anthropic.PermissionDeniedError:
            raise LLMError("The API key doesn't have access to this model.")
        except anthropic.NotFoundError:
            raise LLMError(f"Model '{self.cfg.name}' wasn't found. Check config.toml.")
        except anthropic.RateLimitError:
            raise LLMError("Rate limited by the API. Wait a moment and try again.")
        except anthropic.BadRequestError as e:
            raise LLMError(f"The request was rejected: {e.message}")
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise LLMError("The model service is overloaded or down. Try again shortly.")
            raise LLMError(f"API error {e.status_code}: {e.message}")
        except anthropic.APITimeoutError:
            raise LLMError("The model took too long to respond. Try again.")
        except anthropic.APIConnectionError:
            raise LLMError("Couldn't reach the model. Check your internet connection.")

        self._count(final.usage)
        if final.stop_reason == "refusal":
            raise LLMError("The model declined that request.")

        text = "".join(b.text for b in final.content if b.type == "text")
        return Reply(
            text=text,
            content=list(final.content),
            stop_reason=final.stop_reason,
            tool_uses=[b for b in final.content if b.type == "tool_use"],
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
        )
