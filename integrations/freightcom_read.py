"""Freightcom Customer API reader: GET only, test host only (decision
2026-10-19-fba-external-sources).

Every request goes through ``integrations.live_source_guard.GuardedTransport`` (the one
sender) on the closed grammar ``freightcom.*`` of ``live_source_grammar``: the services
list, shipments (bookings), one shipment (carrier, tracking / PRO number, SCAC, documents)
and its tracking events. Rating (``POST /rate``) and booking (``POST /shipment``) are never
sent: the guard refuses every non-GET to the Freightcom host. The host is the test host
``customer-external-api.ssd-test.freightcom.com``; the production host is named by no
decision and is refused by the guard, so a ``FREIGHTCOM_API_BASE_URL`` naming any other
base is ``setting_invalid`` before any I/O.

Settings (names only; values are never printed, logged or committed):
``FREIGHTCOM_API_TOKEN`` (required) and ``FREIGHTCOM_API_BASE_URL`` (optional, the test
host by default).

Licence: Freightcom's API licence (clauses 5(k)-(m), Portal docs/Internal-SOP.md) forbids
piping rate payloads into an LLM. Every record carries ``licence`` saying so; the rate
amount is kept under ``restricted`` and the probe prints counts only.

Paths beyond ``/rate`` are unverified until the ``freightcom.bookings`` live check passes
on a real read; an unexpected shape fails closed (``SourceUnavailable``), never guessed.
"""

from __future__ import annotations

from decimal import Decimal
import json
import os
import re
import sys
from typing import Any, Mapping, Sequence

from ontology import identifiers as _ids

from . import live_source_grammar as grammar
from .amazon_sp_read import amazon_shipment_id
from .http import SourceUnavailable
from .live_source_guard import (GuardedTransport, ReadOnlyGuardError, Secret,
                                failure_diagnostics, jsonable)

SETTING_TOKEN = "FREIGHTCOM_API_TOKEN"
SETTING_BASE_URL = "FREIGHTCOM_API_BASE_URL"
REQUIRED_SETTINGS = (SETTING_TOKEN,)
SETTINGS = (SETTING_TOKEN, SETTING_BASE_URL)
DEFAULT_BASE_URL = f"https://{grammar.FREIGHTCOM_TEST}"
SOURCE = "freightcom"
MAX_DETAIL_READS = 10
LICENCE = {"restricted": True, "clause": "Freightcom API licence 5(k)-(m)",
           "llm_input": "forbidden for rate payloads"}

BINDINGS: dict[str, dict[str, Any]] = {
    "FreightBooking": {"ontology_type": "Shipment",
                       "properties": {"carrier": "carrier", "tracking_number": "tracking_number",
                                      "status": "status", "ship_date": "ship_date"},
                       "join_key": "amazon_shipment_id"},
    "FreightTrackingEvent": {"ontology_type": None, "properties": {},
                             "join_key": "freightcom_shipment_id",
                             "why_unbound": "no ontology tracking-event type yet"},
}

_CURRENCY = re.compile(r"[A-Z]{3}")
_MINOR_UNITS = re.compile(r"-?[0-9]{1,15}")
_SCAC = re.compile(r"[A-Z]{2,4}")


class CredentialsAbsent(SourceUnavailable):
    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        super().__init__("credentials_absent: " + ", ".join(self.missing))


class SettingInvalid(SourceUnavailable):
    def __init__(self, name: str) -> None:
        self.setting = name
        super().__init__(f"setting_invalid: {name}")


def missing_settings(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [name for name in REQUIRED_SETTINGS if not (env.get(name) or "").strip()]


def settings_from_env(environ: Mapping[str, str] | None = None) -> tuple[Secret, str]:
    """``(token, base_url)``; the base must be the test host exactly (no path, no port)."""
    env = os.environ if environ is None else environ
    missing = missing_settings(env)
    if missing:
        raise CredentialsAbsent(missing)
    base = (env.get(SETTING_BASE_URL) or "").strip().rstrip("/") or DEFAULT_BASE_URL
    if base.lower() != DEFAULT_BASE_URL:
        raise SettingInvalid(SETTING_BASE_URL)
    return Secret(env[SETTING_TOKEN].strip()), DEFAULT_BASE_URL


def _text(value: Any, limit: int = 200) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _money(value: Any) -> dict[str, Any] | None:
    """``{currency, value}`` with value an integer string of minor units (cents).

    No currency, or a value that is not a whole number of minor units, is no money: the
    currency is never defaulted and a decimal-point value is not reinterpreted."""
    if not isinstance(value, Mapping):
        return None
    currency, minor = value.get("currency"), value.get("value")
    if isinstance(minor, int) and not isinstance(minor, bool):
        minor = str(minor)
    if not (isinstance(currency, str) and _CURRENCY.fullmatch(currency.strip())
            and isinstance(minor, str) and _MINOR_UNITS.fullmatch(minor.strip())):
        return None
    return {"amount": format(Decimal(minor.strip()).scaleb(-2), "f"),
            "currency": currency.strip()}


def _reference_ids(details: Mapping[str, Any]) -> list[str]:
    """Distinct Amazon shipment ids among the declared reference fields only."""
    found: list[str] = []
    codes = details.get("reference_codes")
    for code in codes if isinstance(codes, list) else []:
        value = code.get("value") if isinstance(code, Mapping) else code
        canonical = amazon_shipment_id(value)
        if canonical and canonical not in found:
            found.append(canonical)
    return found


def _documents(shipment: Mapping[str, Any]) -> dict[str, Any]:
    """Document kinds present; never a URL (they can be pre-signed)."""
    kinds: list[str] = []
    for key in ("labels", "documents"):
        items = shipment.get(key)
        for item in items if isinstance(items, list) else []:
            kind = _text(item.get("type") if isinstance(item, Mapping) else None, 32)
            if kind and kind.lower() not in kinds:
                kinds.append(kind.lower())
    return {"kinds": sorted(kinds), "bol_present": any("bol" in k or "bill" in k for k in kinds)}


def map_booking(body: Mapping[str, Any]) -> dict[str, Any]:
    """One ``GET /shipment/{id}`` body -> a ``FreightBooking`` record (bound to Shipment)."""
    shipment = body.get("shipment") if isinstance(body.get("shipment"), Mapping) else body
    shipment_id = _text(shipment.get("id"), 64)
    if shipment_id is None or not _ids.valid("freightcom_id", shipment_id):
        raise SourceUnavailable("freightcom shipment has no id")
    details = shipment.get("details") if isinstance(shipment.get("details"), Mapping) else {}
    rate = shipment.get("rate") if isinstance(shipment.get("rate"), Mapping) else {}
    amazon_ids = _reference_ids(details)
    scac = _text(shipment.get("scac") or rate.get("carrier_scac"), 8)
    return {
        "record_type": "FreightBooking", "ontology_type": "Shipment", "source": SOURCE,
        "citation": f"freightcom://shipment/{shipment_id}",
        "licence": dict(LICENCE),
        # Conservative identity: exactly one Amazon id in the declared reference fields
        # joins; more than one is ambiguous and never joins.
        "join_keys": {"freightcom_shipment_id": shipment_id,
                      "amazon_shipment_id": amazon_ids[0] if len(amazon_ids) == 1 else None,
                      "ambiguous_amazon_shipment_ids": len(amazon_ids) > 1},
        "properties": {
            "carrier": _text(rate.get("carrier_name") or rate.get("service_id"), 120),
            "tracking_number": _text(shipment.get("primary_tracking_number"), 64),
            "status": _text(shipment.get("state"), 32),
            "ship_date": _text(details.get("expected_ship_date") if not isinstance(
                details.get("expected_ship_date"), Mapping) else None, 32),
        },
        "freightcom": {
            "transaction_number": _text(shipment.get("transaction_number"), 64),
            "pro_number": _text(shipment.get("pro_number"), 64),
            "scac": scac if scac and _SCAC.fullmatch(scac) else None,
            "tracking_numbers": [t for t in (_text(v, 64) for v in shipment.get(
                "tracking_numbers") or [] if isinstance(shipment.get("tracking_numbers"),
                                                        list)) if t],
            "documents": _documents(shipment),
        },
        "restricted": {"freight_cost": _money(rate.get("total"))},
    }


def map_tracking_events(shipment_id: str, body: Mapping[str, Any]) -> list[dict[str, Any]]:
    events = body.get("events")
    if not isinstance(events, list):
        raise SourceUnavailable("freightcom tracking payload invalid")
    out = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        out.append({"record_type": "FreightTrackingEvent", "ontology_type": None,
                    "source": SOURCE, "licence": dict(LICENCE),
                    "citation": f"freightcom://shipment/{shipment_id}/tracking-events",
                    "join_keys": {"freightcom_shipment_id": shipment_id},
                    "freightcom": {"type": _text(event.get("type"), 32),
                                   "when": _text(event.get("when"), 40)}})
    return out


class FreightcomReader:
    def __init__(self, token: Secret, transport: GuardedTransport) -> None:
        self._token = transport.register_secret(token)
        self.transport = transport

    def __repr__(self) -> str:
        return "FreightcomReader()"

    def _get(self, op: str, **params: str) -> Any:
        url = grammar.url(op, {}, **params)
        status, _headers, raw = self.transport.request(
            "GET", url, {"Accept": "application/json", "Authorization": self._token.reveal()})
        if status != 200:
            error = SourceUnavailable(f"{op} GET returned HTTP {status}")
            error.http_status = status  # type: ignore[attr-defined]
            raise error
        try:
            return json.loads(raw, parse_float=Decimal)
        except ValueError:
            raise SourceUnavailable(f"{op} response is not JSON") from None

    def services(self) -> list[Mapping[str, Any]]:
        body = self._get("freightcom.services")
        rows = body if isinstance(body, list) else (
            body.get("services") if isinstance(body, Mapping) else None)
        if not isinstance(rows, list):
            raise SourceUnavailable("freightcom services payload invalid")
        return [r for r in rows if isinstance(r, Mapping)]

    def shipment_ids(self) -> list[str]:
        """Booked shipment ids (unverified list endpoint); malformed ids are dropped."""
        body = self._get("freightcom.shipments")
        rows = body if isinstance(body, list) else (
            body.get("shipments") if isinstance(body, Mapping) else None)
        if not isinstance(rows, list):
            raise SourceUnavailable("freightcom shipments payload invalid")
        ids = []
        for row in rows:
            value = _text(row.get("id") if isinstance(row, Mapping) else row, 64)
            if value and _ids.valid("freightcom_id", value) and value not in ids:
                ids.append(value)
        return ids

    def booking(self, shipment_id: str) -> dict[str, Any]:
        body = self._get("freightcom.shipment", shipment=shipment_id)
        if not isinstance(body, Mapping):
            raise SourceUnavailable("freightcom shipment payload invalid")
        return map_booking(body)

    def tracking_events(self, shipment_id: str) -> list[dict[str, Any]]:
        body = self._get("freightcom.shipment_tracking", shipment=shipment_id)
        if not isinstance(body, Mapping):
            raise SourceUnavailable("freightcom tracking payload invalid")
        return map_tracking_events(shipment_id, body)


def probe(environ: Mapping[str, str] | None = None, *,
          transport: GuardedTransport | None = None,
          max_details: int = MAX_DETAIL_READS) -> dict[str, Any]:
    """The live check's read: counts and enums only, never a value."""
    env = os.environ if environ is None else environ
    missing = missing_settings(env)
    if missing:
        return {"blocked": "credentials_absent", "missing_settings": missing,
                "requests_sent": 0, "evidence_label": "not-run"}
    try:
        token, _base = settings_from_env(env)
    except SettingInvalid as exc:
        return {"blocked": "setting_invalid", "invalid_settings": [exc.setting],
                "requests_sent": 0, "evidence_label": "not-run"}
    transport = transport or GuardedTransport()
    reader = FreightcomReader(token, transport)
    out: dict[str, Any] = {"host": "test", "evidence_label": (
        "injected-transport" if transport.sender_injected else "live-read")}
    try:
        out["services"] = len(reader.services())
        out["token"] = "ok"
        ids = reader.shipment_ids()
        bookings = [reader.booking(i) for i in ids[:max_details]]
        events = sum(len(reader.tracking_events(i)) for i in ids[:max_details])
        out.update(
            shipments=len(ids), bookings_read=len(bookings), tracking_events=events,
            with_tracking_number=sum(1 for b in bookings if b["properties"]["tracking_number"]),
            with_amazon_shipment_id=sum(1 for b in bookings
                                        if b["join_keys"]["amazon_shipment_id"]),
            with_bol=sum(1 for b in bookings if b["freightcom"]["documents"]["bol_present"]),
            complete=len(ids) <= max_details, ok=True)
    except (SourceUnavailable, ReadOnlyGuardError) as exc:
        out.update(ok=False, token=out.get("token", "unverified"), error=type(exc).__name__,
                   **{k: v for k, v in failure_diagnostics(exc).items() if v is not None})
    out["requests_sent"] = transport.requests_sent
    out["operations_read"] = sorted(transport.operations_read)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["probe"]:
        print("usage: python3 -m integrations.freightcom_read probe", file=sys.stderr)
        return 2
    result = probe()
    print(json.dumps(jsonable(result), sort_keys=True))
    return 0 if result.get("ok") or result.get("blocked") else 1


if __name__ == "__main__":
    raise SystemExit(main())
