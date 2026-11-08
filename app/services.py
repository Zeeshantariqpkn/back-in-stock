"""Talking to the outside world (stock API, email API) and the notify logic."""
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db
from .emails import render_email

log = logging.getLogger("bis")

_client = httpx.Client(timeout=config.HTTP_TIMEOUT)
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class Unavailable(Exception):
    """The remote service could not give a trustworthy answer right now."""


def _get_json(url: str, tries: int = 3):
    last = None
    for attempt in range(tries):
        try:
            r = _client.get(url, headers={"Authorization": f"Bearer {config.STOCK_API_KEY}"})
            if r.status_code == 200:
                return r.json()
            if r.status_code not in RETRYABLE:
                raise Unavailable(f"HTTP {r.status_code}")
            last = f"HTTP {r.status_code}"
        except (httpx.HTTPError, ValueError) as e:
            last = repr(e)
        time.sleep(0.3 * (2 ** attempt) + random.random() * 0.2)
    raise Unavailable(last or "unknown error")


# ---------------- stock ----------------

def get_stock(sku: str) -> dict:
    """Ask the stock API (the source of truth). Raises Unavailable."""
    data = _get_json(f"{config.STOCK_API_URL}/{_quote(sku)}")
    if not isinstance(data, dict):
        raise Unavailable("unexpected stock response")
    return data


def _quote(sku: str) -> str:
    from urllib.parse import quote
    return quote(sku, safe="")


def is_in_stock(data: dict) -> bool:
    qty = data.get("quantity")
    try:
        qty = int(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty = None
    flag = data.get("in_stock")
    if flag is not None and not flag:
        return False
    if qty is not None:
        return qty > 0
    return bool(flag)


_catalogue: dict = {"at": 0.0, "items": {}}
_cat_lock = threading.Lock()


def catalogue() -> dict:
    """SKU -> product dict, cached for 60s. Never raises; returns last good copy."""
    with _cat_lock:
        if time.time() - _catalogue["at"] < 60 and _catalogue["items"]:
            return _catalogue["items"]
    try:
        data = _get_json(config.STOCK_API_URL, tries=2)
        items = data.get("products") or data.get("items") or data.get("data") if isinstance(data, dict) else data
        out = {}
        for p in items or []:
            if isinstance(p, dict) and p.get("sku"):
                out[str(p["sku"]).upper()] = p
        with _cat_lock:
            _catalogue.update(at=time.time(), items=out)
    except Exception as e:  # network or shape problems: keep serving the old copy
        log.warning("catalogue fetch failed: %s", e)
    return _catalogue["items"]


def product_name(sku: str) -> str:
    p = catalogue().get(sku.upper()) or {}
    return p.get("name") or p.get("title") or sku


# ---------------- email ----------------

def send_email(req: dict, product: dict) -> str:
    """Send through the Resend-compatible API. The Idempotency-Key is tied to the
    request id, so any retry (timeout, crash, restart) of the same request can
    never produce a second email at a Resend-compatible API."""
    subject, html, text = render_email(req, product)
    payload = {
        "from": config.EMAIL_FROM,
        "to": [req["email"]],
        "subject": subject,
        "html": html,
        "text": text,
        "tags": [{"name": "type", "value": "back_in_stock"}],
    }
    headers = {
        "Authorization": f"Bearer {config.RESEND_API_KEY}",
        "Idempotency-Key": f"back-in-stock-request-{req['id']}",
    }
    last = None
    for attempt in range(3):
        try:
            r = _client.post(f"{config.RESEND_BASE_URL}/emails", json=payload, headers=headers)
            if 200 <= r.status_code < 300:
                try:
                    return str(r.json().get("id", ""))
                except ValueError:
                    return ""
            if r.status_code == 409:      # idempotent replay of an email already accepted
                return "duplicate-suppressed"
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in RETRYABLE:
                break
        except httpx.HTTPError as e:
            last = repr(e)
        time.sleep(0.5 * (2 ** attempt) + random.random() * 0.3)
    raise Unavailable(last or "send failed")


# ---------------- notify logic ----------------

_sku_locks: dict = {}
_sku_locks_guard = threading.Lock()


def _lock_for(sku: str) -> threading.Lock:
    with _sku_locks_guard:
        return _sku_locks.setdefault(sku, threading.Lock())


def _backoff(attempts: int) -> str:
    secs = min(15 * (2 ** max(attempts - 1, 0)), 600)
    return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat(timespec="seconds")


def process_sku(sku: str) -> dict:
    """Notify everyone waiting on `sku` if, and only if, the stock API says it is
    in stock. Safe to call any number of times, from any thread.
    Returns {"outcome", "verified_qty", "sent"}."""
    lock = _lock_for(sku)
    if not lock.acquire(timeout=60):   # one pass per SKU at a time; others wait their turn
        return {"outcome": "busy", "verified_qty": None, "sent": 0}
    try:
        with db.conn() as c:
            due = c.execute(
                "SELECT * FROM requests WHERE sku=? AND status='open' "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY id",
                (sku, db.now()),
            ).fetchall()
        if not due:
            return {"outcome": "nobody waiting", "verified_qty": None, "sent": 0}
        try:
            stock = get_stock(sku)
        except Unavailable as e:
            log.warning("stock check failed for %s: %s (will retry)", sku, e)
            return {"outcome": "stock API unavailable, will retry", "verified_qty": None, "sent": 0}
        qty = stock.get("quantity")
        if not is_in_stock(stock):
            return {"outcome": "not actually in stock, ignored", "verified_qty": qty, "sent": 0}
        product = {**catalogue().get(sku, {}), **stock, "sku": sku}
        sent = sum(_notify_one(dict(row), product) for row in due)
        return {"outcome": f"in stock, notified {sent}", "verified_qty": qty, "sent": sent}
    finally:
        lock.release()


def _notify_one(req: dict, product: dict) -> int:
    with db.conn() as c:
        # Atomic claim: only one worker in the whole system can move a request
        # from open -> sending. Everyone else gets rowcount 0 and stops.
        cur = c.execute(
            "UPDATE requests SET status='sending', claimed_at=?, attempts=attempts+1 "
            "WHERE id=? AND status='open'",
            (db.now(), req["id"]),
        )
        if cur.rowcount != 1:
            return 0
    try:
        email_id = send_email(req, product)
    except Unavailable as e:
        attempts = req["attempts"] + 1
        final = attempts >= config.MAX_SEND_ATTEMPTS
        with db.conn() as c:
            c.execute(
                "UPDATE requests SET status=?, last_error=?, next_attempt_at=? WHERE id=? AND status='sending'",
                ("failed" if final else "open", str(e)[:300], _backoff(attempts), req["id"]),
            )
        log.warning("email to request %s failed (attempt %s): %s", req["id"], attempts, e)
        return 0
    with db.conn() as c:
        c.execute(
            "UPDATE requests SET status='notified', notified_at=?, email_id=?, last_error=NULL "
            "WHERE id=? AND status='sending'",
            (db.now(), email_id, req["id"]),
        )
    log.info("notified request %s (%s, %s)", req["id"], req["email"], req["sku"])
    return 1


def recover_stuck(on_startup: bool = False) -> int:
    """A request left in 'sending' means the process died mid-send. Put it back
    to 'open'; the retry reuses the same Idempotency-Key, so it is not sent twice."""
    with db.conn() as c:
        if on_startup:
            cur = c.execute("UPDATE requests SET status='open', next_attempt_at=NULL WHERE status='sending'")
        else:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=config.SENDING_STALE_SECONDS)).isoformat(timespec="seconds")
            cur = c.execute("UPDATE requests SET status='open' WHERE status='sending' AND claimed_at<?", (cutoff,))
        return cur.rowcount


def reconcile_once() -> int:
    """Safety net for lost/late webhooks: check every SKU that still has people
    waiting, straight against the stock API."""
    recover_stuck()
    with db.conn() as c:
        skus = [r[0] for r in c.execute(
            "SELECT DISTINCT sku FROM requests WHERE status='open' "
            "AND (next_attempt_at IS NULL OR next_attempt_at<=?)", (db.now(),))]
    total = 0
    for sku in skus:
        try:
            total += process_sku(sku)["sent"]
        except Exception:
            log.exception("reconcile failed for %s", sku)
    return total


class Reconciler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="reconciler")
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                reconcile_once()
            except Exception:
                log.exception("reconcile loop error")
            self.stop_event.wait(config.RECONCILE_INTERVAL)
