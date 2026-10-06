# py_chat-chat-python

Python / FastAPI port of the py_chat chat service (`../py_chat-chat`, Node.js).
It is a **drop-in replacement**: same URLs, methods, request bodies, JSON responses,
status codes, headers, auth, validation envelopes, Socket.IO events, MySQL /
Redis / DynamoDB access and error behaviour. Node quirks are preserved on
purpose and listed in [MIGRATION_REPORT.md](MIGRATION_REPORT.md) — read it
before deploying.

The Node project is the read-only source; nothing in it was modified.

## Stack

Python 3.12+, FastAPI, Pydantic v2 / pydantic-settings, SQLAlchemy 2 (async,
aiomysql), Alembic, redis-py, boto3 (DynamoDB / S3), httpx, python-socketio
(ASGI), firebase-admin, twilio, Uvicorn. Tests: pytest, pytest-asyncio, moto,
fakeredis. Lint: Ruff.

## Layout

```
app/
  main.py            ASGI app: Socket.IO at /socket.io, else Node-order HTTP pipeline + FastAPI router
  __main__.py        `python -m app` (uvicorn, Node port/keep-alive, graceful shutdown)
  api/               pipeline (CORS, helmet, body parsing, rate limit, api-key gate), auth, route table
  core/              config, JS-semantics helpers, JSON.stringify parity, i18n, JWT, logging, http client
  schemas/           Joi/celebrate-compatible validators
  db/ models/        SQLAlchemy engine (Sequelize-compatible type conversion), table definitions
  repositories/      SQL + DynamoDB data access (same queries, same cache keys)
  redis/             cache client, cache keys, translation cache, invalidation bus
  dynamodb/          DocumentClient-compatible wrapper
  services/          chat, WhatsApp (360dialog / Twilio), LINE, FCM push, translate, uploads
  socket/            Socket.IO server, handlers, Redis manager, rate limiter, content filter
  queues/ workers/   in-process translation & webhook queues
  lcc_ota/           localisation OTA updater (disabled unless LCC_OTA_ENABLED=true)
  assets/            locale catalogs, swagger.json
alembic/             additive, idempotent schema changes (never run automatically)
scripts/             backfill_unread_counts.py, content-filter generator
tests/               unit, integration (MySQL + moto + Redis), e2e Node-vs-Python regression
```

## Configuration

Copy `.env.example` to `.env` and fill in real values (never commit `.env`).
Variable names and defaults are identical to the Node service
(`src/config/envs.config.js`). Python-only additions:

| Variable | Purpose |
|---|---|
| `AWS_DYNAMODB_ENDPOINT_URL` | DynamoDB endpoint override (local emulator / VPC endpoint) |
| `AWS_S3_ENDPOINT_URL` | S3 endpoint override (local emulator) |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins; default is Node's hard-coded list |
| `ENABLE_API_DOCS` | `true` serves FastAPI `/docs`; off by default |

## Run

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m app                                     # listens on PORT (default 7091)
```

Health: `GET /livez` (process up), `GET /readyz` (MySQL reachable — same check as Node).

### Docker

```bash
docker build -t py_chat-chat-python .
docker compose up --build
```

The image runs as a non-root user, has a `HEALTHCHECK` on `/livez`, and stops
gracefully on SIGTERM (Uvicorn drains requests; the lifespan hook closes
Socket.IO, the background queues, Redis and the DB pool). The compose MySQL is
an empty local database — the chat service does not own the py_chat schema,
so point `DB_*` at a database that has it.

## Database migrations

The tables belong to the shared py_chat database. `alembic/` contains only
the chat service's own additive changes (ported from Node `migrations/*.sql`),
each idempotent with a no-op downgrade. **Nothing runs on start-up.**

```bash
alembic upgrade head    # review first; safe to re-run
alembic stamp head      # on a DB where the Node SQL migrations were already applied
```

One-off unread-count backfill (port of `scripts/backfill-unread-counts.js`):

```bash
DRY_RUN=true python -m scripts.backfill_unread_counts
python -m scripts.backfill_unread_counts
```

## Tests

```bash
pytest tests --ignore=tests/e2e              # unit + integration
python -m tests.e2e.regression_compare       # Node vs Python side-by-side
ruff check .
```

Integration and regression tests need local MySQL (127.0.0.1:3307), Redis and
a moto server on 127.0.0.1:5005 (`moto_server -H 127.0.0.1 -p 5005`); see
[tests/README.md](tests/README.md). They only create/drop the
`py_chat_chat_regress` database and use Redis DBs 13/14.

## Deployment notes

* **Do not mix Node and Python instances behind one Socket.IO Redis adapter.**
  The Python backplane wire format is not compatible with
  `@socket.io/redis-adapter`; cut the whole fleet over at once.
* **socket.io-client v2 (EIO3) is not supported** by python-socketio. Node
  accepts it (`allowEIO3`). Confirm every client is on socket.io v3+ first.
* Run `alembic` and the backfill script from one instance only.
