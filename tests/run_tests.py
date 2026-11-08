"""End-to-end checks against the local mock harness.
Run from the repo root:  python tests/run_tests.py"""
import concurrent.futures as cf
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK, APP = "http://127.0.0.1:9100", "http://127.0.0.1:8100"
SECRET, PW = "whsec_test", "admin-pw"
DATA = tempfile.mkdtemp()
ENV = {**os.environ, "STOCK_API_URL": f"{MOCK}/api/stock", "STOCK_API_KEY": "test-key",
       "RESEND_API_KEY": "test-key", "RESEND_BASE_URL": MOCK, "WEBHOOK_SECRET": SECRET,
       "ADMIN_PASSWORD": PW, "PORT": "8100", "DB_PATH": f"{DATA}/app.db", "RECONCILE_INTERVAL": "2"}
results = []


def start(module, port, cwd):
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
                         cwd=cwd, env=ENV)
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{port}/docs" if port == 9100 else f"http://127.0.0.1:{port}/health", timeout=1)
            return p
        except httpx.HTTPError:
            time.sleep(0.2)
    raise SystemExit(f"{module} did not start")


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


def inbox_for(email):
    return [m for m in httpx.get(f"{MOCK}/_ctl/inbox").json() if email in m["to"]]


def req(email, sku):
    return httpx.post(f"{APP}/api/requests", json={"email": email, "sku": sku})


def hook(event_id, sku, qty, secret=SECRET):
    return httpx.post(f"{APP}/webhooks/stock", json={"event_id": event_id, "sku": sku, "quantity": qty},
                      headers={"X-Webhook-Secret": secret})


def stock(sku, q):
    httpx.post(f"{MOCK}/_ctl/stock", json={"sku": sku, "quantity": q})


mock = start("mock_harness:app", 9100, os.path.join(ROOT, "tests"))
app = start("app.main:app", 8100, ROOT)
try:
    # --- requests
    r = req("A@Example.com ", "mug-stn-sage")
    check("create request -> 201", r.status_code == 201, r.text)
    r = req("a@example.com", "MUG-STN-SAGE")
    check("same email+sku again -> friendly duplicate", r.status_code == 200 and r.json()["status"] == "already_subscribed", r.text)
    with cf.ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(lambda _: req("b@example.com", "MUG-STN-SAGE").status_code, range(20)))
    check("20 simultaneous identical submits -> exactly one created", codes.count(201) == 1, codes)
    check("invalid email rejected", req("nope", "MUG-STN-SAGE").status_code == 422)
    check("bad JSON rejected", httpx.post(f"{APP}/api/requests", content=b"{oops",
                                          headers={"content-type": "application/json"}).status_code == 400)

    # --- webhook trust
    check("wrong secret -> 401", hook("e0", "MUG-STN-SAGE", 5, secret="bad").status_code == 401)
    check("missing secret -> 401", httpx.post(f"{APP}/webhooks/stock", json={"event_id": "x", "sku": "A"}).status_code == 401)
    check("malformed event -> 400", hook("", "MUG-STN-SAGE", 5).status_code == 400)
    hook("lie-1", "MUG-STN-SAGE", 50)       # stock API still says 0
    time.sleep(1.5)
    check("lying webhook sends nothing", len(inbox_for("a@example.com")) == 0)

    # --- real restock with the same event delivered 15x at once
    stock("MUG-STN-SAGE", 3)
    with cf.ThreadPoolExecutor(15) as ex:
        statuses = list(ex.map(lambda _: hook("evt-dup", "MUG-STN-SAGE", 3).json()["status"], range(15)))
    check("concurrent duplicate deliveries -> 1 accepted", statuses.count("accepted") == 1, statuses)
    time.sleep(2)
    check("a@ got exactly one email", len(inbox_for("a@example.com")) == 1, inbox_for("a@example.com"))
    check("b@ got exactly one email", len(inbox_for("b@example.com")) == 1)
    hook("evt-later", "MUG-STN-SAGE", 3); time.sleep(1.5)
    check("later restock events don't re-email", len(inbox_for("a@example.com")) == 1)
    r = req("a@example.com", "MUG-STN-SAGE")
    check("can re-subscribe after being notified", r.status_code == 201, r.text)
    time.sleep(1.5)
    check("re-subscribe while in stock -> emailed right away (once more)", len(inbox_for("a@example.com")) == 2)

    # --- email API returning 500s
    req("c@example.com", "PAN-CI-26")
    httpx.post(f"{MOCK}/_ctl/fail", json={"n": 2})
    stock("PAN-CI-26", 4); hook("evt-pan", "PAN-CI-26", 4)
    time.sleep(4)
    check("email retried after 500s, delivered once", len(inbox_for("c@example.com")) == 1)

    # --- lost webhook: reconciler catches it
    req("d@example.com", "KNF-CHF-20"); time.sleep(0.5)
    stock("KNF-CHF-20", 2)                  # no webhook at all
    time.sleep(4)
    check("missed webhook caught by reconciler", len(inbox_for("d@example.com")) == 1)

    # --- restart: request survives; crash mid-send doesn't double-send
    req("e@example.com", "LAMP-DSK-BRS"); time.sleep(0.5)
    app.terminate(); app.wait()
    stock("LAMP-DSK-BRS", 1)
    app = start("app.main:app", 8100, ROOT)
    time.sleep(3)
    check("request survived restart and was notified", len(inbox_for("e@example.com")) == 1)

    req("f@example.com", "BEAN-ETH-250"); time.sleep(0.5)
    stock("BEAN-ETH-250", 9); hook("evt-bean", "BEAN-ETH-250", 9); time.sleep(2)
    first = len(inbox_for("f@example.com"))
    app.kill(); app.wait()
    con = sqlite3.connect(ENV["DB_PATH"])   # simulate dying after the email went out but before saving
    con.execute("UPDATE requests SET status='sending', notified_at=NULL WHERE email='f@example.com'"); con.commit(); con.close()
    app = start("app.main:app", 8100, ROOT)
    time.sleep(3)
    check("crash mid-send -> resent with same idempotency key, still one email",
          first == 1 and len(inbox_for("f@example.com")) == 1, (first, len(inbox_for("f@example.com"))))

    # --- pages
    e = httpx.get(f"{APP}/embed?sku=MUG-STN-SAGE")
    check("embed is frameable", e.status_code == 200 and "frame-ancestors *" in e.headers.get("content-security-policy", "")
          and "x-frame-options" not in e.headers)
    check("admin needs password", httpx.get(f"{APP}/admin").status_code == 401)
    check("admin wrong password", httpx.get(f"{APP}/admin", auth=("admin", "x")).status_code == 401)
    a = httpx.get(f"{APP}/admin", auth=("admin", PW))
    check("admin works with password", a.status_code == 200 and "a@example.com" in a.text)
    check("email HTML is mobile-ready", "max-width:600px" in inbox_for("a@example.com")[0]["html"])
    open(os.path.join(DATA, "sample_email.html"), "w").write(inbox_for("a@example.com")[0]["html"])
    open(os.path.join(DATA, "admin.html"), "w").write(a.text)
finally:
    app.terminate(); mock.terminate()

passed = sum(ok for _, ok in results)
print(f"\n{passed}/{len(results)} checks passed   (artifacts in {DATA})")
sys.exit(0 if passed == len(results) else 1)
