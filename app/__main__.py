"""``python -m app`` — run the server with the same port/keep-alive settings as Node."""

from __future__ import annotations

import uvicorn

from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        timeout_keep_alive=int(settings.http_keep_alive_timeout_ms / 1000),
        timeout_graceful_shutdown=10,
        proxy_headers=False,
        server_header=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
