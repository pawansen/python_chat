"""Per-connection, per-event Socket.IO rate limiter (port of socket.rate.limiter.util.js).

Fixed 10 s windows, per event type (send_message 30, delete_message 20,
chat_message_read 120, join_room/join_room_user/join_branch_room 40,
branch_checkin 20, any other event 60) plus 200 events combined per socket.
Over the limit the packet is DROPPED (handler not run) and the client gets
``rate_limit_exceeded``. State dies with the socket. Any limiter fault lets
the packet through.
"""

from __future__ import annotations

import time
from typing import Any

DEFAULT_WINDOW_MS = 10_000
EVENT_LIMITS = {
    "send_message": 30,
    "delete_message": 20,
    "chat_message_read": 120,
    "join_room": 40,
    "join_room_user": 40,
    "join_branch_room": 40,
    "branch_checkin": 20,
}
DEFAULT_EVENT_LIMIT = 60
GLOBAL_LIMIT = 200


class SocketRateLimiter:
    def __init__(self, window_ms: int = DEFAULT_WINDOW_MS) -> None:
        self.window_ms = window_ms
        self.buckets: dict[str, list[float]] = {}

    def _hit(self, key: str, limit: int, now: float) -> bool:
        bucket = self.buckets.get(key)
        if bucket is None or now - bucket[1] >= self.window_ms:
            bucket = [0, now]
            self.buckets[key] = bucket
        bucket[0] += 1
        return bucket[0] <= limit

    def allow(self, event: Any) -> bool:
        if not event or not isinstance(event, str):
            return True
        now = time.monotonic() * 1000
        per_event = EVENT_LIMITS.get(event, DEFAULT_EVENT_LIMIT)
        within_event = self._hit(f"e:{event}", per_event, now)
        within_global = self._hit("__global", GLOBAL_LIMIT, now)
        return within_event and within_global

    def exceeded_payload(self, event: str) -> dict[str, Any]:
        return {
            "status": False,
            "event": event,
            "message": "Too many requests. Please slow down.",
            "retry_after_ms": self.window_ms,
        }


_limiters: dict[str, SocketRateLimiter] = {}


def for_sid(sid: str) -> SocketRateLimiter:
    limiter = _limiters.get(sid)
    if limiter is None:
        limiter = _limiters[sid] = SocketRateLimiter()
    return limiter


def discard(sid: str) -> None:
    _limiters.pop(sid, None)
