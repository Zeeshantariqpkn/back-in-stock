# Back-in-Stock Alerts

Customers ask to be emailed when a sold-out product returns. They get **one** email when it does: not zero, not two.

- `GET /embed`: the sign-up form, built to sit in an iframe on any store (SKU prefill with `?sku=`)
- `POST /api/requests`: `{ "email", "sku" }` → `201 created` or `200 already_subscribed`
- `POST /webhooks/stock`: stock events, authenticated with `X-Webhook-Secret`
- `GET /admin`: every request, its status and when it was notified (HTTP Basic auth, any username + `ADMIN_PASSWORD`)
- `GET /health`

Stack: Python 3.12, FastAPI, SQLite (WAL), httpx. One container, no other services.

## Run it

```bash
docker build -t back-in-stock .
docker run -p 8000:8000 \
  -e PORT=8000 \
  -e STOCK_API_URL=... -e STOCK_API_KEY=... \
  -e RESEND_API_KEY=... -e RESEND_BASE_URL=... \
  -e WEBHOOK_SECRET=... -e ADMIN_PASSWORD=... \
  back-in-stock
```

Add `-v bis-data:/data` to keep the database when the container is *replaced*; a plain restart keeps it anyway.

Without Docker: `pip install -r requirements.txt`, set the env vars (see `.env.example`), then `uvicorn app.main:app --port 8000`.

### Environment

| Variable | Required | Purpose |
|---|---|---|
| `STOCK_API_URL` | yes | `GET {url}/{sku}` for live stock, `GET {url}` for the catalogue |
| `STOCK_API_KEY` | yes | Bearer token for the stock API |
| `RESEND_API_KEY` | yes | Bearer token for the email API |
| `RESEND_BASE_URL` | yes | Email API base; we `POST {base}/emails` in Resend format |
| `WEBHOOK_SECRET` | yes | Must match the `X-Webhook-Secret` header |
| `ADMIN_PASSWORD` | yes | Protects `/admin` |
| `PORT` | yes | Port to listen on (default 8000) |
| `DB_PATH` | no | SQLite file (default `/data/app.db` in Docker) |
| `EMAIL_FROM`, `STORE_NAME`, `PRODUCT_URL_TEMPLATE` | no | Email branding and the "Shop now" link (`{sku}` placeholder) |
| `RECONCILE_INTERVAL` | no | Seconds between safety-net stock checks (default 20) |

## How customers never get a duplicate or a missed email

**Don't trust the webhook**
- The secret is checked with a constant-time comparison; anything else gets `401`. Malformed bodies get `400` and never crash the handler.
- The webhook's `quantity` is treated as a hint only. Before any email goes out, the app asks the stock API itself. A webhook that says "back" while the stock API says 0 sends nothing (the admin page shows both numbers side by side).

**Retries and simultaneous duplicates**
- Every `event_id` is inserted into a table where it is the primary key. A retry, or the same event arriving twice at the same instant, loses that insert and is acknowledged with `200 duplicate` and nothing else happens. This is decided by SQLite, so it holds across threads and restarts.
- The webhook responds immediately and does the work in the background, so a slow email API never causes the sender to retry.

**Exactly one email per request**
- Duplicate sign-ups: a partial unique index allows only one open request per `(email, sku)`, so 20 simultaneous identical submissions create one row.
- One sender: before emailing, a worker must atomically move the request `open → sending` (`UPDATE … WHERE status='open'`). Only one worker can ever win that update.
- Each email is sent with `Idempotency-Key: back-in-stock-request-{id}`. If the network times out after the email was accepted, or the process dies between sending and recording it, the retry reuses the same key and the email API treats it as the same email.

**Never zero**
- Failed sends (HTTP 5xx, timeouts) retry with exponential backoff; the request goes back to `open` with the error shown in the admin, and is picked up again later.
- A background reconciler checks the stock API every 20 seconds for every SKU that still has people waiting. Lost or late webhooks can't leave anyone waiting.
- If someone signs up for a product that is already in stock, they are emailed right away.
- All outgoing HTTP calls have timeouts and retries with jitter. If the stock API is unreachable, nothing is decided; it is simply retried.

**Restarts**
- All state is in SQLite on disk. On startup, any request left in `sending` (the process died mid-send) is put back to `open` and retried with the same idempotency key, then the reconciler runs immediately.

A notified customer can sign up again for the same product later (e.g. it sold out again); that is a new request and gets its own single email.

## Tests

`tests/run_tests.py` starts a local mock of the test server (stock API + Resend-style email API with failure injection and idempotency) and runs 24 end-to-end checks: duplicate and concurrent sign-ups, bad secrets, lying webhooks, 15 simultaneous copies of one event, email 500s, a missing webhook, a restart, and a crash between sending and saving.

```bash
pip install -r requirements.txt
python tests/run_tests.py
```

## Built with

AI tools: Claude (planning, code generation and review), with the reliability tests run against a local mock of the harness.
Time taken: _fill in_.
