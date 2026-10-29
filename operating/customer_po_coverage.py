"""Customer-PO coverage of Portal sales orders, cited and permission-filtered.

Vince's pain point (live evidence 2026-09-30): Portal sales orders often lack
the customer's purchase-order number. The reporting API exposes two fields that
can hold one on ``public.app_sales_order``: ``po_reference`` and the JSON key
``custom_fields.custPoCode``. This module answers, for the calling actor, which
orders lack a customer PO in *every* exposed field, with each order's
``api://`` citation (its CLI is the ``case.customer_po_coverage`` live check).

Everything is read through ``SourceMirror.rows`` for the calling actor, so the
mirror's relation-family audience (default deny) runs before any fill, read or
count: an actor without a grant gets the mirror's ``not_visible`` denial and no
rows, no counts. Nothing here writes Portal, any other system or a case.

Rules (deterministic, documented in devloop/CASE-RUNTIME.md section 4):

* Origin, from ``custom_fields`` of the cited row only, mirroring the Portal
  (petralabx/plx-customer-portal staging@8da33e4, the cited files unchanged
  since 6c9415973; ``devloop/CONTEXT/03-PORTAL.md`` sections 3.2-3.4 and 9):
  Sage-backed as in the JS predicate
  ``lib/wms/sage-ship-queue.ts:isSageBackedSalesOrder`` (not Portal's SQL
  helpers, which differ): ``sageOrdUniq`` or ``sageSourceOrdUniq`` non-empty
  under Portal's ``trimText`` (``String(v).trim()``, so a number counts)
  first, then trimmed ``source ==
  "sage.oe_order_header"`` -> ``sage_import``, winning over ``fmId`` as
  ``isFmOnlySalesOrder`` does and over any other ``source`` value;
  ``source == "fm-oe-ensure"`` with ``fmId`` (FM-ensure created the order) or a
  non-empty ``fmId`` without ``source`` -> ``fm_migrated``; ``source ==
  "gold-so-bridge"`` (entered in Portal, bridged to FM) or no marker ->
  ``portal_entered``. Another ``source`` value, an ``fmId`` key without a value
  (and no Sage marker), ``fm-oe-ensure`` without ``fmId``, an unparseable or
  non-object ``custom_fields``, or a relation mirrored without
  ``custom_fields`` -> ``unknown``. The rule that fired and the ``lineage``
  (which Portal path most likely produced the row) are reported with every
  order, and each present PO field lists ``possible_writers``, inferred from
  the lineage, never audited last-writer provenance (``custom_fields.custPoCode`` only
  ever comes from the legacy ``migrate-fm-to-portal.sql``; a Sage
  ``po_reference`` is ``COALESCE(ponumber, po_reference)``, so text there is
  Sage's own PO field or an earlier value). ``po_reference`` is not free text
  in Portal: ``resolveOrderByNumber`` matches it as an order identity, together with
  the order's customer (a PO is unique per customer).
* A PO field equal to the order's own number (``order_number``,
  ``custom_fields.fmOeNumber`` or ``renumberedTo``) is
  ``present_equals_order_number``. Equality is ambiguous, not proof: the
  Portal bridge computes FM custpocode = ``po_reference ?? order_number`` and
  FM-ensure copies custpocode into an empty ``po_reference``, so the value may
  be that fallback, but a person may also have typed the order number. It is
  conservatively not counted as a customer PO and not as a not-PO-like miss.
* PO shape (conservative: a value is PO-like only if it plausibly is an
  identifier, so a name or a word is never counted as a PO): after trimming,
  3..40 characters from ``A-Z a-z 0-9 - _ / . #``, starting and ending with a
  letter or digit, no whitespace, at least one digit, and not only zeros.
  Anything else in a PO field is ``present_not_po_like`` and its value is never
  echoed (it can be a person's name). A real PO written as ``PO 4500`` is
  therefore reported as not PO-like: a false "needs review" is recoverable, a
  false "has a PO" hides a missing one. Each such field says why
  (``reasons``: ``not_text``, ``too_short``, ``too_long``, ``whitespace``,
  ``no_digit``, ``only_zeros``, ``disallowed_chars``, ``edge_not_alphanumeric``)
  and whether it is a ``probable_po_after_normalization``: PO-like once a
  leading PO marker (``PO``, ``P.O.``, ``P/O``, ``PO#``, ``PO No:``,
  ``Purchase Order``, optionally qualified ``Cust``/``Customer``/``Client``;
  or a bare ``#``) and the whitespace between the digit-bearing groups after
  it are removed (``PO 4500``, ``P.O. 4500``, ``P.O. #4500``, ``PO 4500 12``,
  ``Cust PO 4500``). Only with a PO word marker may one wrapping bracket/quote
  pair and trailing ``.,;:`` be dropped too (``(PO 4500)``, ``PO 4500.``);
  unmarked values are never unwrapped (``4500.``, ``2026.``, ``(4500.00)``,
  ``(2026-01-05)``, ``(#204)``, ``4500,`` are not leads).
  Never a lead: ``Jane Buyer 2``; unmarked digit groups (``905 555 1234``,
  ``4500 1234``, ``2026 01 05``: indistinguishable from phones and dates);
  other words before a number (``Order 4500``, ``per email 45001``,
  ``Invoice 12345``, ``Suite # 204``, ``Customer # 4500``); anything after the
  number that has no digit (``PO 1920 pcs``, ``PO Box 123``). Joining stops
  being safe at a second marker (``PO4500 PO4501`` names two POs, so it is not
  flagged), and bare ``PO`` is a marker only when no letter follows it
  (``Polo2 123``, ``POA1 2`` are not). A value over 80 characters is never
  flagged (known limitation: a PO buried in a long note is not a lead; the
  field still counts as not PO-like). Known leftover: a marker before a non-PO
  number (``PO 2026``) is flagged. A probable PO is a review
  lead, never counted as ``has_customer_po``, and the value is still not echoed;
  its reason codes and the lead reach the case gap text. Aggregates also count
  coarse, value-free shape classes of not-PO-like fields
  (``by_origin_field_not_po_like_shape``, see ``SHAPE_NOTE``) so a live run
  can measure why normalization rejects them; ``python3 -m
  operating.customer_po_coverage`` prints these counts (plus combined shape
  signatures) and nothing else.
* FM order PO (c9e0ba19e1): for an order with ``custom_fields.fmId`` the
  linked FM ``public.order_entry`` row is read through the mirror (same
  audience) and its ``custpocode`` reported in ``fm_order_entry`` with its own
  citation. On Portal-created FM rows (id >= 900000) the bridge wrote
  ``custpocode = po_reference ?? order_number`` and ``erpcode =
  order_number``, so a custpocode equal to ``erpcode`` or to the order's own
  number is no PO. The order's ``status`` still describes the cited Portal
  row only; ``fm_order_entry_holds_customer_po`` marks the linked lead.
* ``absent_in_this_source`` means only that every exposed PO field of the cited
  row was read and is empty or absent. It never means the PO does not exist;
  each such order lists the other places it may be held and whether they were
  looked at. A PO field that could not be read (column not mirrored, or
  ``custom_fields`` unparseable or not an object) or that holds a value that is
  not text or an integer (a float, boolean, list or object) is
  ``not_inspectable``, and an order with such a field and no PO-like value is
  ``po_fields_not_inspectable``: never absent, never a gap of absence, and
  never eligible for a "has no PO" proposal.
* Date filters compare calendar dates or instants, never strings: a
  ``YYYY-MM-DD`` bound compares with the value's own calendar date (so
  ``date_to`` includes that whole day); a timestamp bound compares instants
  (naive timestamps are read as UTC). Invalid or reversed bounds are refused;
  a value in ``date_column`` that is not an ISO date (Postgres ``+HH``/``-HH``
  offsets are accepted) is counted and makes the
  result incomplete.
* Document carriers (REC-01): :func:`document_carriers` reads the ontology
  projection, not the mirror. For every record kind a document class declares it
  ``attests`` (``ontology/schema/document_fields.json``: customer PO number and
  date, confirmation amounts, shipment dates and tracking numbers, AO
  quantities), each record this reader may see whose version is cited to the
  Portal reporting API (``api://``) is compared with the typed values the
  document-field projection extracted from linked mail attachments and
  SharePoint originals, through the one comparison ``plx.object`` runs
  (``operating.attested_conflicts.conflicts``). A value a cited document states
  and every Portal field of the property holds empty is ``missing_in_portal``
  (with the document's page/span citations and the empty rows' citations); a
  different value is ``conflict``; a blank on the paper facing a Portal value is
  ``document_blank``; equal values report nothing. Every finding is a lead
  (machine-read), never written and never authority.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from integrations.source_mirror import MAX_ROWS_LIMIT, portal_trim_text
from ontology.core import Actor

from .capability_guards import own_identifiers, property_carriers, read_field, value_state

RELATION = "public.app_sales_order"
SAGE_SOURCE_MARKER = "sage.oe_order_header"
FM_MARKER_KEY = "fmId"
# Portal (petralabx/plx-customer-portal staging@8da33e4, read in the checkout;
# unchanged since 6c9415973): lib/wms/sage-ship-queue.ts:isSageBackedSalesOrder:32 is
# sageOrdUniq OR sageSourceOrdUniq OR source == 'sage.oe_order_header', and
# isFmOnlySalesOrder:41 is fmId AND NOT Sage-backed (Sage wins).
SAGE_KEYS = ("sageOrdUniq", "sageSourceOrdUniq")
# lib/wms/ensure-sales-order-from-fm-oe.ts stamps source 'fm-oe-ensure' on gold
# SOs it creates from FM order_entry; lib/wms/ensure-fm-oe-from-sales-order.ts
# overwrites source with 'gold-so-bridge' on Portal-entered orders it bridges
# to FM.
FM_ENSURE_SOURCE = "fm-oe-ensure"
GOLD_BRIDGE_SOURCE = "gold-so-bridge"
LINEAGES = ("sage_etl", "fm_oe_ensure", "legacy_fm_sql", "fm_linked_without_source",
            "gold_so_bridge", "portal", "unknown")
# Identifiers of the order itself. The Portal bridge writes FM custpocode =
# po_reference ?? order_number (ensure-fm-oe-from-sales-order.ts), and FM-ensure
# copies custpocode into an empty po_reference on link, so a PO field can hold
# the order's own number; BC renumbering rewrites order_number and keeps the
# old one in fmOeNumber/renumberedTo. Such a value is never a customer PO.
# Both lists are schema data, read from the one declaration the gap guard reads
# (capability_guards.property_carriers): SalesOrder.customer_po's carriers on this
# relation, and the fields of its placeholder_when_equal_to identifiers.
ORDERS_DOMAIN = (Path(__file__).resolve().parents[1] / "ontology" / "schema" / "domains"
                 / "orders.json")


def _customer_po_fields() -> tuple[tuple[tuple[str, str, str | None], ...], ...]:
    types = {t["id"]: t for t in json.loads(ORDERS_DOMAIN.read_text(encoding="utf-8"))
             ["object_types"]}
    found = property_carriers(types, "SalesOrder", "customer_po")
    return tuple(tuple((item["path"], *_split(item["path"])) for item in found[kind]
                       if item["relation"] == RELATION)
                 for kind in ("sources", "placeholders"))


def _split(path: str) -> tuple[str, str | None]:
    column, _, key = path.partition(".")
    return column, key or None


PORTAL_OE_ID_BASE = 900_000  # lib/sales-order/fm-oe-bridge-ids.ts: Portal-created FM OE ids
# (report path, column, JSON key or None). CANDIDATE_FIELDS: every exposed field that
# can hold a customer PO on this relation, checked for every order whatever its origin.
CANDIDATE_FIELDS, ORDER_IDENTIFIERS = _customer_po_fields()
ORIGINS = ("sage_import", "fm_migrated", "portal_entered", "unknown")
NOT_PO_LIKE_REASONS = ("not_text", "too_short", "too_long", "whitespace", "no_digit",
                       "only_zeros", "disallowed_chars", "edge_not_alphanumeric")
_PO_ALLOWED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_/.#")
# Anchored, and no two adjacent repeats can match the same characters, so a
# failed match is linear; longer values use the single-token rule instead.
# Bare "PO" is a marker only when no letter follows it ("Polo2", "POA1" are not).
# A customer qualifier ("Cust PO", "Customer P.O.#", "Client PO") is accepted only
# before a PO marker, never before a bare "#" ("Customer # 4500" can be an
# account number). "P/O" and "Purchase Order" are PO markers; a trailing ":"
# after "#"/"No"/"Number" ("PO#: 4500", "PO No: 4500") belongs to the marker.
_PO_WORD = (r"(?:(?:(?:CUST(?:OMER)?|CLIENT)\.?[\s]*)?(?:P[\s./]*O|PURCHASE\s*ORDER)"
            r"(?:[\s.]*(?:#|NO\b\.?|NUMBER\b|:)(?:[\s]*:)?|\.?(?![A-Za-z])))")
_PO_MARKER = re.compile(r"^(?:" + _PO_WORD + r"|#)", re.IGNORECASE)
# A PO word marker, never a bare "#": only such a value may be unwrapped.
_PO_WORD_MARKER = re.compile(r"^" + _PO_WORD, re.IGNORECASE)
# One pair of wrapping brackets/quotes and trailing ".,;:" are removed only
# when what remains starts with a PO word marker ("(PO 4500)", "PO 4500.");
# unmarked values ("2026.", "(4500.00)", "(#204)", "4500,") are never unwrapped,
# so punctuation never turns a year, quantity, amount or date into a lead.
_WRAPPERS = {"(": ")", "[": "]", '"': '"', "'": "'"}
_TRAILING_PUNCT = ".,;:"
_NORMALIZE_MAX = 80
FIELD_STATUSES = ("present_po_like", "present_not_po_like", "present_equals_order_number",
                  "empty", "field_absent", "not_inspectable")
ORDER_STATUSES = ("has_customer_po", "po_fields_not_inspectable", "po_field_not_po_like",
                  "absent_in_this_source")
MAX_SCAN_ROWS = 20_000
MAX_PAGE_LIMIT = 200
_DATE_VALUE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?"
                         r"(Z|[+-]\d{2}(?::?\d{2})?)?)?$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PO_CHARS = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-_/.#]*[A-Za-z0-9])?$")
_PO_MIN, _PO_MAX = 3, 40
_STALENESS_MAX = 10 * 365 * 86400

PO_SHAPE_RULE = (
    "PO-like: after trimming, 3..40 characters from A-Z a-z 0-9 - _ / . #, starting and "
    "ending with a letter or digit, no whitespace, at least one digit, not only zeros. "
    "Anything else is present_not_po_like and is never echoed; it carries reason codes "
    "and a probable_po_after_normalization flag (a leading PO/P.O./P/O/PO#/PO No/Purchase "
    "Order marker, optionally Cust/Customer/Client-qualified, or a bare # removed, then "
    "whitespace between digit-bearing groups after it; wrapping brackets/quotes and "
    "trailing .,;: only next to a PO word marker; unmarked values are never unwrapped or "
    "joined), which is a review lead and never counts as a customer PO.")
ORIGIN_RULE = (
    "As Portal's JS isSageBackedSalesOrder (sage-ship-queue.ts): sageOrdUniq/"
    "sageSourceOrdUniq non-empty under trimText (String(v).trim(), so numbers and "
    "booleans count), then trimmed source 'sage.oe_order_header' -> sage_import (wins "
    "over fmId and any other source); 'fm-oe-ensure' + fmId or fmId alone "
    "-> fm_migrated; 'gold-so-bridge' or no marker -> portal_entered; other source, empty "
    "fmId, 'fm-oe-ensure' without fmId, unreadable custom_fields -> unknown. lineage and "
    "possible_writers are inferred, not audited provenance.")
STALE_ORDER_NOTE = (
    "Read from a stored capture that is stale or whose refresh failed: this order's status "
    "(a PO present or absent) may not be the Portal's current state.")
ABSENCE_NOTE = (
    "absent_in_this_source means only that every exposed PO field of the cited row was "
    "read and is empty or absent. It does not mean the order has no customer PO: the PO "
    "may be held in the places listed in possible_sources, which this tool did not read. "
    "A PO field that could not be read (not mirrored, or custom_fields unparseable or not "
    "an object) or that holds a non-text value is not_inspectable, and such an order "
    "without a PO-like value is po_fields_not_inspectable, never absent. A PO field equal to the order's own "
    "number (order_number, fmOeNumber or renumberedTo) is present_equals_order_number: "
    "possibly the Portal bridge fallback custpocode = po_reference ?? order_number "
    "(equality is not proof), conservatively neither a customer PO nor a not-PO-like miss, "
    "so an order with only such values is absent_in_this_source.")
DATE_FILTER_RULE = (
    "Calendar dates or instants, never strings: a YYYY-MM-DD bound compares with the "
    "value's own calendar date (date_to includes that whole day); a timestamp bound "
    "compares instants, naive timestamps read as UTC, and a date-only value against a "
    "timestamp bound counts as its whole UTC day. Rows without a value in "
    "date_column are excluded; values that are not ISO dates are counted in "
    "completeness.date_values_unparseable and make the result incomplete.")

# Where a customer PO may be held outside the fields read here, with honest
# availability: not_ingested (this tool does not read the source into the case;
# read access may exist elsewhere, e.g. the GET-only Graph/BC client in the
# credentialed environment), unknown (not exposed to us), not_checked (exposed
# by the reporting API but not read by this tool), read (read for this order;
# see the order's fm_order_entry block), read_incomplete (the linked FM scan was
# incomplete and this order's row was not among the rows read: neither present
# nor absent).
POSSIBLE_SOURCES: dict[str, dict[str, str]] = {
    "microsoft_graph_mail": {
        "availability": "not_ingested",
        "what": "Customer mail and attachments (the PO document itself) via Microsoft Graph",
        "why": "Not read by this tool; mail is reachable only through the GET-only Graph "
               "client, which this tool does not call."},
    "sharepoint": {
        "availability": "not_ingested",
        "what": "SharePoint document libraries",
        "why": "Not read by this tool; no per-item permission mapping is wired yet."},
    "business_central": {
        "availability": "not_ingested",
        "what": "Business Central sales documents (Sales Header 'External Document No.')",
        "why": "Not read by this tool; BC is reachable only through the GET-only BC client, "
               "which this tool does not call. BC 'External Document No.' is not by itself PO "
               "evidence: the Portal sends po_reference there on the sales order "
               "(serializers.ts serializeSalesOrder), for shipments posted before "
               "PlxSalesBridge@1c5dddb:185-186 the ship-only bridge then overwrote the "
               "Sales Header value with the Portal shipment number before posting "
               "(PlxSalesBridge.Codeunit.al PostSalesShipOnly; from then the shipment number "
               "goes to \"PLX Shipment No.\" and the bridged invoice copies the shipment's "
               "External Document No., :289-294), and a standalone BC sales "
               "invoice the Portal posted before TASK-2271 carries the order number there (serializeSalesInvoice). A value is a "
               "PO lead only when it differs from the order number and every shipment number "
               "of the order."},
    "portal_attachments_and_other_tables": {
        "availability": "unknown",
        "what": "Portal order attachments and Portal tables outside the reporting API",
        "why": "Not exposed by the Portal reporting API; whether they hold a PO is unknown."},
    "sage.oe_order_header.ponumber": {
        "availability": "not_checked",
        "what": "Sage order header customer PO (for Sage-imported orders)",
        "why": "Exposed by the reporting API but not read by this tool; check it with "
               "plx.find / plx.object."},
    "public.order_entry.custpocode": {
        "availability": "not_checked",
        "what": "FM order entry customer PO (order_entry.custpocode, row id = fmId)",
        "why": "Read for each order with an fmId when the actor may read public.order_entry "
               "(availability read, block fm_order_entry; read_incomplete when that scan "
               "was incomplete and missed this order's row); otherwise not checked. On "
               "Portal-created rows (id >= 900000) custpocode = po_reference ?? order_number: "
               "a value equal to erpcode or the order number is no PO."},
}
_COMMON_SOURCES = ("microsoft_graph_mail", "sharepoint", "business_central",
                   "portal_attachments_and_other_tables")
_ORIGIN_SOURCES = {"sage_import": ("sage.oe_order_header.ponumber",),
                   "fm_migrated": ("public.order_entry.custpocode",),
                   "unknown": ("sage.oe_order_header.ponumber",
                               "public.order_entry.custpocode")}

REPLICA_LABEL = "staging primary replica (FM/Sage/gold ETL)"  # 03-PORTAL.md section 9 row 8
EVIDENCE_MODE = (
    "source mirror of the Portal reporting API (advisory): " + REPLICA_LABEL +
    ", not the UAT DB the staging Portal UI writes")



class CustomerPoError(ValueError):
    """A refused customer-PO coverage request, with a stable ``code``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


# ---------------------------------------------------------------- rules

def po_like(value: Any) -> bool:
    """True only for a value that plausibly is a PO identifier (see PO_SHAPE_RULE)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return False
    text = value.strip()
    return (_PO_MIN <= len(text) <= _PO_MAX and _PO_CHARS.fullmatch(text) is not None
            and any(char.isdigit() for char in text) and text.strip("0") != "")


def not_po_like_reasons(value: Any) -> list[str]:
    """Why a non-empty value is not PO-like (empty list when it is PO-like)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ["not_text"]
    text = str(value).strip()
    reasons = []
    if len(text) < _PO_MIN:
        reasons.append("too_short")
    if len(text) > _PO_MAX:
        reasons.append("too_long")
    if any(char.isspace() for char in text):
        reasons.append("whitespace")
    if not any(char.isdigit() for char in text):
        reasons.append("no_digit")
    elif text.strip("0") == "":
        reasons.append("only_zeros")
    if any(char not in _PO_ALLOWED and not char.isspace() for char in text):
        reasons.append("disallowed_chars")
    if text and not (text[0].isascii() and text[0].isalnum() and
                     text[-1].isascii() and text[-1].isalnum()):
        reasons.append("edge_not_alphanumeric")
    return reasons


def probable_po_after_normalization(value: Any) -> bool:
    """PO-like once a leading PO/P.O./PO#/# marker and inner whitespace are removed.

    Inner whitespace is only removed after a marker, between groups that each
    contain a digit, and joining stops being safe at a second marker: a value
    that names more than one PO ("PO4500 PO4501") is never flagged. Bare "PO"
    is a marker only when no letter follows it ("Polo2 123", "POA1 2" are not).
    One wrapping bracket/quote pair and trailing ".,;:" are removed only when
    a PO word marker follows ("(PO 4500)", "PO 4500."; never "4500.",
    "(4500.00)", "(#204)"); "Cust"/"Customer"/"Client" may qualify a PO
    marker but never a bare "#". A value over 80 characters is never flagged
    (known limitation: a PO buried in a long note is not a lead; the field
    still counts as not PO-like). Known leftover: a marker before a non-PO
    number ("PO 2026") is flagged, as before.
    """
    if not isinstance(value, str) or po_like(value):
        return False
    if len(value) > _NORMALIZE_MAX:  # raw length, as before (padding counts)
        return False
    text = value.strip()
    unwrapped = _unwrap(text)
    if _PO_WORD_MARKER.match(unwrapped):
        text = unwrapped
    marker = _PO_MARKER.match(text)
    tokens = text[marker.end():].split() if marker else text.split()
    # Groups are joined only after a PO marker ("PO 4500 12"), so phone numbers
    # and dates ("905 555 1234") are not leads, and only digit-bearing groups,
    # so names ("PO Jane 2") never are.
    if not tokens or (len(tokens) > 1 and not marker) or any(
            not any(char.isdigit() for char in token) for token in tokens):
        return False
    if any(_PO_MARKER.match(token) for token in tokens[1:]):
        return False
    return po_like("".join(tokens))


SHAPE_DIMENSIONS = ("reasons", "marker", "tokens", "digit_tokens", "alpha_tokens",
                    "digit_groups", "max_digit_run", "numeric_pattern", "length")
_DIGIT_RUN = re.compile(r"[0-9]+")
_NUMERIC_ONLY = re.compile(r"^[0-9]+(?:[\s\-./]+[0-9]+)*$")
SHAPE_NOTE = (
    "Counts only: coarse shape classes of present_not_po_like values, never the values. "
    "marker: leading PO marker after unwrapping (leading), a PO marker on a later token "
    "(inner) or none; tokens/digit_tokens/alpha_tokens: whitespace-separated tokens, those "
    "with a digit, those with letters and no digit; digit_groups / max_digit_run: maximal "
    "runs of ASCII digits; numeric_pattern: lengths of the digit groups when the value is "
    "only digit groups and - . / or whitespace separators (3+3+4 is phone-shaped, "
    "4+2+2 / 2+2+4 date-shaped), else mixed; length: trimmed characters. The tool output "
    "keeps the most frequent buckets per dimension and folds the rest into 'other'; the "
    "CLI report is uncapped.")


# Per dimension at most this many buckets in the MCP aggregates (the most
# frequent, then "other"), so the envelope stays bounded whatever the data;
# the CLI report is uncapped.
SHAPE_BUCKET_CAP = 24


def _top_buckets(buckets: Mapping[str, int], cap: int | None) -> dict[str, int]:
    ranked = sorted(buckets.items(), key=lambda item: (-item[1], item[0]))
    if cap is None or len(ranked) <= cap:
        return dict(sorted(buckets.items()))
    kept = dict(sorted(ranked[:cap - 1]))
    kept["other"] = sum(count for _, count in ranked[cap - 1:])
    return kept


def _bucket(number: int, edges: Sequence[tuple[int, str]], top: str) -> str:
    for upper, label in edges:
        if number <= upper:
            return label
    return top


def not_po_like_shape(value: Any) -> dict[str, str]:
    """Coarse, value-free shape classes of a not-PO-like value (see SHAPE_NOTE).

    Every class is a bucket label, never a substring of the value, so the
    result can be counted and reported without echoing the value.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return {"reasons": "not_text", **{name: "n/a" for name in SHAPE_DIMENSIONS[1:]}}
    text = str(value).strip()
    tokens = text.split()
    unwrapped = _unwrap(text)
    if _PO_MARKER.match(text) or _PO_WORD_MARKER.match(unwrapped):
        marker = "leading"
    elif any(_PO_MARKER.match(token) for token in tokens[1:]):
        marker = "inner"
    else:
        marker = "none"
    runs = [len(run) for run in _DIGIT_RUN.findall(text)]
    if runs and _NUMERIC_ONLY.fullmatch(text):
        pattern = ("+".join(str(run) if run < 10 else "10+" for run in runs)
                   if len(runs) <= 5 else "6+_groups")
    else:
        pattern = "mixed" if runs else "no_digits"
    return {
        "reasons": "+".join(not_po_like_reasons(value)) or "none",
        "marker": marker,
        "tokens": _bucket(len(tokens), ((0, "0"), (1, "1"), (2, "2"), (3, "3")), "4+"),
        "digit_tokens": _bucket(sum(any(c.isdigit() for c in t) for t in tokens),
                                ((0, "0"), (1, "1"), (2, "2")), "3+"),
        "alpha_tokens": _bucket(sum(any(c.isalpha() for c in t) and
                                    not any(c.isdigit() for c in t) for t in tokens),
                                ((0, "0"), (1, "1")), "2+"),
        "digit_groups": _bucket(len(runs), ((0, "0"), (1, "1"), (2, "2"), (3, "3")), "4+"),
        "max_digit_run": _bucket(max(runs, default=0),
                                 ((0, "0"), (2, "1-2"), (4, "3-4"), (6, "5-6"), (9, "7-9")),
                                 "10+"),
        "numeric_pattern": pattern,
        "length": _bucket(len(text), ((2, "1-2"), (10, "3-10"), (20, "11-20"), (40, "21-40"),
                                      (80, "41-80")), "81+")}


def _unwrap(value: str) -> str:
    """Trimmed value without one wrapping bracket/quote pair and trailing ``.,;:``."""
    text = value.strip().rstrip(_TRAILING_PUNCT).rstrip()
    if len(text) >= 2 and _WRAPPERS.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text.rstrip(_TRAILING_PUNCT).rstrip()


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _custom_fields(row: Mapping[str, Any], columns: Sequence[str]) -> tuple[Any, str | None]:
    """(object or None, problem). Problem is None when the value is usable."""
    if "custom_fields" not in columns:
        return None, "custom_fields_not_mirrored"
    value = row.get("custom_fields")
    if value is None:
        return {}, None
    if isinstance(value, str):
        if not value.strip():
            return {}, None
        try:
            value = json.loads(value)
        except ValueError:
            return None, "custom_fields_unparseable"
    if not isinstance(value, dict):
        return None, "custom_fields_not_object"
    return value, None


def classify_origin(row: Mapping[str, Any], columns: Sequence[str]) -> dict[str, str]:
    """Origin (one of ORIGINS), the Portal lineage behind it, and the rule that fired."""
    fields, problem = _custom_fields(row, columns)
    if problem is not None:
        return {"value": "unknown", "lineage": "unknown", "rule": problem}
    raw_source = fields.get("source")
    # Trimmed, as Portal's JS predicate compares it.
    source = raw_source.strip() if isinstance(raw_source, str) else raw_source
    # Mirrors the JS predicate lib/wms/sage-ship-queue.ts:isSageBackedSalesOrder
    # (not Portal's SQL helpers, which the context pack notes differ): a
    # non-empty trimmed sageOrdUniq or sageSourceOrdUniq first, then the trimmed
    # source marker. So a valid Sage key wins even next to another source value.
    sage_markers = [f"custom_fields.{key}" for key in SAGE_KEYS
                    if portal_trim_text(fields.get(key)) is not None]
    if portal_trim_text(raw_source) == SAGE_SOURCE_MARKER:
        sage_markers.append(f"custom_fields.source == '{SAGE_SOURCE_MARKER}'")
    has_fm_key = FM_MARKER_KEY in fields
    # isFmOnlySalesOrder:41 tests trimText(fmId), as the Sage keys above.
    fm = has_fm_key and portal_trim_text(fields.get(FM_MARKER_KEY)) is not None
    if sage_markers:
        # Portal's own test: Sage-backed wins over fmId and over any other source.
        rule = ("Sage-backed (" + " / ".join(sage_markers) +
                "; JS isSageBackedSalesOrder in Portal sage-ship-queue.ts)")
        if has_fm_key:
            rule += "; also carries fmId, Sage-backed wins as in Portal isFmOnlySalesOrder"
        if not _is_empty(source) and source != SAGE_SOURCE_MARKER:
            rule += "; another source value is overridden by the Sage key"
        return {"value": "sage_import", "lineage": "sage_etl", "rule": rule}
    if not _is_empty(source) and source not in (FM_ENSURE_SOURCE, GOLD_BRIDGE_SOURCE):
        return {"value": "unknown", "lineage": "unknown",
                "rule": "custom_fields.source has an unrecognised value"}
    if has_fm_key and not fm:
        return {"value": "unknown", "lineage": "unknown",
                "rule": "custom_fields.fmId present but empty"}
    if source == GOLD_BRIDGE_SOURCE:
        return {"value": "portal_entered", "lineage": "gold_so_bridge",
                "rule": "custom_fields.source == 'gold-so-bridge' (entered in Portal, bridged "
                        "to an FM order_entry" + (" with fmId" if fm else "") + ")"}
    if source == FM_ENSURE_SOURCE:
        if not fm:
            return {"value": "unknown", "lineage": "unknown",
                    "rule": "custom_fields.source == 'fm-oe-ensure' without fmId"}
        return {"value": "fm_migrated", "lineage": "fm_oe_ensure",
                "rule": "custom_fields.source == 'fm-oe-ensure' and fmId present (gold SO "
                        "created from FM order_entry; PO copied from FM custpocode)"}
    if fm:
        legacy = "custPoCode" in fields
        return {"value": "fm_migrated",
                "lineage": "legacy_fm_sql" if legacy else "fm_linked_without_source",
                "rule": "custom_fields.fmId present, no source" + (
                    " (custPoCode key: legacy migrate-fm-to-portal.sql)" if legacy else "")}
    return {"value": "portal_entered", "lineage": "portal",
            "rule": "no import marker in custom_fields"}


def _field_value(row: Mapping[str, Any], columns: Sequence[str], column: str,
                 key: str | None) -> tuple[bool | str, Any]:
    """(field exists, value) for a column or one key of a JSON-object column
    (``capability_guards.read_field``): ``exists`` is True, False (the field was read and
    is not there) or a string naming why the field could not be read at all
    (``column_not_mirrored``, ``json_unparseable``, ``json_not_object``)."""
    return read_field(row, column if key is None else f"{column}.{key}", columns)


# Who may have written each PO field, inferred from the order's lineage (Portal
# facts via the context pack 03-PORTAL.md section 3.3). This is an inference, not
# audited last-writer provenance: the replica keeps no per-field write history.
# A po_reference is not free text in Portal: resolveOrderByNumber
# (staging@40f7802 lib/orders/order-identity.ts:98-152) matches it as an order identity
# together with the order's customer only (customer_id plus the normalised PO; a PO is
# unique per customer, ontology/schema/identifiers.json customer_po identity_scope).
_POSSIBLE_WRITERS = {
    "po_reference": {
        "sage_etl": "sage_ponumber_or_retained_earlier_value",
        "fm_oe_ensure": "fm_order_entry_custpocode",
        "legacy_fm_sql": "legacy_fm_reference_text_migration",
        "fm_linked_without_source": "portal_entry_or_fm_custpocode_on_link",
        "gold_so_bridge": "portal_order_entry",
        "portal": "portal_order_entry",
        "unknown": "unknown"},
    "custom_fields.custPoCode": "legacy_fm_sql_custpocode",
}


def possible_writers(path: str, lineage: str) -> str:
    """Portal path(s) that may have written this PO field, inferred from the lineage.

    Never audited provenance: the reporting replica has no per-field history.
    """
    entry = _POSSIBLE_WRITERS.get(path, "unknown")
    return entry if isinstance(entry, str) else entry.get(lineage, "unknown")


def _identifier_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def order_identifiers(row: Mapping[str, Any], columns: Sequence[str]) -> dict[str, str]:
    """The order's own numbers (ORDER_IDENTIFIERS) that a PO field may echo."""
    return own_identifiers(row, [path for path, _, _ in ORDER_IDENTIFIERS], columns)


def classify_field(exists: bool | str, value: Any,
                   identifiers: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Status of one PO field from ``capability_guards.value_state`` (the one field
    classification the gap guard uses too): unreadable -> ``not_inspectable``, absent ->
    ``field_absent`` / ``empty``, self_reference -> ``present_equals_order_number``,
    value -> ``present_po_like`` / ``present_not_po_like``."""
    state = value_state(exists, value, identifiers)
    if state["state"] == "unreadable":
        # Never read, or not text: nothing may be concluded, least of all absence.
        return {"status": "not_inspectable", "reason": state["reason"]}
    if state["state"] == "absent":
        return {"status": state["reason"]}
    if state["state"] == "self_reference":
        # Equal to the order's own number: possibly the Portal bridge fallback
        # (custpocode = po_reference ?? order_number), though equality does not
        # prove the fallback ran. A placeholder: no customer PO, absent here.
        return {"status": "present_equals_order_number", "matches": state["matches"]}
    if po_like(value):
        return {"status": "present_po_like", "value": str(value).strip()}
    # Never echo: the value can be a person's name or free text.
    return {"status": "present_not_po_like", "value_withheld": True,
            "reasons": not_po_like_reasons(value),
            "probable_po_after_normalization": probable_po_after_normalization(value)}


def classify_order(row: Mapping[str, Any], columns: Sequence[str]) -> dict[str, Any]:
    """Origin, per-field status and overall status of one sales-order row."""
    origin = classify_origin(row, columns)
    identifiers = order_identifiers(row, columns)
    fields = {path: classify_field(*_field_value(row, columns, column, key),
                                   identifiers=identifiers)
              for path, column, key in CANDIDATE_FIELDS}
    for path, item in fields.items():
        if item["status"].startswith("present_"):
            item["possible_writers"] = possible_writers(path, origin["lineage"])
    statuses = [item["status"] for item in fields.values()]
    values = sorted({item["value"] for item in fields.values() if "value" in item})
    if values:
        status = "has_customer_po"
    elif "not_inspectable" in statuses:
        status = "po_fields_not_inspectable"
    elif "present_not_po_like" in statuses:
        status = "po_field_not_po_like"
    else:
        status = "absent_in_this_source"
    result: dict[str, Any] = {"origin": origin, "fields": fields, "status": status}
    fallback = [path for path, item in fields.items()
                if item["status"] == "present_equals_order_number"]
    if fallback:
        result["equals_order_number_fields"] = fallback
    fm_id = _fm_id(row, columns)
    if fm_id is not None and fm_id >= PORTAL_OE_ID_BASE:
        # fm-oe-bridge-ids.ts: FM order_entry ids >= 900000 were created by Portal.
        result["fm_order_entry_created_by_portal"] = True
    if status != "has_customer_po":
        # A review lead only: never counted as a customer PO.
        result["probable_po_after_normalization"] = any(
            item.get("probable_po_after_normalization") for item in fields.values())
    if values:
        result["customer_po_values"] = values
        if len(values) > 1:
            result["conflicting_po_values"] = True
    return result


_FM_OE_ID = re.compile(r"^-?[0-9]+$")


def parse_fm_oe_id(value: Any) -> int | None:
    """Portal ``parseFmOeId`` (lib/sales-order/fm-oe-bridge-ids.ts): a finite
    number truncated, or trimmed text of ASCII digits; anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str) and _FM_OE_ID.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _fm_id(row: Mapping[str, Any], columns: Sequence[str]) -> int | None:
    exists, value = _field_value(row, columns, "custom_fields", FM_MARKER_KEY)
    return parse_fm_oe_id(value) if exists is True else None


def possible_sources(origin: str, lineage: str | None = None) -> list[dict[str, str]]:
    names = _COMMON_SOURCES + _ORIGIN_SOURCES.get(origin, ())
    if lineage == "gold_so_bridge":  # bridged to an FM order_entry row
        names += ("public.order_entry.custpocode",)
    return [{"source": name, "availability": POSSIBLE_SOURCES[name]["availability"]}
            for name in names]


FM_ORDER_ENTRY_RULE = (
    "An fmId order's FM order_entry.custpocode is read (own citation); on Portal-created "
    "rows (id >= 900000) custpocode = po_reference ?? order_number, so a value equal to "
    "erpcode or the order number is no PO. status still describes the Portal row only.")


def fm_order_entry_field(order_row: Mapping[str, Any], columns: Sequence[str],
                         fm_row: Mapping[str, Any], fm_columns: Sequence[str]
                         ) -> dict[str, Any]:
    """FM ``order_entry.custpocode`` of the order's fmId row, classified (FM_ORDER_ENTRY_RULE)."""
    identifiers = dict(order_identifiers(order_row, columns))
    erpcode = _identifier_text(fm_row.get("erpcode")) if "erpcode" in fm_columns else None
    if erpcode is not None:
        identifiers["public.order_entry.erpcode"] = erpcode
    exists: bool | str = True if "custpocode" in fm_columns else "column_not_mirrored"
    field = classify_field(exists, fm_row.get("custpocode"), identifiers)
    if field["status"].startswith("present_"):
        # FM users type custpocode; on Portal-created rows the bridge wrote it.
        fm_id = parse_fm_oe_id(fm_row.get("id"))
        field["possible_writers"] = (
            "portal_gold_so_bridge_po_reference_or_order_number"
            if fm_id is not None and fm_id >= PORTAL_OE_ID_BASE else "filemaker_user")
    return field


# ---------------------------------------------------------------- coverage

def _choice_list(values: Any, allowed: Sequence[str], name: str) -> frozenset[str] | None:
    if values is None:
        return None
    if (isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)) or
            not 1 <= len(values) <= len(allowed) or
            any(value not in allowed for value in values)):
        raise CustomerPoError(f"invalid_{name}", "one or more of " + ", ".join(allowed))
    return frozenset(values)


def parse_date_value(value: Any) -> date | datetime | None:
    """A ``date`` for YYYY-MM-DD, an aware ``datetime`` for an ISO timestamp, else None.

    Naive timestamps are read as UTC; fractional seconds beyond microseconds are
    truncated. Impossible calendar values (month 13, 25:00) are None.
    """
    if not isinstance(value, str):
        return None
    match = _DATE_VALUE.fullmatch(value.strip())
    if match is None:
        return None
    day, clock, seconds, fraction, offset = match.groups()
    try:
        calendar = date.fromisoformat(day)
        if clock is None:
            return calendar
        hours, minutes = (int(part) for part in clock.split(":"))
        micros = int((fraction or "0")[:6].ljust(6, "0"))
        zone = timezone.utc
        if offset and offset != "Z":
            digits = offset[1:].replace(":", "")
            # Postgres renders whole-hour offsets as "+HH" / "-HH".
            offset_minutes = int(digits[2:] or "0")
            delta_minutes = int(digits[:2]) * 60 + offset_minutes
            if offset_minutes >= 60 or delta_minutes >= 24 * 60:
                return None
            zone = timezone(timedelta(
                minutes=delta_minutes if offset[0] == "+" else -delta_minutes))
        return datetime.combine(calendar, time(hours, minutes, int(seconds or 0), micros),
                                tzinfo=zone)
    except ValueError:
        return None


def _as_instant(value: date | datetime, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.max if end_of_day else time.min, tzinfo=timezone.utc)


def _date_filter(date_column: Any, date_from: Any, date_to: Any) -> dict[str, Any] | None:
    if date_column is None:
        if date_from is not None or date_to is not None:
            raise CustomerPoError("date_column_required",
                                  "date_from/date_to need an explicit date_column")
        return None
    if not isinstance(date_column, str) or _COLUMN.fullmatch(date_column) is None:
        raise CustomerPoError("invalid_date_column")
    if date_from is None and date_to is None:
        raise CustomerPoError("date_bound_required", "give date_from and/or date_to")
    bounds = {}
    for name, value in (("from", date_from), ("to", date_to)):
        if value is None:
            continue
        parsed = parse_date_value(value)
        if parsed is None:
            raise CustomerPoError("invalid_date_bound",
                                  f"date_{name}: use a real YYYY-MM-DD date or ISO timestamp")
        bounds[name] = parsed
    if ("from" in bounds and "to" in bounds and
            _as_instant(bounds["from"]) > _as_instant(bounds["to"], end_of_day=True)):
        raise CustomerPoError("reversed_date_bounds", "date_from is after date_to")
    return {"column": date_column, **bounds}


def _date_in_range(cell: date | datetime, bounds: Mapping[str, Any]) -> bool:
    """Calendar-date comparison for a date bound, instant comparison for a timestamp bound."""
    lower, upper = bounds.get("from"), bounds.get("to")
    if lower is not None:
        if isinstance(lower, datetime):
            if _as_instant(cell, end_of_day=True) < lower:
                return False
        elif (cell.date() if isinstance(cell, datetime) else cell) < lower:
            return False
    if upper is not None:
        if isinstance(upper, datetime):
            if _as_instant(cell) > upper:
                return False
        elif (cell.date() if isinstance(cell, datetime) else cell) > upper:
            return False
    return True


def _staleness(value: Any) -> float | None:
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not 0 <= value <= _STALENESS_MAX):
        raise CustomerPoError("invalid_max_staleness")
    return value


def _scan(mirror: Any, actor: Actor, where: list[list[Any]], max_staleness: float | None,
          scan_limit: int, relation: str = RELATION) -> dict[str, Any]:
    """Read every matching row in bounded mirror pages; report what was not read."""
    rows: list[dict[str, Any]] = []
    offset = 0
    first = None
    reasons: list[str] = []
    columns: list[str] = []
    freshness: dict[str, Any] = {}
    total = 0
    while True:
        page_limit = min(MAX_ROWS_LIMIT, scan_limit - len(rows))
        # Only the first page may refresh; later pages read the same capture.
        page = mirror.rows(actor, relation, columns=None, where=where, limit=page_limit,
                           offset=offset,
                           max_staleness=max_staleness if first is None else _STALENESS_MAX)
        signature = (page["freshness"]["latest_capture_id"],
                     page["freshness"]["last_refresh_at"], page["total_matched"])
        if first is None:
            first = signature
            columns = list(page["columns"])
            freshness = page["freshness"]
            total = page["total_matched"]
        elif signature != first:
            reasons.append("mirror_changed_during_scan")
            break
        rows.extend(page["rows"])
        offset += page["returned"]
        if page["next_offset"] is None or not page["returned"]:
            break
        if len(rows) >= scan_limit:
            reasons.append("scan_bound_reached")
            break
    return {"rows": rows, "columns": columns, "freshness": freshness, "total": total,
            "reasons": reasons}


def coverage(mirror: Any, actor: Actor, *, origins: Sequence[str] | None = None,
             statuses: Sequence[str] | None = None, date_column: str | None = None,
             date_from: str | None = None, date_to: str | None = None,
             max_staleness: float | None = None,
             scan_limit: int = MAX_SCAN_ROWS,
             shape_signatures: bool = False,
             read_fm_order_entry: bool = True) -> dict[str, Any]:
    """Every visible sales order classified, plus origin x status aggregates.

    Returns all selected orders (the MCP layer pages them by bytes). The mirror
    enforces the actor's audience first: a denial raises ``MirrorError``.
    Aggregates always carry per-dimension shape histograms of not-PO-like
    fields (counts only); ``shape_signatures=True`` (the CLI) also counts the
    combined signature of all dimensions, which can be larger than one MCP
    envelope and is therefore not part of the tool output.
    """
    if not isinstance(actor, Actor):
        raise CustomerPoError("invalid_actor")
    origin_filter = _choice_list(origins, ORIGINS, "origin")
    status_filter = _choice_list(statuses, ORDER_STATUSES, "status")
    date_filter = _date_filter(date_column, date_from, date_to)
    # The mirror compares strings, so the date bounds are applied here, per row.
    where = [[date_column, "is_null", False]] if date_filter else []
    staleness = _staleness(max_staleness)
    if isinstance(scan_limit, bool) or not isinstance(scan_limit, int) or not (
            1 <= scan_limit <= MAX_SCAN_ROWS):
        raise CustomerPoError("invalid_scan_limit")
    scan = _scan(mirror, actor, where, staleness, scan_limit)
    columns = scan["columns"]
    freshness = scan["freshness"]
    fm_index, fm_read = _fm_order_entries(mirror, actor, scan, staleness, read_fm_order_entry)
    key_columns = list(freshness.get("key") or [])
    counts = {origin: {status: 0 for status in ORDER_STATUSES} for origin in ORIGINS}
    field_counts = {origin: {path: {status: 0 for status in FIELD_STATUSES}
                             for path, _, _ in CANDIDATE_FIELDS} for origin in ORIGINS}
    reason_counts = {origin: {path: {reason: 0 for reason in NOT_PO_LIKE_REASONS}
                              for path, _, _ in CANDIDATE_FIELDS} for origin in ORIGINS}
    probable_fields = {origin: {path: 0 for path, _, _ in CANDIDATE_FIELDS}
                       for origin in ORIGINS}
    probable_orders = {origin: 0 for origin in ORIGINS}
    lineage_counts = {lineage: {status: 0 for status in ORDER_STATUSES} for lineage in LINEAGES}
    portal_created_fm = {origin: 0 for origin in ORIGINS}
    fm_statuses: dict[str, dict[str, int]] = {}
    fm_po_where_portal_has_none = {origin: 0 for origin in ORIGINS}
    # Sparse: origin -> field -> dimension -> bucket -> count (present_not_po_like only).
    shapes: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    signatures: dict[str, dict[str, dict[str, int]]] = {}
    orders = []
    unparseable = 0
    in_range = 0
    # A row of a stale or refresh-failed capture may not be the Portal's current row: each
    # order entry (present or absent alike) carries the label, not only the report header.
    stale_capture = bool(freshness.get("stale") or freshness.get("refresh_error"))
    for item in scan["rows"]:
        row = item["row"]
        if date_filter is not None:
            cell = parse_date_value(row.get(date_column))
            if cell is None:
                unparseable += 1
                continue
            if not _date_in_range(cell, date_filter):
                continue
        in_range += 1
        verdict = classify_order(row, columns)
        origin, status = verdict["origin"]["value"], verdict["status"]
        counts[origin][status] += 1
        lineage_counts[verdict["origin"]["lineage"]][status] += 1
        if verdict.get("fm_order_entry_created_by_portal"):
            portal_created_fm[origin] += 1
        for path, field in verdict["fields"].items():
            field_counts[origin][path][field["status"]] += 1
            for reason in field.get("reasons", ()):
                reason_counts[origin][path][reason] += 1
            if field.get("probable_po_after_normalization"):
                probable_fields[origin][path] += 1
            if field["status"] == "present_not_po_like":
                _, column, key = next(spec for spec in CANDIDATE_FIELDS if spec[0] == path)
                shape = not_po_like_shape(_field_value(row, columns, column, key)[1])
                shape["probable"] = str(bool(field.get("probable_po_after_normalization"))).lower()
                target = shapes.setdefault(origin, {}).setdefault(path, {})
                for dimension, bucket in shape.items():
                    counter = target.setdefault(dimension, {})
                    counter[bucket] = counter.get(bucket, 0) + 1
                if shape_signatures:
                    signature = "|".join(f"{name}={shape[name]}"
                                         for name in SHAPE_DIMENSIONS + ("probable",))
                    counter = signatures.setdefault(origin, {}).setdefault(path, {})
                    counter[signature] = counter.get(signature, 0) + 1
        if verdict.get("probable_po_after_normalization"):
            probable_orders[origin] += 1
        fm_block = _fm_order_entry_block(row, columns, fm_index, fm_read)
        if fm_block is not None:
            fm_status = fm_block["status"]
            counter = fm_statuses.setdefault(origin, {})
            counter[fm_status] = counter.get(fm_status, 0) + 1
            if fm_status == "present_po_like" and status != "has_customer_po":
                fm_po_where_portal_has_none[origin] += 1
        if origin_filter is not None and origin not in origin_filter:
            continue
        if status_filter is not None and status not in status_filter:
            continue
        order: dict[str, Any] = {"citation": item["citation"], "key": item["key"]}
        if "order_number" in columns:
            order["order_number"] = row.get("order_number")
        order.update(verdict)
        order["capture"] = "stale" if stale_capture else "current"
        if stale_capture:
            order["capture_note"] = STALE_ORDER_NOTE
        if fm_block is not None:
            order["fm_order_entry"] = fm_block
            if fm_block["status"] == "present_po_like" and status != "has_customer_po":
                # A lead from the linked FM row (its own citation), not this row's status.
                order["fm_order_entry_holds_customer_po"] = True
        if status != "has_customer_po":
            order["possible_sources"] = possible_sources(origin, verdict["origin"]["lineage"])
            if fm_block is not None:
                # 'read' only when this order's FM row was actually read (or the scan
                # was complete, so its absence is shown); an incomplete scan that missed
                # it says so instead of claiming a read.
                availability = "read_incomplete" if fm_block.get("problem") else "read"
                for item in order["possible_sources"]:
                    if item["source"] == "public.order_entry.custpocode":
                        item["availability"] = availability
                if not any(item["source"] == "public.order_entry.custpocode"
                           for item in order["possible_sources"]):
                    order["possible_sources"].append(
                        {"source": "public.order_entry.custpocode",
                         "availability": availability})
        orders.append(order)
    scanned = len(scan["rows"])
    reasons = list(scan["reasons"])
    if scanned < scan["total"] and not reasons:
        reasons.append("rows_not_scanned")
    if freshness.get("stale"):
        reasons.append("mirror_capture_stale")
    if freshness.get("refresh_error"):
        reasons.append("mirror_refresh_failed_serving_stored_capture")
    if unparseable:
        reasons.append("date_values_unparseable")
    scan_complete = scanned == scan["total"] and "mirror_changed_during_scan" not in reasons
    complete = scan_complete and not freshness.get("stale") and not freshness.get(
        "refresh_error") and not unparseable
    used = [name for name in key_columns + ["order_number", "po_reference", "custom_fields"]
            + ([date_column] if date_column else []) if name in columns]
    used = list(dict.fromkeys(used))
    missing = [name for name in ("order_number", "po_reference", "custom_fields")
               if name not in columns]
    completeness = {
        "complete": complete, "scan_complete": scan_complete, "reasons": reasons,
        "rows_matched_in_mirror": scan["total"], "rows_scanned": scanned,
        "rows_in_date_range": in_range, "date_values_unparseable": unparseable,
        "scan_bound": scan_limit,
        "mirror_capture": {name: freshness.get(name) for name in (
            "refreshed_now", "refresh_error", "stale", "last_refresh_at",
            "last_refresh_mode", "last_full_traversal_at", "latest_capture_id",
            "rows_absent_from_latest_full_traversal", "source_watermark")},
        "atomic_upstream_snapshot": False, "deletions_proven": False,
        "linked_reads": {FM_RELATION: fm_read},
        "caveats": list(freshness.get("caveats") or []),
        "meaning": "complete is true only when every mirrored row matching the date "
                   "filter was classified from one capture that is within max_staleness "
                   "and whose last refresh succeeded, and every date value could be "
                   "read. Even then it is the mirror's "
                   "non-atomic reporting-replica capture, not current Portal state."}
    aggregates = {
        "scope": "all scanned rows matching the date filter (origin/status filters "
                 "do not apply to aggregates)",
        "rows": in_range, "complete": complete,
        "by_origin_status": counts, "by_origin_field_status": field_counts,
        "by_origin_field_not_po_like_reason": reason_counts,
        "by_origin_field_probable_po_after_normalization": probable_fields,
        "orders_with_probable_po_after_normalization": probable_orders,
        # Sparse: only lineages / origins that occur.
        "by_lineage_status": {lineage: {status: n for status, n in counts_.items() if n}
                              for lineage, counts_ in lineage_counts.items()
                              if any(counts_.values())},
        "orders_with_fm_order_entry_created_by_portal": {
            origin: n for origin, n in portal_created_fm.items() if n},
        # Sparse: origin -> FM order_entry.custpocode status (orders with an fmId only).
        "by_origin_fm_order_entry_custpocode_status": {
            origin: dict(sorted(counter.items())) for origin, counter in fm_statuses.items()},
        "orders_without_portal_po_whose_fm_order_entry_holds_one": {
            origin: n for origin, n in fm_po_where_portal_has_none.items() if n},
        "by_origin_field_not_po_like_shape": {
            origin: {path: {dimension: _top_buckets(buckets, None if shape_signatures
                                                    else SHAPE_BUCKET_CAP)
                            for dimension, buckets in dims.items()}
                     for path, dims in fields.items()}
            for origin, fields in shapes.items()},
        "reasons_note": "a present_not_po_like field can have several reasons; a probable "
                        "PO after normalization is a review lead, not a customer PO",
        "by_status": {status: sum(counts[origin][status] for origin in ORIGINS)
                      for status in ORDER_STATUSES}}
    if shape_signatures:
        aggregates["by_origin_field_not_po_like_shape_signature"] = {
            origin: {path: dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))
                     for path, counter in fields.items()}
            for origin, fields in signatures.items()}
    return {"schema": "plx-customer-po-coverage/v1", "relation": RELATION,
            "evidence_mode": EVIDENCE_MODE,
            "columns_read": used, "columns_expected_but_not_mirrored": missing,
            "candidate_po_fields": [path for path, _, _ in CANDIDATE_FIELDS],
            "filters": {"origin": sorted(origin_filter) if origin_filter else None,
                        "status": sorted(status_filter) if status_filter else None,
                        "date_column": date_column, "date_from": date_from,
                        "date_to": date_to,
                        "date_filter_note": DATE_FILTER_RULE if date_column else None},
            "rules": {"origin": ORIGIN_RULE, "po_shape": PO_SHAPE_RULE,
                      "absence": ABSENCE_NOTE, "fm_order_entry": FM_ORDER_ENTRY_RULE,
                      "citation": "every per-order claim (origin, field statuses, status, "
                                  "values) derives only from the row its citation names"},
            "possible_sources_catalog": POSSIBLE_SOURCES,
            "completeness": completeness, "aggregates": aggregates,
            "orders": orders, "order_count": len(orders)}


def _fm_key(row: Mapping[str, Any], columns: Sequence[str]) -> str | None:
    exists, value = _field_value(row, columns, "custom_fields", FM_MARKER_KEY)
    return _key_text(value) if exists is True else None


def _fm_order_entries(mirror: Any, actor: Actor, scan: Mapping[str, Any],
                      max_staleness: float | None, enabled: bool
                      ) -> tuple[dict[str, list[dict[str, Any]]] | None, dict[str, Any]]:
    """FM order_entry rows by id for the scanned orders' fmIds, read through the mirror."""
    wanted = sum(1 for item in scan["rows"] if _fm_key(item["row"], scan["columns"]))
    info: dict[str, Any] = {"read": False, "problem": None, "orders_with_fm_id": wanted}
    if not enabled:
        info["problem"] = "not_requested"
        return None, info
    if not wanted:
        return None, info
    fm, problem = _related_rows(mirror, actor, FM_RELATION, max_staleness)
    info["problem"] = problem
    if fm is None:
        return None, info
    info["read"] = True
    index: dict[str, list[dict[str, Any]]] = {}
    for item in fm["rows"]:
        key = _key_text(item["row"].get("id"))
        if key is not None:
            index.setdefault(key, []).append({**item, "columns": fm["columns"]})
    return index, info


def _fm_order_entry_block(row: Mapping[str, Any], columns: Sequence[str],
                          index: dict[str, list[dict[str, Any]]] | None,
                          read: Mapping[str, Any]) -> dict[str, Any] | None:
    key = _fm_key(row, columns)
    if key is None or index is None:
        # Not read (no grant, refused or not requested): completeness.linked_reads
        # says why once instead of repeating it on every order.
        return None
    block: dict[str, Any] = {"relation": FM_RELATION, "fm_id": key}
    fm_id = parse_fm_oe_id(key)
    block["created_by_portal"] = fm_id is not None and fm_id >= PORTAL_OE_ID_BASE
    matches = index.get(key, [])
    if len(matches) != 1:
        if not matches and _inconsistent(read.get("problem")):
            # Every row was read but the mirror changed during the scan: the absence
            # is not a fact about either capture.
            block["status"] = "fm_scan_inconsistent"
        elif not matches and read.get("problem"):
            # An incomplete scan cannot show that the row is missing: not scanned, own status.
            block["status"] = "fm_row_not_scanned"
        else:
            block["status"] = "fm_row_not_found" if not matches else "fm_row_ambiguous"
        if read.get("problem"):
            block["problem"] = read["problem"]
        return block
    match = matches[0]
    field = fm_order_entry_field(row, columns, match["row"], match["columns"])
    return {**block, "status": field["status"], "citation": match["citation"],
            "custpocode": field}


# ---------------------------------------------------------------- document carriers

CARRIER_STATUSES = ("missing_in_portal", "conflict", "document_blank")
CARRIER_PAGE = 200
DOCUMENT_CARRIER_RULE = (
    "Per record kind a document class attests (document_fields.json), each record this "
    "reader may see whose version is cited to the Portal reporting API is compared with "
    "the values the document-field projection extracted from linked mail attachments and "
    "SharePoint originals (operating.attested_conflicts, as plx.object): a stated value "
    "every Portal field of the property holds empty is missing_in_portal, a different "
    "value conflict, a blank paper field facing a Portal value document_blank; equal "
    "values report nothing. Machine-read leads, never written, never authority.")


def _portal_cited(value: Mapping[str, Any]) -> bool:
    from integrations.source_mirror import parse_citation
    try:
        parse_citation((value.get("citation") or {}).get("l1_handle"))
    except (TypeError, ValueError):
        return False
    return True


def carrier_record_kinds(schema: Any) -> list[str]:
    """Record types some document class declares it attests (``comparisons``)."""
    from ontology.document_fields import comparisons
    return sorted({(schema.link_types.get(rule["link_type"]) or {}).get("to")
                   for rule in comparisons()} - {None})


def _carrier_finding(item: Mapping[str, Any], record: str, record_type: str
                     ) -> dict[str, Any] | None:
    """One lead or conflict of ``attested_conflicts.conflicts`` as a finding, or None
    when it compares no Portal-cited value (a Business Central record alone, or a
    conflict inside the document)."""
    document, *others = item["values"]
    portal = [value for value in others if _portal_cited(value)]
    if not portal:
        return None
    if item["kind"] == "missing_in_record":
        if len(portal) != len(others):   # an empty read outside Portal: not Portal's gap
            return None
        status = "missing_in_portal"
    elif item["kind"] == "value_disagreement":
        status = "document_blank" if document.get("value") is None else "conflict"
    else:
        return None
    finding = {"status": status, "record": record, "record_type": record_type,
               "subject": item["subject"], "document": document, "portal": portal,
               "tier": item.get("tier", "lead")}
    for key in ("document_citations", "fields_not_read", "propose"):
        if key in item:
            finding[key] = item[key]
    return finding


def document_carriers(tools: Any, actor: Actor) -> dict[str, Any]:
    """Information the captured documents state that Portal lacks or contradicts
    (DOCUMENT_CARRIER_RULE), as ``actor`` may read it in the projection behind
    ``tools`` (an ``OntologyTools``; its policy is the reader's). Records, links,
    documents, values and citations pass the same audience, marking, time and item
    gates as ``plx.object``: a reader sees only findings it could read there."""
    from operating.attested_conflicts import conflicts as attested

    if not isinstance(actor, Actor):
        raise CustomerPoError("invalid_actor")
    if tools.schema is None:
        raise CustomerPoError("schema_unavailable")
    scope = tools._scope(actor, tools._as_known_at({}))
    kinds = carrier_record_kinds(tools.schema)
    checked = {kind: 0 for kind in kinds}
    outside = {kind: 0 for kind in kinds}
    by_kind = {kind: {status: 0 for status in CARRIER_STATUSES} for kind in kinds}
    by_subject: dict[str, dict[str, int]] = {}
    not_compared: dict[str, int] = {}
    findings: list[dict[str, Any]] = []
    for kind in kinds:
        after = ""
        while True:
            rows = tools.store.objects_of_type(scope, kind, after, CARRIER_PAGE)
            if not rows:
                break
            after = rows[-1]["object_id"]
            visible = tools._visible_versions(scope, [row["object_id"] for row in rows])
            for record, version in sorted(visible.items()):
                if not _portal_cited({"citation": {"l1_handle": version["l1_handle"]}}):
                    outside[kind] += 1
                    continue
                checked[kind] += 1
                found, gaps, leads = attested(tools, scope, record, kind)
                for gap in gaps:
                    _count(not_compared, str(gap.get("reason")))
                for item in [*leads, *found]:
                    finding = _carrier_finding(item, record, kind)
                    if finding is None:
                        continue
                    by_kind[kind][finding["status"]] += 1
                    _count(by_subject.setdefault(finding["subject"], {}), finding["status"])
                    findings.append(finding)
            if len(rows) < CARRIER_PAGE:
                break
    return {"schema": "plx-document-carrier-coverage/v1", "rule": DOCUMENT_CARRIER_RULE,
            "record_kinds": kinds,
            "records_checked": {kind: n for kind, n in checked.items() if n},
            "records_not_portal": {kind: n for kind, n in outside.items() if n},
            "by_record_kind_status": {kind: counts for kind, counts in by_kind.items()
                                      if checked[kind]},
            "by_carrier_status": {subject: dict(sorted(counts.items()))
                                  for subject, counts in sorted(by_subject.items())},
            "not_compared_by_reason": dict(sorted(not_compared.items())),
            "by_status": {status: sum(counts[status] for counts in by_kind.values())
                          for status in CARRIER_STATUSES},
            "findings": findings, "finding_count": len(findings)}


# ---------------------------------------------------------------- CLI

FM_RELATION = "public.order_entry"
SAGE_RELATION = "sage.oe_order_header"
_INTEGRAL = re.compile(r"^-?[0-9]+(?:\.0+)?$")


def _key_text(value: Any) -> str | None:
    """Join key text: trimmed; '12.000' and 12 compare equal (ids may arrive as floats)."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if _INTEGRAL.fullmatch(text):
        text = text.split(".")[0].lstrip("0") or "0"
    return text or None


def _related_rows(mirror: Any, actor: Actor, relation: str, max_staleness: float | None,
                  ) -> tuple[dict[str, Any] | None, str | None]:
    """All rows of a related relation through the mirror (audience first), or why not."""
    from integrations.source_mirror import MirrorError

    try:
        scan = _scan(mirror, actor, [], max_staleness, MAX_SCAN_ROWS, relation)
    except MirrorError as error:
        return None, f"mirror_refused:{getattr(error, 'code', 'error')}"
    if (scan["reasons"] == ["mirror_changed_during_scan"]
            and len(scan["rows"]) == scan["total"]):
        # Every row was read, but not from one capture: absence proves nothing.
        return scan, "inconsistent:" + ",".join(scan["reasons"])
    if scan["reasons"] or len(scan["rows"]) != scan["total"]:
        return scan, "incomplete:" + ",".join(scan["reasons"] or ["rows_not_scanned"])
    return scan, None


def _inconsistent(problem: str | None) -> bool:
    return isinstance(problem, str) and problem.startswith("inconsistent:")


def _count(target: dict[str, int], key: str) -> None:
    target[key] = target.get(key, 0) + 1


def _fm_cross_check(sales: Sequence[Mapping[str, Any]], columns: Sequence[str],
                    fm: dict[str, Any] | None, problem: str | None) -> dict[str, Any]:
    """Counts of FM order_entry.custpocode for each sales order with an fmId (no values)."""
    out: dict[str, Any] = {"relation": FM_RELATION, "read": fm is not None,
                           "problem": problem, "by_lineage": {}}
    if fm is None:
        return out
    fm_columns = fm["columns"]
    by_id = {}
    for item in fm["rows"]:
        key = _key_text(item["row"].get("id"))
        if key is not None:
            by_id.setdefault(key, []).append(item["row"])
    for row in sales:
        exists, fm_id = _field_value(row, columns, "custom_fields", FM_MARKER_KEY)
        key = _key_text(fm_id) if exists is True else None
        if key is None:
            continue
        verdict = classify_order(row, columns)
        counter = out["by_lineage"].setdefault(verdict["origin"]["lineage"], {})
        matches = by_id.get(key, [])
        portal_created = key.isdigit() and int(key) >= PORTAL_OE_ID_BASE
        _count(counter, "portal_created_fm_row" if portal_created else "fm_created_fm_row")
        if len(matches) != 1:
            _count(counter, "fm_row_ambiguous" if matches else
                   "fm_scan_inconsistent" if _inconsistent(problem) else
                   "fm_row_not_scanned" if problem else "fm_row_not_found")
            continue
        if "custpocode" not in fm_columns:
            _count(counter, "custpocode_not_mirrored")
            continue
        fm_row = matches[0]
        value = fm_row.get("custpocode")
        if _is_empty(value):
            _count(counter, "custpocode_empty")
            continue
        text = _identifier_text(value)
        identifiers = set(order_identifiers(row, columns).values())
        erpcode = _identifier_text(fm_row.get("erpcode"))
        if text is not None and (text in identifiers or (erpcode and text == erpcode)):
            _count(counter, "custpocode_equals_order_number" +
                   ("_portal_created" if portal_created else ""))
            continue
        portal_values = set(verdict.get("customer_po_values", ()))
        legacy = _field_value(row, columns, "custom_fields", "custPoCode")
        if legacy[0] is True and _identifier_text(legacy[1]) == text:
            _count(counter, "custpocode_equals_legacy_custPoCode")
        if po_like(value):
            if text in portal_values:
                _count(counter, "custpocode_po_like_same_as_portal_po")
            elif portal_values:
                _count(counter, "custpocode_po_like_differs_from_portal_po")
            else:
                _count(counter, "custpocode_po_like_portal_has_no_po")
        else:
            _count(counter, "custpocode_not_po_like")
            if probable_po_after_normalization(value):
                _count(counter, "custpocode_not_po_like_probable")
    return out


def _sage_cross_check(sales: Sequence[Mapping[str, Any]], columns: Sequence[str],
                      sage: dict[str, Any] | None, problem: str | None) -> dict[str, Any]:
    """Counts comparing Sage oe_order_header.ponumber with po_reference (no values)."""
    out: dict[str, Any] = {"relation": SAGE_RELATION, "read": sage is not None,
                           "problem": problem, "by_po_reference_status": {}}
    if sage is None:
        return out
    indexes: dict[str, dict[str, list[dict[str, Any]]]] = {"orduniq": {}, "ordnumber": {}}
    for item in sage["rows"]:
        for column, index in indexes.items():
            key = _key_text(item["row"].get(column))
            if key is not None:
                index.setdefault(key, []).append(item["row"])
    for row in sales:
        verdict = classify_order(row, columns)
        if verdict["origin"]["value"] != "sage_import":
            continue
        field = verdict["fields"]["po_reference"]
        counter = out["by_po_reference_status"].setdefault(field["status"], {})
        matches: list[dict[str, Any]] = []
        for key_name, column in (("sageOrdUniq", "orduniq"), ("sageSourceOrdUniq", "orduniq"),
                                 ("sageOrdNumber", "ordnumber")):
            exists, value = _field_value(row, columns, "custom_fields", key_name)
            key = _key_text(value) if exists is True else None
            if key is not None and column in sage["columns"]:
                matches = indexes[column].get(key, [])
                if matches:
                    break
        if len(matches) != 1:
            _count(counter, "header_not_found" if not matches else "header_ambiguous")
            continue
        if "ponumber" not in sage["columns"]:
            _count(counter, "ponumber_not_mirrored")
            continue
        ponumber = matches[0].get("ponumber")
        reference = _field_value(row, columns, "po_reference", None)[1]
        if _is_empty(ponumber):
            # ETL: po_reference = COALESCE(ponumber, po_reference): an earlier value kept.
            _count(counter, "ponumber_empty")
        elif _identifier_text(ponumber) == _identifier_text(reference):
            _count(counter, "ponumber_equals_po_reference")
        else:
            _count(counter, "ponumber_differs")
            if po_like(ponumber):
                _count(counter, "ponumber_differs_and_po_like")
    return out


def shape_report(mirror: Any, actor: Actor, *, max_staleness: float | None = None,
                 cross_check: bool = True) -> dict[str, Any]:
    """Counts-only coverage report for the live auditor: no values, keys or citations.

    With ``cross_check`` it also reads ``public.order_entry`` and
    ``sage.oe_order_header`` through the mirror (the actor's audience applies;
    a refusal is reported, not raised) and counts how the FM custpocode and the
    Sage ponumber relate to each sales order's PO fields.
    """
    result = coverage(mirror, actor, max_staleness=max_staleness, shape_signatures=True,
                      read_fm_order_entry=cross_check)
    completeness = result["completeness"]
    aggregates = result["aggregates"]
    keep = ("rows", "complete", "by_status", "by_origin_status", "by_origin_field_status",
            "by_origin_field_not_po_like_reason",
            "by_origin_field_probable_po_after_normalization",
            "orders_with_probable_po_after_normalization",
            "by_lineage_status", "orders_with_fm_order_entry_created_by_portal",
            "by_origin_fm_order_entry_custpocode_status",
            "orders_without_portal_po_whose_fm_order_entry_holds_one",
            "by_origin_field_not_po_like_shape",
            "by_origin_field_not_po_like_shape_signature")
    return {"schema": "plx-customer-po-shape-report/v1", "relation": RELATION,
            "evidence_mode": "source mirror of the Portal reporting API (advisory, counts "
                             "only; no value, key or citation is echoed): replica of the "
                             "staging primary (FM/Sage/gold ETL and bridges), not the UAT DB "
                             "the staging Portal UI writes",
            "rules": {"po_shape": PO_SHAPE_RULE, "origin": ORIGIN_RULE, "shapes": SHAPE_NOTE},
            "completeness": {name: completeness[name] for name in (
                "complete", "scan_complete", "reasons", "rows_matched_in_mirror",
                "rows_scanned")},
            "aggregates": {name: aggregates[name] for name in keep if name in aggregates},
            "cross_checks": _cross_checks(mirror, actor, max_staleness) if cross_check else None}


def _cross_checks(mirror: Any, actor: Actor, max_staleness: float | None) -> dict[str, Any]:
    sales = _scan(mirror, actor, [], max_staleness, MAX_SCAN_ROWS)
    rows = [item["row"] for item in sales["rows"]]
    fm, fm_problem = _related_rows(mirror, actor, FM_RELATION, max_staleness)
    sage, sage_problem = _related_rows(mirror, actor, SAGE_RELATION, max_staleness)
    return {"note": "counts only; joins: fmId -> order_entry.id, sageOrdUniq/"
                    "sageSourceOrdUniq -> orduniq else sageOrdNumber -> ordnumber; "
                    "separate non-atomic captures of each relation; when problem says "
                    "incomplete, FM rows not reached count as fm_row_not_scanned and "
                    "Sage *_not_found counts include rows that were not scanned",
            "fm_order_entry": _fm_cross_check(rows, sales["columns"], fm, fm_problem),
            "sage_order_header": _sage_cross_check(rows, sales["columns"], sage,
                                                   sage_problem)}


def _carrier_counts(state_dir: Path, host_config: Path, environ: Mapping[str, str],
                    options: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
    """One host capture cycle into ``<state_dir>/document_carriers`` (``operating.host``:
    GET-only readers; its own mirror, apart from the coverage scan's), then
    :func:`document_carriers` over its projection as ``actor``: counts only. ``state`` is
    ``read`` only when every configured source captured completely, else
    ``read_incomplete`` (the counts cover what was captured)."""
    from .host import Host, HostConfig, HostError

    try:
        host = Host(state_dir / "document_carriers", HostConfig.from_file(host_config),
                    environ=environ, **options)
        cycle = host.cycle()
    except HostError as error:
        return {"state": "capture_failed", "reasons": [error.code]}
    out: dict[str, Any] = {"capture": {name: {key: outcome.get(key) for key in (
        "state", "complete", "reasons") if key in outcome}
        for name, outcome in sorted((cycle.get("sources") or {}).items())}}
    projection = cycle.get("projection") or {}
    if projection.get("state") != "built":
        return {"state": "no_projection", "projection_state": projection.get("state"), **out}
    with host.served_tools(actor) as served:
        tools = served.tools
        tools.policy = served.policy_now(actor)
        with tools.store.lock:
            report = document_carriers(tools, actor)
    complete = all(outcome.get("complete") is True for outcome in out["capture"].values()
                   if outcome.get("state") != "not_configured")
    keep = ("record_kinds", "records_checked", "records_not_portal", "by_record_kind_status",
            "by_carrier_status", "not_compared_by_reason", "by_status", "finding_count")
    return {"state": "read" if complete else "read_incomplete", **out,
            **{name: report[name] for name in keep}}


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None) -> int:
    """``python3 -m operating.customer_po_coverage``: the counts-only shape report.

    ``--host-config`` adds ``document_carriers``: one host capture cycle into the state
    directory and the counts of :func:`document_carriers` over its projection, read as
    the same principals. ``environ`` / ``host_options`` (tests): the variables and the
    host's injected transports (``workbench_fetch`` also serves the coverage read)."""
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="python3 -m operating.customer_po_coverage",
        description="Counts-only customer-PO coverage and not-PO-like shape histogram "
                    "over public.app_sales_order (read-only; prints no values or key).")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--audience", required=True, type=Path)
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--max-staleness", type=float, default=None)
    parser.add_argument("--no-cross-check", action="store_true",
                        help="skip reading public.order_entry and sage.oe_order_header")
    parser.add_argument("--host-config", type=Path, default=None,
                        help="also capture once with this host config and count what the "
                             "captured documents state that Portal lacks or contradicts")
    parser.add_argument("--base-url-env", default="PLX_WORKBENCH_API_URL")
    parser.add_argument("--key-env", default="PLX_WORKBENCH_API_KEY")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    base_url, key = env.get(args.base_url_env), env.get(args.key_env)
    if not base_url or not key:
        parser.error(f"{args.base_url_env} and {args.key_env} must be set (read-only API)")
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from ontology.types import ValidationError
    from .case_runtime import CaseRuntime

    try:
        actor = Actor(args.tenant, frozenset(args.principal), args.clearance)
    except ValidationError as error:
        parser.error(f"invalid principal or clearance: {error}")
    fetch = (host_options or {}).get("workbench_fetch")
    client = (WorkbenchReadApiClient(base_url, key) if fetch is None
              else WorkbenchReadApiClient(base_url, key, fetch=fetch))
    runtime = CaseRuntime(args.state_dir, client=client, audience=args.audience,
                          tenant=args.tenant)
    try:
        report = shape_report(runtime.mirror, actor, max_staleness=args.max_staleness,
                              cross_check=not args.no_cross_check)
    finally:
        runtime.close()
    if args.host_config is not None:
        report["document_carriers"] = _carrier_counts(
            args.state_dir, args.host_config, env, host_options or {}, actor)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


__all__ = ["ABSENCE_NOTE", "CANDIDATE_FIELDS", "CARRIER_STATUSES", "CustomerPoError",
           "DATE_FILTER_RULE", "DOCUMENT_CARRIER_RULE", "carrier_record_kinds",
           "document_carriers",
           "FIELD_STATUSES", "NOT_PO_LIKE_REASONS", "not_po_like_reasons",
           "probable_po_after_normalization",
           "MAX_SCAN_ROWS", "ORDER_STATUSES", "ORIGINS", "ORIGIN_RULE", "POSSIBLE_SOURCES",
           "PO_SHAPE_RULE", "RELATION", "classify_field",
           "classify_order", "classify_origin", "coverage", "not_po_like_shape",
           "parse_date_value", "po_like", "possible_sources", "SHAPE_DIMENSIONS",
           "shape_report", "FM_ORDER_ENTRY_RULE", "fm_order_entry_field", "parse_fm_oe_id",
           "portal_trim_text"]


if __name__ == "__main__":
    raise SystemExit(main())
