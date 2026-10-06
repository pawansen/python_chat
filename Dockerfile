# py_chat-chat-python — production image
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

# Dependencies first for layer caching.
COPY pyproject.toml ./
RUN python - <<'PY' > /tmp/requirements.txt
import tomllib
print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))
PY
RUN pip install -r /tmp/requirements.txt

COPY app ./app
COPY public ./public
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

RUN useradd --system --uid 10001 --home /srv/app chat \
    && mkdir -p /srv/app/.lcc-ota-cache \
    && chown -R chat:chat /srv/app
USER chat

ENV PORT=7091
EXPOSE 7091

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"7091\")}/livez', timeout=4); sys.exit(0)" || exit 1

# Uvicorn handles SIGTERM: stops accepting, drains in-flight requests (10s),
# then the lifespan shutdown closes Socket.IO, background queues, Redis and DB.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "app"]
