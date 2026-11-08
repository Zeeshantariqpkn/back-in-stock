"""SQLite storage. Every guarantee (no duplicate requests, no duplicate events,
one sender per request) is enforced by the database, not by in-memory state,
so it holds across threads, concurrent requests and restarts."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL,
    sku             TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open | sending | notified | failed
    created_at      TEXT NOT NULL,
    claimed_at      TEXT,
    notified_at     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    email_id        TEXT
);

-- At most one *open* request per (email, sku). Enforced atomically by SQLite,
-- so two simultaneous form submits cannot both get in.
CREATE UNIQUE INDEX IF NOT EXISTS ux_requests_open
    ON requests(email, sku) WHERE status IN ('open', 'sending');

CREATE INDEX IF NOT EXISTS ix_requests_sku_status ON requests(sku, status);

-- Every webhook event is recorded once. A retried or concurrently duplicated
-- delivery hits the primary key and is ignored.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id      TEXT PRIMARY KEY,
    sku           TEXT NOT NULL,
    claimed_qty   TEXT,
    received_at   TEXT NOT NULL,
    verified_qty  INTEGER,
    outcome       TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def conn():
    c = connect()
    try:
        yield c
    finally:
        c.close()


def init() -> None:
    folder = os.path.dirname(os.path.abspath(config.DB_PATH))
    os.makedirs(folder, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
