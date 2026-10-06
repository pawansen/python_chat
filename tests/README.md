# Tests

| Suite | Command | Needs |
|---|---|---|
| Unit | `pytest tests/unit` | nothing external |
| Integration | `pytest tests/integration` | MySQL 127.0.0.1:3307, Redis 127.0.0.1:6379, moto on 127.0.0.1:5005 |
| Regression (Node vs Python) | `python -m tests.e2e.regression_compare` | the above + Node 20+, `../maidmatch-chat` with `node_modules` |

Start the DynamoDB/S3 emulator first:

```bash
moto_server -H 127.0.0.1 -p 5005
```

Settings live in `tests/harness/env.py`. Isolation guarantees:

* MySQL: only the `py_chat_regress` database is dropped/created
  (tables copied with `CREATE TABLE ... LIKE` from the schema source, then
  synthetic fixtures). No other database is written.
* DynamoDB: `chat_messages_regress` / `Counters_regress` on moto only.
* Redis: DB 13 (Node) and DB 14 (Python) are flushed.
* Node runs from `tests/harness/.run/node` (empty dir, so the real `.env` is
  never loaded) with LCC OTA disabled; its source tree is only read.
* Outbound providers (360dialog, translate) hit the local mock on port 9099;
  calls are recorded and compared.

The regression driver starts both servers, replays the same HTTP and Socket.IO
scenarios against each (reseeding between runs), normalises volatile values
(ids, timestamps, request ids) and writes `tests/e2e/regression_report.json`.

Fixtures:

* `tests/fixtures/build_validation_corpus.py` — validation corpus run through
  Node's real celebrate/Joi schemas; `test_node_parity.py` asserts the Python
  validators produce identical envelopes.
* `tests/fixtures/generate_node_fixtures.mjs` — helper outputs from the Node
  source (placeholders, content filter, cache keys...) → `node_parity.json`.
