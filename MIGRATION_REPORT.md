# py_chat-chat: Node.js → Python FastAPI migration report

Source: `../py_chat-chat` (Node.js, read-only, unmodified).
Target: `py_chat-chat-python` (this repository).
Date: 2026-10-05.

**Status: functionally ported and regression-verified against the running Node
service. Not production-ready until the deployment blockers in §13 are
resolved and the flows listed there as unexercised are tested against real
provider sandboxes.**

---

## 1. Source architecture (Node)

* **Runtime:** Node.js, ESM, Express 5, `http.createServer`, PM2 cluster (`ecosystem.config.cjs`).
* **HTTP pipeline (`src/server.js`):** CORS preflight → `cors` (fixed origin list, credentials) → `helmet` → X-Request-Id → `express.json` / `urlencoded` (100 kb) → `hpp` (no-op on Express 5 query) → `express.static('public')` → WhatsApp / LINE webhooks (ack 200 + in-process queue, own 300/min limiter) → `/livez`, `/readyz` → global `express-rate-limit` (draft-6 headers, fail-open) → `x-api-key` gate → `i18n.init` → routers (`/chats`, `/whatsapp`, `/global`) → 404 `{message:"Not found!"}` → `handleError`.
* **Validation:** `celebrate` / Joi per route, 400 envelope.
* **Auth:** HS256 JWT (`ACCESS_TOKEN_SECRET`, optional iss/aud) in `Authorization` (Bearer or raw); DB check of user + session expiry (60 min, activity refresh); role lookup cached; socket handshake auth with Redis JWT blocklist.
* **Data:** MySQL via Sequelize (models + raw queries, `user_data_view`); DynamoDB (aws-sdk v2 `DocumentClient`) for messages + atomic `Counters` table; S3 for media.
* **Cache:** redis@4 — versioned list caches, detail caches, translation cache, invalidation bus subscriber (`py_chat:cache:invalidate`), outage back-off.
* **Realtime:** socket.io v4 (`allowEIO3`), `@socket.io/redis-adapter` (optionally sharded), per-connection per-event rate limiter, content filter, v2 room-key shim.
* **Integrations:** 360dialog WhatsApp (templates, flows, OTP, media), Twilio (WhatsApp / Conversations), LINE Messaging API, Firebase FCM, external translate API (retry, circuit breaker, cache), LCC OTA locale updater.
* **Background work:** in-process translation queue and webhook queue (concurrency, depth cap, retries with back-off, dedupe).
* ~22 k lines of JS under `src/`.

## 2. Target architecture (Python)

* **Runtime:** Python 3.12+ (developed on 3.13), Uvicorn, single ASGI app (`app.main:app`).
* **ASGI layout:** `socketio.ASGIApp` serves `/socket.io`; everything else goes through `NodePipelineMiddleware` (`app/api/pipeline.py`), a pure-ASGI reproduction of the Express middleware chain in the same order, then the FastAPI router (`app/api/routes.py`, a declarative `ROUTES` table).
* **Validation:** Pydantic v2 models with Joi semantics (`app/schemas/joi.py`) producing the byte-identical celebrate 400 envelope.
* **Auth:** PyJWT (HS256, same leeway/claims options), same DB/session/role logic (`app/api/auth.py`, `app/core/security.py`).
* **Data:** SQLAlchemy 2 async + aiomysql, with converters that reproduce Sequelize/mysql2 value types (DATE → string, DECIMAL → string, JSON parsed, naive UTC DATETIME, zero-date → null), `time_zone='+00:00'`; Sequelize `create()` value ordering reproduced (`app/models.build_values`). boto3 DynamoDB with DocumentClient number/set conversion, run on a thread pool.
* **Cache:** redis-py asyncio (RESP2), same keys, TTLs, versioning, back-off.
* **Realtime:** python-socketio `AsyncServer` with a custom `ChatRedisManager` (classic or sharded pub/sub; cross-instance `fetch_room_users` replaces `fetchSockets`).
* **Background:** asyncio-based ports of both queues; `app/core/tasks.py` tracks fire-and-forget tasks and drains them on shutdown.
* **JS semantics helpers** (`app/core/js.py`, `app/core/jsonx.py`): `undefined` sentinel, truthiness, `parseInt`/`Number`, `String()`, `JSON.stringify` (drops undefined, NaN → null, ISO-ms dates) so responses match byte for byte.
* **Ops:** Dockerfile (non-root, HEALTHCHECK `/livez`, SIGTERM graceful drain), docker-compose (chat + MySQL + Redis), Alembic (additive/idempotent only), Ruff, pytest.
* ~13 k lines of Python under `app/`.

## 3. Endpoint mapping

Every Node route is served at the same method + path with the same auth, validator, status codes and body. Python handler = `RouteSpec.service` in `app/api/routes.py`.

| Method | Path | Auth | Validator | Python service |
|---|---|---|---|---|
| POST | /chats/create-conversation | JWT | CreateConversation | `create_conversation_controller` |
| POST | /chats/create-conversation-group | JWT | CreateConversationGroup | `create_conversation_group` |
| POST | /chats/validate-change-user-group-cs-interpreter | JWT | ValidateChangeUserGroupCsInterpreter | `validate_change_user_group_cs_interpreter` |
| POST | /chats/conversation-list | JWT | ConversationList | `conversation_list` |
| POST | /chats/conversation-list-all | JWT | ConversationList | `conversation_list_all` |
| POST | /chats/unread-message-count | JWT | — | `unread_message_count` |
| POST | /chats/conversation-list-app | JWT | ConversationList | `conversation_list_app` |
| POST | /chats/conversation-list-no-replay | JWT | ConversationList | `conversation_list_no_replay` |
| POST | /chats/conversactions | JWT | Conversactions | `conversactions` |
| POST | /chats/get-conversactions-history | JWT | Conversactions | `get_conversactions_history` |
| POST | /chats/message-list | JWT | MessageListAws | `message_list` |
| POST | /chats/message-list-app | JWT | MessageListAws | `message_list_app` |
| POST | /chats/message-list-group | JWT | MessageListAws | `get_message_list_grouped_by_date` |
| POST | /chats/chat-history-list | JWT | ChatHistoryList | `chat_history_list` |
| POST | /chats/read-all-message | JWT | ReadAllMessageAws | `read_all_message` |
| POST | /chats/upload-media | JWT | multipart (1 MB, mime filter) | `send_file` |
| POST | /chats/send-template-message | JWT | SendTemplateMessage | `send_template_message` |
| POST | /chats/send-whatsapp-template-message | JWT | SendWhatsAppTemplateMessage | `send_whatsapp_template_message` |
| POST | /chats/whatsapp-template-notification | api-key only | SendWhatsAppTemplateNotification | `send_whatsapp_template_notification` |
| POST | /chats/send-template-message-whatsapp | api-key only | SendWhatsAppTemplateMessage | `send_whatsapp_template_message` |
| POST | /chats/send-template-message-assign-partner | JWT | SendTemplateMessageAssignPartner | `send_template_message_assign_partner` |
| GET | /chats/get-template-list-whatsapp | api-key only | — | `get_template_list` |
| POST | /chats/send-template-message-action-required-passport | api-key only | SendTemplateMessageActionRequired | `send_template_message_action_required` |
| POST | /chats/send-chat-message | JWT | SendChatMessage | `send_chat_message` |
| POST | /chats/add-action-required | JWT | AddActionRequired | `add_action_required` |
| GET | /chats/get-action-list | JWT | GetActionList (query) | `get_action_list` |
| POST | /chats/send-message-whatsapp | JWT | — | `send_message_whatsapp` |
| POST | /chats/send-otp-whatsapp | JWT | SendOTPWhatsapp | `send_otp_whatsapp` |
| POST | /chats/send-otp-whatsapp-direct | JWT | SendOTPWhatsappDirect | `send_otp_whatsapp_direct` |
| POST | /chats/send-welcome-message-whatsapp | api-key only | SendWelcomeMessageWhatsapp | `send_welcome_message_whatsapp` |
| POST | /chats/send-auto-message-whatsapp | JWT | — | `send_auto_message_whatsapp` |
| GET | /chats/getAccessToken | JWT | — | `get_access_token` |
| POST | /chats/addRemoveGroupParticipant | JWT | AddRemoveGroupParticipant | `add_remove_group_participant` |
| POST | /chats/fetchConversation | JWT | FetchConversation | `fetch_conversation` |
| POST | /chats/getMessageById | JWT | GetMessageById | `get_message_by_id` |
| POST | /chats/allMessage | JWT | AllMessage | `all_message` |
| PUT | /chats/updateReadMessageCount | JWT | UpdateReadMessageCount | `update_read_message_count` |
| POST | /chats/filterListConversation | JWT | — | `filter_list_conversation` |
| POST | /chats/fetchAllParticipant | JWT | FetchAllParticipant | `fetch_all_participant` |
| POST | /chats/getParticipantById | JWT | GetParticipantById | `get_participant_by_id` |
| POST | /chats/createWhatsappGroup | JWT | CreateWhatsappGroup | `create_whatsapp_group` |
| POST | /chats/sendMessageText | JWT | SendMessageText | `send_message_text` |
| POST | /whatsapp/send-message | api-key only | SendWhatsAppMessage | `send_whatsapp_message` |
| POST | /whatsapp/send-otp | JWT | SendOTPWhatsapp | `send_otp_whatsapp` |
| POST | /whatsapp/send-otp-direct | JWT | SendOTPWhatsappDirect | `send_otp_whatsapp_direct` |
| POST | /whatsapp/send-welcome-message | JWT | SendWhatsAppWelcomeMessage | `send_whatsapp_welcome_message` |
| POST | /whatsapp/validate-whatsapp | JWT | ValidateMobile | `validate_mobile` |
| GET | /global/get-duplicate-employers-count | JWT | GetDuplicateCount | `get_duplicate_employers_count` |
| GET | /global/get-duplicate-workers-count | JWT | GetDuplicateCount | `get_duplicate_workers_count` |
| GET | /global/get-duplicate-employers | JWT | GetDuplicateEmployers | `get_duplicate_employers` |
| GET | /global/get-duplicate-workers | JWT | GetDuplicateWorkers | `get_duplicate_workers` |
| GET | /global/get-duplicate-jobs-count | JWT | GetDuplicateJobsCount | `get_duplicate_jobs_count` |
| GET | /global/get-duplicate-jobs | JWT | GetDuplicateJobs | `get_duplicate_jobs` |
| GET | /global/get-worker-previous-images | JWT | GetWorkerPreviousImages | `get_worker_previous_images` |
| POST | /global/save-language-translate | JWT | SaveLanguageTranslate | `save_language_translate` |
| GET | /global/get-language-translate | JWT | — | `get_language_translate` |
| POST | /chats/webhook | none (pre-gate) | — | pipeline → webhook queue → `handle_webhook_whatsapp` |
| POST | /chats/line-hook | none (pre-gate) | — | pipeline → webhook queue → LINE handler |
| GET | /livez, /readyz | none | — | pipeline |
| any | static `public/*` | none | — | pipeline static handler |
| any | unmatched | — | — | 404 `{"message":"Not found!"}` |

`src/assets/docs/swagger.json` exists in Node but no route serves it; it is copied to `app/assets/docs/` and likewise not served.

"api-key only" = no JWT, but the global `x-api-key` gate applies to every route after the webhooks/health checks, as in Node.

## 4. Socket event mapping

Path `/socket.io`, transports polling + websocket, ping 25 s / 20 s, `maxHttpBufferSize` from config, same CORS list. Connection auth: handshake token (`auth.token`, query, or header), JWT verify + Redis blocklist (`jwt:blocklist:` prefix), enforced when `SOCKET_HANDSHAKE_AUTH_ENFORCE=true`, as in Node.

| Client → server | Python handler (`app/socket/handlers.py`) | Server emits |
|---|---|---|
| `join_branch_room` | `ChatSocketService.join_branch_room` | `join_branch_room` ack |
| `join_room_user` | `join_room_user` | `join_room_user`, `message_delivered` (delivery sync) |
| `branch_checkin` | `branch_checkin` | `branch_checkin` / `check_in_all` to branch room |
| `join_room` | `join_room` | `join_room`, `chat_message_read` |
| `send_message` | `send_message` (content filter, participants, DynamoDB write, unread counters, WhatsApp/LINE/push fan-out, translation enqueue) | `receive_message`, `send_message`, `receive_message_status`, `check_message_event` |
| `chat_message_read` | `chat_message_read` | `chat_message_read` |
| `delete_message` | `delete_message` | `delete_message` |
| (any, over limit) | rate limiter | `rate_limit_exceeded` |
| (background) | translation worker | `message_translation_updated` |
| (background) | webhook worker (WhatsApp/LINE inbound, status) | `receive_message`, `receive_message_status` |

Room names, payload shapes and key order are identical (v2-style string rooms included). Cross-instance delivery goes through `ChatRedisManager` (see §13 — not wire-compatible with Node instances).

## 5. Database mapping (MySQL)

* Tables used (shared py_chat schema, not owned): models for `groups`, `group_participants`, `users`, `roles`, `employers`, `workers`, `staffs`, `messages`, `messages_log`, `action_logs`; raw SQL also reads `contracts`, `contract_invoices_statuses`, `interview_bookings`, `jobs`, `job_stages`, `job_stages_logs`, `job_views`, `template_categories`, `user_devices`, `user_images`, `worker_interpreter_assignment_log` and the `user_data_view` view. Columns, keys and indexes are declared in `app/models/__init__.py` mirroring the Sequelize models.
* Queries: every Sequelize model call and raw SQL statement was ported 1:1 (`app/repositories/*`, `app/db/raw.py`). Named replacements keep Sequelize semantics, including the error text `Named replacement ":x" has no entry in the replacement map.` and `IN (:list)` expansion.
* Transactions: same boundaries (`raw.transaction`).
* Pool: `POOL_MAX/MIN/ACQUIRE/IDLE` honoured.
* Schema changes: none required. Node's two SQL migrations are ported as Alembic `0002`/`0003` (additive, idempotent, no-op downgrade); `0001` is a no-op baseline. Verified on the test DB: `upgrade head` twice → `0003_groups_job_worker_id (head)`, no error.
* `scripts/backfill-unread-counts.js` → `scripts/backfill_unread_counts.py` (same SQL, `DRY_RUN`, concurrency). Not run against any real data.

## 6. Redis mapping

| Purpose | Key / channel | Python module |
|---|---|---|
| Conversation list (user / all / app) | `chat:conversation:list:{user\|all\|app}:{userId}:v{ver}:{digest}` | `app/redis/cache_keys.py`, `cache_service.py` |
| Conversation version | `chat:version:conversation:{userId}` | same |
| Message list + version | `chat:message:list:{groupId}:v{ver}:{digest}`, `chat:version:message:{groupId}` | same |
| User / group / participant detail & list | `chat:user:detail:*`, `chat:group:detail:*` (+ `:filter:`), `chat:participant:detail:*`, `chat:participant:list:*` | `repositories/*` |
| Translation cache | `chat:translation:{sha1(text)}:{lang}` | `app/redis/translation_cache.py` |
| Socket JWT blocklist | `jwt:blocklist:{jti/token}` (prefix configurable) | `app/core/security.py` |
| Cache invalidation bus | channel `py_chat:cache:invalidate` | `app/redis/invalidation_bus.py` |
| Socket.IO backplane | adapter channels | `app/socket/manager.py` (**different wire format**, §13) |

Same TTLs (`CACHE_TTL_*`), same digest hashing, same outage back-off (cache misses, never errors). Cached values are JSON written with `JSON.stringify` semantics, so Node and Python can read each other's cache entries during a cut-over (verified by the parity fixtures).

## 7. DynamoDB mapping

* Messages table (`AWS_DYNAMODB_*` / config table name) and `Counters` table for atomic id allocation (`UpdateItem ADD`), as in `src/config/dynamodb.js`.
* All repository functions in `src/repositories/aws.message.repository.js` ported to `app/repositories/aws_message.py`: create (single / multi-language), translation update, unread counts (per group, per receiver, WhatsApp receiver, all), delivered marking (batched conditional updates, capped queries), read-status updates (by id / by WhatsApp id), delete, filtered/sorted/paginated listing, combined history, last message, lookups by waid / receiver.
* Same GSIs, key conditions, filter expressions, `Limit`/pagination behaviour and caps (`DELIVERED_SYNC_*`, `READ_SYNC_*`, `HISTORY_MAX_GROUPS`, `RECEIVER_LOOKUP_*`).
* Value conversion matches DocumentClient (numbers ↔ `Decimal`, NaN rejected like aws-sdk). boto3 calls run in a thread pool to keep the event loop free.
* Verified against moto (`chat_messages_regress`, `Counters_regress`), not against AWS.

## 8. Authentication mapping

| Node | Python |
|---|---|
| `jsonwebtoken.verify` HS256, `clockTolerance` 10 s, optional `issuer`/`audience` | PyJWT `decode`, `leeway=10`, same optional iss/aud, iat/sub/jti not required |
| HTTP token from `Authorization` (Bearer or raw) | same |
| Invalid/expired token on HTTP → 403 `Access forbidden.` (localised) | same |
| User lookup + 60-minute inactivity expiry + activity refresh | same (`app/api/auth.py`) |
| Role lookup with in-process cache | same TTL cache |
| `x-api-key` gate, sha256 + `timingSafeEqual`, `API_KEYS` rotation, 400 on failure | sha256 + `hmac.compare_digest`, same rotation and response |
| Socket handshake auth + Redis blocklist + auth cache TTL | same (`authenticate_socket_connection`) |

## 9. External API mapping

| Provider | Node | Python | Verified how |
|---|---|---|---|
| 360dialog WhatsApp | axios, `WHATSAPP_API_URL` + `D360-API-KEY` | httpx shared client, same URLs/headers/bodies | mock provider: request bodies and headers compared with Node's outbound calls |
| Translate API | axios + retry + breaker + cache | httpx, same retry/back-off/breaker/cache | mock provider, compared |
| Twilio (WhatsApp / Conversations / tokens) | `twilio` SDK | `twilio` Python SDK, same calls | unit tests only, fake SID — **no real call** |
| LINE Messaging API | REST via axios | httpx, same endpoints | unit tests only — **no real call** |
| Firebase FCM | `firebase-admin` | `firebase_admin` (same data-value rules) | unit tests with dummy service account — **no real send** |
| AWS S3 (uploads) | aws-sdk v2 `upload` | boto3 `put_object`, same key naming / ACL / URL | moto only |
| LCC OTA | fetch + file writes | httpx + same files | unit tests; disabled by default as in Node |

## 10. Test results

| Suite | Result |
|---|---|
| Unit + integration (`pytest tests --ignore=tests/e2e`) | **1819 passed**, 0 failed (45 s). Includes the 1713-case validation corpus run through Node's real celebrate/Joi schemas, helper-parity fixtures generated from the Node source, and repository tests against MySQL + moto + Redis. |
| Node vs Python regression (`python -m tests.e2e.regression_compare`) | **115 / 115 scenarios identical** (138 s): HTTP status, headers, bodies, Socket.IO emits and outbound provider calls (360dialog, translate) compared after normalising ids/timestamps. Report: `tests/e2e/regression_report.json`. |
| Alembic | `upgrade head` applied twice on the test DB without error (idempotent). |
| Ruff | `ruff check .` — all checks passed. |

Caveats: the previous run was 113/115 — one was a mock-recorder header-order artefact (fixed in the harness), the other is the `message-list` delivered-receipt race (D5), which happens to match in this run but is non-deterministic in both implementations. Twilio, LINE, FCM and S3 are covered by unit tests only. No load test was run. The Docker image was **not built** here (Docker is not installed on this machine).

## 11. Security findings

Behaviour that could break clients was preserved and is reported here, per the migration rules. Items marked **(Node)** exist in the original service and are reproduced unless stated.

| # | Severity | Finding | Python behaviour |
|---|---|---|---|
| S1 | High (Node) | `branch_checkin` failure path throws a `ReferenceError` inside a `.then` with no `unhandledRejection` handler — any client can crash a Node worker (DoS). | Not reproducible as a crash: the exception is caught and logged per event. The only intentional behavioural difference. |
| S2 | High (Node) | WhatsApp (`/chats/webhook`) and LINE (`/chats/line-hook`) signature verification is commented out; anyone can post forged inbound messages/status updates. | Preserved (no verification). `WHATSAPP_WEBHOOK_SECRET` / `LINE_CHANNEL_SECRET` are read but unused, as in Node. Recommend enabling both together on Node and Python. |
| S3 | High (Node) | `setWhatsUpWebhook` hard-codes a sandbox API key (`N3WAmg_sandbox`). | Preserved literally to keep behaviour; must be moved to config/secret store. |
| S4 | Medium (Node) | Socket `send_message` participant check ignores `is_deleted`: removed participants can still send to and receive from a group. | Preserved. |
| S5 | Medium (Node) | `branch_checkin` echoes the full payload (including the token) to the branch room; `delete_message` failure echoes `data` (including token) back. | Preserved. |
| S6 | Medium (Node) | Webhook payload `entry` objects are broadcast to rooms in socket emits (provider metadata leakage to clients). | Preserved. |
| S7 | Low (Node) | `/readyz` returns the raw DB error message on 503. | Preserved. |
| S8 | Low (Node) | Malformed JSON body → Express final-handler 500 HTML (because `handleError` calls `req.__` before i18n init). | Preserved byte-for-byte (incl. `Content-Security-Policy: default-src 'none'`). |
| S9 | Info | No secrets in the repo: `.env.example` is placeholders only; tests use a dummy Firebase SA, fake Twilio SID and a local short JWT secret (the `InsecureKeyLengthWarning` in test output is from that test secret). Logs redact passwords, tokens, secrets and keys (`app/core/logging.py`). | — |
| S10 | Info | FastAPI `/docs` and `/openapi.json` are disabled unless `ENABLE_API_DOCS=true` (Node only exposes its own swagger). | — |
| S11 | Info | Docker image runs as non-root (uid 10001); `.dockerignore` excludes `.env`. | — |

## 12. Performance findings

* **Event loop:** all MySQL/Redis/HTTP I/O is async; boto3 (sync) and file I/O run in the default thread pool. Under heavy DynamoDB load, size the pool (or run more Uvicorn workers) — boto3 is the likeliest bottleneck.
* **Concurrency limits preserved:** `CHAT_LIST_DYNAMO_CONCURRENCY`, queue concurrency/depth caps, webhook fan-out concurrency, delivered/read sync caps.
* **Caching identical to Node:** same keys/TTLs/versioning, so hit ratios carry over and caches stay warm across a cut-over.
* **Scaling:** Node scales with PM2 cluster + Redis adapter. Python: run several Uvicorn processes/containers with `REDIS_SOCKET_ADAPTER_ENABLED=true`; Socket.IO polling needs sticky sessions at the load balancer (same requirement as Node).
* **Not done:** no load test was run for either implementation (`loadtest.yml` in the Node repo was not executed). Latency/throughput parity is unmeasured.

## 13. Missing / incomplete functionality and deployment blockers

**Blockers**

1. **socket.io-client v2 (EIO3) not supported.** Node enables `allowEIO3`; python-socketio only speaks EIO4. Any client still on socket.io v2 cannot connect. Confirm client versions (mobile apps, admin panel) before cut-over.
2. **Mixed Node/Python fleet not possible.** The Python Redis backplane is not wire-compatible with `@socket.io/redis-adapter`; messages between a Node and a Python instance are not delivered. Cut over all instances at once.

**Ported but not exercised end-to-end** (unit-tested only; need real sandbox runs before release):

* LINE send / receive (note: Node's LINE image send is a no-op — `sendLineImageMessageSDK` takes no parameters; preserved).
* 360dialog sandbox webhook setup, contacts check, media download.
* Twilio WhatsApp / Conversations / access tokens.
* FCM real sends.
* S3 real uploads (moto only).
* LCC OTA against a live LCC server.

**Not ported:** nothing in `src/` is knowingly missing. Node-only ops files (`ecosystem.config.cjs`, `appspec.yml`, `aws/`, `yml_scripts/`, `loadtest.yml`) are replaced by Docker / compose and are not ported.

## 14. Node vs FastAPI differences

Intentional or unavoidable:

| # | Difference | Impact |
|---|---|---|
| D1 | EIO3 clients rejected (§13.1). | Old socket clients cannot connect. |
| D2 | Redis adapter wire format (§13.2). | No mixed fleet. |
| D3 | `branch_checkin` error no longer crashes the process (S1). | Strictly safer. |
| D4 | `createConversation` with platform `wechat`: Node never responds (request hangs until the client gives up). Python waits for the client to disconnect, then times out with 504. | Same from a client's view in practice (no useful response). |
| D5 | `message-list` delivered-receipt update is fire-and-forget in both; whether the response sees the updated status is a race in both implementations. | Non-deterministic in Node too. |
| D6 | Python-only env vars: `AWS_DYNAMODB_ENDPOINT_URL`, `AWS_S3_ENDPOINT_URL`, `ALLOWED_ORIGINS`, `ENABLE_API_DOCS`. | Optional; defaults reproduce Node. |

Node bugs reproduced on purpose (fixing them would change client-visible behaviour; each is a candidate for a coordinated fix):

* Content-filter file is double-encoded in Node, so zh-HK / zh-CN refusal messages and some regex alternatives are mojibake — generated byte-identical (`scripts/gen_content_filter.py`).
* FCM pushes with numeric data values always fail (firebase rejects non-string data).
* `sendChatMessage` (WhatsApp) returns `entity_id is not defined` after sending; app branch returns 500 `content is not defined` when tokens exist.
* `checkValidSid` selects a nonexistent `messaging_service_sid` column → returns an error object that is truthy.
* `filterListConversation.totalNotReply` is always a Sequelize error object.
* Duplicate-check endpoints' catch blocks throw `ReferenceError` → 500.
* `/` and `/connect` are unreachable (registered after the 404 handler).
* `getTemplateCategoryContent` compares `reason === 2` (string vs number).
* `Accept-Language: zh-HK` does not select `zh_HK`; `?lang=` is lower-cased.

## 15. Final migration status

**Ported — not yet cleared for production.**

* All HTTP endpoints, Socket.IO events, background queues, cache behaviour, DynamoDB and MySQL access from `src/` have been ported and inspected against the Node source; the side-by-side regression is 115/115 identical.
* Before release:
  1. Confirm no client uses socket.io v2 (EIO3) — §13.1.
  2. Plan an all-at-once fleet cut-over (no mixed Node/Python Redis adapter) — §13.2.
  3. Run the unexercised provider flows (LINE, Twilio, FCM, 360dialog sandbox/media, S3, LCC OTA) against real sandboxes.
  4. Build and smoke-test the Docker image in CI.
  5. Load-test both implementations with the same traffic profile.
  6. Decide with product/security on S2 (webhook signatures), S3 (hard-coded sandbox key), S4 (removed participants), S5/S6 (payload echo) — fixing them changes behaviour and should land in Node and Python together.
* The Node project at `../py_chat-chat` was not modified.
