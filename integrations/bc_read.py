"""Read-only Business Central (API v2.0) reader with resolvable citations.

BC is Production with no sandbox: this module issues GETs only, through
``live_source_guard.GuardedTransport`` (exact host allowlist, action-path
denylist incl. every ``Microsoft.NAV.*`` bound action, no redirects, byte
caps, timeouts). The app-only token can read every company, so entitlements
come from a declared ``LiveSourceAudience``: ``bc:<environment>:companies`` for
the company list and ``bc:<environment>:<company-id>:<entitySet>`` for entity
reads, default deny, checked before the request. BC's API exposes no
per-record ACL; records carry ``source_acl.status = "not_exposed"``, so a
resource listed in ``source_acl_required`` is always withheld.

Every URL comes from the closed request grammar (``live_source_grammar``:
``bc.companies``, ``bc.entities``, ``bc.entity`` and one literal
``bc.lines.<documentSet>`` template per header/line pair, with typed GUID,
tenant, environment and allowlisted entity-set parameters); a get-by-id must
return the requested id. Query options come only from
``live_source_grammar.bc_query``: ``$filter`` is one equality on an
allowlisted field built from a validated number or GUID (never a
caller-written expression), ``$top`` is 1..1000, ``$select`` names come from
the entity set's allowlist, ``$expand`` is only a header set's own line set. A typed
listing (``list_entities``, ``list_lines``) whose ``$select``/``$filter``/``$orderby``
names a field the pinned ``$metadata`` does not declare is refused before any request;
a failed read carries its ``request_shape`` (entity path, option and field names, never a
value).
Lines of a document are read by the nested navigation (or with their header page,
``read_document_page``) and must name that document (``documentId``); others are
dropped and counted. Listings carry ``completeness`` (truncation is never silent).

Lead lookup: ``find_by_number`` returns every record whose number (or
another allowlisted number field such as ``externalDocumentNumber``) equals
the value, with ``ambiguous`` and ``status``; it never picks one and never
asserts identity (``integrations.bc_case_evidence`` adds the Portal caveats).

Money and quantities stay exact: every JSON number (integer or not) is parsed
as Decimal and stored as its decimal text, so one field never mixes int and
string representations.

Canonical identity: company and record ids are GUIDs, lower-cased before they
form the resource key, the request and the citation; entity-set names are
validated and matched case-insensitively by the audience, so no alias of one
BC resource can select a different audience entry.

Citation: ``bc://<environment>/<company-id>/<entitySet>/<id>@<@odata.etag>``.
``resolve_citation`` re-reads the record by id and reports ``current`` or
``changed_since``.

Transient failures: every GET (never the OAuth token POST) is retried at
most twice through ``live_source_guard.BoundedBackoff`` (the same helper as
``plx_hub_guard.HubTransport``): on a sender failure with no status
(``URLError``, timeout, reset, TLS, a cut body) and on HTTP 429/502/503/504,
with jittered exponential backoff capped at 4 s, ``Retry-After`` honoured up
to the cap, and at most ``RETRY_BUDGET_SECONDS`` of sleep per ``build_bc``
api so the probe's sequential burst cannot stall for minutes. 400/401/403/404
are never retried. After the bound the original error is raised with
"gave up after N attempts". The token POST is sent once: issuing a token has
no business effect, but the transport keeps every POST single-shot so the
read-only rule stays simple to audit; the token is cached (~1 h), so a failed
token request fails only that read and the next read requests a new one.
A probe's GETs use ``ProbeBackoff`` instead (the same one retry layer, longer pauses):
live connect failures come in bursts that outlast three attempts within seconds, so a
probe GET pauses 5 s, then 15 s, honours a 429's ``Retry-After`` up to 15 s and stops on a
longer one; no read starts, and no GET is repeated, that could end past the probe's wall
cap (``PROBE_WALL_SECONDS``, under every probe check's timeout); a read cut there,
before it starts or at a retry, fails typed ``wall_cap``.

Versioned capture (L1; ``BcCapture`` into ``integrations.capture_store``): every
company the pinned book map names (``bc_company_books.json``, by GUID only, from
Portal ``company-pins.ts``; lineage ``bc_<book>``) and every entity set of
``CAPTURE_ENTITY_SETS`` (document lines in the header page's GET by ``$expand`` where
the pinned ``$metadata`` declares the navigation, else, and for any header whose expansion
is not whole, per header by the nested navigation), paged by ``@odata.nextLink`` with
each page committed with its cursor, so a capture resumes after the last committed
page. Captured records cite
``bc://<environment>/<company-id>/<entitySet>/<id>@<generation>-<capture-id>``
and resolve offline; a changed etag adds a version and keeps the old one.
Completeness is reported per company and entity set; a partial read (budget,
record cap, refused or throttled GET) is never reported complete. Incremental
capture (``mode="auto"``, devloop/DECISIONS/2026-10-02-bc-capture-incremental.md):
modified-since reads per company x entity set after the first full pass, posted
invoice/credit-memo lines kept while their header version is unchanged, and a
key-only full pass weekly that marks keys BC no longer lists
``missing_from_source_since`` (see ``BcCapture``). A capture run is complete only
when the company listing is complete and lists every pinned company.

Probe and capture (credentialed auditor; counts and shapes only):
``python3 -m integrations.bc_read probe [--entity purchaseOrders] [--case-entities]``
``python3 -m integrations.bc_read metadata-drift [--out FILE]`` (the schema pin's live check)
``python3 -m integrations.bc_read capture [--db F] [--budget-seconds N] [--resolve-sample N]
[--mode auto|full] [--passes N] [--key-pass-last] [--generated-first]``
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Collection, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

from ontology.core import Actor

from . import live_source_grammar as grammar
from .capture_store import (CaptureError, CaptureStore, CapturedRecord, bc_unit,
                            is_capture_handle, make_handle, source_kind)
from .live_source_guard import (BC_HOST, BC_SCOPE, MESSAGE_CLASS_TOKENS,
                                BoundedBackoff, ClientCredentials, GuardedTransport,
                                LiveSourceAudience, NotVisible, ReadOnlyGuardError,
                                RequestBudgetExhausted,
                                SourceApi, SourceNotFound, SourceUnavailable, TokenProvider,
                                canonical_sha256, completeness_with_withheld,
                                failure_diagnostics, is_error_code_token,
                                is_nextlink_refusal,
                                jsonable, nextlink_refused, parse_handle,
                                q, unavailable_detail)


SCHEME = "bc"
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _guid(value: Any, label: str) -> str:
    if not isinstance(value, str) or _GUID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a GUID")
    return value.lower()


def company_key(company_id: str) -> str:
    """A company's key in a report that never carries its id (``capture_probe``): 16 hex
    of the sha256 of its lower-case GUID."""
    return canonical_sha256(company_id.lower())[:16]


def _entity_set(value: Any) -> str:
    if not grammar.TYPES["entity_set"].valid(value):
        raise ValueError("entity set not in the read allowlist")
    return value


def citation(environment: str, company_id: str, entity_set: str, record_id: str,
             etag: str) -> str:
    return (f"{SCHEME}://{q(environment.lower())}/{q(company_id)}/{entity_set}/"
            f"{q(record_id)}@{q(etag)}")


_KEY = re.compile(r"\([^)]*\)")
_LITERAL = re.compile(r"'(?:[^']|'')*'")
_COMPARED = re.compile(r"\b([A-Za-z][A-Za-z0-9]{0,63})\s+(?:eq|ne|gt|ge|lt|le)\s")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")


def _query_shape(pairs: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """Query option names (no ``$``), the ``$select`` names and the fields ``$filter``
    compares and ``$orderby`` sorts by; never a value: quoted literals are dropped first
    and a field is only a name left of a comparison."""
    shape: dict[str, set[str]] = {"options": set(), "select": set(), "filter": set(),
                                  "orderby": set()}
    for key, value in pairs:
        shape["options"].add(key.lstrip("$"))
        if key == "$select":
            shape["select"].update(value.split(","))
        elif key == "$filter":
            shape["filter"].update(_COMPARED.findall(_LITERAL.sub("''", value)))
        elif key == "$orderby":
            shape["orderby"].update(value.split()[:1])
    return {k: sorted(n for n in names if _NAME.fullmatch(n)) for k, names in shape.items()}


def request_shape(url: str) -> dict[str, Any]:
    """The shape of one BC read, never a value (gap-1b49174b629f): ``path`` the entity
    sets below the company with every record key dropped (``customerPaymentJournals``,
    ``salesInvoices/salesInvoiceLines``) and the ``_query_shape`` of its query."""
    parts = urlsplit(url)
    path = _KEY.sub("", unquote(parts.path)).partition("/api/v2.0/")[2].split("/")
    path = path[1:] if path[0] == "companies" and len(path) > 1 else path
    return {"path": "/".join(s if _NAME.fullmatch(s) else "?" for s in path),
            **_query_shape(parse_qsl(parts.query, keep_blank_values=True))}


_PINNED: dict[str, frozenset[str]] | None = None


def pinned_properties() -> dict[str, frozenset[str]]:
    """Property names per entity set of the pinned ``$metadata``
    (``ontology/schema/generated/meta/bc_metadata.json``)."""
    global _PINNED
    if _PINNED is None:
        from ontology.generator.bc import load_pin
        _PINNED = {name: frozenset(p["name"] for p in entry.get("properties", ()))
                   for name, entry in load_pin()["entity_sets"].items()}
    return _PINNED


def _require_pinned(entity_set: str, query: Mapping[str, str] | None) -> None:
    """Refuse, before any request, a read whose ``$select``, ``$filter`` or ``$orderby``
    names a field the pinned ``$metadata`` does not declare for ``entity_set`` (BC answers
    400 to it in every company): the request is the defect, never BC's configuration."""
    shape = _query_shape((query or {}).items())
    lacking = sorted({*shape["select"], *shape["filter"], *shape["orderby"]}
                     - pinned_properties().get(entity_set, frozenset()))
    if lacking:
        raise ValueError(f"the pinned $metadata declares no {', '.join(lacking)} "
                         f"on {entity_set}")


def _shaped(read: Callable[[str], Any], url: str) -> Any:
    """``read(url)``; a failure carries the ``request_shape`` of ``url``."""
    try:
        return read(url)
    except SourceUnavailable as exc:
        exc.request_shape = request_shape(url)
        raise


# The purchase-order field page (``capture_store.BC_FIELD_PAGES``): each purchase order's
# ``Your_Reference`` from ODataV4 Page 50, stored under the API v2.0 order's id, one unit
# per company with a pinned name (``BcCapture._capture_references``).
REFERENCE_SET = "purchaseOrderReferences"
REFERENCE_PARENT_SET = "purchaseOrders"
# ``SourceApi.get_pages``'s own item bound; live 2026-10-21: 355 orders in My Company. A
# company with more is read truncated (the unit partial), never silently whole.
REFERENCE_MAX_ITEMS = 5000
# Page 50 rows per page (``Prefer: odata.maxpagesize``): three short fields a row, so the
# reference traversal takes BC's page ceiling whatever the record page size of the call.
REFERENCE_PAGE_SIZE = 1000
# The counts of a field-page unit (``BcCapture._capture_references``); the ``REFERENCE_
# DROPPED`` ones are rows never stored.
REFERENCE_DROPPED = ("no_number", "ambiguous_api_record", "duplicate_number", "vendor_mismatch")
REFERENCE_COUNTS = (*REFERENCE_DROPPED, "no_api_record", "blank_reference", "stored")


class BusinessCentralReader:
    """Entitlement-checked GETs against one BC tenant/environment."""

    def __init__(self, api: SourceApi, audience: LiveSourceAudience, *, tenant_id: str,
                 environment: str) -> None:
        if not isinstance(environment, str) or _ENVIRONMENT.fullmatch(environment) is None:
            raise ValueError("BC environment name invalid")
        self._api = api
        self._audience = audience
        self.environment = environment
        if not grammar.TYPES["tenant"].valid(tenant_id):
            raise ValueError("BC tenant id invalid")
        self._where = {"tenant": tenant_id, "environment": environment}

    def __repr__(self) -> str:
        return f"BusinessCentralReader(environment={self.environment!r})"

    @property
    def tenant_id(self) -> str:
        return self._where["tenant"]

    @property
    def evidence_label(self) -> str:
        """``live-read`` when requests go through the real GET-only sender, else
        ``injected-transport`` (an injected test sender). Never ``live-verified``:
        that label comes only from a live verifier result."""
        transport = getattr(self._api, "_transport", None)
        injected = getattr(transport, "sender_injected", True)
        return "injected-transport" if injected is not False else "live-read"

    @property
    def requests_sent(self) -> int:
        """Requests the guarded transport has sent (token POSTs included), else 0."""
        transport = getattr(self._api, "_transport", None)
        sent = getattr(transport, "requests_sent", 0)
        return sent if isinstance(sent, int) else 0

    def resource(self, company_id: str | None = None, entity_set: str | None = None) -> str:
        env = self.environment.lower()
        if company_id is None:
            return f"bc:{env}:companies"
        return f"bc:{env}:{_guid(company_id, 'company id')}:{_entity_set(entity_set)}"

    @staticmethod
    def _source_acl() -> dict[str, Any]:
        return {"status": "not_exposed", "identities": [],
                "note": "BC API v2.0 exposes no per-record ACL; audience map only"}

    def _record(self, company_id: str, entity_set: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """A report page keyed by its party (``grammar.BC_RECORD_KEY_FIELD``: the aged
        balances) is identified by that key; when BC sends it no etag, its version is
        the digest of its fields (a recomputed report row has no stored version)."""
        key = grammar.BC_RECORD_KEY_FIELD.get(entity_set, "id")
        record_id, etag = raw.get(key), raw.get("@odata.etag")
        if not isinstance(record_id, str) or not record_id:
            raise SourceUnavailable("BC record lacks id")
        fields = {k: v for k, v in raw.items() if isinstance(k, str) and not k.startswith("@")}
        if (not isinstance(etag, str) or not etag) and key != "id":
            etag = "sha256:" + canonical_sha256(jsonable(fields))
        if not isinstance(etag, str) or not etag:
            raise SourceUnavailable("BC record lacks @odata.etag")
        record = {"source": "business-central", "environment": self.environment,
                  "company_id": company_id, "entity_set": entity_set, "id": record_id,
                  "etag": etag, "last_modified": raw.get("lastModifiedDateTime"),
                  "fields": fields, "source_acl": self._source_acl()}
        record = jsonable(record)
        record["content_sha256"] = canonical_sha256(record)
        record["citation"] = citation(self.environment, company_id, entity_set, record_id, etag)
        return record

    def companies(self, actor: Actor, *, max_items: int = 50) -> dict[str, Any]:
        resource = self.resource()
        self._audience.require(actor, resource)
        raws, completeness = self._api.get_pages(grammar.url("bc.companies", **self._where),
                                                 max_pages=2,
                                                 max_items=max_items)
        items = [jsonable({"id": raw.get("id"), "name": raw.get("name"),
                           "display_name": raw.get("displayName"),
                           "etag": raw.get("@odata.etag")}) for raw in raws]
        return {"source": "business-central", "environment": self.environment,
                "items": items, "completeness": completeness_with_withheld(completeness, 0)}

    def metadata(self, actor: Actor) -> bytes:
        """The API v2.0 ``$metadata`` document (CSDL XML): entity, property and navigation
        names and types, no record. Audience resource ``bc:<environment>:metadata``."""
        self._audience.require(actor, f"bc:{self.environment.lower()}:metadata")
        return self._api.get_document(grammar.url("bc.metadata", **self._where))

    def _list(self, actor: Actor, company_id: str, entity_set: str, url: str, *,
              resources: Sequence[str], page_size: int, max_pages: int, max_items: int,
              parent_id: str | None = None) -> dict[str, Any]:
        if isinstance(page_size, bool) or not isinstance(page_size, int) \
                or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be 1..1000")
        raws, completeness = _shaped(lambda u: self._api.get_pages(
            u, max_pages=max_pages, max_items=max_items,
            headers={"Prefer": f"odata.maxpagesize={page_size}"}), url)
        records, withheld, dropped = [], 0, 0
        parent_field = grammar.BC_LINE_PARENT_FIELD.get(entity_set, "documentId")
        for raw in raws:
            if parent_id is not None and str(raw.get(parent_field, "")).lower() != parent_id:
                dropped += 1  # a line that does not name the requested document
                continue
            record = self._record(company_id, entity_set, raw)
            if all(self._audience.item_visible(actor, r, record["source_acl"])
                   for r in resources):
                records.append(record)
            else:
                withheld += 1
        completeness = completeness_with_withheld(completeness, withheld)
        if parent_id is not None:
            completeness["provenance_dropped"] = dropped
            completeness["items_returned"] -= dropped
        return {"source": "business-central", "environment": self.environment,
                "company_id": company_id, "entity_set": entity_set, "items": records,
                "completeness": completeness}

    def list_entities(self, actor: Actor, company_id: str, entity_set: str, *,
                      equals: tuple[str, str] | None = None,
                      select: Sequence[str] | None = None, top: int | None = None,
                      orderby: str | None = None, page_size: int = 100, max_pages: int = 5,
                      max_items: int = 500) -> dict[str, Any]:
        """One typed listing: ``equals`` is ``(field, value)`` from the entity
        set's filter allowlist, ``select`` from its ``$select`` allowlist,
        ``top`` 1..1000, ``orderby`` ``"<field> asc|desc"`` from its orderby
        allowlist (``live_source_grammar.bc_query``)."""
        company_id = _guid(company_id, "company id")
        resource = self.resource(company_id, entity_set)
        self._audience.require(actor, resource)
        query = grammar.bc_query(entity_set, equals=equals, top=top, select=select,
                                 orderby=orderby)
        _require_pinned(entity_set, query)
        url = grammar.url("bc.entities", query or None, company=company_id,
                          entity=entity_set, **self._where)
        return self._list(actor, company_id, entity_set, url, resources=[resource],
                          page_size=page_size, max_pages=max_pages, max_items=max_items)

    def purchase_order_references(self, actor: Actor, company_id: str, company_name: str, *,
                                  page_size: int = 500, max_pages: int = 50,
                                  max_items: int = REFERENCE_MAX_ITEMS) -> dict[str, Any]:
        """The ``Your_Reference`` (FileMaker PO number) of every purchase order of one
        company, read from the ODataV4 web-service page ``PurchaseOrder`` (Page 50): API
        v2.0 ``purchaseOrders`` does not expose it. The one request shape is
        ``grammar.bc_odata_purchase_order_query`` (``$select=No,Your_Reference,
        Buy_from_Vendor_No``) on ``Company('<company_name>')``; ``company_name`` must be
        the pinned name of ``company_id`` (``bc_company_books.json``) and the caller
        checks it against the company listing (``BcCapture``). The fields are fields of
        the company's purchase orders, so the audience resource is ``purchaseOrders``'.
        Items: ``{"no", "fields", "etag"}`` (``fields`` exactly the three selected; a row
        BC sends with no etag is versioned by the digest of its fields); a row without a
        ``No`` is counted ``no_number``, never returned."""
        company_id = _guid(company_id, "company id")
        if isinstance(page_size, bool) or not isinstance(page_size, int) \
                or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be 1..1000")
        resource = self.resource(company_id, REFERENCE_PARENT_SET)
        self._audience.require(actor, resource)
        url = grammar.url("bc.odata_purchase_orders", grammar.bc_odata_purchase_order_query(),
                          company=company_name, **self._where)
        raws, completeness = _shaped(lambda u: self._api.get_pages(
            u, max_pages=max_pages, max_items=max_items,
            headers={"Prefer": f"odata.maxpagesize={page_size}"}), url)
        items, withheld, no_number = [], 0, 0
        for raw in raws:
            fields = {name: raw.get(name) for name in grammar.BC_ODATA_PO_FIELDS}
            number = fields["No"]
            if not isinstance(number, str) or not number.strip():
                no_number += 1
                continue
            if not self._audience.item_visible(actor, resource, self._source_acl()):
                withheld += 1
                continue
            etag = raw.get("@odata.etag")
            if not isinstance(etag, str) or not etag:
                etag = "sha256:" + canonical_sha256(jsonable(fields))
            items.append({"no": number, "fields": jsonable(fields), "etag": etag})
        completeness = completeness_with_withheld(completeness, withheld)
        completeness["no_number"] = no_number
        return {"source": "business-central", "environment": self.environment,
                "company_id": company_id, "items": items, "completeness": completeness}

    def list_lines(self, actor: Actor, company_id: str, document_set: str, document_id: str, *,
                   select: Sequence[str] | None = None, page_size: int = 100,
                   max_pages: int = 5, max_items: int = 500,
                   flat: bool = False) -> dict[str, Any]:
        """Lines of one BC document by the nested navigation
        ``<document_set>(<id>)/<lines>``, or (``flat``) by the line collection filtered
        ``documentId eq <id>`` (a document line set only; the capture's fallback when
        the navigation fails for one document). The audience must grant both the
        document set and the line set; a line that does not name the requested
        document (``documentId``, ``journalId`` for payment journal lines) is dropped
        and counted (``provenance_dropped``)."""
        company_id = _guid(company_id, "company id")
        if document_set not in grammar.BC_DOCUMENT_LINES:
            raise ValueError("entity set has no line navigation")
        line_set = grammar.BC_DOCUMENT_LINES[document_set]
        resources = [self.resource(company_id, document_set),
                     self.resource(company_id, line_set)]
        for resource in resources:
            self._audience.require(actor, resource)
        document_id = _guid(document_id, "document id")
        if flat:
            query = grammar.bc_query(line_set, equals=("documentId", document_id),
                                     select=select)
            _require_pinned(line_set, query)
            url = grammar.url("bc.entities", query, company=company_id, entity=line_set,
                              **self._where)
        else:
            query = grammar.bc_query(line_set, select=select)
            _require_pinned(line_set, query)
            url = grammar.url(f"bc.lines.{document_set}", query or None, company=company_id,
                              record=document_id, **self._where)
        listing = self._list(actor, company_id, line_set, url, resources=resources,
                             page_size=page_size, max_pages=max_pages, max_items=max_items,
                             parent_id=document_id)
        listing["document"] = {"entity_set": document_set, "id": document_id}
        return listing

    def find_by_number(self, actor: Actor, company_id: str, entity_set: str, number: str, *,
                       field: str = "number", max_matches: int = 50,
                       select: Sequence[str] | None = None) -> dict[str, Any]:
        """Lead lookup: every record whose ``field`` equals ``number`` (one
        exact-equality ``$filter``). Returns ALL matches with an ambiguity flag
        and never picks one; a match is a lead, not an identity.

        ``$top`` is ``max_matches + 1`` so more matches than the cap reads as
        ``truncated``. BC may compare Code fields case-insensitively; records
        whose value differs other than by case are dropped and counted
        (``mismatch_dropped``), and ``case_exact`` says whether the text is
        identical. ``select`` (the entity set's ``$select`` allowlist) always
        includes ``field`` so the match can be checked."""
        if isinstance(max_matches, bool) or not isinstance(max_matches, int) \
                or not 1 <= max_matches <= 200:
            raise ValueError("max_matches must be 1..200")
        spec = grammar.BC_QUERY_SPECS.get(entity_set)
        if spec is not None and spec.filters.get(field) == "guid":
            if not isinstance(number, str):  # the grammar checks the GUID itself
                raise ValueError("number invalid")
        elif not grammar.bc_number_ok(number):
            raise ValueError("number invalid")
        if select is not None:
            if isinstance(select, (str, bytes)):
                raise ValueError("select must be a sequence of names")
            select = sorted({*select, field})
        listing = self.list_entities(actor, company_id, entity_set, equals=(field, number),
                                     select=select, top=max_matches + 1,
                                     page_size=max_matches + 1, max_pages=2,
                                     max_items=max_matches)
        matches, mismatched = [], 0
        for record in listing["items"]:
            value = record["fields"].get(field)
            if not isinstance(value, str) or value.casefold() != number.casefold():
                mismatched += 1
                continue
            matches.append({"case_exact": value == number, "record": record})
        completeness = dict(listing["completeness"])
        completeness["mismatch_dropped"] = mismatched
        hidden = bool(completeness.get("truncated")) or completeness.get("withheld_count", 0) > 0
        if completeness.get("truncated"):
            status = "truncated"
        elif completeness.get("withheld_count", 0) > 0:
            status = "partially_withheld"
        elif not matches:
            status = "none_found"
        elif len(matches) == 1:
            status = "single_candidate"
        else:
            status = "ambiguous"
        return {"source": "business-central", "environment": self.environment,
                "company_id": listing["company_id"], "entity_set": entity_set,
                "field": field, "number": number, "evidence_role": "lead",
                "identity_asserted": False, "status": status, "match_count": len(matches),
                "ambiguous": len(matches) > 1 or hidden,
                "matches": matches, "completeness": completeness}

    def collection_url(self, company_id: str, entity_set: str,
                       query: Mapping[str, str] | None = None) -> str:
        """The listing URL of one entity set (closed grammar); ``query`` must come from
        ``live_source_grammar.bc_query``."""
        return grammar.url("bc.entities", dict(query) if query else None,
                           company=_guid(company_id, "company id"),
                           entity=_entity_set(entity_set), **self._where)

    def read_page(self, actor: Actor, company_id: str, entity_set: str, url: str | None, *,
                  page_size: int = 100, query: Mapping[str, str] | None = None
                  ) -> tuple[list[dict[str, Any]], int, str | None]:
        """One page of a listing: (visible records, withheld count, next URL).

        ``url`` is None for the first page (the collection with ``query``, a
        ``bc_query`` mapping such as a modified-since filter) or a next URL this
        method returned earlier (a persisted capture cursor). A cursor must continue
        that same first page (collection and query) and be one the guard sends
        (``SourceApi.continuation_url``); otherwise ``nextlink_refused`` is raised
        before any request. The audience is checked before the request."""
        if query and "$expand" in query:
            raise ValueError("an expanded page is read by read_document_page")
        company_id = _guid(company_id, "company id")
        resource = self.resource(company_id, entity_set)
        self._audience.require(actor, resource)
        raws, next_url = self._page(company_id, entity_set, url, page_size, query)
        records, withheld = [], 0
        for raw in raws:
            record = self._record(company_id, entity_set, raw)
            if self._audience.item_visible(actor, resource, record["source_acl"]):
                records.append(record)
            else:
                withheld += 1
        return records, withheld, next_url

    def read_document_page(self, actor: Actor, company_id: str, header_set: str,
                           url: str | None, *, page_size: int = 100,
                           query: Mapping[str, str]
                           ) -> tuple[list[dict[str, Any]], int, str | None,
                                      dict[str, list[dict[str, Any]]]]:
        """One page of document headers with their lines in the same GET (``query`` from
        ``bc_query(header_set, ..., expand=<its line set>)``): (visible headers, withheld
        count, next URL, ``{header id: its line records}``).

        The audience must grant the header set and the line set before the request. A
        header's record never holds its lines (its fields are those of a plain listing).
        Its lines are returned only when the expansion is whole and clean: a list of
        records, no nested ``@odata.nextLink`` (BC paged them), each naming the header
        (``BC_LINE_PARENT_FIELD``), each a valid record and visible under both resources.
        Any other header is left out of the mapping, so its lines are read by the nested
        navigation (``list_lines``), which drops and withholds with counts."""
        line_set = grammar.BC_DOCUMENT_LINES.get(header_set)
        if line_set is None or not query or query.get("$expand") != line_set:
            raise ValueError("an expanded page needs $expand of the header's line set")
        company_id = _guid(company_id, "company id")
        resources = [self.resource(company_id, header_set),
                     self.resource(company_id, line_set)]
        for resource in resources:
            self._audience.require(actor, resource)
        raws, next_url = self._page(company_id, header_set, url, page_size, query)
        parent_field = grammar.BC_LINE_PARENT_FIELD.get(line_set, "documentId")
        records, withheld, lines = [], 0, {}
        for raw in raws:
            nested = raw.get(line_set)
            record = self._record(company_id, header_set, {
                k: v for k, v in raw.items()
                if not (isinstance(k, str) and (k == line_set or k.startswith(line_set + "@")))})
            if not self._audience.item_visible(actor, resources[0], record["source_acl"]):
                withheld += 1
                continue
            records.append(record)
            if (not isinstance(nested, list) or f"{line_set}@odata.nextLink" in raw
                    or not all(isinstance(line, Mapping) for line in nested)):
                continue
            try:
                rows = [self._record(company_id, line_set, line) for line in nested]
            except SourceUnavailable:
                continue
            if all(str(line.get(parent_field, "")).lower() == record["id"].lower()
                   for line in nested) and all(
                    self._audience.item_visible(actor, r, row["source_acl"])
                    for row in rows for r in resources):
                lines[record["id"]] = rows
        return records, withheld, next_url, lines

    def _page(self, company_id: str, entity_set: str, url: str | None, page_size: int,
              query: Mapping[str, str] | None) -> tuple[list[Mapping[str, Any]], str | None]:
        if isinstance(page_size, bool) or not isinstance(page_size, int) \
                or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be 1..1000")
        first = self.collection_url(company_id, entity_set, query)
        if url is None:
            url = first
        else:
            # The same check as a fresh nextLink, so a cursor stored by older code or
            # for another read is refused here, typed, never by the guard mid-run.
            url = self._api.continuation_url(first, url)
            if url is None:
                raise nextlink_refused(f"{self._api.source} nextLink escaped the "
                                       "collection (stored cursor)")
        return _shaped(lambda u: self._api.get_page(
            u, headers={"Prefer": f"odata.maxpagesize={page_size}"}), url)

    def get_entity(self, actor: Actor, company_id: str, entity_set: str,
                   record_id: str) -> dict[str, Any]:
        company_id = _guid(company_id, "company id")
        resource = self.resource(company_id, entity_set)
        self._audience.require(actor, resource)
        record_id = _guid(record_id, "record id")
        url = grammar.url("bc.entity", company=company_id, entity=entity_set,
                          record=record_id, **self._where)
        try:
            raw = _shaped(self._api.get_json, url)
        except SourceNotFound:
            raise NotVisible() from None
        key = grammar.BC_RECORD_KEY_FIELD.get(entity_set, "id")
        if not isinstance(raw, Mapping) or str(raw.get(key, "")).lower() != record_id:
            raise SourceUnavailable("BC read returned another identity")
        record = self._record(company_id, entity_set, raw)
        if not self._audience.item_visible(actor, resource, record["source_acl"]):
            raise NotVisible()
        return record

    def resolve_citation(self, handle: str, actor: Actor) -> dict[str, Any]:
        """Re-read a live ``bc://…@<etag>`` citation. A capture handle
        (``…@<generation>-<capture-id>``) is refused (``ValueError``): its tail is no
        etag, and it resolves offline through ``capture_store.CaptureStore``."""
        if is_capture_handle(handle):
            raise ValueError("capture handle: resolve it through the capture store")
        (environment, company_id, entity_set, record_id), etag, _ = parse_handle(handle, SCHEME, 4)
        if environment != self.environment.lower():
            raise NotVisible()
        current = self.get_entity(actor, company_id, entity_set, record_id)
        return {"citation": handle,
                "status": "current" if current["etag"] == etag else "changed_since",
                "cited_version": etag, "current_version": current["etag"],
                "current_citation": current["citation"], "record": current}


# Total backoff sleep per build_bc api (all GETs together); a single GET sleeps
# at most 2 x 4 s. Beyond the budget a failing GET raises at once.
RETRY_BUDGET_SECONDS = 30.0
# The full capture makes many more GETs than a probe; its backoff budget is larger.
CAPTURE_RETRY_BUDGET_SECONDS = 180.0


def build_bc(environ: Mapping[str, str] | None = None, *, sender=None,
             retry: BoundedBackoff | None = None
             ) -> tuple[SourceApi, GuardedTransport, str, str]:
    """``retry`` defaults to ``BoundedBackoff(budget_seconds=RETRY_BUDGET_SECONDS)``
    (real sleep); tests inject one with a recording sleep."""
    env = os.environ if environ is None else environ
    transport = GuardedTransport(sender, environ=env) if sender else GuardedTransport(environ=env)
    credentials = ClientCredentials.from_env("BC", env)
    environment = env.get("BC_ENVIRONMENT") or ""
    if _ENVIRONMENT.fullmatch(environment) is None:
        raise SourceUnavailable("BC_ENVIRONMENT missing or invalid")
    tokens = TokenProvider(credentials, BC_SCOPE, transport)
    if retry is None:
        retry = BoundedBackoff(budget_seconds=RETRY_BUDGET_SECONDS)
    api = SourceApi("business-central", tokens, transport, exact_integers=True, retry=retry)
    return api, transport, credentials.tenant_id, environment


# ------------------------------------------------------------------ versioned capture

# Entity sets the L1 capture keeps per company (10-DECISION 2; 09 lane L2): every BC
# entity set an ontology binding reads (ontology/schema/domains/*.json), so a BC
# projection can be complete on its own, and sales quotes (with their lines) for the
# quotes domain. Document headers with a line navigation also capture their lines, per
# document; the company record itself is a ``companies`` unit of its own (``BcCapture``).
# The setup sets (company information, locations, units of measure, item categories,
# exchange rates) are the masters the generated BC types bind (ontology/generator/bc.py:
# the generator binds exactly what this capture holds).
# The ledgers and the chart of accounts come last: a bounded call reaches the documents
# first. Payment state is read from the posted invoices themselves (``status``,
# ``remainingAmount``; the capture reads every field, never a ``$select``), with the
# aged receivable/payable balances per party and the payment journals: API v2.0's
# customerPayments / vendorPayments are unposted journal lines (lines of their journal)
# that leave the API once posted, and the Portal skips customer/vendor ledger entries as
# not published on api/v2.0 for these companies (plx-customer-portal@790256c
# inbound.ts:248-268, shouldSkipMissingPostedCollection;
# devloop/DECISIONS/2026-10-07-bc-finance-2-payment-ledger.md). A set a company does not expose
# (404 on its listing) fails its unit with reason ``not_exposed``: a gap, never empty.
CAPTURE_ENTITY_SETS = ("purchaseOrders", "purchaseReceipts", "purchaseInvoices", "salesOrders",
                       "salesInvoices", "salesShipments", "salesCreditMemos",
                       "purchaseCreditMemos", "salesQuotes", "customers", "vendors", "contacts",
                       "items", "companyInformation", "locations", "unitsOfMeasure",
                       "itemCategories", "currencyExchangeRates",
                       "customerPaymentJournals", "vendorPaymentJournals",
                       "agedAccountsReceivables", "agedAccountsPayables", "itemLedgerEntries",
                       "accounts", "generalLedgerEntries")
# Records read per capture call for unbounded journals; the run stays resumable, so
# successive calls continue from the cursor (completeness says in_progress/record_cap).
CAPTURE_RECORD_CAPS = {"itemLedgerEntries": 25000, "generalLedgerEntries": 25000}
# The posted invoice sets a capture roots on, first (a budget cut keeps them), and the
# plx.process graph whose evidence types name the rest (``bc_entity_sets``).
CAPTURE_ROOT_SETS = ("salesInvoices", "purchaseInvoices")
CAPTURE_PROCESS = "purchase_to_invoice"
COMPANY_SET = "companies"
# Records per listing page (``Prefer: odata.maxpagesize``). Live bc.capture_full at 100
# spent its budget on paging (manufacturing items: 157 pages for 15,646 records, the item
# ledger stopped on budget); the largest captured record averages ~2 KB, so a page of 500
# stays far below the 4 MB response bound.
CAPTURE_PAGE_SIZE = 500
# Headers per page when a page also carries their lines (``$expand``): one GET per page
# instead of one more per header. 100 documents with their lines stay far below the 4 MB
# response bound at the ~2 KB records above unless the page averages ~20 lines of each;
# a page over the bound falls back to the plain read (``BcCapture._capture_set``).
CAPTURE_EXPAND_PAGE_SIZE = 100
# Consecutive first-attempt line reads that fail before a lines unit pauses (the source
# looks down); a single header's failure stays pending and the unit goes on (``BcCapture``).
LINE_FAILURE_STREAK = 3
BOOKS_FILE = Path(__file__).with_name("bc_company_books.json")
BOOKS_SCHEMA = "plx-bc-company-books/v1"
_BOOK = re.compile(r"^[a-z][a-z0-9_]{0,46}$")
# A throttle whose Retry-After exceeds the backoff cap defers the unit at least this long.
THROTTLE_DEFAULT_SECONDS = 60.0
_THROTTLE_STATUSES = frozenset({429, 503})
_PERMANENT_STATUSES = frozenset({400, 401, 403})
def load_company_scope(source: str | Path | Mapping[str, Any] | None = None
                       ) -> tuple[frozenset[str], dict[str, dict[str, str]]]:
    """``(only_pinned environments, {environment: {company GUID: pinned name}})`` from the
    pinned map. In an ``only_pinned`` environment a listed company the map does not pin is
    out of scope by design (counted ``out_of_scope_companies``, never read), not a finding;
    a pinned GUID the listing shows under another name is not read (``pinned_name_mismatch``).
    The names are the company ``name`` BC lists (live: "My Company" for the Production
    manufacturing book)."""
    document = (source if isinstance(source, Mapping) else json.loads(
        Path(source or BOOKS_FILE).read_text(encoding="utf-8")))
    if document.get("schema") != BOOKS_SCHEMA:
        raise ValueError("bc company books document invalid")
    only = document.get("only_pinned", [])
    names = document.get("names", {})
    if (not isinstance(only, list) or not all(isinstance(e, str) for e in only)
            or not isinstance(names, Mapping)):
        raise ValueError("bc company scope invalid")
    out: dict[str, dict[str, str]] = {}
    for environment, pins in names.items():
        if not isinstance(environment, str) or not isinstance(pins, Mapping):
            raise ValueError("bc company scope invalid")
        entry = out.setdefault(environment.lower(), {})
        for company, name in pins.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("bc company scope invalid")
            entry[_guid(company, "company id")] = name
    return frozenset(e.lower() for e in only), out


def select_pinned(items: Sequence[Mapping[str, Any]], environment: str,
                  pinned: Mapping[str, str], *, only_pinned: bool = False,
                  names: Mapping[str, str] | None = None
                  ) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """The pinned companies of a company listing: (``[(company id, book)]``, counts).
    Counts: ``unpinned`` (not pinned, environment not ``only_pinned``), ``out_of_scope``
    (not pinned, environment ``only_pinned``), ``name_mismatch`` (pinned GUID listed under
    a name other than the pinned one: not selected, fail closed). A company is matched by
    GUID; the name only ever rules a pinned GUID out."""
    targets: list[tuple[str, str]] = []
    counts = {"unpinned": 0, "out_of_scope": 0, "name_mismatch": 0}
    for company in items:
        company_id = company.get("id")
        book = (pinned.get(company_id.lower()) if isinstance(company_id, str)
                and _GUID.fullmatch(company_id) else None)
        if book is None:
            counts["out_of_scope" if only_pinned else "unpinned"] += 1
            continue
        wanted = (names or {}).get(company_id.lower())
        if wanted is not None and company.get("name") != wanted:
            counts["name_mismatch"] += 1
            continue
        targets.append((company_id.lower(), book))
    return targets, counts


# BC codes of a 400 that says the page found nothing it needs in the company (the payment
# journal pages fail on a missing journal template): the company does not serve the set.
# The request is not at fault when another company served the same read (gap-1b49174b629f,
# directive 4b59246fac05: the daily d298b51 read internal_data_not_found_filter).
UNSERVED_CODES = frozenset({"internal_data_not_found_filter"})
_REFUSED_STATUSES = frozenset({401, 403})
# Reasons a header unit fails with when its company refuses or does not serve the set.
_HEADER_REFUSALS = frozenset({"source_refused", "not_exposed"})
# The later ask that carries the sets a company does not serve (a refused unit whose error is
# ``http_400:<UNSERVED_CODES>`` while another company served the set): listed, never complete.
UNSERVED_LATER_ASK = "bc-unserved-endpoints"
_UNSERVED_ERRORS = frozenset(f"http_400:{code}" for code in UNSERVED_CODES)


def load_company_books(source: str | Path | Mapping[str, Any] | None = None
                       ) -> dict[str, dict[str, str]]:
    """``{environment (lower): {company GUID (lower): book}}`` from the pinned map.

    Companies are matched by GUID only, never by name (Portal ``company-pins.ts``);
    a company the map does not pin is not captured and is counted as unpinned. A book is
    one company per environment: a book pinned to two (which one ``--expected-refusal``
    entry would then excuse both of) or a company pinned twice is refused."""
    document = source if isinstance(source, Mapping) else json.loads(
        Path(source or BOOKS_FILE).read_text(encoding="utf-8"))
    if document.get("schema") != BOOKS_SCHEMA or not isinstance(
            document.get("environments"), Mapping):
        raise ValueError("bc company books document invalid")
    books: dict[str, dict[str, str]] = {}
    for environment, pins in document["environments"].items():
        if not isinstance(environment, str) or _ENVIRONMENT.fullmatch(environment) is None \
                or not isinstance(pins, Mapping):
            raise ValueError("bc company books environment invalid")
        entry = books.setdefault(environment.lower(), {})
        for company, book in pins.items():
            if not isinstance(book, str) or _BOOK.fullmatch(book) is None:
                raise ValueError("bc company book invalid")
            company_id = _guid(company, "company id")
            if company_id in entry or book in entry.values():
                raise ValueError(f"bc company book {book!r} pinned twice in {environment}")
            entry[company_id] = book
    return books


def load_ledger_scope(source: str | Path | Mapping[str, Any] | None = None
                      ) -> dict[str, dict[str, Any]]:
    """``{environment (lower): scope}`` of ``bc.ledgers`` from the pinned map's ``ledgers``
    section: the ``required_book`` (the Workbench's BC book, Production / My Company), the
    ``required_sets`` it must serve with lines, and the ``expected_gaps``: companies (by
    GUID) whose named refusal of named sets is a reason-coded gap with an owner and a cause
    (directive 8a8b59c65f56). Anything malformed fails the load."""
    document = source if isinstance(source, Mapping) else json.loads(
        Path(source or BOOKS_FILE).read_text(encoding="utf-8"))
    section = document.get("ledgers", {})
    if not isinstance(section, Mapping):
        raise ValueError("bc ledger scope invalid")
    out: dict[str, dict[str, Any]] = {}
    for environment, scope in section.items():
        if not isinstance(environment, str) or not isinstance(scope, Mapping):
            raise ValueError("bc ledger scope invalid")
        book, sets = scope.get("required_book"), scope.get("required_sets", [])
        if (not isinstance(book, str) or _BOOK.fullmatch(book) is None
                or not isinstance(sets, list) or not sets
                or any(n not in FINANCE_ENTITY_SETS for n in sets)):
            raise ValueError("bc ledger scope invalid")
        gaps = []
        for gap in scope.get("expected_gaps", []):
            if (not isinstance(gap, Mapping) or gap.get("http_status") not in _PERMANENT_STATUSES
                    or not isinstance(gap.get("sets"), list) or not gap["sets"]
                    or any(n not in FINANCE_ENTITY_SETS for n in gap["sets"])
                    or not is_error_code_token(gap.get("error_code"))
                    or gap.get("error_code") == "other"
                    or gap.get("message_class") not in MESSAGE_CLASS_TOKENS
                    or any(not isinstance(gap.get(k), str) or not gap[k].strip()
                           for k in ("label", "owner", "cause"))):
                raise ValueError("bc ledger scope invalid")
            gaps.append({**gap, "company": _guid(gap.get("company"), "company id")})
        if len({g["company"] for g in gaps}) != len(gaps):
            raise ValueError("bc ledger scope invalid")
        out[environment.lower()] = {"required_book": book, "required_sets": list(sets),
                                    "expected_gaps": gaps}
    return out


# The start of the verifier session a check runs in (devloop/live_verify.py sets it): a
# consumer of the session's capture store reuses only what that session completed
# (``BcCapture.capture(fresh_since=)``). Unset outside a session: nothing is reused.
FRESH_SINCE_ENV = "PLX_CAPTURE_FRESH_SINCE"


def session_fresh_since(environ: Mapping[str, str] | None = None) -> str | None:
    """``FRESH_SINCE_ENV`` of ``environ`` (default the process's), else None."""
    return (os.environ if environ is None else environ).get(FRESH_SINCE_ENV) or None


# Incremental capture (devloop/DECISIONS/2026-10-02-bc-capture-incremental.md).
# ``mode="full"`` reads every unit in full each call (and marks records a complete
# traversal did not see as missing). ``mode="auto"`` schedules per unit (company x
# entity set): a set with ``lastModifiedDateTime`` (``grammar.BC_MODIFIED_SINCE_SETS``)
# reads only records modified since its watermark, and a key-only full pass runs at
# least every ``FULL_PASS_INTERVAL_DAYS`` (daily after a pass found a missing key).
CAPTURE_MODES = ("full", "auto")
WATERMARK_OVERLAP = timedelta(minutes=10)
FULL_PASS_INTERVAL_DAYS = 7
FULL_PASS_INTERVAL_AFTER_MISSING_DAYS = 1
# Posted documents BC corrects with a separate document, never by editing the posted
# one (decision rule 3): an unchanged header version of these keeps its lines. Posted
# receipts and shipments are not here: Undo Receipt/Shipment adds correction lines to
# the posted document itself. Statuses from BC API v2.0 salesInvoice/purchaseInvoice
# (``Draft`` and ``In Review`` are unposted). UNVERIFIED live for credit memos.
_POSTED_STATUSES = frozenset({"Open", "Paid", "Canceled", "Corrective"})
POSTED_DOCUMENT_STATUSES: dict[str, frozenset[str]] = {
    "purchaseInvoices": _POSTED_STATUSES, "salesInvoices": _POSTED_STATUSES,
    "purchaseCreditMemos": _POSTED_STATUSES, "salesCreditMemos": _POSTED_STATUSES}
# A finished delta run: the store keeps it ``partial`` with this reason (it did not
# traverse the unit, so it must not mark unseen records missing); the capture report
# counts it complete for its mode.
INCREMENTAL = "incremental"
# Run marks (committed with the step they describe): the run's plan, and signals the
# decision names as "how we will know this was wrong".
PLAN_MARK = "plan:"
SIGNAL_CHANGED_BELOW_WATERMARK = "signal:changed_below_watermark"
SIGNAL_LINE_SKIP_CHANGED = "signal:line_skip_changed"
LINES_SKIPPED_MARK = "signal:lines_skipped"
# A header version whose lines read withheld, dropped or truncated a line (or whose
# header vanished): committed with that header's mark, it disqualifies the skip.
LINES_INCOMPLETE_MARK = "incomplete:"
# ``LINES_FAILED_MARK + <header mark>``: that header version's line read failed in this
# run. The header stays unread (pending): the run pauses ``line_reads_failed`` (with the
# error class) and its next call retries it once, after the headers it has not tried yet.
LINES_FAILED_MARK = "line_read_failed:"
# ``LINES_EXHAUSTED_MARK + <header mark>``: the retry failed too. The header is counted
# dropped and the run finishes ``partial`` / ``line_reads_failed`` (error class kept in a
# ``LINES_ERROR_MARK`` mark), so the next run starts a fresh full pass of the unit and
# lines under unchanged headers are read again: a document that always fails never
# freezes the unit.
LINES_EXHAUSTED_MARK = "line_read_exhausted:"
LINES_ERROR_MARK = "line_error:"
# Committed with a header mark: the document is gone (its lines and the header itself
# answer 404), so its lines are complete as nothing, never a pending failure.
LINES_GONE_MARK = "lines_gone:"
# Document line sets the reader can also list filtered by ``documentId`` (the fallback a
# failed nested navigation of one document gets, once, before it counts as failed).
_FLAT_LINE_SETS = frozenset(line for line in grammar.BC_DOCUMENT_LINES.values()
                            if "documentId" in getattr(grammar.BC_QUERY_SPECS.get(line),
                                                       "filters", {}))
# Per committed page: the latest ``lastModifiedDateTime`` it held ("none" when a
# record had none or was withheld). The next run's watermark comes from these, never
# from a scan of the store.
SEEN_MAX_MARK = "seen_max:"
# Consecutive clean full passes (no change below the watermark) that lift
# ``incremental_disabled``.
CLEAN_FULL_PASSES_TO_RESUME = 2
_RESOLVE_BATCH = 1000
_BC_INSTANT = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
                         r"(?:\.([0-9]{1,7}))?(?:Z|\+00:00)$")
# A watermark before this is BC's blank date (0001-01-01), not a modification time.
_WATERMARK_FLOOR = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _instant(value: Any) -> datetime | None:
    """A BC/store UTC timestamp as an aware datetime, else None."""
    found = _BC_INSTANT.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        return None
    try:
        moment = datetime.strptime(found[1], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    fraction = (found[2] or "")[:6].ljust(6, "0")
    return moment.replace(microsecond=int(fraction), tzinfo=timezone.utc)


def _bc_instant(moment: datetime) -> str:
    """The grammar's fixed-width instant (milliseconds, floored)."""
    text = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{text}.{moment.microsecond // 1000:03d}Z"


def _detail(run: Mapping[str, Any] | None) -> dict[str, Any] | None:
    text = None if run is None else run.get("detail")
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _settled(run: Mapping[str, Any] | None) -> bool:
    """A run that finished its mode's traversal (a full pass or a clean delta read)."""
    return run is not None and (run["state"] == "complete" or (
        run["state"] == "partial" and run.get("reason") == INCREMENTAL))


_EXPANDABLE: frozenset[str] | None = None


def expandable_sets() -> frozenset[str]:
    """Document header sets whose lines a header page may ``$expand``: the pairs of
    ``grammar.BC_DOCUMENT_LINES`` whose header the pinned ``$metadata``
    (``ontology/schema/generated/meta/bc_metadata.json``) declares with that line set as a
    contained collection navigation. A pair the pin does not declare so is read per
    header by the nested navigation."""
    global _EXPANDABLE
    if _EXPANDABLE is None:
        from ontology.generator.bc import load_pin
        sets = load_pin()["entity_sets"]
        _EXPANDABLE = frozenset(
            header for header, line_set in grammar.BC_DOCUMENT_LINES.items()
            if any(nav.get("name") == line_set and nav.get("collection") is True
                   and nav.get("contained") is True
                   for nav in (sets.get(header) or {}).get("navigation", [])))
    return _EXPANDABLE


def _expand_refused(exc: SourceUnavailable) -> bool:
    """A failed expanded page the plain read may not hit: BC refused the query (400), the
    page passed the response bound, or its nextLink did not keep ``$expand`` (refused)."""
    return (getattr(exc, "http_status", None) == 400 or is_nextlink_refusal(exc)
            or unavailable_detail(exc) == "response_too_large")


def _default_plan() -> dict[str, Any]:
    return {"mode": "full", "keys_only": False, "since": None, "watermark": None,
            "missing_before": 0, "prior_run": None, "last_full_at": None,
            "interval_days": FULL_PASS_INTERVAL_DAYS, "incremental_disabled": False,
            "clean_full_passes": 0, "line_skip_disabled": False, "full_reason": None,
            "expand": None}


class BcCapture:
    """Versioned, resumable L1 capture of BC entity sets into a ``CaptureStore``.

    Every read goes through ``BusinessCentralReader`` (GET only, audience checked
    before each request). Each page is committed with its next-page cursor, so an
    interrupted capture resumes where it stopped. Bounded retry is the reader's
    ``BoundedBackoff``; a GET that still fails pauses the unit (cursor kept) and
    the capture goes on with the next unit, except a throttle (429/503), which
    defers the unit until ``Retry-After`` (at least ``THROTTLE_DEFAULT_SECONDS``)
    and ends the call. 400/401/403/404 on a fresh listing mark the unit failed;
    a 400 on a resumed cursor (an expired continuation) restarts it once. A
    refused continuation (a nextLink, or a stored cursor, that is not the same
    collection and query, or one the guard refuses) fails the unit
    ``nextlink_refused`` and drops the cursor: the next run restarts it from its
    first page, and later units go on. A nextLink back to a page the unit already
    read in this call (a paging cycle) is refused the same way. A refused continuation of one document's
    lines marks that document's lines incomplete (counted as ``nextlink_refused``
    in the lines unit's report) and the next document goes on.
    Completeness is reported per unit and is never claimed for a partial read.

    Modes (``capture(mode=...)``; a resumed run keeps the plan it started with,
    stored as a run mark and as the finished run's ``detail``):

    * ``full``: every unit is read in full (content), as a complete traversal.
    * ``auto``: per unit, the first capture is a full content read. A set that
      declares calculated fields (``grammar.BC_CALCULATED_FIELDS``: balances,
      inventory, document totals and status, ledger cost amounts) stays on full
      content reads: those values move without the etag or ``lastModifiedDateTime``.
      Later runs of any other set with ``lastModifiedDateTime`` read
      ``lastModifiedDateTime ge <since>``, ``since`` = the latest
      ``lastModifiedDateTime`` the store holds for the unit (from the per-page
      ``SEEN_MAX_MARK`` marks, no store scan) minus ``WATERMARK_OVERLAP``, used only
      after a settled run, so a failed or partial read never leads to a delta. A delta run finishes ``partial`` /
      ``incremental`` in the store: it never marks unseen records missing. Every
      ``FULL_PASS_INTERVAL_DAYS`` (1 day after a pass found a new missing key), and
      after any unsettled run, the unit gets a key-only full pass
      (``$select=id,lastModifiedDateTime``): a key whose etag and
      ``lastModifiedDateTime`` equal the stored current version confirms that
      version (re-saved unchanged: same digest, no new version), any other key is
      read by id, and the complete traversal marks keys BC no longer lists
      ``missing_from_source_since``. A full pass that finds a changed or new record
      older than the watermark marks the unit's incremental reads unreliable
      (``SIGNAL_CHANGED_BELOW_WATERMARK``: full passes until
      ``CLEAN_FULL_PASSES_TO_RESUME`` consecutive clean ones). A 400 on a get-by-id
      in that pass fails the key (the unit does not settle); it never restarts the
      run.
    * Lines: a header's lines are re-read unless (auto mode, between the lines
      unit's own weekly full passes) the header is a posted invoice or credit memo
      (``POSTED_DOCUMENT_STATUSES``) and the same header version's lines were read
      cleanly (nothing withheld, dropped or truncated) or skipped by the previous
      lines run. A full pass that re-reads such lines
      and finds them changed stops the skip for the unit
      (``SIGNAL_LINE_SKIP_CHANGED``)."""

    def __init__(self, reader: BusinessCentralReader, store: CaptureStore, *,
                 books: Mapping[str, Mapping[str, str]] | None = None,
                 names: Mapping[str, Mapping[str, str]] | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.reader = reader
        self.store = store
        # The shipped map also scopes the environment (only its pinned companies are read) and
        # names them; an injected map is exactly what the caller pinned, no more. ``names``
        # ({environment: {company GUID: name}}) pins the names of an injected map: the
        # listing must show them, and a named company's purchase-order field page
        # (``_capture_references``) is read by that name.
        self.only_pinned, self.names = (load_company_scope() if books is None
                                        else (frozenset(), {}))
        if names is not None:
            self.names = {str(env).lower(): {_guid(c, "company id"): n for c, n in pins.items()}
                          for env, pins in names.items()}
        self.books = load_company_books() if books is None else books
        self._monotonic = monotonic
        self._deadline: float | None = None
        # the end of the unit's share of the budget (the roots phase of ``capture``)
        self._share_end: float | None = None
        self._share_cut = False
        self._stopped: str | None = None
        self._throttle_status: int | None = None
        self._full_pass_due = False
        self._fresh_since: datetime | None = None

    def _out_of_time(self, run_id: int) -> bool:
        """Past the call's budget: pause the run (cursor kept) and stop the call. Past the
        unit's share of it (``_share_end``): pause the run alone; the call goes on."""
        if self._deadline is None:
            return False
        now = self._monotonic()
        if now < self._deadline and (self._share_end is None or now < self._share_end):
            return False
        self.store.pause_run(run_id, reason="budget")
        if now >= self._deadline:
            self._stopped = "budget"
        else:
            self._share_cut = True
        return True

    def capture(self, actor: Actor, *, entity_sets: Sequence[str] = CAPTURE_ENTITY_SETS,
                page_size: int = CAPTURE_PAGE_SIZE, budget_seconds: float | None = None,
                record_caps: Mapping[str, int] | None = None,
                mode: str = "full", full_pass_due: bool = False,
                roots: Sequence[str] = CAPTURE_ROOT_SETS,
                fresh_since: str | None = None) -> dict[str, Any]:
        """Capture (or resume) every pinned company's entity sets for ``actor``.
        ``full_pass_due`` (auto mode) makes every new unit run the key-only full pass
        now instead of on its schedule (the live check exercises that form). The
        ``roots`` among ``entity_sets`` are read in every company before any line or
        other set, so a budget stop keeps them; under a budget each root unit reads within
        an even share of what is left (one large root set never starves the later
        companies') and a unit its share cut resumes on what the others left.
        ``fresh_since`` (a UTC instant, the start of the verifier session whose store this
        is: ``FRESH_SINCE_ENV``): a unit whose latest run began at or after it and finished
        ``complete`` is reported from the store (``reused``), not read again, so each check
        sharing the session's store tops it up (a paused unit resumes and the rest are
        read). A unit the store holds complete from before (an earlier invocation) is only
        a starting point: it is read again like any other, so a change or deletion since is
        seen. Without ``fresh_since`` nothing is reused. A complete unit reports
        ``capture_age_seconds`` (since its run began), the report the oldest of them. A unit
        a run completed that began before ``fresh_since`` (paused in an earlier invocation,
        resumed now) holds pages read before the session: it is reported ``stale``
        (``units_stale``) and the call is never ``all_complete``; the session's next call
        reads it again."""
        if mode not in CAPTURE_MODES:
            raise ValueError("mode must be full or auto")
        since = None if fresh_since is None else _instant(fresh_since)
        if fresh_since is not None and since is None:
            raise ValueError("fresh_since must be a UTC instant")
        self._full_pass_due = bool(full_pass_due)
        self._fresh_since = since
        for name in entity_sets:
            _entity_set(name)
        caps = dict(CAPTURE_RECORD_CAPS if record_caps is None else record_caps)
        self._deadline = (None if budget_seconds is None
                          else self._monotonic() + float(budget_seconds))
        self._stopped = self._throttle_status = None
        environment = self.reader.environment.lower()
        # One store holds one BC tenant/environment: another one is refused before
        # any read (binding_mismatch).
        self.store.bind_source("bc", f"{self.reader.tenant_id.lower()}/{environment}")
        pinned = self.books.get(environment, {})
        try:
            listing = self.reader.companies(actor)
        except SourceUnavailable as exc:
            # A throttled listing (429/503 after the reader's retry) stops the call before
            # any company, as a throttle does mid-capture: a throttle block, never a failure.
            status = getattr(exc, "http_status", None)
            if status not in _THROTTLE_STATUSES:
                raise
            self._stopped, self._throttle_status = "throttled", status
            listing = {"items": [], "completeness": {"complete": False}}
        report: dict[str, Any] = {"source": "business-central",
                                  "evidence_label": self.reader.evidence_label,
                                  "environment_is_production": environment == "production",
                                  "mode": mode,
                                  "companies_listed": len(listing["items"]),
                                  "companies_listing_complete":
                                      bool(listing["completeness"].get("complete")),
                                  "unpinned_companies": 0, "companies": [],
                                  "stopped_reason": None, "atomic_upstream_snapshot": False,
                                  "deletions_proven": False}
        listed = {c["id"].lower() for c in listing["items"]
                  if isinstance(c.get("id"), str)}
        targets, counts = select_pinned(
            listing["items"], environment, pinned, only_pinned=environment in self.only_pinned,
            names=self.names.get(environment))
        report["unpinned_companies"] = counts["unpinned"]
        report["out_of_scope_companies"] = counts["out_of_scope"]
        report["pinned_name_mismatch"] = counts["name_mismatch"]
        # A pinned company the listing did not show (a truncated or paged listing, or a
        # company BC no longer lists) is never captured: the run is not complete.
        report["pinned_companies_missing"] = len(set(pinned) - listed)
        entries = []
        for company_id, book in targets:
            entry = {"company_id": company_id, "book": book, "lineage": f"bc_{book}",
                     "units": []}
            report["companies"].append(entry)
            entries.append((company_id, entry))
        # The root document sets of every company first, then their lines and the other
        # sets: a budget cut keeps every company's roots, never one company's lines.
        root_sets = [s for s in entity_sets if s in roots]
        self._capture_roots(actor, [(company_id, entry, entity_set)
                                    for company_id, entry in entries
                                    for entity_set in root_sets],
                            page_size, caps, mode)
        for company_id, entry in entries:
            for entity_set in entity_sets:
                if entity_set not in root_sets:
                    self._counted(entry, self._capture_set, actor, company_id, entity_set,
                                  entry["lineage"], page_size, caps.get(entity_set), mode)
                if entity_set in grammar.BC_DOCUMENT_LINES:
                    self._counted(entry, self._capture_lines, actor, company_id, entity_set,
                                  entry["lineage"], mode)
            if self._references_planned(environment, company_id, entity_sets):
                self._counted(entry, self._capture_references, actor, company_id,
                              self.names[environment][company_id], entry["lineage"],
                              REFERENCE_PAGE_SIZE)
            if self._stopped is None:  # from the listing already read: no request
                entry["units"].append(self._capture_company(
                    actor, company_id, listing["items"], entry["lineage"]))
        report["stopped_reason"] = self._stopped
        self._deadline = None
        units = [u for c in report["companies"] for u in c["units"]]
        # Every planned unit (per pinned company: each set, each set's lines and the
        # company record, plus the purchase-order field page of a named company), whether
        # or not the call reached it before a stop.
        report["units_total"] = len(entries) * (
            len(entity_sets) + sum(1 for s in entity_sets if s in grammar.BC_DOCUMENT_LINES)
            + 1) + sum(1 for company_id, _entry in entries
                       if self._references_planned(environment, company_id, entity_sets))
        report["units_reached"] = len(units)
        report["units_complete"] = sum(1 for u in units if u.get("complete"))
        unserved = self._mark_unserved(report["companies"])
        report["units_unserved"] = len(unserved)
        report["unserved_units"] = unserved
        report["units_not_exposed"] = sum(1 for u in units if u.get("reason") == "not_exposed")
        report["units_reused"] = sum(1 for u in units if u.get("reused"))
        report["units_stale"] = sum(1 for u in units if u.get("stale"))
        ages = [u["capture_age_seconds"] for u in units
                if u.get("capture_age_seconds") is not None]
        report["capture_age_seconds"] = max(ages) if ages else None
        report["throttle"] = (None if self._stopped != "throttled"
                              else self._throttle_report(report["companies"], entity_sets,
                                                         listed=bool(listing["items"])))
        report["company_proof"] = self._company_proof(report["companies"], listing["items"])
        report["all_complete"] = (bool(units)
                                  and report["units_complete"] + report["units_unserved"]
                                  == report["units_total"]
                                  and report["units_stale"] == 0
                                  and report["stopped_reason"] is None
                                  and report["unpinned_companies"] == 0
                                  and report["pinned_name_mismatch"] == 0
                                  and report["companies_listing_complete"]
                                  and report["pinned_companies_missing"] == 0)
        return report

    def _company_proof(self, companies: Sequence[Mapping[str, Any]],
                       listed: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        """The counts-only record that the call read one company: ``environment``,
        ``company_name`` (as BC lists it) and ``po_count`` (the purchase orders the capture holds
        for it, None until that unit is complete), nothing else: no PO number, amount or id.
        None unless exactly one company was captured."""
        if len(companies) != 1:
            return None
        entry = companies[0]
        row = next((r for r in listed
                    if str(r.get("id", "")).lower() == entry["company_id"]), {})
        unit = next((u for u in entry["units"] if u.get("entity_set") == "purchaseOrders"), {})
        count = unit.get("records_current") if unit.get("complete") else None
        return {"environment": self.reader.environment, "company_name": row.get("name"),
                "po_count": count}

    @staticmethod
    def _mark_unserved(companies: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Settle each unit as ``accounted`` (complete, or unserved) and return the unserved
        ones. A unit refused with a 400 whose BC code is in ``UNSERVED_CODES`` while another
        company completed the same set in this call is unserved (``later_ask``
        ``UNSERVED_LATER_ASK``): the request is valid there, so the company does not serve the
        set. It stays ``failed`` / ``source_refused``, is listed and never counted complete.
        A set no company served, a 403 or another code is not."""
        served = {u["entity_set"] for c in companies for u in c["units"] if u.get("complete")}
        unserved = []
        for index, company in enumerate(companies):
            for unit in company["units"]:
                is_unserved = (unit.get("state") == "failed"
                               and unit.get("reason") == "source_refused"
                               and unit.get("error") in _UNSERVED_ERRORS
                               and unit["entity_set"] in served)
                if is_unserved:
                    unit["later_ask"] = UNSERVED_LATER_ASK
                    unserved.append({"company_index": index,
                                     "book": company["book"], "entity_set": unit["entity_set"],
                                     "later_ask": UNSERVED_LATER_ASK})
                unit["accounted"] = bool(unit.get("complete") or is_unserved)
        return unserved

    def _counted(self, entry: dict[str, Any] | None, read: Callable[..., dict[str, Any]],
                 *args: Any) -> dict[str, Any] | None:
        """Read one unit (``read(*args)``) unless the call has stopped; its report, with
        the requests it sent, is added to the company's units (``entry``) and returned."""
        if self._stopped is not None:
            return None
        sent = self.reader.requests_sent
        unit = read(*args)
        unit["requests"] = self.reader.requests_sent - sent
        if entry is not None:
            entry["units"].append(unit)
        return unit

    def _capture_roots(self, actor: Actor, units: Sequence[tuple[str, dict[str, Any], str]],
                       page_size: int, caps: Mapping[str, int], mode: str) -> None:
        """The root units (company, its report entry, root set), in order. Under a budget
        each reads within an even share of what the call has left, so a unit that ends
        early leaves the rest to the later ones and a large one stops at its share end
        (paused, cursor kept); then each unit its share cut resumes on what is left."""
        reads: list[tuple[dict[str, Any] | None, bool]] = []
        for index, (company_id, entry, entity_set) in enumerate(units):
            if self._deadline is not None:
                now = self._monotonic()
                self._share_end = now + (self._deadline - now) / (len(units) - index)
            self._share_cut = False
            reads.append((self._counted(None, self._capture_set, actor, company_id,
                                        entity_set, entry["lineage"], page_size,
                                        caps.get(entity_set), mode), self._share_cut))
        self._share_end = None
        for (company_id, entry, entity_set), (unit, cut) in zip(units, reads):
            again = self._counted(None, self._capture_set, actor, company_id, entity_set,
                                  entry["lineage"], page_size, caps.get(entity_set),
                                  mode) if cut else None
            if unit is not None and again is not None:
                unit = {**again, "requests": unit["requests"] + again["requests"]}
            if unit is not None:
                entry["units"].append(unit)

    def _throttle_report(self, companies: Sequence[Mapping[str, Any]],
                         entity_sets: Sequence[str], *, listed: bool = True
                         ) -> dict[str, Any]:
        """What a throttle (429/503) left unread: its HTTP status, the entity sets (and
        line sets) of any pinned company paused by it or never reached after it, and the
        units it paused. A throttled company listing (``listed`` false) left every set of
        every pinned company unread, and says so (``companies_listing``: the pinned
        companies the report misses are the throttle's)."""
        wanted = {s for name in entity_sets
                  for s in (name, grammar.BC_DOCUMENT_LINES.get(name)) if s}
        unread: set[str] = set() if listed else set(wanted)
        paused = 0
        for company in companies:
            unread |= wanted - {u["entity_set"] for u in company["units"]}
            for unit in company["units"]:
                if unit.get("reason") == "throttled" and not unit.get("complete"):
                    unread.add(unit["entity_set"])
                    paused += 1
        return {"http_status": self._throttle_status, "entity_sets": sorted(unread),
                "units": paused, **({} if listed else {"companies_listing": True})}

    # ---------------------------------------------------------------- plans

    def _now(self) -> datetime:
        moment = _instant(self.store.now())
        if moment is None:
            raise CaptureError("clock_invalid", "store clock text is not a UTC instant")
        return moment

    @staticmethod
    def _plan_mark(plan: Mapping[str, Any]) -> str:
        return PLAN_MARK + json.dumps(dict(plan), sort_keys=True, separators=(",", ":"))

    def _resumed_plan(self, run_id: int) -> tuple[dict[str, Any], bool]:
        """The plan an open run started with, or (no plan mark: a run begun before
        its first commit, or by older code) a full content read."""
        for mark in self.store.run_marks(run_id):
            if mark.startswith(PLAN_MARK):
                try:
                    plan = json.loads(mark[len(PLAN_MARK):])
                except ValueError:
                    break
                if isinstance(plan, dict):
                    return {**_default_plan(), **plan}, True
        return _default_plan(), False

    def _missing(self, actor: Actor, unit: str) -> int:
        try:
            report = self.store.operator_completeness(actor, "bc", unit)
            return int(report["records_missing"] or 0)
        except CaptureError:
            return 0

    def _carry(self, actor: Actor, unit: str, prior: Mapping[str, Any] | None
               ) -> dict[str, Any]:
        """What a new run inherits from the unit's previous (finished) run."""
        carry = {k: _default_plan()[k] for k in ("last_full_at", "interval_days",
                                                 "incremental_disabled",
                                                 "clean_full_passes",
                                                 "line_skip_disabled")}
        if prior is None:
            return carry
        plan = _detail(prior)
        if plan is None:  # a run written before plans existed: a full content read
            if prior["state"] == "complete":
                carry["last_full_at"] = prior["finished_at"]
            return carry
        for key in carry:
            if key in plan:
                carry[key] = plan[key]
        marks = self.store.run_marks(prior["run_id"])
        if SIGNAL_LINE_SKIP_CHANGED in marks:
            carry["line_skip_disabled"] = True
        if SIGNAL_CHANGED_BELOW_WATERMARK in marks:
            carry["incremental_disabled"], carry["clean_full_passes"] = True, 0
        elif plan.get("mode") == "full" and prior["state"] == "complete":
            carry["clean_full_passes"] = int(carry["clean_full_passes"] or 0) + 1
            if (carry["incremental_disabled"]
                    and carry["clean_full_passes"] >= CLEAN_FULL_PASSES_TO_RESUME):
                # One missed change (e.g. a record modified while a long run paged past
                # it) must not keep the unit on full reads for good.
                carry["incremental_disabled"], carry["clean_full_passes"] = False, 0
        if plan.get("mode") == "full" and prior["state"] == "complete":
            carry["last_full_at"] = prior["finished_at"]
            found = self._missing(actor, unit) > int(plan.get("missing_before") or 0)
            carry["interval_days"] = (FULL_PASS_INTERVAL_AFTER_MISSING_DAYS if found
                                      else FULL_PASS_INTERVAL_DAYS)
        return carry

    def _watermark(self, prior: Mapping[str, Any] | None) -> datetime | None:
        """The latest ``lastModifiedDateTime`` the store holds for the unit, from the
        ``SEEN_MAX_MARK`` marks of the previous run (a complete full pass saw every
        record; any other run adds to the watermark it started from). None when a
        record had none or was withheld (the field is not reliable there)."""
        plan = _detail(prior)
        if prior is None or plan is None:
            return None
        values: list[Any] = [m[len(SEEN_MAX_MARK):]
                             for m in self.store.run_marks(prior["run_id"])
                             if m.startswith(SEEN_MAX_MARK)]
        if not (plan.get("mode") == "full" and prior["state"] == "complete"):
            values.append(plan.get("watermark"))
        moments = [_instant(v) for v in values]
        if not moments or any(m is None for m in moments):
            return None
        best = max(m for m in moments if m is not None)
        return best if best >= _WATERMARK_FLOOR else None

    def _seen_max(self, records: Sequence[Mapping[str, Any]], withheld: int) -> list[str]:
        if not records and not withheld:
            return []
        moments = [_instant(r.get("last_modified")) for r in records]
        if withheld or any(m is None for m in moments):
            return [SEEN_MAX_MARK + "none"]
        return [SEEN_MAX_MARK + _bc_instant(max(m for m in moments if m is not None))]

    def _new_plan(self, actor: Actor, unit: str, entity_set: str, mode: str,
                  prior: Mapping[str, Any] | None) -> dict[str, Any]:
        plan = {**_default_plan(), **self._carry(actor, unit, prior),
                "prior_run": None if prior is None else prior["run_id"]}
        if prior is None:
            return plan
        plan["missing_before"] = self._missing(actor, unit)
        if mode != "auto" or entity_set not in grammar.BC_MODIFIED_SINCE_SETS:
            return plan
        if grammar.bc_has_calculated_fields(entity_set):
            # Calculated values move without the etag or lastModifiedDateTime: only a
            # content read sees them, so no delta and no key-only confirmation.
            plan["full_reason"] = "calculated_fields"
            return plan
        watermark = self._watermark(prior)
        plan["watermark"] = None if watermark is None else _bc_instant(watermark)
        last_full = _instant(plan["last_full_at"])
        if last_full is None:
            return plan  # no full pass yet: a full content read sets the baseline
        plan["keys_only"] = True
        due = (self._full_pass_due
               or self._now() - last_full >= timedelta(days=int(plan["interval_days"])))
        if (not due and _settled(prior) and watermark is not None
                and not plan["incremental_disabled"]):
            plan.update(mode=INCREMENTAL, keys_only=False,
                        since=_bc_instant(watermark - WATERMARK_OVERLAP))
        return plan

    # ---------------------------------------------------------------- units

    def _unit_report(self, actor: Actor, unit: str, entity_set: str, *,
                     plan: Mapping[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        report = self.store.operator_completeness(actor, "bc", unit)
        result = {"entity_set": entity_set, **report, **extra}
        result["complete"] = (report["state"] == "complete"
                              or (report["state"] == "partial"
                                  and report["reason"] == INCREMENTAL))
        result["rows_changed"] = report["new_versions"]
        began = _instant(report["started_at"])
        result["capture_age_seconds"] = (round((self._now() - began).total_seconds(), 3)
                                         if result["complete"] and began is not None else None)
        result["stale"] = (self._fresh_since is not None and result["complete"]
                           and (began is None or began < self._fresh_since))
        if plan is not None:
            full_now = plan["mode"] == "full" and report["state"] == "complete"
            result.update({"mode": plan["mode"], "keys_only": bool(plan.get("keys_only")),
                           "since": plan.get("since"), "full_reason": plan.get("full_reason"),
                           "last_full_pass_at": report["finished_at"] if full_now
                           else plan.get("last_full_at")})
        return result

    def _reusable(self, unit: str) -> bool:
        """This session completed the unit: its latest run began at or after
        ``capture(fresh_since=)`` and finished ``complete``."""
        latest = self.store.run("bc", unit) if self._fresh_since is not None else None
        began = None if latest is None else _instant(latest["started_at"])
        return (latest is not None and latest["state"] == "complete" and began is not None
                and began >= self._fresh_since)

    def _deferred(self, run: Mapping[str, Any]) -> bool:
        until = run.get("retry_not_before")
        return isinstance(until, str) and self.store.now() < until

    def _failure(self, run_id: int, exc: Exception, *, resumed: bool,
                 plan: Mapping[str, Any] | None = None, label: str | None = None) -> str:
        """Pause or fail a unit after a refused GET; a throttle also stops the call.
        Returns ``throttled``, ``failed`` or ``paused``.

        A refused continuation (``is_nextlink_refusal``: a nextLink or stored cursor
        that is not the same collection and query) fails the unit with reason
        ``nextlink_refused``; ``finish_run`` drops the cursor, so the next run
        restarts the unit from its first page and the refused link is never sent."""
        status = getattr(exc, "http_status", None)
        explicit = label is not None
        label = label or _error_label(exc)
        if is_nextlink_refusal(exc):
            self.store.finish_run(run_id, "failed", reason="nextlink_refused", error=label,
                                  detail=plan)
            return "failed"
        if status in _THROTTLE_STATUSES:
            wait = max(THROTTLE_DEFAULT_SECONDS, getattr(exc, "retry_after_seconds", None) or 0)
            self.store.pause_run(run_id, reason="throttled", error=label,
                                 retry_not_before=self.store.later(wait))
            self._stopped, self._throttle_status = "throttled", status
            return "throttled"
        if isinstance(exc, SourceNotFound) or (status in _PERMANENT_STATUSES and not resumed):
            # A 404 on a unit's first page: the company does not expose this collection,
            # a typed gap. A 404 later (a continuation, a line read) is a refusal.
            reason = ("not_exposed" if isinstance(exc, SourceNotFound) and not resumed
                      else "source_refused")
            # The class carries BC's error code, which tells an unserved set (``_mark_unserved``).
            self.store.finish_run(run_id, "failed", reason=reason,
                                  error=label if explicit else _error_class(exc), detail=plan)
            return "failed"
        self.store.pause_run(run_id, reason="source_unavailable", error=label)
        return "paused"

    def _finish(self, run_id: int, unit: str, plan: Mapping[str, Any], *,
                traversed: bool) -> None:
        """``traversed``: the run saw every record of the unit (a full pass)."""
        if traversed:
            self.store.finish_run(run_id, "complete", detail=plan)
            return
        run = self.store.run("bc", unit) or {}
        reason = "withheld_or_dropped" if run.get("withheld") or run.get("dropped") \
            else INCREMENTAL
        self.store.finish_run(run_id, "partial", reason=reason, detail=plan)

    def _begin(self, actor: Actor, unit: str, entity_set: str, resources: Sequence[str],
               mode: str) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        """(run, plan, marks still to commit with the run's first step)."""
        prior = self.store.run("bc", unit)
        run = self.store.begin_run("bc", unit, resources)
        if run["resumed"]:
            plan, stored = self._resumed_plan(run["run_id"])
            return run, plan, [] if stored else [self._plan_mark(plan)]
        return run, self._new_plan(actor, unit, entity_set, mode, prior), []

    def _capture_company(self, actor: Actor, company_id: str,
                         listed: Sequence[Mapping[str, Any]], lineage: str) -> dict[str, Any]:
        """The company's own record from the listing just read (no other request), as
        unit ``bc:<company>/companies`` (resource ``bc:<env>:<company>:companies``): the
        ontology's ``BcCompany``; an etag-less row is versioned by its content digest."""
        environment = self.reader.environment.lower()
        resource = f"bc:{environment}:{company_id}:{COMPANY_SET}"
        unit = bc_unit(company_id, COMPANY_SET)
        if not self.reader._audience.allows(actor, resource):
            return {"entity_set": COMPANY_SET, "unit": unit, "state": "not_visible",
                    "complete": False}
        run = self.store.begin_run("bc", unit, [resource])
        row = next((r for r in listed if str(r.get("id", "")).lower() == company_id), {})
        fields = {"id": company_id, "name": row.get("name"),
                  "displayName": row.get("display_name")}
        # A listing row BC sends with no etag (live: one company's) is versioned by the digest
        # of its fields, as ``_record`` versions a report row: a rename adds a version.
        etag = row.get("etag")
        if not isinstance(etag, str) or not etag:
            etag = "sha256:" + canonical_sha256(fields)
        meta = {"environment": environment, "company_id": company_id,
                "entity_set": COMPANY_SET, "id": company_id, "etag": etag,
                "last_modified": None, "source_acl": BusinessCentralReader._source_acl(),
                "content_sha256": canonical_sha256(fields)}
        self.store.save_page(run["run_id"], [CapturedRecord(
            path=(environment, company_id, COMPANY_SET, company_id),
            payload={"row": fields, "meta": meta}, source_version=etag,
            lineage=lineage)], None)
        self.store.finish_run(run["run_id"], "complete")
        return self._unit_report(actor, unit, COMPANY_SET, requests=0)

    def _references_planned(self, environment: str, company_id: str,
                            entity_sets: Sequence[str]) -> bool:
        """The call reads ``company_id``'s purchase-order field page: it captures
        ``purchaseOrders`` and the pinned map names the company (ODataV4 addresses a company
        by name; an unnamed company's page is never read, by a guessed name or otherwise)."""
        return (REFERENCE_PARENT_SET in entity_sets
                and bool(self.names.get(environment, {}).get(company_id)))

    def _capture_references(self, actor: Actor, company_id: str, company_name: str,
                            lineage: str, page_size: int) -> dict[str, Any]:
        """The company's purchase-order field page as unit ``bc:<company>/
        purchaseOrderReferences`` (``capture_store.BC_FIELD_PAGES``; audience: the
        ``purchaseOrders`` resource): ``Your_Reference`` and ``Buy_from_Vendor_No`` of each
        purchase order, read in one traversal of ODataV4 Page 50
        (``BusinessCentralReader.purchase_order_references``) and stored under the API v2.0
        order whose ``number`` is the page row's ``No``, read in this capture. Read only
        after the company's ``purchaseOrders`` unit settled (else ``skipped``,
        ``purchase_orders_not_settled``: a reference is never joined to an order the
        capture does not hold). A page row is stored only when exactly one held order has
        its number and the vendor numbers do not differ; every other row is counted, never
        stored: ``no_number`` (no ``No``), ``no_api_record`` (no held order),
        ``ambiguous_api_record`` (several), ``duplicate_number`` (the page lists the number
        twice: every such row), ``vendor_mismatch``. ``blank_reference`` counts stored rows
        whose ``Your_Reference`` is blank (the link step counts them ``missing``). A read
        the item or page cap truncated, or one that dropped a row (``REFERENCE_DROPPED``: a
        row the page does not tie to one held order alone) finishes ``partial``;
        ``no_api_record`` (an order the capture does not hold, e.g. one created since its
        ``purchaseOrders`` read) is counted and leaves a complete traversal ``complete``,
        which marks orders no longer listed missing. A company name the grammar refuses
        (``grammar.bc_company_name_ok``: not a pinned name) fails the unit
        ``company_name_refused`` before any request."""
        environment = self.reader.environment.lower()
        resource = self.reader.resource(company_id, REFERENCE_PARENT_SET)
        unit = bc_unit(company_id, REFERENCE_SET)
        if not self.reader._audience.allows(actor, resource):
            return {"entity_set": REFERENCE_SET, "unit": unit, "state": "not_visible",
                    "complete": False}
        if not _settled(self.store.run("bc", bc_unit(company_id, REFERENCE_PARENT_SET))):
            return {"entity_set": REFERENCE_SET, "unit": unit, "state": "skipped",
                    "reason": "purchase_orders_not_settled", "complete": False}
        if not grammar.bc_company_name_ok(company_name):
            return {"entity_set": REFERENCE_SET, "unit": unit, "state": "failed",
                    "reason": "company_name_refused", "complete": False}
        if self._reusable(unit):
            return self._unit_report(actor, unit, REFERENCE_SET, reused=True)
        run = self.store.begin_run("bc", unit, [resource])
        if self._deferred(run):
            return self._unit_report(actor, unit, REFERENCE_SET, deferred=True)
        run_id = run["run_id"]
        try:
            listing = self.reader.purchase_order_references(
                actor, company_id, company_name, page_size=page_size)
        except SourceUnavailable as exc:
            self._failure(run_id, exc, resumed=False)
            return self._unit_report(actor, unit, REFERENCE_SET)
        counts = dict.fromkeys(REFERENCE_COUNTS, 0)
        counts["no_number"] = int(listing["completeness"].get("no_number") or 0)
        orders: dict[str, list[CapturedRecord]] = {}
        for record in self.store.current_records("bc", bc_unit(company_id,
                                                               REFERENCE_PARENT_SET)):
            number = (record.payload.get("row") or {}).get("number")
            if isinstance(number, str) and number:
                orders.setdefault(number, []).append(record)
        listed: dict[str, int] = {}
        for item in listing["items"]:
            listed[item["no"]] = listed.get(item["no"], 0) + 1
        captured: list[CapturedRecord] = []
        for item in listing["items"]:
            fields, held = item["fields"], orders.get(item["no"], [])
            if listed[item["no"]] > 1:
                counts["duplicate_number"] += 1
                continue
            if not held:
                counts["no_api_record"] += 1
                continue
            if len(held) > 1:
                counts["ambiguous_api_record"] += 1
                continue
            order = held[0]
            theirs = (order.payload.get("row") or {}).get("vendorNumber")
            ours = fields.get("Buy_from_Vendor_No")
            if (isinstance(theirs, str) and theirs.strip() and isinstance(ours, str)
                    and ours.strip() and theirs.strip() != ours.strip()):
                counts["vendor_mismatch"] += 1
                continue
            reference = fields.get("Your_Reference")
            if not isinstance(reference, str) or not reference.strip():
                counts["blank_reference"] += 1
            record_id = order.path[3]
            meta = {"environment": environment, "company_id": company_id,
                    "entity_set": REFERENCE_SET, "id": record_id, "etag": item["etag"],
                    "last_modified": None, "source_acl": BusinessCentralReader._source_acl(),
                    "content_sha256": canonical_sha256(fields),
                    "parent": order.source_version,
                    "live_citation": citation(environment, company_id, REFERENCE_SET,
                                              record_id, item["etag"])}
            captured.append(CapturedRecord(
                path=(environment, company_id, REFERENCE_SET, record_id),
                payload={"row": fields, "meta": meta}, source_version=item["etag"],
                lineage=lineage))
        counts["stored"] = len(captured)
        withheld = int(listing["completeness"].get("withheld") or 0)
        self.store.save_page(run_id, captured, None, withheld=withheld,
                             dropped=sum(counts[k] for k in REFERENCE_DROPPED))
        if listing["completeness"].get("complete") and not withheld:
            self.store.finish_run(run_id, "complete", detail={"references": counts})
        else:
            reason = ("withheld_or_dropped" if withheld
                      else listing["completeness"].get("truncation_reason") or "truncated")
            self.store.finish_run(run_id, "partial", reason=reason,
                                  detail={"references": counts})
        return self._unit_report(actor, unit, REFERENCE_SET, references=counts)

    def _expands(self, actor: Actor, company_id: str, header_set: str) -> bool:
        """Whether a header page of ``header_set`` may carry its lines: the pinned metadata
        declares the navigation (``expandable_sets``) and ``actor`` may read both sets."""
        line_set = grammar.BC_DOCUMENT_LINES.get(header_set)
        return (header_set in expandable_sets() and all(
            self.reader._audience.allows(actor, self.reader.resource(company_id, name))
            for name in (header_set, line_set)))

    def _capture_set(self, actor: Actor, company_id: str, entity_set: str, lineage: str,
                     page_size: int, cap: int | None, mode: str = "full",
                     expand: bool = True) -> dict[str, Any]:
        """One entity set's unit. A document header set's content read (not a key-only
        pass) carries its lines (``$expand``, ``read_document_page``) when ``_expands``;
        the lines are committed to the lines unit page by page (``_expanded_lines``), so
        ``_capture_lines`` reads per header only what no page carried. The choice is part
        of the run's plan (a resumed run keeps its query, so its cursor stays valid). An
        expanded page BC refuses (``_expand_refused``) fails the run ``expand_refused``,
        and the unit is read again at once without ``$expand`` (``expand`` false)."""
        resource = self.reader.resource(company_id, entity_set)
        unit = bc_unit(company_id, entity_set)
        if not self.reader._audience.allows(actor, resource):
            return {"entity_set": entity_set, "unit": unit, "state": "not_visible",
                    "complete": False}
        if self._reusable(unit):
            return self._unit_report(actor, unit, entity_set, reused=True)
        run, plan, pending = self._begin(actor, unit, entity_set, [resource], mode)
        if self._deferred(run):
            return self._unit_report(actor, unit, entity_set, deferred=True)
        run_id, cursor = run["run_id"], run["cursor"]
        line_set = grammar.BC_DOCUMENT_LINES.get(entity_set)
        if not run["resumed"]:
            plan["expand"] = (line_set if expand and not plan["keys_only"]
                              and self._expands(actor, company_id, entity_set)
                              and not self._reusable(bc_unit(company_id, line_set))
                              else None)
            pending = [self._plan_mark(plan)]
            if plan["mode"] == INCREMENTAL or plan["keys_only"]:
                # Pin the plan (its ``since``) before the first read, so a run stopped
                # on its first page resumes with the same filter.
                self.store.save_page(run_id, [], None, marks=pending)
                pending = []
        expanding = plan.get("expand") is not None
        if expanding and (plan["expand"] != line_set
                          or not self._expands(actor, company_id, entity_set)):
            # A resumed expanded run whose reader may no longer read the lines (or a
            # plan naming another set): never sent; the unit is read again without.
            self.store.finish_run(run_id, "failed", reason="expand_refused",
                                  error="expand_not_allowed", detail=plan)
            return self._capture_set(actor, company_id, entity_set, lineage, page_size, cap,
                                     mode, expand=False)
        posted_only = entity_set in grammar.BC_POSTED_ONLY_SETS
        query = (grammar.bc_query(entity_set, modified_since=plan["since"],
                                  keys_only=bool(plan["keys_only"]),
                                  expand=plan.get("expand"), posted_only=posted_only)
                 if plan["since"] or plan["keys_only"] or expanding or posted_only else None)
        page = min(page_size, CAPTURE_EXPAND_PAGE_SIZE) if expanding else page_size
        current: dict[tuple[str, ...], int] | None = None
        taken, restarted, refetched, key_failures, expanded = 0, False, 0, 0, 0
        # Every page URL this call sent for the unit (None: its first page). A nextLink
        # back to one of them is a cycle (A -> B -> A ...), refused like any other bad
        # continuation, so no unit pages forever; ``get_pages`` keeps the same set.
        first = self.reader.collection_url(company_id, entity_set, query)
        sent: set[str] = set()

        def stop(exc: SourceUnavailable, resumed: bool) -> dict[str, Any]:
            if pending:
                self.store.save_page(run_id, [], cursor, marks=pending)
            self._failure(run_id, exc, resumed=resumed, plan=plan)
            return report()

        def report() -> dict[str, Any]:
            return self._unit_report(actor, unit, entity_set, plan=plan,
                                     keys_refetched=refetched, key_failures=key_failures,
                                     **({"lines_expanded": expanded} if expanding else {}))

        while not self._out_of_time(run_id):
            key = cursor or first
            if key in sent:
                return stop(nextlink_refused("business-central pagination loop (a nextLink "
                                             "repeated a page)"), True)
            sent.add(key)
            lines: dict[str, list[dict[str, Any]]] = {}
            try:
                if expanding:
                    records, withheld, next_url, lines = self.reader.read_document_page(
                        actor, company_id, entity_set, cursor, page_size=page, query=query)
                else:
                    records, withheld, next_url = self.reader.read_page(
                        actor, company_id, entity_set, cursor, page_size=page, query=query)
            except SourceUnavailable as exc:
                # Only the listing GET can be an expired continuation.
                if (cursor is not None and not restarted
                        and getattr(exc, "http_status", None) == 400):
                    self.store.restart_run(run_id, "continuation_refused")
                    cursor, restarted, sent = None, True, set()
                    continue
                if expanding and _expand_refused(exc):
                    self.store.finish_run(run_id, "failed", reason="expand_refused",
                                          error=_error_class(exc), detail=plan)
                    return self._capture_set(actor, company_id, entity_set, lineage,
                                             page_size, cap, mode, expand=False)
                return stop(exc, cursor is not None)
            except ReadOnlyGuardError:
                if cursor is None:
                    raise
                # Backstop: the guard refused a cursor GET the reader's own check let
                # through. The same typed refusal, never an uncaught stall on the cursor.
                return stop(nextlink_refused("business-central nextLink escaped the "
                                             "collection (refused by the guard)"), True)
            dropped, marks = 0, self._seen_max(records, withheld)
            if plan["keys_only"]:
                if current is None:
                    current = dict(self.store.current_versions("bc", unit))
                try:
                    captured, dropped, signals, fetched, failed = self._confirm_keys(
                        actor, company_id, entity_set, lineage, records, current, plan)
                except SourceUnavailable as exc:  # a get-by-id: pause, never restart
                    return stop(exc, True)
                refetched, key_failures = refetched + fetched, key_failures + failed
                marks += signals
            else:
                captured = [self._captured(r, lineage) for r in records]
            self.store.save_page(run_id, captured, next_url, withheld=withheld,
                                 dropped=dropped, marks=[*pending, *marks])
            pending = []
            if lines:
                expanded += self._expanded_lines(actor, company_id, entity_set, lineage, mode,
                                                 records, lines)
            taken += len(records) + withheld
            if next_url is None:
                self._finish(run_id, unit, plan, traversed=plan["mode"] == "full")
                break
            cursor = next_url
            if cap is not None and taken >= cap:
                self.store.pause_run(run_id, reason="record_cap")
                break
        return report()

    def _confirm_keys(self, actor: Actor, company_id: str, entity_set: str, lineage: str,
                      keys: Sequence[Mapping[str, Any]], current: Mapping[tuple[str, ...], int],
                      plan: Mapping[str, Any]
                      ) -> tuple[list[CapturedRecord], int, list[str], int, int]:
        """One key-only page of a full pass: (records to save, dropped, marks, reads,
        failed reads). Only sets without calculated fields get here: the etag and
        ``lastModifiedDateTime`` then cover every field the stored version holds.

        A key whose etag and ``lastModifiedDateTime`` equal the stored current version
        (same lineage) is that version, seen again: it is re-saved with the stored
        content (same digest: no new version, ``last_seen`` advances). Any other key
        is read by id; a record gone since the listing is dropped and counted, and a
        400 on that read is a failed key (counted as dropped: the unit does not
        settle, and the run is not restarted). Other failures propagate."""
        if grammar.bc_has_calculated_fields(entity_set):
            raise CaptureError("keys_only_refused", "a set with calculated fields")
        kind = source_kind("bc")
        paths = [kind.canonical_path((key["environment"], company_id, entity_set, key["id"]))
                 for key in keys]
        known = [(p, make_handle(kind, p, self.store.generation, current[p]))
                 for p in paths if p in current]
        stored: dict[tuple[str, ...], Mapping[str, Any]] = {}
        for start in range(0, len(known), _RESOLVE_BATCH):
            chunk = known[start:start + _RESOLVE_BATCH]
            results = self.store.resolve_many([h for _p, h in chunk], actor)["results"]
            stored.update({p: r for (p, _h), r in zip(chunk, results) if r.get("ok")})
        watermark = _instant(plan.get("watermark"))
        floor = None if watermark is None else watermark - WATERMARK_OVERLAP
        captured: list[CapturedRecord] = []
        dropped, fetched, failed, below = 0, 0, 0, False
        for path, key in zip(paths, keys):
            version = stored.get(path)
            meta = version.get("meta", {}) if version else {}
            if (version is not None and version["lineage"] == lineage
                    and meta.get("etag") == key["etag"]
                    and meta.get("last_modified") == key["last_modified"]):
                captured.append(CapturedRecord(
                    path=path, payload={"row": version["row"], "meta": meta},
                    source_version=version["source_version"], lineage=lineage))
                continue
            try:
                record = self.reader.get_entity(actor, company_id, entity_set, key["id"])
            except NotVisible:
                dropped += 1
                continue
            except SourceUnavailable as exc:
                if getattr(exc, "http_status", None) != 400:
                    raise
                dropped, failed = dropped + 1, failed + 1
                continue
            fetched += 1
            moment = _instant(record["last_modified"])
            if floor is not None and (moment is None or moment < floor):
                below = True  # incremental reads would not have seen this change
            captured.append(self._captured(record, lineage))
        return (captured, dropped, [SIGNAL_CHANGED_BELOW_WATERMARK] if below else [], fetched,
                failed)

    def _posted_marks(self, actor: Actor, header_set: str,
                      headers: Sequence[tuple[tuple[str, ...], int]],
                      prior_marks: set[str]) -> set[str]:
        """Marks of current header versions that are posted documents of a
        ``POSTED_DOCUMENT_STATUSES`` set and whose lines the previous lines run
        covered cleanly for that same version (no ``LINES_INCOMPLETE_MARK``)."""
        statuses = POSTED_DOCUMENT_STATUSES.get(header_set)
        if not statuses or not prior_marks:
            return set()
        kind = source_kind("bc")
        wanted = [(str(c), make_handle(kind, path, self.store.generation, c))
                  for path, c in headers if str(c) in prior_marks
                  and LINES_INCOMPLETE_MARK + str(c) not in prior_marks]
        posted: set[str] = set()
        for start in range(0, len(wanted), _RESOLVE_BATCH):
            chunk = wanted[start:start + _RESOLVE_BATCH]
            results = self.store.resolve_many([h for _m, h in chunk], actor)["results"]
            for (mark, _h), result in zip(chunk, results):
                status = result.get("row", {}).get("status") if result.get("ok") else None
                if isinstance(status, str) and status in statuses:
                    posted.add(mark)
        return posted

    def _lines_run(self, actor: Actor, company_id: str, header_set: str, mode: str
                   ) -> dict[str, Any] | tuple[str, dict[str, Any], dict[str, Any], list[str]]:
        """The lines unit of ``header_set`` opened to read: (unit, run, plan, marks to commit
        with its first step); or its report when nothing is read (not visible to ``actor``,
        completed in this session, deferred by a throttle)."""
        line_set = grammar.BC_DOCUMENT_LINES[header_set]
        resources = [self.reader.resource(company_id, header_set),
                     self.reader.resource(company_id, line_set)]
        unit = bc_unit(company_id, line_set)
        if not all(self.reader._audience.allows(actor, r) for r in resources):
            return {"entity_set": line_set, "unit": unit, "state": "not_visible",
                    "complete": False}
        if self._reusable(unit):
            return self._unit_report(actor, unit, line_set, reused=True)
        run, plan, pending = self._begin(actor, unit, line_set, resources, "full")
        if self._deferred(run):
            return self._unit_report(actor, unit, line_set, deferred=True)
        if not run["resumed"]:
            last_full = _instant(plan["last_full_at"])
            skip_allowed = (mode == "auto" and header_set in POSTED_DOCUMENT_STATUSES
                            and not plan["line_skip_disabled"] and not self._full_pass_due
                            and last_full is not None
                            and self._now() - last_full
                            < timedelta(days=int(plan["interval_days"])))
            plan["mode"] = INCREMENTAL if skip_allowed else "full"
            pending = [self._plan_mark(plan)]
        return unit, run, plan, pending

    def _prior_line_marks(self, plan: Mapping[str, Any]) -> set[str]:
        return (self.store.run_marks(plan["prior_run"])
                if plan["prior_run"] is not None and not plan["line_skip_disabled"] else set())

    def _expanded_lines(self, actor: Actor, company_id: str, header_set: str, lineage: str,
                        mode: str, headers: Sequence[Mapping[str, Any]],
                        lines: Mapping[str, Sequence[Mapping[str, Any]]]) -> int:
        """Commit the lines a header page carried (``read_document_page``) to the lines
        unit's run, as ``_capture_lines`` commits a nested navigation read: each header
        version's mark (its capture id, the version the page just saved) with its lines, in
        one step per page (posted documents first, so a change in their lines is signalled
        as ``_capture_lines`` does). A header the page left out stays unmarked and is read
        per header. Nothing is committed when the lines unit is not to be read
        (``_lines_run``). Returns the headers committed."""
        kind = source_kind("bc")
        current = dict(self.store.current_versions("bc", bc_unit(company_id, header_set)))
        versions = []
        for header in headers:
            path = kind.canonical_path((header["environment"], company_id, header_set,
                                        header["id"]))
            if header["id"] in lines and path in current:
                versions.append((path, current[path], lines[header["id"]]))
        opened = self._lines_run(actor, company_id, header_set, mode) if versions else {}
        if isinstance(opened, dict):
            return 0
        _unit, run, plan, pending = opened
        posted = self._posted_marks(actor, header_set, [(p, c) for p, c, _r in versions],
                                    self._prior_line_marks(plan))
        signal: list[str] = []
        for is_posted in (True, False):
            group = [(str(c), rows) for _p, c, rows in versions
                     if (str(c) in posted) == is_posted]
            if not group and not (signal or pending):
                continue
            counts = self.store.save_page(
                run["run_id"], [self._captured(r, lineage) for _m, rows in group for r in rows],
                None, marks=[*pending, *signal, *(m for m, _rows in group)])
            pending = []
            if is_posted and counts["new_versions"]:
                # Lines of an unchanged posted document changed: the skip is unsafe here.
                signal = [SIGNAL_LINE_SKIP_CHANGED]
        return len(versions)

    def _capture_lines(self, actor: Actor, company_id: str, header_set: str,
                       lineage: str, mode: str = "full") -> dict[str, Any]:
        """Lines of every captured current header, by the nested navigation (the
        live-proven path).

        Progress is a set of marks, one per header *version* whose lines this run
        read or confirmed (its capture id), committed with those lines; there is no
        positional cursor. A resumed run reads every current header version it has
        not marked, so a header added (at any id) or changed since the run began is
        read too. The unit finishes ``complete`` only when the header unit's latest
        run is settled and every current header version is marked at finish (with
        nothing skipped); otherwise ``partial`` (``headers_incomplete`` /
        ``headers_changed``, or ``incremental`` when posted documents were skipped),
        or still ``in_progress`` (``line_reads_failed``) while a header's read is pending.

        A failed line read of one header (live: a server error on a few documents'
        lines on every run) leaves that header unread and pending (``LINES_FAILED_MARK``,
        not counted dropped) and the run goes on with the next header; at the end the
        run pauses ``line_reads_failed`` (resumable, with the classified error
        ``_error_class``), and its next call reads the headers it has not tried first,
        then retries exactly the pending ones once: once they succeed the unit finishes
        complete; a header whose retry fails too is counted dropped
        (``LINES_EXHAUSTED_MARK``) and the run finishes ``partial`` /
        ``line_reads_failed``, so the next run is a fresh full pass. Running out of the
        request budget is a budget stop, never a document failure. A 404 on a document's lines is checked with a get of the header: a
        header that is gone too is recorded gone (``LINES_GONE_MARK``, its lines are
        nothing), one that still exists is a failed read. A throttle, or
        ``LINE_FAILURE_STREAK`` first-attempt failures in a row (the source looks down),
        pauses the unit as before.

        Skips (auto mode, ``POSTED_DOCUMENT_STATUSES`` sets only) run between the
        lines unit's own full passes (same schedule as entity sets) and need the same
        header version's lines read cleanly before: a read that withheld, dropped or
        truncated a line commits ``LINES_INCOMPLETE_MARK`` with the header mark, and
        that version is re-read."""
        line_set = grammar.BC_DOCUMENT_LINES[header_set]
        opened = self._lines_run(actor, company_id, header_set, mode)
        if isinstance(opened, dict):
            return opened
        unit, run, plan, pending = opened
        header_unit = bc_unit(company_id, header_set)
        run_id = run["run_id"]
        headers = self.store.current_versions("bc", header_unit)
        posted = self._posted_marks(actor, header_set, headers, self._prior_line_marks(plan))
        done = self.store.run_marks(run_id)
        skipped, refused, gone, streak, line_error, via_filter = 0, 0, 0, 0, None, 0
        failed_now = False

        def flush() -> None:
            nonlocal pending
            if pending:
                self.store.save_page(run_id, [], None, marks=pending)
                pending = []

        def report() -> dict[str, Any]:
            return self._unit_report(actor, unit, line_set, plan=plan, lines_skipped=skipped,
                                     nextlink_refused=refused, lines_gone=gone,
                                     lines_via_filter=via_filter)

        # Headers not tried yet first, then the ones whose read failed in an earlier
        # call of this run: a few failing documents never hold back the rest.
        order = sorted(headers, key=lambda h: LINES_FAILED_MARK + str(h[1]) in done)
        for path, header_capture in order:
            mark = str(header_capture)
            if mark in done:
                continue
            if plan["mode"] == INCREMENTAL and mark in posted:
                pending += [mark, LINES_SKIPPED_MARK]  # duplicate marks are ignored
                skipped += 1
                continue
            flush()
            if self._out_of_time(run_id):
                return report()
            document_id = path[3]
            retry = LINES_FAILED_MARK + mark in done
            failure: SourceUnavailable | None = None
            try:
                try:
                    listing = self.reader.list_lines(actor, company_id, header_set,
                                                     document_id, page_size=100, max_pages=50,
                                                     max_items=5000)
                except SourceNotFound as missing:
                    # Gone only when the document itself is gone too; a header that
                    # still exists with no readable lines is a failed read.
                    try:
                        self.reader.get_entity(actor, company_id, header_set, document_id)
                    except NotVisible:
                        self.store.save_page(run_id, [], None,
                                             marks=[mark, LINES_GONE_MARK + mark])
                        gone += 1
                        continue
                    raise missing
            except SourceUnavailable as exc:
                if is_nextlink_refusal(exc):
                    # This document's lines continuation is refused: its lines are
                    # incomplete (the unit cannot settle complete), the refused link is
                    # not followed, and the next document goes on.
                    self.store.save_page(run_id, [], None, dropped=1,
                                         marks=[mark, LINES_INCOMPLETE_MARK + mark])
                    refused += 1
                    continue
                if isinstance(exc, RequestBudgetExhausted):
                    # Budget, not this document: the run resumes it as it was.
                    self.store.pause_run(run_id, reason="budget")
                    self._stopped = "budget"
                    return report()
                line_error = _error_class(exc)
                if getattr(exc, "http_status", None) in _THROTTLE_STATUSES:
                    self._failure(run_id, exc, resumed=True, plan=plan, label=line_error)
                    return report()
                failure, listing = exc, None
            if (failure is not None and line_set in _FLAT_LINE_SETS
                    and getattr(failure, "http_status", None) not in _REFUSED_STATUSES):
                # The navigation failed for this document (live: a few purchase orders'
                # lines on every run): read the same lines by the filtered line set once.
                # A refusal (401/403) is never routed around by another read path.
                try:
                    listing = self.reader.list_lines(actor, company_id, header_set,
                                                     document_id, page_size=100,
                                                     max_pages=50, max_items=5000, flat=True)
                except SourceUnavailable as exc:
                    if isinstance(exc, RequestBudgetExhausted):
                        self.store.pause_run(run_id, reason="budget")
                        self._stopped = "budget"
                        return report()
                    if getattr(exc, "http_status", None) in _THROTTLE_STATUSES:
                        self._failure(run_id, exc, resumed=True, plan=plan, label=line_error)
                        return report()
                    listing = None
            if listing is None and retry:
                # Second failure in this run: dropped, the run will finish partial.
                self.store.save_page(run_id, [], None, dropped=1,
                                     marks=[mark, LINES_INCOMPLETE_MARK + mark,
                                            LINES_EXHAUSTED_MARK + mark,
                                            LINES_ERROR_MARK + line_error])
                continue
            if listing is None:
                failed_now = True
                # The header stays unread (no header mark, nothing counted dropped).
                self.store.save_page(run_id, [], None, marks=[LINES_FAILED_MARK + mark])
                streak += 1
                if streak >= LINE_FAILURE_STREAK:
                    self._failure(run_id, failure, resumed=True, plan=plan, label=line_error)
                    return report()
                continue
            streak = 0
            via_filter += failure is not None
            completeness = listing["completeness"]
            dropped = (completeness.get("provenance_dropped", 0)
                       + (1 if completeness.get("truncated") else 0))
            withheld = completeness.get("withheld_count", 0)
            counts = self.store.save_page(
                run_id, [self._captured(r, lineage) for r in listing["items"]], None,
                withheld=withheld, dropped=dropped,
                marks=[mark] + ([LINES_INCOMPLETE_MARK + mark] if withheld or dropped
                                else []))
            if mark in posted and counts["new_versions"]:
                # Lines of an unchanged posted document changed: the skip is unsafe here.
                pending.append(SIGNAL_LINE_SKIP_CHANGED)
        flush()
        if failed_now:
            # Resumable: the next call retries exactly the pending headers.
            self.store.pause_run(run_id, reason="line_reads_failed", error=line_error)
            return report()
        header_run = self.store.run("bc", header_unit)
        done = self.store.run_marks(run_id)
        unvisited = [p for p, c in self.store.current_versions("bc", header_unit)
                     if str(c) not in done]
        if header_run is not None and header_run["state"] == "failed" \
                and header_run.get("reason") in _HEADER_REFUSALS:
            # The company refuses (or does not expose) the header set, so it has no headers
            # to read lines of: the lines unit fails with that reason, never "incomplete".
            self.store.finish_run(run_id, "failed", reason=header_run["reason"],
                                  error=header_run.get("error"), detail=plan)
        elif not _settled(header_run):
            # The header unit's own error names why it is open (a paused read's class).
            self.store.finish_run(run_id, "partial", reason="headers_incomplete",
                                  error=(header_run or {}).get("error"), detail=plan)
        elif unvisited:
            self.store.finish_run(run_id, "partial", reason="headers_changed", detail=plan)
        elif any(m.startswith(LINES_EXHAUSTED_MARK) for m in done):
            errors = sorted(m[len(LINES_ERROR_MARK):] for m in done
                            if m.startswith(LINES_ERROR_MARK))
            self.store.finish_run(run_id, "partial", reason="line_reads_failed",
                                  error=errors[-1] if errors else None, detail=plan)
        else:
            self._finish(run_id, unit, plan, traversed=LINES_SKIPPED_MARK not in done)
        return report()

    @staticmethod
    def _captured(record: Mapping[str, Any], lineage: str) -> CapturedRecord:
        meta = {"environment": record["environment"], "company_id": record["company_id"],
                "entity_set": record["entity_set"], "id": record["id"], "etag": record["etag"],
                "last_modified": record["last_modified"], "source_acl": record["source_acl"],
                "content_sha256": record["content_sha256"],
                "live_citation": record["citation"]}
        return CapturedRecord(path=(record["environment"], record["company_id"],
                                    record["entity_set"], record["id"]),
                              payload={"row": record["fields"], "meta": meta},
                              source_version=record["etag"], lineage=lineage)


# Entity sets the case tools read (no employees; no Microsoft.NAV actions).
CASE_ENTITY_SETS = ("purchaseOrders", "purchaseReceipts", "purchaseInvoices", "salesOrders",
                    "salesInvoices", "items", "vendors", "customers")
# Finance sets ``probe --finance-entities`` checks (devloop/live-checks/bc.ledgers.json):
# field names, the ``$select`` allowlist BC accepts, lines of the first header.
FINANCE_ENTITY_SETS = ("itemLedgerEntries", "generalLedgerEntries", "agedAccountsReceivables",
                       "agedAccountsPayables", "purchaseCreditMemos",
                       "customerPaymentJournals", "vendorPaymentJournals")


def _error_label(exc: Exception) -> str:
    return type(exc).__name__ + (": " + str(exc) if isinstance(exc, SourceUnavailable) else "")


def _error_class(exc: Exception) -> str:
    """A unit error from fixed vocabularies only: ``unavailable_detail`` (the HTTP
    status, e.g. ``http_500``, or the failure kind) and the upstream error-code token,
    ``http_500:internal_server_error``. Never message text, a URL, an id or a value."""
    detail = unavailable_detail(exc) or type(exc).__name__
    code = getattr(exc, "error_code", None)
    label = f"{detail}:{code}" if is_error_code_token(code) else detail
    return label if len(label) <= 64 else detail


# The probe's retry (``ProbeBackoff``): the pauses before a GET's second and third attempt
# (also the longest ``Retry-After`` a probe waits), the total pause per probe, and its wall
# cap with the time one attempt may take (``GuardedTransport``'s 30 s request timeout).
# Every live check running the probe kills it at 300 s, above the cap by more than one
# attempt (asserted by integrations.tests.test_bc_read_probe_counts); healthy probes took 50-200 s.
PROBE_UNIT_PAUSES = (5.0, 15.0)
PROBE_RETRY_BUDGET_SECONDS = 60.0
PROBE_WALL_SECONDS = 240.0
PROBE_ATTEMPT_SECONDS = 30.0


class ProbeBackoff(BoundedBackoff):
    """The probe's one retry layer (``SourceApi``'s, at most ``MAX_RETRIES`` per GET): a
    GET that failed in transport or with 429/502/503/504 is sent again after
    ``PROBE_UNIT_PAUSES`` (5 s, then 15 s; live connect failures come in bursts), or after
    the response's ``Retry-After``; one naming a longer wait than the longest pause is
    not retried (a sooner retry is throttled again). At most ``budget_seconds`` of pause
    per probe. ``begin`` starts the wall: a GET is repeated, and ``read`` starts a read,
    only when it can end (``attempt_seconds``) by ``wall_seconds``; a read past it, or
    whose GET the wall alone kept from a retry, fails typed (``unavailable_detail``
    ``wall_cap``, counted in ``wall_capped``), so the probe ends inside the check's
    timeout and says why. Sleep and timer are injected so tests never wait."""

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep,
                 timer: Callable[[], float] = time.monotonic,
                 budget_seconds: float = PROBE_RETRY_BUDGET_SECONDS,
                 wall_seconds: float = PROBE_WALL_SECONDS,
                 attempt_seconds: float = PROBE_ATTEMPT_SECONDS) -> None:
        super().__init__(max_retries=len(PROBE_UNIT_PAUSES), sleep=sleep,
                         budget_seconds=budget_seconds)
        self._timer = timer
        self.wall_seconds, self.attempt_seconds = wall_seconds, attempt_seconds
        self._start = timer()
        self.wall_capped = 0
        self._cut = False  # the wall alone refused a retry of the current request

    def begin(self) -> None:
        self._start = self._timer()

    def elapsed(self) -> float:
        return self._timer() - self._start

    def request(self) -> None:
        self._cut = False

    def _ends_in_wall(self, wait: float = 0.0) -> bool:
        return self.elapsed() + wait + self.attempt_seconds <= self.wall_seconds

    def _retry_after(self, headers: Mapping[str, str] | None) -> float | None:
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        return self.retry_after(lowered.get("retry-after"))

    def delay(self, attempt: int, headers: Mapping[str, str] | None = None) -> float:
        wait = self._retry_after(headers)
        return wait if wait is not None else PROBE_UNIT_PAUSES[attempt]

    def may_retry(self, attempt: int, headers: Mapping[str, str] | None = None) -> bool:
        wait = self._retry_after(headers)
        if wait is not None and wait > PROBE_UNIT_PAUSES[-1]:
            return False
        if not super().may_retry(attempt, headers):
            return False
        if self._ends_in_wall(self.delay(attempt, headers)):
            return True
        self._cut = True
        return False

    def _wall_cap(self) -> SourceUnavailable:
        self.wall_capped += 1
        return SourceUnavailable("business-central probe wall cap reached")

    def read(self, call: Callable[[], Any]) -> Any:
        """``call()`` (one probe read, its GETs retried by ``SourceApi``) if it can end
        inside the wall, else a ``wall_cap`` failure without any request. A read that
        fails when the wall alone refused its last GET a retry fails ``wall_cap`` too, not
        with that GET's own error; an earlier GET's cut does not type it (``request``)."""
        if not self._ends_in_wall():
            raise self._wall_cap()
        self._cut = False
        try:
            return call()
        except SourceUnavailable:
            if not self._cut:
                raise
            raise self._wall_cap() from None


def _message_classed(exc: BaseException) -> dict[str, Any]:
    """``failure_diagnostics`` plus the ``MESSAGE_CLASS_TOKENS`` class of the error
    body's message (``upstream_message_class``): a token, never the message; and the
    ``request_shape`` of the failed read (null when no request was sent)."""
    message_class = getattr(exc, "message_class", None)
    return {**failure_diagnostics(exc), "message_class": message_class
            if isinstance(message_class, str) and message_class in MESSAGE_CLASS_TOKENS
            else None, "request": getattr(exc, "request_shape", None)}


def _probe_entity(reader: "BusinessCentralReader", actor: Actor, company_id: str,
                  entity_set: str, sample: int, wall: ProbeBackoff,
                  select_check: bool = False, message_class: bool = False) -> dict[str, Any]:
    """Counts and field names of one entity set (+ its lines); never values. A 404 is
    ``not_exposed`` (the company does not serve the set): a gap, never zero records. Every
    read goes through ``wall.read`` (none starts past the probe's wall cap). A failure
    carries ``failure_diagnostics``, plus its ``message_class`` when asked."""
    diagnose = _message_classed if message_class else failure_diagnostics
    result: dict[str, Any] = {"entity_set": entity_set}
    try:
        listing = wall.read(lambda: reader.list_entities(
            actor, company_id, entity_set, top=sample, page_size=sample, max_pages=1,
            max_items=sample))
    except (SourceUnavailable, NotVisible) as exc:
        result.update(error=_error_label(exc), failure=diagnose(exc),
                      not_exposed=isinstance(exc, SourceNotFound))
        return result
    items = listing["items"]
    names = sorted({k for i in items for k in i["fields"]})
    result.update({"records_returned": len(items), "field_names": names,
                   "truncated": listing["completeness"]["truncated"],
                   # a content-digest version (``_record``) is no etag BC sent
                   "has_etag": all(not i["etag"].startswith("sha256:") for i in items)
                   if items else None})
    spec = grammar.BC_QUERY_SPECS.get(entity_set)
    if spec is not None and items:
        result["select_allowlist_absent"] = sorted(spec.select - set(names))
        result["filter_fields_absent"] = sorted(set(spec.filters) - set(names))
    if spec is not None and select_check:
        # BC answers 400 to a $select naming a field it does not serve; one the pinned
        # $metadata does not declare is never sent.
        try:
            wall.read(lambda: reader.list_entities(
                actor, company_id, entity_set, top=1, page_size=1, max_pages=1, max_items=1,
                select=sorted(spec.select)))
            result["select_accepted"] = True
        except (SourceUnavailable, NotVisible) as exc:
            result.update(select_accepted=False, select_error=_error_class(exc))
        except ValueError:
            result.update(select_accepted=False, select_error="metadata_unsupported")
    line_set = grammar.BC_DOCUMENT_LINES.get(entity_set)
    if line_set and items:
        try:
            lines = wall.read(lambda: reader.list_lines(
                actor, company_id, entity_set, items[0]["id"], page_size=50, max_pages=1,
                max_items=50))
            line_names = sorted({k for i in lines["items"] for k in i["fields"]})
            line_spec = grammar.BC_QUERY_SPECS.get(line_set)
            result["lines"] = {"entity_set": line_set, "records_returned": len(lines["items"]),
                               "truncated": lines["completeness"]["truncated"],
                               "provenance_dropped": lines["completeness"]["provenance_dropped"],
                               "field_names": line_names,
                               "select_allowlist_absent": sorted(line_spec.select
                                                                 - set(line_names))
                               if lines["items"] and line_spec else None}
            if lines["items"]:
                try:
                    result["lines"]["citation_roundtrip"] = wall.read(
                        lambda: reader.resolve_citation(lines["items"][0]["citation"],
                                                        actor))["status"]
                except (SourceUnavailable, NotVisible, ValueError) as exc:
                    result["lines"]["citation_roundtrip"] = "error: " + type(exc).__name__
        except (SourceUnavailable, NotVisible) as exc:
            result["lines"] = {"entity_set": line_set, "error": _error_label(exc),
                               "failure": diagnose(exc)}
    if items:
        try:
            result["citation_roundtrip"] = wall.read(
                lambda: reader.resolve_citation(items[0]["citation"], actor))["status"]
        except (SourceUnavailable, NotVisible) as exc:
            result["citation_roundtrip"] = "error: " + type(exc).__name__
    return result


def expected_refusal(value: Any, books: Collection[str]) -> tuple[str, str, str]:
    """One expected refusal ``<company book>:<entity set>:<BC error code>`` (a spec's
    ``--expected-refusal``): a book of ``books`` (the pinned companies of the probe's
    environment, ``bc_company_books.json``, matched by GUID; a company index is a position,
    not an identity), a ``FINANCE_ENTITY_SETS`` name and a named error-code token
    (``is_error_code_token``, never ``other``, a code outside the vocabulary). Anything else,
    a company-less
    ``<entity set>:<BC error code>`` included, is refused."""
    parts = value.split(":") if isinstance(value, str) else []
    if (len(parts) != 3 or parts[0] not in books or parts[1] not in FINANCE_ENTITY_SETS
            or not is_error_code_token(parts[2]) or parts[2] == "other"):
        raise ValueError("expected refusal must be "
                         "<company book>:<finance entity set>:<BC error code>")
    return parts[0], parts[1], parts[2]


def probe(environ: Mapping[str, str] | None = None, *, sender=None,
          entity_set: str = "purchaseOrders", case_entities: bool = False,
          sample: int = 1, retry: ProbeBackoff | None = None,
          finance_entities: bool = False,
          expected_refusals: Sequence[str] = (),
          books: Mapping[str, Mapping[str, str]] | None = None,
          ledger_scope: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Counts and shapes only: no company names, numbers, parties or amounts.

    ``case_entities`` adds, for every company (by index, not id), the record
    count of a ``$top=<sample>`` read, field names, line counts of the first
    document, citation round-trips and which allowlisted ``$select``/filter
    names the live schema lacks. Every failed read is also one row of
    ``case_entity_errors``: company index, the entity set that failed, ``header``
    or ``lines``, and ``failure_diagnostics`` (class, HTTP status, error-code
    class, detail token), never a message or body. ``finance_entities`` does the same for
    ``FINANCE_ENTITY_SETS``, each failure also naming its ``message_class`` (the BC error
    message classified into ``MESSAGE_CLASS_TOKENS``, never copied) and its ``request``
    (``request_shape``: entity path, option names, ``$select`` and ``$filter`` field names,
    never a value), and also reads each with its whole ``$select`` allowlist
    (``select_accepted``; ``select_error`` ``metadata_unsupported``, unsent, when the pinned
    ``$metadata`` lacks one of its names); ``finance_not_exposed`` lists the sets a company
    answers 404 for (company index and entity set). ``finance_refused`` lists the upstream
    refusals: a header read answered 400/401/403 with a BC JSON error code (so BC, not a
    proxy, refused) while another company served the same read in this run (so the request
    itself is valid and authorized), by company index, book (the pinned company's,
    ``books``, default ``bc_company_books.json``; null when unpinned), entity set, status
    and BC code, each naming its ``message_class`` as diagnosis. Only one the caller
    expects for that company (``expected_refusals``, ``<company book>:<entity set>:<BC
    error code>``, data from the live-check spec, architect-20261013T0921Z) is ``expected``
    and reported only, whatever its message says; any
    other, the same set refused by another company or a configuration-class message
    included, is also a ``finance_entity_errors`` row, so the check fails at once, except a 400 whose BC
    code is in ``UNSERVED_CODES`` (the company does not serve the set): a ``blocked_upstream``
    part (``relation_unavailable``, named by book or company index and set), no error. A set
    refused in every company is no such proof: it stays an error. Every other failed read
    is one row of ``finance_entity_errors``, counted in ``finance_errors``;
    ``finance_served`` counts the header reads BC answered. ``retry`` (default ``ProbeBackoff()``) is the GETs' one
    retry layer and the probe's wall cap: ``get_retries`` / ``unit_retries`` and
    ``retry_sleep_seconds`` / ``unit_retry_sleep_seconds`` both report it (the unit
    fields kept for the records), ``stopped_reason`` is ``wall_cap`` when it cut a read
    (before it started or at a retry)."""
    if isinstance(sample, bool) or not isinstance(sample, int) or not 1 <= sample <= 5:
        raise ValueError("sample must be 1..5")
    if isinstance(expected_refusals, str):
        raise ValueError("expected_refusals must be a sequence")
    if retry is not None and not isinstance(retry, ProbeBackoff):
        raise ValueError("retry must be a ProbeBackoff")
    wall = retry if retry is not None else ProbeBackoff()
    wall.begin()
    api, transport, tenant_id, environment = build_bc(environ, sender=sender, retry=wall)
    pinned = (load_company_books() if books is None else books).get(environment.lower(), {})
    scope = ((load_ledger_scope() if books is None else {}) if ledger_scope is None
             else ledger_scope).get(environment.lower())
    expected = frozenset(expected_refusal(value, frozenset(pinned.values()))
                         for value in expected_refusals)
    entity_set = _entity_set(entity_set)
    env = environment.lower()
    audience = LiveSourceAudience({f"bc:{env}:*": ["probe:operator"]}, tenant="probe")
    actor = Actor("probe", frozenset({"probe:operator"}), "restricted")
    reader = BusinessCentralReader(api, audience, tenant_id=tenant_id, environment=environment)
    companies = wall.read(lambda: reader.companies(actor))
    report: dict[str, Any] = {"source": "business-central", "evidence_label": "live-read",
                              "read_only_guard": "GET-only; token POST only",
                              "token": "ok", "environment_is_production":
                                  environment.lower() == "production",
                              "companies_count": len(companies["items"]),
                              "companies_completeness": companies["completeness"]}
    if companies["items"]:
        company_id = companies["items"][0]["id"]
        listing = wall.read(lambda: reader.list_entities(
            actor, company_id, entity_set, page_size=1, max_pages=1, max_items=1))
        items = listing["items"]
        report["entity"] = {"entity_set": entity_set, "records_returned": len(items),
                            "completeness": listing["completeness"],
                            "field_names": sorted({k for i in items for k in i["fields"]}),
                            "has_etag": all(bool(i["etag"]) for i in items) if items else None}
        if items:
            report["entity"]["citation_roundtrip"] = wall.read(lambda: reader.resolve_citation(
                items[0]["citation"], actor))["status"]
    if case_entities:
        report["case_entities"] = [
            {"company_index": index,
             "entities": [_probe_entity(reader, actor, company["id"], name, sample, wall)
                          for name in CASE_ENTITY_SETS]}
            for index, company in enumerate(companies["items"])
            if isinstance(company.get("id"), str) and _GUID.fullmatch(company["id"])]
        report["case_entity_errors"] = [
            {"company_index": company["company_index"], "entity_set": part["entity_set"],
             "part": name, **part["failure"]}
            for company in report["case_entities"] for entity in company["entities"]
            for name, part in (("header", entity), ("lines", entity.get("lines") or {}))
            if "failure" in part]
    if finance_entities:
        report["finance_entities"] = [
            {"company_index": index, "book": pinned.get(company["id"].lower()),
             "entities": [_probe_entity(reader, actor, company["id"], name, sample, wall,
                                        select_check=True, message_class=True)
                          for name in FINANCE_ENTITY_SETS]}
            for index, company in enumerate(companies["items"])
            if isinstance(company.get("id"), str) and _GUID.fullmatch(company["id"])]
        rows = [(c["company_index"], c["book"], e) for c in report["finance_entities"]
                for e in c["entities"]]
        served = [e["entity_set"] for _i, _b, e in rows if "records_returned" in e]
        required_book = scope["required_book"] if scope else None
        gap_of = {g["company"]: g for g in (scope or {}).get("expected_gaps", [])}
        guids = {i: c["id"].lower() for i, c in enumerate(companies["items"])
                 if isinstance(c.get("id"), str) and _GUID.fullmatch(c["id"])}

        def gap(index: int, entity: Mapping[str, Any]) -> Mapping[str, Any] | None:
            # The listed company's exact refusal (status, BC code, message class) of a
            # listed set: data in the scope, matched by GUID.
            listed = gap_of.get(guids.get(index, ""))
            failure = entity.get("failure") or {}
            if (listed is None or entity.get("not_exposed")
                    or entity["entity_set"] not in listed["sets"]
                    or failure.get("http_status") != listed["http_status"]
                    or failure.get("error_code") != listed["error_code"]
                    or failure.get("message_class") != listed["message_class"]):
                return None
            return listed

        def refused(entity: Mapping[str, Any]) -> bool:
            failure = entity.get("failure") or {}
            return (not entity.get("not_exposed") and entity["entity_set"] in served
                    and failure.get("http_status") in _PERMANENT_STATUSES
                    and failure.get("error_code") is not None)

        def excused(book: str | None, entity: Mapping[str, Any]) -> bool:
            # Only the listed company's refusal: an unpinned company (no book) never is.
            # The message class is diagnosis, never a reason (architect-20261013T0921Z).
            return refused(entity) and (
                book, entity["entity_set"], entity["failure"]["error_code"]) in expected

        def unserved(book: str | None, entity: Mapping[str, Any]) -> bool:
            # A refusal the BC code reads as "this company does not serve the set", that no
            # spec entry expects: a blocked part, never a finance error. Never the required
            # book's: that one must serve.
            failure = entity.get("failure") or {}
            return (refused(entity) and not excused(book, entity)
                    and not (required_book is not None and book == required_book)
                    and failure.get("http_status") == 400
                    and failure.get("error_code") in UNSERVED_CODES)
        gaps = [(i, b, e, g) for i, b, e in rows for g in [gap(i, e)] if g is not None]
        report["finance_not_exposed"] = [{"company_index": i, "entity_set": e["entity_set"]}
                                         for i, _b, e in rows if e.get("not_exposed")]
        report["finance_refused"] = [{"company_index": i, "book": b,
                                      "entity_set": e["entity_set"], **e["failure"],
                                      "expected": excused(b, e)}
                                     for i, b, e in rows if refused(e) and gap(i, e) is None]
        report["finance_expected_gaps"] = [
            {"company_index": i, "book": g["label"], "entity_set": e["entity_set"],
             "reason": g["message_class"], "owner": g["owner"], "cause": g["cause"],
             "http_status": e["failure"]["http_status"],
             "error_code": e["failure"]["error_code"],
             "message_class": e["failure"]["message_class"]} for i, _b, e, g in gaps]
        # A served set with no record (200, empty value) is no error; a failed read (header
        # other than a 404 or an expected refusal, or lines) is one.
        report["finance_entity_errors"] = [
            {"company_index": i, "entity_set": part["entity_set"], "part": name,
             **part["failure"]}
            for i, b, e in rows
            for name, part in (("header", e), ("lines", e.get("lines") or {}))
            if "failure" in part
            and not (name == "header" and (e.get("not_exposed") or excused(b, e)
                                           or unserved(b, e) or gap(i, e) is not None))]
        blocked = [{"part": f"{b or f'company_{i}'}:{e['entity_set']}",
                    "source": "business-central", "http_status": 400,
                    "error_code": e["failure"]["error_code"], "cause": "relation_unavailable",
                    "reason": "entity set not served by the company"}
                   for i, b, e in rows if unserved(b, e) and gap(i, e) is None]
        if blocked:
            report["blocked_upstream"] = blocked
        report["finance_required"], report["finance_required_unserved"] = [], []
        if scope:
            mine = [(i, e) for i, b, e in rows if b == required_book]
            for name in scope["required_sets"]:
                entity = next((e for _i, e in mine if e["entity_set"] == name), None)
                if entity is None:
                    reason = "company_not_listed"
                elif "records_returned" not in entity:
                    reason = "header_not_served"
                elif not entity["records_returned"]:
                    reason = "no_journal_to_read_lines"
                elif "records_returned" not in (entity.get("lines") or {}):
                    reason = "lines_not_served"
                elif not entity["lines"]["records_returned"]:
                    reason = "journal_without_lines"
                else:
                    report["finance_required"].append(
                        {"book": required_book, "company_index": mine[0][0],
                         "entity_set": name, "records_returned": entity["records_returned"],
                         "lines_returned": entity["lines"]["records_returned"]})
                    continue
                report["finance_required_unserved"].append(
                    {"book": required_book, "entity_set": name, "reason": reason})
        report["finance_errors"] = len(report["finance_entity_errors"])
        report["finance_served"] = len(served)
        report["finance_select_rejected"] = sorted({e["entity_set"] for _i, _b, e in rows
                                                    if e.get("select_accepted") is False})
    report["requests_sent"] = transport.requests_sent
    report["get_retries"] = report["unit_retries"] = wall.retries
    report["retry_sleep_seconds"] = report["unit_retry_sleep_seconds"] = round(
        wall.slept_seconds, 3)
    report["stopped_reason"] = "wall_cap" if wall.wall_capped else None
    report["elapsed_seconds"] = round(wall.elapsed(), 1)
    return report


_UNIT_FIELDS = ("entity_set", "state", "complete", "records_current", "records_missing",
                "versions_total", "pages", "records", "new_versions", "withheld", "dropped",
                "restarts", "reason", "error", "resumable", "deferred", "payload_bytes",
                "mode", "keys_only", "since", "requests", "rows_changed",
                "last_full_pass_at", "lines_skipped", "keys_refetched", "key_failures",
                "full_reason", "nextlink_refused", "lines_gone", "lines_via_filter",
                "reused", "capture_age_seconds", "stale", "lines_expanded", "accounted",
                "later_ask", "references")


def bc_entity_sets(schema: Any, process: str = CAPTURE_PROCESS,
                   roots: Sequence[str] = CAPTURE_ROOT_SETS) -> list[str]:
    """The BC entity sets an eval's own capture needs (the host captures
    ``CAPTURE_ENTITY_SETS``), from the curated schema alone: every BC binding of each
    object type bound to a root set (``roots``) or named by the ``process`` graph's
    evidence and quantity checks, in capture order with the root sets first (a budget cut
    keeps the roots). Line sets come with their documents; the unbounded journals
    (``CAPTURE_RECORD_CAPS``: the payment stage's ledger) are left out, as no oracle
    aspect reads them."""
    # The process graphs' directory (operating.ontology_tools_process.PROCESS_DIR), named
    # from the schema root: integrations import no operating module.
    from ontology.schema import SCHEMA_DIR

    graph = json.loads((SCHEMA_DIR / "process" / f"{process}.json").read_text(encoding="utf-8"))
    types = {e["object_type"] for stage in graph.get("stages", [])
             for e in stage.get("evidence", [])}
    for check in graph.get("quantity_checks", []):
        types.add(check["group_by"]["object_type"])
        types.update(side["object_type"] for side in check.get("sides", []))
    types |= {tid for tid, t in schema.object_types.items()
              for b in t.get("bindings", [])
              if b.get("source") == "bc" and b.get("relation") in roots}
    wanted = {b["relation"] for tid in types
              for b in (schema.object_types.get(tid) or {}).get("bindings", [])
              if b.get("source") == "bc"}
    wanted -= set(CAPTURE_RECORD_CAPS)
    return [s for s in roots if s in wanted] + [
        s for s in CAPTURE_ENTITY_SETS if s in wanted and s not in roots]


def capture_probe(environ: Mapping[str, str] | None = None, *, sender=None,
                  db: str | Path | None = None, budget_seconds: float | None = None,
                  entity_sets: Sequence[str] = CAPTURE_ENTITY_SETS,
                  page_size: int = CAPTURE_PAGE_SIZE,
                  resolve_sample: int = 200, passes: int = 1,
                  retry: BoundedBackoff | None = None,
                  books: Mapping[str, Mapping[str, str]] | None = None,
                  monotonic: Callable[[], float] = time.monotonic,
                  mode: str = "auto", key_pass_last: bool = False,
                  audience: LiveSourceAudience | None = None,
                  actor: Actor | None = None,
                  roots: Sequence[str] = CAPTURE_ROOT_SETS,
                  fresh_since: str | None = None) -> dict[str, Any]:
    """Run (or resume) the capture into ``db`` and resolve a sample of its handles
    offline. Counts, states, book keys and field-free shapes only: never a value,
    a company id (``company_key`` instead; ``company_proof`` alone names the one captured
    company, with the environment and its purchase-order count), a number or a citation (a unit's
    ``since`` is a time, not a value). ``mode`` is ``BcCapture.capture``'s (default
    ``auto``: delta reads after the first full pass); ``key_pass_last`` makes the last
    pass the key-only full pass (auto mode, a unit with an earlier full pass).
    ``audience`` and ``actor`` (together): the capture audience and principal the store
    is bound to and the capture runs as, as a host captures (``operating.host``), so the
    host's readers resolve its handles; default a service audience bound to the BC tenant
    itself.
    ``roots``: the sets every company captures first (``BcCapture.capture``).
    ``fresh_since``: the verifier session's start (``session_fresh_since``): units ``db``
    holds complete from this session are not read again, any other unit is
    (``BcCapture.capture``)."""
    if mode not in CAPTURE_MODES:
        raise ValueError("mode must be full or auto")
    if (audience is None) != (actor is None):
        raise ValueError("audience and actor go together")
    if isinstance(passes, bool) or not isinstance(passes, int) or not 1 <= passes <= 3:
        raise ValueError("passes must be 1..3")
    if isinstance(resolve_sample, bool) or not isinstance(resolve_sample, int) \
            or not 0 <= resolve_sample <= 5000:
        raise ValueError("resolve_sample must be 0..5000")
    api, transport, tenant_id, environment = build_bc(environ, sender=sender, retry=retry)
    env = environment.lower()
    # The service audience and actor are bound to the BC tenant itself, and the store
    # pins tenant and environment (BcCapture.capture -> bind_source): a --db written
    # for another BC tenant or environment refuses this run.
    if audience is None:
        tenant = f"bc-tenant:{tenant_id.lower()}"
        audience = LiveSourceAudience({f"bc:{env}:*": ["capture:operator"]}, tenant=tenant)
        actor = Actor(tenant, frozenset({"capture:operator"}), "restricted")
    reader = BusinessCentralReader(api, audience, tenant_id=tenant_id, environment=environment)
    with tempfile.TemporaryDirectory() as scratch:
        store = CaptureStore(Path(db) if db else Path(scratch) / "capture.sqlite", audience)
        try:
            capture = BcCapture(reader, store, books=books, monotonic=monotonic)
            started = monotonic()
            # A pass after an unfinished pass (a stop, or a unit left resumable) continues
            # it: what this call already completed is reused, never read again
            # (gap-d4538d178d26: a second pass that restarted every unit re-read pass 1's
            # units on its own budget and never reached the rest). The call's start is the
            # freshness floor unless the caller's session start is earlier.
            call_start = store.now()
            reports: list[dict[str, Any]] = []
            resumed: list[bool] = []
            for index in range(passes):
                resume = bool(reports) and _unfinished(reports[-1])
                resumed.append(resume)
                reports.append(capture.capture(
                    actor, entity_sets=entity_sets, page_size=page_size,
                    budget_seconds=budget_seconds, mode=mode,
                    full_pass_due=key_pass_last and index == passes - 1, roots=roots,
                    fresh_since=fresh_since if fresh_since is not None or not resume
                    else call_start))
            seconds = monotonic() - started
            last = reports[-1]
            result: dict[str, Any] = {
                "source": "business-central", "evidence_label": last["evidence_label"],
                "read_only_guard": "GET-only; token POST only",
                "environment_is_production": last["environment_is_production"],
                "companies_listed": last["companies_listed"],
                "companies_listing_complete": last["companies_listing_complete"],
                "unpinned_companies": last["unpinned_companies"],
                "out_of_scope_companies": last["out_of_scope_companies"],
                "pinned_name_mismatch": last["pinned_name_mismatch"],
                "company_proof": last["company_proof"],
                "pinned_companies_missing": last["pinned_companies_missing"],
                "mode": mode,
                "modes_per_pass": [sorted({u["mode"] for c in r["companies"]
                                           for u in c["units"] if u.get("mode")})
                                   for r in reports],
                "requests_per_pass": [sum(u.get("requests") or 0 for c in r["companies"]
                                          for u in c["units"]) for r in reports],
                "keys_refetched_per_pass": [sum(u.get("keys_refetched") or 0
                                                for c in r["companies"] for u in c["units"])
                                            for r in reports],
                "lines_skipped_per_pass": [sum(u.get("lines_skipped") or 0
                                               for c in r["companies"] for u in c["units"])
                                           for r in reports],
                "rows_changed_per_pass": [sum(u.get("rows_changed") or 0
                                              for c in r["companies"] for u in c["units"])
                                          for r in reports],
                # Refused continuations: units failed nextlink_refused plus documents
                # whose lines continuation was refused (live checks require 0).
                "nextlink_refused_per_pass": [
                    sum((u.get("reason") == "nextlink_refused") + (u.get("nextlink_refused") or 0)
                        for c in r["companies"] for u in c["units"]) for r in reports],
                "companies_captured": len(last["companies"]),
                "books": sorted(c["book"] for c in last["companies"]),
                "companies": [{"company_index": index, "book": c["book"],
                               "company_key": company_key(c["company_id"]),
                               "lineage": c["lineage"],
                               "units": [{k: u.get(k) for k in _UNIT_FIELDS if k in u}
                                         for u in c["units"]]}
                              for index, c in enumerate(last["companies"])],
                "units_total": last["units_total"], "units_reached": last["units_reached"],
                "units_complete": last["units_complete"],
                "units_unserved": last["units_unserved"],
                "unserved_units": last["unserved_units"],
                "units_not_exposed": last["units_not_exposed"],
                "units_reused": last["units_reused"], "units_stale": last["units_stale"],
                "capture_age_seconds": last["capture_age_seconds"],
                "lines_via_filter_per_pass": [sum(u.get("lines_via_filter") or 0
                                                  for c in r["companies"] for u in c["units"])
                                              for r in reports],
                "all_complete": last["all_complete"], "stopped_reason": last["stopped_reason"],
                "throttle": last["throttle"], "passes": passes,
                "resumed_per_pass": resumed,
                "new_versions_per_pass": [sum(u.get("new_versions") or 0
                                              for c in r["companies"] for u in c["units"])
                                          for r in reports],
                "capture_seconds": round(seconds, 3),
                "atomic_upstream_snapshot": False, "deletions_proven": False}
            handles: list[str] = []
            for company in last["companies"]:
                for unit in company["units"]:
                    if len(handles) >= resolve_sample or unit.get("state") == "not_visible":
                        continue
                    listing = store.records(actor, "bc", unit["unit"],
                                            limit=min(1000, resolve_sample - len(handles)))
                    handles += [item["citation"] for item in listing["items"]]
            started = monotonic()
            resolved = store.resolve_many(handles, actor) if handles else {"ok_count": 0}
            rows = store.claim_rows(actor)
            row_ok = sum(1 for h in handles if isinstance((rows(h) or {}).get("row"), Mapping))
            result["store_payload_bytes"] = sum(u.get("payload_bytes") or 0
                                                for c in last["companies"] for u in c["units"])
            result["store_pruned_versions"] = sum((u.get("retention") or {}).get(
                "pruned_versions", 0) for c in last["companies"] for u in c["units"])
            result.update({"handles_resolved": len(handles),
                           "handles_ok": resolved["ok_count"],
                           "claim_rows_ok": row_ok,
                           "resolve_all_ok": resolved["ok_count"] == len(handles) == row_ok,
                           "resolve_seconds": round(monotonic() - started, 3)})
        finally:
            store.close()
    result["requests_sent"] = transport.requests_sent
    result["get_retries"] = api.retry.retries if api.retry is not None else 0
    result["retry_sleep_seconds"] = round(api.retry.slept_seconds, 3) \
        if api.retry is not None else 0.0
    return result


def _unfinished(report: Mapping[str, Any]) -> bool:
    """A capture pass that left work to continue: it stopped (budget, throttle) or a unit it
    reached is still resumable (a share cut, a paused read)."""
    return report.get("stopped_reason") is not None or any(
        u.get("resumable") or u.get("state") == "in_progress"
        for c in report.get("companies") or () for u in c.get("units") or ())


def capture_unit(company: Mapping[str, Any], entity_set: str) -> Mapping[str, Any] | None:
    """A ``capture_probe`` report company's unit of ``entity_set`` (None: never reached)."""
    return next((u for u in company.get("units") or () if u.get("entity_set") == entity_set),
                None)


def metadata_drift(environ: Mapping[str, str] | None = None, *, sender=None,
                   retry: BoundedBackoff | None = None, out: str | Path | None = None
                   ) -> dict[str, Any]:
    """One GET of the API v2.0 ``$metadata``, compared with the pinned document and the
    compiled schema (``ontology.generator.bc.drift``): names, digests and counts, never a
    record. ``out``: also write the document there (the credentialed re-pin:
    ``python3 -m ontology.generator.bc pin FILE``)."""
    from ontology.generator import bc as generator_bc

    api, transport, tenant_id, environment = build_bc(environ, sender=sender, retry=retry)
    env = environment.lower()
    audience = LiveSourceAudience({f"bc:{env}:*": ["probe:operator"]}, tenant="probe")
    actor = Actor("probe", frozenset({"probe:operator"}), "restricted")
    reader = BusinessCentralReader(api, audience, tenant_id=tenant_id, environment=environment)
    raw = reader.metadata(actor)
    if out is not None:
        Path(out).write_bytes(raw)
    report = generator_bc.drift_document(raw)
    report.update({"source": "business-central", "evidence_label": reader.evidence_label,
                   "read_only_guard": "GET-only; token POST only",
                   "environment_is_production": env == "production",
                   "requests_sent": transport.requests_sent})
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender=None, retry: BoundedBackoff | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.bc_read",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("probe", help="read-only counts/shapes (no values printed)")
    run.add_argument("--entity", default="purchaseOrders")
    run.add_argument("--case-entities", action="store_true",
                     help="per company: counts, field names, lines and citation round-trips "
                          "for " + ", ".join(CASE_ENTITY_SETS))
    run.add_argument("--finance-entities", action="store_true",
                     help="per company: the same for " + ", ".join(FINANCE_ENTITY_SETS)
                          + ", plus whether BC accepts each $select allowlist")
    run.add_argument("--sample", type=int, default=1, help="$top per entity set (1..5)")
    run.add_argument("--expected-refusal", action="append", default=[],
                     metavar="COMPANY_BOOK:ENTITY_SET:BC_CODE",
                     help="repeatable: a finance set BC may refuse with this code for this "
                          "pinned company (bc_company_books.json) while another serves it "
                          "(reported, not counted)")
    cap = commands.add_parser("capture", help="versioned, resumable capture of every pinned "
                              "company's entity sets into a capture store (counts only)")
    cap.add_argument("--db", default=None, help="capture store file (default: a temp file)")
    cap.add_argument("--budget-seconds", type=float, default=None)
    cap.add_argument("--entity-set", action="append", default=None,
                     help="repeatable; default: " + ", ".join(CAPTURE_ENTITY_SETS))
    cap.add_argument("--page-size", type=int, default=CAPTURE_PAGE_SIZE)
    cap.add_argument("--resolve-sample", type=int, default=200)
    cap.add_argument("--passes", type=int, default=1,
                     help="capture passes (a second pass shows unchanged records add no version)")
    cap.add_argument("--mode", choices=CAPTURE_MODES, default="auto",
                     help="full: read every unit in full; auto (default): modified-since "
                          "reads after the first full pass, a key-only full pass weekly")
    cap.add_argument("--generated-first", action="store_true",
                     help="root every company's capture on the sets the generated BC "
                          "ontology binds (ontology/schema/generated/meta/bc_coverage.json) "
                          "and read their lines first, so a budget stop keeps them")
    cap.add_argument("--key-pass-last", action="store_true",
                     help="auto mode: the last pass is the key-only full pass")
    cap.add_argument("--live-audience", default=None,
                     help="plx-live-source-audience/v1 file: bind --db to its tenant and "
                          "capture as --principal, as a host captures (its readers resolve "
                          "the store only then); default a service audience bound to the BC "
                          "tenant itself")
    cap.add_argument("--principal", default=None,
                     help="the principal --live-audience grants (required with it)")
    drift = commands.add_parser("metadata-drift", help="the API v2.0 $metadata against the "
                                "pinned document and the schema (names and counts only)")
    drift.add_argument("--out", default=None,
                       help="also write the $metadata document to this file (to re-pin)")
    args = parser.parse_args(argv)
    from ontology.generator.bc import MetadataError

    try:
        if args.command == "metadata-drift":
            result = metadata_drift(environ, sender=sender, retry=retry, out=args.out)
        elif args.command == "capture":
            sets, roots = tuple(args.entity_set or CAPTURE_ENTITY_SETS), CAPTURE_ROOT_SETS
            if args.generated_first:
                from ontology.generator.bc import generated_capture_sets
                first = tuple(s for s in generated_capture_sets() if s in sets)
                sets = first + tuple(s for s in sets if s not in first)
                roots = (*CAPTURE_ROOT_SETS, *first)
            if (args.live_audience is None) != (args.principal is None):
                raise ValueError("--live-audience and --principal go together")
            audience = actor = None
            if args.live_audience is not None:
                audience = LiveSourceAudience.from_file(args.live_audience)
                actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
            result = capture_probe(environ, sender=sender, db=args.db,
                                   audience=audience, actor=actor,
                                   budget_seconds=args.budget_seconds,
                                   entity_sets=sets, roots=roots,
                                   page_size=args.page_size,
                                   resolve_sample=args.resolve_sample, passes=args.passes,
                                   mode=args.mode, key_pass_last=args.key_pass_last,
                                   retry=retry if retry is not None else
                                   BoundedBackoff(budget_seconds=CAPTURE_RETRY_BUDGET_SECONDS))
        else:
            result = probe(environ, sender=sender, entity_set=args.entity,
                           case_entities=args.case_entities, sample=args.sample, retry=retry,
                           finance_entities=args.finance_entities,
                           expected_refusals=args.expected_refusal)
    except ReadOnlyGuardError as exc:
        print(json.dumps({"error": "read_only_guard", "message": str(exc)}), file=sys.stderr)
        return 2
    except MetadataError as exc:   # its fixed class is published, the message is withheld
        print(json.dumps({"error": "MetadataError", "error_code": exc.code,
                          "message": str(exc)}), file=sys.stderr)
        return 1
    except CaptureError as exc:   # its stable code names the refusal (binding_mismatch, ...)
        print(json.dumps({"error": "CaptureError", "error_code": exc.code,
                          "message": str(exc)}), file=sys.stderr)
        return 1
    except (SourceUnavailable, NotVisible, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


__all__ = ["BOOKS_FILE", "BcCapture", "BusinessCentralReader", "CAPTURE_ENTITY_SETS",
           "PROBE_UNIT_PAUSES", "PROBE_WALL_SECONDS", "ProbeBackoff", "expected_refusal",
           "CAPTURE_MODES", "CAPTURE_PAGE_SIZE", "FINANCE_ENTITY_SETS", "CAPTURE_RECORD_CAPS", "FULL_PASS_INTERVAL_DAYS",
           "POSTED_DOCUMENT_STATUSES", "WATERMARK_OVERLAP", "CAPTURE_RETRY_BUDGET_SECONDS", "CASE_ENTITY_SETS",
           "FRESH_SINCE_ENV", "RETRY_BUDGET_SECONDS", "build_bc", "capture_probe", "capture_unit",
           "citation",
           "company_key",
           "load_company_books", "load_ledger_scope", "main", "metadata_drift", "probe", "session_fresh_since"]


if __name__ == "__main__":
    raise SystemExit(main())
