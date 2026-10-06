"""Fire-and-forget task helper.

The Node code starts many promises without awaiting them (cache bumps,
invalidations, delivered receipts, FCM pushes). :func:`spawn` is the asyncio
equivalent: it schedules the coroutine, keeps a strong reference so it is not
garbage-collected mid-flight, and swallows/logs its exception (Node attached
``.catch(() => {})`` to these). :func:`drain` lets graceful shutdown wait for
in-flight work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from app.core.logging import logger

_background: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task | None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return None
    _background.add(task)

    def _done(t: asyncio.Task) -> None:
        _background.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.debug("background task failed", {"error": f"{t.exception()!r}"})

    task.add_done_callback(_done)
    return task


async def drain(timeout: float = 5.0) -> None:
    """Wait (bounded) until in-flight tasks AND the tasks they spawn have finished."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _background:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.wait(list(_background), timeout=remaining)


def pending() -> int:
    return len(_background)
