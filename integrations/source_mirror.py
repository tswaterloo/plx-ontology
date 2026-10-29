"""Permission-enforcing local mirror of the Portal Workbench reporting API.

Part A of the case runtime (devloop/CASE-RUNTIME.md section 1 and the
``plx.source_catalog`` / ``plx.source_rows`` / ``plx.source_resolve`` tools).

The mirror is a disposable SQLite projection of approved Workbench API
relations. A relation is filled on first use or by :meth:`SourceMirror.sync`,
which captures every served catalog relation the caller may see, and is
refreshed through the relation's catalog ``since`` column; a relation without
one can only be refreshed by a full traversal. Pages come from a reporting
replica, so no capture is an atomic upstream snapshot and absence of a row
never proves a deletion; both facts are recorded on every capture and
reported with every answer.

Paging follows the service exactly (petralabx/plx-customer-portal
``services/workbench-api/src/core/query.mjs`` and ``src/http/app.mjs``): keyset
pages ordered by the catalog key, an opaque cursor, ``since`` as ``>=``,
``columns=`` projections that always carry the key, at most 5000 rows and 5 MB
per page, equal-key runs never split. Each page is committed with the
traversal's cursor, so a failed traversal resumes from the last committed page
instead of starting over. ``wb_api`` allows 5 connections and 30 s statements:
each relation reads one page at a time (``sync(workers=)`` reads up to four
relations at once), waits out HTTP 429/503 (``Retry-After`` when sent,
bounded), and halves the page size after a ``statement-timeout`` or an HTTP 500
(down to ``min_page_size``, then retries with backoff); the 500s whose remedy is
a larger page (``key-run-too-long``, ``null-key``) double it up to 5000; one
cursor is never read again at a size already proven too small or too large, so
when both errors close that window the relation is a ``source_defect``. A
keyset that breaks its contract (a key on a later page again, a cursor that does
not move or does not name the page's last key) is a ``paging_defect``; it and a
traversal past ``max_rows_per_relation`` (``row_bound``: rows counted over the
whole traversal, across the runs it resumes in) are abandoned, never resumed into
a capture stitched from two reads. Every other interrupted
traversal (the time budget, throttling, a 500, a source defect) keeps its
committed pages and resumes on the next run: that is how a relation too large
for one budget completes over several runs.

Absence: a full traversal, or a key-only scan (``columns=`` the key), marks
mirrored rows the source no longer returned with ``missing_from_source_since``.
Such rows are hidden from ``rows`` (unless ``include_missing``) and ``resolve``
but stay readable through their citations with that label; a row the source
returns again is unmarked. Missing is never "deleted".

Source deletions: a relation whose catalog columns include a soft-delete column
declared in ``integrations/source_mirror_ingest.json`` (``soft_delete_rules``:
relation patterns and a column, never a list of relations) asserts its own
deletions. A stored row with a value there is labelled ``deleted_in_source`` (that
value as an instant, basis ``source_soft_delete``; a value that is no instant, or
one later than the capture that read it, gives that capture's time, basis
``source_soft_delete_observed``) and is hidden and readable exactly like a missing
row. Every stretch a key was missing or soft-deleted is kept in ``row_removal``
(``since``, ``until``; ``until`` set when the key returns or is undeleted), so a
past removal is not lost when the record comes back; the ontology projection reads
those intervals (``ontology.project.MirrorCaptures``). A key deleted again while
deleted keeps its interval and its label: the first deletion the mirror read (it never
saw the key held in between), so the label and the interval name one time. An undelete
ends the interval at the row's own update time (its relation's timestamp ``since``
column) when the source serves one inside the interval that is later than the update
time the mirror read while the key was deleted (a restore that leaves it alone is no
undelete time), else at the capture that saw it; ``until_basis`` says which
(``source_updated_at`` or ``capture_observed``, as is every return of a missing key).
``deletions_proven`` stays false: absence never proves a deletion, and a soft delete is
labelled as the source's own assertion.

Replaced rows: an upstream job may delete a record's child rows and insert fresh ids
for them (the Portal ETL re-inserts unreceived purchase-order lines; a receipt upsert
re-inserts its lines). A full capture that finds a key missing looks for its
successor by the row's natural key, derived from the generated Prisma facts
(``ontology/schema/generated/meta/portal_prisma.json``, :func:`replacement_key`),
never from a list of relations: a model owned by a parent (a relation with
``onDelete: Cascade``) is matched on a unique constraint that includes that parent's
columns (even the parent's alone: one row per value at a time), else on all its
foreign-key columns, and never on those when they are the parent's columns alone (a
delete and an add under one parent stay a removal and an addition). A file labelled
under an earlier rule (``mirror_meta`` ``replacement_rule`` other than
:data:`REPLACEMENT_RULE`) drops the labels the rule in force does not infer when a
writer opens it, as does a relation filled from a persisted copy; until then
``ontology.project.MirrorCaptures`` refuses to read its labels. A key missing now whose
natural key a key new since the previous full capture carries is labelled ``replaced_in_source``
with its ``successor`` citation (basis ``source_replacement``) when the pairing is
one to one in both captures, else ``ambiguous_replacement`` with every candidate
citation and no successor (:func:`pair_replacements`). The label rides on the key's
open ``row_removal`` absence, so reviews, mentions and links end there through the
one removal path, and the ontology projection learns the successor from it. A key
with no successor stays ``missing_from_source_since``; an incremental capture never
marks a key missing, so it never infers a replacement.

Lineage (09-CRITIQUE R1): every stored row carries a ``lineage`` derived from
the declared rules in ``integrations/source_mirror_ingest.json`` (schema,
``external_id`` prefixes, ``custom_fields`` markers, Portal bridge ids), so a
staging-native or unclassified gold row is labelled wherever the API exposes
the marker.

Operator CLI (GET only; key from ``PLX_WORKBENCH_API_KEY``, never printed)::

    python3 -m integrations.source_mirror --db M --audience A --principal P sync \
        [--full] [--absence-scan] [--budget-seconds N] [--relation R ...]
    python3 -m integrations.source_mirror --db M --audience A --principal P coverage
    python3 -m integrations.source_mirror --db M --audience A --principal P \
        scan-absence R
    python3 -m integrations.source_mirror --db M --audience A --principal P \
        cite-batch HANDLES.txt

Entitlements: the ``plxwb_`` key is one shared login, so this module enforces
a declared relation-family audience (default deny). Every operation checks the
actor against the audience before it fills, refreshes, reads, ranks or counts
anything. The audience map is configuration, not authority.

Design choices (see the module tests for the executable contract):

* Columns. A relation is mirrored with every catalog-approved column unless the
  operator configures a narrower per-relation projection (key and ``since``
  columns are always kept). Requests never widen or narrow what is stored, so a
  citation always names one exact stored row shape and a later request for
  another column needs no second traversal. Payload is bounded by the row
  bound, the API's own 5 MB page limit and optional projections for wide Sage
  relations.
* Staleness. ``max_staleness`` (default 900 s) is compared with the time since
  the last successful refresh. A stale relation is refreshed before answering:
  incrementally through ``since`` when possible, otherwise by full traversal. A
  failed refresh returns the stored rows marked ``stale`` with the error; only
  a relation that has never been filled fails outright.
* Incremental windows are derived from the maximum ``since`` value seen in the
  rows themselves (capped at the replica's reported time), never from the local
  clock, and re-read an overlap window (default 900 s plus the previous read's
  duration; one day for day-granular Sage dates) so boundary rows are not
  missed. Rows are upserted by key.
* Citations. ``api://<schema.relation>/<url-escaped key JSON>@<generation>-<capture-id>``
  names the capture that first observed that exact row content. Superseded
  contents are retained, so an older citation still resolves to what it cited
  and reports that the row changed since and what the current citation is.
* Lifecycle. Capture ids are local AUTOINCREMENT values, so they are only
  meaningful inside one mirror file. Each mirror file gets a random
  ``generation`` when it is created, and every citation carries it. Deleting
  and rebuilding the mirror starts a new generation: citations minted by the
  old one resolve as ``capture_unknown`` (reason
  ``mirror_generation_mismatch``), never to whatever new content reused the
  capture id. Cases keep their recorded digests and values, but those
  citations can no longer be verified. Legacy handles without a generation
  (``...@<capture-id>``) still parse; they resolve only in a mirror file that
  already held that capture before generations were introduced.
* Binding. The first fill pins the tenant and a digest of the upstream base
  URL in the mirror metadata. Reopening the file for another tenant, or with a
  client for another upstream, refuses every operation
  (``binding_mismatch``); an offline mirror checks only the tenant. The tenant
  compared is the one of the audience in force (a re-read audience file), and
  a client must expose a non-empty ``base_url``.
* Persisted capture (opt-in: ``persisted_store``, a
  ``mirror_capture.MirrorCaptureStore`` the host injects; the case runtime passes
  the one an explicitly set ``PLX_WORKBENCH_STATE_DIR`` names; never for a mirror
  without a client, which cannot check the upstream). When ``rows`` or ``resolve``
  fills a relation this file never held and the live API is out (5xx, 429,
  unreachable; never 401/403/404), that one relation is filled from the newest
  verified complete copy in the capture store (``integrations/mirror_capture.py``)
  for this tenant and upstream: its captures get new local capture ids (their
  reason names the copy) and it is marked stale in ``persisted_relation``.
  Relations this file holds, their citations and the client are untouched.
  Such a relation is read live again once ``max_staleness`` has passed since
  the last attempt; a full traversal then replaces the copied rows and clears
  the mark. Every answer about a stale relation carries the machine-readable
  ``source_freshness`` and the human ``freshness_label``; an answer over
  several relations lists the stale ones in ``stale_relations``. A copy restored
  whole by ``mirror_capture.sync_or_persisted`` carries ``PERSISTED_FALLBACK_META``:
  it opens offline only, never refreshes and labels every answer. Audiences
  apply to copied rows exactly as to fresh ones.
"""

from __future__ import annotations

import argparse
import secrets
import contextlib
import functools
import itertools
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote, unquote

from ontology import identifiers
from ontology.core import LEVELS, Actor

from .http import SourceUnavailable
from .workbench_read_api import PagingDefect, RelationUnavailable, catalog_revision
from .capture_store import bound_wal, checkpoint


AUDIENCE_SCHEMA = "plx-source-mirror-audience/v1"
MAX_ROWS_LIMIT = 500
MAX_ROWS_OFFSET = 1_000_000
MAX_BATCH_HANDLES = 50_000
_UTC = timezone.utc
_RELATION = re.compile(r"^([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")
_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.(?:[a-z][a-z0-9_]*|[a-z0-9_]*\*)$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# An identifier path is a column or one key inside a JSON-object column.
_IDENTIFIER_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
_HANDLE = re.compile(r"^api://([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)/([^/@\s]+)"
                     r"@(?:([0-9a-f]{16})-)?([1-9][0-9]{0,17})$")
_GENERATION = re.compile(r"^[0-9a-f]{16}$")
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
        conn.execute("CREATE TEMP VIEW capture_state AS SELECT * FROM shared.capture_state "
                     "WHERE capture_id IN (SELECT capture_id FROM temp.capture)")
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
# A relation in a sync waits at most LOCK_WAIT_ALLOWANCE_SECONDS in all for another
# connection's write lock, then fails as mirror_store_busy. That wait is not charged to
# the run's time budget while the run's shared credit (LOCK_WAIT_RUN_CAP_SECONDS over
# all its relations) lasts; past it the wait counts against the budget.
LOCK_WAIT_ALLOWANCE_SECONDS = 120.0
LOCK_WAIT_RUN_CAP_SECONDS = 600.0
# A failed refresh's ``refresh_failure`` write waits at most this long for the write
# lock; a record it cannot write is reported (``record_error``), never waited out.
FAILURE_RECORD_WAIT_SECONDS = 5.0
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


# ---------------------------------------------------------------- audience

class AudienceMap:
    """Relation-family audience: pattern -> principals, default deny.

    A pattern is an exact ``schema.relation`` or a trailing-``*`` prefix such as
    ``sage.*`` or ``public.app_*``. The most specific pattern wins (exact name,
    then the longest prefix), so an exact entry with no principals denies one
    relation inside a granted family. Principals never include ``*``.
    """

    def __init__(self, audiences: Mapping[str, Iterable[str]], *, tenant: str,
                 classifications: Mapping[str, str] | None = None,
                 default_classification: str = "internal") -> None:
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("source_mirror_audience_tenant_required")
        if not isinstance(audiences, Mapping):
            raise ValueError("source_mirror_audience_map_invalid")
        if default_classification not in LEVELS:
            raise ValueError("source_mirror_audience_classification_invalid")
        self.tenant = tenant.strip()
        self.default_classification = default_classification
        self._audiences: dict[str, frozenset[str]] = {}
        for pattern, principals in audiences.items():
            _check_pattern(pattern)
            if isinstance(principals, (str, bytes)) or not isinstance(principals, Iterable):
                raise ValueError("source_mirror_audience_principals_invalid")
            values = list(principals)
            if any(not isinstance(item, str) or not item.strip() or item != item.strip()
                   or "*" in item or len(item) > 200 for item in values):
                raise ValueError("source_mirror_audience_principal_invalid")
            self._audiences[pattern] = frozenset(values)
        self._classifications: dict[str, str] = {}
        for pattern, level in (classifications or {}).items():
            _check_pattern(pattern)
            if level not in LEVELS:
                raise ValueError("source_mirror_audience_classification_invalid")
            self._classifications[pattern] = level

    @classmethod
    def from_json(cls, document: Any) -> "AudienceMap":
        allowed = {"schema", "tenant", "audiences", "classifications",
                   "default_classification", "note"}
        if (not isinstance(document, dict) or document.get("schema") != AUDIENCE_SCHEMA
                or set(document) - allowed or not isinstance(document.get("audiences"), dict)):
            raise ValueError("source_mirror_audience_document_invalid")
        classifications = document.get("classifications", {})
        if not isinstance(classifications, dict):
            raise ValueError("source_mirror_audience_document_invalid")
        for principals in document["audiences"].values():
            if not isinstance(principals, list):
                raise ValueError("source_mirror_audience_principals_invalid")
        return cls(document["audiences"], tenant=document.get("tenant"),
                   classifications=classifications,
                   default_classification=document.get("default_classification",
                                                       "internal"))

    @classmethod
    def from_file(cls, path: str | Path) -> "AudienceMap":
        return cls.from_json(_load_strict_json(Path(path).read_bytes()))

    @staticmethod
    def _match(table: Mapping[str, Any], relation: str) -> Any:
        if relation in table:
            return table[relation]
        best = None
        for pattern, value in table.items():
            if pattern.endswith("*") and relation.startswith(pattern[:-1]):
                if best is None or len(pattern) > len(best[0]):
                    best = (pattern, value)
        return None if best is None else best[1]

    def principals_for(self, relation: str) -> frozenset[str]:
        return self._match(self._audiences, relation) or frozenset()

    def classification_for(self, relation: str) -> str:
        return self._match(self._classifications, relation) or self.default_classification

    def grants_any(self, actor: Actor) -> bool:
        """Whether any pattern could make some relation visible to ``actor``."""
        return (isinstance(actor, Actor) and actor.tenant == self.tenant and
                any(principals & actor.principals for principals in self._audiences.values()))

    def allows(self, actor: Actor, relation: str) -> bool:
        if (not isinstance(actor, Actor) or not isinstance(relation, str)
                or _RELATION.fullmatch(relation) is None or actor.tenant != self.tenant):
            return False
        if LEVELS[self.classification_for(relation)] > LEVELS[actor.clearance]:
            return False
        return bool(self.principals_for(relation) & actor.principals)


def _check_pattern(pattern: Any) -> None:
    if not isinstance(pattern, str) or _PATTERN.fullmatch(pattern) is None:
        raise ValueError("source_mirror_audience_pattern_invalid")


def _load_strict_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("source_mirror_audience_duplicate_key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


class _FileAudience:
    """Re-reads the audience file on every use; any read or parse error denies all."""

    def __init__(self, path: Path, tenant: str) -> None:
        self.path = path
        self._tenant = tenant
        self._digest: str | None = None
        self._map = AudienceMap({}, tenant=tenant)
        self.error: str | None = None

    def current(self) -> AudienceMap:
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self._digest, self.error = None, "audience file unreadable: " + type(exc).__name__
            self._map = AudienceMap({}, tenant=self._tenant)
            return self._map
        digest = sha256(raw).hexdigest()
        if digest != self._digest:
            try:
                self._map, self.error = AudienceMap.from_json(_load_strict_json(raw)), None
            except (ValueError, TypeError, UnicodeDecodeError) as exc:
                self._map = AudienceMap({}, tenant=self._tenant)
                self.error = "audience file invalid: " + str(exc)
            self._digest = digest
        return self._map


# ---------------------------------------------------------------- citations

def make_citation(relation: str, key: Mapping[str, Any], capture_id: int,
                  generation: str | None = None) -> str:
    """Citation handle; ``generation`` is the minting mirror's (omitted = legacy form)."""
    if generation is not None and _GENERATION.fullmatch(generation) is None:
        raise ValueError("mirror_generation_invalid")
    tail = f"{generation}-{int(capture_id)}" if generation else str(int(capture_id))
    return f"api://{relation}/{quote(_canonical(dict(key)), safe='')}@{tail}"


def parse_citation(handle: Any) -> tuple[str, str, dict[str, Any], int]:
    """(relation, canonical key JSON, key, capture id) or ValueError."""
    return _parse_handle(handle)[:4]


def citation_generation(handle: Any) -> str | None:
    """The mirror generation a handle names, or None for a legacy handle."""
    return _parse_handle(handle)[4]


def _parse_handle(handle: Any) -> tuple[str, str, dict[str, Any], int, str | None]:
    if not isinstance(handle, str) or len(handle) > 4096:
        raise ValueError("citation_handle_invalid")
    match = _HANDLE.fullmatch(handle)
    if match is None:
        raise ValueError("citation_handle_invalid")
    try:
        key = json.loads(unquote(match[2], errors="strict"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("citation_handle_invalid") from None
    if (not isinstance(key, dict) or not 1 <= len(key) <= 16 or
            any(not isinstance(name, str) or _COLUMN.fullmatch(name) is None
                for name in key) or
            any(isinstance(value, (dict, list, float)) for value in key.values())):
        raise ValueError("citation_handle_invalid")
    return match[1], _canonical(key), key, int(match[4]), match[3]


# ---------------------------------------------------------------- helpers

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_instant(text: Any) -> datetime | None:
    """ISO timestamp; a value without a zone is UTC (catalog mode timestamp-utc)."""
    if not isinstance(text, str):
        return None
    match = _TIMESTAMP.fullmatch(text.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    tz = _UTC
    if zone and zone != "Z":
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        tz = timezone(offset if zone[0] == "+" else -offset)
    try:
        return datetime(int(year), int(month), int(day), int(hour), int(minute),
                        int(second or 0), int((fraction or "0")[:6].ljust(6, "0")),
                        tzinfo=tz).astimezone(_UTC)
    except ValueError:
        return None


def _since_instant(mode: str, value: Any) -> datetime | None:
    if value is None:
        return None
    if mode == "yyyymmdd-text":
        match = _YYYYMMDD.fullmatch(str(value).strip())
        if match is None:
            return None
        try:
            return datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=_UTC)
        except ValueError:
            return None
    return _parse_instant(value)


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


# ---------------------------------------------------------------- ingest config

INGEST_SCHEMA = "plx-source-mirror-ingest/v1"
DEFAULT_INGEST_PATH = Path(__file__).with_name("source_mirror_ingest.json")
# The C0 lineage values (ontology/schema/_format.schema.json $defs.lineage, 09-CRITIQUE
# R1) a Portal mirror row can carry. Like the C0 binding examples, a gold row with
# no exposed origin marker defaults to gold_native_staging (never authority until a
# human confirms it, Q1); its rule id (lineage_rule ``gold_no_origin_marker``)
# says the origin is unproven. A row no rule matches has lineage None.
LINEAGES = frozenset({"fm_bronze", "sage_bronze", "silver_view", "gold_etl_from_fm",
                      "gold_etl_from_sage", "gold_native_staging"})
UNCLASSIFIED = (None, "no_lineage_rule")
_LINEAGE_OPS = ("eq", "prefix", "gte", "in", "is_null", "present")
# The highest ``max_rows_per_relation`` a configuration may set. The bound stops a
# runaway traversal (one that keeps reading rows appended behind its cursor, as the
# Portal ETL's cuid() re-inserts can); it is counted over the whole traversal, across
# the runs a budget-cut traversal resumes in, and must never cut a real relation.
# Measured 2026-10-29 by key-ordered full reads (page size 5000): the largest served
# relation, public.app_manufacturing_order_line, holds 31,658,270 rows (262,608
# natural keys: the ETL's per-run copies), read in 3,608 s (8,776 rows/s). The
# configured bound (source_mirror_ingest.json) is about twice that; the cap leaves
# room to raise it again without letting one traversal stage an unbounded file.
MAX_ROWS_PER_RELATION_CAP = 100_000_000
_SYNC_KEYS = {"page_size": (1, 5000), "min_page_size": (1, 5000),
              "max_rows_per_relation": (1, MAX_ROWS_PER_RELATION_CAP),
              "throttle_attempts": (1, 20),
              "max_retry_after_seconds": (0, 3600)}
# Why a key was not held by its source over a ``row_removal`` interval; the row label
# of a soft delete says how its time was read (the source's value, or the capture).
MISSING_FROM_SOURCE = "missing_from_source"
SOFT_DELETE = "source_soft_delete"
SOFT_DELETE_OBSERVED = "source_soft_delete_observed"
# How a closed ``row_removal`` interval's end was read: the row's own update time, or
# the capture that saw the key held again.
UNTIL_SOURCE_UPDATE = "source_updated_at"
UNTIL_OBSERVED = "capture_observed"
# The label of a missing key whose successor a full capture found (module docstring).
SOURCE_REPLACEMENT = "source_replacement"
AMBIGUOUS_REPLACEMENT = "ambiguous_replacement"
# The rule :func:`replacement_key` applies; a mirror file labelled under another one
# drops, when opened, the labels the rule in force no longer infers, and its labels are
# not read before (``ontology.project.MirrorCaptures``). v2: a unique constraint on the
# cascade parent alone pairs again (v1 dropped it).
REPLACEMENT_RULE = "beyond-cascade-parent/v2"
PRISMA_FACTS_PATH = (Path(__file__).resolve().parent.parent / "ontology" / "schema" /
                     "generated" / "meta" / "portal_prisma.json")


def _check_lineage_rules(rules: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(rules, (str, bytes)) or not isinstance(rules, Sequence):
        raise ValueError("source_mirror_lineage_rules_invalid")
    checked, ids = [], set()
    for rule in rules:
        if (not isinstance(rule, Mapping) or
                set(rule) - {"id", "relations", "when", "lineage", "evidence"} or
                not isinstance(rule.get("id"), str) or not rule["id"] or rule["id"] in ids or
                rule.get("lineage") not in LINEAGES or
                isinstance(rule.get("relations"), (str, bytes)) or
                not isinstance(rule.get("relations"), Sequence) or not rule["relations"]):
            raise ValueError("source_mirror_lineage_rules_invalid")
        for pattern in rule["relations"]:
            _check_pattern(pattern)
        clauses = []
        for clause in rule.get("when", ()):
            if (isinstance(clause, (str, bytes)) or not isinstance(clause, Sequence) or
                    len(clause) != 3 or not isinstance(clause[0], str) or
                    _IDENTIFIER_PATH.fullmatch(clause[0]) is None or
                    clause[1] not in _LINEAGE_OPS):
                raise ValueError("source_mirror_lineage_rules_invalid")
            clauses.append((clause[0], clause[1], clause[2]))
        ids.add(rule["id"])
        checked.append({"id": rule["id"], "relations": tuple(rule["relations"]),
                        "when": tuple(clauses), "lineage": rule["lineage"]})
    return tuple(checked)


def _check_soft_delete_rules(rules: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(rules, (str, bytes)) or not isinstance(rules, Sequence):
        raise ValueError("source_mirror_soft_delete_rules_invalid")
    checked, ids = [], set()
    for rule in rules:
        if (not isinstance(rule, Mapping) or
                not {"id", "relations", "column"} <= set(rule) <=
                {"id", "relations", "column", "evidence"} or
                not isinstance(rule["id"], str) or not rule["id"] or rule["id"] in ids or
                not isinstance(rule["column"], str) or
                _COLUMN.fullmatch(rule["column"]) is None or
                not isinstance(rule.get("evidence", "-"), str) or
                not rule.get("evidence", "-") or
                isinstance(rule["relations"], (str, bytes)) or
                not isinstance(rule["relations"], Sequence) or not rule["relations"]):
            raise ValueError("source_mirror_soft_delete_rules_invalid")
        for pattern in rule["relations"]:
            _check_pattern(pattern)
        ids.add(rule["id"])
        checked.append({"id": rule["id"], "relations": tuple(rule["relations"]),
                        "column": rule["column"]})
    return tuple(checked)


def soft_delete_columns(rules: Sequence[Mapping[str, Any]], relation: str,
                        columns: Iterable[str]) -> list[str]:
    """The declared soft-delete columns ``relation``'s catalog ``columns`` carry."""
    have = set(columns)
    return sorted({rule["column"] for rule in rules if rule["column"] in have and
                   any(_pattern_matches(p, relation) for p in rule["relations"])})


def _soft_deleted(rows: Iterable[Mapping[str, Any]], columns: Sequence[str],
                  observed: str) -> tuple[str, str] | None:
    """(deleted_in_source, deleted_basis) of one key's rows (``observed``: the capture
    that read them), the earliest when several carry a value; None when no row has a
    value in a soft-delete column. A value that is no instant, or one later than that
    capture, is still a deletion (fail closed), at the capture's time."""
    found = []
    for row in rows:
        for column in columns:
            value = row.get(column)
            if value is None:
                continue
            moment = _parse_instant(value)
            text = None if moment is None else _iso(moment)
            found.append((text, SOFT_DELETE) if text is not None and text <= observed
                         else (observed, SOFT_DELETE_OBSERVED))
    return min(found) if found else None


def _source_updated(rows: Iterable[Mapping[str, Any]], since: Mapping[str, str] | None
                    ) -> str | None:
    """The latest update time one key's rows carry in their relation's ``since`` column
    when it is a timestamp (a date-only column is no time of day), else None."""
    if not since or since.get("mode") not in ("timestamptz", "timestamp-utc"):
        return None
    found = [moment for moment in (_since_instant(since["mode"], row.get(since["column"]))
                                   for row in rows) if moment is not None]
    return _iso(max(found)) if found else None


def replacement_models(facts: Any) -> dict[str, dict[str, Any]]:
    """``public.<table>`` -> (id, unique, relations) of each model in generated Prisma
    facts (``plx-prisma-facts/v1``); ValueError on any other shape."""
    def names(value: Any) -> bool:
        return (isinstance(value, list) and
                all(isinstance(c, str) and _COLUMN.fullmatch(c) for c in value))
    models = facts.get("models") if isinstance(facts, Mapping) else None
    if not isinstance(models, Mapping):
        raise ValueError("source_mirror_prisma_facts_invalid")
    out = {}
    for model in models.values():
        if (not isinstance(model, Mapping) or not isinstance(model.get("table"), str) or
                not names(model.get("id")) or not isinstance(model.get("unique"), list) or
                not all(names(u) for u in model["unique"]) or
                not isinstance(model.get("relations"), list) or
                not all(isinstance(r, Mapping) and names(r.get("columns")) and
                        isinstance(r.get("on_delete", ""), (str, type(None)))
                        for r in model["relations"])):
            raise ValueError("source_mirror_prisma_facts_invalid")
        out[f"public.{model['table']}"] = {
            "id": list(model["id"]), "unique": [list(u) for u in model["unique"]],
            "relations": [{"columns": list(r["columns"]), "on_delete": r.get("on_delete")}
                          for r in model["relations"]]}
    return out


@functools.lru_cache(maxsize=1)
def generated_replacement_models() -> dict[str, dict[str, Any]]:
    """:func:`replacement_models` of the committed generated Prisma facts."""
    return replacement_models(_load_strict_json(PRISMA_FACTS_PATH.read_bytes()))


def stale_replacement_relations(conn: sqlite3.Connection,
                                models: Mapping[str, Mapping[str, Any]],
                                relation: str | None = None) -> list[str]:
    """The relations of mirror file ``conn`` (of ``relation`` alone when given) holding a
    replacement label that :func:`replacement_key` under ``models`` no longer infers, for
    their stored columns (a relation not in ``relation_state`` has none, so no key). The
    one rule a writer clearing stale labels and a read-only reader skipping them apply."""
    return [name for name, columns in conn.execute(
                "SELECT DISTINCT rm.relation, rs.columns_json FROM row_removal rm "
                "LEFT JOIN relation_state rs ON rs.relation = rm.relation "
                "WHERE rm.replaced_basis IS NOT NULL AND (? IS NULL OR rm.relation = ?) "
                "ORDER BY rm.relation", (relation, relation))
            if replacement_key(models, name, json.loads(columns) if columns else ()) is None]


def replacement_key(models: Mapping[str, Mapping[str, Any]], relation: str,
                    columns: Iterable[str]) -> list[str] | None:
    """The columns a successor of a missing ``relation`` row must match, or None when
    no replacement is inferred: the model has no owning parent (a relation with
    ``onDelete: Cascade``), or ``columns`` (the stored ones) lack one of them. The
    natural key is a unique constraint holding an owner's columns and no id column
    (the owner's alone included: the source holds one row per value at a time), else
    every foreign-key column (parent and the rest, pairing one to one only) less an
    unserved ``onDelete: SetNull`` one, never when those are the parent's columns alone
    (an unrelated delete and add under one parent is no replacement)."""
    model = models.get(relation)
    if model is None:
        return None
    owners = [set(r["columns"]) for r in model["relations"]
              if r["on_delete"] == "Cascade" and r["columns"]]
    if not owners:
        return None
    ident = set(model["id"])
    natural = next((u for u in model["unique"] if not ident & set(u)
                    and any(owner <= set(u) for owner in owners)), None)
    if natural is None:
        # An optional link the Portal clears itself (onDelete SetNull) that the source does
        # not serve takes no part: it is unknown on both sides of every pairing, and the
        # one-to-one rule of pair_replacements still holds on the served keys.
        natural = list(dict.fromkeys(
            c for r in model["relations"] for c in r["columns"]
            if r["on_delete"] != "SetNull" or set(r["columns"]) <= set(columns)))
        if any(set(natural) <= owner for owner in owners):
            return None
    return list(natural) if not ident & set(natural) and set(natural) <= set(columns) \
        else None


def natural_text(row: Mapping[str, Any], natural: Sequence[str]) -> str | None:
    """The canonical natural-key values of ``row``, or None when one is null (a null
    never matches, as in a unique constraint)."""
    values = [row.get(column) for column in natural]
    return None if any(value is None for value in values) else _canonical(values)


def pair_replacements(vanished: Mapping[str, str], appeared: Mapping[str, str],
                      kept: Mapping[str, int]) -> dict[str, tuple[str, list[str]]]:
    """Missing key -> (basis, successor keys) for the keys of ``vanished`` (key ->
    natural text, gone since the previous capture) whose natural text a key of
    ``appeared`` (new since then) carries. ``kept``: natural text -> keys held in both
    captures. ``source_replacement`` names the one successor only when the text
    belongs to exactly one key in each capture; any other pairing is
    ``ambiguous_replacement`` with every candidate. A key with no candidate is left
    out (missing, nothing more)."""
    gone = Counter(vanished.values())
    fresh: dict[str, list[str]] = {}
    for key, text in appeared.items():
        fresh.setdefault(text, []).append(key)
    out = {}
    for key, text in vanished.items():
        found = sorted(fresh.get(text, ()))
        if found:
            one = gone[text] == 1 and len(found) == 1 and not kept.get(text)
            out[key] = (SOURCE_REPLACEMENT if one else AMBIGUOUS_REPLACEMENT, found)
    return out


def load_ingest_config(source: str | Path | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The validated ingest configuration (default: the bundled file)."""
    document = (source if isinstance(source, Mapping) else
                _load_strict_json(Path(source or DEFAULT_INGEST_PATH).read_bytes()))
    if (not isinstance(document, Mapping) or document.get("schema") != INGEST_SCHEMA or
            set(document) - {"schema", "note", "sync", "projections", "lineage_rules",
                             "soft_delete_rules"}):
        raise ValueError("source_mirror_ingest_config_invalid")
    sync = document.get("sync", {})
    if not isinstance(sync, Mapping) or set(sync) - set(_SYNC_KEYS):
        raise ValueError("source_mirror_ingest_config_invalid")
    for name, value in sync.items():
        low, high = _SYNC_KEYS[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("source_mirror_ingest_config_invalid")
    projections = document.get("projections", {})
    if not isinstance(projections, Mapping):
        raise ValueError("source_mirror_ingest_config_invalid")
    return {"sync": dict(sync), "projections": {k: list(v) for k, v in projections.items()},
            "lineage_rules": _check_lineage_rules(document.get("lineage_rules", ())),
            "soft_delete_rules": _check_soft_delete_rules(
                document.get("soft_delete_rules", ()))}


def _pattern_matches(pattern: str, relation: str) -> bool:
    return relation == pattern or (pattern.endswith("*") and relation.startswith(pattern[:-1]))


def _lineage_clause(row: Mapping[str, Any], path: str, op: str, value: Any) -> bool:
    cell = _identifier_value(row, path)
    if op == "present":
        return (portal_trim_text(cell) is not None) is bool(value)
    if op == "is_null":
        return (cell is None) is bool(value)
    if cell is None:
        return False
    if op == "gte":
        left, right = _number(cell), _number(value)
        return left is not None and right is not None and left >= right
    return _matches({"v": cell}, "v", op, value)


def classify_lineage(rules: Sequence[Mapping[str, Any]], relation: str,
                     row: Mapping[str, Any]) -> tuple[str, str]:
    """(lineage, rule id) of one row: the first rule whose relations and clauses match."""
    for rule in rules:
        if (any(_pattern_matches(pattern, relation) for pattern in rule["relations"]) and
                all(_lineage_clause(row, *clause) for clause in rule["when"])):
            return rule["lineage"], rule["id"]
    return UNCLASSIFIED


def _rules_digest(rules: Sequence[Mapping[str, Any]], relation: str) -> str:
    return _sha(_canonical([[rule["id"], list(rule["relations"]),
                             [list(clause) for clause in rule["when"]], rule["lineage"]]
                            for rule in rules
                            if any(_pattern_matches(p, relation) for p in rule["relations"])]))


# ---------------------------------------------------------------- mirror

_DDL = """
CREATE TABLE IF NOT EXISTS mirror_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS upstream_catalog (
  id INTEGER PRIMARY KEY CHECK (id = 1), fetched_at TEXT NOT NULL,
  relations_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capture (
  capture_id INTEGER PRIMARY KEY AUTOINCREMENT, relation TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('lazy_fill','full_traversal','incremental')),
  reason TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
  since_sent TEXT, catalog_revision TEXT NOT NULL, columns_json TEXT NOT NULL,
  page_count INTEGER NOT NULL, rows_received INTEGER NOT NULL,
  keys_new INTEGER NOT NULL, keys_changed INTEGER NOT NULL,
  keys_unchanged INTEGER NOT NULL, keys_absent INTEGER NOT NULL,
  health_before_json TEXT NOT NULL, health_after_json TEXT NOT NULL,
  atomic_upstream_snapshot INTEGER NOT NULL DEFAULT 0 CHECK (atomic_upstream_snapshot = 0),
  deletions_proven INTEGER NOT NULL DEFAULT 0 CHECK (deletions_proven = 0));
CREATE TABLE IF NOT EXISTS relation_state (
  relation TEXT PRIMARY KEY, catalog_revision TEXT NOT NULL,
  catalog_entry_json TEXT NOT NULL, key_columns_json TEXT NOT NULL,
  columns_json TEXT NOT NULL, since_column TEXT, since_mode TEXT,
  key_unique INTEGER NOT NULL, last_full_capture_id INTEGER NOT NULL,
  last_full_traversal_at TEXT NOT NULL, last_capture_id INTEGER NOT NULL,
  last_refresh_at TEXT NOT NULL, last_refresh_mode TEXT NOT NULL,
  last_read_seconds REAL NOT NULL, watermark TEXT, watermark_raw TEXT,
  row_count INTEGER NOT NULL, absent_count INTEGER NOT NULL,
  null_since_rows INTEGER NOT NULL, future_since_rows INTEGER NOT NULL,
  health_before_json TEXT NOT NULL, health_after_json TEXT NOT NULL,
  identifier_columns_json TEXT NOT NULL,
  atomic_upstream_snapshot INTEGER NOT NULL DEFAULT 0 CHECK (atomic_upstream_snapshot = 0),
  deletions_proven INTEGER NOT NULL DEFAULT 0 CHECK (deletions_proven = 0));
CREATE TABLE IF NOT EXISTS refresh_failure (
  relation TEXT PRIMARY KEY, failed_at TEXT NOT NULL, error TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS row_current (
  relation TEXT NOT NULL, key_json TEXT NOT NULL, run_index INTEGER NOT NULL,
  run_length INTEGER NOT NULL, row_json TEXT NOT NULL, row_digest TEXT NOT NULL,
  capture_id INTEGER NOT NULL, first_seen_capture INTEGER NOT NULL,
  first_seen_at TEXT NOT NULL, last_seen_capture INTEGER NOT NULL,
  last_seen_at TEXT NOT NULL, absent_since_capture INTEGER,
  missing_from_source_since TEXT, missing_basis TEXT, lineage TEXT, lineage_basis TEXT,
  deleted_in_source TEXT, deleted_basis TEXT,
  PRIMARY KEY (relation, key_json, run_index));
CREATE TABLE IF NOT EXISTS row_removal (
  relation TEXT NOT NULL, key_json TEXT NOT NULL,
  basis TEXT NOT NULL CHECK (basis IN ('missing_from_source','source_soft_delete')),
  since TEXT NOT NULL, until TEXT, replaced_basis TEXT, replaced_by TEXT, until_basis TEXT,
  CHECK (until IS NULL OR until >= since));
CREATE INDEX IF NOT EXISTS idx_row_removal_key ON row_removal(relation, key_json);
CREATE UNIQUE INDEX IF NOT EXISTS idx_row_removal_open ON row_removal(relation, key_json, basis)
  WHERE until IS NULL;
CREATE TABLE IF NOT EXISTS relation_soft_delete (
  relation TEXT PRIMARY KEY, columns_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS row_version (
  relation TEXT NOT NULL, key_json TEXT NOT NULL, capture_id INTEGER NOT NULL,
  run_index INTEGER NOT NULL, run_length INTEGER NOT NULL,
  row_json TEXT NOT NULL, row_digest TEXT NOT NULL,
  last_seen_capture INTEGER NOT NULL, last_seen_at TEXT NOT NULL,
  superseded_by_capture INTEGER NOT NULL, lineage TEXT, lineage_basis TEXT,
  PRIMARY KEY (relation, key_json, capture_id, run_index));
CREATE TABLE IF NOT EXISTS identifier_index (
  norm_value TEXT NOT NULL, relation TEXT NOT NULL, column_name TEXT NOT NULL,
  key_json TEXT NOT NULL, run_index INTEGER NOT NULL,
  PRIMARY KEY (norm_value, relation, column_name, key_json, run_index));
CREATE INDEX IF NOT EXISTS idx_identifier_key ON identifier_index(relation, key_json);
CREATE TABLE IF NOT EXISTS capture_state (
  capture_id INTEGER PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('in_progress','complete','abandoned')),
  note TEXT);
CREATE TABLE IF NOT EXISTS sync_progress (
  relation TEXT PRIMARY KEY, capture_id INTEGER NOT NULL, mode TEXT NOT NULL,
  reason TEXT NOT NULL, since_sent TEXT, catalog_revision TEXT NOT NULL,
  columns_json TEXT NOT NULL, cursor TEXT, page_size INTEGER NOT NULL,
  pages INTEGER NOT NULL, rows_received INTEGER NOT NULL, keys_new INTEGER NOT NULL,
  keys_changed INTEGER NOT NULL, keys_unchanged INTEGER NOT NULL,
  max_run_length INTEGER NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  resumed INTEGER NOT NULL, health_before_json TEXT NOT NULL, owner TEXT);
CREATE TABLE IF NOT EXISTS stage_row (
  relation TEXT NOT NULL, capture_id INTEGER NOT NULL, key_json TEXT NOT NULL,
  run_index INTEGER NOT NULL, run_length INTEGER NOT NULL, row_json TEXT NOT NULL,
  row_digest TEXT NOT NULL, lineage TEXT, lineage_basis TEXT,
  PRIMARY KEY (capture_id, key_json, run_index)) WITHOUT ROWID;
-- The keys a full traversal saw with their content unchanged: they are not staged and
-- their rows are not written (``_apply_page``); only this key is, until ``_finish``.
CREATE TABLE IF NOT EXISTS stage_seen (
  relation TEXT NOT NULL, capture_id INTEGER NOT NULL, key_json TEXT NOT NULL,
  PRIMARY KEY (capture_id, key_json)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS absence_scan (
  scan_id INTEGER PRIMARY KEY AUTOINCREMENT, relation TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('in_progress','complete','abandoned')),
  started_at TEXT NOT NULL, finished_at TEXT, catalog_revision TEXT NOT NULL,
  cursor TEXT, page_size INTEGER NOT NULL, pages INTEGER NOT NULL,
  keys_seen INTEGER NOT NULL, keys_missing INTEGER, keys_returned INTEGER,
  keys_unmirrored INTEGER, resumed INTEGER NOT NULL, health_before_json TEXT NOT NULL,
  health_after_json TEXT);
CREATE INDEX IF NOT EXISTS idx_absence_scan_relation ON absence_scan(relation, scan_id);
CREATE TABLE IF NOT EXISTS absence_seen (
  scan_id INTEGER NOT NULL, key_json TEXT NOT NULL,
  PRIMARY KEY (scan_id, key_json)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS relation_lineage (
  relation TEXT PRIMARY KEY, rules_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS persisted_relation (
  relation TEXT PRIMARY KEY, freshness_json TEXT NOT NULL, attempted_at TEXT NOT NULL);
"""
# Columns added after the first release; ``_migrate`` adds them to older files.
_ADDED_COLUMNS = {
    "row_current": ("missing_from_source_since TEXT", "missing_basis TEXT",
                    "lineage TEXT", "lineage_basis TEXT", "deleted_in_source TEXT",
                    "deleted_basis TEXT"),
    "row_version": ("lineage TEXT", "lineage_basis TEXT"),
    # A missing key's successor label (SOURCE_REPLACEMENT or AMBIGUOUS_REPLACEMENT) and
    # its successor keys with their content captures ([[key, capture id], ...]).
    "row_removal": ("replaced_basis TEXT", "replaced_by TEXT",
                    # UNTIL_SOURCE_UPDATE or UNTIL_OBSERVED once the interval is closed
                    "until_basis TEXT"),
    # The writer that holds an in-progress traversal (NULL on rows written
    # before owners existed; the next refresh takes those over as a resume).
    "sync_progress": ("owner TEXT",),
    # The latest full traversal that saw every row of the relation then present, and
    # when it finished: a row it saw unchanged is not written (``_sighting_sql``).
    "relation_state": ("seen_through_capture INTEGER", "seen_through_at TEXT"),
}
_ADDED_COLUMNS["row_current"] += (
    # The ``seen_through_capture`` in force when a key scan lifted the row's absence:
    # that traversal did not see the row, so only a later one counts as its sighting.
    "sighting_floor INTEGER",)
_ROW_CURRENT_COLUMNS = ("relation, key_json, run_index, run_length, row_json, row_digest, "
                        "capture_id, first_seen_capture, first_seen_at, last_seen_capture, "
                        "last_seen_at, absent_since_capture, missing_from_source_since, "
                        "missing_basis, lineage, lineage_basis, deleted_in_source, "
                        "deleted_basis")


def _sighting_sql(r: str) -> tuple[str, str]:
    """SQL for the last sighting (capture id, time) of a ``row_current`` row ``r``.

    A full traversal writes only the rows whose content or state changed; a row it saw
    unchanged keeps its stored ``last_seen_*`` and the relation records the traversal
    once (``relation_state.seen_through_*``, set only when every row then present was
    seen). A present row's sighting is the later of the two; an absent row's (and one
    whose absence a key scan lifted after that traversal: ``sighting_floor``) is its
    own. Every write that ends a row's presence or supersedes it stores this value first,
    so the stored value is exact wherever the rule no longer applies."""
    applies = (f"s.relation = {r}.relation AND {r}.absent_since_capture IS NULL AND "
               f"s.seen_through_capture > COALESCE({r}.sighting_floor, 0)")
    return (f"MAX({r}.last_seen_capture, COALESCE((SELECT s.seen_through_capture FROM "
            f"relation_state s WHERE {applies}), 0))",
            f"MAX({r}.last_seen_at, COALESCE((SELECT s.seen_through_at FROM "
            f"relation_state s WHERE {applies}), ''))")


_SEEN_CAPTURE, _SEEN_AT = _sighting_sql("row_current")


def _seen(record: sqlite3.Row, part: str) -> Any:
    """A cited record's last sighting (``part``: ``capture`` or ``at``): a current row
    read with ``_sighting_sql`` (``seen_*``), else a version's stored value."""
    if f"seen_{part}" in record.keys():
        return record[f"seen_{part}"]
    return record[f"last_seen_{part}"]


# The replacement label of a key's open absence, for a ``row_current`` row ``{r}``.
_REPLACED_SQL = ("(SELECT json_array(rm.replaced_basis, rm.replaced_by) FROM row_removal rm "
                 "WHERE rm.relation = {r}.relation AND rm.key_json = {r}.key_json AND "
                 "rm.basis = 'missing_from_source' AND rm.until IS NULL AND "
                 "rm.replaced_basis IS NOT NULL) AS replaced")


def _superseded_outcome(progress: Mapping[str, Any]) -> str:
    """What a superseded refresh leaves behind, for its ``capture_superseded`` message.

    Pages it committed before it was superseded are not discarded: they stay with
    the stored traversal, which the refresh that took it over continues (or has
    completed) from them. Only its uncommitted page is dropped.
    """
    committed = progress["pages"] - progress.get("pages_at_start", 0)
    if progress.get("capture_id") is None or committed <= 0:
        return "this refresh stored nothing"
    return (f"this refresh stores no further page; the {committed} page(s) it committed "
            "stay with the traversal the other refresh holds")


class SourceMirror:
    """SQLite mirror of approved Workbench API relations with enforced audiences.

    ``client`` is a ``WorkbenchReadApiClient`` (or ``None`` for an offline
    mirror that can only answer from stored captures). ``audience_map`` is an
    ``AudienceMap``, a plain ``{pattern: principals}`` mapping (for
    ``tenant``), or a path to an audience JSON file that is re-read on every
    operation so a revocation takes effect immediately.
    """

    def __init__(self, path: str | Path, client: Any, audience_map: Any, *,
                 tenant: str = "plx",
                 identifiers: Mapping[str, Sequence[Sequence[str]]] | None = None,
                 projections: Mapping[str, Sequence[str]] | None = None,
                 default_max_staleness_seconds: float = 900,
                 overlap_seconds: float = 900, yyyymmdd_overlap_days: int = 1,
                 max_rows_per_relation: int = 100_000, page_size: int = 5000,
                 catalog_max_age_seconds: float = 3600, read_attempts: int = 2,
                 retry_delay_seconds: float = 2.0, throttle_attempts: int = 6,
                 max_retry_after_seconds: float = 120, min_page_size: int = 100,
                 lineage_rules: Sequence[Mapping[str, Any]] | None = None,
                 soft_delete_rules: Sequence[Mapping[str, Any]] | None = None,
                 prisma_facts: Mapping[str, Any] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 persisted_store: Any = None,
                 _busy_timeout_ms: int = 10000) -> None:
        # The policy a sync worker's sibling connection is opened with (_sibling).
        self._options: dict[str, Any] = dict(
            tenant=tenant, identifiers=identifiers, projections=projections,
            default_max_staleness_seconds=default_max_staleness_seconds,
            overlap_seconds=overlap_seconds, yyyymmdd_overlap_days=yyyymmdd_overlap_days,
            max_rows_per_relation=max_rows_per_relation, page_size=page_size,
            catalog_max_age_seconds=catalog_max_age_seconds, read_attempts=read_attempts,
            retry_delay_seconds=retry_delay_seconds, throttle_attempts=throttle_attempts,
            max_retry_after_seconds=max_retry_after_seconds, min_page_size=min_page_size,
            lineage_rules=lineage_rules, soft_delete_rules=soft_delete_rules,
            prisma_facts=prisma_facts, clock=clock, sleep=sleep, monotonic=monotonic,
            persisted_store=persisted_store)
        # Where the last complete captures are kept (a MirrorCaptureStore; None: never).
        self._persisted_store = persisted_store
        if isinstance(audience_map, AudienceMap):
            self._audience_source: Any = audience_map
        elif isinstance(audience_map, (str, Path)):
            self._audience_source = _FileAudience(Path(audience_map), tenant)
        elif isinstance(audience_map, Mapping):
            self._audience_source = AudienceMap(audience_map, tenant=tenant)
        else:
            raise ValueError("source_mirror_audience_map_invalid")
        for value, low, high in ((default_max_staleness_seconds, 0, 10 * 365 * 86400),
                                 (overlap_seconds, 0, 30 * 86400),
                                 (catalog_max_age_seconds, 0, 30 * 86400),
                                 (retry_delay_seconds, 0, 60),
                                 (max_retry_after_seconds, 0, 3600)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not low <= value <= high):
                raise ValueError("source_mirror_policy_invalid")
        if (not isinstance(yyyymmdd_overlap_days, int) or not 1 <= yyyymmdd_overlap_days <= 31
                or not isinstance(max_rows_per_relation, int)
                or not 1 <= max_rows_per_relation <= MAX_ROWS_PER_RELATION_CAP
                or not isinstance(page_size, int) or not 1 <= page_size <= 5000
                or not isinstance(read_attempts, int) or not 1 <= read_attempts <= 5
                or not isinstance(throttle_attempts, int) or not 1 <= throttle_attempts <= 20
                or not isinstance(min_page_size, int) or not 1 <= min_page_size <= 5000):
            raise ValueError("source_mirror_policy_invalid")
        self._client = client
        base_url = getattr(client, "base_url", None) if client is not None else None
        if client is not None and (not isinstance(base_url, str) or not base_url.strip()):
            # Without an upstream identity the mirror could not pin (or check)
            # which upstream filled it.
            raise ValueError("source_mirror_client_base_url_required")
        # A digest, not the URL: the pin is compared, never displayed.
        self._source_digest = _sha(base_url) if base_url is not None else None
        self._identifiers = self._check_identifiers(
            DEFAULT_IDENTIFIERS if identifiers is None else identifiers)
        self._projections: dict[str, frozenset[str]] = {}
        for name, columns in (projections or {}).items():
            if (not isinstance(name, str) or _RELATION.fullmatch(name) is None or
                    isinstance(columns, (str, bytes)) or not columns or
                    any(not isinstance(col, str) or _COLUMN.fullmatch(col) is None
                        for col in columns)):
                raise ValueError("source_mirror_projection_invalid")
            self._projections[name] = frozenset(columns)
        self.default_max_staleness_seconds = float(default_max_staleness_seconds)
        self._overlap_seconds = float(overlap_seconds)
        self._overlap_days = yyyymmdd_overlap_days
        self._max_rows = max_rows_per_relation
        self._page_size = page_size
        self._catalog_max_age = float(catalog_max_age_seconds)
        self._read_attempts = read_attempts
        self._retry_delay = float(retry_delay_seconds)
        self._throttle_attempts = throttle_attempts
        self._max_retry_after = float(max_retry_after_seconds)
        self._min_page_size = min(min_page_size, page_size)
        self._lineage_rules = (load_ingest_config()["lineage_rules"] if lineage_rules is None
                               else _check_lineage_rules(lineage_rules))
        self._soft_delete_rules = (load_ingest_config()["soft_delete_rules"]
                                   if soft_delete_rules is None
                                   else _check_soft_delete_rules(soft_delete_rules))
        # Natural keys for replaced rows (default: the committed generated Prisma facts).
        self._replacement_models = (generated_replacement_models() if prisma_facts is None
                                    else replacement_models(prisma_facts))
        self._clock = clock or (lambda: datetime.now(_UTC))
        self._sleep = sleep
        self._monotonic = monotonic
        # Seconds waited on HTTP 429/503 during this mirror's lifetime (counts only).
        self.throttle_waits: list[float] = []
        # Relations whose last refresh failed without a ``refresh_failure`` record (the
        # write was blocked), with the blocking exception type: this instance's coverage
        # counts them failed until a refresh succeeds. Another connection cannot see
        # a record that was never written.
        self._unrecorded: dict[str, str] = {}
        # (when, failure) of the last catalog read that met an outage (``_catalog``).
        self._catalog_outage: tuple[datetime, MirrorError] | None = None
        # Seconds the current sync relation waited for another connection's write
        # lock, the part of them not charged to its time budget (_past), and the
        # run's credit and deadline that bound the wait (_begin; None outside a sync).
        self._lock_wait = 0.0
        self._lock_credit = 0.0
        self._lock_scope: tuple[_LockCredit, float | None] | None = None
        self._busy_timeout_ms = int(_busy_timeout_ms)
        path = Path(path)
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout = {int(_busy_timeout_ms)}")
        # A scoped fallback reads the shared mirror through scope views; it is never written
        # (no schema, migration or meta write), and only offline.
        self.scope: list[str] | None = attach_scoped(self._db)
        if self.scope is not None:
            if client is not None:
                self._db.close()
                raise ValueError("source_mirror_persisted_copy_is_offline")
            self._open_scoped()
            return
        self._db.execute("PRAGMA journal_mode = WAL")
        bound_wal(self._db)
        removals_new = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'row_removal'").fetchone() is None
        self._db.executescript(_DDL)
        self._migrate(removals_new)
        self._db.execute("INSERT OR IGNORE INTO mirror_meta VALUES ('schema', "
                         "'plx-source-mirror/v1')")
        with self._write():
            if self._meta("generation") is None:
                # A file filled before generations existed keeps its legacy
                # (generation-less) citations valid up to its last capture.
                legacy = self._db.execute("SELECT MAX(capture_id) FROM capture").fetchone()[0]
                if legacy is not None:
                    self._db.execute("INSERT INTO mirror_meta VALUES "
                                     "('legacy_capture_max', ?)", (str(legacy),))
                self._db.execute("INSERT INTO mirror_meta VALUES ('generation', ?)",
                                 (secrets.token_hex(8),))
            if self._meta("replacement_rule") != REPLACEMENT_RULE:
                self._drop_stale_replacements()
            # A file filled before binding pins existed is pinned when it is
            # first reopened, not at its next capture, so reads in between are
            # already checked. An empty file has nothing to protect yet.
            has_captures = self._db.execute("SELECT 1 FROM capture LIMIT 1").fetchone()
            # While an audience file is unusable its tenant is unknown; the
            # host's configured tenant (validated at startup) is pinned instead
            # so a later edit to another tenant cannot find the file unpinned.
            tenant_now = self._pinnable_tenant() or tenant
            if has_captures is not None:
                # Opening never fails on a mismatch; every operation refuses.
                self._pin_binding(tenant_now, verify=False)
        self.generation: str = self._meta("generation")
        legacy_max = self._meta("legacy_capture_max")
        self._legacy_capture_max = int(legacy_max) if legacy_max is not None else None
        fallback = self._meta(PERSISTED_FALLBACK_META)
        if fallback is not None and client is not None:
            self._db.close()
            raise ValueError("source_mirror_persisted_copy_is_offline")
        # Set on a persisted copy served while the live source is unavailable.
        self.fallback: dict[str, Any] | None = (json.loads(fallback) if fallback is not None
                                                else None)

    def _open_scoped(self) -> None:
        """The read state of a scoped fallback: its generation, legacy marker and the
        fallback freshness it was restored with (its own ``mirror_meta``)."""
        self.generation = self._meta("generation")
        legacy_max = self._meta("legacy_capture_max")
        self._legacy_capture_max = int(legacy_max) if legacy_max is not None else None
        fallback = self._meta(PERSISTED_FALLBACK_META)
        self.fallback = json.loads(fallback) if fallback is not None else None

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Close the connection after a checkpoint of the WAL (no wait), so the
        writes of a sync never stay in a growing WAL file (``capture_store.checkpoint``)."""
        checkpoint(self._db)
        self._db.close()

    def __enter__(self) -> "SourceMirror":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[None]:
        self._begin()
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _begin(self) -> None:
        """``BEGIN IMMEDIATE``. Outside a sync relation a held write lock is waited for
        up to the busy timeout. Inside one (``_lock_scope``) the relation waits at most
        ``LOCK_WAIT_ALLOWANCE_SECONDS`` in all; what the run's credit covers is added to
        ``_lock_credit`` (not charged to the budget) and the charged rest never outlasts
        the budget. A lock not taken in time raises ``mirror_store_busy``, or
        ``sync_budget_exhausted`` when the budget was the tighter bound."""
        self._db.execute("PRAGMA busy_timeout = 0")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
        finally:
            self._db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        if self._lock_scope is None:
            self._db.execute("BEGIN IMMEDIATE")
            return
        credit, deadline = self._lock_scope
        timeout = max(0.0, LOCK_WAIT_ALLOWANCE_SECONDS - self._lock_wait)
        code = "mirror_store_busy"
        reserved = 0.0
        if deadline is not None:
            # The credit this wait may use is taken before waiting, so two workers
            # waiting at once never both count on the same seconds; the unused part
            # goes back afterwards.
            reserved = credit.take(timeout)
            budget_left = max(0.0, deadline - (self._monotonic() - self._lock_credit))
            bound = reserved + budget_left
            if bound < timeout:
                timeout, code = bound, "sync_budget_exhausted"
        started = self._monotonic()
        self._db.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
            if code == "mirror_store_busy":
                message = (f"the mirror file stayed locked past the relation's "
                           f"{LOCK_WAIT_ALLOWANCE_SECONDS:g} s lock-wait allowance")
            else:
                message = "time budget ended waiting for the mirror file's write lock"
            raise MirrorError(code, message + "; the read resumes from its last "
                              "committed page") from None
        finally:
            waited = self._monotonic() - started
            self._lock_wait += waited
            used = min(waited, reserved)
            credit.refund(reserved - used)
            self._lock_credit += used
            self._db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")

    def _past(self, deadline: float | None) -> bool:
        """Whether the time budget has ended for the current relation: the uncharged
        part of its wait for the mirror file's write lock does not count."""
        return deadline is not None and self._monotonic() - self._lock_credit >= deadline

    def _meta(self, name: str) -> str | None:
        row = self._db.execute("SELECT value FROM mirror_meta WHERE name = ?",
                               (name,)).fetchone()
        return None if row is None else row["value"]

    def _drop_stale_replacements(self, relation: str | None = None) -> None:
        """Clear the replacement labels :func:`replacement_key` no longer infers (a
        file labelled under an earlier rule, e.g. on the cascade parent alone) of
        ``relation``, or of the whole file and then record :data:`REPLACEMENT_RULE`;
        call inside ``_write``."""
        stale = stale_replacement_relations(self._db, self._replacement_models, relation)
        self._db.executemany(
            "UPDATE row_removal SET replaced_basis = NULL, replaced_by = NULL "
            "WHERE relation = ?", [(name,) for name in stale])
        if relation is None:
            self._db.execute("INSERT OR REPLACE INTO mirror_meta VALUES "
                             "('replacement_rule', ?)", (REPLACEMENT_RULE,))

    def _migrate(self, removals_new: bool) -> None:
        """Add columns introduced after a file was created (idempotent); a file from
        before ``row_removal`` opens the interval of every key it holds missing."""
        added = set()
        for table, columns in _ADDED_COLUMNS.items():
            have = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column.split()[0] not in have:
                    try:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
                        added.add(f"{table}.{column.split()[0]}")
                    except sqlite3.OperationalError as exc:  # another process won the race
                        if "duplicate column" not in str(exc):
                            raise
        if "row_removal.until_basis" in added:   # every earlier end was a capture's
            self._db.execute("UPDATE row_removal SET until_basis = ? WHERE until IS NOT NULL "
                             "AND until_basis IS NULL", (UNTIL_OBSERVED,))
        if removals_new:
            self._start_removals("absent_since_capture IS NOT NULL AND "
                                 "missing_from_source_since IS NOT NULL", (), None)

    def _pinnable_tenant(self) -> str | None:
        """Tenant of the audience in force, or None while an audience file is unusable."""
        audience = self._audience()
        if isinstance(self._audience_source, _FileAudience) and self._audience_source.error:
            return None
        return audience.tenant

    def _pin_binding(self, tenant: str, *, verify: bool = True) -> None:
        """Pin tenant/upstream (first writer wins) and verify; call inside ``_write``."""
        self._db.execute("INSERT OR IGNORE INTO mirror_meta VALUES ('tenant', ?)", (tenant,))
        if self._source_digest is not None:
            self._db.execute("INSERT OR IGNORE INTO mirror_meta VALUES "
                             "('source_digest', ?)", (self._source_digest,))
        if verify:
            self._check_binding(tenant)

    def _check_binding(self, tenant_now: str | None = None) -> None:
        """Refuse to serve a mirror filled for another tenant or upstream.

        The tenant compared is the one of the audience in force now (an audience
        file is re-read), not the one the mirror was constructed with.
        """
        if tenant_now is None:
            tenant_now = self._audience().tenant
        tenant = self._meta("tenant")
        if tenant is not None and tenant != tenant_now:
            raise MirrorError("binding_mismatch",
                              "this mirror was filled for another tenant; rebuild it "
                              "instead of reopening it")
        source = self._meta("source_digest")
        if (source is not None and self._source_digest is not None
                and source != self._source_digest):
            raise MirrorError("binding_mismatch",
                              "this mirror was filled from another upstream; rebuild it "
                              "instead of reopening it")

    # -- configuration ---------------------------------------------------

    @staticmethod
    def _check_identifiers(config: Mapping[str, Sequence[Sequence[str]]]
                           ) -> dict[str, tuple[tuple[str, str, str], ...]]:
        result = {}
        for relation, specs in config.items():
            if _RELATION.fullmatch(relation) is None or isinstance(specs, (str, bytes)):
                raise ValueError("source_mirror_identifiers_invalid")
            checked = []
            for spec in specs:
                if (isinstance(spec, (str, bytes)) or len(spec) != 3 or
                        _IDENTIFIER_PATH.fullmatch(str(spec[0])) is None or
                        not isinstance(spec[1], str) or not spec[1] or
                        spec[2] not in _STRENGTHS):
                    raise ValueError("source_mirror_identifiers_invalid")
                checked.append((spec[0], spec[1], spec[2]))
            result[relation] = tuple(checked)
        return result

    def _audience(self) -> AudienceMap:
        source = self._audience_source
        return source.current() if isinstance(source, _FileAudience) else source

    def set_audience_map(self, audience_map: AudienceMap | Mapping[str, Iterable[str]],
                         *, tenant: str = "plx") -> None:
        """Replace the in-memory audience; the next operation uses it."""
        self._audience_source = (audience_map if isinstance(audience_map, AudienceMap)
                                 else AudienceMap(audience_map, tenant=tenant))

    def _require_visible(self, actor: Actor, relation: Any) -> AudienceMap:
        if not isinstance(relation, str) or _RELATION.fullmatch(relation) is None:
            raise MirrorError("invalid_relation", "relation must be schema.relation")
        if not isinstance(actor, Actor):
            raise MirrorError("invalid_actor", "actor must be an ontology Actor")
        # One audience read per operation: the allow check and the binding check
        # must judge the same audience, or an edit between them could pass one
        # tenant's binding check and another tenant's allow check.
        audience = self._audience()
        if not audience.allows(actor, relation):
            raise MirrorError("not_visible", f"{relation} is not visible to this actor")
        self._check_binding(audience.tenant)
        return audience

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            raise MirrorError("clock_invalid", "mirror clock must be timezone-aware")
        return moment.astimezone(_UTC)

    def _staleness_limit(self, max_staleness: Any) -> float:
        if max_staleness is None:
            return self.default_max_staleness_seconds
        if (isinstance(max_staleness, bool) or not isinstance(max_staleness, (int, float))
                or not 0 <= max_staleness <= 10 * 365 * 86400):
            raise MirrorError("invalid_max_staleness",
                              "max_staleness must be 0..315360000 seconds")
        return float(max_staleness)

    # -- upstream --------------------------------------------------------

    def _call(self, call: Callable[[], Any], *, page: bool = False,
              split: bool = False) -> Any:
        """One upstream read under the service's retry contract.

        HTTP 429 (Lambda throttling) and 503 (``too-many-connections``, the
        sixth concurrent ``wb_api`` request) wait ``Retry-After`` when the
        response sends one, else an exponential backoff from
        ``retry_delay_seconds``; at most ``throttle_attempts`` tries and never a
        single wait above ``max_retry_after_seconds``. Another 4xx is final, and so is
        a relation the service does not serve (``relation_unavailable``, naming it).
        Any other failure is retried ``read_attempts`` times. On a page read a
        ``statement-timeout`` goes back to the pager at once (a smaller page is
        the remedy) and a rejected cursor restarts the traversal; with ``split``
        (the page is still above the floor) an HTTP 500 goes back to the pager
        the same way. Every final failure carries the upstream HTTP status.
        """
        generic = throttled = 0
        while True:
            try:
                return call()
            except PagingDefect as exc:
                # A broken keyset: the same cursor would break the same way.
                raise MirrorError("paging_defect", str(exc)) from None
            except RelationUnavailable as exc:
                # The service does not serve this relation now; a retry reads the same.
                raise MirrorError("relation_unavailable", str(exc), status=exc.status,
                                  upstream_code=exc.error_code) from None
            except SourceUnavailable as exc:
                status = getattr(exc, "status", None)
                code = getattr(exc, "error_code", None)
                detail = (f" (HTTP {status}{' ' + code if code else ''})"
                          if status is not None else "")
                if page and code == "statement-timeout":
                    raise MirrorError("statement_timeout", f"{exc}{detail}",
                                      status=status, upstream_code=code) from None
                if page and status == 500 and code in _GROW_PAGE_CODES:
                    raise MirrorError("page_too_small", f"{exc}{detail}", status=status,
                                      upstream_code=code) from None
                if page and status == 500 and code in _FINAL_PAGE_CODES:
                    raise MirrorError("source_defect", f"{exc}{detail}", status=status,
                                      upstream_code=code) from None
                if page and split and status == 500:
                    raise MirrorError("page_server_error", f"{exc}{detail}",
                                      status=status, upstream_code=code) from None
                if page and code == "invalid-cursor":
                    raise MirrorError("cursor_rejected", f"{exc}{detail}",
                                      status=status, upstream_code=code) from None
                if status == 503 and code in _FINAL_DATABASE_CODES:
                    raise MirrorError("source_unavailable", f"{exc}{detail}",
                                      status=status, upstream_code=code) from None
                if status in (429, 503):
                    throttled += 1
                    delay = getattr(exc, "retry_after_seconds", None)
                    if delay is None:
                        delay = min(self._max_retry_after,
                                    self._retry_delay * 2 ** (throttled - 1))
                    if delay > self._max_retry_after:
                        raise MirrorError(
                            "source_throttled",
                            f"Retry-After {delay:.0f} s exceeds the "
                            f"{self._max_retry_after:.0f} s policy{detail}",
                            status=status, upstream_code=code) from None
                    if throttled >= self._throttle_attempts:
                        raise MirrorError("source_throttled",
                                          f"still throttled after {throttled} "
                                          f"attempt(s){detail}", status=status,
                                          upstream_code=code) from None
                    self.throttle_waits.append(float(delay))
                    self._sleep(delay)
                    continue
                generic += 1
                if (status is not None and 400 <= status < 500) or \
                        generic >= self._read_attempts:
                    raise MirrorError("source_unavailable", f"{exc}{detail}",
                                      status=status, upstream_code=code) from None
                # Backoff before the same request again: 1x, 2x, 4x retry_delay_seconds.
                self._sleep(self._retry_delay * 2 ** (generic - 1))
            except ValueError as exc:
                raise MirrorError("source_contract", str(exc)) from None

    def _catalog(self, *, force: bool = False) -> tuple[dict[str, Any], str, str | None]:
        """(relations, fetched_at, error). Falls back to the cached catalog on error."""
        cached = self._db.execute(
            "SELECT fetched_at, relations_json FROM upstream_catalog WHERE id = 1").fetchone()
        now = self._now()
        if (cached is not None and not force and
                (now - _parse_instant(cached["fetched_at"])).total_seconds()
                <= self._catalog_max_age):
            return json.loads(cached["relations_json"]), cached["fetched_at"], None
        error = "no Workbench API client is configured"
        self._catalog_failure: MirrorError | None = None
        held = self._catalog_outage
        if self._client is not None and held is not None \
                and 0 <= (now - held[0]).total_seconds() < CATALOG_OUTAGE_HOLD_SECONDS:
            error = f"{held[1].code}: {held[1]}"
            self._catalog_failure = held[1]
        elif self._client is not None:
            try:
                relations = self._call(self._client.relations)
                fetched_at = _iso(now)
                self._db.execute("INSERT OR REPLACE INTO upstream_catalog VALUES (1, ?, ?)",
                                 (fetched_at, _canonical(relations)))
                self._catalog_outage = None
                return relations, fetched_at, None
            except MirrorError as exc:
                error = f"{exc.code}: {exc}"
                self._catalog_failure = exc
                outage = exc.code == "source_throttled" or (
                    exc.code == "source_unavailable"
                    and (exc.status is None or exc.status >= 500))
                self._catalog_outage = (now, exc) if outage else None
        if cached is None:
            raise self._catalog_failed(error)
        return json.loads(cached["relations_json"]), cached["fetched_at"], error

    def _catalog_failed(self, error: str) -> MirrorError:
        """``source_unavailable`` for a failed catalog read, with the upstream HTTP
        status and error code of the failure behind it, if any."""
        failure = getattr(self, "_catalog_failure", None)
        return MirrorError("source_unavailable", error,
                           status=failure.status if failure else None,
                           upstream_code=failure.upstream_code if failure else None)

    def _projection(self, relation: str, entry: Mapping[str, Any]) -> list[str]:
        columns = list(entry["columns"])
        configured = self._projections.get(relation)
        if configured is None:
            return columns
        keep = set(configured) | set(entry.get("key") or [])
        if entry.get("since"):
            keep.add(entry["since"]["column"])
        # A narrower projection never hides the source's own deletion marker.
        keep.update(soft_delete_columns(self._soft_delete_rules, relation, columns))
        # Nor the natural key a replaced row's successor is matched on.
        keep.update(replacement_key(self._replacement_models, relation, columns) or ())
        return [column for column in columns if column in keep]

    @staticmethod
    def _served_entry(relation: str, catalog: Mapping[str, Any]) -> Mapping[str, Any]:
        entry = catalog.get(relation)
        if entry is None:
            raise MirrorError("unknown_relation", f"{relation} is not in the API catalog")
        if entry.get("unavailable") or not entry.get("key"):
            raise MirrorError("relation_unavailable", f"{relation} is not served")
        if entry.get("since") and entry["since"].get("mode") not in _SINCE_MODES:
            raise MirrorError("source_contract", f"unknown since mode for {relation}")
        return entry

    def check_health(self) -> dict[str, Any]:
        """One ``GET /v1/health`` under the retry contract: the replica's time and lag,
        or ``MirrorError`` (with the upstream HTTP status and error code) when the
        source is unreachable, answers non-2xx or is not ready."""
        if self._client is None:
            raise MirrorError("source_unavailable", "no Workbench API client is configured")
        return self._health()

    def source_freshness(self, relation: str | None = None) -> dict[str, Any] | None:
        """The persisted-capture freshness of this whole mirror (a copy restored by
        ``sync_or_persisted``) or of ``relation`` (filled from a copy while the live
        source was out); ``None`` when it is not served from a copy."""
        if self.fallback is not None:
            return dict(self.fallback)
        if relation is None:
            return None
        row = self._db.execute("SELECT freshness_json FROM persisted_relation "
                               "WHERE relation = ?", (relation,)).fetchone()
        return json.loads(row["freshness_json"]) if row is not None else None

    def _health(self) -> dict[str, Any]:
        value = self._call(self._client.health)
        return {"generated_at": value.get("generatedAt"),
                "replay_lag_seconds": value.get("replayLagSeconds")}

    def _read_page(self, relation: str, entry: Mapping[str, Any], columns: list[str],
                   progress: dict[str, Any], deadline: float | None) -> dict[str, Any]:
        """The next page for ``progress``.

        A statement timeout or an HTTP 500 halves the page and retries the same
        cursor, down to ``min_page_size`` (at most log2(page_size / floor) halvings
        per page). At the floor a statement timeout is final and an HTTP 500 gets
        the ordinary retry policy (with backoff); the final error carries the HTTP
        status and the service's error code. The two 500s whose remedy the service
        names as a *larger* page (``key-run-too-long``: an equal-key run longer than
        the page; ``null-key``: the page ends on NULL keys) double the page up to the
        service's 5000-row maximum instead; there, and for ``row-too-large``, the
        relation is a ``source_defect`` no page size repairs.

        Both remedies stay inside this cursor's window: above the largest size that
        was too small, below the smallest that timed out or failed. A step that would
        leave it takes the window's midpoint instead, so one cursor costs at most
        O(log page_size) requests even with no deadline; when no size is left the
        relation is a ``source_defect`` naming both upstream errors.
        """
        too_small, too_large = 0, MAX_PAGE_SIZE + 1
        small_code = large_code = None
        while True:
            if self._past(deadline):
                raise MirrorError("sync_budget_exhausted",
                                  f"time budget ended after {progress['pages']} page(s) of "
                                  f"{relation}; the read resumes from its last committed page")
            size = progress["page_size"]
            try:
                return self._call(lambda: self._client.read_page(
                    relation, entry, columns=columns, cursor=progress["cursor"],
                    since=progress.get("since_sent"), page_size=size), page=True,
                    split=size > self._min_page_size)
            except MirrorError as exc:
                if exc.code == "page_too_small":
                    if size >= MAX_PAGE_SIZE:
                        raise MirrorError("source_defect",
                                          f"{exc} at the largest page ({MAX_PAGE_SIZE} rows)",
                                          status=exc.status,
                                          upstream_code=exc.upstream_code) from None
                    too_small, small_code = size, exc.upstream_code or exc.code
                    target = min(MAX_PAGE_SIZE, size * 2)
                elif (exc.code in ("statement_timeout", "page_server_error") and
                        size > self._min_page_size):
                    too_large, large_code = size, exc.upstream_code or exc.code
                    target = max(self._min_page_size, size // 2)
                else:
                    raise
                if not too_small < target < too_large:
                    target = (too_small + too_large) // 2
                if not too_small < target < too_large:
                    closed = MirrorError(
                        "source_defect",
                        f"no page size reads this cursor of {relation}: {too_small} rows "
                        f"ended in {small_code}, {too_large} rows in {large_code}",
                        status=exc.status, upstream_code=exc.upstream_code)
                    # The window closed on this read; the source may serve the cursor
                    # later, so the traversal stays and the next run defers it.
                    closed.defer_next_run = True
                    raise closed from None
                progress["page_size"] = target

    # -- refresh ---------------------------------------------------------

    def _state(self, relation: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM relation_state WHERE relation = ?",
                                (relation,)).fetchone()

    def _progress(self, relation: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM sync_progress WHERE relation = ?",
                                (relation,)).fetchone()

    def _plan(self, state: sqlite3.Row | None, entry: Mapping[str, Any],
              columns: list[str], force_full: bool) -> tuple[str, str, str | None]:
        if state is None:
            return "lazy_fill", "first use of relation", None
        if force_full:
            return "full_traversal", "full traversal requested", None
        if not entry.get("since"):
            return "full_traversal", "relation has no since column", None
        if _canonical(entry) != state["catalog_entry_json"]:
            return "full_traversal", "catalog entry changed", None
        if json.loads(state["columns_json"]) != columns:
            return "full_traversal", "configured projection changed", None
        if not state["key_unique"]:
            return "full_traversal", "key is not unique in this relation", None
        if not state["watermark"]:
            return "full_traversal", "no since watermark observed yet", None
        watermark = _parse_instant(state["watermark"])
        if entry["since"]["mode"] == "yyyymmdd-text":
            day = watermark.date() - timedelta(days=self._overlap_days)
            since = datetime(day.year, day.month, day.day, tzinfo=_UTC)
        else:
            since = watermark - timedelta(seconds=self._overlap_seconds +
                                          float(state["last_read_seconds"]))
        return "incremental", "stale; since watermark minus overlap", _iso(since)

    def _start(self, relation: str, entry: Mapping[str, Any], columns: list[str],
               mode: str, reason: str, since: str | None,
               planned_against: int | None) -> dict[str, Any]:
        """A new traversal; its capture row is written with the first page.

        ``planned_against`` is the ``relation_state.last_capture_id`` the plan was
        made from (None: no completed capture); the first page is refused if a
        concurrent refresh completed a newer capture meanwhile.
        """
        return {"relation": relation, "capture_id": None, "mode": mode, "reason": reason,
                "since_sent": since, "catalog_revision": catalog_revision(entry),
                "columns_json": _canonical(columns), "cursor": None,
                "page_size": self._page_size, "pages": 0, "rows_received": 0,
                "keys_new": 0, "keys_changed": 0, "keys_unchanged": 0, "max_run_length": 0,
                "started_at": _iso(self._now()), "updated_at": None, "resumed": 0,
                "health_before_json": _canonical(self._health()),
                "owner": secrets.token_hex(8), "planned_against": planned_against}

    def _resume(self, relation: str, entry: Mapping[str, Any], columns: list[str],
                mode: str) -> dict[str, Any] | None:
        """The committed progress of an interrupted traversal, if it can continue.

        It continues when the catalog entry and projection are unchanged and it
        covers what is asked: an interrupted fill or full traversal serves any
        request; an interrupted incremental read does not serve a full one.
        """
        row = self._progress(relation)
        if row is None:
            return None
        progress = dict(row)
        same_shape = (progress["catalog_revision"] == catalog_revision(entry) and
                      progress["columns_json"] == _canonical(columns))
        if same_shape and (progress["mode"] != "incremental" or mode == "incremental"):
            self._health()  # the replica must be ready before reading on
            # Take the traversal over by compare-and-swap on its owner: the
            # writer that held it (crashed or still reading) fails with
            # capture_superseded at its next page or finish.
            owner = secrets.token_hex(8)
            with self._write():
                taken = self._db.execute(
                    """UPDATE sync_progress SET owner = ?, resumed = resumed + 1
                       WHERE relation = ? AND capture_id = ? AND owner IS ? AND pages = ?
                       AND EXISTS (SELECT 1 FROM capture_state s WHERE s.capture_id = ?
                                   AND s.state = 'in_progress')""",
                    (owner, relation, progress["capture_id"], progress["owner"],
                     progress["pages"], progress["capture_id"])).rowcount
            if taken != 1:
                # Nothing read or written yet: the swap is this refresh's first write.
                raise MirrorError("capture_superseded",
                                  "another refresh took over or completed the interrupted "
                                  "traversal first; this refresh stored nothing")
            progress.update(owner=owner, resumed=progress["resumed"] + 1)
            return progress
        self._abandon(progress, "catalog entry or projection changed" if not same_shape
                      else "a full traversal was requested")
        return None

    def _check_owner(self, progress: Mapping[str, Any]) -> None:
        """Inside a write transaction: refuse unless ``progress`` is still the stored
        traversal of its relation.

        A first page (no capture yet) needs no stored traversal and the completed
        capture the plan was made from still the latest; a later page, a finish or
        an abandon needs the same capture, owner and page count with the capture
        still in progress. Otherwise another refresh started, took over or
        completed a traversal, and this writer's rows must never be stored: once a
        capture is complete nothing stored under its id changes, and an older read
        never becomes current over a newer completed capture.
        """
        relation = progress["relation"]
        row = self._progress(relation)
        if progress["capture_id"] is None:
            state = self._state(relation)
            latest = state["last_capture_id"] if state is not None else None
            if latest != progress.get("planned_against"):
                problem = ("another refresh completed a newer capture of this relation "
                           "while this one read its first page")
            elif row is not None:
                problem = "another refresh started a traversal of this relation first"
            else:
                return
        elif row is None or row["capture_id"] != progress["capture_id"]:
            problem = "another refresh completed this traversal"
        elif row["owner"] != progress["owner"] or row["pages"] != progress["pages"]:
            problem = "another refresh took this traversal over"
        else:
            state = self._db.execute("SELECT state FROM capture_state WHERE capture_id = ?",
                                     (progress["capture_id"],)).fetchone()
            if state is not None and state["state"] == "in_progress":
                return
            problem = "another refresh completed this traversal"
        raise MirrorError("capture_superseded", f"{problem}; {_superseded_outcome(progress)}")

    def _abandon(self, progress: Mapping[str, Any], note: str) -> None:
        """Stop an interrupted traversal and drop its staged (never served) pages.

        Only while ``progress`` still describes the stored traversal (same
        capture, owner and page count, capture in progress): a completed
        capture is never marked abandoned.
        """
        with self._write():
            self._check_owner(progress)
            if progress.get("capture_id") is not None:
                self._db.execute("UPDATE capture_state SET state = 'abandoned', note = ? "
                                 "WHERE capture_id = ?", (note, progress["capture_id"]))
                self._db.execute("DELETE FROM stage_row WHERE capture_id = ?",
                                 (progress["capture_id"],))
                self._db.execute("DELETE FROM stage_seen WHERE capture_id = ?",
                                 (progress["capture_id"],))
            self._db.execute("DELETE FROM sync_progress WHERE relation = ?",
                             (progress["relation"],))

    def _refresh(self, relation: str, tenant: str, *, force_full: bool = False,
                 catalog: Mapping[str, Any] | None = None,
                 deadline: float | None = None) -> dict[str, Any]:
        if self._client is None:
            raise MirrorError("source_unavailable", "no Workbench API client is configured")
        if catalog is None:
            catalog, _, error = self._catalog(force=True)
            if error:
                raise self._catalog_failed(error)
        entry = self._served_entry(relation, catalog)
        columns = self._projection(relation, entry)
        state = self._state(relation)
        mode, reason, since = self._plan(state, entry, columns, force_full)
        planned_against = state["last_capture_id"] if state is not None else None
        progress = (self._resume(relation, entry, columns, mode) or
                    self._start(relation, entry, columns, mode, reason, since,
                                planned_against))
        progress["pages_at_start"] = progress["pages"]
        for attempt in range(2):
            try:
                # A traversal whose last page is committed (cursor NULL after at
                # least one page) goes straight to _finish; pages are never replayed.
                while not (progress["pages"] and progress["cursor"] is None):
                    page = self._read_page(relation, entry, columns, progress, deadline)
                    if progress["rows_received"] + len(page["rows"]) > self._max_rows:
                        raise MirrorError("row_bound",
                                          "workbench API relation row bound reached "
                                          f"(max_rows_per_relation={self._max_rows})")
                    self._apply_page(relation, list(entry["key"]), page, progress, tenant)
                break
            except MirrorError as exc:
                exc.detail = {"pages": progress["pages"],
                              "rows_received": progress["rows_received"],
                              "page_size": progress["page_size"]}
                if exc.code in ("paging_defect", "row_bound"):
                    # A traversal that broke the keyset or outgrew the bound can never
                    # complete into a capture of one relation state: resuming it would
                    # stitch its staged pages to a later read. Drop it; the next run
                    # starts over. Every other failure, the time budget included,
                    # keeps the traversal so the next run resumes it.
                    with contextlib.suppress(MirrorError):
                        self._abandon(progress, f"{exc.code}: the traversal cannot complete")
                    raise
                if exc.code != "cursor_rejected" or attempt:
                    raise
                self._abandon(progress, "the source rejected the stored cursor")
                progress = self._start(relation, entry, columns, progress["mode"],
                                       progress["reason"] + "; restarted after the source "
                                       "rejected the stored cursor", progress["since_sent"],
                                       planned_against)
                progress["pages_at_start"] = 0
        capture_id = self._finish(relation, entry, progress, self._health(), tenant)
        self._db.execute("DELETE FROM refresh_failure WHERE relation = ?", (relation,))
        self._unrecorded.pop(relation, None)
        return {"refreshed_now": progress["mode"], "reason": progress["reason"],
                "capture_id": capture_id, "since_sent": progress["since_sent"],
                "rows_received": progress["rows_received"], "pages": progress["pages"],
                "resumed": progress["resumed"], "page_size": progress["page_size"]}

    def _ensure_fresh(self, relation: str, tenant: str, limit: float, *, force: bool = False,
                      force_full: bool = False, allow_fill: bool = True,
                      catalog: Mapping[str, Any] | None = None,
                      deadline: float | None = None, persisted_ok: bool = False
                      ) -> tuple[sqlite3.Row | None, dict[str, Any]]:
        """Fill or refresh when older than ``limit`` seconds (0 always refreshes).

        ``persisted_ok`` (reads only, never a sync): a fill that fails because the
        live source is out fills that relation from the persisted copy
        (``_fill_from_persisted``). A relation filled so is tried live again only
        once ``limit`` has passed since the last attempt, by a full traversal.

        ``tenant`` is that of the audience the calling operation already
        checked; a capture pins and verifies it without reading the audience
        again.
        """
        state = self._state(relation)
        if self.fallback is not None:
            # A persisted copy is never refreshed and never filled.
            if state is None and allow_fill:
                raise MirrorError("missing_mirror", f"{relation} is not in the persisted "
                                  f"capture; {self.fallback['label']}")
            if state is None:
                return None, {"refreshed_now": "none", "reason": "not mirrored"}
            return state, {"refreshed_now": "none", "reason": "persisted capture",
                           "error": self.fallback["label"]}
        if state is None and not allow_fill:
            return None, {"refreshed_now": "none", "reason": "not mirrored"}
        persisted = self._persisted(relation) if state is not None else None
        if state is not None and not force:
            age = (self._now() - _parse_instant(state["last_refresh_at"])).total_seconds()
            if age < limit:
                return state, {"refreshed_now": "none", "reason": "within max_staleness"}
            if persisted is not None and (self._now() - _parse_instant(
                    persisted["attempted_at"])).total_seconds() < limit:
                return state, self._persisted_info(persisted["freshness"])
        try:
            info = self._refresh(relation, tenant, catalog=catalog, deadline=deadline,
                                 force_full=force_full or persisted is not None)
        except MirrorError as exc:
            if exc.code == "binding_mismatch":
                raise  # never serve stored rows past a failed binding check
            # A blocked record never replaces the refresh's own error.
            exc.failure_record = (self._write_failure(relation, f"{exc.code}: {exc}")
                                  or "recorded")
            if exc.failure_record != "recorded":
                self._unrecorded[relation] = exc.failure_record
            if state is None:
                if persisted_ok and self._fill_from_persisted(relation, tenant, exc):
                    return self._state(relation), self._persisted_info(
                        self._persisted(relation)["freshness"])
                raise
            if persisted is not None:
                freshness = self._mark_persisted(relation, persisted["freshness"]
                                                 ["captures_as_of"], exc)
                return self._state(relation) or state, self._persisted_info(freshness)
            # Re-read: a concurrent refresh may have completed a newer capture.
            failed = {"refreshed_now": "none", "reason": "refresh failed",
                      "error": f"{exc.code}: {exc}", "error_code": exc.code,
                      "upstream_error_code": exc.upstream_code, "detail": dict(exc.detail),
                      "defer_next_run": exc.defer_next_run}
            if exc.status is not None:
                failed["http_status"] = exc.status
            if exc.failure_record != "recorded":
                failed["record_error"] = exc.failure_record
            return self._state(relation) or state, failed
        if persisted is not None:  # live answered: the copied rows are replaced
            self._db.execute("DELETE FROM persisted_relation WHERE relation = ?", (relation,))
        return self._state(relation), info

    def _persisted(self, relation: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT freshness_json, attempted_at FROM persisted_relation "
                               "WHERE relation = ?", (relation,)).fetchone()
        return (None if row is None else
                {"freshness": json.loads(row["freshness_json"]),
                 "attempted_at": row["attempted_at"]})

    @staticmethod
    def _persisted_info(freshness: Mapping[str, Any]) -> dict[str, Any]:
        info = {"refreshed_now": "none", "reason": "persisted capture",
                "error": freshness["label"], "error_code": freshness["error_code"],
                "upstream_error_code": None, "detail": {}}
        if freshness.get("http_status") is not None:
            info["http_status"] = freshness["http_status"]
        return info

    def _mark_persisted(self, relation: str, captures_as_of: str,
                        exc: MirrorError) -> dict[str, Any]:
        """Record (again) that ``relation`` is served from a copy because of ``exc``."""
        from .mirror_capture import stale_label
        code = exc.upstream_code or exc.code
        freshness = {"state": "stale", "live": False, "served_from": "persisted_capture",
                     "captures_as_of": captures_as_of, "http_status": exc.status,
                     "error_code": code, "complete": True,
                     "label": stale_label(captures_as_of, exc.status, code)}
        self._db.execute("INSERT OR REPLACE INTO persisted_relation VALUES (?, ?, ?)",
                         (relation, _canonical(freshness), _iso(self._now())))
        return freshness

    def _fill_from_persisted(self, relation: str, tenant: str, exc: MirrorError) -> bool:
        """Fill ``relation`` (never held here) from the newest verified complete copy
        for this tenant and upstream, marked stale with ``exc``. False, and nothing
        changed, when no store is configured, the mirror has no client (it cannot
        check the upstream), the failure is not an outage or no copy verifies."""
        from .capture_mail import CaptureError
        from .mirror_capture import is_outage
        store = self._persisted_store
        if store is None or self._client is None or not is_outage(exc.code, exc.status):
            return False
        try:
            manifest = store.load([relation], tenant=tenant, source_digest=self._source_digest)
            with self._write():
                if self._state(relation) is not None:
                    return True  # a concurrent read filled it meanwhile
                self._import_relation(relation, Path(manifest["path"]))
                self._mark_persisted(relation, manifest["captures_as_of"], exc)
        except (MirrorError, CaptureError, OSError, sqlite3.DatabaseError, KeyError):
            return False
        return True

    def _import_relation(self, relation: str, path: Path) -> None:
        """Copy one relation's completed captures from a persisted copy into this file
        under new local capture ids (call inside ``_write``). Only the relation's own
        rows are written; no other relation, citation or capture changes."""
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        try:
            def columns(table: str) -> list[str]:
                have = {row["name"] for row in src.execute(f"PRAGMA table_info({table})")}
                return [row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")
                        if row["name"] in have]

            mapping: dict[int, int] = {}
            capture_columns = [c for c in columns("capture") if c != "capture_id"]
            for row in src.execute(f"SELECT capture_id, {', '.join(capture_columns)} FROM "
                                   "capture WHERE relation = ? ORDER BY capture_id",
                                   (relation,)):
                values = dict(row)
                values["reason"] = ("restored from the persisted source-mirror capture; " +
                                    values["reason"])
                cursor = self._db.execute(
                    f"INSERT INTO capture ({', '.join(capture_columns)}) VALUES "
                    f"({', '.join('?' for _ in capture_columns)})",
                    [values[c] for c in capture_columns])
                mapping[row["capture_id"]] = cursor.lastrowid
            refs = {"capture_state": ("capture_id",),
                    # Every column holding a capture id of the relation: the sighting
                    # rule (``_sighting_sql``) compares them with each other.
                    "relation_state": ("last_full_capture_id", "last_capture_id",
                                       "seen_through_capture"),
                    "row_current": ("capture_id", "first_seen_capture", "last_seen_capture",
                                    "absent_since_capture", "sighting_floor"),
                    "row_version": ("capture_id", "last_seen_capture",
                                    "superseded_by_capture"),
                    "identifier_index": (), "relation_lineage": (), "row_removal": (),
                    "relation_soft_delete": ()}
            for table, remapped in refs.items():
                names = columns(table)
                if not names:
                    continue   # a table the copy predates (labels are derived again)
                where = ("capture_id IN (SELECT capture_id FROM capture WHERE relation = ?)"
                         if table == "capture_state" else "relation = ?")
                insert = (f"INSERT INTO {table} ({', '.join(names)}) VALUES "
                          f"({', '.join('?' for _ in names)})")
                rows = src.execute(f"SELECT {', '.join(names)} FROM {table} WHERE {where}",
                                   (relation,))
                self._db.executemany(insert, (
                    [mapping[row[c]] if c in remapped and row[c] is not None else row[c]
                     for c in names] for row in rows))
            # A copy labelled under an earlier rule brings no label this one drops.
            self._drop_stale_replacements(relation)
        finally:
            src.close()

    def _apply_page(self, relation: str, key_columns: list[str], page: Mapping[str, Any],
                    progress: dict[str, Any], tenant: str) -> None:
        """Stage one page and commit it together with the traversal's cursor.

        Staged rows are never served: ``_finish`` merges the whole traversal into
        ``row_current`` in one transaction, so readers see the previous completed
        capture until then.
        """
        runs: dict[str, list[tuple[str, str, Mapping[str, Any]]]] = {}
        for row in page["rows"]:
            key_json = _canonical({column: row.get(column) for column in key_columns})
            try:
                row_json = _canonical(row)
            except ValueError:
                raise MirrorError("source_contract", "row contains non-finite JSON") from None
            runs.setdefault(key_json, []).append((_sha(row_json), row_json, row))
        for run in runs.values():
            run.sort(key=lambda item: (item[0], item[1]))
        now_iso = _iso(self._now())
        with self._write():
            self._pin_binding(tenant)
            self._check_owner(progress)
            capture_id = progress["capture_id"]
            if capture_id is None:
                capture_id = self._db.execute(
                    """INSERT INTO capture (relation, mode, reason, started_at, finished_at,
                       since_sent, catalog_revision, columns_json, page_count, rows_received,
                       keys_new, keys_changed, keys_unchanged, keys_absent,
                       health_before_json, health_after_json)
                       VALUES (?,?,?,?,?,?,?,?,0,0,0,0,0,0,?,'{}')""",
                    (relation, progress["mode"], progress["reason"], progress["started_at"],
                     now_iso, progress["since_sent"], progress["catalog_revision"],
                     progress["columns_json"], progress["health_before_json"])).lastrowid
                self._db.execute("INSERT INTO capture_state VALUES (?, 'in_progress', NULL)",
                                 (capture_id,))
            keys = list(runs)
            for start in range(0, len(keys), 500):
                batch = keys[start:start + 500]
                marks = ",".join("?" for _ in batch)
                if self._db.execute(
                        f"SELECT 1 FROM stage_row WHERE capture_id = ? AND key_json IN ({marks}) "
                        f"UNION ALL SELECT 1 FROM stage_seen WHERE capture_id = ? AND key_json "
                        f"IN ({marks}) LIMIT 1",
                        [capture_id, *batch, capture_id, *batch]).fetchone():
                    # The service keeps an equal-key run on one page; a key that
                    # recurs on a later page means a broken page boundary.
                    raise MirrorError("paging_defect", "workbench API duplicate page key")
            unchanged = (self._unchanged_keys(relation, runs, progress, now_iso)
                         if progress["mode"] != "incremental" else set())
            self._db.executemany("INSERT INTO stage_seen VALUES (?, ?, ?)",
                                 [(relation, capture_id, key_json) for key_json in unchanged])
            self._db.executemany(
                "INSERT INTO stage_row VALUES (?,?,?,?,?,?,?,?,?)",
                [(relation, capture_id, key_json, index, len(run), row_json, digest,
                  *classify_lineage(self._lineage_rules, relation, row))
                 for key_json, run in runs.items() if key_json not in unchanged
                 for index, (digest, row_json, row) in enumerate(run)])
            pages = progress["pages"] + 1
            received = progress["rows_received"] + len(page["rows"])
            longest = max([progress["max_run_length"]] + [len(run) for run in runs.values()])
            self._db.execute(
                """UPDATE capture SET finished_at = ?, page_count = ?, rows_received = ?
                   WHERE capture_id = ?""", (now_iso, pages, received, capture_id))
            # A plain INSERT (first page) or an UPDATE of the row _check_owner
            # just matched: never a replace of another writer's progress.
            if progress["capture_id"] is None:
                self._insert_progress(relation, capture_id, progress, page, pages, received,
                                      longest, now_iso)
            else:
                self._db.execute(
                    """UPDATE sync_progress SET cursor = ?, pages = ?, rows_received = ?,
                       max_run_length = ?, updated_at = ? WHERE relation = ?""",
                    (page["next_cursor"], pages, received, longest, now_iso, relation))
        progress.update(capture_id=capture_id, cursor=page["next_cursor"], pages=pages,
                        rows_received=received, max_run_length=longest, updated_at=now_iso)

    def _unchanged_keys(self, relation: str,
                        runs: Mapping[str, list[tuple[str, str, Mapping[str, Any]]]],
                        progress: Mapping[str, Any], now_iso: str) -> set[str]:
        """The keys of a full traversal's page whose stored state the merge would leave
        as it is: present (not absent), the same row digests in the same order, and the
        same soft-delete state (deleted or not; a key deleted again keeps its label).
        Their rows are neither staged nor written: ``stage_seen`` holds the key and the
        relation's ``seen_through_*`` their sighting (``_sighting_sql``)."""
        soft = soft_delete_columns(self._soft_delete_rules, relation,
                                   json.loads(progress["columns_json"]))
        existing = self._existing_runs(relation, list(runs))
        unchanged = set()
        for key_json, run in runs.items():
            old = existing.get(key_json)
            if old is None or old["absent"] or old["digests"] != tuple(i[0] for i in run):
                continue
            deleted = _soft_deleted([i[2] for i in run], soft, now_iso) if soft else None
            if (deleted is None) != (old["deleted"] is None):
                continue
            unchanged.add(key_json)
        return unchanged

    def _insert_progress(self, relation: str, capture_id: int, progress: Mapping[str, Any],
                         page: Mapping[str, Any], pages: int, received: int, longest: int,
                         now_iso: str) -> None:
        """The first page's traversal row (inside ``_apply_page``'s transaction).

        ``_check_owner`` already refused a first page when another traversal of
        the relation is stored; the primary key is the last line of defence, and
        its violation (``UNIQUE constraint failed: sync_progress.relation``) is the
        same supersession, never a raw ``sqlite3`` error escaping the callers'
        ``except MirrorError``. Any other integrity error propagates as a real error.
        """
        try:
            self._db.execute(
                """INSERT INTO sync_progress (relation, capture_id, mode, reason,
                   since_sent, catalog_revision, columns_json, cursor, page_size, pages,
                   rows_received, keys_new, keys_changed, keys_unchanged, max_run_length,
                   started_at, updated_at, resumed, health_before_json, owner)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?,?,?,?,?)""",
                (relation, capture_id, progress["mode"], progress["reason"],
                 progress["since_sent"], progress["catalog_revision"],
                 progress["columns_json"], page["next_cursor"], progress["page_size"],
                 pages, received, longest, progress["started_at"], now_iso,
                 progress["resumed"], progress["health_before_json"], progress["owner"]))
        except sqlite3.IntegrityError as error:
            if "sync_progress.relation" not in str(error):
                raise  # any other constraint (NOT NULL, CHECK, ...) is a real defect
            raise MirrorError("capture_superseded",
                              "another refresh stored its traversal row of this relation "
                              "first; this refresh stored nothing") from None

    def _merge_stage(self, relation: str, capture_id: int, identifier_columns: list[str],
                     now_iso: str, soft_columns: Sequence[str],
                     since: Mapping[str, str] | None = None) -> dict[str, int]:
        """Upsert a completed traversal's staged rows by key (inside ``_finish``); a
        returned key closes its absence interval, and ``soft_columns`` label and open or
        close its soft-delete interval (an undelete at the rows' ``since`` column time
        when it is a timestamp, ``_track_soft_delete``)."""
        counts = {"new": 0, "changed": 0, "unchanged": 0}
        staged = self._db.execute(
            """SELECT key_json, run_index, row_json, row_digest, lineage, lineage_basis
               FROM stage_row WHERE capture_id = ? ORDER BY key_json, run_index""",
            (capture_id,))
        pending: dict[str, list[sqlite3.Row]] = {}

        def flush() -> None:
            existing = self._existing_runs(relation, list(pending))
            for key_json, run in pending.items():
                old = existing.get(key_json)
                rows = [json.loads(r["row_json"]) for r in run] if soft_columns else []
                deleted = _soft_deleted(rows, soft_columns, now_iso) if soft_columns else None
                if old is not None and old["absent"]:
                    self._end_removal(relation, key_json, MISSING_FROM_SOURCE, now_iso)
                undeleted = old is not None and old["deleted"] is not None and deleted is None
                label = self._track_soft_delete(
                    relation, key_json, old["deleted"] if old else None, deleted, now_iso,
                    self._restored_at(relation, key_json, rows, since) if undeleted else None
                ) or (None, None)
                digests = tuple(record["row_digest"] for record in run)
                if old is not None and old["digests"] == digests:
                    counts["unchanged"] += 1
                    self._db.execute(
                        """UPDATE row_current SET last_seen_capture = ?, last_seen_at = ?,
                           absent_since_capture = NULL, missing_from_source_since = NULL,
                           missing_basis = NULL, deleted_in_source = ?, deleted_basis = ?
                           WHERE relation = ? AND key_json = ?""",
                        (capture_id, now_iso, *label, relation, key_json))
                    continue
                first_capture, first_at = capture_id, now_iso
                if old is not None:
                    counts["changed"] += 1
                    first_capture, first_at = old["first_seen_capture"], old["first_seen_at"]
                    self._db.execute(
                        f"""INSERT OR IGNORE INTO row_version (relation, key_json, capture_id,
                           run_index, run_length, row_json, row_digest, last_seen_capture,
                           last_seen_at, superseded_by_capture, lineage, lineage_basis)
                           SELECT relation, key_json, capture_id, run_index, run_length,
                           row_json, row_digest, {_SEEN_CAPTURE}, {_SEEN_AT}, ?,
                           lineage, lineage_basis
                           FROM row_current WHERE relation = ? AND key_json = ?""",
                        (capture_id, relation, key_json))
                    self._db.execute("DELETE FROM row_current WHERE relation = ? AND key_json = ?",
                                     (relation, key_json))
                else:
                    counts["new"] += 1
                self._db.executemany(
                    f"INSERT INTO row_current ({_ROW_CURRENT_COLUMNS}) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?)",
                    [(relation, key_json, index, len(run), record["row_json"],
                      record["row_digest"], capture_id, first_capture, first_at, capture_id,
                      now_iso, record["lineage"], record["lineage_basis"], *label)
                     for index, record in enumerate(run)])
                self._index_key(relation, key_json,
                                [(record["row_digest"], record["row_json"]) for record in run],
                                identifier_columns)
            pending.clear()

        while True:
            chunk = staged.fetchmany(2000)
            if not chunk:
                break
            for record in chunk:
                if record["key_json"] not in pending and len(pending) >= 500:
                    flush()
                pending.setdefault(record["key_json"], []).append(record)
        if pending:
            flush()
        return counts

    def _capture_watermark(self, capture_id: int, since: Mapping[str, str] | None,
                           generated_at: Any) -> tuple[datetime | None, Any, int, int]:
        """(max since instant, its raw value, null/unparseable count, future count)
        over the rows this capture staged."""
        if not since:
            return None, None, 0, 0
        cap = _parse_instant(generated_at)
        best: tuple[datetime, Any] | None = None
        null_count = future = 0
        path = f'$."{since["column"]}"'
        # The rows seen unchanged were not staged: their stored content is what was read.
        for (raw,) in self._db.execute(
                "SELECT json_extract(row_json, ?) FROM stage_row WHERE capture_id = ? "
                "UNION ALL SELECT json_extract(r.row_json, ?) FROM stage_seen v JOIN "
                "row_current r ON r.relation = v.relation AND r.key_json = v.key_json "
                "WHERE v.capture_id = ?", (path, capture_id, path, capture_id)):
            moment = _since_instant(since["mode"], raw)
            if moment is None:
                null_count += 1
            elif cap is not None and moment > cap:
                future += 1
            elif best is None or moment > best[0]:
                best = (moment, raw)
        return (best[0] if best else None, best[1] if best else None, null_count, future)

    def _finish(self, relation: str, entry: Mapping[str, Any], progress: Mapping[str, Any],
                health_after: Mapping[str, Any], tenant: str) -> int:
        """Close a traversal in one transaction: merge the staged pages, mark absence
        (full only), watermark and relation state. All or nothing, so a failure here
        leaves the traversal resumable straight into ``_finish``."""
        capture_id = progress["capture_id"]
        full = progress["mode"] != "incremental"
        prior = self._state(relation)
        health_before = json.loads(progress["health_before_json"])
        # Cap the watermark at the replica time reported after the read; the
        # overlap (which includes the read duration) covers rows committed during it.
        replica_time = health_after.get("generated_at") or health_before.get("generated_at")
        now = self._now()
        now_iso = _iso(now)
        # Keep the index consistent with the columns it was built from; a config
        # change is applied by a whole-relation rebuild (_sync_identifier_index).
        identifier_columns = (json.loads(prior["identifier_columns_json"]) if prior else
                              self._identifier_columns(relation,
                                                       json.loads(progress["columns_json"])))
        columns = json.loads(progress["columns_json"])
        with self._write():
            self._pin_binding(tenant)
            self._check_owner(progress)
            self._sync_lineage(relation, in_transaction=True)
            self._sync_soft_deletes(relation, columns, in_transaction=True)
            top, top_raw, null_since, future = self._capture_watermark(
                capture_id, entry.get("since"), replica_time)
            counts = self._merge_stage(
                relation, capture_id, identifier_columns, now_iso,
                soft_delete_columns(self._soft_delete_rules, relation, columns),
                entry.get("since"))
            absent = 0
            if full:
                counts["unchanged"] += self._lift_seen(relation, capture_id, now_iso)
                # Seen by this traversal = last_seen_capture == capture_id (staged) or its
                # key in stage_seen (seen unchanged); seen by a later capture (another
                # process) = greater. None of them is missing.
                missing = ("relation = ? AND last_seen_capture < ? "
                           "AND absent_since_capture IS NULL AND NOT EXISTS (SELECT 1 FROM "
                           "stage_seen v WHERE v.capture_id = ? AND "
                           "v.key_json = row_current.key_json)")
                params = (relation, capture_id, capture_id)
                absent = self._db.execute(
                    f"SELECT COUNT(DISTINCT key_json) FROM row_current WHERE {missing}",
                    params).fetchone()[0]
                self._start_removals(missing, params, now_iso)
                if absent and prior is not None and prior["last_full_capture_id"]:
                    self._label_replacements(relation, prior["last_full_capture_id"],
                                             columns, missing, capture_id)
                # Their last sighting is stored first: once absent, the relation's
                # seen_through no longer stands for it.
                self._db.execute(
                    f"UPDATE row_current SET last_seen_capture = {_SEEN_CAPTURE}, "
                    f"last_seen_at = {_SEEN_AT}, absent_since_capture = ?, "
                    f"missing_from_source_since = ?, missing_basis = ? WHERE {missing}",
                    (capture_id, now_iso, f"full_traversal:{capture_id}", *params))
            self._db.execute("DELETE FROM stage_row WHERE capture_id = ?", (capture_id,))
            self._db.execute("DELETE FROM stage_seen WHERE capture_id = ?", (capture_id,))
            self._db.execute(
                """UPDATE capture SET finished_at = ?, keys_new = ?, keys_changed = ?,
                   keys_unchanged = ?, keys_absent = ?, health_after_json = ?
                   WHERE capture_id = ?""",
                (now_iso, counts["new"], counts["changed"], counts["unchanged"], absent,
                 _canonical(dict(health_after)), capture_id))
            self._db.execute("UPDATE capture_state SET state = 'complete' WHERE capture_id = ?",
                             (capture_id,))
            watermark, watermark_raw = top, top_raw
            if not full and prior is not None and prior["watermark"]:
                previous = _parse_instant(prior["watermark"])
                if watermark is None or previous >= watermark:
                    watermark, watermark_raw = previous, prior["watermark_raw"]
            present, absent_count = self._db.execute(
                """SELECT COALESCE(SUM(absent_since_capture IS NULL), 0),
                   COALESCE(SUM(absent_since_capture IS NOT NULL), 0)
                   FROM row_current WHERE relation = ?""", (relation,)).fetchone()
            since_entry = entry.get("since") or {}
            runs_unique = progress["max_run_length"] <= 1
            key_unique = runs_unique if full else bool(prior["key_unique"]) and runs_unique
            read_seconds = max(0.0, (now - _parse_instant(progress["started_at"])).total_seconds())
            self._db.execute(
                """INSERT OR REPLACE INTO relation_state (relation, catalog_revision,
                   catalog_entry_json, key_columns_json, columns_json, since_column,
                   since_mode, key_unique, last_full_capture_id, last_full_traversal_at,
                   last_capture_id, last_refresh_at, last_refresh_mode, last_read_seconds,
                   watermark, watermark_raw, row_count, absent_count, null_since_rows,
                   future_since_rows, health_before_json, health_after_json,
                   identifier_columns_json, seen_through_capture, seen_through_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (relation, progress["catalog_revision"], _canonical(entry),
                 _canonical(list(entry["key"])), progress["columns_json"],
                 since_entry.get("column"), since_entry.get("mode"), int(key_unique),
                 capture_id if full else prior["last_full_capture_id"],
                 now_iso if full else prior["last_full_traversal_at"],
                 capture_id, now_iso, progress["mode"], read_seconds,
                 _iso(watermark) if watermark else None,
                 None if watermark_raw is None else str(watermark_raw),
                 present, absent_count,
                 null_since if full else prior["null_since_rows"], future,
                 _canonical(health_before), _canonical(dict(health_after)),
                 _canonical(identifier_columns),
                 # Every row present now was seen by a full traversal (staged, seen
                 # unchanged, or marked absent just above).
                 capture_id if full else (prior["seen_through_capture"] if prior else None),
                 now_iso if full else (prior["seen_through_at"] if prior else None)))
            self._db.execute("DELETE FROM sync_progress WHERE relation = ?", (relation,))
        return capture_id

    def _lift_seen(self, relation: str, capture_id: int, now_iso: str) -> int:
        """The keys this full traversal saw unchanged (``stage_seen``; inside
        ``_finish``): their count, after the absence of any of them marked while the
        traversal ran (a key scan) is lifted, as the merge lifts a staged key's."""
        seen = ("relation = ? AND absent_since_capture IS NOT NULL AND key_json IN "
                "(SELECT key_json FROM stage_seen WHERE capture_id = ?)")
        self._db.execute(
            "UPDATE row_removal SET until = MAX(since, ?), until_basis = ? WHERE "
            "relation = ? AND basis = ? AND until IS NULL AND key_json IN "
            f"(SELECT key_json FROM row_current WHERE {seen})",
            (now_iso, UNTIL_OBSERVED, relation, MISSING_FROM_SOURCE, relation, capture_id))
        self._db.execute(
            "UPDATE row_current SET last_seen_capture = ?, last_seen_at = ?, "
            "absent_since_capture = NULL, missing_from_source_since = NULL, "
            f"missing_basis = NULL, sighting_floor = NULL WHERE {seen}",
            (capture_id, now_iso, relation, capture_id))
        return self._db.execute("SELECT COUNT(*) FROM stage_seen WHERE capture_id = ?",
                                (capture_id,)).fetchone()[0]

    def _label_replacements(self, relation: str, previous_full: int, columns: Sequence[str],
                            missing: str, capture_id: int) -> None:
        """Label the open absence of every key this full capture found ``missing`` (a
        ``row_current`` condition on relation and capture id) with its successor
        (:func:`pair_replacements`): keys first seen after the ``previous_full``
        capture are new, the other keys this capture saw are held in both. Inside
        ``_write``, before the keys are marked; one pass over the relation's rows."""
        natural = replacement_key(self._replacement_models, relation, columns)
        if not natural:
            return
        vanished = {}
        for key_json, row_json in self._db.execute(
                f"SELECT key_json, row_json FROM row_current WHERE {missing} "
                "AND run_index = 0", (relation, capture_id, capture_id)):
            text = natural_text(json.loads(row_json), natural)
            if text is not None:
                vanished[key_json] = text
        if not vanished:
            return
        wanted = set(vanished.values())
        appeared, content, kept = {}, {}, Counter()
        for key_json, first, content_capture, row_json in self._db.execute(
                "SELECT key_json, first_seen_capture, capture_id, row_json FROM row_current "
                "WHERE relation = ? AND run_index = 0 AND (last_seen_capture >= ? OR EXISTS "
                "(SELECT 1 FROM stage_seen v WHERE v.capture_id = ? AND "
                "v.key_json = row_current.key_json))",
                (relation, capture_id, capture_id)):
            text = natural_text(json.loads(row_json), natural)
            if text not in wanted:
                continue
            if first > previous_full:
                appeared[key_json] = text
                content[key_json] = content_capture
            else:
                kept[text] += 1
        self._db.executemany(
            "UPDATE row_removal SET replaced_basis = ?, replaced_by = ? WHERE relation = ? "
            "AND key_json = ? AND basis = ? AND until IS NULL",
            [(basis, _canonical([[json.loads(k), content[k]] for k in successors]),
              relation, key_json, MISSING_FROM_SOURCE)
             for key_json, (basis, successors) in
             pair_replacements(vanished, appeared, kept).items()])

    def _replacement(self, relation: str, record: Any) -> dict[str, Any]:
        """The replacement label of a ``row_current`` record read with
        :data:`_REPLACED_SQL`: the successor citation, or every candidate's."""
        if record["absent_since_capture"] is None or not record["replaced"]:
            return {}
        basis, pairs = json.loads(record["replaced"])
        handles = [make_citation(relation, key, capture, self.generation)
                   for key, capture in json.loads(pairs)]
        since = record["missing_from_source_since"]
        if basis == SOURCE_REPLACEMENT and len(handles) == 1:
            return {"replaced_in_source": since, "replaced_basis": basis,
                    "successor": handles[0]}
        return {"ambiguous_replacement": since, "replacement_candidates": handles}

    def _existing_runs(self, relation: str, keys: list[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            for row in self._db.execute(
                    """SELECT key_json, row_digest, first_seen_capture, first_seen_at,
                       last_seen_capture, absent_since_capture, deleted_in_source,
                       deleted_basis FROM row_current WHERE relation = ? AND key_json IN ("""
                    + ",".join("?" for _ in batch) + ") ORDER BY key_json, run_index",
                    [relation, *batch]):
                item = found.setdefault(row["key_json"], {
                    "digests": [], "first_seen_capture": row["first_seen_capture"],
                    "first_seen_at": row["first_seen_at"],
                    "last_seen_capture": row["last_seen_capture"],
                    "absent": row["absent_since_capture"] is not None,
                    "deleted": ((row["deleted_in_source"], row["deleted_basis"])
                                if row["deleted_in_source"] is not None else None)})
                item["digests"].append(row["row_digest"])
        for item in found.values():
            item["digests"] = tuple(item["digests"])
        return found

    # -- lineage -----------------------------------------------------------

    def _sync_lineage(self, relation: str, *, in_transaction: bool = False) -> None:
        """Classify every stored row of ``relation`` with the rules in force.

        A relation's rows are always classified by one rule set, recorded by
        digest; another rule set (a new configuration, or a file filled before
        lineage existed) reclassifies the whole relation once.
        """
        digest = _rules_digest(self._lineage_rules, relation)
        stored = self._db.execute("SELECT rules_digest FROM relation_lineage WHERE relation = ?",
                                  (relation,)).fetchone()
        if stored is not None and stored[0] == digest:
            return
        if not in_transaction:
            with self._write():
                return self._sync_lineage(relation, in_transaction=True)
        for table, key in (("row_current", "key_json, run_index"),
                           ("row_version", "key_json, capture_id, run_index")):
            updates = []
            for record in self._db.execute(
                    f"SELECT rowid, row_json FROM {table} WHERE relation = ?", (relation,)):
                updates.append((*classify_lineage(self._lineage_rules, relation,
                                                  json.loads(record["row_json"])),
                                record["rowid"]))
            self._db.executemany(f"UPDATE {table} SET lineage = ?, lineage_basis = ? "
                                 "WHERE rowid = ?", updates)
        self._db.execute("INSERT OR REPLACE INTO relation_lineage VALUES (?, ?)",
                         (relation, digest))

    def _sync_rows(self, relation: str) -> None:
        """Lineage and soft-delete labels of ``relation``'s rows under the rules in force."""
        self._sync_lineage(relation)
        self._sync_soft_deletes(relation)

    # -- removals ------------------------------------------------------------

    def _sync_soft_deletes(self, relation: str, columns: Sequence[str] | None = None, *,
                           in_transaction: bool = False) -> None:
        """Label every stored row of ``relation`` with its soft delete, once per set
        of soft-delete columns (``columns``: the stored ones; default: the relation
        state's). A file filled before soft deletes existed, or a rule change, is
        relabelled whole; a key no longer deleted closes its interval."""
        if columns is None:
            state = self._state(relation)
            if state is None:
                return
            columns = json.loads(state["columns_json"])
        soft = soft_delete_columns(self._soft_delete_rules, relation, columns)
        digest = _sha(_canonical(soft))
        stored = self._db.execute(
            "SELECT columns_digest FROM relation_soft_delete WHERE relation = ?",
            (relation,)).fetchone()
        if stored is not None and stored[0] == digest:
            return
        if not in_transaction:
            with self._write():
                return self._sync_soft_deletes(relation, columns, in_transaction=True)
        now_iso = _iso(self._now())
        changed = []
        cursor = self._db.execute(
            f"SELECT key_json, row_json, {_SEEN_AT} AS last_seen_at, deleted_in_source, "
            "deleted_basis FROM row_current WHERE relation = ? AND "
            + ("1" if soft else "deleted_in_source IS NOT NULL")
            + " ORDER BY key_json, run_index", (relation,))
        for key_json, records in itertools.groupby(cursor, key=lambda r: r["key_json"]):
            run = list(records)
            before = ((run[0]["deleted_in_source"], run[0]["deleted_basis"])
                      if run[0]["deleted_in_source"] is not None else None)
            after = (_soft_deleted([json.loads(r["row_json"]) for r in run], soft,
                                   run[0]["last_seen_at"]) if soft else None)
            if after != before:
                changed.append((key_json, before, after))
        for key_json, before, after in changed:
            label = self._track_soft_delete(relation, key_json, before, after, now_iso)
            self._db.execute("UPDATE row_current SET deleted_in_source = ?, "
                             "deleted_basis = ? WHERE relation = ? AND key_json = ?",
                             (*(label or (None, None)), relation, key_json))
        self._db.execute("INSERT OR REPLACE INTO relation_soft_delete VALUES (?, ?)",
                         (relation, digest))

    def _restored_at(self, relation: str, key_json: str, rows: Sequence[Mapping[str, Any]],
                     since: Mapping[str, str] | None) -> str | None:
        """The update time an undeleted key's ``rows`` carry, only when it is later than
        the one the mirror last read while the key was deleted (its stored rows, still
        current here): an undelete that left the update time alone (a raw-SQL restore)
        keeps the deleting update's time, which is no time of the undelete. None then, or
        when either time is not served."""
        stored = _source_updated([json.loads(r[0]) for r in self._db.execute(
            "SELECT row_json FROM row_current WHERE relation = ? AND key_json = ?",
            (relation, key_json))], since)
        updated = _source_updated(rows, since)
        return updated if stored is not None and updated is not None and updated > stored \
            else None

    def _track_soft_delete(self, relation: str, key_json: str,
                           before: tuple[str, str] | None, after: tuple[str, str] | None,
                           now_iso: str, updated: str | None = None) -> tuple[str, str] | None:
        """Open the key's soft-delete interval at its deletion time, or close it (inside
        ``_write``): at ``updated`` (the row's own update time, ``_restored_at``) when it
        falls inside the interval, else now. Returns the key's label: a key deleted again while deleted
        keeps its interval and the label it opened with, so the two name one time."""
        if after is not None and before is None:
            self._db.execute("INSERT OR IGNORE INTO row_removal (relation, key_json, basis, "
                             "since) VALUES (?, ?, ?, ?)",
                             (relation, key_json, SOFT_DELETE, after[0]))
        elif after is None and before is not None:
            inside = updated is not None and before[0] <= updated <= now_iso
            self._end_removal(relation, key_json, SOFT_DELETE, updated if inside else now_iso,
                              UNTIL_SOURCE_UPDATE if inside else UNTIL_OBSERVED)
        elif after is not None:
            return before
        return after

    def _end_removal(self, relation: str, key_json: str, basis: str, until: str,
                     until_basis: str = UNTIL_OBSERVED) -> None:
        self._db.execute("UPDATE row_removal SET until = MAX(since, ?), until_basis = ? "
                         "WHERE relation = ? AND key_json = ? AND basis = ? AND until IS NULL",
                         (until, until_basis, relation, key_json, basis))

    def _start_removals(self, where: str, params: Sequence[Any], now_iso: str | None) -> None:
        """Open an absence interval at ``now_iso`` (None: at each row's own mark) for
        every key of the ``row_current`` rows ``where`` selects (inside ``_write``,
        before they are marked)."""
        self._db.execute(
            "INSERT OR IGNORE INTO row_removal (relation, key_json, basis, since) "
            "SELECT DISTINCT relation, key_json, ?, COALESCE(?, missing_from_source_since) "
            f"FROM row_current WHERE {where}", (MISSING_FROM_SOURCE, now_iso, *params))

    # -- absence scans -------------------------------------------------------

    def _scan_absence(self, relation: str, tenant: str, *,
                      catalog: Mapping[str, Any] | None = None,
                      deadline: float | None = None) -> dict[str, Any]:
        """Key-only traversal marking mirrored rows the source no longer returns."""
        if self._client is None:
            raise MirrorError("source_unavailable", "no Workbench API client is configured")
        state = self._state(relation)
        if state is None:
            raise MirrorError("not_mirrored", f"{relation} has no completed capture to compare")
        if catalog is None:
            catalog, _, error = self._catalog(force=True)
            if error:
                raise MirrorError("source_unavailable", error)
        entry = self._served_entry(relation, catalog)
        key_columns = list(entry["key"])
        if json.loads(state["key_columns_json"]) != key_columns:
            raise MirrorError("full_traversal_required",
                              f"the catalog key of {relation} changed; a full traversal "
                              "replaces the key scan")
        revision = catalog_revision(entry)
        for attempt in range(2):
            scan = self._absence_scan_progress(relation, revision)
            try:
                while not (scan["pages"] and scan["cursor"] is None):
                    page = self._read_page(relation, entry, key_columns, scan, deadline)
                    with self._write():
                        self._pin_binding(tenant)
                        before = self._db.total_changes
                        self._db.executemany(
                            "INSERT OR IGNORE INTO absence_seen VALUES (?, ?)",
                            [(scan["scan_id"], _canonical({c: row.get(c) for c in key_columns}))
                             for row in page["rows"]])
                        added = self._db.total_changes - before
                        self._db.execute(
                            """UPDATE absence_scan SET cursor = ?, page_size = ?,
                               pages = pages + 1, keys_seen = keys_seen + ?
                               WHERE scan_id = ?""",
                            (page["next_cursor"], scan["page_size"], added, scan["scan_id"]))
                    scan.update(cursor=page["next_cursor"], pages=scan["pages"] + 1)
                break
            except MirrorError as exc:
                if exc.code != "cursor_rejected" or attempt:
                    raise
                self._end_scan(scan["scan_id"], "abandoned")
        health_after = self._health()
        scan_id, started = scan["scan_id"], scan["started_at"]
        now_iso = _iso(self._now())
        unseen = ("NOT EXISTS (SELECT 1 FROM absence_seen s WHERE s.scan_id = ? "
                  "AND s.key_json = row_current.key_json)")
        with self._write():
            self._pin_binding(tenant)
            # Rows a content capture saw after the scan started are present.
            missing = (f"relation = ? AND absent_since_capture IS NULL AND {_SEEN_AT} < ? "
                       f"AND {unseen}")
            keys_missing = self._db.execute(
                f"SELECT COUNT(DISTINCT key_json) FROM row_current WHERE {missing}",
                (relation, started, scan_id)).fetchone()[0]
            self._start_removals(missing, (relation, started, scan_id), now_iso)
            self._db.execute(
                f"UPDATE row_current SET last_seen_capture = {_SEEN_CAPTURE}, "
                f"last_seen_at = {_SEEN_AT}, absent_since_capture = ?, "
                f"missing_from_source_since = ?, missing_basis = ? WHERE {missing}",
                (state["last_capture_id"], now_iso, f"key_scan:{scan_id}",
                 relation, started, scan_id))
            # Only a mark made before this scan started is lifted by it.
            returned = (f"relation = ? AND absent_since_capture IS NOT NULL AND "
                        f"COALESCE(missing_from_source_since, '') < ? AND NOT {unseen}")
            keys_returned = self._db.execute(
                f"SELECT COUNT(DISTINCT key_json) FROM row_current WHERE {returned}",
                (relation, started, scan_id)).fetchone()[0]
            self._db.execute(
                "UPDATE row_removal SET until = MAX(since, ?), until_basis = ? WHERE "
                "relation = ? AND basis = ? AND until IS NULL AND key_json IN "
                f"(SELECT key_json FROM row_current WHERE {returned})",
                (now_iso, UNTIL_OBSERVED, relation, MISSING_FROM_SOURCE, relation, started,
                 scan_id))
            # The key scan saw the key, not its content: the full traversal in force
            # did not see the row, so only a later one counts as its sighting.
            self._db.execute(
                "UPDATE row_current SET absent_since_capture = NULL, "
                "missing_from_source_since = NULL, missing_basis = NULL, sighting_floor = "
                "(SELECT seen_through_capture FROM relation_state s WHERE s.relation = "
                f"row_current.relation) WHERE {returned}",
                (relation, started, scan_id))
            unmirrored = self._db.execute(
                """SELECT COUNT(*) FROM absence_seen s WHERE s.scan_id = ? AND NOT EXISTS
                   (SELECT 1 FROM row_current r
                    WHERE r.relation = ? AND r.key_json = s.key_json)""",
                (scan_id, relation)).fetchone()[0]
            self._db.execute(
                """UPDATE absence_scan SET state = 'complete', finished_at = ?,
                   keys_missing = ?, keys_returned = ?, keys_unmirrored = ?,
                   health_after_json = ? WHERE scan_id = ?""",
                (now_iso, keys_missing, keys_returned, unmirrored,
                 _canonical(health_after), scan_id))
            self._db.execute("DELETE FROM absence_seen WHERE scan_id = ?", (scan_id,))
            self._db.execute(
                """UPDATE relation_state SET
                   row_count = (SELECT COUNT(*) FROM row_current WHERE relation = ?
                                AND absent_since_capture IS NULL),
                   absent_count = (SELECT COUNT(*) FROM row_current WHERE relation = ?
                                   AND absent_since_capture IS NOT NULL)
                   WHERE relation = ?""", (relation, relation, relation))
        return self._scan_dict(self._db.execute("SELECT * FROM absence_scan WHERE scan_id = ?",
                                                (scan_id,)).fetchone())

    def _absence_scan_progress(self, relation: str, revision: str) -> dict[str, Any]:
        row = self._db.execute(
            """SELECT * FROM absence_scan WHERE relation = ? AND state = 'in_progress'
               ORDER BY scan_id DESC LIMIT 1""", (relation,)).fetchone()
        if row is not None and row["catalog_revision"] == revision:
            self._health()
            self._db.execute("UPDATE absence_scan SET resumed = resumed + 1 WHERE scan_id = ?",
                             (row["scan_id"],))
            return dict(row)
        if row is not None:
            self._end_scan(row["scan_id"], "abandoned")
        health = self._health()
        with self._write():
            scan_id = self._db.execute(
                """INSERT INTO absence_scan (relation, state, started_at, catalog_revision,
                   page_size, pages, keys_seen, resumed, health_before_json)
                   VALUES (?, 'in_progress', ?, ?, ?, 0, 0, 0, ?)""",
                (relation, _iso(self._now()), revision, self._page_size,
                 _canonical(health))).lastrowid
        return dict(self._db.execute("SELECT * FROM absence_scan WHERE scan_id = ?",
                                     (scan_id,)).fetchone())

    def _end_scan(self, scan_id: int, state: str) -> None:
        with self._write():
            self._db.execute("UPDATE absence_scan SET state = ?, finished_at = ? "
                             "WHERE scan_id = ?", (state, _iso(self._now()), scan_id))
            self._db.execute("DELETE FROM absence_seen WHERE scan_id = ?", (scan_id,))

    @staticmethod
    def _scan_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"scan_id": row["scan_id"], "state": row["state"],
                "started_at": row["started_at"], "finished_at": row["finished_at"],
                "pages": row["pages"], "keys_seen": row["keys_seen"],
                "keys_missing": row["keys_missing"], "keys_returned": row["keys_returned"],
                "keys_unmirrored": row["keys_unmirrored"], "resumed": row["resumed"],
                "columns": "key_only"}

    # -- identifier index -------------------------------------------------

    def _identifier_columns(self, relation: str, columns: Sequence[str]) -> list[str]:
        available = set(columns)
        return [spec[0] for spec in self._identifiers.get(relation, ())
                if spec[0].split(".")[0] in available]

    def _index_key(self, relation: str, key_json: str, run: list[tuple[str, str]],
                   identifier_columns: list[str]) -> None:
        self._db.execute("DELETE FROM identifier_index WHERE relation = ? AND key_json = ?",
                         (relation, key_json))
        entries = set()
        for index, (_, row_json) in enumerate(run):
            row = json.loads(row_json)
            for column in identifier_columns:
                value = _norm_identifier(_identifier_value(row, column))
                if value is not None:
                    entries.add((value, relation, column, key_json, index))
        self._db.executemany("INSERT INTO identifier_index VALUES (?,?,?,?,?)",
                             sorted(entries))

    def _sync_identifier_index(self, relation: str, state: sqlite3.Row) -> None:
        wanted = self._identifier_columns(relation, json.loads(state["columns_json"]))
        if json.loads(state["identifier_columns_json"]) == wanted:
            return
        with self._write():
            self._db.execute("DELETE FROM identifier_index WHERE relation = ?", (relation,))
            runs: dict[str, list[tuple[str, str]]] = {}
            for row in self._db.execute(
                    """SELECT key_json, row_digest, row_json FROM row_current
                       WHERE relation = ? ORDER BY key_json, run_index""", (relation,)):
                runs.setdefault(row["key_json"], []).append((row["row_digest"],
                                                             row["row_json"]))
            for key_json, run in runs.items():
                self._index_key(relation, key_json, run, wanted)
            self._db.execute("""UPDATE relation_state SET identifier_columns_json = ?
                                WHERE relation = ?""", (_canonical(wanted), relation))

    # -- freshness --------------------------------------------------------

    def _freshness(self, state: sqlite3.Row, limit: float,
                   info: Mapping[str, Any] | None = None) -> dict[str, Any]:
        now = self._now()
        age = (now - _parse_instant(state["last_refresh_at"])).total_seconds()
        since = ({"column": state["since_column"], "mode": state["since_mode"]}
                 if state["since_column"] else None)
        watermark_age = None
        if state["watermark"]:
            watermark_age = round((now - _parse_instant(state["watermark"])).total_seconds(), 3)
        failure = self._db.execute("SELECT failed_at, error FROM refresh_failure "
                                   "WHERE relation = ?", (state["relation"],)).fetchone()
        info = dict(info or {"refreshed_now": "none"})
        marked = self.source_freshness(state["relation"])
        stale = age > limit or marked is not None
        caveats = list(_BASE_CAVEATS)
        if since is None:
            caveats.append("No since column: any refresh is a full traversal of the relation.")
        else:
            caveats.append(
                f"Changes are detected through {since['column']} ({since['mode']}); "
                f"{state['null_since_rows']} row(s) had no usable value at the last full "
                "traversal and change only on a full traversal.")
            if since["mode"] == "yyyymmdd-text":
                caveats.append("Sage audit dates are day-granular; incremental reads "
                               f"re-read {self._overlap_days} extra day(s).")
            if since["mode"] == "timestamp-utc":
                caveats.append("Zone-less source timestamps are compared in the source's "
                               "own domain; watermark age assumes they are UTC.")
        if state["future_since_rows"]:
            caveats.append(f"{state['future_since_rows']} row(s) in the latest capture had a "
                           "since value later than the replica's reported time and were "
                           "excluded from the watermark.")
        if not state["key_unique"]:
            caveats.append("Key is not unique (view); a citation resolves to the whole "
                           "key run and refresh uses full traversal.")
        if state["absent_count"]:
            caveats.append(f"{state['absent_count']} mirrored row(s) are missing from the "
                           "source (missing_from_source_since) and hidden from rows and "
                           "resolve (citations still resolve).")
        progress = self._progress(state["relation"])
        in_progress = None
        if progress is not None:
            in_progress = {"mode": progress["mode"], "capture_id": progress["capture_id"],
                           "pages": progress["pages"],
                           "rows_received": progress["rows_received"],
                           "started_at": progress["started_at"],
                           "updated_at": progress["updated_at"]}
            caveats.append(f"An unfinished {progress['mode']} read is staged, not served; "
                           "answers come from the last completed capture.")
        if marked is not None:
            caveats.append(marked["label"])
        elif stale:
            caveats.append("Mirror data is older than max_staleness.")
        return {"relation": state["relation"], "refreshed_now": info.get("refreshed_now"),
                "refresh_reason": info.get("reason"), "refresh_error": info.get("error"),
                "refresh_since_sent": info.get("since_sent"),
                "refresh_rows_received": info.get("rows_received"),
                # A failed refresh: the HTTP status and code it failed with (the rows are
                # the last capture held, as of last_refresh_at, never a live read).
                "refresh_http_status": info.get("http_status") if info.get("error") else None,
                "refresh_error_code": ((info.get("upstream_error_code") or info.get("error_code"))
                                       if info.get("error") else None),
                "last_refresh_failure": dict(failure) if failure else None,
                "stale": stale, "max_staleness_seconds": limit,
                "last_refresh_at": state["last_refresh_at"],
                "last_refresh_mode": state["last_refresh_mode"],
                "local_age_seconds": round(age, 3),
                "last_full_traversal_at": state["last_full_traversal_at"],
                "last_full_capture_id": state["last_full_capture_id"],
                "latest_capture_id": state["last_capture_id"],
                "refresh_strategy": "incremental_since" if since and state["key_unique"]
                                    else "full_traversal_only",
                "since": since, "source_watermark": state["watermark"],
                "source_watermark_raw": state["watermark_raw"],
                "source_watermark_age_seconds": watermark_age,
                "row_count": state["row_count"],
                "rows_absent_from_latest_full_traversal": state["absent_count"],
                "sync_in_progress": in_progress,
                "last_absence_scan": self._scan_dict(self._db.execute(
                    "SELECT * FROM absence_scan WHERE relation = ? AND state = 'complete' "
                    "ORDER BY scan_id DESC LIMIT 1", (state["relation"],)).fetchone()),
                "key": json.loads(state["key_columns_json"]),
                "key_unique": bool(state["key_unique"]),
                "catalog_revision": state["catalog_revision"],
                "health_before": json.loads(state["health_before_json"]),
                "health_after": json.loads(state["health_after_json"]),
                "atomic_upstream_snapshot": False, "deletions_proven": False,
                "caveats": caveats, **self._relation_label(state["relation"])}

    def _relation_label(self, relation: str) -> dict[str, Any]:
        """``source_freshness`` and ``freshness_label`` of a relation served from a
        persisted copy; nothing for a live one."""
        freshness = self.source_freshness(relation)
        return ({} if freshness is None else
                {"source_freshness": freshness, "freshness_label": freshness["label"]})

    def _label(self, relations: Iterable[str]) -> dict[str, Any]:
        """For an answer over several relations: the whole-copy freshness, or which
        of ``relations`` are served from a copy (``stale_relations``), never one
        fresh label over a mix."""
        if self.fallback is not None:
            return {"source_freshness": dict(self.fallback),
                    "freshness_label": self.fallback["label"]}
        copied = self._copied()
        stale = [dict(copied[name], relation=name) for name in sorted(set(relations))
                 if name in copied]
        return {"stale_relations": stale} if stale else {}

    def _copied(self) -> dict[str, dict[str, Any]]:
        """Every relation filled from a persisted copy -> its freshness (one query)."""
        return {row["relation"]: json.loads(row["freshness_json"]) for row in
                self._db.execute("SELECT relation, freshness_json FROM persisted_relation")}

    # -- public operations -------------------------------------------------

    def catalog(self, actor: Actor, *, refresh_catalog: bool = False) -> dict[str, Any]:
        """Relations this actor may read, with columns, freshness and coverage."""
        if not isinstance(actor, Actor):
            raise MirrorError("invalid_actor", "actor must be an ontology Actor")
        audience = self._audience()
        self._check_binding(audience.tenant)
        try:
            upstream, fetched_at, error = self._catalog(force=refresh_catalog)
        except MirrorError as exc:
            upstream, fetched_at, error = {}, None, f"{exc.code}: {exc}"
        mirrored = {row["relation"] for row in
                    self._db.execute("SELECT relation FROM relation_state")}
        visible = sorted(name for name in set(upstream) | mirrored
                         if audience.allows(actor, name))
        relations = []
        for name in visible:
            entry = upstream.get(name)
            state = self._state(name)
            columns = (json.loads(state["columns_json"]) if state is not None else
                       self._projection(name, entry) if entry is not None else [])
            specs = [spec for spec in self._identifiers.get(name, ())
                     if spec[0].split(".")[0] in columns]
            relations.append({
                "name": name, "domain": entry.get("domain") if entry else None,
                "in_upstream_catalog": entry is not None,
                "unavailable": entry.get("unavailable") if entry else None,
                "key": (entry or {}).get("key"), "since": (entry or {}).get("since"),
                "columns": columns, "mirrored": state is not None,
                "identifier_columns": [{"column": column, "kind": kind,
                                        "identifier_strength": strength,
                                        **({"strength_depends_on_row":
                                            ROW_STRENGTH_RULES[(name, column)][1]}
                                           if (name, column) in ROW_STRENGTH_RULES else {})}
                                       for column, kind, strength in specs],
                "freshness": (self._freshness(state, self.default_max_staleness_seconds)
                              if state is not None else None)})
        source = self._audience_source
        return {"schema": "plx-source-mirror-catalog/v1", "tenant": audience.tenant,
                "mirror_generation": self.generation,
                "relation_count": len(relations), "relations": relations,
                "catalog_fetched_at": fetched_at, "catalog_error": error,
                "audience_error": source.error if isinstance(source, _FileAudience) else None,
                "policy": {"default_max_staleness_seconds": self.default_max_staleness_seconds,
                           "overlap_seconds": self._overlap_seconds,
                           "yyyymmdd_overlap_days": self._overlap_days,
                           "max_rows_per_relation": self._max_rows,
                           "page_size": self._page_size,
                           "throttle_attempts": self._throttle_attempts,
                           "max_retry_after_seconds": self._max_retry_after,
                           "row_limit": MAX_ROWS_LIMIT},
                "entitlement": "Relations outside the caller's audience are omitted and "
                               "not counted; the audience map is configuration, not "
                               "authority.",
                **self._label(item["name"] for item in relations)}

    def refresh(self, actor: Actor, relation: str, *, full: bool = False) -> dict[str, Any]:
        """Explicitly fill or refresh one visible relation now."""
        audience = self._require_visible(actor, relation)
        state, info = self._ensure_fresh(relation, audience.tenant, 0.0, force=True,
                                         force_full=full)
        return self._freshness(state, self.default_max_staleness_seconds, info)

    def scan_absence(self, actor: Actor, relation: str, *,
                     budget_seconds: float | None = None) -> dict[str, Any]:
        """Key-only traversal (``columns=`` the key) of one visible mirrored relation.

        Mirrored rows whose key the source no longer returns get
        ``missing_from_source_since``; marked rows whose key is returned again
        are unmarked. Content is not re-read. Resumable like a refresh.
        """
        audience = self._require_visible(actor, relation)
        deadline = self._deadline(budget_seconds)
        self._lock_wait = self._lock_credit = 0.0
        self._lock_scope = (_LockCredit(LOCK_WAIT_RUN_CAP_SECONDS), deadline)
        try:
            return self._scan_absence(relation, audience.tenant, deadline=deadline)
        finally:
            self._lock_scope = None

    def _deadline(self, budget_seconds: Any) -> float | None:
        if budget_seconds is None:
            return None
        if (isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float))
                or not 0 <= budget_seconds <= 7 * 86400):
            raise MirrorError("invalid_budget", "budget_seconds must be 0..604800")
        return self._monotonic() + float(budget_seconds)

    def sync(self, actor: Actor, *, relations: Sequence[str] | None = None,
             full: bool = False, absence_scan: bool = False,
             budget_seconds: float | None = None,
             order: Sequence[str] | None = None, workers: int = 1) -> dict[str, Any]:
        """Capture every served catalog relation visible to ``actor``.

        A relation never mirrored is filled; a mirrored one is refreshed
        (incrementally where its ``since`` allows, else by full traversal;
        ``full`` forces full traversals). ``absence_scan`` adds a key-only
        scan after an incremental refresh, the only refresh that cannot see
        deletions. A relation interrupted by ``budget_seconds`` or a failure
        continues from its last committed page on the next call (the next run,
        when the mirror file persists), which is how a relation too large for one
        budget completes; only a ``row_bound`` or ``paging_defect`` traversal is
        abandoned and starts over. ``order`` names relations to capture first, in
        that order, inside the same budget; the rest follow by name (a name
        not in the catalog, not visible or not selected is skipped).

        ``workers`` (1..4) relations are read at the same time, each by its own
        connection to this mirror file and its own fenced traversal; pages of one
        relation stay in order. Four leaves one of the service's five ``wb_api``
        connections free; HTTP 429/503 are waited out per request as always. A
        relation that fails (HTTP 500 after the page remedies, a paging defect, the
        row bound, an unexpected exception: ``internal_error`` with its type) is
        recorded (in ``refresh_failure`` too, retried once the workers are done, or
        ``failure_recorded`` false says why not and this instance's coverage counts it
        failed until a refresh succeeds; another process cannot see such a failure) and
        never stops the others. A source defect whose page-size window closed
        keeps its traversal and is marked ``defer_next_run``. A wait for another
        worker's write lock (``lock_wait_seconds``) is bounded per relation and not
        charged to its budget up to a per-run credit (``LOCK_WAIT_*``). Returns
        per-relation outcomes in capture order (with ``failure_class``, HTTP status, the
        service's error code, pages, rows, final page size and seconds) and
        :meth:`coverage`.
        """
        if not isinstance(actor, Actor):
            raise MirrorError("invalid_actor", "actor must be an ontology Actor")
        for value, label in ((relations, "relations"), (order, "order")):
            if value is not None and (isinstance(value, (str, bytes)) or
                                      not isinstance(value, Sequence) or
                                      any(not isinstance(name, str) for name in value)):
                raise MirrorError("invalid_relation", f"{label} must be a list of names")
        if (isinstance(workers, bool) or not isinstance(workers, int) or
                not 1 <= workers <= MAX_SYNC_WORKERS):
            raise MirrorError("invalid_workers",
                              f"workers must be an integer 1..{MAX_SYNC_WORKERS}")
        audience = self._audience()
        self._check_binding(audience.tenant)
        deadline = self._deadline(budget_seconds)
        if not audience.grants_any(actor):
            # Nothing can be visible: no upstream request at all, not even the catalog.
            return {"schema": "plx-source-mirror-sync/v1", "full": full,
                    "absence_scan": absence_scan, "runs": [], "workers": workers,
                    "throttle_waits": {"count": 0, "seconds": 0.0},
                    "coverage": self._coverage(actor, audience, {}, None, None)}
        if self._client is None:
            raise MirrorError("source_unavailable", "no Workbench API client is configured")
        catalog, fetched_at, error = self._catalog(force=True)
        if error:   # with the HTTP status behind it, as when no catalog is cached
            raise self._catalog_failed(error)
        wanted = None if relations is None else set(relations)
        names = sorted(name for name in catalog if audience.allows(actor, name)
                       and (wanted is None or name in wanted))
        if order:
            rank = {name: index for index, name in enumerate(dict.fromkeys(order))}
            names.sort(key=lambda name: (rank.get(name, len(rank)), name))
        waits_before = len(self.throttle_waits)
        step = dict(actor=actor, tenant=audience.tenant, full=full,
                    absence_scan=absence_scan, catalog=catalog, deadline=deadline,
                    lock_credit=_LockCredit(LOCK_WAIT_RUN_CAP_SECONDS))
        if workers == 1 or len(names) <= 1:
            try:
                runs = [self._sync_one(name, **step) for name in names]
            finally:
                self._lock_scope = None
            waits = self.throttle_waits[waits_before:]
        else:
            runs, waits = self._sync_parallel(names, workers, step)
            self.throttle_waits.extend(waits)
        for run in runs:
            if "record_error" not in run:
                # A capture, or a failure now in refresh_failure, supersedes it.
                if run.get("outcome") == "captured" or run.get("failure_recorded"):
                    self._unrecorded.pop(run["relation"], None)
                continue
            # The workers are done, so the write lock is contended no more: the record
            # gets a second try with this connection's own busy timeout.
            record_error = self._write_failure(run["relation"], run["error"],
                                               wait_ms=self._busy_timeout_ms)
            if record_error is None:
                run["failure_recorded"] = True
                del run["record_error"]
                self._unrecorded.pop(run["relation"], None)
            else:
                run["record_error"] = self._unrecorded[run["relation"]] = record_error
        # The sync's safe point: its writes leave the WAL (no wait: a projection build
        # holding a snapshot makes this busy, and the host checkpoints after the build).
        checkpoint(self._db)
        return {"schema": "plx-source-mirror-sync/v1", "full": full,
                "absence_scan": absence_scan, "runs": runs, "workers": workers,
                "throttle_waits": {"count": len(waits), "seconds": round(sum(waits), 3)},
                "coverage": self._coverage(actor, audience, catalog, fetched_at, None)}

    def _sync_one(self, name: str, *, actor: Actor, tenant: str, full: bool,
                  absence_scan: bool, catalog: Mapping[str, Any],
                  deadline: float | None, lock_credit: _LockCredit) -> dict[str, Any]:
        """One relation of :meth:`sync`: its run record (never raises but on a binding
        mismatch, which stops the whole sync). Every failure is in ``refresh_failure``
        so coverage counts the relation failed, whatever stored capture it has."""
        entry = catalog[name]
        if entry.get("unavailable") or not entry.get("key"):
            return {"relation": name, "outcome": "unavailable"}
        if deadline is not None and self._monotonic() >= deadline:
            return {"relation": name, "outcome": "budget_exhausted",
                    "failure_class": "budget"}
        # Wall time for the report only; the budget runs on the injectable monotonic.
        started = time.perf_counter()
        self._lock_wait = self._lock_credit = 0.0
        self._lock_scope = (lock_credit, deadline)
        try:
            # A long sync re-reads the audience before each relation, so a
            # revocation stops the fill for the rest of the run.
            current = self._audience()
            if current.tenant != tenant or not current.allows(actor, name):
                return {"relation": name, "outcome": "access_revoked"}
            state, info = self._ensure_fresh(name, tenant, 0.0, force=True,
                                             force_full=full, catalog=catalog,
                                             deadline=deadline)
        except MirrorError as exc:
            if exc.code == "binding_mismatch":
                raise
            info = {"error": f"{exc.code}: {exc}", "error_code": exc.code,
                    "upstream_error_code": exc.upstream_code, "detail": dict(exc.detail),
                    "defer_next_run": exc.defer_next_run}
            if exc.status is not None:
                info["http_status"] = exc.status
            if exc.failure_record is None:
                self._record_failure(name, info)
            elif exc.failure_record != "recorded":
                info["record_error"] = exc.failure_record
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                info = self._internal_error(exc)
            else:
                # Another worker held the mirror file past the busy timeout; the
                # relation's committed pages stay and the next sync resumes them.
                info = {"error": "mirror_store_busy: the mirror file stayed locked",
                        "error_code": "mirror_store_busy", "detail": {}}
            self._record_failure(name, info)
        except Exception as exc:  # noqa: BLE001 - recorded per relation, never swallowed
            info = self._internal_error(exc)
            self._record_failure(name, info)
        seconds = round(time.perf_counter() - started, 3)
        lock_wait = round(self._lock_wait, 3)
        if info.get("error"):
            code = info.get("error_code")
            detail = info.get("detail") or {}
            failed = {"relation": name,
                      "outcome": ("budget_exhausted" if code == "sync_budget_exhausted"
                                  else "failed"),
                      "error": info["error"],
                      "failure_class": failure_class(code, info.get("http_status")),
                      "upstream_error_code": info.get("upstream_error_code"),
                      "exception_type": info.get("exception_type"),
                      "http_status": info.get("http_status"),
                      "pages": detail.get("pages"),
                      "rows_received": detail.get("rows_received"),
                      "page_size_final": detail.get("page_size"), "seconds": seconds,
                      "lock_wait_seconds": lock_wait,
                      "defer_next_run": bool(info.get("defer_next_run")),
                      "failure_recorded": "record_error" not in info}
            if "record_error" in info:
                failed["record_error"] = info["record_error"]
            return failed
        run = {"relation": name, "outcome": "captured", "mode": info["refreshed_now"],
               "pages": info["pages"], "rows_received": info["rows_received"],
               "resumed": bool(info["resumed"]), "page_size_final": info.get("page_size"),
               "seconds": seconds, "lock_wait_seconds": lock_wait}
        if absence_scan and info["refreshed_now"] == "incremental":
            try:
                run["absence_scan"] = self._scan_absence(name, tenant, catalog=catalog,
                                                         deadline=deadline)
            except MirrorError as exc:
                if exc.code == "binding_mismatch":
                    raise
                run["absence_scan"] = {"error": f"{exc.code}: {exc}"}
            except Exception as exc:  # noqa: BLE001 - the capture stands; the scan failed
                run["absence_scan"] = {"error": f"internal_error: {type(exc).__name__}",
                                       "exception_type": type(exc).__name__}
        return run

    def _internal_error(self, exc: Exception) -> dict[str, Any]:
        """The failed-run info for an unexpected exception in one relation's sync: its
        type only (a message could carry row values); ``_write`` already rolled back
        whatever it held."""
        name = type(exc).__name__
        return {"error": f"internal_error: {name}", "error_code": "internal_error",
                "exception_type": name, "detail": {}}

    def _record_failure(self, relation: str, info: dict[str, Any]) -> None:
        """Record a failed sync of ``relation`` in ``refresh_failure`` (as a failed
        refresh does), so coverage lists it as failed. A record that cannot be written
        is never dropped: ``info`` gets ``record_error`` (its exception type)."""
        record_error = self._write_failure(relation, info["error"])
        if record_error is not None:
            info["record_error"] = record_error

    def _write_failure(self, relation: str, error: str, *,
                       wait_ms: int | None = None) -> str | None:
        """Write ``refresh_failure`` for ``relation``, waiting at most ``wait_ms``
        (default ``FAILURE_RECORD_WAIT_SECONDS``) for the write lock. Returns None, or
        the type of the exception that blocked the write (the caller's own error
        stands)."""
        if wait_ms is None:
            wait_ms = int(FAILURE_RECORD_WAIT_SECONDS * 1000)
        self._db.execute(f"PRAGMA busy_timeout = {int(wait_ms)}")
        try:
            self._db.execute("INSERT OR REPLACE INTO refresh_failure VALUES (?, ?, ?)",
                             (relation, _iso(self._now()), error))
        except sqlite3.Error as exc:
            return type(exc).__name__
        finally:
            self._db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return None

    def _sibling(self) -> "SourceMirror":
        """Another connection to this mirror file with this mirror's policy, for one
        sync worker thread (SQLite connections stay in the thread that opened them)."""
        source = self._audience_source
        audience: Any = source.path if isinstance(source, _FileAudience) else source
        # A long merge by one worker holds the file; the others wait instead of
        # failing, from the sibling's first (schema) write on.
        return SourceMirror(self._path, self._client, audience, _busy_timeout_ms=600000,
                            **self._options)

    def _sync_parallel(self, names: Sequence[str], workers: int,
                       step: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[float]]:
        """Run :meth:`_sync_one` over ``names`` on ``workers`` threads, each with its own
        sibling mirror. Runs come back in ``names`` order; a binding mismatch in any
        worker stops every worker before its next relation and is raised here."""
        pending = list(enumerate(names))
        results: dict[int, dict[str, Any]] = {}
        waits: list[float] = []
        lock = threading.Lock()
        stop: list[BaseException] = []

        def work() -> None:
            try:
                mirror = self._sibling()
            except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
                with lock:
                    stop.append(exc)
                return
            try:
                while True:
                    with lock:
                        if stop or not pending:
                            return
                        index, name = pending.pop(0)
                    try:
                        run = mirror._sync_one(name, **step)
                    except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
                        with lock:
                            stop.append(exc)
                        return
                    with lock:
                        results[index] = run
            finally:
                with lock:
                    waits.extend(mirror.throttle_waits)
                mirror.close()

        threads = [threading.Thread(target=work, name=f"mirror-sync-{n}", daemon=True)
                   for n in range(min(workers, len(names)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if stop:
            raise stop[0]
        return [results[index] for index in sorted(results)], waits

    def coverage(self, actor: Actor) -> dict[str, Any]:
        """Per-relation completeness counts for every relation visible to ``actor``."""
        if not isinstance(actor, Actor):
            raise MirrorError("invalid_actor", "actor must be an ontology Actor")
        audience = self._audience()
        self._check_binding(audience.tenant)
        try:
            catalog, fetched_at, error = self._catalog()
        except MirrorError as exc:
            catalog, fetched_at, error = {}, None, f"{exc.code}: {exc}"
        return self._coverage(actor, audience, catalog, fetched_at, error)

    def _coverage(self, actor: Actor, audience: AudienceMap, catalog: Mapping[str, Any],
                  fetched_at: str | None, error: str | None) -> dict[str, Any]:
        """Coverage of the relations ``actor`` may read. A relation whose failure this
        instance could not write to ``refresh_failure`` (``_unrecorded``) counts failed,
        never complete; another process's coverage cannot see that failure."""
        unrecorded = self._unrecorded
        mirrored = {row["relation"] for row in
                    self._db.execute("SELECT relation FROM relation_state")}
        names = sorted(name for name in set(catalog) | mirrored if audience.allows(actor, name))
        relations = []
        for name in names:
            entry = catalog.get(name)
            served = bool(entry) and not entry.get("unavailable") and bool(entry.get("key"))
            state = self._state(name)
            progress = self._progress(name)
            failure = self._db.execute("SELECT failed_at, error FROM refresh_failure "
                                       "WHERE relation = ?", (name,)).fetchone()
            lineage_counts: dict[str, int] = {}
            latest = None
            missing = deleted = 0
            replaced: dict[str, int] = {}
            if state is not None:
                self._sync_rows(name)
                # Distinct keys, both (a duplicate-key run is one record).
                missing, deleted = self._db.execute(
                    "SELECT COUNT(DISTINCT CASE WHEN absent_since_capture IS NOT NULL THEN "
                    "key_json END), COUNT(DISTINCT CASE WHEN absent_since_capture IS NULL AND "
                    "deleted_in_source IS NOT NULL THEN key_json END) FROM row_current "
                    "WHERE relation = ?", (name,)).fetchone()
                replaced = dict(self._db.execute(
                    "SELECT replaced_basis, COUNT(*) FROM row_removal WHERE relation = ? AND "
                    "basis = ? AND until IS NULL AND replaced_basis IS NOT NULL "
                    "GROUP BY replaced_basis", (name, MISSING_FROM_SOURCE)).fetchall())
                lineage_counts = {
                    (row[0] or "unknown"): row[1] for row in self._db.execute(
                        """SELECT lineage, COUNT(*) FROM row_current WHERE relation = ?
                           AND absent_since_capture IS NULL GROUP BY lineage ORDER BY lineage""",
                        (name,))}
                capture = self._db.execute("SELECT * FROM capture WHERE capture_id = ?",
                                           (state["last_capture_id"],)).fetchone()
                latest = {"capture_id": capture["capture_id"], "mode": capture["mode"],
                          "pages": capture["page_count"],
                          "rows_received": capture["rows_received"],
                          "keys_absent": capture["keys_absent"]} if capture else None
            relations.append({
                "relation": name, "in_upstream_catalog": entry is not None, "served": served,
                "mirrored": state is not None,
                "complete": served and state is not None and progress is None
                            and failure is None and name not in unrecorded,
                "rows_current": state["row_count"] if state else 0,
                "rows_missing_from_source": missing,
                "rows_deleted_in_source": deleted,
                "rows_replaced_in_source": replaced.get(SOURCE_REPLACEMENT, 0),
                "rows_ambiguous_replacement": replaced.get(AMBIGUOUS_REPLACEMENT, 0),
                "soft_delete_columns": (soft_delete_columns(
                    self._soft_delete_rules, name, json.loads(state["columns_json"]))
                    if state else []),
                "lineage_counts": lineage_counts,
                "last_full_traversal_at": state["last_full_traversal_at"] if state else None,
                "last_refresh_at": state["last_refresh_at"] if state else None,
                "last_refresh_mode": state["last_refresh_mode"] if state else None,
                "latest_capture": latest,
                "sync_in_progress": ({"mode": progress["mode"], "pages": progress["pages"],
                                      "rows_received": progress["rows_received"],
                                      "started_at": progress["started_at"]}
                                     if progress else None),
                "last_refresh_failure": dict(failure) if failure else None,
                "unrecorded_refresh_failure": unrecorded.get(name),
                "last_absence_scan": self._scan_dict(self._db.execute(
                    "SELECT * FROM absence_scan WHERE relation = ? AND state = 'complete' "
                    "ORDER BY scan_id DESC LIMIT 1", (name,)).fetchone())})
        in_catalog = [item for item in relations if item["in_upstream_catalog"]]
        served = [item for item in in_catalog if item["served"]]
        complete = [item for item in served if item["complete"]]
        return {"schema": "plx-source-mirror-coverage/v1", "tenant": audience.tenant,
                "mirror_generation": self.generation, "catalog_fetched_at": fetched_at,
                "catalog_error": error,
                "catalog_relation_count": len(in_catalog),
                "served_relation_count": len(served),
                "unavailable_relation_count": len(in_catalog) - len(served),
                "complete_relation_count": len(complete),
                "all_served_complete": bool(served) and len(complete) == len(served),
                "in_progress_count": sum(1 for item in relations if item["sync_in_progress"]),
                "failed_count": sum(1 for item in relations if item["last_refresh_failure"]
                                    or item["unrecorded_refresh_failure"]),
                "not_mirrored_count": sum(1 for item in served if not item["mirrored"]),
                "rows_current_total": sum(item["rows_current"] for item in relations),
                "rows_missing_total": sum(item["rows_missing_from_source"]
                                          for item in relations),
                "rows_deleted_in_source_total": sum(item["rows_deleted_in_source"]
                                                    for item in relations),
                "rows_replaced_in_source_total": sum(item["rows_replaced_in_source"]
                                                     for item in relations),
                "rows_ambiguous_replacement_total": sum(item["rows_ambiguous_replacement"]
                                                        for item in relations),
                "relations": relations, "atomic_upstream_snapshot": False,
                "deletions_proven": False,
                "entitlement": "Relations outside the caller's audience are omitted and "
                               "not counted."}

    def rows(self, actor: Actor, relation: str, columns: Sequence[str] | None = None,
             where: Sequence[Sequence[Any]] = (), order: Sequence[Any] = (),
             limit: int = 100, max_staleness: float | None = None,
             offset: int = 0, include_missing: bool = False) -> dict[str, Any]:
        """Filtered rows of one visible relation, each with its citation, lineage and
        ``content_sha256`` (the stored row's content digest: the same across captures and
        mirror generations while the content is the same, as the projection's version
        digest is).

        ``offset`` skips that many matches of the ordered result, so a caller
        can page past ``MAX_ROWS_LIMIT``. Pages are separate reads of the
        mirror: compare ``freshness.latest_capture_id`` across pages to detect
        a refresh in between. ``where`` is evaluated in SQLite first (a
        superset prefilter over the stored JSON) and then exactly, so the
        result is the same as a full scan. ``include_missing`` also returns
        rows the source no longer returned, labelled
        ``missing_from_source_since`` (and ``replaced_in_source`` with its
        ``successor``, or ``ambiguous_replacement`` with ``replacement_candidates``,
        when a full capture found who replaced them), and rows the source
        soft-deleted, labelled ``deleted_in_source`` with its ``deleted_basis``.
        """
        if not isinstance(include_missing, bool):
            raise MirrorError("invalid_include_missing", "include_missing must be a boolean")
        audience = self._require_visible(actor, relation)
        predicates = _check_where(where)
        ordering = _check_order(order)
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= MAX_ROWS_LIMIT):
            raise MirrorError("invalid_limit", f"limit must be 1..{MAX_ROWS_LIMIT}")
        if (isinstance(offset, bool) or not isinstance(offset, int)
                or not 0 <= offset <= MAX_ROWS_OFFSET):
            raise MirrorError("invalid_offset", f"offset must be 0..{MAX_ROWS_OFFSET}")
        if columns is not None and (isinstance(columns, (str, bytes)) or not columns or
                                    len(set(columns)) != len(columns) or
                                    any(not isinstance(col, str) for col in columns)):
            raise MirrorError("invalid_columns", "columns must be a list of unique names")
        staleness = self._staleness_limit(max_staleness)
        if self._state(relation) is None:
            try:
                upstream = self._catalog()[0]
            except MirrorError:
                upstream = {}  # the fill below fails the same way (or serves a copy)
            if relation in upstream:
                self._check_columns(self._projection(relation, upstream[relation]),
                                    columns, predicates, ordering)
        state, info = self._ensure_fresh(relation, audience.tenant, staleness,
                                         persisted_ok=True)
        mirrored = json.loads(state["columns_json"])
        self._check_columns(mirrored, columns, predicates, ordering)
        key_columns = json.loads(state["key_columns_json"])
        selected = list(columns) if columns is not None else mirrored
        self._sync_rows(relation)
        clauses, params = ["relation = ?"], [relation]
        if not include_missing:
            clauses += ["absent_since_capture IS NULL", "deleted_in_source IS NULL"]
        for predicate in predicates:
            clause, values = _sql_prefilter(*predicate)
            clauses.append(clause)
            params.extend(values)
        matches = []
        for record in self._db.execute(
                "SELECT key_json, run_index, run_length, row_json, row_digest, capture_id, "
                f"{_SEEN_AT} AS last_seen_at, absent_since_capture, missing_from_source_since, "
                "lineage, "
                "deleted_in_source, deleted_basis, " + _REPLACED_SQL.format(r="row_current") +
                " FROM row_current WHERE " + " AND ".join(clauses), params):
            row = json.loads(record["row_json"])
            if all(_matches(row, column, op, value) for column, op, value in predicates):
                matches.append((row, record))
        ordering = ordering or [(column, False) for column in key_columns]
        # Stable passes from the least significant column; nulls always last.
        matches.sort(key=lambda match: (match[1]["key_json"], match[1]["run_index"]))
        for column, descending in reversed(ordering):
            present = [match for match in matches if match[0].get(column) is not None]
            present.sort(key=lambda match: _order_value(match[0][column]), reverse=descending)
            matches = present + [match for match in matches if match[0].get(column) is None]
        items = []
        for row, record in matches[offset:offset + limit]:
            key = json.loads(record["key_json"])
            item = {"citation": make_citation(relation, key, record["capture_id"],
                                              self.generation),
                    "key": key, "row": {column: row.get(column) for column in selected},
                    "capture_id": record["capture_id"],
                    "content_sha256": record["row_digest"],
                    "content_last_seen_at": record["last_seen_at"],
                    "lineage": record["lineage"]}
            if record["run_length"] > 1:
                item["key_run"] = {"index": record["run_index"],
                                   "length": record["run_length"]}
            if record["absent_since_capture"] is not None:
                item["missing_from_source_since"] = record["missing_from_source_since"]
                item.update(self._replacement(relation, record))
            if record["deleted_in_source"] is not None:
                item.update(deleted_in_source=record["deleted_in_source"],
                            deleted_basis=record["deleted_basis"])
            items.append(item)
        return {"schema": "plx-source-mirror-rows/v1", "relation": relation,
                "columns": selected, "where": [list(item) for item in predicates],
                "order": [[column, "desc" if descending else "asc"]
                          for column, descending in ordering],
                "limit": limit, "offset": offset, "include_missing": include_missing,
                "total_matched": len(matches),
                "returned": len(items),
                "next_offset": (offset + len(items)
                                if offset + len(items) < len(matches) else None),
                "truncated": len(matches) > len(items), "rows": items,
                "freshness": self._freshness(state, staleness, info)}

    @staticmethod
    def _check_columns(available: Sequence[str], columns: Sequence[str] | None,
                       predicates: list[tuple[str, str, Any]],
                       ordering: list[tuple[str, bool]]) -> None:
        known = set(available)
        wanted = list(columns or []) + [item[0] for item in predicates] + \
            [item[0] for item in ordering]
        unknown = sorted({column for column in wanted if column not in known})
        if unknown:
            raise MirrorError("unknown_column",
                              "columns not mirrored for this relation: " + ", ".join(unknown))

    def resolve(self, actor: Actor, literal: str, *, fill_missing: bool = False,
                max_staleness: float | None = None, limit: int = 50) -> dict[str, Any]:
        """Exact identifier candidates across visible relations; never picks a winner."""
        if not isinstance(actor, Actor):
            raise MirrorError("invalid_actor", "actor must be an ontology Actor")
        normalized = _norm_identifier(literal) if isinstance(literal, str) else None
        if normalized is None:
            raise MirrorError("invalid_literal",
                              "literal must be 1..256 printable characters after trimming")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise MirrorError("invalid_limit", "limit must be 1..200")
        staleness = self._staleness_limit(max_staleness)
        audience = self._audience()
        self._check_binding(audience.tenant)
        visible = [name for name in sorted(self._identifiers) if audience.allows(actor, name)]
        upstream: Mapping[str, Any] | None = None
        if visible:
            try:
                upstream = self._catalog()[0]
            except MirrorError:
                upstream = None
        searched, not_mirrored, not_in_catalog, failed = [], [], [], []
        for relation in visible:
            if (upstream is not None and relation not in upstream and
                    self._state(relation) is None):
                not_in_catalog.append(relation)
                continue
            try:
                state, info = self._ensure_fresh(relation, audience.tenant, staleness,
                                                 allow_fill=fill_missing,
                                                 persisted_ok=True)
            except MirrorError as exc:
                failed.append({"relation": relation, "error": f"{exc.code}: {exc}"})
                continue
            if state is None:
                not_mirrored.append(relation)
                continue
            self._sync_identifier_index(relation, state)
            self._sync_rows(relation)
            state = self._state(relation)
            freshness = self._freshness(state, staleness, info)
            searched.append({
                "relation": relation,
                "identifier_columns": json.loads(state["identifier_columns_json"]),
                "refreshed_now": freshness["refreshed_now"], "stale": freshness["stale"],
                "last_refresh_at": freshness["last_refresh_at"],
                "refresh_error": freshness["refresh_error"],
                "refresh_strategy": freshness["refresh_strategy"]})
        specs = {(relation, spec[0]): spec for relation in self._identifiers
                 for spec in self._identifiers[relation]}
        candidates = []
        names = [item["relation"] for item in searched]
        if names:
            records = self._db.execute(
                f"""SELECT i.relation, i.column_name, i.key_json, i.run_index,
                    r.run_length, r.row_json, r.capture_id, r.lineage
                    FROM identifier_index i JOIN row_current r
                      ON r.relation = i.relation AND r.key_json = i.key_json
                     AND r.run_index = i.run_index
                    WHERE i.norm_value = ? AND r.absent_since_capture IS NULL
                      AND r.deleted_in_source IS NULL
                      AND i.relation IN ({",".join("?" for _ in names)})
                    ORDER BY i.relation, i.column_name, i.key_json, i.run_index""",
                (normalized, *names)).fetchall()
            for record in records:
                spec = specs.get((record["relation"], record["column_name"]))
                if spec is None:
                    continue
                key = json.loads(record["key_json"])
                row = json.loads(record["row_json"])
                candidate = {
                    "relation": record["relation"], "column": record["column_name"],
                    "kind": spec[1], "identifier_strength": spec[2],
                    "value": _identifier_value(row, record["column_name"]),
                    "key": key, "lineage": record["lineage"],
                    "citation": make_citation(record["relation"], key, record["capture_id"],
                                              self.generation)}
                rule = ROW_STRENGTH_RULES.get((record["relation"], record["column_name"]))
                if rule is not None:
                    strength, basis = rule[0](row)
                    candidate["identifier_strength"] = strength
                    candidate["strength_basis"] = basis
                identity = _candidate_identity(record["relation"], record["column_name"],
                                               candidate["value"], row)
                if identity is not None:
                    candidate["identity"] = identity
                note = IDENTIFIER_NOTES.get((record["relation"], record["column_name"]))
                if note and not (rule is not None and
                                 candidate["identifier_strength"] != "free_text"):
                    candidate["note"] = note
                if record["run_length"] > 1:
                    candidate["key_run"] = {"index": record["run_index"],
                                            "length": record["run_length"]}
                candidates.append(candidate)
        return {"schema": "plx-source-mirror-resolve/v1", "literal": literal.strip(),
                "match": "exact, case-insensitive, trimmed",
                "candidate_count": len(candidates), "candidates": candidates[:limit],
                "truncated": len(candidates) > limit, "selection_required": True,
                "coverage": {"searched": searched, "not_mirrored": not_mirrored,
                             "not_in_upstream_catalog": not_in_catalog,
                             "failed": failed},
                "note": "Candidates are listed in relation/column/key order, not ranked; "
                        "the mirror never selects a winner. Only relations visible to "
                        "the caller are searched or counted.",
                **self._label(item["relation"] for item in searched)}

    def resolve_citation(self, handle: str, actor: Actor, *,
                         max_staleness: float | None = None) -> dict[str, Any]:
        """The exact stored row a citation names, or a precise error."""
        try:
            relation, key_json, key, capture_id, generation = _parse_handle(handle)
        except ValueError:
            return {"ok": False, "error": "invalid_handle", "citation": handle,
                    "message": "expected api://<schema.relation>/"
                               "<url-escaped key JSON>@<generation>-<capture-id>"}
        audience = self._audience()
        if not isinstance(actor, Actor) or not audience.allows(actor, relation):
            return {"ok": False, "error": "not_visible", "citation": handle,
                    "message": "the cited relation is not visible to this actor"}
        try:
            self._check_binding(audience.tenant)
        except MirrorError as exc:
            return {"ok": False, "error": exc.code, "citation": handle, "message": str(exc)}
        # Capture ids are local to one mirror file; a handle minted by another
        # generation (a rebuilt mirror) must never resolve to reused ids.
        foreign = (generation != self.generation if generation is not None else
                   self._legacy_capture_max is None or capture_id > self._legacy_capture_max)
        refresh = None
        if max_staleness is not None and self._state(relation) is not None:
            try:
                limit = self._staleness_limit(max_staleness)
            except MirrorError as exc:
                return {"ok": False, "error": exc.code, "citation": handle,
                        "message": str(exc)}
            try:
                _, refresh = self._ensure_fresh(relation, audience.tenant, limit)
            except MirrorError as exc:
                # A repin by another process during the refresh: refuse as a
                # typed result, like every other citation error.
                return {"ok": False, "error": exc.code, "citation": handle,
                        "message": str(exc)}
        self._sync_rows(relation)
        lookup = self._citation_lookup([(relation, key_json, capture_id)])
        return self._citation_result(handle, relation, key, capture_id, foreign, lookup, 0,
                                     refresh)

    def resolve_citations(self, handles: Sequence[str], actor: Actor) -> dict[str, Any]:
        """Resolve many citations offline in a fixed number of queries.

        Each result equals what :meth:`resolve_citation` returns for that
        handle without ``max_staleness`` (no refresh). Visibility is checked
        per relation, once, against one audience read.
        """
        if (isinstance(handles, (str, bytes)) or not isinstance(handles, Sequence) or
                len(handles) > MAX_BATCH_HANDLES):
            raise MirrorError("invalid_handles",
                              f"handles must be a list of at most {MAX_BATCH_HANDLES}")
        audience = self._audience()
        binding_error = None
        try:
            self._check_binding(audience.tenant)
        except MirrorError as exc:
            binding_error = exc
        results: list[dict[str, Any] | None] = [None] * len(handles)
        visible: dict[str, bool] = {}
        pending = []
        for index, handle in enumerate(handles):
            try:
                relation, key_json, key, capture_id, generation = _parse_handle(handle)
            except ValueError:
                results[index] = {"ok": False, "error": "invalid_handle", "citation": handle,
                                  "message": "expected api://<schema.relation>/"
                                             "<url-escaped key JSON>@<generation>-<capture-id>"}
                continue
            if relation not in visible:
                visible[relation] = isinstance(actor, Actor) and audience.allows(actor, relation)
            if not visible[relation]:
                results[index] = {"ok": False, "error": "not_visible", "citation": handle,
                                  "message": "the cited relation is not visible to this actor"}
            elif binding_error is not None:
                results[index] = {"ok": False, "error": binding_error.code, "citation": handle,
                                  "message": str(binding_error)}
            else:
                foreign = (generation != self.generation if generation is not None else
                           self._legacy_capture_max is None or
                           capture_id > self._legacy_capture_max)
                pending.append((index, handle, relation, key_json, key, capture_id, foreign))
        for relation in sorted({item[2] for item in pending}):
            self._sync_rows(relation)
        lookup = self._citation_lookup([(item[2], item[3], item[5]) for item in pending])
        for position, (index, handle, relation, _, key, capture_id, foreign) in \
                enumerate(pending):
            results[index] = self._citation_result(handle, relation, key, capture_id, foreign,
                                                   lookup, position, None)
        return {"schema": "plx-source-mirror-resolve-batch/v1", "count": len(results),
                "ok_count": sum(1 for item in results if item and item.get("ok")),
                "results": results, "atomic_upstream_snapshot": False,
                "deletions_proven": False, **self._label(item[2] for item in pending)}

    def _citation_lookup(self, items: Sequence[tuple[str, str, int]]) -> dict[str, Any]:
        """Stored content for (relation, key JSON, capture id) items in five queries."""
        found: dict[str, Any] = {"current": [[] for _ in items],
                                 "version": [[] for _ in items],
                                 "known": [False] * len(items), "captures": {}, "states": {},
                                 "copied": self._copied()}
        if not items:
            return found
        self._db.execute("""CREATE TEMP TABLE IF NOT EXISTS cite_batch (
            idx INTEGER PRIMARY KEY, relation TEXT NOT NULL, key_json TEXT NOT NULL,
            capture_id INTEGER NOT NULL)""")
        # One read transaction: every item is judged against the same state.
        self._db.execute("BEGIN")
        try:
            self._db.execute("DELETE FROM temp.cite_batch")
            self._db.executemany("INSERT INTO temp.cite_batch VALUES (?, ?, ?, ?)",
                                 [(index, *item) for index, item in enumerate(items)])
            for record in self._db.execute(
                    """SELECT b.idx AS batch_idx, r.*, """ + _REPLACED_SQL.format(r="r") +
                    ", {} AS seen_capture, {} AS seen_at".format(*_sighting_sql("r")) +
                    """ FROM temp.cite_batch b
                       JOIN row_current r ON r.relation = b.relation AND r.key_json = b.key_json
                       ORDER BY b.idx, r.run_index"""):
                found["current"][record["batch_idx"]].append(record)
            for record in self._db.execute(
                    """SELECT b.idx AS batch_idx, v.* FROM temp.cite_batch b
                       JOIN row_version v ON v.relation = b.relation
                        AND v.key_json = b.key_json AND v.capture_id = b.capture_id
                       ORDER BY b.idx, v.run_index"""):
                found["version"][record["batch_idx"]].append(record)
            for (index,) in self._db.execute(
                    """SELECT b.idx FROM temp.cite_batch b WHERE EXISTS (
                       SELECT 1 FROM row_version v WHERE v.relation = b.relation
                       AND v.key_json = b.key_json)"""):
                found["known"][index] = True
            for record in self._db.execute(
                    """SELECT c.*, s.state AS capture_state FROM capture c
                       LEFT JOIN capture_state s ON s.capture_id = c.capture_id
                       WHERE c.capture_id IN (SELECT capture_id FROM temp.cite_batch
                       UNION SELECT v.superseded_by_capture FROM temp.cite_batch b
                       JOIN row_version v ON v.relation = b.relation
                       AND v.key_json = b.key_json AND v.capture_id = b.capture_id)"""):
                found["captures"][record["capture_id"]] = record
            for record in self._db.execute(
                    """SELECT * FROM relation_state WHERE relation IN
                       (SELECT relation FROM temp.cite_batch)"""):
                found["states"][record["relation"]] = record
            self._db.execute("DELETE FROM temp.cite_batch")
        finally:
            self._db.execute("COMMIT")
        return found

    def _citation_result(self, handle: str, relation: str, key: dict[str, Any],
                         capture_id: int, foreign: bool, lookup: Mapping[str, Any],
                         index: int, refresh: Mapping[str, Any] | None) -> dict[str, Any]:
        state = lookup["states"].get(relation)
        current = lookup["current"][index]
        cited = (current if not foreign and current and current[0]["capture_id"] == capture_id
                 else None)
        status = "current"
        if cited is None and not foreign:
            cited = lookup["version"][index] or None
            status = "superseded"
        if cited is None:
            known = bool(current) or lookup["known"][index]
            if known and foreign:
                return {"ok": False, "error": "capture_unknown",
                        "reason": "mirror_generation_mismatch", "citation": handle,
                        "mirror_generation": self.generation,
                        "message": "the citation was minted by another mirror generation "
                                   "(the mirror was rebuilt); its content is not held here "
                                   "and cannot be verified"}
            if known:
                return {"ok": False, "error": "capture_unknown", "citation": handle,
                        "message": "the row exists but no retained content has this capture id"}
            return {"ok": False, "error": "not_found", "citation": handle,
                    "message": "no mirrored row has this key"}
        rows = [json.loads(record["row_json"]) for record in cited]
        lineages = sorted({record["lineage"] for record in cited},
                          key=lambda value: (value is None, value or ""))
        result: dict[str, Any] = {
            "ok": True, "status": status, "citation": handle, "relation": relation,
            "key": key, "mirror_generation": self.generation,
            "changed_since": status == "superseded",
            "capture": _capture_dict(lookup["captures"].get(capture_id)),
            "content_last_seen_at": _seen(cited[0], "at"),
            "content_last_seen_capture": _seen(cited[0], "capture"),
            "relation_last_refresh_at": state["last_refresh_at"] if state else None,
            "lineage": lineages[0] if len(lineages) == 1 else "mixed",
            "lineage_rule": cited[0]["lineage_basis"] if len(lineages) == 1 else None,
            "atomic_upstream_snapshot": False, "deletions_proven": False}
        if len(rows) == 1:
            result["row"] = rows[0]
        else:
            result.update(rows=rows, key_unique=False)
        if status == "superseded":
            result["superseded_by_capture"] = cited[0]["superseded_by_capture"]
            # When the superseding capture completed, as the projection dates the version's
            # end (``ontology.project.MirrorCaptures``); None while it has not.
            later = lookup["captures"].get(cited[0]["superseded_by_capture"])
            result["superseded_at"] = (later["finished_at"] if later is not None and
                                       (later["capture_state"] or "complete") == "complete"
                                       else None)
            result["current_citation"] = (make_citation(relation, key, current[0]["capture_id"],
                                                        self.generation)
                                          if current else None)
            result["content_equal_to_current"] = bool(current) and sorted(
                r["row_digest"] for r in current) == sorted(r["row_digest"] for r in cited)
        else:
            result["current_citation"] = make_citation(relation, key, capture_id,
                                                       self.generation)
        if current:
            absent = current[0]["absent_since_capture"]
            basis = current[0]["missing_basis"] or ""
            result["upstream_presence"] = (
                ("absent_from_latest_key_scan" if basis.startswith("key_scan:")
                 else "absent_from_latest_full_traversal") if absent is not None else
                "observed_in_latest_full_traversal"
                if state and _seen(current[0], "capture") >= state["last_full_capture_id"]
                else "not_rechecked_since_latest_full_traversal")
            if absent is not None:
                result["absent_since_capture"] = absent
                result["missing_from_source_since"] = current[0]["missing_from_source_since"]
                result["missing_basis"] = current[0]["missing_basis"]
                result.update(self._replacement(relation, current[0]))
            if current[0]["deleted_in_source"] is not None:
                result["deleted_in_source"] = current[0]["deleted_in_source"]
                result["deleted_basis"] = current[0]["deleted_basis"]
        if refresh is not None:
            result["refresh"] = refresh
        result["verification_note"] = (
            "changed_since reflects captures up to relation_last_refresh_at; changes "
            "invisible to the since column appear only after a full traversal or a key scan "
            "(missing_from_source_since).")
        freshness = (dict(self.fallback) if self.fallback is not None
                     else lookup["copied"].get(relation))
        if freshness is not None:
            result.update(source_freshness=freshness, freshness_label=freshness["label"])
        return result


def _capture_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"capture_id": row["capture_id"], "relation": row["relation"],
            "mode": row["mode"], "reason": row["reason"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "since_sent": row["since_sent"], "catalog_revision": row["catalog_revision"],
            "page_count": row["page_count"], "rows_received": row["rows_received"],
            "state": (row["capture_state"] or "complete")
                     if "capture_state" in row.keys() else "complete",
            "health_before": json.loads(row["health_before_json"]),
            "health_after": json.loads(row["health_after_json"]),
            "atomic_upstream_snapshot": False, "deletions_proven": False}


def _check_where(where: Any) -> list[tuple[str, str, Any]]:
    if isinstance(where, (str, bytes)) or not isinstance(where, Sequence) or len(where) > 20:
        raise MirrorError("invalid_where", "where must be a list of at most 20 clauses")
    result = []
    for clause in where:
        if (isinstance(clause, (str, bytes)) or not isinstance(clause, Sequence) or
                len(clause) != 3):
            raise MirrorError("invalid_where", "each clause is (column, op, value)")
        column, op, value = clause
        if not isinstance(column, str) or _COLUMN.fullmatch(column) is None:
            raise MirrorError("invalid_where", "where column name invalid")
        if op not in _WHERE_OPS:
            raise MirrorError("invalid_where", f"op must be one of {', '.join(_WHERE_OPS)}")
        scalar = (str, int, float)
        if op == "is_null":
            if value is None:
                value = True
            if not isinstance(value, bool):
                raise MirrorError("invalid_where", "is_null takes true or false")
        elif op == "in":
            if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or
                    not 1 <= len(value) <= 200 or
                    any(isinstance(item, bool) or not isinstance(item, scalar)
                        for item in value)):
                raise MirrorError("invalid_where", "in takes a list of 1..200 scalars")
            value = list(value)
        elif op == "prefix":
            if not isinstance(value, str) or not value:
                raise MirrorError("invalid_where", "prefix takes a non-empty string")
        elif isinstance(value, bool) or not isinstance(value, scalar):
            raise MirrorError("invalid_where", f"{op} takes a string or number")
        if isinstance(value, float) and not math.isfinite(value):
            raise MirrorError("invalid_where", "numbers must be finite")
        result.append((column, op, value))
    return result


def _check_order(order: Any) -> list[tuple[str, bool]]:
    if isinstance(order, (str, bytes)) or not isinstance(order, Sequence) or len(order) > 5:
        raise MirrorError("invalid_order", "order must be a list of at most 5 items")
    result = []
    for item in order:
        if isinstance(item, str):
            column, direction = item, "asc"
        elif isinstance(item, Sequence) and len(item) == 2:
            column, direction = item
        else:
            raise MirrorError("invalid_order", "order item is column or (column, asc|desc)")
        if (not isinstance(column, str) or _COLUMN.fullmatch(column) is None or
                direction not in ("asc", "desc")):
            raise MirrorError("invalid_order", "order item is column or (column, asc|desc)")
        result.append((column, direction == "desc"))
    return result


def _matches(row: Mapping[str, Any], column: str, op: str, value: Any) -> bool:
    cell = row.get(column)
    if op == "is_null":
        return (cell is None) is value
    if cell is None:
        return False
    if op == "eq":
        return _text(cell) == _text(value)
    if op == "in":
        return _text(cell) in {_text(item) for item in value}
    if op == "prefix":
        text = _text(cell)
        return text is not None and text.startswith(value)
    left, right = _number(cell), _number(value)
    if left is None or right is None:
        if not isinstance(cell, str) or not isinstance(value, str):
            return False
        left, right = cell, value
    return left >= right if op == "gte" else left <= right


_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")


def _sql_prefilter(column: str, op: str, value: Any) -> tuple[str, list[Any]]:
    """A SQLite condition over ``row_json`` that every exact match satisfies.

    It narrows the candidates without changing the result: ``_matches``
    decides every candidate. Text and integer cells are compared in SQL;
    other JSON types pass through to the exact check when they could match.
    """
    path = f'$."{column}"'
    kind = "json_type(row_json, ?)"
    if op == "is_null":
        if value:
            return f"({kind} IS NULL OR {kind} = 'null')", [path, path]
        return f"{kind} <> 'null'", [path]
    if op in ("gte", "lte"):
        return f"{kind} <> 'null'", [path]
    if op == "prefix":
        return (f"(({kind} = 'text' AND substr(json_extract(row_json, ?), 1, ?) = ?) OR "
                f"{kind} IN ('integer', 'real', 'true', 'false', 'object', 'array'))",
                [path, path, len(value), value, path])
    texts, integers, others = set(), set(), set()
    for item in (value if op == "in" else [value]):
        text = _text(item)
        texts.add(text)
        if isinstance(item, int) or (isinstance(item, str) and _INTEGER_TEXT.fullmatch(item)):
            number = int(item)
            if -2 ** 63 <= number < 2 ** 63:
                integers.add(number)
            else:  # SQLite cannot bind it; let every integer cell through
                others.add("integer")
        if isinstance(item, float) or (isinstance(item, str) and
                                       _NUMERIC_TEXT.fullmatch(item.strip())):
            others.add("real")
        if text in ("true", "false"):
            others.update(("true", "false"))
        if text.startswith(("{", "[")):
            others.update(("object", "array"))
    parts = [f"({kind} = 'text' AND json_extract(row_json, ?) IN "
             f"({','.join('?' for _ in texts)}))"]
    params: list[Any] = [path, path, *sorted(texts)]
    if integers:
        parts.append(f"({kind} = 'integer' AND json_extract(row_json, ?) IN "
                     f"({','.join('?' for _ in integers)}))")
        params += [path, path, *sorted(integers)]
    if others:
        parts.append(f"{kind} IN ({','.join('?' for _ in others)})")
        params += [path, *sorted(others)]
    return "(" + " OR ".join(parts) + ")", params


# ---------------------------------------------------------------- CLI

def _build_client(base_url: str | None, key_env: str) -> Any:
    key = os.environ.get(key_env)
    if not base_url or not key:
        return None
    from .workbench_read_api import WorkbenchReadApiClient
    return WorkbenchReadApiClient(base_url, key)


def mirror_options(config: Mapping[str, Any]) -> dict[str, Any]:
    """``SourceMirror`` keyword options from a loaded ingest configuration."""
    sync = config.get("sync", {})
    names = {"page_size": "page_size", "min_page_size": "min_page_size",
             "max_rows_per_relation": "max_rows_per_relation",
             "throttle_attempts": "throttle_attempts",
             "max_retry_after_seconds": "max_retry_after_seconds"}
    options: dict[str, Any] = {target: sync[name] for name, target in names.items()
                               if name in sync}
    if config.get("projections"):
        options["projections"] = config["projections"]
    options["lineage_rules"] = config["lineage_rules"]
    options["soft_delete_rules"] = config["soft_delete_rules"]
    return options


PROBE_PRINCIPAL = "service:mirror-full-coverage-probe"


def soft_delete_counts(coverage: Mapping[str, Any]) -> dict[str, Any]:
    """Counts only: rows the source soft-deleted, in total and per relation whose
    catalog carries a declared soft-delete column (relation names, never values)."""
    declared = {item["relation"]: item["rows_deleted_in_source"]
                for item in coverage["relations"] if item["soft_delete_columns"]}
    return {"rows_deleted_in_source_total": coverage["rows_deleted_in_source_total"],
            "soft_delete_relation_count": len(declared),
            "soft_deleted_rows": dict(sorted(declared.items()))}


def full_coverage_probe(client: Any, db_path: str | Path, *,
                        budget_seconds: float | None = None, resolve_sample: int = 5000,
                        ingest: str | Path | Mapping[str, Any] | None = None,
                        absence_scan: bool = False,
                        order: Sequence[str] | None = None,
                        monotonic: Callable[[], float] = time.monotonic,
                        **options: Any) -> dict[str, Any]:
    """Full sync of every served relation, then one batch resolve: counts and shapes only.

    The audience grants one service principal every schema the live catalog
    serves, because the probe measures the mirror, not an employee's view; it
    prints relation names, counts, booleans and timings, never a row value,
    key or citation. ``order`` (see :meth:`SourceMirror.sync`) captures those
    relations first inside the one budget; the report then adds their outcomes.
    """
    if (isinstance(resolve_sample, bool) or not isinstance(resolve_sample, int) or
            not 0 <= resolve_sample <= MAX_BATCH_HANDLES):
        raise ValueError("resolve_sample must be 0..%d" % MAX_BATCH_HANDLES)
    actor = Actor("plx", frozenset({PROBE_PRINCIPAL}))
    config = load_ingest_config(ingest)
    started = monotonic()
    with SourceMirror(db_path, client, AudienceMap({}, tenant="plx"), tenant="plx",
                      **{**mirror_options(config), **options}) as mirror:
        # The catalog read goes through the mirror's 429/503 retry policy.
        catalog, _, error = mirror._catalog(force=True)
        if error:
            raise MirrorError("source_unavailable", error)
        schemas = sorted({name.split(".", 1)[0] for name in catalog})
        mirror.set_audience_map(AudienceMap(
            {f"{schema}.*": [PROBE_PRINCIPAL] for schema in schemas}, tenant="plx"))
        report = mirror.sync(actor, absence_scan=absence_scan, budget_seconds=budget_seconds,
                             order=order)
        sync_seconds = monotonic() - started
        sample = mirror._db.execute(
            """SELECT relation, key_json, MIN(capture_id) AS capture_id FROM row_current
               WHERE absent_since_capture IS NULL GROUP BY relation, key_json
               ORDER BY relation, key_json LIMIT ?""", (resolve_sample,)).fetchall()
        handles = [make_citation(row["relation"], json.loads(row["key_json"]),
                                 row["capture_id"], mirror.generation) for row in sample]
        resolve_started = monotonic()
        batch = mirror.resolve_citations(handles, actor)
        resolve_seconds = monotonic() - resolve_started
    coverage = report["coverage"]
    lineage_totals: dict[str, int] = {}
    for item in coverage["relations"]:
        for lineage, count in item["lineage_counts"].items():
            lineage_totals[lineage] = lineage_totals.get(lineage, 0) + count
    outcomes: dict[str, int] = {}
    first = set(order or ())
    first_outcomes: dict[str, int] = {}
    for run in report["runs"]:
        outcomes[run["outcome"]] = outcomes.get(run["outcome"], 0) + 1
        if run["relation"] in first:
            first_outcomes[run["outcome"]] = first_outcomes.get(run["outcome"], 0) + 1
    ordered = {}
    if order is not None:
        ordered = {"capture_order": "ordered_first",
                   "ordered_in_catalog_count": len(first & set(catalog)),
                   "ordered_run_outcomes": first_outcomes}
    return {
        "probe": "mirror-full-coverage/v1",
        **ordered,
        "relation_count": len(catalog),
        "catalog_relation_count": coverage["catalog_relation_count"],
        "served_relation_count": coverage["served_relation_count"],
        "unavailable_relation_count": coverage["unavailable_relation_count"],
        "complete_relation_count": coverage["complete_relation_count"],
        "all_served_complete": coverage["all_served_complete"],
        "complete_equals_catalog": (coverage["complete_relation_count"] ==
                                    coverage["catalog_relation_count"] == len(catalog)),
        "in_progress_count": coverage["in_progress_count"],
        "failed_count": coverage["failed_count"],
        "run_outcomes": outcomes,
        "rows_current_total": coverage["rows_current_total"],
        "rows_missing_total": coverage["rows_missing_total"],
        **soft_delete_counts(coverage),
        "lineage_totals": lineage_totals,
        "relations": {item["relation"]: {
            "complete": item["complete"], "rows_current": item["rows_current"],
            "rows_missing": item["rows_missing_from_source"],
            "rows_deleted": item["rows_deleted_in_source"],
            "pages": (item["latest_capture"] or {}).get("pages")}
            for item in coverage["relations"]},
        "sync_seconds": round(sync_seconds, 3),
        "throttle_waits": report["throttle_waits"],
        "handles_resolved": len(handles),
        "handles_ok": batch["ok_count"],
        "resolve_all_ok": batch["ok_count"] == len(handles),
        "batch_resolve_seconds": round(resolve_seconds, 3),
        "batch_resolve_under_3s": resolve_seconds < 3.0,
        "atomic_upstream_snapshot": False, "deletions_proven": False}


SCAN_PAGE = 500
_SCAN_STALENESS_MAX = 86_400.0 * 365


def scan_relation(mirror: Any, actor: Actor, relation: str, limit: int,
                  max_staleness: float | None) -> dict[str, Any]:
    """Every row of one relation for ``actor`` in bounded pages of one capture, or the
    denial. The first page refreshes the mirror per ``max_staleness``; a capture that
    changes between pages stops the scan (``mirror_changed_during_scan``) and a scan cut
    at ``limit`` says so (``scan_bound_reached``): neither is ``complete``. A Portal
    failure carries its HTTP status and error code (``upstream``) for the live verifier's
    ``blocked_upstream``."""
    rows: list[dict[str, Any]] = []
    reasons: list[str] = []
    offset, first, columns = 0, None, []
    try:
        while True:
            page = mirror.rows(actor, relation, columns=None, limit=min(SCAN_PAGE, limit),
                               offset=offset, max_staleness=(
                                   max_staleness if first is None else _SCAN_STALENESS_MAX))
            signature = (page["freshness"].get("latest_capture_id"), page["total_matched"])
            if first is None:
                first, columns = signature, list(page["columns"])
            elif signature != first:
                reasons.append("mirror_changed_during_scan")
                break
            rows.extend(page["rows"])
            offset += page["returned"]
            if page["next_offset"] is None or not page["returned"]:
                break
            if len(rows) >= limit:
                reasons.append("scan_bound_reached")
                break
    except MirrorError as exc:
        status = "not_visible" if exc.code == "not_visible" else "unavailable"
        upstream = ({"http_status": exc.status} if isinstance(exc.status, int) else {}) | (
            {"error_code": exc.upstream_code} if exc.upstream_code else {})
        return {"status": status, "rows": [], "columns": [], "complete": False,
                "reasons": [status if status == "not_visible" else f"mirror_error:{exc.code}"],
                **({"upstream": upstream} if exc.code == "source_unavailable" else {})}
    return {"status": "read", "rows": rows, "columns": columns, "complete": not reasons,
            "reasons": reasons}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.source_mirror",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--audience", required=True, type=Path,
                        help="audience JSON (schema plx-source-mirror-audience/v1)")
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--clearance", default="internal", choices=sorted(LEVELS))
    parser.add_argument("--base-url", default=os.environ.get("PLX_WORKBENCH_API_URL"))
    parser.add_argument("--key-env", default="PLX_WORKBENCH_API_KEY",
                        help="name of the environment variable holding the plxwb_ key")
    commands = parser.add_subparsers(dest="command", required=True)
    catalog = commands.add_parser("catalog")
    catalog.add_argument("--refresh-catalog", action="store_true")
    refresh = commands.add_parser("refresh")
    refresh.add_argument("relation")
    refresh.add_argument("--full", action="store_true")
    rows = commands.add_parser("rows")
    rows.add_argument("relation")
    rows.add_argument("--columns", help="comma-separated column names")
    rows.add_argument("--where", default="[]",
                      help='JSON list of [column, op, value], e.g. [["id","eq",1]]')
    rows.add_argument("--order", action="append", default=[],
                      help="column or column:desc; repeatable")
    rows.add_argument("--limit", type=int, default=100)
    rows.add_argument("--max-staleness", type=float)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("literal")
    resolve.add_argument("--fill-missing", action="store_true")
    resolve.add_argument("--max-staleness", type=float)
    cite = commands.add_parser("cite")
    cite.add_argument("handle")
    cite.add_argument("--max-staleness", type=float)
    cite_batch = commands.add_parser("cite-batch", help="resolve handles (one per line) offline")
    cite_batch.add_argument("handles_file", type=Path)
    sync = commands.add_parser("sync", help="capture every visible served relation")
    sync.add_argument("--relation", action="append")
    sync.add_argument("--full", action="store_true")
    sync.add_argument("--absence-scan", action="store_true")
    sync.add_argument("--budget-seconds", type=float)
    commands.add_parser("coverage", help="per-relation completeness counts")
    scan = commands.add_parser("scan-absence", help="key-only absence scan of one relation")
    scan.add_argument("relation")
    scan.add_argument("--budget-seconds", type=float)
    rows.add_argument("--include-missing", action="store_true")
    parser.add_argument("--ingest-config", type=Path,
                        help="ingest configuration "
                             "(default integrations/source_mirror_ingest.json)")
    args = parser.parse_args(argv)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", args.key_env) is None:
        parser.error("--key-env must name an environment variable")
    try:
        actor = Actor(args.tenant, frozenset(args.principal), args.clearance)
        client = _build_client(args.base_url, args.key_env)
        with SourceMirror(args.db, client, args.audience, tenant=args.tenant,
                          **mirror_options(load_ingest_config(args.ingest_config))) as mirror:
            if args.command == "catalog":
                result = mirror.catalog(actor, refresh_catalog=args.refresh_catalog)
            elif args.command == "refresh":
                result = mirror.refresh(actor, args.relation, full=args.full)
            elif args.command == "rows":
                order = [item.split(":", 1) if ":" in item else item for item in args.order]
                result = mirror.rows(actor, args.relation,
                                     columns=args.columns.split(",") if args.columns else None,
                                     where=json.loads(args.where), order=order,
                                     limit=args.limit, max_staleness=args.max_staleness,
                                     include_missing=args.include_missing)
            elif args.command == "resolve":
                result = mirror.resolve(actor, args.literal, fill_missing=args.fill_missing,
                                        max_staleness=args.max_staleness)
            elif args.command == "cite":
                result = mirror.resolve_citation(args.handle, actor,
                                                 max_staleness=args.max_staleness)
            elif args.command == "cite-batch":
                handles = [line.strip() for line in
                           args.handles_file.read_text(encoding="utf-8").splitlines()
                           if line.strip()]
                result = mirror.resolve_citations(handles, actor)
            elif args.command == "sync":
                result = mirror.sync(actor, relations=args.relation, full=args.full,
                                     absence_scan=args.absence_scan,
                                     budget_seconds=args.budget_seconds)
            elif args.command == "coverage":
                result = mirror.coverage(actor)
            else:
                result = mirror.scan_absence(actor, args.relation,
                                             budget_seconds=args.budget_seconds)
    except MirrorError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": "invalid_request", "message": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1, sort_keys=True, ensure_ascii=False))
    return 0


__all__ = ["AUDIENCE_SCHEMA", "AudienceMap", "DEFAULT_IDENTIFIERS", "IDENTIFIER_NOTES",
           "INGEST_SCHEMA", "LINEAGES", "MAX_BATCH_HANDLES", "classify_lineage",
           "full_coverage_probe", "load_ingest_config", "mirror_options", "scan_relation",
           "PO_REFERENCE_STRENGTH_RULE", "ROW_STRENGTH_RULES", "portal_trim_text",
           "sales_order_po_reference_strength",
           "MAX_ROWS_LIMIT", "MirrorError", "PERSISTED_FALLBACK_META", "POINTER_TABLE",
           "SourceMirror", "attach_scoped",
           "citation_generation",
           "make_citation", "main", "parse_citation", "parse_decimal",
           "MISSING_FROM_SOURCE", "SOFT_DELETE", "SOFT_DELETE_OBSERVED",
           "SOURCE_REPLACEMENT", "AMBIGUOUS_REPLACEMENT", "PRISMA_FACTS_PATH",
           "generated_replacement_models", "natural_text", "pair_replacements",
           "replacement_key", "replacement_models", "stale_replacement_relations",
           "soft_delete_columns", "soft_delete_counts"]


if __name__ == "__main__":
    raise SystemExit(main())
