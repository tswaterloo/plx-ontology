"""Amazon Selling Partner API reader: read only (decision 2026-10-19-fba-external-sources).

Every request goes through ``integrations.live_source_guard.GuardedTransport`` (the one
sender): the Login with Amazon refresh-token POST to ``https://api.amazon.com/auth/o2/token``
and GETs of a closed per-operation grammar on the region host
(``sellingpartnerapi-<na|eu|fe>.amazon.com``, ``live_source_grammar.SP_QUERIES``):

* Fulfillment Inbound v0 ``getShipments`` / ``getShipmentItemsByShipmentId``: the Amazon
  shipment id, destination fulfilment centre, status, label prep type and lines;
* Fulfillment Inbound v0 ``getLabels``: whether a box or pallet label document exists for
  a shipment (its pre-signed download URL is never kept, logged or fetched);
* FBA Inventory v1 ``getInventorySummaries``: on-hand and inbound per seller SKU;
* Sales v1 ``getOrderMetrics``: units ordered per seller SKU over the last 30 days.

Settings (names only; values are never printed, logged or committed):
``AMAZON_SP_API_LWA_CLIENT_ID``, ``AMAZON_SP_API_LWA_CLIENT_SECRET``,
``AMAZON_SP_API_REFRESH_TOKEN`` (required), ``AMAZON_SP_API_REGION`` (``na`` by default)
and ``AMAZON_SP_API_MARKETPLACE_IDS`` (required, comma list). A missing setting is
``credentials_absent`` naming it, before any I/O.

Records are typed to the ontology's nearest types (``BINDINGS``): an inbound shipment is a
``Shipment`` whose join key is the Amazon shipment id (``amazon_shipment_id``, the one
grammar in ``ontology/schema/identifiers.json``); inventory and sales rows are keyed by the
seller SKU, which is not a PLX item code (FBA-05: no join without a cited mapping). Money
and quantities are Decimal text, each amount with its own currency.

``python3 -m integrations.amazon_sp_read probe`` prints counts and enums only (the
``amazon.inbound_shipments`` live check).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlencode

from ontology import identifiers as _ids

from . import live_source_grammar as grammar
from .http import SourceUnavailable
from .live_source_guard import (LWA_HOST, LWA_TOKEN_PATH, GuardedTransport, ReadOnlyGuardError,
                                Secret, failure_diagnostics, jsonable)

SETTING_CLIENT_ID = "AMAZON_SP_API_LWA_CLIENT_ID"
SETTING_CLIENT_SECRET = "AMAZON_SP_API_LWA_CLIENT_SECRET"
SETTING_REFRESH_TOKEN = "AMAZON_SP_API_REFRESH_TOKEN"
SETTING_REGION = "AMAZON_SP_API_REGION"
SETTING_MARKETPLACES = "AMAZON_SP_API_MARKETPLACE_IDS"
REQUIRED_SETTINGS = (SETTING_CLIENT_ID, SETTING_CLIENT_SECRET, SETTING_REFRESH_TOKEN,
                     SETTING_MARKETPLACES)
SETTINGS = (*REQUIRED_SETTINGS, SETTING_REGION)
DEFAULT_REGION = "na"
SOURCE = "amazon_sp_api"
MAX_PAGES = 20
SALES_WINDOW_DAYS = 30
# getOrderMetrics: 0.5 requests per second (SP-API usage plan); one SKU per request.
ORDER_METRICS_INTERVAL_SECONDS = 2.0
MAX_METRIC_SKUS = 20
MAX_MARKETPLACES = 10

# Reader record -> nearest ontology type (ontology/schema/domains/*.json) and the existing
# ontology property each reader field fills. Fields with no ontology property stay under
# the record's ``amazon`` block until the source family is projected (decision record).
BINDINGS: dict[str, dict[str, Any]] = {
    "InboundShipment": {"ontology_type": "Shipment",
                        "properties": {"status": "status"},
                        "join_key": "amazon_shipment_id"},
    "InboundShipmentLine": {"ontology_type": "ShipmentLine",
                            "properties": {"shipped_quantity": "shipped_quantity"},
                            "join_key": "amazon_shipment_id"},
    "InboundShipmentLabels": {"ontology_type": None, "properties": {},
                              "join_key": "amazon_shipment_id",
                              "why_unbound": "a label document is no ontology type; only its "
                                             "presence is recorded"},
    "FbaInventorySummary": {"ontology_type": None, "properties": {},
                            "join_key": "seller_sku",
                            "why_unbound": "seller SKU to PLX item mapping unconfirmed (FBA-05)"},
    "SkuSalesMetric": {"ontology_type": None, "properties": {}, "join_key": "seller_sku",
                       "why_unbound": "seller SKU to PLX item mapping unconfirmed (FBA-05)"},
}


class CredentialsAbsent(SourceUnavailable):
    """A required setting is not set; ``missing`` names it (never a value)."""

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        super().__init__("credentials_absent: " + ", ".join(self.missing))


class SettingInvalid(SourceUnavailable):
    def __init__(self, name: str) -> None:
        self.setting = name
        super().__init__(f"setting_invalid: {name}")


class SpApiSettings:
    __slots__ = ("client_id", "client_secret", "refresh_token", "region", "marketplaces")

    def __init__(self, client_id: Secret, client_secret: Secret, refresh_token: Secret,
                 region: str, marketplaces: tuple[str, ...]) -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self.refresh_token, self.region, self.marketplaces = refresh_token, region, marketplaces

    def __repr__(self) -> str:
        return f"SpApiSettings(region={self.region!r}, marketplaces={len(self.marketplaces)})"


def missing_settings(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [name for name in REQUIRED_SETTINGS if not (env.get(name) or "").strip()]


def settings_from_env(environ: Mapping[str, str] | None = None) -> SpApiSettings:
    """The settings, or ``CredentialsAbsent`` / ``SettingInvalid`` (names only)."""
    env = os.environ if environ is None else environ
    missing = missing_settings(env)
    if missing:
        raise CredentialsAbsent(missing)
    region = (env.get(SETTING_REGION) or "").strip().lower() or DEFAULT_REGION
    if region not in grammar.SP_API_HOSTS:
        raise SettingInvalid(SETTING_REGION)
    marketplaces = tuple(m.strip() for m in env[SETTING_MARKETPLACES].split(",") if m.strip())
    if (not marketplaces or len(marketplaces) > MAX_MARKETPLACES
            or len(set(marketplaces)) != len(marketplaces)
            or not all(_ids.valid("amazon_marketplace_id", m) for m in marketplaces)):
        raise SettingInvalid(SETTING_MARKETPLACES)
    return SpApiSettings(Secret(env[SETTING_CLIENT_ID].strip()),
                         Secret(env[SETTING_CLIENT_SECRET].strip()),
                         Secret(env[SETTING_REFRESH_TOKEN].strip()), region, marketplaces)


def amazon_shipment_id(value: Any) -> str | None:
    """The canonical Amazon shipment id of ``value`` (trim, upper case), else None."""
    if not isinstance(value, str):
        return None
    canonical = value.strip().upper()
    return canonical if _ids.valid("amazon_shipment_id", canonical) else None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def _money(value: Any) -> dict[str, Any] | None:
    """``{amount, currencyCode}`` -> Decimal text with its own currency; no currency, no money."""
    if not isinstance(value, Mapping):
        return None
    amount, currency = _decimal(value.get("amount")), value.get("currencyCode")
    if amount is None or not (isinstance(currency, str) and len(currency.strip()) == 3):
        return None
    return {"amount": format(amount, "f"), "currency": currency.strip().upper()}


def _text(value: Any, limit: int = 200) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


class SpApiReader:
    """GET-only SP-API reads for one seller account through the guarded transport."""

    def __init__(self, settings: SpApiSettings, transport: GuardedTransport, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self.transport = transport
        self._clock, self._sleep = clock, sleep
        self._token: Secret | None = None
        self._expires_at = 0.0
        self._last_metric_at: float | None = None
        for secret in (settings.client_id, settings.client_secret, settings.refresh_token):
            transport.register_secret(secret)

    def __repr__(self) -> str:
        return f"SpApiReader(region={self.settings.region!r})"

    # -- token
    def access_token(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._expires_at - 120:
            return self._token.reveal()
        status, _headers, raw = self.transport.request(
            "POST", f"https://{LWA_HOST}{LWA_TOKEN_PATH}", {"Accept": "application/json"},
            form={"grant_type": "refresh_token",
                  "refresh_token": self.settings.refresh_token.reveal(),
                  "client_id": self.settings.client_id.reveal(),
                  "client_secret": self.settings.client_secret.reveal()},
            max_bytes=200_000)
        if status != 200:
            raise _status_failure("token request refused", status)
        try:
            payload = json.loads(raw)
            token, lifetime = payload["access_token"], int(payload.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError):
            raise SourceUnavailable("token response invalid") from None
        if not isinstance(token, str) or not token:
            raise SourceUnavailable("token response invalid")
        self.transport.forget_secret(self._token)
        self._token = self.transport.register_secret(Secret(token))
        self._expires_at = now + max(0, min(lifetime, 3600))
        return token

    # -- GET
    def _get(self, name: str, query: Mapping[str, str], **params: str) -> Any:
        op = f"sp_api.{self.settings.region}.{name}"
        url = grammar.url(op, query, **params)
        status, _headers, raw = self.transport.request(
            "GET", url, {"Accept": "application/json", "x-amz-access-token": self.access_token(),
                         "User-Agent": "plx-workbench-read/1.0 (Language=Python)"})
        if status != 200:
            raise _status_failure(f"{name} GET returned HTTP {status}", status)
        try:
            return json.loads(raw, parse_float=Decimal)
        except ValueError:
            raise SourceUnavailable(f"{name} response is not JSON") from None

    def inbound_shipments(self, marketplace: str, *, updated_after: datetime,
                          updated_before: datetime,
                          statuses: Iterable[str] = ("WORKING", "READY_TO_SHIP", "SHIPPED",
                                                     "IN_TRANSIT", "DELIVERED", "CHECKED_IN",
                                                     "RECEIVING", "CLOSED"),
                          max_pages: int = MAX_PAGES) -> dict[str, Any]:
        """Inbound shipments updated in the window; ``completeness`` says whether it is whole."""
        query = {"MarketplaceId": marketplace, "QueryType": "DATE_RANGE",
                 "ShipmentStatusList": ",".join(statuses),
                 "LastUpdatedAfter": _instant(updated_after),
                 "LastUpdatedBefore": _instant(updated_before)}
        rows: list[Mapping[str, Any]] = []
        pages, token = 0, None
        while True:
            page_query = (query if token is None else
                          {"MarketplaceId": marketplace, "QueryType": "NEXT_TOKEN",
                           "NextToken": token})
            body = self._get("inbound_shipments", page_query)
            pages += 1
            payload = body.get("payload") if isinstance(body, Mapping) else None
            data = payload.get("ShipmentData") if isinstance(payload, Mapping) else None
            if not isinstance(data, list):
                raise SourceUnavailable("inbound_shipments payload invalid")
            rows.extend(r for r in data if isinstance(r, Mapping))
            token = payload.get("NextToken")
            if not token:
                return {"records": [map_inbound_shipment(r, marketplace) for r in rows],
                        "completeness": {"complete": True, "pages": pages}}
            if pages >= max_pages:
                return {"records": [map_inbound_shipment(r, marketplace) for r in rows],
                        "completeness": {"complete": False, "pages": pages,
                                         "reason": "page_cap"}}

    def inbound_shipment_items(self, marketplace: str, shipment_id: str) -> list[dict[str, Any]]:
        canonical = amazon_shipment_id(shipment_id)
        if canonical is None:
            raise ValueError("not an Amazon shipment id")
        body = self._get("inbound_shipment_items", {"MarketplaceId": marketplace},
                         shipment=canonical)
        payload = body.get("payload") if isinstance(body, Mapping) else None
        data = payload.get("ItemData") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            raise SourceUnavailable("inbound_shipment_items payload invalid")
        return [map_inbound_item(r, canonical, marketplace) for r in data
                if isinstance(r, Mapping)]

    def inbound_labels(self, marketplace: str, shipment_id: str, *, page_type: str,
                       label_type: str, number_of_packages: int | None = None,
                       number_of_pallets: int | None = None) -> dict[str, Any]:
        """getLabels for one shipment: whether the label document exists, never its URL."""
        canonical = amazon_shipment_id(shipment_id)
        if canonical is None:
            raise ValueError("not an Amazon shipment id")
        query = {"MarketplaceId": marketplace, "PageType": page_type, "LabelType": label_type}
        for name, count in (("NumberOfPackages", number_of_packages),
                            ("NumberOfPallets", number_of_pallets)):
            if count is not None:
                query[name] = str(count)
        if not grammar.external_query_ok(f"sp_api.{self.settings.region}.inbound_labels",
                                         urlencode(query)):
            raise ValueError("label request outside the getLabels grammar")
        body = self._get("inbound_labels", query, shipment=canonical)
        payload = body.get("payload") if isinstance(body, Mapping) else None
        if not isinstance(payload, Mapping):
            raise SourceUnavailable("inbound_labels payload invalid")
        return map_inbound_labels(payload, canonical, marketplace, page_type, label_type)

    def inventory_summaries(self, marketplace: str, *,
                            max_pages: int = MAX_PAGES) -> dict[str, Any]:
        query = {"details": "true", "granularityType": "Marketplace",
                 "granularityId": marketplace, "marketplaceIds": marketplace}
        rows: list[Mapping[str, Any]] = []
        pages, token = 0, None
        while True:
            body = self._get("inventory_summaries",
                             query if token is None else {**query, "nextToken": token})
            pages += 1
            payload = body.get("payload") if isinstance(body, Mapping) else None
            data = payload.get("inventorySummaries") if isinstance(payload, Mapping) else None
            if not isinstance(data, list):
                raise SourceUnavailable("inventory_summaries payload invalid")
            rows.extend(r for r in data if isinstance(r, Mapping))
            pagination = body.get("pagination") if isinstance(body, Mapping) else None
            token = pagination.get("nextToken") if isinstance(pagination, Mapping) else None
            if not token or pages >= max_pages:
                complete = not token
                return {"records": [map_inventory_summary(r, marketplace) for r in rows],
                        "completeness": {"complete": complete, "pages": pages,
                                         **({} if complete else {"reason": "page_cap"})}}

    def sku_sales(self, marketplace: str, sku: str, *, now: datetime,
                  days: int = SALES_WINDOW_DAYS) -> dict[str, Any]:
        """Units ordered of one seller SKU over the ``days`` before ``now`` (one total)."""
        if not _ids.valid("amazon_seller_sku", sku):
            raise ValueError("not a seller SKU the grammar accepts")
        end = now.astimezone(timezone.utc).replace(microsecond=0)
        start = end - timedelta(days=days)
        if self._last_metric_at is not None:
            wait = ORDER_METRICS_INTERVAL_SECONDS - (self._clock() - self._last_metric_at)
            if wait > 0:
                self._sleep(wait)
        self._last_metric_at = self._clock()
        body = self._get("order_metrics", {
            "marketplaceIds": marketplace, "interval": f"{_instant(start)}--{_instant(end)}",
            "granularity": "Total", "sku": sku})
        payload = body.get("payload") if isinstance(body, Mapping) else None
        if not isinstance(payload, list):
            raise SourceUnavailable("order_metrics payload invalid")
        return map_sku_sales(payload, sku, marketplace, _instant(start), _instant(end))


def _instant(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("instant needs a time zone")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status_failure(message: str, status: int) -> SourceUnavailable:
    error = SourceUnavailable(f"{message}: HTTP {status}" if "HTTP" not in message else message)
    error.http_status = status  # type: ignore[attr-defined]
    return error


# ------------------------------------------------------------------ mapping

def _citation(*parts: str) -> str:
    return "amazon-sp://" + "/".join(parts)


def map_inbound_shipment(row: Mapping[str, Any], marketplace: str) -> dict[str, Any]:
    """One getShipments ``InboundShipmentInfo`` -> an ``InboundShipment`` record."""
    shipment = amazon_shipment_id(row.get("ShipmentId"))
    return {
        "record_type": "InboundShipment", "ontology_type": "Shipment", "source": SOURCE,
        "citation": _citation(marketplace, "inbound", shipment) if shipment else None,
        "join_keys": {"amazon_shipment_id": shipment},
        "identity": "exact" if shipment else "unresolved",
        "properties": {"status": _text(row.get("ShipmentStatus"), 32)},
        "amazon": {"marketplace_id": marketplace,
                   "shipment_name": _text(row.get("ShipmentName")),
                   "destination_fc": _text(row.get("DestinationFulfillmentCenterId"), 16),
                   "label_prep_type": _text(row.get("LabelPrepType"), 32),
                   "are_cases_required": row.get("AreCasesRequired")
                   if isinstance(row.get("AreCasesRequired"), bool) else None,
                   "box_contents_source": _text(row.get("BoxContentsSource"), 32),
                   "confirmed_need_by_date": _text(row.get("ConfirmedNeedByDate"), 32)},
    }


def map_inbound_item(row: Mapping[str, Any], shipment: str, marketplace: str
                     ) -> dict[str, Any]:
    """One ``InboundShipmentItem``. Quantities are units; a case quantity stays apart."""
    row_shipment = amazon_shipment_id(row.get("ShipmentId")) or shipment
    sku = _text(row.get("SellerSKU"), 64)
    shipped = _decimal(row.get("QuantityShipped"))
    received = _decimal(row.get("QuantityReceived"))
    in_case = _decimal(row.get("QuantityInCase"))
    return {
        "record_type": "InboundShipmentLine", "ontology_type": "ShipmentLine", "source": SOURCE,
        "citation": _citation(marketplace, "inbound", shipment, "items", sku or "?"),
        # A line whose own ShipmentId names another shipment is kept apart, never joined.
        "join_keys": {"amazon_shipment_id": shipment if row_shipment == shipment else None,
                      "seller_sku": sku},
        "properties": {"shipped_quantity": format(shipped, "f") if shipped is not None
                       else None},
        "amazon": {"fnsku": _text(row.get("FulfillmentNetworkSKU"), 32),
                   "quantity_unit": "unit",
                   "quantity_received": format(received, "f") if received is not None
                   else None,
                   "quantity_in_case": format(in_case, "f") if in_case is not None else None},
    }


def map_inbound_labels(payload: Mapping[str, Any], shipment: str, marketplace: str,
                       page_type: str, label_type: str) -> dict[str, Any]:
    """A getLabels ``LabelDownloadURL``: presence only. The URL is a bearer credential to
    the document for its lifetime, so it never enters a record."""
    url = payload.get("DownloadURL")
    return {
        "record_type": "InboundShipmentLabels", "ontology_type": None, "source": SOURCE,
        "citation": _citation(marketplace, "inbound", shipment, "labels", label_type),
        "join_keys": {"amazon_shipment_id": shipment},
        "amazon": {"marketplace_id": marketplace, "page_type": page_type,
                   "label_type": label_type,
                   "document_available": isinstance(url, str) and bool(url.strip())},
    }


_INVENTORY_DETAIL = ("fulfillableQuantity", "inboundWorkingQuantity", "inboundShippedQuantity",
                     "inboundReceivingQuantity", "unfulfillableQuantity")


def map_inventory_summary(row: Mapping[str, Any], marketplace: str) -> dict[str, Any]:
    sku = _text(row.get("sellerSku"), 64)
    details = row.get("inventoryDetails") if isinstance(row.get("inventoryDetails"),
                                                        Mapping) else {}
    reserved = details.get("reservedQuantity") if isinstance(details.get("reservedQuantity"),
                                                            Mapping) else {}
    quantities = {name: _decimal(details.get(name)) for name in _INVENTORY_DETAIL}
    quantities["reservedQuantity"] = _decimal(reserved.get("totalReservedQuantity"))
    quantities["totalQuantity"] = _decimal(row.get("totalQuantity"))
    return {
        "record_type": "FbaInventorySummary", "ontology_type": None, "source": SOURCE,
        "citation": _citation(marketplace, "inventory", sku or "?"),
        "join_keys": {"seller_sku": sku},
        "as_of": _text(row.get("lastUpdatedTime"), 32),
        "amazon": {"marketplace_id": marketplace, "fnsku": _text(row.get("fnSku"), 32),
                   "asin": _text(row.get("asin"), 16), "condition": _text(row.get("condition"),
                                                                         32),
                   "quantity_unit": "unit",
                   "quantities": {k: (format(v, "f") if v is not None else None)
                                  for k, v in quantities.items()}},
    }


def map_sku_sales(payload: Sequence[Any], sku: str, marketplace: str, start: str,
                  end: str) -> dict[str, Any]:
    """One SKU's getOrderMetrics ``Total`` row. More than one row, or none, is no total."""
    rows = [r for r in payload if isinstance(r, Mapping)]
    row = rows[0] if len(rows) == 1 else None
    units = _decimal(row.get("unitCount")) if row is not None else None
    return {
        "record_type": "SkuSalesMetric", "ontology_type": None, "source": SOURCE,
        "citation": _citation(marketplace, "sales", sku, f"{start}--{end}"),
        "join_keys": {"seller_sku": sku},
        "window": {"start": start, "end": end, "days": SALES_WINDOW_DAYS},
        "amazon": {"marketplace_id": marketplace,
                   "units_ordered": format(units, "f") if units is not None else None,
                   "order_count": jsonable(_decimal(row.get("orderCount"))) if row else None,
                   "total_sales": _money(row.get("totalSales")) if row else None,
                   "rows": len(rows)},
    }


def join_on_amazon_shipment_id(*sources: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Join records of any sources on their exact ``join_keys.amazon_shipment_id``.

    Conservative identity: only an id that passes the grammar joins; a record naming none
    is ``unjoined``; a record whose source found more than one id (``ambiguous``) never
    joins and is listed apart. Returns ``{joined: {id: {source: [citations]}}, unjoined,
    ambiguous}``; an id seen in one source only is joined to itself and nothing else."""
    joined: dict[str, dict[str, list[str]]] = {}
    unjoined: list[str | None] = []
    ambiguous: list[str | None] = []
    for records in sources:
        for record in records:
            keys = record.get("join_keys") if isinstance(record, Mapping) else None
            if not isinstance(keys, Mapping):
                unjoined.append(None)
                continue
            if keys.get("ambiguous_amazon_shipment_ids"):
                ambiguous.append(record.get("citation"))
                continue
            shipment = amazon_shipment_id(keys.get("amazon_shipment_id"))
            if shipment is None:
                unjoined.append(record.get("citation"))
                continue
            joined.setdefault(shipment, {}).setdefault(str(record.get("source")), []).append(
                record.get("citation"))
    return {"joined": joined, "unjoined": unjoined, "ambiguous": ambiguous}


# ------------------------------------------------------------------ probe

def probe(environ: Mapping[str, str] | None = None, *,
          transport: GuardedTransport | None = None,
          now: datetime | None = None, max_skus: int = MAX_METRIC_SKUS,
          sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """The live check's read: counts and enums only, never a value."""
    env = os.environ if environ is None else environ
    missing = missing_settings(env)
    if missing:
        return {"blocked": "credentials_absent", "missing_settings": missing,
                "requests_sent": 0, "evidence_label": "not-run"}
    try:
        settings = settings_from_env(env)
    except SettingInvalid as exc:
        return {"blocked": "setting_invalid", "invalid_settings": [exc.setting],
                "requests_sent": 0, "evidence_label": "not-run"}
    transport = transport or GuardedTransport()
    reader = SpApiReader(settings, transport, sleep=sleep)
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {"region": settings.region, "marketplaces": len(settings.marketplaces),
                           "evidence_label": ("injected-transport" if transport.sender_injected
                                              else "live-read")}
    try:
        reader.access_token()
        out["token"] = "ok"
        shipments = inventory = items = metric_skus = units_known = with_fc = 0
        complete = True
        for marketplace in settings.marketplaces:
            listed = reader.inbound_shipments(marketplace, updated_after=now - timedelta(days=90),
                                              updated_before=now - timedelta(minutes=5))
            complete = complete and listed["completeness"]["complete"]
            shipments += len(listed["records"])
            with_fc += sum(1 for r in listed["records"] if r["amazon"]["destination_fc"])
            for record in listed["records"][:3]:
                shipment = record["join_keys"]["amazon_shipment_id"]
                if shipment:
                    items += len(reader.inbound_shipment_items(marketplace, shipment))
            summaries = reader.inventory_summaries(marketplace)
            complete = complete and summaries["completeness"]["complete"]
            inventory += len(summaries["records"])
            skus = [r["join_keys"]["seller_sku"] for r in summaries["records"]
                    if r["join_keys"]["seller_sku"]
                    and _ids.valid("amazon_seller_sku", r["join_keys"]["seller_sku"])]
            for sku in skus[:max(0, max_skus - metric_skus)]:
                sales = reader.sku_sales(marketplace, sku, now=now)
                metric_skus += 1
                units_known += sales["amazon"]["units_ordered"] is not None
        out.update(inbound_shipments=shipments, inbound_with_fc=with_fc, inbound_items=items,
                   inventory_skus=inventory, sales_skus_read=metric_skus,
                   sales_skus_with_units=units_known, complete=complete, ok=True)
    except (SourceUnavailable, ReadOnlyGuardError) as exc:
        out.update(ok=False, token=out.get("token", "failed"), error=type(exc).__name__,
                   **{k: v for k, v in failure_diagnostics(exc).items() if v is not None})
        status = getattr(exc, "http_status", None)
        if isinstance(status, int):
            out["http_status"] = status
    out["requests_sent"] = transport.requests_sent
    out["operations_read"] = sorted(transport.operations_read)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["probe"]:
        print("usage: python3 -m integrations.amazon_sp_read probe", file=sys.stderr)
        return 2
    result = probe()
    print(json.dumps(jsonable(result), sort_keys=True))
    return 0 if result.get("ok") or result.get("blocked") else 1


if __name__ == "__main__":
    raise SystemExit(main())
