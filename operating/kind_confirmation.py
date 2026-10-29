"""Confirmation of an original's document kind and Portal-taxonomy fields by an employee session.

A model or extractor *proposes* a document's metadata (``plx.document_fields`` mode propose:
``operating.doc_metadata.build_proposals`` over an attached original, in the Portal's own
column set, ``operating.doc_metadata_taxonomy``). A proposal is never a fact. It becomes a
**confirmed** fact only here, through the answer to a confirmation form the Workbench itself
sends to the session's MCP client.

What is enforced, and what is not: a confirmation requires a session holding exactly one
employee principal and no agent principal, whose client declared elicitation at initialize.
Principal labels are host-configured (not PLX authentication) and the client is whatever the
host connected, so **the Workbench cannot prove that a person answered**: a scripted client
with an elicitation handler answers its own form. Hosts must declare elicitation only for
clients that show the form to a person. Within that boundary:

* **Employee principal only.** The session must hold exactly one ``employee:<entra-oid>``
  principal and no agent or service principal (``agent:``/``sp_``, the one rule of
  ``operating.entitlements``). A session holding an agent principal can neither ask nor answer
  (no self-consent).
* **A form, not a sentence.** ``request`` builds the proposal itself, server-side, for the
  requesting employee and returns an MCP ``elicitation/create`` form (``FORM_MODE``) addressed
  by the id of a Workbench confirmation form (``operating.confirmation_forms``, purpose
  ``document_fields``: the one form id, TTL, single-use answer and store the playbook adoption
  form uses too). The host sends that request to the *client* on the MCP transport; the client
  shows it to the person and returns the person's answer as the JSON-RPC response to that id
  (``handle_response``). The id never appears in a tool result, the store keeps only its
  SHA-256, and it is single use: there is no confirmation text an agent could copy, record as a
  finding or replay through any tool. An open form survives a host restart in this module's
  SQLite file and is answered by the requesting employee once. A client that did not declare the
  ``elicitation`` capability gets no form and no pending request (refused,
  ``human_confirmation_surface_unavailable``). The form request is written to the client
  before the tool result (``mcp_server.serve_stdio``), i.e. while the tool call is in flight.
* **Binds the exact proposal and original.** The proposal digest (``proposal_digest``) covers
  the original's file citation, its SHA-256, provenance (SharePoint drive, item, eTag, cTag,
  hashes), the extraction version and text hash, the model suggestions and every proposed
  column (status, value, basis). ``request`` refuses when the caller's
  ``expected_proposal_digest`` differs from the current one; ``complete`` rebuilds the
  proposal for the answering employee and refuses when the digest moved (re-extraction, other
  model suggestions, other bytes) or when a newer request for the same original superseded
  this one. The confirmation records the digest it was given for.
* **Reader permission.** Every step reads the original through the case runtime for the
  session actor (``CaseRuntime.read_original`` / ``OriginalStore.text_document``: audience,
  live SharePoint source gate with the current item ACL, byte re-hash). An original the
  employee may not read is refused (``original_not_visible``), at request and at answer.
  ``confirmed_kinds`` re-reads the original for the *consuming* reader before it serves any
  confirmation.
* **Our own state only.** Confirmations live in this module's SQLite file in the Workbench
  state directory. Nothing is written to SharePoint, the Portal or any business system
  (``business_system_writes: 0``); a confirmation is never a source column.
* **Supersedable.** A later confirmation of the same original supersedes the earlier one
  (``supersedes`` names it); history stays. A person corrects a value by answering a new form
  (the form's fields default to the proposal and accept any value the Portal defines for the
  column; changed columns are recorded as ``amended_columns``).
* **Two kinds of answer.** Portal-taxonomy columns (``COLUMNS``, the Portal's own set) and,
  separately, the Workbench document kind (``DOCUMENT_KIND``, one of
  ``playbook_originals.KINDS``; never a SharePoint column). The Portal has no category for a
  proforma invoice, a forwarder's freight quote or an AO scan, so those kinds are recorded only
  as a confirmed ``document_kind``.

**Onto the Document object** (QA-07: one path, no separate tagging store).
``projection_step(path, tenant)`` is a ``build_store`` ``post_project`` step: the latest
unsuperseded confirmation of a SharePoint original's bytes (through any original of them; the
latest one when every one is superseded) becomes values
of its ``SharePointFile`` object in the projection (``document_kind``,
``document_category``, ``proposed_portal_metadata``; ``ontology/schema/domains/
documents.json``) on every captured version of the same drive item with the same eTag and
bytes, from its time until a confirmation of its original supersedes it with other bytes
(lineage ``workbench``; the version's markings; cited by the version's handle and its
``content_sha256`` field, the bytes the person confirmed). ``plx.find`` then answers them
(``object_type`` SharePointFile, a ``document_category`` filter) and ``plx.object`` shows
them, both re-gated per reader like the file itself. A store version holds one value per
property, so another confirmation of the same bytes is not projected (``superseded``; its
record stays in this store's history): ``as_known_at`` before the projected one finds none,
never the other one. The confirmed Portal columns are *proposed* SharePoint metadata
(``status`` proposed, ``written_to_sources`` false): never written to SharePoint or the
Portal. Each projected ``proposed_portal_metadata`` carries ``against_portal``: the confirmed
columns against what the Portal states on that very version (its ``portal_metadata``,
``operating.doc_metadata.confirmed_against_stated``); a confirmed value that differs is a
``conflict`` with both values kept, as for a proposal (QA-06), and the step counts the
versions in conflict (``portal_conflicts``). Neither side overrides the other. Any other original (a mail attachment, a local file) and a record whose bytes or
eTag no projected version holds project nothing (counted).

**Earlier ``plx.review`` document types are history here** (QA-07: one store of kinds, one
write path). This module's form is the only path
that records what a document is; ``plx.review`` refuses a new document type.
``import_reviews`` copies the document-kind decisions ``ontology.reviews`` held in force into
this store under their review ids, append-only and once (``review_record``: ``channel``
``plx_review``, the cited versions kept, the Portal type (Prisma ``DocumentType``) as
``portal_document_type`` and the two Portal columns the Portal itself derives from it,
``DocumentType`` by tag-taxonomy.ts ``sharePointDocumentType`` and ``DocumentCategory`` by
storage-category.ts ``resolveStorageCategory``, named in ``derived_columns``; its
``document_kind``, the Workbench kind, stays empty). One on a ``SharePointFile`` is bound like
an answered form to the drive item, eTag and ``content_sha256`` of the version current when it
was recorded (``sharepoint_binding``), so ``projection_step`` projects it like any other
confirmation and a later confirmation of the same bytes supersedes it. One on any other record
(a mail, an attached original) or on a file version without bytes is imported unbound (its
cited version, no bytes): it projects nothing (``not_sharepoint``) but stays listed and
retractable. An import never supersedes a newer confirmation of its record
(``newer_current``); one equal to the current one under another id is ``held``; a type that
is not a Portal type is not imported (``not_bound``). ``withdraw`` (``plx.review``
``retract``) appends a withdrawal of the current one (``retracts`` names it, at the
retraction's own time whatever the clocks did): it supersedes it and projects nothing.
``reviewed`` gives each with its state in this store. Every build
(``projection_step(..., reviews=)``) and every ``plx.review`` status and retraction imports
first.

``confirmed_kinds(confirmations, read, actor)`` is the ``KindConfirmationsRead`` the playbook
evaluator (``operating.playbook_originals``, kind source ``workbench_confirmation``)
consumes: original id -> current confirmation, served only while the
original still reads, for the consuming reader, as the same bytes from the same item. Live
SharePoint columns stay a separate source; a disagreement is ``document_kind_ambiguous``.

Portal taxonomy mirrored (petralabx/plx-customer-portal staging @5724575, unchanged since
8da33e4 for these paths; the DocStatus and RetentionCategory choices re-read at 5cb95e1, where
TASK-2215 adds Superseded and the PLX retention values): columns
``scripts/document-metadata/columns.mjs``; written by
``setDocumentMetadata`` ``portal/src/lib/services/document-governance.ts:130-144``
(DocumentType, ReviewStatus, ExpiryDate, CustomerRef, ProjectRef, RetentionCategory, PartyType,
PartyRef, DocumentCategory, ItemRef, DocStatus, Source, Confidentiality); categories
``portal/src/lib/storage/folder-taxonomy.ts``; content types
``portal/src/lib/sharepoint/sharepoint-metadata.ts``.

Known residuals (stated, not hardened against): the MCP server cannot prove that the client
showed the form to a person; a client that auto-answers elicitations defeats the human check,
so a host must declare elicitation only for clients that show the form (the record keeps the
client name).
Evidence label: isolated-fixture / injected-transport, **live-unverified**: tests drive a
simulated MCP client; no real client (Claude Code, Claude Desktop) has yet been shown to render
this form-mode elicitation. No live read is made here beyond the case runtime's existing
GET-only readers.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from integrations.live_source_guard import NotVisible

from . import doc_metadata_taxonomy as taxonomy
from .cases import CitationError
from .confirmation_forms import DEFAULT_TTL_SECONDS, ConfirmationForms, FormRefused, is_response
from .entitlements import _AGENT_PREFIXES  # "agents are never principals" (one rule)
from .playbook_originals import KINDS, confirmation_binds

SCHEMA = "plx.document-fields-confirmation/v1"
PROPOSAL_DIGEST_SCHEMA = "plx.document-fields-proposal-digest/v1"
CHANNEL = "mcp_elicitation"
STORE_FILE = "kind-confirmations.sqlite"  # in the case runtime directory (store_path)
FORM_MODE = "form"
PURPOSE = "document_fields"   # this module's confirmation forms
EMPLOYEE = "employee:"
_ORIGINAL = re.compile(r"^original://(orig-[0-9a-f]{32})(?:#extraction=([2-9]|[1-9][0-9]))?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROPOSED = ("proposed", "model_suggestion")

# The confirmable columns and the values the Portal defines for each (QA-06: the Portal's
# column set, never a fork). Text columns take any short single-line text.
# CustomerVisible is left out on purpose: the Portal sets it from app_document.customer_visible;
# so are ProductRef and OrderRef, which it walks from its record links (TASK-2222).
ALLOWED: dict[str, tuple[str, ...] | None] = {
    "DocumentType": tuple(taxonomy.SP_DOCUMENT_TYPES),
    "ReviewStatus": tuple(taxonomy.SP_REVIEW_STATUSES),
    "ExpiryDate": None, "CustomerRef": None, "ProjectRef": None,
    # legacy 7_YEAR style values stay valid; Portal writes use the PLX choices (TASK-2215)
    "RetentionCategory": (*taxonomy.SP_RETENTION_CATEGORIES,
                          *taxonomy.RETENTION_CATEGORY_CHOICES),
    "PartyType": taxonomy.COLUMN_BY_NAME["PartyType"]["choices"],
    "PartyRef": None,
    "DocumentCategory": tuple(sorted(taxonomy.STORAGE_CATEGORIES)),
    "ItemRef": None,
    "DocStatus": taxonomy.DOC_STATUS_CHOICES,  # PlxDocStatus: columns.mjs + Superseded
    "Source": taxonomy.COLUMN_BY_NAME["Source"]["choices"],
    "Confidentiality": taxonomy.COLUMN_BY_NAME["Confidentiality"]["choices"],
    "ContentType": tuple(sorted(taxonomy.CONTENT_TYPES)),
}
COLUMNS = tuple(ALLOWED)
# The Workbench's own document kind (operating.playbook_originals.KINDS): kept apart from the
# Portal columns (never a SharePoint column, QA-06), answered on the same form. The Portal has
# no category for a proforma invoice, a forwarder's freight quote or an AO scan, so those kinds
# can only be recorded this way.
DOCUMENT_KIND = "document_kind"
ANSWERS: dict[str, tuple[str, ...] | None] = {**ALLOWED, DOCUMENT_KIND: tuple(KINDS)}
DISPLAY = {**{c["name"]: c["display_name"] for c in taxonomy.COLUMNS},
           "ContentType": "Content Type",
           DOCUMENT_KIND: "Document kind (Workbench classification, not a SharePoint column)"}
PORTAL_SOURCES = (
    "scripts/document-metadata/columns.mjs@8e3a951",
    "portal/src/lib/services/document-governance.ts@19f1d94:49-80,121-140 (DocStatus "
    "PlxDocStatus, RetentionCategory legacy or PLX, documentMetadataFields)",
    "portal/src/lib/sharepoint/plx-document-schema.ts@5cb95e1:23-56",
    "portal/src/lib/storage/folder-taxonomy.ts@19f1d94:61-165",
    "portal/src/lib/sharepoint/sharepoint-metadata.ts@5724575")


class ConfirmationRefused(PermissionError):
    """A confirmation step refused; ``code`` says why (no source detail)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"confirmation_refused:{code}")
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_agent_principal(principal: Any) -> bool:
    text = str(principal)
    return text == "*" or text.strip().lower().startswith(_AGENT_PREFIXES)


def human_principal(actor: Any) -> str:
    """The one ``employee:<oid>`` principal of a human session, else ``ConfirmationRefused``."""
    principals = sorted(str(p) for p in (getattr(actor, "principals", None) or ()))
    if not principals or any(is_agent_principal(p) for p in principals):
        raise ConfirmationRefused("agent_or_service_principal_cannot_confirm")
    employees = [p for p in principals if p.lower().startswith(EMPLOYEE)]
    if len(employees) != 1 or len(employees[0]) <= len(EMPLOYEE):
        raise ConfirmationRefused("one_employee_principal_required")
    return employees[0]


def _original(citation: Any) -> tuple[str, str]:
    """(original id, file citation) of an ``original://`` file citation (never a page)."""
    match = _ORIGINAL.fullmatch(citation) if isinstance(citation, str) else None
    if match is None:
        raise ValueError("original_file_citation_required")
    return match.group(1), citation


def _valid_value(column: str, value: Any) -> str | None:
    """The value as stored, or None when the Portal does not define it for ``column``."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 255 or any(ord(c) < 32 for c in value):
        return None
    allowed = ANSWERS[column]
    if allowed is not None:
        return value if value in allowed else None
    if column == "ExpiryDate":
        if not _DATE.fullmatch(value):
            return None
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
    return value


# --------------------------------------------------------------------------- proposals

def proposal_digest(proposal: Mapping[str, Any], view: Mapping[str, Any],
                    model_suggestions: Sequence[Mapping[str, Any]] = (),
                    proposed_document_kind: str | None = None) -> str:
    """Digest binding one ``plx.document_fields`` proposal to the exact original.

    ``view`` is the original's file-citation read for the same actor (provenance)."""
    provenance = view.get("provenance") if isinstance(view.get("provenance"), Mapping) else {}
    body = {
        "schema": PROPOSAL_DIGEST_SCHEMA,
        "citation": proposal.get("document_id"),
        "original_sha256": proposal.get("original_sha256"),
        "view_sha256": view.get("sha256"),
        "extraction_version": proposal.get("extraction_version"),
        "extraction_text_sha256": proposal.get("extraction_text_sha256"),
        "provenance": {key: provenance.get(key) for key in (
            "source", "drive_id", "item_id", "etag", "ctag", "source_hashes")},
        "model_suggestions": list(model_suggestions),
        "proposed_document_kind": proposed_document_kind,
        "columns": [{key: row.get(key) for key in ("column", "status", "value", "basis")}
                    for row in proposal.get("columns") or []],
    }
    return _sha(_canon(body))


def _read_view(runtime: Any, actor: Any, citation: str) -> dict[str, Any]:
    """The original's file citation as ``actor`` may read it now (audience, live source
    gate with the current item ACL, byte re-hash), else ``original_not_visible``."""
    try:
        view = runtime.read_original(citation, actor)
    except (NotVisible, PermissionError, CitationError):
        raise ConfirmationRefused("original_not_visible") from None
    if not isinstance(view, Mapping) or not view.get("ok"):
        raise ConfirmationRefused("original_not_visible")
    return dict(view)


def _proposal(runtime: Any, actor: Any, citation: str,
              model_suggestions: Sequence[Mapping[str, Any]],
              proposed_document_kind: str | None = None) -> tuple[dict, dict, str]:
    """(proposal, view, digest) built server-side for ``actor``."""
    from .case_context_tools import document_metadata

    view = _read_view(runtime, actor, citation)
    try:
        proposal = document_metadata(runtime, actor, citation,
                                     model_suggestions=list(model_suggestions))
    except (NotVisible, PermissionError, CitationError):
        raise ConfirmationRefused("original_not_visible") from None
    return proposal, view, proposal_digest(proposal, view, model_suggestions,
                                           proposed_document_kind)


# --------------------------------------------------------------------------- the store

class KindConfirmations:
    """Confirmations in the Workbench's own SQLite file (append-mostly); their open
    requests are confirmation forms (``operating.confirmation_forms``) in the same file."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.path = str(path)
        self._clock = clock or _now
        self.forms = ConfirmationForms(path, clock=self._clock, ttl_seconds=ttl_seconds)
        with closing(self._db()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS confirmation (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                tenant TEXT NOT NULL, original_id TEXT NOT NULL, drive_id TEXT,
                item_id TEXT, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
                supersedes TEXT)""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_confirmation_item
                ON confirmation(tenant, drive_id, item_id)""")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _iso(self, value: datetime | None = None) -> str:
        return (value or self._clock()).isoformat()

    # ---- request ----

    def request(self, runtime: Any, actor: Any, citation: str, *,
                columns: Sequence[str] | None = None,
                model_suggestions: Sequence[Mapping[str, Any]] = (),
                proposed_document_kind: str | None = None,
                expected_proposal_digest: str | None = None,
                client: str | None = None) -> tuple[dict[str, Any], str, dict[str, Any]]:
        """Build the proposal for ``actor`` and open one single-use confirmation request.

        Returns ``(public, token, form)``: ``public`` is safe to show the agent (no token);
        ``token`` addresses the answer (the elicitation request id) and must only travel to the
        client on the MCP transport; ``form`` is the
        ``elicitation/create`` params."""
        channel = CHANNEL
        principal = human_principal(actor)
        original_id, citation = _original(citation)
        suggestions = [dict(item) for item in model_suggestions]
        if proposed_document_kind is not None and proposed_document_kind not in KINDS:
            raise ValueError("proposed_document_kind_invalid")
        proposal, view, digest = _proposal(runtime, actor, citation, suggestions,
                                           proposed_document_kind)
        if expected_proposal_digest is not None and expected_proposal_digest != digest:
            raise ConfirmationRefused("proposal_changed")
        rows = {row["column"]: row for row in proposal.get("columns") or []}
        if columns is None:
            chosen = [name for name in COLUMNS if name in rows
                      and rows[name].get("status") in _PROPOSED
                      and _valid_value(name, rows[name].get("value")) is not None]
        else:
            if not isinstance(columns, (list, tuple)) or not columns or len(set(columns)) != \
                    len(columns) or any(name not in ANSWERS for name in columns):
                raise ValueError("columns_invalid: Portal taxonomy columns or document_kind")
            chosen = list(columns)
        if proposed_document_kind is not None and DOCUMENT_KIND not in chosen:
            chosen.append(DOCUMENT_KIND)
        # a proposed document kind is an agent's or model's proposal, nothing more
        rows[DOCUMENT_KIND] = ({"status": "model_suggestion", "value": proposed_document_kind,
                                "basis": "agent_proposal"}
                               if proposed_document_kind is not None else {})
        if not chosen:
            raise ConfirmationRefused("nothing_proposed_to_confirm")
        proposed = {}
        for name in chosen:
            row = rows.get(name) or {}
            value = (_valid_value(name, row.get("value"))
                     if row.get("status") in _PROPOSED else None)
            proposed[name] = {"value": value, "status": row.get("status") or "no_proposal",
                              "basis": row.get("basis")}
        # A newer request for the same original by the same person supersedes older open
        # ones: only the latest form the person saw can be answered.
        token, expires_at = self.forms.open(PURPOSE, actor.tenant, original_id, principal, {
            "original_id": original_id, "citation": citation, "columns": chosen,
            "suggestions": suggestions, "proposal_digest": digest, "proposed": proposed,
            "proposed_kind": proposed_document_kind, "channel": channel, "client": client})
        form = _form(view, citation, proposed)
        public = {"schema": SCHEMA, "status": "awaiting_human_confirmation",
                  "channel": channel, "citation": citation, "proposal_digest": digest,
                  "columns": chosen,
                  "proposed": {name: item["value"] for name, item in proposed.items()},
                  "expires_at": expires_at, "confirmed": False,
                  "business_system_writes": 0,
                  "note": ("The person answers a form in their own client; nothing an agent "
                           "returns or records counts as a confirmation.")}
        return public, token, form

    # ---- answer ----

    def complete(self, runtime: Any, actor: Any, token: Any,
                 answer: Mapping[str, Any]) -> dict[str, Any]:
        """Record the person's answer to request ``token`` (single use).

        ``answer`` is the elicitation result ``{"action", "content"}``. Refused (and the
        request closed) when: the session is not the requesting employee; the request expired
        or was superseded; the original is no longer readable by this employee; or the
        proposal or original changed (digest). ``decline``/``cancel`` closes it unconfirmed."""
        try:
            form = self.forms.claim(token, purpose=PURPOSE, tenant=getattr(actor, "tenant", None),
                                    answerer=lambda: human_principal(actor))
        except FormRefused as refused:
            raise ConfirmationRefused(refused.code) from None
        principal, row = form["requested_by"], form["payload"]
        try:
            action = answer.get("action") if isinstance(answer, Mapping) else None
            if action in ("decline", "cancel"):
                self.forms.finish(token, action + "d" if action == "decline" else "cancelled",
                                  None)
                return {"schema": SCHEMA, "confirmed": False, "outcome": action,
                        "business_system_writes": 0}
            if action != "accept":
                raise ConfirmationRefused("answer_invalid")
            columns, proposed = row["columns"], row["proposed"]
            confirmed = _answer_values(answer.get("content"), columns)
            _proposal_now, view, digest = _proposal(runtime, actor, row["citation"],
                                                    row["suggestions"], row["proposed_kind"])
            if digest != row["proposal_digest"]:
                raise ConfirmationRefused("proposal_or_original_changed")
        except ConfirmationRefused as refused:
            self.forms.finish(token, "refused", refused.code)
            raise
        except BaseException:
            self.forms.finish(token, "refused", "answer_failed")   # spent: ask again
            raise
        provenance = view.get("provenance") if isinstance(view.get("provenance"),
                                                          Mapping) else {}
        record = {
            "schema": SCHEMA, "id": "kc-rec-" + secrets.token_hex(12),
            "tenant": actor.tenant, "original_id": row["original_id"],
            "citation": row["citation"], "original_sha256": view.get("sha256"),
            "extraction_version": view.get("extraction_version", 1),
            "provenance": {key: provenance.get(key) for key in (
                "source", "drive_id", "item_id", "etag", "ctag", "source_hashes")},
            "proposal_digest": row["proposal_digest"],
            "proposed": {name: item["value"] for name, item in proposed.items()},
            "confirmed_fields": {k: v for k, v in confirmed.items() if k != DOCUMENT_KIND},
            "document_kind": confirmed.get(DOCUMENT_KIND),
            "amended_columns": sorted(name for name in columns
                                      if confirmed[name] != proposed[name]["value"]),
            "confirmed_by": principal, "channel": row["channel"], "client": row["client"],
            "requested_at": form["created_at"], "confirmed_at": self._iso(),
            "reader_permission": "original read for the confirming employee at request and "
                                 "answer (audience, live source gate, byte re-hash)",
            "portal_taxonomy": list(PORTAL_SOURCES), "written_to_sources": False,
        }
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._append(db, record)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                self.forms.finish(token, "refused", "answer_failed")
                raise
        self.forms.finish(token, "confirmed", record["id"])
        return {"schema": SCHEMA, "confirmed": True, "confirmation_id": record["id"],
                "confirmed_fields": confirmed, "amended_columns": record["amended_columns"],
                "supersedes": record["supersedes"], "business_system_writes": 0}

    def _append(self, db: sqlite3.Connection, record: dict[str, Any]) -> None:
        """Insert ``record`` (in the caller's transaction) superseding the current
        confirmation of its original."""
        active = self._active(db, record["tenant"], record["original_id"])
        record["supersedes"] = active["id"] if active else None
        db.execute("""INSERT INTO confirmation (id, tenant, original_id, drive_id,
            item_id, record_json, record_sha256, supersedes) VALUES (?,?,?,?,?,?,?,?)""",
            (record["id"], record["tenant"], record["original_id"],
             record["provenance"]["drive_id"], record["provenance"]["item_id"],
             _canon(record), _sha(_canon(record)), record["supersedes"]))

    # ---- earlier plx.review document types ----

    def import_reviews(self, conn: Any, reviews: Any, tenant: str) -> dict[str, int]:
        """Copy ``tenant``'s document-kind decisions in force in the review store
        ``reviews`` (``ontology.reviews.ReviewStore``) into this store (module docstring),
        a ``SharePointFile``'s bound to its version current in the projection ``conn`` when
        it was recorded. Idempotent: an imported decision keeps its review id and is never
        copied twice."""
        from ontology.reviews import effective
        from ontology.store import instant

        counts = {"imported": 0, "held": 0, "not_bound": 0, "newer_current": 0}
        decisions = list(effective(reviews.kind_history(tenant)).values())
        with closing(self._db()) as db:
            known = {row[0] for row in db.execute(
                "SELECT id FROM confirmation WHERE tenant=? AND id IN (SELECT value FROM "
                "json_each(?))", (tenant, json.dumps([d["review_id"] for d in decisions])))}
        for decision in decisions:
            if decision["document_kind"] not in taxonomy.PRISMA_DOCUMENT_TYPES:
                counts["not_bound"] += 1
                continue
            if decision["review_id"] in known:   # imported before
                counts["held"] += 1
                continue
            binding = sharepoint_binding(conn, decision["subject"], decision["recorded_at"])
            record = review_record(tenant, decision["subject"], binding or _unbound(decision),
                                   decision["document_kind"], decision["evidence"],
                                   decision["author"], decision["recorded_at"],
                                   review_id=decision["review_id"])
            with closing(self._db()) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    current = self._active(db, tenant, record["original_id"])
                    if db.execute("SELECT 1 FROM confirmation WHERE id=?",
                                  (record["id"],)).fetchone() or (current is not None and all(
                            current.get(key) == record[key] for key in _SAME_REVIEW)):
                        outcome = "held"
                    elif current is not None and instant(current["confirmed_at"]) > instant(
                            record["confirmed_at"]):
                        outcome = "newer_current"   # an import never supersedes it
                    else:
                        self._append(db, record)
                        outcome = "imported"
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            counts[outcome] += 1
        return counts

    def get(self, tenant: str, record_id: str) -> dict[str, Any] | None:
        with closing(self._db()) as db:
            row = db.execute("SELECT * FROM confirmation WHERE tenant=? AND id=?",
                             (tenant, record_id)).fetchone()
        return None if row is None else self._load(row)

    def withdraw(self, tenant: str, record_id: str, author: Mapping[str, Any], at: str
                 ) -> dict[str, Any]:
        """Append a withdrawal of the current ``plx.review`` document type ``record_id``
        by ``author`` (the session's) at store time ``at``; refused
        (``ConfirmationRefused``) when it names no such record, a withdrawal, or one no
        longer current (``already_retracted``, ``kind_superseded``). Never time-guarded:
        a fresh record supersedes the current one whatever the clocks did."""
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM confirmation WHERE tenant=? AND id=?",
                                 (tenant, record_id)).fetchone()
                target = None if row is None else self._load(row)
                if target is None or target.get("channel") != REVIEW_CHANNEL:
                    raise ConfirmationRefused("review_not_found")
                if target.get("retracts"):
                    raise ConfirmationRefused("retract_of_a_retraction")
                current = self._active(db, tenant, target["original_id"])
                if current is None or current["id"] != record_id:
                    raise ConfirmationRefused(
                        "already_retracted" if current is not None
                        and current.get("retracts") == record_id else "kind_superseded")
                record = {key: target[key] for key in (
                    "schema", "tenant", "original_id", "citation", "original_sha256",
                    "provenance", "portal_taxonomy")}
                record.update({
                    "id": "rev-" + secrets.token_hex(16), "retracts": record_id,
                    "proposal_digest": None, "proposed": {}, "portal_document_type": None,
                    "confirmed_fields": {}, "document_kind": None, "amended_columns": [],
                    "confirmed_by": author["principal"], "author": dict(author),
                    "channel": REVIEW_CHANNEL, "client": None, "evidence": [],
                    "requested_at": None, "confirmed_at": at, "written_to_sources": False})
                self._append(db, record)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return record

    def reviewed(self, tenant: str, object_id: str
                 ) -> list[tuple[dict[str, Any], str | None, str | None]]:
        """``(record, state, reason)`` of every ``plx.review`` document type of
        ``object_id`` and every withdrawal of one, in store order, with its state in this
        store: a withdrawal ``effective``; one withdrawn ``retracted``; one a later record
        superseded, or a later confirmation of the same bytes outranks (the one
        ``projection_step`` projects), ``superseded``; else None (in force here, its cited
        versions decide)."""
        with closing(self._db()) as db:
            chain = [self._load(row) for row in db.execute(
                "SELECT * FROM confirmation WHERE tenant=? AND original_id=? ORDER BY seq",
                (tenant, object_id))]
            after = {r["supersedes"]: r for r in chain if r.get("supersedes")}
            out: list[tuple[dict[str, Any], str | None, str | None]] = []
            for record in chain:
                if record.get("channel") != REVIEW_CHANNEL:
                    continue
                later = after.get(record["id"])
                if record.get("retracts"):
                    out.append((record, "effective", None))
                elif later is not None and later.get("retracts") == record["id"]:
                    out.append((record, "retracted", "retracted_by_review"))
                elif later is not None or self._outranked(db, record):
                    out.append((record, "superseded", "later_review"))
                else:
                    out.append((record, None, None))
        return out

    def _outranked(self, db: sqlite3.Connection, record: Mapping[str, Any]) -> bool:
        """A later unsuperseded confirmation of ``record``'s SharePoint bytes is held."""
        from ontology.store import instant

        key = _bytes_key(record)
        if key is None:
            return False
        rows = db.execute("SELECT * FROM confirmation WHERE tenant=? AND drive_id=? AND "
                          "item_id=?", (record["tenant"], key[0], key[1])).fetchall()
        superseded = {row["supersedes"] for row in db.execute(
            "SELECT supersedes FROM confirmation WHERE tenant=? AND supersedes IS NOT NULL",
            (record["tenant"],))}
        mine = (instant(record["confirmed_at"]), record["id"])
        return any(other.get("retracts") is None and _bytes_key(other) == key and (
            instant(other["confirmed_at"]), other["id"]) > mine
                   for other in (self._load(row) for row in rows
                                 if row["id"] not in superseded))

    # ---- reading confirmations ----

    @staticmethod
    def _load(row: sqlite3.Row) -> dict[str, Any]:
        record = json.loads(row["record_json"])
        if _sha(row["record_json"]) != row["record_sha256"]:
            raise RuntimeError("confirmation_record_tampered")
        return record

    def _active(self, db: sqlite3.Connection, tenant: str, original_id: str
                ) -> dict[str, Any] | None:
        rows = db.execute("SELECT * FROM confirmation WHERE tenant=? AND original_id=? "
                          "ORDER BY seq", (tenant, original_id)).fetchall()
        superseded = {row["supersedes"] for row in rows if row["supersedes"]}
        live = [row for row in rows if row["id"] not in superseded]
        return self._load(live[-1]) if live else None

    def active(self, tenant: str, original_id: str) -> dict[str, Any] | None:
        """The current (not superseded, not withdrawn) confirmation of one original, or
        None."""
        with closing(self._db()) as db:
            record = self._active(db, tenant, original_id)
        return None if record is None or record.get("retracts") else record

    def history(self, tenant: str, original_id: str) -> list[dict[str, Any]]:
        with closing(self._db()) as db:
            return [self._load(row) for row in db.execute(
                "SELECT * FROM confirmation WHERE tenant=? AND original_id=? ORDER BY seq",
                (tenant, original_id))]

    def status(self, runtime: Any, actor: Any, citation: str) -> dict[str, Any]:
        """The current confirmation of an original for a reader who may read it now."""
        original_id, citation = _original(citation)
        view = _read_view(runtime, actor, citation)
        record = self.active(actor.tenant, original_id)
        out: dict[str, Any] = {"schema": SCHEMA, "citation": citation, "confirmed": False,
                               "pending_requests": self.forms.pending(PURPOSE, actor.tenant,
                                                                      original_id),
                               "business_system_writes": 0}
        if record is None:
            return out
        bound = confirmation_binds(record, view)  # the evaluator's binding rule
        principals = {str(p) for p in actor.principals}
        out.update(confirmed=bound, confirmation_id=record["id"],
                   confirmed_fields=record["confirmed_fields"],
                   document_kind=record.get("document_kind"),
                   amended_columns=record["amended_columns"],
                   proposal_digest=record["proposal_digest"],
                   confirmed_at=record["confirmed_at"], channel=record["channel"],
                   confirmed_by_you=record["confirmed_by"] in principals,
                   supersedes=record["supersedes"],
                   **({} if bound else {"stale": "original_changed"}))
        return out


def _answer_values(content: Any, columns: Sequence[str]) -> dict[str, str]:
    """The person's values: every asked column, each a value the Portal defines for it, and
    the explicit ``confirm`` tick."""
    if not isinstance(content, Mapping) or content.get("confirm") is not True:
        raise ConfirmationRefused("not_confirmed_by_the_person")
    if set(content) - set(columns) - {"confirm"}:
        raise ConfirmationRefused("answer_invalid")
    values = {}
    for name in columns:
        value = _valid_value(name, content.get(name))
        if value is None:
            raise ConfirmationRefused("answer_value_not_a_portal_value")
        values[name] = value
    return values


def _form(view: Mapping[str, Any], citation: str,
          proposed: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """``elicitation/create`` params (MCP 2025-11-25 form mode): one field per column,
    defaulting to the proposal, plus the person's explicit confirmation tick."""
    properties: dict[str, Any] = {}
    for name, item in proposed.items():
        field: dict[str, Any] = {"type": "string", "title": DISPLAY.get(name, name),
                                 "description": (f"proposed ({item['basis'] or 'no basis'})"
                                                 if item["value"] is not None
                                                 else f"no proposal ({item['status']})")}
        if ANSWERS[name] is not None:
            field["enum"] = list(ANSWERS[name])
        elif name == "ExpiryDate":
            field["format"] = "date"
        else:
            field["maxLength"] = 255
        if item["value"] is not None:
            field["default"] = item["value"]
        properties[name] = field
    properties["confirm"] = {
        "type": "boolean", "default": False,
        "title": "I opened the original and these values are right"}
    sha = str(view.get("sha256") or "")[:12]
    return {"mode": FORM_MODE,
            "message": (f"Confirm the Portal document metadata of {view.get('filename')} "
                        f"({citation}, SHA-256 {sha}...). The values were proposed by a model "
                        "or extractor. Open the original and check each value before you "
                        "confirm. Answer only if you are the employee this session is for; an "
                        "agent must not answer it. The answer is recorded in the PLX Workbench "
                        "only and written to no Petra system."),
            "requestedSchema": {"type": "object", "properties": properties,
                                "required": [*proposed, "confirm"]}}


# --------------------------------------------------------------------------- plx.review

REVIEW_CHANNEL = "plx_review"
DERIVED_COLUMNS = {
    "DocumentType": "portal/src/lib/documents/tag-taxonomy.ts@8da33e4 sharePointDocumentType",
    "DocumentCategory": "portal/src/lib/documents/storage-category.ts@8da33e4 "
                        "resolveStorageCategory"}


_SAME_REVIEW = ("portal_document_type", "citation", "original_sha256", "evidence",
                "confirmed_by")


def sharepoint_binding(conn: Any, object_id: str, at: str) -> dict[str, Any] | None:
    """The ``SharePointFile`` ``object_id`` as projection ``conn`` held it at ``at`` (store
    time text): its version handle (``citation``), bytes (``original_sha256``, its
    ``content_sha256`` value) and drive item and eTag (``provenance``), the binding
    ``projection_step`` matches. None for any other record, a time it was not held or a
    version without bytes."""
    from integrations.capture_sharepoint import SCHEME
    from integrations.live_source_guard import parse_handle

    row = conn.execute(
        "SELECT v.l1_handle, o.native_key, c.value FROM objects o JOIN object_versions v ON "
        "v.object_id = o.object_id JOIN object_values c ON c.object_id = v.object_id AND "
        "c.l1_handle = v.l1_handle AND c.property = 'content_sha256' WHERE o.object_id = ? "
        "AND o.object_type = ? AND o.binding_id = ? AND julianday(v.recorded_at) <= "
        "julianday(?) AND (v.superseded_at IS NULL OR julianday(v.superseded_at) > "
        "julianday(?))", (object_id, PROJECTED_TYPE, PROJECTED_BINDING, at, at)).fetchone()
    if row is None or not isinstance(row[2], str) or not row[2]:
        return None
    try:
        (drive_id, item_id), etag, _ = parse_handle(row[0], SCHEME, 2)
        key = json.loads(row[1])
    except (TypeError, ValueError):
        return None
    if key != {"drive_id": drive_id, "id": item_id}:
        return None
    return {"citation": row[0], "original_sha256": row[2],
            "provenance": {"source": SHAREPOINT_ORIGIN, "drive_id": drive_id,
                           "item_id": item_id, "etag": etag, "ctag": None,
                           "source_hashes": None}}


def _unbound(decision: Mapping[str, Any]) -> dict[str, Any]:
    """The binding of a decision on a record with no SharePoint bytes: the version of the
    subject it cites (else its first cited version), no bytes, no drive item."""
    cited = [item for item in decision["evidence"] if item["object_id"] == decision["subject"]]
    return {"citation": (cited or decision["evidence"])[0]["l1_handle"], "original_sha256": None,
            "provenance": {key: None for key in ("source", "drive_id", "item_id", "etag",
                                                 "ctag", "source_hashes")}}


def _bytes_key(record: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    """(drive id, item id, eTag, SHA-256) of a confirmation of SharePoint bytes, else None."""
    origin = record.get("provenance") or {}
    key = (origin.get("drive_id"), origin.get("item_id"), origin.get("etag"),
           record.get("original_sha256"))
    if origin.get("source") != SHAREPOINT_ORIGIN or not all(
            isinstance(v, str) and v for v in key):
        return None
    return key  # type: ignore[return-value]


def review_record(tenant: str, subject: str, binding: Mapping[str, Any], document_type: str,
                  evidence: Sequence[Mapping[str, Any]], author: Mapping[str, Any], at: str,
                  *, review_id: str | None = None) -> dict[str, Any]:
    """The confirmation an earlier ``plx.review`` Portal document type of ``subject`` is
    (module docstring): ``binding`` from ``sharepoint_binding`` (or ``_unbound``),
    ``evidence`` the versions the person reviewed, ``author`` the session's
    (``principal``), ``at`` store time text."""
    if document_type not in taxonomy.PRISMA_DOCUMENT_TYPES:
        raise ValueError("document_type_invalid")
    return {
        "schema": SCHEMA, "id": review_id or "rev-" + secrets.token_hex(16),
        "tenant": tenant, "original_id": subject, "citation": binding["citation"],
        "original_sha256": binding["original_sha256"],
        "provenance": dict(binding["provenance"]), "proposal_digest": None, "proposed": {},
        "portal_document_type": document_type,
        "confirmed_fields": {
            "DocumentType": taxonomy.sharepoint_document_type(document_type),
            "DocumentCategory": taxonomy.resolve_storage_category(document_type)},
        "derived_columns": dict(DERIVED_COLUMNS), "document_kind": None,
        "amended_columns": [], "confirmed_by": author["principal"], "author": dict(author),
        "channel": REVIEW_CHANNEL, "client": None,
        "evidence": [dict(item) for item in evidence], "requested_at": None,
        "confirmed_at": at,
        "reader_permission": "file and every cited version read for the reviewing employee "
                             "when recorded (plx.review)",
        "portal_taxonomy": list(PORTAL_SOURCES), "written_to_sources": False}


# --------------------------------------------------------------------------- consumption

def store_path(state_dir: str | Path) -> Path:
    """Where a host keeps the store: the case runtime directory of ``state_dir``."""
    from .case_runtime import CASE_RUNTIME_DIR
    return Path(state_dir) / CASE_RUNTIME_DIR / STORE_FILE


def confirmed_kinds(confirmations: KindConfirmations, actor: Any
                    ) -> Callable[[str], Mapping[str, Any] | None]:
    """``KindConfirmationsRead`` (``operating.playbook_originals``) for one session's tenant.

    A store lookup only (original id -> current confirmation record). The evaluator binds the
    record to its own file-citation read of the original for the evaluating reader
    (``playbook_originals.confirmation_binds``), so there is no second source read to refuse,
    labels what it establishes ``workbench_confirmation`` and lets every other recorded source
    (a live SharePoint column, the text) contradict it."""
    tenant = actor.tenant

    def current(original_id: str) -> Mapping[str, Any] | None:
        return confirmations.active(tenant, original_id)
    return current


# --------------------------------------------------------------------------- projection

PROJECTED_TYPE = "SharePointFile"
PROJECTED_BINDING = "sharepoint.drive_items"
SHAREPOINT_ORIGIN = "sharepoint-drive"   # case_runtime provenance of a SharePoint original
PROPOSED_FORMAT = "plx-proposed-portal-metadata/v1"


def _current_records(path: Path, tenant: str) -> tuple[list[tuple[dict, str, str | None]], int]:
    """(record, valid from, valid until or None) of every confirmation of ``tenant`` while
    it was the current one of its original, and how many originals were left out because
    a record of theirs fails its digest. Read-only; no file is no confirmation."""
    if not path.is_file():
        return [], 0
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM confirmation WHERE tenant=? ORDER BY seq",
                          (tenant,)).fetchall()
    chains: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        chains.setdefault(row["original_id"], []).append(row)
    out: list[tuple[dict, str, str | None]] = []
    tampered = 0
    for chain in chains.values():
        try:
            records = [KindConfirmations._load(row) for row in chain]
        except RuntimeError:   # one altered record: nothing of that original projects
            tampered += 1
            continue
        until = {r["supersedes"]: r["confirmed_at"] for r in records if r.get("supersedes")}
        out += [(r, r["confirmed_at"], until.get(r["id"])) for r in records]
    return out, tampered


def _proposed(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"format": PROPOSED_FORMAT, "status": "proposed", "written_to_sources": False,
            "columns": dict(sorted(record["confirmed_fields"].items())),
            "document_kind": record.get("document_kind"),
            "amended_columns": record.get("amended_columns") or [],
            "confirmation_id": record["id"], "confirmed_at": record["confirmed_at"],
            "portal_taxonomy": record.get("portal_taxonomy") or list(PORTAL_SOURCES),
            **{key: record[key] for key in ("portal_document_type", "derived_columns")
               if key in record}}


def projection_step(path: str | Path, tenant: str, *, reviews: Any = None
                    ) -> Callable[[Any, Any], dict[str, Any]]:
    """The ``post_project`` step writing ``tenant``'s confirmations from the store at
    ``path`` onto their ``SharePointFile`` objects (module docstring), after importing the
    document-kind decisions of the review store ``reviews`` (``import_reviews``; the store
    file is created only when there is one)."""
    def step(builder: Any, projector: Any) -> dict[str, Any]:
        from ontology.project import POST_PROJECT
        from ontology.store import instant

        imported = (KindConfirmations(path).import_reviews(builder.connection, reviews, tenant)
                    if reviews is not None and reviews.kind_history(tenant) else None)
        spec = (getattr(projector, "types", None) or {}).get(PROJECTED_TYPE) or {}
        declared = {p["name"]: p["value_type"] for p in spec.get("properties", ())
                    if any(s.get("parser") == POST_PROJECT for s in p["sources"])}
        records, tampered = _current_records(Path(path), tenant)
        counts = {"confirmations": len(records), "tampered_originals": tampered,
                  "withdrawals": 0, "not_sharepoint": 0, "superseded": 0,
                  "not_projected": 0, "projected": 0, "values": 0, "portal_conflicts": 0}
        same_bytes: dict[tuple, list[tuple[str, str, str | None, dict]]] = {}
        for record, since, until in records:
            if record.get("retracts"):   # ends the record it withdraws; projects nothing
                counts["withdrawals"] += 1
                continue
            key = _bytes_key(record)
            if key is None:
                counts["not_sharepoint"] += 1
                continue
            same_bytes.setdefault(key, []).append(
                (instant(since), record["id"], None if until is None else instant(until),
                 record))
        conn = builder.connection
        rows: list[dict[str, Any]] = []
        for (drive_id, item_id, etag_of, digest), held in sorted(same_bytes.items()):
            # The latest unsuperseded confirmation of the bytes (else the latest): a
            # superseded later one never hides an earlier one still in force.
            since, _, until, record = max(held, key=lambda h: (h[2] is None, h[:2]))
            counts["superseded"] += len(held) - 1
            bound = _bind(conn, (drive_id, item_id), etag_of, digest, since, until)
            counts["projected" if bound else "not_projected"] += 1
            for version in bound:
                against = _against_portal(record, version.pop("portal_metadata"))
                counts["portal_conflicts"] += against["status"] == "conflict"
                rows += [dict(version, property=name, value_type=declared[name], value=value)
                         for name, value in _values(record, against).items()
                         if name in declared and value is not None]
        counts["values"] = builder.add("object_values", rows)
        out = {"format": SCHEMA, "counts": counts, "business_system_writes": 0}
        if imported is not None:
            out["imported_reviews"] = imported
        return out
    return step


def _against_portal(record: Mapping[str, Any], stated: str | None) -> dict[str, Any]:
    """The record's confirmed columns against the Portal-stated columns of one version
    (its projected ``portal_metadata`` value, canonical JSON, or None)."""
    from .doc_metadata import confirmed_against_stated
    try:
        parsed = json.loads(stated) if isinstance(stated, str) else None
    except ValueError:
        parsed = None
    return confirmed_against_stated(record["confirmed_fields"], parsed)


def _values(record: Mapping[str, Any], against: Mapping[str, Any]) -> dict[str, str | None]:
    from ontology.store import canonical
    return {"document_kind": record.get("document_kind"),
            "document_category": record["confirmed_fields"].get("DocumentCategory"),
            "proposed_portal_metadata": canonical({**_proposed(record),
                                                   "against_portal": dict(against)})}


def _bind(conn: Any, ids: tuple[str, str], etag_of: str, digest: str, since: str,
          until: str | None) -> list[dict[str, Any]]:
    """The value rows (without property and value) of every projected version of drive
    item ``ids`` with eTag ``etag_of`` and bytes ``digest``, clipped to [since, until),
    each with the version's projected ``portal_metadata`` (None: none)."""
    from integrations.capture_sharepoint import SCHEME
    from integrations.live_source_guard import parse_handle
    from ontology.project import POST_PROJECT
    from ontology.store import canonical

    out = []
    for version in conn.execute(
            "SELECT v.object_id, v.l1_handle, v.digest, v.markings, v.recorded_at, "
            "v.superseded_at, c.field_path, (SELECT p.value FROM object_values p WHERE "
            "p.object_id = v.object_id AND p.l1_handle = v.l1_handle AND p.property = "
            "'portal_metadata' ORDER BY p.recorded_at DESC LIMIT 1) "
            "FROM objects o JOIN object_versions v ON "
            "v.object_id = o.object_id JOIN object_values c ON c.object_id = v.object_id "
            "AND c.l1_handle = v.l1_handle AND c.property = 'content_sha256' "
            "AND c.value = ? WHERE o.object_type = ? AND o.binding_id = ? "
            "AND o.native_key = ? ORDER BY v.recorded_at",
            (digest, PROJECTED_TYPE, PROJECTED_BINDING,
             canonical({"drive_id": ids[0], "id": ids[1]}))):
        try:
            named, etag, _ = parse_handle(version[1], SCHEME, 2)
        except (TypeError, ValueError):
            continue
        start = max(version[4], since)
        end = min((t for t in (version[5], until) if t is not None), default=None)
        if tuple(named) != ids or etag != etag_of or (end is not None and end <= start):
            continue
        out.append({"object_id": version[0], "unit": None, "quantity_role": None,
                    "currency": None, "l1_handle": version[1], "field_path": version[6],
                    "digest": version[2], "lineage": "workbench",
                    "binding_id": PROJECTED_BINDING, "parser": POST_PROJECT,
                    "authority_rank": None, "markings": version[3], "recorded_at": start,
                    "superseded_at": end, "portal_metadata": version[7]})
    return out


# --------------------------------------------------------------------------- MCP surface

TOOL: dict[str, Any] = {
    "name": "plx.document_fields",
    "description": (
        "Portal-taxonomy metadata of one attached original (citation: its original:// file "
        "citation, audience-checked and re-hashed). mode=propose: per Portal column "
        "(PartyType, PartyRef, DocumentCategory, ItemRef, DocStatus, ...) a status, value, "
        "rule and evidence spans; record numbers are candidates, never a winner. mode=request: "
        "send the PERSON a confirmation form (MCP elicitation) for that proposal and/or "
        "proposed_document_kind (expected_proposal_digest refuses a moved one); agent sessions "
        "and clients without elicitation are refused; only the person's answer confirms. "
        "mode=status: the current confirmation. Nothing is written to any source."),
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["citation", "mode"],
        "properties": {
            "citation": {"type": "string", "minLength": 8, "maxLength": 200},
            "mode": {"type": "string", "enum": ["propose", "request", "status"]},
            "max_staleness": {"type": "number", "minimum": 0, "maximum": 315360000},
            "columns": {"type": "array", "minItems": 1, "maxItems": len(ANSWERS),
                        "items": {"type": "string", "enum": list(ANSWERS)}},
            "proposed_document_kind": {"type": "string", "enum": list(KINDS)},
            "expected_proposal_digest": {"type": "string", "minLength": 64, "maxLength": 64},
            "model_suggestions": {"type": "array", "maxItems": 20, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"column": {"type": "string", "maxLength": 64},
                               "value": {"type": "string", "maxLength": 255},
                               "model": {"type": "string", "maxLength": 120},
                               "basis": {"type": "string",
                                         "enum": ["document_text", "document_title"]},
                               "span": {"type": "object", "additionalProperties": False,
                                        "properties": {"start": {"type": "integer",
                                                                 "minimum": 0},
                                                       "end": {"type": "integer",
                                                               "minimum": 1}},
                                        "required": ["start", "end"]},
                               "quote": {"type": "string", "maxLength": 500}},
                "required": ["column", "value", "model", "span", "quote"]}}}},
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
}


def client_supports_elicitation(capabilities: Any) -> bool:
    """The client declared form elicitation at initialize (``{"elicitation": {}}`` or
    ``{"elicitation": {"form": {}}}``)."""
    if not isinstance(capabilities, Mapping):
        return False
    elicitation = capabilities.get("elicitation")
    if not isinstance(elicitation, Mapping):
        return False
    return not elicitation or isinstance(elicitation.get("form"), Mapping)


def call_tool(arguments: Mapping[str, Any], *, confirmations: KindConfirmations,
              runtime: Any, actor: Any, client_capabilities: Any,
              send_request: Callable[[dict[str, Any]], None],
              client_name: str | None = None) -> dict[str, Any]:
    """Run ``plx.document_fields`` modes request and status for the session ``actor``.

    ``send_request(message)`` writes one server-to-client JSON-RPC request
    (``elicitation/create``) on the session's MCP transport; its id is the single-use form id,
    which therefore never enters this tool's result. The client's response comes back through
    ``handle_response``."""
    if not isinstance(arguments, Mapping) or set(arguments) - set(
            TOOL["inputSchema"]["properties"]):
        raise ValueError("arguments invalid")
    citation = arguments.get("citation")
    mode = arguments.get("mode", "request")
    if not isinstance(citation, str) or mode not in ("request", "status"):
        raise ValueError("arguments invalid")
    if mode == "status":
        return confirmations.status(runtime, actor, citation)
    human_principal(actor)  # an agent session cannot even ask
    if not client_supports_elicitation(client_capabilities):
        raise ConfirmationRefused("human_confirmation_surface_unavailable: this MCP client "
                                  "declared no elicitation, so the person cannot be asked")
    digest = arguments.get("expected_proposal_digest")
    suggestions = arguments.get("model_suggestions") or []
    kind = arguments.get("proposed_document_kind")
    if kind is not None and kind not in KINDS:
        raise ValueError("proposed_document_kind invalid")
    if digest is not None and not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}",
                                                                              digest)):
        raise ValueError("expected_proposal_digest invalid")
    if not isinstance(suggestions, list) or not all(isinstance(s, Mapping) for s in suggestions):
        raise ValueError("model_suggestions invalid")
    public, token, form = confirmations.request(
        runtime, actor, citation, columns=arguments.get("columns"),
        model_suggestions=suggestions, proposed_document_kind=kind,
        expected_proposal_digest=digest, client=client_name)
    send_request({"jsonrpc": "2.0", "id": token, "method": "elicitation/create",
                  "params": form})
    return public


def handle_response(message: Mapping[str, Any], *, confirmations: KindConfirmations,
                    runtime: Any, actor: Any) -> dict[str, Any]:
    """Complete a request from the client's JSON-RPC response on the session transport.

    Returns the outcome for the host's log (a response to a server request gets no reply);
    a refusal is returned, not raised."""
    if not is_response(message):
        raise ValueError("not_an_elicitation_response")
    answer = message.get("result") if "result" in message else {"action": "cancel"}
    try:
        return confirmations.complete(runtime, actor, message["id"], answer)
    except ConfirmationRefused as refused:
        return {"schema": SCHEMA, "confirmed": False, "refused": refused.code,
                "business_system_writes": 0}


__all__ = ["ALLOWED", "ANSWERS", "COLUMNS", "DOCUMENT_KIND", "ConfirmationRefused",
           "KindConfirmations", "REVIEW_CHANNEL", "SCHEMA", "TOOL", "call_tool",
           "client_supports_elicitation", "confirmed_kinds", "handle_response", "store_path",
           "human_principal", "projection_step", "proposal_digest",
           "review_record", "sharepoint_binding"]
