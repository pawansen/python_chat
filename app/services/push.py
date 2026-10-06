"""FCM push notifications (port of src/utils/pushNotification.util.js).

Credentials: FIREBASE_SERVICE_ACCOUNT_BASE64 (preferred), else
FCM_SERVICE_ACCOUNT_PATH, else ``./fcmServiceAccountKey.json`` (git-ignored).
The payload is identical (notification + high-priority android with sound /
channel "default" + APNs sound and priority 10). A single token uses
``send``; several use ``send_each_for_multicast`` in batches of 500 with at
most 5 batches in flight. Errors are logged, never raised.

Difference from Node (documented): Node crashed at boot when no credentials
were found; here push is disabled with an error log and the service runs.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR, get_settings
from app.core.logging import logger

FCM_MULTICAST_LIMIT = 500
FCM_MAX_CONCURRENT_BATCHES = 5

_app = None
_initialised = False


def _load_service_account() -> dict[str, Any] | None:
    settings = get_settings()
    if settings.firebase_service_account_base64:
        return json.loads(base64.b64decode(settings.firebase_service_account_base64).decode("utf-8"))
    path = Path(settings.firebase_service_account_path) if settings.firebase_service_account_path else BASE_DIR / "fcmServiceAccountKey.json"
    return json.loads(path.read_text(encoding="utf-8"))


def init_firebase() -> None:
    global _app, _initialised
    if _initialised:
        return
    _initialised = True
    try:
        import firebase_admin
        from firebase_admin import credentials

        account = _load_service_account()
        _app = firebase_admin.initialize_app(credentials.Certificate(account), name="py_chat-chat")
    except Exception as error:  # noqa: BLE001
        logger.error("Firebase initialisation failed — push notifications disabled", {"error": error.__class__.__name__})
        _app = None


def _build_message_kwargs(title: Any, body: Any, data: dict[str, Any]) -> dict[str, Any]:
    from firebase_admin import messaging

    # firebase-admin (JS and Python) rejects non-string data values. Several Node
    # call sites pass numeric ids, so those pushes always failed with this error
    # and were only logged. Preserved on purpose (see MIGRATION_REPORT.md §11).
    for value in (data or {}).values():
        if not isinstance(value, str):
            raise ValueError("data must only contain string values")
    return {
        "notification": messaging.Notification(title=title, body=body),
        "data": dict(data or {}),
        "android": messaging.AndroidConfig(
            priority="high", notification=messaging.AndroidNotification(sound="default", channel_id="default")
        ),
        "apns": messaging.APNSConfig(
            payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")), headers={"apns-priority": "10"}
        ),
    }


async def send_fcm_notification(tokens: Any, title: Any, body: Any, data: dict[str, Any] | None = None, priority: str = "high") -> None:
    token_list = [t for t in (tokens if isinstance(tokens, list) else [tokens]) if t]
    if not token_list:
        return
    init_firebase()
    if _app is None:
        return
    from firebase_admin import messaging

    if len(token_list) == 1:
        try:
            message = messaging.Message(token=token_list[0], **_build_message_kwargs(title, body, data or {}))
            await asyncio.to_thread(messaging.send, message, False, _app)
        except Exception as error:  # noqa: BLE001
            logger.error("Error sending message", {"error": error.__class__.__name__})
        return

    batches = [token_list[i : i + FCM_MULTICAST_LIMIT] for i in range(0, len(token_list), FCM_MULTICAST_LIMIT)]
    semaphore = asyncio.Semaphore(FCM_MAX_CONCURRENT_BATCHES)

    async def send_batch(batch: list[str]) -> None:
        async with semaphore:
            try:
                message = messaging.MulticastMessage(tokens=batch, **_build_message_kwargs(title, body, data or {}))
                await asyncio.to_thread(messaging.send_each_for_multicast, message, False, _app)
            except Exception as error:  # noqa: BLE001
                logger.error("Error sending multicast message", {"error": error.__class__.__name__})

    await asyncio.gather(*(send_batch(b) for b in batches))
