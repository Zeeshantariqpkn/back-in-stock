"""A tiny local stand-in for the challenge test server, used by tests/run_tests.py.
Stock API + Resend-compatible /emails (with Idempotency-Key replay) + controls."""
import threading
import uuid

from fastapi import FastAPI, Header, HTTPException, Request

KEY = "test-key"
app = FastAPI()
lock = threading.Lock()
state = {
    "stock": {"MUG-STN-SAGE": 0, "PAN-CI-26": 0, "KNF-CHF-20": 0, "LAMP-DSK-BRS": 0,
              "TEE-ORG-WHT-M": 5, "BEAN-ETH-250": 0},
    "fail_next": 0,
    "inbox": [],
    "idem": {},
    "stock_calls": 0,
}
NAMES = {"MUG-STN-SAGE": ("Stoneware Mug, Sage", 18), "PAN-CI-26": ("Cast Iron Skillet, 26 cm", 64),
         "KNF-CHF-20": ("Chef's Knife, 20 cm", 89), "LAMP-DSK-BRS": ("Brass Desk Lamp", 135),
         "TEE-ORG-WHT-M": ("Organic Cotton Tee, White, M", 24), "BEAN-ETH-250": ("Ethiopia Yirgacheffe Beans, 250 g", 14)}


def auth(h):
    if h != f"Bearer {KEY}":
        raise HTTPException(401)


def item(sku):
    q = state["stock"].get(sku, 0)
    name, price = NAMES.get(sku, (sku, 0))
    return {"sku": sku, "name": name, "price": price, "quantity": q, "in_stock": q > 0}


@app.get("/api/stock")
def cat(authorization: str = Header("")):
    auth(authorization)
    return {"products": [item(s) for s in state["stock"]]}


@app.get("/api/stock/{sku}")
def one(sku: str, authorization: str = Header("")):
    auth(authorization)
    with lock:
        state["stock_calls"] += 1
    return item(sku)


@app.post("/emails")
async def emails(request: Request, authorization: str = Header(""), idempotency_key: str = Header("")):
    auth(authorization)
    body = await request.json()
    with lock:
        if state["fail_next"] > 0:
            state["fail_next"] -= 1
            raise HTTPException(500, "simulated failure")
        if idempotency_key and idempotency_key in state["idem"]:
            return {"id": state["idem"][idempotency_key]}
        eid = str(uuid.uuid4())
        state["idem"][idempotency_key] = eid
        state["inbox"].append({"to": body["to"], "subject": body["subject"], "html": body["html"]})
        return {"id": eid}


@app.post("/_ctl/stock")
def set_stock(d: dict):
    state["stock"][d["sku"]] = d["quantity"]
    return {}


@app.post("/_ctl/fail")
def fail(d: dict):
    state["fail_next"] = d["n"]
    return {}


@app.get("/_ctl/inbox")
def inbox():
    return state["inbox"]
