"""Source mirror values: the default identifiers, row strength rules, mirror errors and
failure classes, audience maps, citations and the value parsers the mirror stores and
matches rows with (split from ``integrations.source_mirror``, which re-exports every
name)."""

from __future__ import annotations

import functools
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import math
import re
import sqlite3
import threading
from typing import Any, Callable, Iterable, Mapping

from ontology import identifiers


_NUMERIC_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_TIMESTAMP = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})"
                        r"(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}(?::?\d{2})?)?$")
_YYYYMMDD = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_SINCE_MODES = ("timestamptz", "timestamp-utc", "yyyymmdd-text")
# mirror_meta name of a copy restored from the capture store (integrations/mirror_capture.py).
PERSISTED_FALLBACK_META = "persisted_fallback"
# A scoped fallback (``integrations/mirror_capture.py``): a small file holding its own
# ``mirror_meta`` and this table, which names the shared mirror (attached read-only) and the
# scope relations; TEMP views over the shared mirror's relation tables show only the scope.
POINTER_TABLE = "mirror_pointer"
# Shared-mirror tables keyed by relation, shown through scope views; capture_state and
# absence_seen follow their capture and scan.
_SCOPED_TABLES = ("absence_scan", "capture", "identifier_index", "persisted_relation",
                  "refresh_failure", "relation_lineage", "relation_soft_delete",
                  "relation_state", "row_current", "row_removal", "row_version", "stage_row",
                  "stage_seen", "sync_progress")


def attach_scoped(conn: sqlite3.Connection) -> list[str] | None:
    """When ``conn``'s file is a scoped fallback (it holds ``POINTER_TABLE``): attach the
    shared mirror it names read-only and shadow its relation tables with TEMP views of the
    scope; returns the scope relations. None for any other file (nothing changed). Staged
    pages and unfinished traversals are never shown (they were never served)."""
    if conn.execute("SELECT 1 FROM main.sqlite_master WHERE name = ?",
                    (POINTER_TABLE,)).fetchone() is None:
        return None
    path, relations = conn.execute(f"SELECT path, relations FROM {POINTER_TABLE}").fetchone()
    scope = json.loads(relations)
    conn.execute("ATTACH DATABASE ? AS shared", (f"file:{path}?mode=ro",))
    have = {row[0] for row in conn.execute("SELECT name FROM shared.sqlite_master "
                                           "WHERE type = 'table'")}
    # A TEMP view cannot take parameters: the scope is written into it as quoted literals.
    quoted = ", ".join("'" + str(name).replace("'", "''") + "'" for name in scope)
    for table in _SCOPED_TABLES:
        if table not in have:
            continue
        where = "0" if table in ("stage_row", "stage_seen", "sync_progress") \
            else f"relation IN ({quoted})"
        conn.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM shared.{table} WHERE {where}")
    if "capture_state" in have:
        # Correlated on the capture's key: a filtered read (the unfinished captures) looks
        # up only its own rows' captures instead of listing every scope capture first.
        conn.execute("CREATE TEMP VIEW capture_state AS SELECT s.* FROM shared.capture_state s "
                     "WHERE EXISTS (SELECT 1 FROM temp.capture c "
                     "WHERE c.capture_id = s.capture_id)")
    if "absence_seen" in have and "absence_scan" in have:
        conn.execute("CREATE TEMP VIEW absence_seen AS SELECT * FROM shared.absence_seen "
                     "WHERE scan_id IN (SELECT scan_id FROM temp.absence_scan)")
    return scope


_WHERE_OPS = ("eq", "prefix", "in", "gte", "lte", "is_null")
# IEEE-754 binary64 round-trips at most 15 significant decimal digits.
_BINARY64_DIGITS = 15
_BASE_CAVEATS = (
    "Rows are the reporting replica of the Portal staging primary (FM/Sage/gold ETL), "
    "not from the UAT database the staging Portal UI writes: not what staff entered "
    "or see in the staging Portal.",
    "Pages are read one at a time from a reporting replica; no capture is an "
    "atomic upstream snapshot.",
    "Deletions are not proven: a row deleted upstream stays until a full "
    "traversal no longer returns it, and even that absence is not proof.",
)

# Exact-match identifier columns per relation, seeded from the approved
# Workbench manifest. Strength tells the caller how much a match means:
# system_document_number is assigned by the owning system; external_reference
# is typed from another party's document and can collide; free_text is not an
# identity at all. ``column.key`` names one key inside a JSON-object column.
DEFAULT_IDENTIFIERS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "public.app_sales_order": (
        ("order_number", "portal_sales_order_number", "system_document_number"),
        # Declared conservatively; each match is re-rated from its own row by
        # ROW_STRENGTH_RULES (Sage/FM-ensure/Portal entry = external_reference,
        # legacy SO-FM-* migration = free_text).
        ("po_reference", "sales_order_po_reference_text", "free_text"),
        ("custom_fields.erpCode", "fm_order_erp_code", "system_document_number"),
        ("custom_fields.custPoCode", "customer_po_number", "external_reference"),
        ("custom_fields.sageOrdNumber", "sage_order_number", "system_document_number"),
        # order_number is rewritten to the BC document No. after drain
        # (business-central/align-portal-so-number.ts); the earlier number stays here.
        ("custom_fields.fmOeNumber", "portal_sales_order_previous_number",
         "system_document_number")),
    "public.app_purchase_order": (
        ("po_number", "portal_purchase_order_number", "system_document_number"),
        ("tracking_number", "carrier_tracking_number", "external_reference")),
    "public.app_receipt": (
        ("receipt_number", "portal_receipt_number", "system_document_number"),
        ("po_reference", "receipt_po_reference", "external_reference"),
        ("vendor_invoice_number", "vendor_invoice_number", "external_reference")),
    "public.app_shipment": (
        ("shipment_code", "portal_shipment_code", "system_document_number"),
        ("tracking_number", "carrier_tracking_number", "external_reference")),
    "public.app_manufacturing_order": (
        ("mo_code", "portal_manufacturing_order_code", "system_document_number"),
        ("lot_number", "lot_number", "lot_number")),
    "public.app_quote": (
        ("quote_number", "portal_quote_number", "system_document_number"),),
    "public.app_product": (
        ("code", "portal_product_code", "product_code"),
        ("erp_code", "erp_item_code", "product_code"),
        ("barcode", "product_barcode", "barcode")),
    "public.app_fg_item": (("code", "portal_fg_item_code", "product_code"),),
    "public.assembly_order": (
        ("nameamo_c", "fm_assembly_order_code", "system_document_number"),
        ("lotno", "lot_number", "lot_number")),
    "public.order_entry": (
        ("erpcode", "fm_order_erp_code", "system_document_number"),
        ("custpocode", "customer_po_number", "external_reference"),
        ("reference", "fm_order_reference_text", "free_text")),
    "public.generated_purchase_order": (
        ("ponumber", "fm_purchase_order_number", "system_document_number"),),
    "public.finished_product": (
        ("codefp_c", "fm_finished_product_code", "product_code"),
        ("codeerp", "erp_item_code", "product_code"),
        ("barcode", "product_barcode", "barcode")),
    "public.bulk_product": (("bulkcode_u", "fm_bulk_product_code", "product_code"),),
    "public.packaging": (
        ("packcode", "fm_packaging_code", "product_code"),
        ("barcode_pack_c", "packaging_barcode", "barcode")),
    "public.raw_material": (
        ("rawcode", "fm_raw_material_code", "product_code"),
        ("barcode_raw_c", "raw_material_barcode", "barcode")),
    "sage.oe_order_header": (
        ("ordnumber", "sage_order_number", "system_document_number"),
        ("ponumber", "customer_po_number", "external_reference"),
        ("invnumber", "sage_invoice_number", "system_document_number")),
    "sage.oe_invoice_header": (
        ("invnumber", "sage_invoice_number", "system_document_number"),
        ("ordnumber", "sage_order_number", "system_document_number"),
        ("ponumber", "customer_po_number", "external_reference"),
        ("shinumber", "sage_shipment_number", "system_document_number")),
    # Portal snapshots of BC invoices (no company column: a match is a lead, never an identity).
    "public.bc_invoice_snapshot": (
        ("number", "bc_sales_invoice_number", "system_document_number"),
        ("external_document_number", "bc_invoice_external_document_number", "external_reference")),
    "public.bc_purchase_invoice_snapshot": (
        ("number", "bc_purchase_invoice_number", "system_document_number"),
        ("order_number", "bc_purchase_order_number", "system_document_number"),
        ("vendor_invoice_number", "vendor_invoice_number", "external_reference")),
    "sage.oe_shipment_header": (
        ("shinumber", "sage_shipment_number", "system_document_number"),
        ("ordnumber", "sage_order_number", "system_document_number"),
        ("ponumber", "customer_po_number", "external_reference"),
        ("shiptrack", "carrier_tracking_number", "external_reference")),
    "sage.po_header": (
        ("ponumber", "sage_purchase_order_number", "system_document_number"),
        ("rqnnumber", "sage_requisition_number", "system_document_number")),
    "sage.po_receipt_header": (
        ("rcpnumber", "sage_receipt_number", "system_document_number"),
        ("ponumber", "sage_purchase_order_number", "system_document_number"),
        ("invnumber", "vendor_invoice_number", "external_reference")),
    "sage.ic_item": (
        ("itemno", "sage_item_number", "product_code"),
        ("fmtitemno", "sage_formatted_item_number", "product_code")),
}
_STRENGTHS = frozenset({"system_document_number", "external_reference", "free_text",
                        "product_code", "lot_number", "barcode"})
IDENTIFIER_NOTES = {
    ("public.app_sales_order", "po_reference"):
        "On legacy migrated rows (migrate-fm-to-portal.sql, order_number SO-FM-*) this "
        "holds FM order_entry.reference text, observed to be person names: a match "
        "there is not a PO identity.",
    ("public.app_sales_order", "custom_fields.custPoCode"):
        "Written only by the legacy migrate-fm-to-portal.sql (FM custpocode at "
        "migration time); no runtime Portal code reads or writes it.",
}

# JavaScript String.prototype.trim also removes U+FEFF, which Python's strip keeps.
_JS_TRIM = re.compile(r"^[\s\ufeff]+|[\s\ufeff]+$")


def _js_string(value: Any) -> str:
    """JavaScript ``String(value)`` for a JSON-decoded value.

    Exact for null, booleans, integers, integral floats below 1e21 and text;
    other floats use Python's repr (JavaScript may format the exponent
    differently, but the text is never empty, which is what callers test).
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return str(int(value)) if value.is_integer() and abs(value) < 1e21 else repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Array.prototype.join: null elements render as "".
        return ",".join("" if item is None else _js_string(item) for item in value)
    return "[object Object]"


def portal_trim_text(value: Any) -> str | None:
    """Portal's ``trimText`` (lib/wms/sage-ship-queue.ts:25): null for null/undefined,
    else ``String(value).trim()`` or null when that is empty.

    Mirrored exactly so a numeric ``sageOrdUniq`` (``70123.0``), a boolean or an
    object counts as Portal counts it; only ``None`` and blank text are empty.
    """
    if value is None:
        return None
    text = _JS_TRIM.sub("", _js_string(value))
    return text or None



LEGACY_SALES_ORDER_PREFIX = "SO-FM-"
PO_REFERENCE_STRENGTH_RULE = (
    "public.app_sales_order.po_reference strength follows the Portal writer of the row "
    "(petralabx/plx-customer-portal staging@8da33e4): legacy migrate-fm-to-portal.sql rows "
    "(id fm-so-*, order_number or custom_fields.fmOeNumber SO-FM-*, or a custPoCode key) "
    "copied FM order_entry.reference text -> free_text; Sage ETL "
    "(etl-sage-distribution-gold.mjs, Sage-backed as lib/wms/sage-ship-queue.ts), FM-ensure "
    "(ensure-sales-order-from-fm-oe.ts, custpocode) and Portal order entry "
    "(sales-order/service.ts, also bridged 'gold-so-bridge' rows; an fmId row without "
    "source is basis fm_linked_without_source) -> external_reference, "
    "which Portal resolves as an order identity only together with the order's customer, "
    "on customer_id plus the normalised PO (staging@40f7802 lib/orders/order-identity.ts "
    "resolveOrderByNumber:98-152, customer-po.ts normalizePoReference; a PO is unique per "
    "customer, so each match carries its customer-scoped identity); an unreadable "
    "custom_fields, missing order_number or another source value -> free_text.")


def _custom_fields_object(row: Mapping[str, Any]) -> dict[str, Any] | None:
    value = row.get("custom_fields")
    if value is None:
        return {}
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def sales_order_po_reference_strength(row: Mapping[str, Any]) -> tuple[str, str]:
    """(strength, basis) of one app_sales_order row's po_reference (PO_REFERENCE_STRENGTH_RULE)."""
    fields = _custom_fields_object(row) if "custom_fields" in row else None
    number = row.get("order_number")
    previous = fields.get("fmOeNumber") if fields else None
    if (str(row.get("id") or "").startswith("fm-so-") or
            any(isinstance(text, str) and text.strip().startswith(LEGACY_SALES_ORDER_PREFIX)
                for text in (number, previous)) or
            (fields is not None and "custPoCode" in fields)):
        return "free_text", "legacy_fm_migration_reference_text"
    if fields is None or not isinstance(number, str) or not number.strip():
        return "free_text", "origin_unknown"
    if (portal_trim_text(fields.get("sageOrdUniq")) or
            portal_trim_text(fields.get("sageSourceOrdUniq")) or
            portal_trim_text(fields.get("source")) == "sage.oe_order_header"):
        return "external_reference", "sage_etl_ponumber"
    source = portal_trim_text(fields.get("source"))
    if source == "fm-oe-ensure":
        return "external_reference", "fm_oe_ensure_custpocode"
    if source is None and portal_trim_text(fields.get("fmId")):
        # Same lineage name as customer_po_coverage.classify_origin for an fmId row
        # without source (review 7f818ce minor); the strength is unchanged.
        return "external_reference", "fm_linked_without_source"
    if source in (None, "gold-so-bridge"):
        return "external_reference", "portal_order_entry"
    return "free_text", "origin_unknown"


# (relation, identifier path) -> rule re-rating each match from its own row.
ROW_STRENGTH_RULES: dict[tuple[str, str], tuple[Callable[[Mapping[str, Any]],
                                                          tuple[str, str]], str]] = {
    ("public.app_sales_order", "po_reference"):
        (sales_order_po_reference_strength, PO_REFERENCE_STRENGTH_RULE),
}


class MirrorError(Exception):
    """A refused or failed mirror operation with a stable ``code``."""

    def __init__(self, code: str, message: str = "", *, status: int | None = None,
                 upstream_code: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status  # the upstream HTTP status behind the failure, if any
        # The service's own error code (``{"error": {"code": ...}}``), if it sent one.
        self.upstream_code = upstream_code
        # Counts of the traversal the failure stopped (pages, rows_received, page_size).
        self.detail: dict[str, Any] = {}
        # Whether a refresh already wrote this failure to ``refresh_failure``: None (not
        # tried), "recorded", or the type of the exception that blocked the write.
        self.failure_record: str | None = None
        # A failure no page size cures on this cursor now, whose traversal is kept: the
        # next run reads the relation last instead of giving it up (``defer_next_run``).
        self.defer_next_run = False


# A catalog read that met an outage (5xx, unreachable, throttled past the policy) is
# answered from that failure, without a request, for this long: through an outage every
# read would otherwise pay the full retry contract again (live 2026-10-05:
# case.unfamiliar_order spent its 1500 s re-reading a catalog that answered 500).
CATALOG_OUTAGE_HOLD_SECONDS = 60.0

# Failure classes a sync run reports per relation, so the next failure is evidence-led.
# HTTP 500 codes the service raises where a *larger* page is the remedy, and those no
# page size can fix (petralabx/plx-customer-portal
# services/workbench-api/src/core/query.mjs@c3ffda3 encodeCursor/pageResult).
_GROW_PAGE_CODES = ("key-run-too-long", "null-key")
_FINAL_PAGE_CODES = ("row-too-large", "key-too-long")
# 503 codes for a database connection only an operator can repair (wrong or expired
# wb_api login, wrong database or host, TLS): a retry reads the same, so they are final
# (docs/runbooks/WORKBENCH-API.md@12db891 "Database errors", TASK-2358). The other
# database-* 503s (unreachable, unavailable, busy, permission-denied during a full load)
# can clear and keep the throttle retries.
_FINAL_DATABASE_CODES = ("database-login-failed", "database-not-found",
                         "database-host-not-found", "database-tls")
MAX_PAGE_SIZE = 5000      # the service's LIMITS.maxLimit
MAX_SYNC_WORKERS = 4      # wb_api allows 5 connections; one stays free for other readers


# A first fill merges its staged rows into ``row_current`` in transactions of about this
# many rows (``_merge_batches``), each committed with the merge's progress, so a killed run
# keeps them and the next continues; one transaction for a 30M-row fill never finishes
# inside a budget and its rollback repeated it every cycle.
MERGE_BATCH_ROWS = 20_000
# A relation whose last capture or in-progress traversal holds at least this many rows is
# synced after every smaller one (``sync``), so it never starves them.
LARGE_RELATION_ROWS = 1_000_000
_FAILURE_CLASSES = {"sync_budget_exhausted": "budget", "paging_defect": "paging_defect",
                    "row_bound": "row_bound", "source_defect": "source_defect",
                    "statement_timeout": "source_defect", "source_throttled": "throttled",
                    "source_contract": "contract", "capture_superseded": "superseded",
                    "mirror_store_busy": "store_busy", "internal_error": "internal_error"}


def failure_class(code: str | None, status: int | None) -> str:
    """One word for why a relation's refresh failed (see ``_FAILURE_CLASSES``)."""
    if code in _FAILURE_CLASSES:
        return _FAILURE_CLASSES[code]
    if code == "source_unavailable":
        if status is not None and status >= 500:
            return "source_defect"
        return "http_client_error" if status is not None else "transport"
    return "other"


# Failure classes (``failure_class``) that are the source's own failure, not the run's
# budget or the local store.
UPSTREAM_FAILURES = frozenset({"source_defect", "http_client_error", "transport", "throttled",
                               "contract", "paging_defect"})
_UPSTREAM_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:\-]{0,63}")


def upstream_failures(runs: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Relation -> HTTP status / error code of each sync run the source itself failed
    (``UPSTREAM_FAILURES``); a budget cut or a local store failure is not upstream."""
    out: dict[str, dict[str, Any]] = {}
    for run in runs:
        if run.get("outcome") != "failed" or run.get("failure_class") not in UPSTREAM_FAILURES:
            continue
        code = run.get("upstream_error_code") or str(run.get("error") or "").partition(":")[0]
        status = run.get("http_status")
        out[str(run["relation"])] = {
            **({"http_status": status} if isinstance(status, int) else {}),
            **({"error_code": code} if isinstance(code, str) and _UPSTREAM_CODE.fullmatch(code)
               else {})}
    return out


class _LockCredit:
    """Lock-wait seconds a sync run may still leave uncharged, shared by its workers."""

    def __init__(self, seconds: float) -> None:
        self._left = float(seconds)
        self._guard = threading.Lock()

    def take(self, seconds: float) -> float:
        """Up to ``seconds`` of the credit; returns the seconds granted."""
        with self._guard:
            granted = max(0.0, min(seconds, self._left))
            self._left -= granted
            return granted

    def refund(self, seconds: float) -> None:
        """Return the unused part of a ``take``."""
        with self._guard:
            self._left += max(0.0, seconds)


# ---------------------------------------------------------------- decimals

def parse_decimal(value: Any) -> dict[str, Any]:
    """Money/quantity reading that keeps the raw value and never hides rounding.

    ``exact`` is the Decimal of the text the API sent. ``normalized`` equals it
    unless the value has a fractional part and more than 15 significant digits,
    which a binary64 float cannot carry meaningfully (for example
    ``"35.099999999999994"``); then it is rounded half-even to 15 significant
    digits and ``changed_by_normalization`` is true. Callers choose which to use
    and cite ``rule``.
    """
    result: dict[str, Any] = {"raw": value, "raw_type": type(value).__name__,
                              "exact": None, "normalized": None,
                              "significant_digits": None,
                              "changed_by_normalization": False}
    if value is None:
        return {**result, "rule": "null"}
    if isinstance(value, bool):
        return {**result, "rule": "not_numeric"}
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return {**result, "rule": "non_finite"}
        text = repr(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            return {**result, "rule": "non_finite"}
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.lower().lstrip("+-") in {"nan", "inf", "infinity"}:
            return {**result, "rule": "non_finite"}
        if not _NUMERIC_TEXT.fullmatch(text):
            return {**result, "rule": "not_numeric"}
    else:
        return {**result, "rule": "not_numeric"}
    try:
        exact = Decimal(text)
    except InvalidOperation:
        return {**result, "rule": "not_numeric"}
    digits = len(exact.as_tuple().digits)
    result.update(exact=_plain(exact), significant_digits=digits)
    if digits > _BINARY64_DIGITS and exact.as_tuple().exponent < 0:
        with localcontext() as context:
            context.prec = _BINARY64_DIGITS
            rounded = +exact
        normalized = _plain(rounded.normalize())
        return {**result, "normalized": normalized,
                "rule": "binary64-artifact-rounded-to-15-significant-digits",
                "changed_by_normalization": Decimal(normalized) != exact}
    return {**result, "normalized": result["exact"], "rule": "exact-decimal"}


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    return "0" if text in {"-0", "+0"} else text


def _load_strict_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("source_mirror_audience_duplicate_key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return _canonical(value)


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str) and _NUMERIC_TEXT.fullmatch(value.strip()):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None


def _norm_identifier(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text or len(text) > 256 or any(ord(char) < 32 for char in text):
        return None
    return text.casefold()


def _identifier_value(row: Mapping[str, Any], path: str) -> Any:
    column, _, field = path.partition(".")
    value = row.get(column)
    if not field:
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value.get(field) if isinstance(value, dict) else None


@functools.lru_cache(maxsize=256)
def _scoped_carrier(relation: str, column: str) -> tuple[str, tuple[str, str] | None] | None:
    """(kind, identity scope) of a column carrying a scoped identifier kind, else None
    (read once per column: the grammar is fixed for the process)."""
    kind = identifiers.carrier_kind(relation, column)
    if kind is None or not identifiers.kind_spec(kind).get("identity_scope"):
        return None
    return kind, identifiers.identity_scope(kind, relation, column)


def _candidate_identity(relation: str, column: str, value: Any,
                        row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The scoped identity one match names, for a column the identifier grammar declares
    a carrier of a kind unique only within a scope (``identity_scope``: a customer PO is
    unique per customer; Portal resolveOrderByNumber matches customer plus normalised PO,
    staging@40f7802 lib/orders/order-identity.ts:98-152). Matches with one ``key`` name
    one identity; ``key`` None names none and says why (``reason``): no scope field is
    declared for the column, the row holds no scope value (the Portal skips the PO match
    without a customer) or the value is not a form of the kind. None for other columns."""
    carrier = _scoped_carrier(relation, column)
    if carrier is None:
        return None
    kind, scope = carrier
    out: dict[str, Any] = {"kind": kind, "scope_field": scope[1] if scope else None}
    key = identifiers.identity_key(
        kind, value, (scope[0], _identifier_value(row, scope[1])) if scope else None)
    if key is not None:
        out["key"] = sha256(json.dumps(key).encode("utf-8")).hexdigest()[:24]
    elif identifiers.normalize(kind, value) is None:
        out.update(key=None, reason="value_not_a_form_of_kind")
    else:
        out.update(key=None, reason="no_scope_value" if scope else "no_scope_field")
    return out


def _order_value(value: Any) -> tuple[int, Any]:
    number = _number(value)
    return (0, number) if number is not None else (1, _text(value))
