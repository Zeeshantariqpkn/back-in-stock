import hmac
import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config, db, services
from .emails import format_price

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bis")
logging.getLogger("httpx").setLevel(logging.WARNING)

templates = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
MAX_BODY = 32 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    recovered = services.recover_stuck(on_startup=True)
    if recovered:
        log.info("recovered %s request(s) left mid-send by a previous run", recovered)
    worker = services.Reconciler()
    worker.start()
    yield
    worker.stop_event.set()


app = FastAPI(title="Back-in-Stock Alerts", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS"],
                   allow_headers=["Content-Type"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.url.path == "/embed":
        # Must be frameable by any store; never send X-Frame-Options here.
        response.headers["Content-Security-Policy"] = "frame-ancestors *"
    else:
        response.headers["X-Frame-Options"] = "DENY"
    return response


def _json_error(status: int, message: str, code: str):
    return JSONResponse({"status": "error", "error": code, "message": message}, status_code=status)


async def _read_json(request: Request):
    body = await request.body()
    if len(body) > MAX_BODY:
        return None, _json_error(413, "Request too large.", "too_large")
    ctype = request.headers.get("content-type", "")
    try:
        if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
            data = dict(await request.form())
        else:
            data = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return None, _json_error(400, "Body must be valid JSON.", "invalid_json")
    if not isinstance(data, dict):
        return None, _json_error(400, "Body must be a JSON object.", "invalid_json")
    return data, None


# ---------------------------------------------------------------- pages

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/embed")


@app.get("/health")
def health():
    with db.conn() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/embed", response_class=HTMLResponse)
def embed(sku: str = ""):
    sku = sku.strip().upper() if SKU_RE.match(sku.strip()) else ""
    return templates.get_template("embed.html").render(sku=sku, store=config.STORE_NAME)


@app.get("/api/products")
def products():
    """Public product names for the form (names/prices only, never keys)."""
    out = []
    for sku, p in services.catalogue().items():
        out.append({"sku": sku, "name": p.get("name") or p.get("title") or sku,
                    "price": format_price(p),
                    "in_stock": services.is_in_stock(p) if ("quantity" in p or "in_stock" in p) else None})
    return {"products": out}


# ---------------------------------------------------------------- requests

@app.post("/api/requests")
async def create_request(request: Request, background: BackgroundTasks):
    data, err = await _read_json(request)
    if err:
        return err
    email = str(data.get("email") or "").strip().lower()
    sku = str(data.get("sku") or "").strip().upper()
    if not EMAIL_RE.match(email) or len(email) > 254:
        return _json_error(422, "Please enter a valid email address.", "invalid_email")
    if not SKU_RE.match(sku):
        return _json_error(422, "Please enter a valid product code (SKU).", "invalid_sku")

    name = services.product_name(sku)
    try:
        with db.conn() as c:
            cur = c.execute("INSERT INTO requests (email, sku, status, created_at) VALUES (?, ?, 'open', ?)",
                            (email, sku, db.now()))
            req_id = cur.lastrowid
    except sqlite3.IntegrityError:
        # Already waiting for this product: not an error for the customer.
        return JSONResponse({"status": "already_subscribed", "email": email, "sku": sku, "product": name,
                             "message": f"You're already on the list for {name}. We'll email you once."},
                            status_code=200)

    # If it is (or just came) back in stock, notify right away instead of waiting.
    background.add_task(services.process_sku, sku)
    return JSONResponse({"status": "created", "id": req_id, "email": email, "sku": sku, "product": name,
                         "message": f"Done. We'll email {email} as soon as {name} is back."},
                        status_code=201)


# ---------------------------------------------------------------- webhook

def _handle_event(event_id: str, sku: str):
    result = services.process_sku(sku)
    with db.conn() as c:
        c.execute("UPDATE webhook_events SET verified_qty=?, outcome=? WHERE event_id=?",
                  (result["verified_qty"], result["outcome"], event_id))


@app.post("/webhooks/stock")
async def stock_webhook(request: Request, background: BackgroundTasks):
    given = request.headers.get("x-webhook-secret", "")
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(given.encode(), config.WEBHOOK_SECRET.encode()):
        return _json_error(401, "Invalid webhook secret.", "unauthorized")

    data, err = await _read_json(request)
    if err:
        return err
    event_id = data.get("event_id")
    sku = str(data.get("sku") or "").strip().upper()
    if not isinstance(event_id, (str, int)) or not str(event_id).strip() or len(str(event_id)) > 200:
        return _json_error(400, "Missing or invalid event_id.", "invalid_event")
    if not SKU_RE.match(sku):
        return _json_error(400, "Missing or invalid sku.", "invalid_event")
    event_id = str(event_id).strip()

    with db.conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO webhook_events (event_id, sku, claimed_qty, received_at, outcome) "
            "VALUES (?, ?, ?, ?, 'queued')",
            (event_id, sku, str(data.get("quantity"))[:32], db.now()),
        )
    if cur.rowcount == 0:
        # Retry or concurrent duplicate: acknowledge so the sender stops, do nothing else.
        return {"status": "duplicate", "event_id": event_id}

    # The payload's quantity is only a hint; the handler re-checks the stock API.
    background.add_task(_handle_event, event_id, sku)
    return {"status": "accepted", "event_id": event_id}


# ---------------------------------------------------------------- admin

security = HTTPBasic(realm="Back-in-Stock admin")


def require_admin(creds: HTTPBasicCredentials = Depends(security)):
    if not config.ADMIN_PASSWORD:
        raise HTTPException(503, "Set ADMIN_PASSWORD to enable the admin page.")
    if not hmac.compare_digest(creds.password.encode(), config.ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": 'Basic realm="Back-in-Stock admin"'})
    return True


STATUSES = ["open", "sending", "notified", "failed"]


@app.get("/admin", response_class=HTMLResponse)
def admin(status: str = "", q: str = "", _: bool = Depends(require_admin)):
    status = status if status in STATUSES else ""
    q = q.strip()[:100]
    where, args = [], []
    if status:
        where.append("status = ?"); args.append(status)
    if q:
        where.append("(email LIKE ? OR sku LIKE ?)"); args += [f"%{q.lower()}%", f"%{q.upper()}%"]
    sql = "SELECT * FROM requests" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 500"
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        counts = {s: 0 for s in STATUSES}
        for s, n in c.execute("SELECT status, COUNT(*) FROM requests GROUP BY status"):
            counts[s] = n
        events = [dict(r) for r in c.execute("SELECT * FROM webhook_events ORDER BY received_at DESC LIMIT 25")]
    cat = services.catalogue()
    for r in rows:
        p = cat.get(r["sku"], {})
        r["product"] = p.get("name") or p.get("title") or ""
    return templates.get_template("admin.html").render(
        rows=rows, counts=counts, total=sum(counts.values()), events=events,
        status=status, q=q, store=config.STORE_NAME)
