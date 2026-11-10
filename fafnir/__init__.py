"""Fafnir: a voice-first assistant harness. See AGENT.md."""

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
