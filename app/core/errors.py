"""Error types and the central error -> response mapping.

Port of src/config/status.error.config.js + handle.error.config.js, plus the
error shapes produced by Express/celebrate/multer/body-parser that clients
see today:

* ``StatusError(code, message)`` -> ``{success:false, message: __(message)}``
  (``message`` falls back to the localized ``serverError``);
* an object message is spread into the body (``{success:false, ...message}``);
* any other exception -> 500 ``{success:false, message: __("serverError")}``
  (stack traces are never returned);
* multer limit codes -> 400 ``{error: __(key)}``;
* celebrate/Joi -> 400 ``{statusCode, error, message, validation}``.
"""

from __future__ import annotations

from typing import Any


class StatusError(Exception):
    def __init__(self, status_code: int, message: Any = None) -> None:
        super().__init__(message if isinstance(message, str) else "")
        self.status_code = status_code
        self.message = message

    @classmethod
    def bad_request(cls, message: Any = None) -> StatusError:
        return cls(400, message or "badRequest")

    @classmethod
    def unauthorized(cls, message: Any = None) -> StatusError:
        return cls(401, message or "unAuthorizedAccess")

    @classmethod
    def forbidden(cls, message: Any = None) -> StatusError:
        return cls(403, message or "forbidden")

    @classmethod
    def not_found(cls, message: Any = None) -> StatusError:
        return cls(404, message or "notFound")

    @classmethod
    def server_error(cls, message: Any = None) -> StatusError:
        return cls(500, message or "serverError")

    @classmethod
    def bad_gateway(cls, message: Any = None) -> StatusError:
        return cls(502, message or "badGateWay")


class ForbiddenEmptyMessage(StatusError):
    """``StatusError.forbidden("")`` raised by verifyAccessToken.

    ``"" || "forbidden"`` makes the message the ``forbidden`` key, so an
    invalid/expired JWT on HTTP answers 403 ``__("forbidden")``
    ("Access forbidden."). Kept as a distinct type for readability.
    """

    def __init__(self) -> None:
        super().__init__(403, "forbidden")


class UploadError(Exception):
    """multer error codes mapped by handleError to ``{error: __(key)}``."""

    CODES = {
        "LIMIT_FILE_SIZE": "fileSizeNotMatch",
        "LIMIT_FILE_TYPE": "invalidFile",
        "LIMIT_UNEXPECTED_FILE": "maximumFileLimit",
        "STORAGE_EXCEED": "storageExceeded",
    }

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BodyParseError(Exception):
    """body-parser failures (malformed JSON 400, too large 413)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class CelebrateValidationError(Exception):
    def __init__(self, segment: str, keys: list[str], message: str) -> None:
        super().__init__(message)
        self.segment = segment
        self.keys = keys
        self.message = message

    def body(self) -> dict[str, Any]:
        return {
            "statusCode": 400,
            "error": "Bad Request",
            "message": "Validation failed",
            "validation": {self.segment: {"source": self.segment, "keys": self.keys, "message": self.message}},
        }


def error_response_body(exc: BaseException, translate) -> tuple[int, dict[str, Any]]:
    """Return ``(status, body)`` for an exception, mirroring handleError."""
    if isinstance(exc, UploadError) and exc.code in UploadError.CODES:
        return 400, {"error": translate(UploadError.CODES[exc.code])}
    if isinstance(exc, CelebrateValidationError):
        return 400, exc.body()
    if isinstance(exc, StatusError):
        if isinstance(exc.message, dict):
            return exc.status_code, {"success": False, **exc.message}
        message = exc.message or translate("serverError")
        return exc.status_code, {"success": False, "message": translate(message)}
    if isinstance(exc, BodyParseError):
        return exc.status_code, {"success": False, "message": translate(exc.message)}
    return 500, {"success": False, "message": translate("serverError")}
