"""The back-in-stock email: table layout + inline CSS so it renders in Gmail,
Outlook and Apple Mail, single column and full-width button on phones."""
from html import escape

from . import config


def format_price(product: dict) -> str:
    p = product.get("price")
    if p is None:
        p = product.get("price_pence")
        if isinstance(p, (int, float)):
            return f"£{p / 100:.2f}"
        return ""
    if isinstance(p, str):
        return p
    if isinstance(p, (int, float)):
        cur = str(product.get("currency", "GBP")).upper()
        sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(cur, "")
        return f"{sym}{p:,.2f}".replace(".00", "")
    return ""


def product_url(sku: str, product: dict) -> str:
    return product.get("url") or config.PRODUCT_URL_TEMPLATE.format(sku=sku)


def render_email(req: dict, product: dict):
    sku = req["sku"]
    name = product.get("name") or product.get("title") or sku
    price = format_price(product)
    url = product_url(sku, product)
    store = config.STORE_NAME
    qty = product.get("quantity")
    try:
        low = qty is not None and 0 < int(qty) <= 10
    except (TypeError, ValueError):
        low = False

    subject = f"It's back: {name}"
    pre = f"Good news, the {name} you asked about is back in stock. Stock is limited."

    e = escape
    image = product.get("image") or product.get("image_url")
    image_html = (
        f'<tr><td style="padding:0 32px"><img src="{e(image)}" alt="{e(name)}" width="536" '
        f'style="display:block;width:100%;max-width:536px;height:auto;border-radius:12px;border:0"></td></tr>'
        if image else
        f'<tr><td style="padding:0 32px"><div style="background:#F3EFE7;border-radius:12px;padding:36px 24px;'
        f'text-align:center;font-family:Georgia,serif;font-size:22px;color:#2B2A27">{e(name)}</div></td></tr>'
    )
    urgency = (
        '<p style="margin:12px 0 0;font-size:14px;color:#B4532A;font-weight:600">'
        'Only a few left. These tend to go quickly.</p>' if low else
        '<p style="margin:12px 0 0;font-size:14px;color:#6B6862">Stock is limited, so we can\'t hold it for you.</p>'
    )

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<title>{e(subject)}</title>
<style>
@media only screen and (max-width:620px){{
  .card{{width:100%!important;border-radius:0!important}}
  .pad{{padding-left:20px!important;padding-right:20px!important}}
  .btn a{{display:block!important}}
  h1{{font-size:24px!important}}
}}
</style></head>
<body style="margin:0;padding:0;background:#F6F4EF">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{e(pre)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F6F4EF">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" class="card" width="600" cellpadding="0" cellspacing="0"
  style="width:600px;max-width:600px;background:#FFFFFF;border-radius:16px;overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#2B2A27">
  <tr><td class="pad" style="padding:28px 32px 8px;font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:#6B6862">
    {e(store)}</td></tr>
  <tr><td class="pad" style="padding:8px 32px 20px">
    <p style="margin:0 0 8px;display:inline-block;background:#E4F1E8;color:#1F6B3A;font-size:12px;font-weight:700;
      letter-spacing:.06em;text-transform:uppercase;padding:5px 10px;border-radius:999px">Back in stock</p>
    <h1 style="margin:12px 0 0;font-size:28px;line-height:1.2;font-weight:700">It's back, and it's yours if you're quick.</h1>
  </td></tr>
  {image_html}
  <tr><td class="pad" style="padding:20px 32px 4px">
    <p style="margin:0;font-size:18px;font-weight:600">{e(name)}</p>
    <p style="margin:4px 0 0;font-size:16px;color:#6B6862">{e(price)}{' · ' if price else ''}SKU {e(sku)}</p>
    {urgency}
  </td></tr>
  <tr><td class="pad btn" style="padding:24px 32px 8px">
    <a href="{e(url)}" style="display:inline-block;background:#2B2A27;color:#FFFFFF;text-decoration:none;
      font-size:16px;font-weight:600;padding:15px 28px;border-radius:10px;text-align:center">Shop now</a>
  </td></tr>
  <tr><td class="pad" style="padding:24px 32px 28px;font-size:13px;line-height:1.5;color:#8A867E;border-top:1px solid #EFECE6">
    You're receiving this one-time email because you asked {e(store)} to tell you when this item was back.
    We won't email you about it again.
  </td></tr>
</table>
</td></tr></table>
</body></html>"""

    text = (
        f"{store}\n\nIt's back: {name}\n"
        f"{price + ' - ' if price else ''}SKU {sku}\n\n"
        f"Shop now: {url}\n\n"
        "Stock is limited, so we can't hold it for you.\n\n"
        f"You're receiving this one-time email because you asked {store} to tell you when this item was back."
    )
    return subject, html, text
