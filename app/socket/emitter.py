"""Room-targeted emit facade (the ``io`` / ``req.io`` handle of the Node code).

Every Node emit site went through ``io.to(room).emit(event, data)`` where
``io`` was the default namespace; the v2-compat shim coerced numeric room ids
to strings. This facade does the same: rooms are normalised with
:func:`app.core.js.room` (``12`` -> ``"12"``, ``NaN`` -> ``"NaN"``) and the
emit goes through the python-socketio manager, so it fans out across
instances via the Redis backplane.
"""

from __future__ import annotations

from typing import Any

from app.core.js import js_string, nullish

_sio = None


def bind(sio) -> None:
    global _sio
    _sio = sio


def get_server():
    return _sio


def _room_key(value: Any) -> str:
    return js_string(value)


class Emitter:
    """Async equivalent of ``io.to(a).to(b).emit(event, payload)``."""

    async def emit_to(self, rooms: Any, event: str, data: Any) -> None:
        if _sio is None:
            return
        if isinstance(rooms, list | tuple):
            targets = [_room_key(r) for r in rooms]
            if not targets:
                return
            await _sio.emit(event, data, to=targets)
        else:
            await _sio.emit(event, data, to=_room_key(rooms))

    async def fetch_room_users(self, room: Any) -> list[dict[str, Any]]:
        """``io.in(room).fetchSockets()`` -> ``socket.data.user`` of every socket, across instances."""
        if _sio is None:
            return []
        manager = _sio.manager
        if hasattr(manager, "fetch_room_users"):
            return await manager.fetch_room_users("/", _room_key(room))
        users = []
        for sid, _eio_sid in manager.get_participants("/", _room_key(room)):
            try:
                session = await _sio.get_session(sid)
            except KeyError:
                continue
            users.append({"sid": sid, "user": session.get("user")})
        return users

    async def room_has_sockets(self, room: Any) -> bool:
        return len(await self.fetch_room_users(room)) > 0


io = Emitter()


def is_room(value: Any) -> bool:
    return not nullish(value)
