"""In-process inbound-webhook queue (port of src/queue/webhook.queue.js).

The webhook HTTP handlers ack 200 immediately and enqueue; this queue
processes with bounded concurrency (WEBHOOK_QUEUE_CONCURRENCY), retries with
backoff (safe: processing is idempotent per wa_message_id), caps depth with
drop-oldest, and isolates failures. Jobs are not durable across a restart
(providers re-deliver unacknowledged webhooks).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import get_settings
from app.core.logging import component_log

log = component_log("webhook-queue")

Processor = Callable[[dict[str, Any]], Awaitable[None]]

_state: dict[str, Any] = {
    "queue": deque(),
    "active": 0,
    "processor": None,
    "seq": 0,
    "stats": {"queued": 0, "processed": 0, "failed": 0, "retried": 0, "dropped": 0},
}
_drain_scheduled = False


def _cfg():
    return get_settings().webhook


def set_webhook_processor(fn: Processor) -> None:
    _state["processor"] = fn
    log("info", "processor_registered")
    _schedule_drain()


def queue_webhook(job: dict[str, Any]) -> int | None:
    try:
        _state["seq"] += 1
        job_id = _state["seq"]
        max_depth = int(_cfg().max_depth or 5000)
        if len(_state["queue"]) >= max_depth:
            dropped = _state["queue"].popleft()
            _state["stats"]["dropped"] += 1
            log("error", "webhook_queue_overflow", maxDepth=max_depth, droppedId=dropped.get("__id"), totalDropped=_state["stats"]["dropped"])
        _state["queue"].append({**job, "__id": job_id, "attempt": 0})
        _state["stats"]["queued"] += 1
        log("info", "webhook_queued", id=job_id, source=job.get("source"), depth=len(_state["queue"]))
        _schedule_drain()
        return job_id
    except Exception as error:  # noqa: BLE001
        log("error", "enqueue_error", error=str(error))
        return None


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
    while _state["active"] < int(_cfg().concurrency) and _state["queue"]:
        asyncio.get_running_loop().create_task(_run_job(_state["queue"].popleft()))


async def _run_job(job: dict[str, Any]) -> None:
    _state["active"] += 1
    started = time.monotonic()
    log("info", "webhook_started", id=job["__id"], attempt=job["attempt"])
    try:
        await _state["processor"](job)
        _state["stats"]["processed"] += 1
        log("info", "webhook_completed", id=job["__id"], attempt=job["attempt"], ms=int((time.monotonic() - started) * 1000))
    except Exception as error:  # noqa: BLE001
        cfg = _cfg()
        if job["attempt"] < cfg.retries:
            attempt = job["attempt"] + 1
            backoff = min(cfg.backoff_ms * 2 ** job["attempt"], cfg.backoff_max_ms)
            _state["stats"]["retried"] += 1
            log("error", "webhook_failed_retry", id=job["__id"], attempt=attempt, backoff=backoff, error=str(error))

            def requeue() -> None:
                _state["queue"].append({**job, "attempt": attempt})
                _schedule_drain()

            asyncio.get_running_loop().call_later(backoff / 1000, requeue)
        else:
            _state["stats"]["failed"] += 1
            log("error", "webhook_failed_permanent", id=job["__id"], attempts=job["attempt"] + 1, error=str(error))
    finally:
        _state["active"] -= 1
        _schedule_drain()


def get_webhook_queue_stats() -> dict[str, Any]:
    return {"depth": len(_state["queue"]), "active": _state["active"], **_state["stats"]}


def reset_for_tests() -> None:
    _state["queue"].clear()
    _state["active"] = 0
    _state["processor"] = None
    _state["seq"] = 0
    for key in _state["stats"]:
        _state["stats"][key] = 0
