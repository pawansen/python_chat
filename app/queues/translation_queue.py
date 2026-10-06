"""In-process background translation queue (port of src/queue/translation.queue.js).

Guarantees preserved: synchronous non-blocking enqueue, bounded concurrency
(TRANSLATE_QUEUE_CONCURRENCY), per-job retry with exponential backoff
(TRANSLATE_JOB_RETRIES / _BACKOFF_MS / _BACKOFF_MAX_MS), de-duplication by
message id, depth cap with drop-oldest (TRANSLATE_QUEUE_MAX_DEPTH), structured
logs + counters, and a failing job never crashes the process.

Cluster note (unchanged): each process has its own queue; a message is
translated by the instance that persisted it and the update fans out to all
instances through the Socket.IO Redis backplane. Jobs are not durable across
a restart.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import get_settings
from app.core.js import js_string, nullish, truthy
from app.core.logging import component_log

log = component_log("translation-queue")

Processor = Callable[[dict[str, Any]], Awaitable[None]]

_state: dict[str, Any] = {
    "queue": deque(),
    "active": 0,
    "keys": set(),
    "processor": None,
    "stats": {"queued": 0, "processed": 0, "failed": 0, "retried": 0, "dropped": 0},
}
_drain_scheduled = False


def _cfg():
    return get_settings().translate


def set_translation_processor(fn: Processor) -> None:
    _state["processor"] = fn
    log("info", "processor_registered")
    _schedule_drain()


def queue_translation(job: dict[str, Any]) -> None:
    try:
        if not job or not truthy(job.get("content")) or nullish(job.get("messageId")):
            return
        key = js_string(job["messageId"])
        if key in _state["keys"]:
            return
        max_depth = int(_cfg().max_queue_depth or 5000)
        if len(_state["queue"]) >= max_depth:
            dropped = _state["queue"].popleft()
            _state["keys"].discard(dropped.get("key"))
            _state["stats"]["dropped"] += 1
            log(
                "error",
                "translation_queue_overflow",
                maxDepth=max_depth,
                droppedMessageId=dropped.get("messageId"),
                totalDropped=_state["stats"]["dropped"],
            )
        _state["keys"].add(key)
        _state["queue"].append({**job, "key": key, "attempt": 0, "enqueuedAt": int(time.time() * 1000)})
        _state["stats"]["queued"] += 1
        log("info", "translation_queued", messageId=job["messageId"], groupId=job.get("groupId"), depth=len(_state["queue"]))
        _schedule_drain()
    except Exception as error:  # noqa: BLE001 - enqueue must never throw into the hot path
        log("error", "enqueue_error", error=str(error))


def _schedule_drain() -> None:
    global _drain_scheduled
    if _drain_scheduled:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _drain_scheduled = True

    def run() -> None:
        global _drain_scheduled
        _drain_scheduled = False
        _drain()

    loop.call_soon(run)


def _drain() -> None:
    if _state["processor"] is None:
        return
    concurrency = int(_cfg().queue_concurrency)
    while _state["active"] < concurrency and _state["queue"]:
        job = _state["queue"].popleft()
        asyncio.get_running_loop().create_task(_run_job(job))


async def _run_job(job: dict[str, Any]) -> None:
    _state["active"] += 1
    started = time.monotonic()
    log("info", "translation_started", messageId=job.get("messageId"), attempt=job["attempt"])
    try:
        await _state["processor"](job)
        _state["stats"]["processed"] += 1
        _state["keys"].discard(job["key"])
        log(
            "info",
            "translation_completed",
            messageId=job.get("messageId"),
            attempt=job["attempt"],
            ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as error:  # noqa: BLE001
        cfg = _cfg()
        if job["attempt"] < cfg.job_retries:
            attempt = job["attempt"] + 1
            backoff = min(cfg.job_backoff_ms * 2 ** job["attempt"], cfg.job_backoff_max_ms)
            _state["stats"]["retried"] += 1
            log("error", "translation_failed_retry", messageId=job.get("messageId"), attempt=attempt, backoff=backoff, error=str(error))

            def requeue() -> None:
                _state["queue"].append({**job, "attempt": attempt})
                _schedule_drain()

            asyncio.get_running_loop().call_later(backoff / 1000, requeue)
        else:
            _state["stats"]["failed"] += 1
            _state["keys"].discard(job["key"])
            log("error", "translation_failed_permanent", messageId=job.get("messageId"), attempts=job["attempt"] + 1, error=str(error))
    finally:
        _state["active"] -= 1
        _schedule_drain()


def get_queue_stats() -> dict[str, Any]:
    return {"depth": len(_state["queue"]), "active": _state["active"], **_state["stats"]}


def reset_for_tests() -> None:
    _state["queue"].clear()
    _state["active"] = 0
    _state["keys"].clear()
    _state["processor"] = None
    for key in _state["stats"]:
        _state["stats"][key] = 0
