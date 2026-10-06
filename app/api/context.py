"""Request context handed to the services (stands in for Express's ``req``).

The Node services read and *mutate* ``req.body`` / ``req.userDetails``
(e.g. sendTemplateMessage swaps ``req.userDetails`` for the sender's view
row before calling createConversation), so this is a plain mutable object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.i18n import Translator
from app.socket.emitter import Emitter, io


@dataclass
class RequestContext:
    body: Any = None
    query: dict[str, Any] = field(default_factory=dict)
    user_details: Any = None
    translator: Translator = field(default_factory=Translator)
    io: Emitter = io
    file: Any = None

    def __(self, key: Any) -> Any:  # noqa: N802 - mirrors req.__
        return self.translator(key)

    t = __

    def get_locale(self) -> str:
        return self.translator.get_locale()
