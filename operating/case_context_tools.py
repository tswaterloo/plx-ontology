"""Shared context on the case host: ingestion status and document metadata proposals.

Two read-only views that let any MCP client (the case host first) work on the
same context employees see, through the case runtime's existing permission
checks. Neither view writes anything; both act for the session actor after
the case runtime's employee gate (``CaseRuntime.employee_gate``).

``ingest_status`` -- the ingestion store (``operating.ingestion``) read
read-only (sqlite ``mode=ro``: the store file is never created or modified, the run
lock never probed; WAL sidecars may appear; a missing or schema-less
store is ``runs.store: not_initialised``) in the ``plx-ingest-status/v1`` unit
shape, trimmed to the units the actor may see. A unit is shown only when the
actor may read its configured source scope over every key the reader checks:

* ``portal_relation``: the source mirror's audience for the relation.
* ``mail_folder``: the live-source audience over ``mail:<identity>`` for every
  directory identity of the mailbox (object id, UPN, mail, proxies; the most
  restrictive wins, as ``GraphMailReader._open``), taken from the owner ACL the
  reader recorded on the unit's stored messages, plus that owner ACL (an
  employee must own the mailbox). No recorded identity set: withheld.
* ``sharepoint_folder``: the live-source audience for the folder's keys
  (``sharepoint:<drive>:<path>`` and ``<path>/``).
* ``bc_entity_set``: the live-source audience for ``bc:<env>:companies`` (the
  company listing and a never-run unit) or ``bc:<env>:<company>:<entity set>``;
  the company of an ``all``-companies unit is recovered from the unit's own
  stored items (hash-checked against the unit id), else the unit is withheld.

Anything undecidable is withheld: default deny. Withheld units are counted
(over the unfiltered set), never named. Counts are what the ingest principal
read; a SharePoint unit is shown without counts, lag or cursor signals
(``counts_visible: false``) when items carry their own ACL for this actor
(``source_acl_required`` keys, or any folder for an employee) or when an
audience or classification pattern under the folder does not grant the actor
(a narrower pattern wins below the folder).

``document_metadata`` -- ``operating.doc_metadata.build_proposals`` over one
attached original's current extraction (``OriginalStore.text_document``:
audience, live source gate and byte re-hash first). Printed record numbers are
looked up with ``plx.find`` for the same actor, on the case host's one discovery
path (``operating.ontology_host.CaseHostTools``: the identifier grammar of
``ontology/schema/identifiers.json`` and the ``ontology.resolve`` resolver; the
read policy filters before anything is counted or ranked). Its candidates are
reported, never selected: an ambiguous answer stays several ranked candidates.
Proposals only: nothing is saved or applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from integrations.source_mirror import MirrorError
from ontology.core import LEVELS, Actor

from . import ingestion
from .cases import CitationUnresolvable
from .doc_metadata import OCR_WARNING, build_proposals
from .ontology_host import CaseHostTools


INGEST_VIEW_SCHEMA = "plx-case-ingest-status/v1"
DOCUMENT_VIEW_SCHEMA = "plx-case-document-metadata/v1"
_WITHHELD_REASON = re.compile(r"^withheld:\d+$")

# The ``plx.find`` arguments for the record a doc_metadata link points at: an
# identifier kind of ontology/schema/identifiers.json (its alias kinds included) and,
# where the link names one, the object type. Links without an identifier kind (bulk
# lot, customer, vendor) stay ``lookup_unavailable``.
LINK_FIND_ARGUMENTS: dict[str, dict[str, str]] = {
    "assembly_order": {"identifier_kind": "ao_code", "object_type": "AssemblyOrder"},
    "sales_order": {"identifier_kind": "so_number", "object_type": "SalesOrder"},
    "petra_po": {"identifier_kind": "petra_po", "object_type": "PurchaseOrder"},
    "lot": {"identifier_kind": "lot_number"},
}
MAX_LINK_CANDIDATES = 10
_FIND_STATUS = {"found": "one_candidate", "ambiguous": "several_candidates",
                "not_found": "no_visible_candidate"}


@dataclass(frozen=True)
class IngestionStatusSource:
    """Where the host's ingestion store is and which sources it was configured with."""

    state_dir: Path
    sources: tuple[Mapping[str, Any], ...]
    tenant: str

    @classmethod
    def from_file(cls, state_dir: str | Path, path: str | Path, tenant: str,
                  environ: Mapping[str, str] | None = None) -> "IngestionStatusSource":
        file_tenant, _actor, sources = ingestion.load_sources_file(path, environ)
        if file_tenant != tenant:
            raise ValueError("ingest_sources_tenant_mismatch")
        return cls(Path(state_dir), tuple(sources), tenant)


# ------------------------------------------------------------------ ingest status

class _ReadOnlyStore:
    """The ingest store opened read-only (``mode=ro``): the file is never created or
    modified and never locked; a file without the schema reads as not initialised."""

    def __init__(self, state_dir: Path) -> None:
        path = Path(state_dir) / ingestion.STORE_DIR / ingestion.STORE_FILE
        self.db: sqlite3.Connection | None = None
        if path.is_file():
            # mode=ro never creates or modifies the store file; on a WAL store SQLite may
            # still create the -wal/-shm sidecars to read it consistently (immutable=1
            # would avoid them but could read a torn state while a run writes).
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA busy_timeout = 2000")
            tables = {row[0] for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"run", "run_unit", "item"} <= tables:  # created, schema not yet written
                self.db.close()
                self.db = None

    def close(self) -> None:
        if self.db is not None:
            self.db.close()

    def unit_record(self, unit_id: str) -> dict[str, Any] | None:
        """The newest stored item record of one unit (for scope decisions only)."""
        row = self.db.execute("SELECT record_json FROM item WHERE unit_id = ? "
                              "ORDER BY last_seen_run DESC, item_key LIMIT 1",
                              (unit_id,)).fetchone() if self.db is not None else None
        if row is None:
            return None
        record = json.loads(row["record_json"])
        return record if isinstance(record, dict) else None


def _never_run(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {"unit_id": spec["id"], "source_id": spec["id"], "kind": spec["kind"],
            "completeness_basis": ingestion.COMPLETENESS_BASIS[spec["kind"]],
            "state": "never_run", "last_run_id": None, "reasons": [], "counts": {},
            "lag": {}, "cursor_advanced": False, "items_current": 0, "items_absent": 0,
            "last_complete_age_seconds": None}


def _read_status(store: _ReadOnlyStore, sources: Sequence[Mapping[str, Any]],
                 now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(runs, units) in the ``plx-ingest-status/v1`` unit shape, read-only.

    Unlike ``ingestion.status`` this never probes the run lock (a probe would race
    a starting run), so a unit or run still marked running is reported as
    ``running_or_interrupted``: without the lock the two cannot be told apart.
    """
    db = store.db
    if db is None:
        return ({"store": "not_initialised", "total": 0, "running_or_interrupted": 0,
                 "interrupted": 0, "last_run_id": None, "last_run_state": None},
                [_never_run(spec) for spec in sources])
    runs = db.execute("SELECT COUNT(*) AS total, "
                      "COALESCE(SUM(state = 'interrupted'), 0) AS interrupted, "
                      "COALESCE(SUM(state = 'running'), 0) AS running, "
                      "MAX(run_id) AS last FROM run").fetchone()
    last_state = None
    if runs["last"] is not None:
        last_state = db.execute("SELECT state FROM run WHERE run_id = ?",
                                (runs["last"],)).fetchone()["state"]
    latest: dict[str, dict[str, Any]] = {}
    for row in db.execute("SELECT * FROM run_unit ORDER BY run_id"):
        latest[row["unit_id"]] = dict(row)
    complete_at = {row["unit_id"]: row["at"] for row in db.execute(
        "SELECT unit_id, MAX(finished_at) AS at FROM run_unit WHERE state = 'complete' "
        "GROUP BY unit_id")}
    items = {row["unit_id"]: (row["current"], row["absent"]) for row in db.execute(
        "SELECT unit_id, COALESCE(SUM(absent_since_run IS NULL), 0) AS current, "
        "COALESCE(SUM(absent_since_run IS NOT NULL), 0) AS absent FROM item "
        "GROUP BY unit_id")}
    units = []
    for unit_id, row in sorted(latest.items()):
        state = "running_or_interrupted" if row["state"] == "running" else row["state"]
        before = json.loads(row["cursor_before_json"]) if row["cursor_before_json"] else None
        after = json.loads(row["cursor_after_json"]) if row["cursor_after_json"] else None
        done = ingestion._instant(complete_at.get(unit_id))
        current, absent = items.get(unit_id, (0, 0))
        units.append({
            "unit_id": unit_id, "source_id": row["source_id"], "kind": row["kind"],
            "completeness_basis": ingestion.COMPLETENESS_BASIS[row["kind"]],
            "state": state, "last_run_id": row["run_id"],
            "reasons": [r for r in json.loads(row["reasons_json"])
                        if ingestion._REASON.fullmatch(r)],
            "counts": {k: v for k, v in json.loads(row["counts_json"]).items()
                       if isinstance(v, int) and not isinstance(v, bool)},
            "lag": ingestion._lag_numbers(json.loads(row["lag_json"])),
            "cursor_advanced": (after is not None and after != before
                                and state not in ("interrupted", "running_or_interrupted")),
            "items_current": current, "items_absent": absent,
            "last_complete_age_seconds": (round((now - done).total_seconds(), 3)
                                          if done else None)})
    seen = {u["source_id"] for u in units}
    units += [_never_run(spec) for spec in sources if spec["id"] not in seen]
    return ({"store": "read", "total": runs["total"], "interrupted": runs["interrupted"],
             "running_or_interrupted": runs["running"], "last_run_id": runs["last"],
             "last_run_state": ("running_or_interrupted" if last_state == "running"
                                else last_state)}, units)


def _folder_keys(drive: str, path: str) -> list[str]:
    canonical = path.rstrip("/") or "/"
    keys = [f"sharepoint:{drive}:{canonical}"]
    if canonical != "/":
        keys.append(f"sharepoint:{drive}:{canonical}/")
    return keys


def _subtree_uniform(audience: Any, actor: Actor, prefix: str) -> bool:
    """Whether every audience and classification pattern that can decide a key at
    or under ``prefix`` grants ``actor`` within its clearance (most specific wins,
    so a narrower pattern under the folder can deny what the folder allows).

    Reads the audience's pattern tables (``integrations.live_source_guard``):
    patterns under the prefix, and wildcard patterns whose stem is a prefix of it.
    Unmatched keys are default-denied for every reader, ingest principal included.
    """
    prefix = prefix.lower()

    def applies(pattern: str) -> bool:
        stem = pattern[:-1] if pattern.endswith("*") else pattern
        return stem.startswith(prefix) or (pattern.endswith("*") and prefix.startswith(stem))

    clearance = LEVELS[actor.clearance]
    for pattern, principals in audience._audiences.items():
        if applies(pattern) and not principals & actor.principals:
            return False
    if LEVELS[audience.default_classification] > clearance:
        return False
    if any(applies(pattern) for pattern in audience._acl_required):
        return False  # items under the folder carry their own ACL for some reader
    return all(LEVELS[level] <= clearance for pattern, level
               in audience._classifications.items() if applies(pattern))


def _bc_company(store: _ReadOnlyStore, unit_id: str, source_id: str,
                spec: Mapping[str, Any]) -> str | None:
    """The company a per-company BC unit covers, or None when undecidable."""
    companies = spec.get("companies")
    candidates: list[str] = list(companies) if isinstance(companies, list) else []
    if companies == "all":
        record = store.unit_record(unit_id)
        company = record.get("company_id") if record else None
        if isinstance(company, str):
            candidates = [company.lower()]
    for company in candidates:
        if f"{source_id}.{sha256(company.encode()).hexdigest()[:10]}" == unit_id:
            return company
    return None


def _mailbox_acl(store: _ReadOnlyStore, unit_id: str, mailbox: str) -> dict[str, Any] | None:
    """The mailbox-owner ACL (every directory identity) stored with the unit's
    messages, when it names the configured mailbox; else None (undecidable)."""
    record = store.unit_record(unit_id)
    acl = record.get("source_acl") if record else None
    if (not isinstance(acl, Mapping) or acl.get("kind") != "mailbox_owner"
            or acl.get("status") != "derived" or not isinstance(acl.get("identities"), list)):
        return None
    identities = [i.lower() for i in acl["identities"] if isinstance(i, str) and i]
    if mailbox.lower() not in identities:
        return None
    return {"status": "derived", "kind": "mailbox_owner", "identities": sorted(identities)}


def _unit_scope(runtime: Any, actor: Actor, unit: Mapping[str, Any],
                spec: Mapping[str, Any] | None, store: _ReadOnlyStore
                ) -> tuple[bool, bool, str]:
    """(visible, counts_visible, basis) of one status unit for ``actor``."""
    if spec is None or spec["kind"] != unit["kind"]:
        return False, False, "source_not_configured"
    kind, live = spec["kind"], runtime.live
    if kind == "portal_relation":
        # The mirror's own audience (re-read on every use), as every mirror read uses.
        allowed = runtime.mirror._audience().allows(actor, spec["relation"])
        return allowed, allowed, "source_mirror_audience"
    if live is None:
        return False, False, "live_source_audience_not_configured"
    audience = live.audience
    if kind == "mail_folder":
        mailbox = spec.get("mailbox")
        if not isinstance(mailbox, str) or not mailbox:
            return False, False, "mailbox_not_configured"
        # GraphMailReader._open: every directory identity must be allowed (most
        # restrictive wins), so decide over the identity set the reader recorded.
        acl = _mailbox_acl(store, unit["unit_id"], mailbox)
        if acl is None:
            return False, False, "mailbox_identities_not_recorded"
        keys = [f"mail:{identity}" for identity in acl["identities"]]
        allowed = (audience.visible_all(actor, keys, acl)
                   and runtime._employee_acl_denial(actor, "graph-mail",
                                                    {"source_acl": acl}) is None)
        return allowed, allowed, "live_source_audience_and_owner_acl"
    if kind == "sharepoint_folder":
        drive = spec.get("drive_id")
        if not isinstance(drive, str) or not drive:
            return False, False, "drive_not_configured"
        keys = _folder_keys(drive, spec["path"])
        allowed = audience.allows_all(actor, keys)
        uniform = _subtree_uniform(audience, actor, keys[0])
        item_acl = (audience.source_acl_required_any(keys)
                    or runtime.employee_gate.entitlement(actor) is not None)
        return allowed, allowed and uniform and not item_acl, "live_source_audience"
    if live.bc is None:
        return False, False, "bc_reader_not_configured"
    source_id, unit_id = spec["id"], unit["unit_id"]
    if unit_id in (source_id, f"{source_id}.companies"):
        resource = live.bc.resource()
    else:
        company = _bc_company(store, unit_id, source_id, spec)
        if company is None:
            return False, False, "bc_company_undetermined"
        resource = live.bc.resource(company, spec["entity_set"])
    allowed = audience.allows(actor, resource)
    return allowed, allowed, "live_source_audience"


def _summary(units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    states = [u["state"] for u in units]
    summary: dict[str, Any] = {state: states.count(state) for state in
                               ("complete", "incomplete", "interrupted",
                                "running_or_interrupted", "never_run")}
    summary["units"] = len(units)
    summary["items_current"] = sum(u["items_current"] for u in units
                                   if u.get("counts_visible"))
    return summary


CAVEATS = (
    "Portal pages are not an atomic snapshot; deletions are never proven.",
    "Mail completeness is by received time only; a complete mail unit is never proof "
    "that a message is absent.",
    "Withheld items are inaccessible to the ingest principal, not absent; absence is "
    "marked only after a clean full pass and is never a deletion proof.",
    "BC has no modified-since read in the closed grammar: every run is a bounded full scan.",
    "Read without the run lock: a unit still marked running is running_or_interrupted.")


def ingest_status(runtime: Any, actor: Actor, host: IngestionStatusSource, *,
                  kind: str | None = None, source_id: str | None = None,
                  clock: Any = None) -> dict[str, Any]:
    """Counts-only ingestion status trimmed to what ``actor`` may see (see module doc)."""
    actor = runtime.employee_gate(actor)  # refuses an employee it cannot serve
    if actor.tenant != host.tenant:
        raise PermissionError("ingest_status_tenant_mismatch")
    specs = {spec["id"]: spec for spec in host.sources}
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    store = _ReadOnlyStore(host.state_dir)
    visible: list[dict[str, Any]] = []
    withheld = 0
    try:
        runs, units = _read_status(store, host.sources, now)
        for unit in units:
            shown, counts_shown, basis = _unit_scope(runtime, actor, unit,
                                                     specs.get(unit["source_id"]), store)
            if not shown:
                withheld += 1  # over the unfiltered set: filters are no existence oracle
                continue
            if kind is not None and unit["kind"] != kind:
                continue
            if source_id is not None and unit["source_id"] != source_id:
                continue
            entry = dict(unit, visibility_basis=basis, counts_visible=counts_shown)
            if not counts_shown:
                entry.update(counts={}, items_current=None, items_absent=None, lag={},
                             cursor_advanced=None,
                             reasons=sorted({"withheld" if _WITHHELD_REASON.fullmatch(r)
                                             else r for r in unit["reasons"]}))
            visible.append(entry)
    finally:
        store.close()
    return {"schema": INGEST_VIEW_SCHEMA, "status_schema": ingestion.STATUS_SCHEMA,
            "runs": runs, "units": visible, "summary": _summary(visible),
            "visibility": {
                "units_visible": len(visible), "units_withheld": withheld,
                "units_withheld_scope": "all configured and stored units, before the "
                                        "kind/source_id filters",
                "rule": "a unit is shown only when the session actor may read its "
                        "configured source scope (mirror or live-source audience over "
                        "every key the reader checks, mailbox owner); undecidable units "
                        "are withheld and only counted; counts, lag and cursor signals "
                        "are hidden where items carry their own ACL for this actor or a "
                        "narrower audience pattern under the folder does not grant it"},
            "counts_scope": "items read by the ingest principal, not by this actor",
            "completeness_basis": dict(ingestion.COMPLETENESS_BASIS),
            "completeness_rule": "complete only when a unit has no reason; a cap, "
                                 "truncation, error or unfinished traversal is incomplete",
            "caveats": list(CAVEATS), "business_system_writes": 0}


# ------------------------------------------------------------------ document metadata

def _page_citation(document_id: str, version: int, page: int) -> str:
    base = document_id.split("#", 1)[0]
    return (f"{base}#page={page}" if version == 1
            else f"{base}#extraction={version}&page={page}")


def _add_page_citations(value: Any, document_id: str, version: int) -> None:
    """Every evidence span with a page also names the citable page."""
    if isinstance(value, dict):
        if (type(value.get("page")) is int and isinstance(value.get("provenance"), str)
                and value["provenance"].startswith(document_id)):
            value["page_citation"] = _page_citation(document_id, version, value["page"])
        for item in value.values():
            _add_page_citations(item, document_id, version)
    elif isinstance(value, list):
        for item in value:
            _add_page_citations(item, document_id, version)


def link_find_arguments(report: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``plx.find`` arguments that look up one linked-record report, or None when
    the link has no identifier kind."""
    arguments = LINK_FIND_ARGUMENTS.get(report["link"])
    if arguments is None:
        return None
    return dict(arguments, query=report["values"][0], limit=MAX_LINK_CANDIDATES)


def _link_candidates(find: Callable[[dict[str, Any]], dict[str, Any]],
                     report: dict[str, Any]) -> None:
    """``report``'s candidates from one ``plx.find`` answer: ranked, never selected."""
    arguments = link_find_arguments(report)
    if arguments is None:
        report["record_lookup"] = "no_identifier_kind_for_this_link"
        return
    try:
        found = find(arguments)
    except MirrorError as exc:  # the read policy could not read the mirror's audience
        report.update(record_status="lookup_failed", record_lookup=f"source_mirror_{exc.code}")
        return
    if "error" in found:  # visible, never an empty candidate list
        report.update(record_status="lookup_failed",
                      record_lookup=f"plx_find_{found['error']['code']}")
        return
    records = [{"object_id": item["object"]["object_id"],
                "object_type": item["object"]["object_type"], "rank": item["rank"],
                "tier": item["tier"], "reason": item["reason"],
                "identifier_kind": item["matched"]["identifier_kind"],
                "binding": item["matched"]["binding"],
                "property": item["matched"]["property"],
                "citation": item["matched"]["raw"]["origin"]["citation"]}
               for item in found["candidates"]]
    report.update(records=records, records_truncated=found["completeness"]["truncated"],
                  record_status=_FIND_STATUS[found["status"]], record_lookup="plx.find",
                  selection_required=True, lookup_gaps=_gap_counts(found["gaps"]))


def _gap_counts(gaps: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """``plx.find``'s typed gaps counted by ``state:reason`` (what was not searched or
    not established), so a no-match is never read as absence."""
    counts: dict[str, int] = {}
    for gap in gaps:
        key = f"{gap['state']}:{gap['reason']}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def document_metadata(runtime: Any, actor: Actor, citation: str, *,
                      model_suggestions: Sequence[Mapping[str, Any]] = (),
                      max_staleness: float | None = None) -> dict[str, Any]:
    """Portal-taxonomy metadata proposals for one attached original (see module doc)."""
    document = runtime.originals.text_document(citation, actor)
    view = document["view"]
    version = view.get("extraction_version", 1)
    warnings = list(document["warnings"])
    if document["ocr_pages"] and OCR_WARNING not in warnings:
        warnings.append(OCR_WARNING)  # machine-read text is review-grade evidence
    attrs: dict[str, Any] = {"title": view["filename"], "text": document["text"],
                             "extraction": {"pages": document["pages"],
                                            "warnings": warnings}}
    provenance = view.get("provenance") or {}
    source = native_id = None
    if (provenance.get("source") == "sharepoint-drive"
            and isinstance(provenance.get("drive_id"), str)
            and isinstance(provenance.get("item_id"), str)):
        source = "sharepoint-drive"
        native_id = f"{provenance['drive_id']}:{provenance['item_id']}"
        attrs.update(source_container=provenance["drive_id"],
                     source_revision=provenance.get("etag"))
    result = build_proposals(document_id=citation, revision=view["sha256"],
                             fingerprint=document["text_sha256"], attrs=attrs,
                             locator=citation, lookup=None,
                             model_suggestions=list(model_suggestions),
                             current_columns=_captured_columns(runtime, actor, provenance,
                                                               view["sha256"])
                             if source else None,
                             source=source, native_id=native_id)
    host: CaseHostTools | None = None

    def find(arguments: dict[str, Any]) -> dict[str, Any]:
        # The case host's plx.find path: the session actor's tools, the employee-gated
        # actor's read policy decided for this call.
        nonlocal host
        if host is None:
            host = CaseHostTools(runtime, actor, max_staleness=max_staleness)
        return host.call("plx.find", arguments, runtime.employee_gate(actor))
    try:
        for report in result["linked_records"]:
            if report.get("record_status") == "lookup_unavailable":
                _link_candidates(find, report)
    finally:
        if host is not None:
            host.close()
    _add_page_citations(result, citation, version)
    result.update(view_schema=DOCUMENT_VIEW_SCHEMA, extraction_version=version,
                  original_sha256=view["sha256"], extraction_text_sha256=document["text_sha256"],
                  span_offsets="characters of the whole extraction text of this version",
                  applied=False, saved=False)
    if result["current_columns"].get("status") == "not_read":
        result["current_columns"]["reason"] = (
            "no SharePoint listItem fields snapshot: the host's SharePoint capture holds "
            "none of this version for this reader, so current columns are not compared")
    return result


def _captured_columns(runtime: Any, actor: Actor, provenance: Mapping[str, Any],
                      content_sha256: str) -> Mapping[str, Any] | None:
    """The listItem columns the host's SharePoint capture read for the very version (item,
    eTag and bytes) an original was attached from, through the capture's gate for
    ``actor`` (``CaseCaptures.document``); None when it holds none for this reader."""
    from integrations.capture_sharepoint import version_handle

    document = getattr(getattr(runtime, "captures", None), "document", None)
    etag = provenance.get("etag")
    if document is None or not isinstance(etag, str) or not etag:
        return None
    try:
        found = document(version_handle(provenance["drive_id"], provenance["item_id"], etag)
                         + "#sha256=" + content_sha256, actor)
    except (CitationUnresolvable, ValueError):
        return None
    snapshot = found.get("row", {}).get("list_item_fields") if found else None
    if not isinstance(snapshot, Mapping):
        return None
    if "field_names" not in snapshot and isinstance(snapshot.get("fields"), Mapping):
        # Captured before the capture recorded its request (DocumentCategory only): only
        # the columns it holds were read.
        snapshot = {**snapshot, "field_names": sorted(snapshot["fields"])}
    return snapshot


__all__ = ["DOCUMENT_VIEW_SCHEMA", "INGEST_VIEW_SCHEMA", "IngestionStatusSource",
           "LINK_FIND_ARGUMENTS", "document_metadata", "ingest_status", "link_find_arguments"]
