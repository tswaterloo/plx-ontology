"""Shopify Admin REST reader: orders with line items, customers and fulfilments. GET only,
through the one guarded transport; off until its settings exist.

Shopify orders are one of Petra's order channels (beside Zendesk order@, Commercebuild to
Sage, Amazon and EDI). ``orders.json?status=any&updated_at_min=`` is read page by page
(``limit=250``; the next page is the ``page_info`` of the response's ``Link`` rel="next",
rebuilt by this module, never followed as sent), customers the same way from
``customers.json``. Line items and fulfilments ride on the order; ``fulfillments`` reads an
order's fulfilments separately only when the order body carries none. Rate limit: a 429 is
retried within the guard's bounded ``Retry-After`` backoff, and the leaky-bucket header
``X-Shopify-Shop-Api-Call-Limit`` slows the sweep before it is hit (``BUCKET_SLOWDOWN``).

Settings (names only): ``SHOPIFY_SHOP`` (the shop's one host label), ``SHOPIFY_ACCESS_TOKEN``
(a read-only Admin API token with read_orders, read_customers, read_fulfillments only) and
optional ``SHOPIFY_API_VERSION`` (``YYYY-01|04|07|10``, default ``DEFAULT_API_VERSION``).

Money is Decimal from Shopify's decimal strings with the order's own ``currency``; a money
string that is not a plain decimal, or no currency, is no money (never defaulted). A
customer joins a PLX customer only by an exact e-mail the PLX customer carries (a join key,
never a merge here).

Paths and fields are from Shopify's public Admin REST reference and unverified until the
``shopify.orders`` live check has a result.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlsplit

from . import live_source_grammar as grammar
from . import capture_vendor as cv
from . import vendor_sweep as vs
from .http import SourceUnavailable
from .live_source_guard import GuardedTransport, Secret, jsonable

SOURCE = "shopify"
SETTING_SHOP, SETTING_TOKEN, SETTING_VERSION = (
    "SHOPIFY_SHOP", "SHOPIFY_ACCESS_TOKEN", "SHOPIFY_API_VERSION")
REQUIRED_SETTINGS = (SETTING_SHOP, SETTING_TOKEN)
DEFAULT_API_VERSION = "2025-10"
AUDIENCE_UNIT = "shopify:orders"
MAX_PAGES = 40                      # 40 x 250 orders per sweep; the cursor resumes the rest
BUCKET_SLOWDOWN = 0.8               # sleep when the call bucket is this full
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?")
_DECIMAL = re.compile(r"-?[0-9]{1,15}(?:\.[0-9]{1,6})?")
_CURRENCY = re.compile(r"[A-Z]{3}")
_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')
_ADDRESS = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}")
_ID = re.compile(r"[0-9]{1,20}")

# record -> nearest ontology type and the ontology property each reader field fills
# (checked against ontology/schema/domains/*.json by the tests).
BINDINGS: dict[str, dict[str, Any]] = {
    "ShopifyOrder": {"ontology_type": "SalesOrder", "join_key": "shopify_order_id",
                     "properties": {"order_number": "order_number", "order_date": "order_date",
                                    "currency": "currency", "subtotal": "subtotal",
                                    "total_amount": "total_amount", "status": "status"}},
    "ShopifyOrderLine": {"ontology_type": "SalesOrderLine", "join_key": "shopify_order_id",
                         "properties": {"ordered_quantity": "ordered_quantity",
                                        "unit_price": "unit_price", "line_total": "line_total",
                                        "description": "description"}},
    "ShopifyCustomer": {"ontology_type": "Customer", "join_key": "customer_email",
                        "properties": {"name": "name", "email": "email", "currency": "currency"}},
    "ShopifyFulfillment": {"ontology_type": "Shipment", "join_key": "shopify_order_id",
                           "properties": {"status": "status", "carrier": "carrier",
                                          "tracking_number": "tracking_number",
                                          "ship_date": "ship_date"}},
}


def settings_from_env(environ: Mapping[str, str] | None = None
                      ) -> tuple[str, str, dict[str, str], Secret]:
    env = os.environ if environ is None else environ
    missing = vs.missing_settings(REQUIRED_SETTINGS, env)
    if missing:
        raise vs.CredentialsAbsent(missing)
    label = env[SETTING_SHOP].strip().lower()
    if not _LABEL.fullmatch(label):
        raise vs.SettingInvalid(SETTING_SHOP)
    version = (env.get(SETTING_VERSION) or "").strip() or DEFAULT_API_VERSION
    if not grammar.TYPES["shopify_version"].valid(version):
        raise vs.SettingInvalid(SETTING_VERSION)
    token = Secret(env[SETTING_TOKEN].strip())
    return f"{label}.myshopify.com", version, {"X-Shopify-Access-Token": token.reveal()}, token


def money(amount: Any, currency: Any) -> dict[str, str] | None:
    """``{amount, currency}`` (a plain decimal string, never a float) or None."""
    if not (isinstance(amount, str) and _DECIMAL.fullmatch(amount.strip())
            and isinstance(currency, str) and _CURRENCY.fullmatch(currency.strip())):
        return None
    try:
        value = Decimal(amount.strip())
    except InvalidOperation:
        return None
    return {"amount": format(value, "f"), "currency": currency.strip()}


def _id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    s = str(value).strip()
    return s if _ID.fullmatch(s) else None


def _email(value: Any) -> str | None:
    t = vs.text(value, 320)
    return t.lower() if t and _ADDRESS.fullmatch(t) else None


def _day(value: Any) -> str | None:
    t = vs.text(value, 40)
    return t[:10] if t and re.match(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", t) else None


def _later(a: str, b: str) -> bool:
    try:
        return datetime.fromisoformat(a.replace("Z", "+00:00")) > datetime.fromisoformat(
            b.replace("Z", "+00:00"))
    except ValueError:
        return False


def map_line(order_id: str, line: Mapping[str, Any], currency: Any, n: int) -> dict[str, Any]:
    qty = line.get("quantity")
    return {"record_type": "ShopifyOrderLine", "ontology_type": "SalesOrderLine",
            "source": SOURCE, "audience_unit": AUDIENCE_UNIT,
            "citation": f"shopify://order/{order_id}/line/{_id(line.get('id')) or n}",
            "join_keys": {"shopify_order_id": order_id, "sku": vs.text(line.get("sku"), 64)},
            "properties": {
                "ordered_quantity": qty if isinstance(qty, int) and not isinstance(qty, bool)
                and qty >= 0 else None,
                "unit_price": money(line.get("price"), currency),
                "description": vs.text(line.get("title"), 300)}}


def map_fulfillment(order_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    fid = _id(raw.get("id"))
    if fid is None:
        raise SourceUnavailable("shopify fulfillment has no id")
    return {"record_type": "ShopifyFulfillment", "ontology_type": "Shipment",
            "source": SOURCE, "audience_unit": AUDIENCE_UNIT,
            "citation": f"shopify://order/{order_id}/fulfillment/{fid}",
            "join_keys": {"shopify_order_id": order_id},
            "properties": {"status": vs.text(raw.get("status"), 24),
                           "carrier": vs.text(raw.get("tracking_company"), 120),
                           "tracking_number": vs.text(raw.get("tracking_number"), 64),
                           "ship_date": _day(raw.get("created_at"))}}


def map_order(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One order body -> its ``ShopifyOrder``, its lines and its embedded fulfilments."""
    order_id = _id(raw.get("id"))
    if order_id is None:
        raise SourceUnavailable("shopify order has no id")
    currency = raw.get("currency")
    customer = raw.get("customer") if isinstance(raw.get("customer"), Mapping) else {}
    lines = raw.get("line_items")
    if not isinstance(lines, list):
        raise SourceUnavailable("shopify order has no line_items list")
    status = ("cancelled" if raw.get("cancelled_at") else
              vs.text(raw.get("fulfillment_status"), 24) or "unfulfilled")
    order = {
        "record_type": "ShopifyOrder", "ontology_type": "SalesOrder", "source": SOURCE,
        "audience_unit": AUDIENCE_UNIT, "citation": f"shopify://order/{order_id}",
        "join_keys": {"shopify_order_id": order_id,
                      "customer_email": _email(raw.get("email") or customer.get("email")),
                      "shopify_customer_id": _id(customer.get("id"))},
        "properties": {"order_number": vs.text(raw.get("name"), 32),
                       "order_date": _day(raw.get("created_at")),
                       "updated_at": vs.text(raw.get("updated_at"), 40),
                       "currency": currency if isinstance(currency, str)
                       and _CURRENCY.fullmatch(currency) else None,
                       "subtotal": money(raw.get("subtotal_price"), currency),
                       "total_amount": money(raw.get("total_price"), currency),
                       "status": status,
                       "financial_status": vs.text(raw.get("financial_status"), 24)},
        "line_count": len(lines)}
    out = [order]
    out += [map_line(order_id, ln, currency, n) for n, ln in enumerate(lines, 1)
            if isinstance(ln, Mapping)]
    out += [map_fulfillment(order_id, f) for f in raw.get("fulfillments") or []
            if isinstance(f, Mapping) and isinstance(raw.get("fulfillments"), list)]
    return out


def map_customer(raw: Mapping[str, Any]) -> dict[str, Any]:
    cid = _id(raw.get("id"))
    if cid is None:
        raise SourceUnavailable("shopify customer has no id")
    name = " ".join(p for p in (vs.text(raw.get("first_name"), 80),
                                vs.text(raw.get("last_name"), 80)) if p) or None
    currency = raw.get("currency")
    return {"record_type": "ShopifyCustomer", "ontology_type": "Customer", "source": SOURCE,
            "audience_unit": AUDIENCE_UNIT, "citation": f"shopify://customer/{cid}",
            "join_keys": {"shopify_customer_id": cid, "customer_email": _email(raw.get("email"))},
            "properties": {"name": name, "email": _email(raw.get("email")),
                           "currency": currency if isinstance(currency, str)
                           and _CURRENCY.fullmatch(currency) else None}}


def next_page_info(headers: Mapping[str, str], host: str, op: str, version: str) -> str | None:
    """The ``page_info`` of a ``Link: <url>; rel="next"`` header, only when that URL is this
    shop's own endpoint for ``op`` (host, path and token all checked)."""
    link = vs.header(headers, "Link")
    found = _NEXT.search(link) if link else None
    if not found:
        return None
    parts = urlsplit(found.group(1))
    expected = urlsplit(grammar.url(op, tenant_host=host, version=version)).path
    token = (parse_qs(parts.query).get("page_info") or [None])[0]
    if (parts.scheme != "https" or (parts.hostname or "").lower() != host
            or parts.path != expected or not token
            or not grammar.external_query_ok(op, "page_info=" + quote(token, safe=""))):
        raise SourceUnavailable("shopify next link is not this shop's own page")
    return token


class ShopifyReader(vs.VendorClient):
    def __init__(self, host: str, version: str, auth_headers: Mapping[str, str],
                 transport: GuardedTransport, *, sleep: Callable[[float], None] = time.sleep,
                 **kw: Any) -> None:
        super().__init__(transport, auth_headers, **kw)
        self.host, self.version, self._sleep = host, version, sleep

    def _pages(self, op: str, first: Mapping[str, str], key: str):
        query: dict[str, str] = dict(first)
        for _ in range(MAX_PAGES):
            url = grammar.url(op, query, tenant_host=self.host, version=self.version)
            body, headers = self.get_json(url)
            rows = body.get(key) if isinstance(body, Mapping) else None
            if not isinstance(rows, list):
                raise SourceUnavailable(f"shopify {key} payload invalid")
            used = vs.header(headers, "X-Shopify-Shop-Api-Call-Limit") or ""
            got = re.fullmatch(r"([0-9]+)/([0-9]+)", used)
            if got and int(got[2]) and int(got[1]) / int(got[2]) >= BUCKET_SLOWDOWN:
                self._sleep(2.0)
            token = next_page_info(headers, self.host, op, self.version)
            yield [r for r in rows if isinstance(r, Mapping)], token
            if token is None:
                return
            query = {"page_info": token, "limit": "250"}
        raise SourceUnavailable("shopify page bound reached")

    def _stream(self, op: str, key: str, mapper: Callable[[Mapping[str, Any]], Any],
                entry: Mapping[str, Any], first: Mapping[str, str]) -> dict[str, Any]:
        """Records changed since the saved ``updated_at_min`` watermark; it moves to the
        newest ``updated_at`` seen only when the whole sweep succeeded (a re-read record is
        the same record: the watermark is inclusive)."""
        since = entry.get("cursor") or "1970-01-01T00:00:00Z"
        records: list[dict[str, Any]] = []
        newest = since
        for rows, _token in self._pages(op, {**first, "updated_at_min": since,
                                             "limit": "250"}, key):
            for row in rows:
                mapped = mapper(row)
                records.extend(mapped if isinstance(mapped, list) else [mapped])
                updated = vs.text(row.get("updated_at"), 40)
                if updated and _later(updated, newest):
                    newest = updated
        return {"records": records, "cursor": newest, "complete": True}

    def orders(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        return self._stream("shopify.orders", "orders", map_order, entry, {"status": "any"})

    def customers(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        return self._stream("shopify.customers", "customers", map_customer, entry, {})

    def fulfillments(self, order_id: str) -> list[dict[str, Any]]:
        url = grammar.url("shopify.order_fulfillments", {"limit": "250"},
                          tenant_host=self.host, version=self.version, order=order_id)
        body, _h = self.get_json(url)
        rows = body.get("fulfillments") if isinstance(body, Mapping) else None
        if not isinstance(rows, list):
            raise SourceUnavailable("shopify fulfillments payload invalid")
        return [map_fulfillment(order_id, r) for r in rows if isinstance(r, Mapping)]


def sweep(environ: Mapping[str, str] | None = None, *, state_path: str | Path,
          transport: GuardedTransport | None = None, now: datetime | None = None,
          sleep: Callable[[float], None] = time.sleep, stream: str = "orders",
          store: Any = None) -> dict[str, Any]:
    """One bounded incremental read of ``stream``; with ``store`` its records are saved
    under the shop (unit ``shopify:<shop label>``) before the cursor moves."""
    if stream not in ("orders", "customers"):
        raise ValueError("stream is orders or customers")
    env = os.environ if environ is None else environ
    missing = vs.missing_settings(REQUIRED_SETTINGS, env)
    if missing:
        return {"source": SOURCE, "blocked": "credentials_absent", "missing_settings": missing,
                "requests_sent": 0, "evidence_label": "not-run"}
    try:
        host, version, headers, secret = settings_from_env(env)
    except vs.SettingInvalid as exc:
        return {"source": SOURCE, "blocked": "setting_invalid",
                "invalid_settings": [exc.setting], "requests_sent": 0,
                "evidence_label": "not-run"}
    transport = transport or GuardedTransport()
    transport.register_secret(secret)
    reader = ShopifyReader(host, version, headers, transport, sleep=sleep)
    label = host.split(".", 1)[0]
    persist = cv.persist_into(store, cv.SHOPIFY_KIND, lambda _r: label, binding=host,
                              source_api=f"shopify.{stream}")
    out = vs.run_sweep(f"{SOURCE}_{stream}", vs.SweepState(state_path),
                       {"orders": reader.orders, "customers": reader.customers}[stream],
                       transport, now=now, persist=persist)
    out["source"] = SOURCE
    out["evidence_label"] = "injected-transport" if transport.sender_injected else "live-read"
    return out


def probe(environ: Mapping[str, str] | None = None, *, state_path: str | Path | None = None,
          transport: GuardedTransport | None = None) -> dict[str, Any]:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = sweep(environ, state_path=state_path or Path(tmp) / "state.json",
                       transport=transport)
    records = result.pop("records", [])
    for kind, name in (("ShopifyOrder", "orders"), ("ShopifyOrderLine", "lines"),
                       ("ShopifyFulfillment", "fulfillments")):
        result[name] = sum(1 for r in records if r["record_type"] == kind)
    result["ok"] = "error" not in result and "blocked" not in result
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["probe"]:
        print("usage: python3 -m integrations.shopify_read probe", file=sys.stderr)
        return 2
    result = probe()
    print(json.dumps(jsonable(result), sort_keys=True))
    return 0 if result.get("ok") or result.get("blocked") else 1


if __name__ == "__main__":
    raise SystemExit(main())
