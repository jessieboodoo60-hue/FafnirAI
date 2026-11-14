"""Fafnir: a voice-first assistant harness. See AGENT.md."""

# Don't inherit another Claude Code session's sign-in. When Fafnir was started from inside a Claude
# Code session (for example restarted by the assistant that maintains it), it inherited that session's
# short-lived token and private server address (ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL, CLAUDECODE,
# CLAUDE_CODE_*). Its own Claude Code then used them instead of the owner's sign-in and reported
# "Claude Code isn't logged in" once they expired (2026-11-14). Fafnir always uses the owner's own
# sign-in, so these are removed before anything starts.
import os as _os

for _k in [k for k in _os.environ if k.startswith(("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_"))
           or k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PREVIEW_CLASSIFIER_FLOOR",
                    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")]:
    _os.environ.pop(_k, None)

# Python 3.15 lazy-imports workaround. The standard library loads
# concurrent.futures.ThreadPoolExecutor lazily ("lazy from .thread import ..."). Under some
# load orders (Qt, the Google client and the Claude Agent SDK all imported, then a pool
# first used from a background thread) the placeholder is never swapped for the real
# class, and asyncio fails with "'lazy_import' object is not callable". Loading the real
# classes now and putting them in place makes every later use see the real thing.
import concurrent.futures as _cf
from concurrent.futures.process import ProcessPoolExecutor as _ProcessPool
from concurrent.futures.thread import ThreadPoolExecutor as _ThreadPool

_cf.ThreadPoolExecutor = _ThreadPool
_cf.ProcessPoolExecutor = _ProcessPool
