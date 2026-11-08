import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


STOCK_API_URL = _env("STOCK_API_URL").rstrip("/")
STOCK_API_KEY = _env("STOCK_API_KEY")
RESEND_API_KEY = _env("RESEND_API_KEY")
RESEND_BASE_URL = _env("RESEND_BASE_URL", "https://api.resend.com").rstrip("/")
WEBHOOK_SECRET = _env("WEBHOOK_SECRET")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")

# Optional settings (sensible defaults, nothing else is required to run)
DB_PATH = _env("DB_PATH", "./data/app.db")
EMAIL_FROM = _env("EMAIL_FROM", "Restock Alerts <alerts@restock-alerts.dev>")
STORE_NAME = _env("STORE_NAME", "The Store")
PRODUCT_URL_TEMPLATE = _env("PRODUCT_URL_TEMPLATE", "https://example.com/products/{sku}")
RECONCILE_INTERVAL = float(_env("RECONCILE_INTERVAL", "20"))
HTTP_TIMEOUT = float(_env("HTTP_TIMEOUT", "8"))
SENDING_STALE_SECONDS = int(_env("SENDING_STALE_SECONDS", "120"))
MAX_SEND_ATTEMPTS = int(_env("MAX_SEND_ATTEMPTS", "12"))
