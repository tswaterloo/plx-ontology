"""Mail inside cases: related messages, attachments and people, cited and permission-filtered.

WS-08 (related people and communications, subject to permissions), ING-02 /
ING-13 (Graph mail from the operational inbox allowlist), DOC-04 (originals
stay openable). For one case and one reading actor this module answers "which
mail belongs to this case, what did it carry, and who was on it", using only
what the actor may read now:

* **Which mail.** The versioned mail capture (``integrations.capture_mail``,
  the one L1 mail store; decision 2026-10-02-mail-and-mentions-store) is the
  index, read *as the case reader* (its live audience over the mailbox
  identities and owner ACL recorded at capture) and narrowed to the case mail
  scope: nothing is listed again from a mailbox, and the capture's own
  completeness (delta rounds, attachment lists) decides ``mail_source.complete``.
  The older ingestion mail unit (``operating.ingestion``) is no longer read
  here. Messages are related to the case by

  - the case's Petra purchase orders (``api://public.app_purchase_order/...``
    roots or cited rows), through the stored mention links of the projection
    (``ontology/mentions.py``: ``Email.mentions_<type>``), read from the store file
    read-only and re-gated for the case reader with no source call (``_stored_relations``:
    the message in the reader's scoped capture index, the PO row the reader resolved,
    no marking (a marked link is counted in ``withheld.po_links``, never shown), and every
    record its corroborating features cite readable, else a corroborated link is shown as
    its lead: the one rule of ``plx.object`` and ``plx.around``,
    ``ontology.mentions.corroboration_handles`` / ``reader_view``): a ``reviewed`` link (a person accepted it) is identity
    ``verified``, a ``corroborated`` one (two or more independent mention features
    agree: the mention, the sender at the supplier's distinctive domain, an internal
    mention in the same conversation) is identity ``corroborated``, a ``lead`` is
    for a person to confirm with ``plx.review`` (each relation names the link in
    ``review``). A person's decisions reach the links through the build
    (``ontology.reviews.link_step``: a rejected link ends the mention link) and,
    for a rejection recorded after the build (instants compared as instants, an
    unparseable one counts as after), through the review store read now
    (``person_reviews``: the Workbench state's ``reviews.sqlite``, read-only), so a
    rejected link never relates its message (ER-09); a store that is there but
    cannot be read refuses the case read (``CaseMailError``
    ``review_store_unreadable``), never reads as no decisions. Without a projection
    (``ontology_store``, default the host's ``projection/ontology.sqlite`` in the
    state directory) no PO relation is read and the evidence is not complete;
  - mail the case already cites (``graph-mail://`` citations of its findings);
  - the same conversation in the same mailbox as a related message
    (``same_conversation``, a lead: the thread is related, the content is
    not checked).

* **Permission.** Every message is re-read for the actor through the case
  runtime (``CaseRuntime.mail_message``: GET-only ``GraphMailReader``, mailbox
  audience and owner ACL before any message read). A mailbox the actor may not
  read is a withheld marker -- how many of its messages relate to the case is
  not shown. Only identifiers (ids, conversation ids) are taken from the
  capture index; every message shown is re-read for the reader.

* **Scope.** Only mailboxes in the case mail scope
  (``operating.case_mail_scope``, the reported 12-address allowlist) can enter
  a case: ``CaseRuntime`` applies the built-in scope by default (an
  ``MailScope.unrestricted()`` runtime is refused here), which refuses every
  read of another mailbox before any message is requested, and every record
  read here is checked against the scope again. Directory lookups are shared
  for one call (``GraphMailReader.directory_cache``), so the Graph cost stays bounded.

* **Originals.** A message is cited body-pinned
  (``graph-mail://<mailbox>/<message>@<changeKey>#body_sha256=..&shown_sha256=..``);
  a file attachment byte-pinned
  (``graph-mail://<mailbox>/<message>@<changeKey>#attachment=<id>&sha256=<hex>``,
  ``integrations.graph_evidence``); both re-read the source on every case read
  and ``CaseRuntime.read_mail_original`` returns the body / bytes through the
  same GET-only path. A PDF attachment can also be stored as an ``original://``
  (text, OCR, page citations) whose every read re-checks the message for the
  reader.

* **People.** Sender and recipients of the visible related messages, by
  e-mail address only (``graph_evidence.mail_people``): never merged, never
  resolved to a Portal contact or an employee.

``record=True`` writes one Workbench-written fact finding per related message
(citing the message, its attachments, stored originals and the case PO row),
so the case itself carries the mail; a finding is withheld from every reader
who cannot resolve all of its citations. A finding the case refuses at the write (a
citation that moved on or cannot be re-read now) is counted
(``findings_not_recorded``) and the other messages' findings are still written.
Nothing is sent, moved or written to any business system.

Supplier mail about freight or a supplier's own order number reaches a case only through
a stored mention link (the PO number it names, or a supplier number a person recorded as
the order's alias, ``ontology.reviews.BuildAliases``) or its conversation: no freight
keyword makes a relation. A number naming several orders is a lead to each
(``resolution: ambiguous``), never a conflict decided here.

CLI (credentialed live verifier; counts only, never subjects, addresses, ids,
numbers or citations)::

    python3 -m operating.case_mail scan --state-dir D --mail-capture-scope S \\
        --mirror-audience M --live-audience L --principal staff:purchasing \\
        --principal staff:operations --withheld-principal staff:operations
"""

from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

from integrations.live_source_guard import parse_handle
from ontology.core import Actor

from .case_mail_scope import MailScope, MailScopeError

SCHEMA = "plx-case-mail-evidence/v1"
COUNTS_SCHEMA = "plx-case-mail-evidence-counts/v1"
PO_RELATION = "public.app_purchase_order"
PO_PREFIX = f"api://{PO_RELATION}/"
MAIL_PREFIX = "graph-mail://"
MENTIONS_PO = "Email.mentions_purchase_order"
# Read into the mirror the projection is built from: the PO rows a mention can name (gold,
# FM, Sage) and the suppliers whose distinctive domains corroborate one.
SURVEY_RELATIONS = (("po_source", PO_RELATION),
                    ("fm_po_source", "public.generated_purchase_order"),
                    ("sage_po_source", "sage.po_header"),
                    ("supplier_source", "public.app_supplier"))
MAX_SURVEY_ROWS = 50_000
WITHHELD = "withheld_citation_unavailable"
MAX_MESSAGES = 50
MAX_ATTACHMENTS = 10           # per message
MAX_THREAD_SIBLINGS = 20       # per related message
INGEST_INDEX_LIMIT = 5000
DEFAULT_ATTACHMENT_BYTES = 20_000_000
_STATEMENT_MAX = 3900
LIMITATIONS = (
    "Related mail comes from the versioned mail capture's index of the captured mailbox "
    "folders (integrations.capture_mail), read as this reader: mail never captured is "
    "not found, and a message not found is not absent unless the capture is complete.",
    "Purchase-order relations are the projection's stored mention links, as fresh as its "
    "last build (a rejection recorded since is applied, an acceptance waits for the next "
    "build): reviewed (verified) and corroborated links are PO-number identity, not proof "
    "of the message's quantities, prices or dates; leads need a person. "
    "same_conversation is a lead. A marked link is counted as withheld, never shown.",
    "Freight and shipping mail relates only through a PO number or reviewed supplier alias "
    "it names, or its conversation; a number naming several orders is a lead to each.",
    "A mailbox the reader may not read is a withheld marker; how many of its messages "
    "relate to the case is not shown. Messages that changed or moved since they were "
    "indexed are counted as unreadable.",
    "Only mailboxes in the case mail scope (operating/case_mail_scope.json, the reported "
    "Graph allowlist) are read; the allowlist itself is reported, not live-verified "
    "against the Exchange policy group.",
    "People are e-mail addresses from the From/To/Cc headers of visible messages; no "
    "name is read and no address is resolved to a person, contact or employee.",
    "Attachment citations re-read and re-hash the bytes on every case read (cost grows "
    "with attachment size); only file attachments carry bytes (an attached message or a "
    "cloud link is listed, not cited).",
)


class CaseMailError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- helpers

def _mail_ref(handle: str) -> tuple[str, str] | None:
    try:
        (mailbox, message_id), _version, _extra = parse_handle(handle, "graph-mail", 2)
    except ValueError:
        return None
    return mailbox.lower(), message_id


def _key_text(key: Any) -> str:
    return json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)


def _clean(text: Any, limit: int = 300) -> str:
    value = "".join(ch if ch >= " " or ch in "\t" else " " for ch in str(text or ""))
    return value[:limit]


def capture_index(runtime: Any, actor: Actor, state_dir: str | Path, *,
                  limit: int = INGEST_INDEX_LIMIT, mail_scope: Any = None) -> dict[str, Any]:
    """The mail capture's index for this reader (``MailCaptureStore.index``): the
    reader's employee-gated actor under the runtime's live audience, optionally
    narrowed to ``mail_scope`` (``allows(identities)``). ``status``: ``read``,
    ``store_absent``, ``source_not_configured`` (no live audience or mail reader on
    this host) or ``withheld`` (the reader holds no mail grant at all)."""
    from integrations.capture_mail import MailCaptureStore, store_exists

    empty = {"items": [], "truncated": False, "complete": False, "mailboxes_in_scope": 0,
             "mailboxes_outside_scope": 0}
    live = getattr(runtime, "live", None)
    audience = getattr(live, "audience", None)
    if audience is None or getattr(live, "mail", None) is None:
        # No mail reader on this host: indexed messages could not be re-read.
        return {"status": "source_not_configured", **empty}
    reader = runtime.employee_gate(actor)
    if not audience.may_read_any(reader, "mail:"):
        return {"status": "withheld", **empty}
    if not store_exists(state_dir):
        return {"status": "store_absent", **empty}
    store = MailCaptureStore(state_dir)
    try:
        allowed = getattr(mail_scope, "allows", None)
        return store.index(reader, audience, limit=limit,
                           mailbox_allowed=allowed if callable(allowed) else None)
    finally:
        store.close()


# ---------------------------------------------------------------- evidence

def _case_inputs(runtime: Any, actor: Actor, case: Mapping[str, Any]) -> dict[str, Any]:
    """PO rows and mail the case names, as the actor may read them now."""
    from .cases import CitationUnresolvable

    handles = list(case.get("roots") or [])
    withheld_findings = 0
    for bucket, items in (case.get("findings") or {}).items():
        for finding in items:
            if bucket == "withheld":
                withheld_findings += 1
                continue
            handles += [c["handle"] for c in finding.get("citations") or []
                        if isinstance(c.get("handle"), str)]
    pos: dict[str, dict[str, Any]] = {}
    po_withheld = 0
    mail: list[tuple[str, str]] = []
    for handle in dict.fromkeys(handles):
        if handle.startswith(PO_PREFIX):
            try:
                view = runtime.resolve_citation(handle, actor)
            except CitationUnresolvable:
                po_withheld += 1
                continue
            if view.get("relation") == PO_RELATION and isinstance(view.get("row"), Mapping):
                pos.setdefault(_key_text(view.get("key")),
                               {"citation": handle, "key": view.get("key"),
                                "po_number": view["row"].get("po_number")})
        elif handle.startswith(MAIL_PREFIX):
            ref = _mail_ref(handle)
            if ref is not None and ref not in mail:
                mail.append(ref)
    return {"pos": pos, "po_withheld": po_withheld, "mail": mail,
            "findings_withheld": withheld_findings}


# Stored links from a mail message into a case's purchase order: the mention links
# (ontology/mentions.py) and a person's reviewed / contradicted links of the same types
# (ontology.reviews.link_step), current in the projection.
LINKS_SQL = ("SELECT l.link_id, l.link_type, l.from_object, l.to_object, l.tier, l.reason, "
             "l.why_not, l.evidence_handles, l.markings FROM links l JOIN objects o "
             "ON o.object_id = l.from_object WHERE l.to_object = ? AND o.object_type = 'Email' "
             "AND l.binding_kind IN ('mention', 'reviewed') AND l.superseded_at IS NULL "
             "ORDER BY l.link_id")
_IDENTITY = {"reviewed": "verified", "corroborated": "corroborated"}
_RANK = {"reviewed": 0, "corroborated": 1, "lead": 2}


def _email_ref(object_id: str) -> tuple[str, str] | None:
    """(mailbox, message id) of an ``Email`` object id (its native key)."""
    try:
        key = json.loads(object_id.split(":", 2)[2])
    except (IndexError, ValueError):
        return None
    if not isinstance(key, Mapping) or not all(isinstance(key.get(k), str)
                                               for k in ("mailbox", "id")):
        return None
    return key["mailbox"].lower(), key["id"]


def _relation(row: Mapping[str, Any], shown: bool) -> dict[str, Any]:
    """A stored link as a case relation. ``shown``: the reader may read every record the
    link's corroborating features cite; otherwise a corroborated link is shown as the lead
    its mention is (the corroboration rests on a record this reader may not read)."""
    from ontology.mentions import reader_view

    tier, reason, why_not = reader_view(row["tier"], json.loads(row["reason"]),
                                        json.loads(row["why_not"]), shown)
    via = {f.get("via") for f in reason if f.get("feature") == "identity_candidate"}
    relation: dict[str, Any] = {
        "basis": "reviewed_alias" if "reviewed_alias" in via else "mention_link",
        "status": tier, "identity_asserted": tier in _IDENTITY,
        "requires_human_confirmation": tier not in _IDENTITY,
        "features": sorted({f["feature"] for f in reason if f.get("agrees")}),
        "why_not": sorted({f["feature"] for f in why_not}),
        "review": {"subject": row["from_object"], "target": row["to_object"],
                   "link_type": row["link_type"]}}
    if tier in _IDENTITY:
        relation["identity"] = _IDENTITY[tier]
    return relation


def _instant(value: Any) -> str | None:
    """``value`` as the store's fixed-width UTC instant text (text order is time order),
    or None when it is not an ISO instant with a zone."""
    from ontology.store import StoreError, instant

    try:
        return instant(value)
    except StoreError:
        return None


def _rejected_since(runtime: Any, actor: Actor, built_at: str | None) -> set[tuple[str, str]]:
    """(Email, target) pairs a ``reject_link`` in force now names that the projection's
    build has not seen: recorded after ``built_at``, compared as instants (offsets and
    fractions apart). Fail-closed: without a readable ``built_at`` every rejection counts,
    and so does one whose own instant cannot be read."""
    from ontology import reviews as rv
    from .build_steps import ReviewStoreUnreadable

    try:
        history = person_reviews(runtime).link_history(actor.tenant)
    except ReviewStoreUnreadable as exc:
        raise CaseMailError("review_store_unreadable") from exc
    built = _instant(built_at)
    rejected = set()
    for r in rv.effective(history).values():
        if r["decision"] != "reject_link":
            continue
        recorded = _instant(r["recorded_at"])
        if built is None or recorded is None or recorded > built:
            rejected.add((r["subject"], r["target"]))
    return rejected


def _stored_relations(runtime: Any, actor: Actor, pos: Mapping[str, Mapping[str, Any]],
                      store: Path, in_index: set[tuple[str, str]],
                      mail_source: dict[str, Any], withheld: dict[str, Any]
                      ) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """The stored links into the case's purchase orders, by message, for ``actor``.

    Read-only, and re-gated for the reader without any source call: the message end must
    be in the reader's scoped capture index (``in_index``: its mailbox audience and the
    case mail scope; every related message is then re-read for the reader) and the PO end
    is the case's own row the reader resolved. A link with a marking is never shown (no
    case reader holds one): it is counted in ``withheld["po_links"]``. A corroborated link
    keeps its tier only when the reader may read every record its corroborating features
    cite (``ontology.mentions.corroboration_handles``: a message in ``in_index``, a mirror
    row the reader resolves; a cited version not in the store reads as unreadable), the one
    rule of ``plx.object`` and ``plx.around``; any other record the link cites does not
    decide it. A link of a pair a person rejected (in the projection: contradicted;
    recorded since the build: in the review store now) relates nothing."""
    from ontology.mentions import corroboration_handles, corroboration_objects
    from .cases import CitationUnresolvable

    found: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if not store.is_file():
        mail_source.update(po_links="projection_not_built", complete=False)
        return found
    readable: dict[str, bool] = {}

    def may_read(handle: str) -> bool:
        if handle not in readable:
            ref = _mail_ref(handle) if handle.startswith(MAIL_PREFIX) else None
            if ref is not None:
                readable[handle] = ref in in_index
            else:
                try:
                    runtime.resolve_citation(handle, actor)
                    readable[handle] = True
                except CitationUnresolvable:
                    readable[handle] = False
        return readable[handle]

    statuses: set[str] = set()
    in_store: dict[str, str | None] = {}
    with closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        meta = dict(conn.execute("SELECT key, value FROM store_meta").fetchall())
        rejected = _rejected_since(runtime, actor, meta.get("built_at"))
        for po in pos.values():
            target = conn.execute("SELECT object_id FROM object_versions WHERE l1_handle = ?",
                                  (po["citation"],)).fetchone()
            if target is None:
                statuses.add("po_not_in_projection")
                continue
            rows = conn.execute(LINKS_SQL, (target[0],)).fetchall()
            refused = {r["from_object"] for r in rows if r["tier"] == "contradicted"}
            best: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                ref = _email_ref(row["from_object"])
                if (ref is None or ref not in in_index or row["tier"] == "contradicted"
                        or row["from_object"] in refused
                        or (row["from_object"], row["to_object"]) in rejected):
                    continue
                if json.loads(row["markings"]):
                    withheld["po_links"] += 1
                    continue
                reason = json.loads(row["reason"])
                cited = corroboration_handles(reason)
                shown = (cited is not None
                         and corroboration_objects(conn, reason, in_store) is not None
                         and all(may_read(h) for h in cited
                                 if h != po["citation"] and _mail_ref(h) != ref))
                relation = _relation(row, shown)
                # One relation per message and order: its strongest link (a message
                # naming the number in the subject and the body holds two).
                held = best.get(ref)
                if held is None or _RANK[relation["status"]] < _RANK[held["status"]]:
                    best[ref] = relation
            for ref, relation in best.items():
                found.setdefault(ref, []).append(dict(relation, po_citation=po["citation"],
                                                      po_number=po["po_number"]))
    mail_source["po_links"] = ",".join(sorted(statuses)) or "read"
    if statuses:
        mail_source["complete"] = False
    return found


def _attachments(runtime: Any, actor: Actor, record: Mapping[str, Any], *,
                 max_bytes: int, attach_pdf_originals: bool,
                 counts: dict[str, int]) -> list[dict[str, Any]]:
    from .case_originals import OriginalError
    from .cases import CitationUnresolvable

    if record.get("has_attachments") is not True:
        return []
    try:
        listing = runtime.mail_attachments(actor, record["mailbox"], record["id"])
    except CitationUnresolvable:
        counts["attachment_listings_unavailable"] += 1
        return []
    if (listing.get("completeness") or {}).get("truncated"):
        counts["attachment_listings_truncated"] += 1
    out = []
    for item in listing["items"]:
        counts["attachments_listed"] += 1
        entry: dict[str, Any] = {"id": item.get("id"), "name": item.get("name"),
                                 "content_type": item.get("content_type"),
                                 "size": item.get("size"), "is_inline": item.get("is_inline"),
                                 "citation": None}
        out.append(entry)
        if item.get("kind") != "#microsoft.graph.fileAttachment":
            entry["not_cited"] = "not_a_file_attachment"
            continue
        if isinstance(item.get("size"), int) and item["size"] > max_bytes + 64_000:
            # Graph's size is a little above the file's; far above the bound: not read.
            entry["not_cited"] = "exceeds_byte_bound"
            counts["attachments_over_bound"] += 1
            continue
        if len([e for e in out if e.get("citation")]) >= MAX_ATTACHMENTS:
            entry["not_cited"] = "attachment_bound_reached"
            continue
        try:
            read = runtime.mail_attachment(actor, record["mailbox"], record["id"], item["id"],
                                           max_bytes=max_bytes)
        except CitationUnresolvable:
            entry["not_cited"] = "unreadable_or_over_bound"
            counts["attachments_unreadable"] += 1
            continue
        content = read["content"]
        if sha256(content).hexdigest() != read["attachment"]["content_sha256"]:
            entry["not_cited"] = "content_hash_mismatch"  # pragma: no cover - reader checks
            continue
        entry.update(citation=read["citation"], bytes=len(content),
                     content_sha256=read["attachment"]["content_sha256"])
        counts["attachments_cited"] += 1
        counts["attachment_bytes_read"] += len(content)
        if attach_pdf_originals and content.startswith(b"%PDF-"):
            try:
                original = runtime.attach_mail_attachment_original(
                    actor, record["mailbox"], record["id"], item["id"], max_bytes=max_bytes)
            except OriginalError as exc:
                entry["original_error"] = exc.code
                continue
            entry["original"] = {"citation": original["citation"],
                                 "page_citations": original["page_citations"],
                                 "sha256": original["sha256"],
                                 "extraction_status": original["extraction"]["status"],
                                 "ocr_pages": original["extraction"]["ocr_pages"]}
            counts["originals_attached"] += 1
    return out


def case_mail_evidence(runtime: Any, actor: Actor, case_id: str, *,
                       mail_capture_dir: str | Path | None,
                       include_attachments: bool = True,
                       attachment_max_bytes: int = DEFAULT_ATTACHMENT_BYTES,
                       attach_pdf_originals: bool = False, thread: bool = True,
                       record: bool = False, max_messages: int = MAX_MESSAGES,
                       internal_domains: Iterable[str] = (),
                       ontology_store: str | Path | None = None) -> dict[str, Any]:
    """Mail evidence of one case for ``actor`` (see module docstring).

    ``mail_capture_dir``: the state directory of the mail capture (read as
    ``actor``, narrowed to the case mail scope). ``ontology_store``: the projection
    whose stored mention links relate mail to the case's purchase orders (default
    ``operating.host.projection_path`` of the runtime's state directory). Directory
    lookups are shared for the whole call (``GraphMailReader.directory_cache``).
    """
    from .employee_permissions import EmployeeAccessRefused
    from .host import projection_path

    if not isinstance(actor, Actor):
        raise CaseMailError("invalid_actor")
    scope = getattr(runtime, "mail_scope", None)
    if not isinstance(scope, MailScope):
        raise CaseMailError("mail_scope_not_configured")
    if scope.is_unrestricted:
        raise CaseMailError("mail_scope_unrestricted")
    if (isinstance(max_messages, bool) or not isinstance(max_messages, int)
            or not 1 <= max_messages <= MAX_MESSAGES):
        raise CaseMailError("invalid_max_messages")
    if (isinstance(attachment_max_bytes, bool) or not isinstance(attachment_max_bytes, int)
            or not 1 <= attachment_max_bytes <= DEFAULT_ATTACHMENT_BYTES):
        raise CaseMailError("invalid_attachment_max_bytes")
    internal = frozenset(scope.internal_domains | {d.strip().lower() for d in internal_domains
                                                   if isinstance(d, str) and d.strip()})
    counts = {"po_roots": 0, "mail_cited_in_case": 0, "messages_related": 0,
              "messages_visible": 0, "messages_unreadable": 0, "messages_outside_scope": 0,
              "thread_siblings_read": 0, "attachments_listed": 0, "attachments_cited": 0,
              "attachments_unreadable": 0, "attachments_over_bound": 0,
              "attachment_listings_unavailable": 0, "attachment_listings_truncated": 0,
              "attachment_bytes_read": 0, "originals_attached": 0, "people": 0,
              "people_internal": 0, "people_external": 0, "findings_recorded": 0,
              "findings_already_cited": 0, "findings_not_recorded": 0}
    result: dict[str, Any] = {
        "schema": SCHEMA, "case_id": case_id, "evidence_role": "case_mail_evidence",
        "identity_asserted": False, "mail_scope": scope.describe(),
        "limitations": list(LIMITATIONS), "messages": [], "people": [],
        "withheld": {"markers": [], "mailboxes": 0, "mailboxes_not_visible_or_unavailable": 0,
                     "messages_unknown": False, "messages_unreadable": 0,
                     "case_findings": 0, "po_rows": 0, "po_links": 0},
        "counts": counts, "findings": [], "business_system_writes": 0}
    try:
        runtime.employee_gate(actor)
    except EmployeeAccessRefused:
        result["actor_status"] = "refused"
        result["withheld"].update(messages_unknown=True)
        result["withheld"]["markers"].append({"scope": "case", "content": WITHHELD})
        return result
    result["actor_status"] = "accepted"
    case = runtime.cases.get(actor, case_id)  # CaseNotFound: not visible == not there
    inputs = _case_inputs(runtime, actor, case)
    result["withheld"]["case_findings"] = inputs["findings_withheld"]
    result["withheld"]["po_rows"] = inputs["po_withheld"]
    counts["po_roots"] = len(inputs["pos"])
    counts["mail_cited_in_case"] = len(inputs["mail"])
    related: dict[tuple[str, str], list[dict[str, Any]]] = {}
    mail_source: dict[str, Any] = {"mail_capture": "not_used", "complete": True}
    result["mail_source"] = mail_source
    store = Path(ontology_store if ontology_store is not None
                 else projection_path(Path(runtime.state_dir)))
    with directory_cache(runtime):
        index = _evidence_sources(runtime, actor, inputs, mail_capture_dir, scope, related,
                                  result, store=store, thread=thread)
        return _evidence_messages(runtime, actor, case_id, case, related, index, result,
                                  include_attachments=include_attachments,
                                  attachment_max_bytes=attachment_max_bytes,
                                  attach_pdf_originals=attach_pdf_originals, thread=thread,
                                  record=record, max_messages=max_messages,
                                  internal=internal)


def person_reviews(runtime: Any) -> Any:
    """The Workbench's review store of ``runtime``'s state, read-only and never created
    (``build_steps.ReviewsIfPresent``, strict: an unreadable store raises rather than
    reading as no decisions)."""
    from ontology.reviews import store_path
    from .build_steps import ReviewsIfPresent

    return ReviewsIfPresent(store_path(runtime.state_dir), strict=True)


def directory_cache(runtime: Any) -> Any:
    """The mail reader's per-run directory cache, or a no-op without one."""
    mail = getattr(getattr(runtime, "live", None), "mail", None)
    block = getattr(mail, "directory_cache", None)
    return block() if callable(block) else nullcontext()


def _evidence_sources(runtime: Any, actor: Actor, inputs: Mapping[str, Any],
                      capture_dir: str | Path | None, scope: MailScope,
                      related: dict[tuple[str, str], list[dict[str, Any]]],
                      result: dict[str, Any], *, store: Path,
                      thread: bool) -> dict[str, Any] | None:
    """PO relations (stored mention links, kept to the scoped capture index) and the
    thread index."""
    mail_source = result["mail_source"]
    index = None
    if capture_dir is not None and (inputs["pos"] or (thread and inputs["mail"])):
        index = capture_index(runtime, actor, capture_dir, mail_scope=scope)
        mail_source.update(mail_capture=index["status"],
                           index_mailboxes_outside_scope=index["mailboxes_outside_scope"])
        if index["status"] == "withheld":
            # The reader holds no mail grant at all: one withheld mailbox, count unknown.
            withheld = result["withheld"]
            withheld["mailboxes"] += 1
            withheld["messages_unknown"] = True
            withheld["markers"].append({"scope": "mailbox", "content": WITHHELD})
    if capture_dir is None and (inputs["pos"] or (thread and inputs["mail"])):
        # A thread-only case (mail cited, no PO) also needs the capture: its siblings
        # are searched there, so without one the evidence is not complete.
        mail_source.update(mail_capture="not_configured", complete=False)
    if inputs["pos"] and index is not None:
        if index["truncated"] or not index["complete"] or index["status"] != "read":
            mail_source["complete"] = False
    if inputs["pos"] and index is not None and index["status"] == "read":
        in_index = {(item["mailbox"].lower(), item["message_id"]) for item in index["items"]}
        for ref, relations in _stored_relations(runtime, actor, inputs["pos"], store,
                                                in_index, mail_source,
                                                result["withheld"]).items():
            related.setdefault(ref, []).extend(relations)
        if result["withheld"]["po_links"]:
            result["withheld"]["markers"].append({"scope": "po_link", "content": WITHHELD})
    for ref in inputs["mail"]:
        related.setdefault(ref, []).append({"basis": "cited_in_case", "status": "cited"})
    if not (thread and related) or index is None:
        return None
    # Thread siblings are searched in the capture index: it is only as complete as
    # the capture is for this reader (rounds, attachment lists, page budget, cut).
    if index["truncated"] or not index["complete"] or index["status"] != "read":
        mail_source["complete"] = False
    return index


def _evidence_messages(runtime: Any, actor: Actor, case_id: str, case: Mapping[str, Any],
                       related: dict[tuple[str, str], list[dict[str, Any]]],
                       index: Mapping[str, Any] | None, result: dict[str, Any], *,
                       include_attachments: bool, attachment_max_bytes: int,
                       attach_pdf_originals: bool, thread: bool, record: bool,
                       max_messages: int, internal: frozenset[str]) -> dict[str, Any]:
    """Re-read every related message for ``actor``: entries, attachments, people."""
    from integrations.graph_evidence import mail_people
    from .case_runtime import LiveSourceNotConfigured
    from .cases import CitationUnresolvable
    from .employee_permissions import EmployeeAccessRefused

    counts, mail_source = result["counts"], result["mail_source"]
    messages: dict[tuple[str, str], dict[str, Any]] = {}
    queue = list(related)

    def read(ref: tuple[str, str]) -> dict[str, Any] | None:
        try:
            message = runtime.mail_message(actor, ref[0], ref[1])
        except LiveSourceNotConfigured:
            mail_source["complete"] = False
            mail_source["mail_reader"] = "source_not_configured"
            return None
        except (CitationUnresolvable, EmployeeAccessRefused):
            result["withheld"]["messages_unreadable"] += 1
            counts["messages_unreadable"] += 1
            return None
        if message.get("mailbox_in_scope") is not True:  # defense in depth: never enters
            counts["messages_outside_scope"] += 1
            return None
        return message

    position = 0
    while position < len(queue):
        ref = queue[position]
        position += 1
        if len(messages) >= max_messages:
            mail_source["complete"] = False
            mail_source["message_bound_reached"] = True
            break
        message = read(ref)
        if message is None:
            continue
        record = message["record"]
        key = (record["mailbox"].lower(), record["id"])
        entry = messages.get(key)
        if entry is None:
            entry = {"citation": message["citation"],
                     "metadata_citation": message["metadata_citation"],
                     "mailbox": record["mailbox"], "mailbox_upn": record.get("mailbox_upn"),
                     "received_at": record.get("received_at"), "sent_at": record.get("sent_at"),
                     "subject": record.get("subject"), "from": record.get("from"),
                     "to": list(record.get("to") or []), "cc": list(record.get("cc") or []),
                     "has_attachments": record.get("has_attachments"),
                     "original_url": record.get("original_url"),
                     "body_sha256": message["body_sha256"],
                     "body_chars": message["body_chars"],
                     "body_truncated": message["body_truncated"],
                     "relations": [], "attachments": [],
                     "_record": {**record, "source_acl": {
                         "identities": list(message.get("mailbox_identities") or [])}}}
            messages[key] = entry
        entry["relations"].extend(related.get(ref, []))
        if not thread or index is None or not record.get("conversation_id"):
            continue
        siblings = [(item["mailbox"], item["message_id"]) for item in index["items"]
                    if item["mailbox"] == key[0] and item["message_id"] != key[1]
                    and item["conversation_id"] == record["conversation_id"]]
        for sibling in siblings[:MAX_THREAD_SIBLINGS]:
            if sibling in related:
                continue
            related[sibling] = [{"basis": "same_conversation", "status": "lead",
                                 "identity_asserted": False,
                                 "with": message["metadata_citation"]}]
            queue.append(sibling)
            counts["thread_siblings_read"] += 1
    counts["messages_related"] = len(related)
    for _ in range(result["withheld"]["case_findings"]):
        result["withheld"]["markers"].append({"scope": "case_finding", "content": WITHHELD})
    for entry in messages.values():
        if include_attachments:
            entry["attachments"] = _attachments(runtime, actor, entry["_record"],
                                                max_bytes=attachment_max_bytes,
                                                attach_pdf_originals=attach_pdf_originals,
                                                counts=counts)
    shown = sorted(messages.values(), key=lambda m: (str(m.get("received_at") or ""),
                                                     m["metadata_citation"]))
    result["people"] = mail_people([{"citation": m["citation"], "record": m["_record"]}
                                    for m in shown], internal_domains=internal)
    counts["messages_visible"] = len(shown)
    counts["people"] = len(result["people"])
    counts["people_internal"] = sum(1 for p in result["people"] if p["internal"])
    counts["people_external"] = counts["people"] - counts["people_internal"]
    if record and shown:
        result["findings"] = _record_findings(runtime, actor, case_id, case, shown, counts)
    result["messages"] = [{k: v for k, v in m.items() if k != "_record"} for m in shown]
    return result



def _statement(message: Mapping[str, Any]) -> str:
    cited = [a for a in message["attachments"] if a.get("citation")]
    names = ", ".join(f"'{_clean(a.get('name'), 120)}' ({a['bytes']} bytes, sha256 "
                      f"{a['content_sha256']})" for a in cited)
    relations = []
    for relation in message["relations"]:
        if relation.get("po_number") is not None:
            decided = (f"; identity: {relation['identity']}"
                       if relation.get("identity") else "")
            relations.append(f"purchase order {_clean(relation['po_number'], 80)} by "
                             f"{relation['basis']} ({relation['status']}{decided})")
        else:
            relations.append(f"{relation['basis']} ({relation['status']})")
    text = (f"Mail message in mailbox {message.get('mailbox_upn')}: from "
            f"{message.get('from') or 'unknown sender'} to "
            f"{', '.join(message.get('to') or []) or 'no listed recipient'}"
            f"{'; cc ' + ', '.join(message['cc']) if message.get('cc') else ''}, received "
            f"{message.get('received_at') or 'unknown'}, subject "
            f"'{_clean(message.get('subject'))}'; plain-text body ({message['body_chars']} "
            f"characters{', truncated at the bound' if message['body_truncated'] else ''}) "
            f"sha256 {message['body_sha256']}; "
            f"{len(cited)} attachment(s) cited{': ' + names if names else ''}. "
            f"Related to this case: {'; '.join(relations) or 'cited in case'}. "
            "A relation marked lead is for a person to confirm; a verified or "
            "corroborated link is PO-number "
            "identity, not proof of the quantities, prices or dates in the message.")
    return _clean(text, _STATEMENT_MAX)


def _record_findings(runtime: Any, actor: Actor, case_id: str, case: Mapping[str, Any],
                     shown: Sequence[Mapping[str, Any]], counts: dict[str, int]
                     ) -> list[dict[str, Any]]:
    """One Workbench-written fact finding per visible related message. A message whose
    finding the case refuses (a citation that moved on, or that cannot be re-read now:
    its source unavailable or no longer visible) has no finding, its error recorded and
    counted (``findings_not_recorded``); it never ends the read of the other messages."""
    from ontology.core import ValidationError
    from .cases import CitationUnresolvable

    cited = {c.get("handle") for items in (case.get("findings") or {}).values()
             for finding in items for c in finding.get("citations") or []}
    findings = []
    for message in shown:
        if message["citation"] in cited:
            counts["findings_already_cited"] += 1
            continue
        handles = [message["citation"]]
        for attachment in message["attachments"]:
            if attachment.get("citation"):
                handles.append(attachment["citation"])
            if attachment.get("original"):
                handles.append(attachment["original"]["citation"])
        handles += [r["po_citation"] for r in message["relations"] if r.get("po_citation")]
        handles = list(dict.fromkeys(handles))[:20]
        try:
            finding = runtime.cases.finding(actor, case_id, _statement(message), "fact",
                                            "supported", handles, workbench_statement=True)
        except (ValidationError, CitationUnresolvable) as exc:
            # e.g. a citation that moved on, or whose source failed, between the read and
            # the write.
            counts["findings_not_recorded"] += 1
            # The refusal's code (its text up to the first ':'), value-free for the counts.
            code = str(exc).split(":", 1)[0]
            findings.append({"message_citation": message["citation"],
                             "error": str(exc)[:120],
                             "refusal": code if code.isidentifier() and code.islower()
                             and len(code) <= 64 else "unparsed"})
            continue
        counts["findings_recorded"] += not finding.get("merged")   # a repeat adds none
        findings.append({"message_citation": message["citation"],
                         "finding_id": finding.get("finding_id"), "citations": handles})
    return findings


# ---------------------------------------------------------------- counts / CLI

def counts_only(report: Mapping[str, Any], *, evidence_label: str) -> dict[str, Any]:
    """No subjects, addresses, ids, numbers or citations: counts and enums only."""
    relations: dict[str, int] = {}
    for message in report.get("messages") or []:
        for relation in message["relations"]:
            name = f"{relation['basis']}:{relation['status']}"
            relations[name] = relations.get(name, 0) + 1
    refused: dict[str, int] = {}
    for finding in report.get("findings") or []:
        if finding.get("refusal"):
            refused[finding["refusal"]] = refused.get(finding["refusal"], 0) + 1
    withheld = report.get("withheld") or {}
    return {"schema": COUNTS_SCHEMA, "evidence_label": evidence_label,
            "actor_status": report.get("actor_status"),
            "counts": dict(report.get("counts") or {}), "relations": relations,
            "findings_refused": refused,
            "withheld": {k: v for k, v in withheld.items() if k != "markers"}
            | {"markers": len(withheld.get("markers") or [])},
            "mail_source": {k: v for k, v in (report.get("mail_source") or {}).items()
                            if isinstance(v, (bool, int, str)) or v is None},
            "mail_scope": report.get("mail_scope")}


def roundtrip(runtime: Any, actor: Actor, report: Mapping[str, Any]) -> dict[str, int]:
    """Re-read every message and attachment citation of ``report`` for ``actor``
    through ``CaseRuntime.read_mail_original`` (GET-only reader) and check the
    body / bytes against the cited hash; also check every read record's mailbox
    against the mail scope (``outside_scope`` must stay 0)."""
    result = {"checked": 0, "ok": 0, "bytes_match": 0, "body_match": 0, "changed_since": 0,
              "unresolvable": 0, "outside_scope": 0}
    for message in report.get("messages") or []:
        handles = [message["citation"]] + [a["citation"] for a in message["attachments"]
                                           if a.get("citation")]
        for handle in handles:
            result["checked"] += 1
            read = runtime.read_mail_original(handle, actor)
            if not read.get("ok"):
                key = ("changed_since" if read.get("error") == "citation_changed_since"
                       else "unresolvable")
                result[key] += 1
                continue
            result["ok"] += 1
            if read["kind"] == "mail_attachment":
                cited = handle.rsplit("sha256=", 1)[1]
                result["bytes_match"] += sha256(read["content"]).hexdigest() == cited
            else:
                cited = handle.split("body_sha256=", 1)[1].split("&", 1)[0]
                result["body_match"] += sha256(read["body_text"].encode("utf-8")
                                               ).hexdigest() == cited
            # The runtime decides on the mailbox's full directory identity set.
            result["outside_scope"] += read.get("mailbox_in_scope") is False
    return result


def evidence_label(report: Mapping[str, Any], injected: bool) -> str:
    """``no-source-read`` unless mail was read into the case: at least one message
    visible to the reader. A PO relation read alone is not mail evidence."""
    counts = report.get("counts") or {}
    if counts.get("messages_visible", 0) <= 0:
        return "no-source-read"
    return "injected-transport" if injected else "live-read"


def read_relations(runtime: Any, actor: Actor, *, max_staleness: float | None
                   ) -> dict[str, dict[str, Any]]:
    """Read the ``SURVEY_RELATIONS`` for ``actor`` into the source mirror the projection
    is built from (GET-only, bounded pages; ``integrations.source_mirror.scan_relation``):
    per part, its status, rows read, completeness and reasons, value-free, and the
    Portal's failure (``upstream``) when the Portal failed the read."""
    from integrations.source_mirror import scan_relation

    out: dict[str, dict[str, Any]] = {}
    for part, relation in SURVEY_RELATIONS:
        scan = scan_relation(runtime.mirror, actor, relation, MAX_SURVEY_ROWS, max_staleness)
        out[part] = {"relation": relation, "status": scan["status"],
                     "rows_scanned": len(scan["rows"]), "complete": scan["complete"],
                     "reasons": list(scan["reasons"]),
                     **({"upstream": dict(scan["upstream"])} if "upstream" in scan else {})}
    return out


def blocked_upstream(sources: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The relation reads the Portal failed (``source_unavailable``): the live verifier's
    ``blocked_upstream``. A relation withheld from the reader is not blocked."""
    return [{"part": part, "source": "portal-workbench-api", **entry["upstream"]}
            for part, entry in sources.items() if "upstream" in entry]


def unread_inputs(read: Mapping[str, int], blocked: Iterable[str],
                  required: Iterable[str] | None = None) -> dict[str, Any]:
    """Which inputs the run read (``read``: rows or messages per input):
    ``inputs_read`` counts the inputs with at least one, ``unblocked_unread`` names
    the ``required`` ones (default: all) that read nothing although no
    ``blocked_upstream`` part stopped them (``blocked``): a product gap even in a
    blocked run, so the live checks' ``expect_blocked`` requires it empty."""
    stopped = set(blocked)
    needed = set(read if required is None else required)
    return {"inputs_read": sum(1 for count in read.values() if count > 0),
            "unblocked_unread": sorted(name for name in needed
                                       if read.get(name, 0) <= 0 and name not in stopped)}


# Every current stored link from a mail message to a purchase order, with the order's
# current version (its L1 handle is the case root a pick opens).
SURVEY_SQL = ("SELECT l.link_id, l.from_object, l.to_object, l.tier, l.markings, "
              "v.l1_handle FROM links l JOIN objects o ON o.object_id = l.from_object "
              "JOIN object_versions v ON v.object_id = l.to_object AND v.superseded_at IS NULL "
              "WHERE l.link_type = ? AND o.object_type = 'Email' "
              "AND l.binding_kind IN ('mention', 'reviewed') AND l.superseded_at IS NULL "
              "ORDER BY l.link_id")


def mail_survey(runtime: Any, actor: Actor, store: Path, index: Mapping[str, Any], *,
                max_picks: int) -> tuple[dict[str, Any], list[str]]:
    """What the projection's stored mention links say about the mail ``actor`` may read
    (``index``: its scoped capture index), value-free, and up to ``max_picks`` Portal
    purchase orders they name, strongest link first (reviewed, corroborated, lead; then
    link id), each one the reader resolves: which purchase orders become cases is data,
    never a hardcoded case.
    A link whose message is outside the reader's index is not counted; a marked one is
    counted as withheld, never as a link or a pick; a contradicted one is counted, never
    picked."""
    from .cases import CitationUnresolvable

    in_index = {(item["mailbox"].lower(), item["message_id"]) for item in index["items"]}
    survey: dict[str, Any] = {
        "messages_scanned": len(in_index), "mail_capture": index["status"],
        "mail_complete": bool(index["status"] == "read" and index["complete"]
                              and not index["truncated"]),
        "messages_with_po_links": 0, "po_links": 0, "links_by_tier": {},
        "po_links_withheld": 0, "pos_linked": 0, "projection": "read"}
    if not store.is_file():
        survey["projection"] = "not_built"
        return survey, []
    ranked: dict[str, tuple[int, str]] = {}
    messages, orders = set(), set()
    with closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True)) as conn:
        for link_id, email, order, tier, markings, handle in conn.execute(
                SURVEY_SQL, (MENTIONS_PO,)):
            if _email_ref(email) not in in_index:
                continue
            if json.loads(markings):
                survey["po_links_withheld"] += 1
                continue
            survey["po_links"] += 1
            survey["links_by_tier"][tier] = survey["links_by_tier"].get(tier, 0) + 1
            messages.add(email)
            orders.add(order)
            if tier in _RANK and handle.startswith(PO_PREFIX):
                ranked[handle] = min(ranked.get(handle, (9, "")), (_RANK[tier], link_id))
    survey.update(messages_with_po_links=len(messages), pos_linked=len(orders))
    picks = []
    for handle in sorted(ranked, key=ranked.__getitem__):
        if len(picks) >= max_picks:
            break
        try:
            runtime.resolve_citation(handle, actor)
        except CitationUnresolvable:
            continue
        picks.append(handle)
    return survey, picks


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         graph_sender: Any = None, bc_sender: Any = None, workbench_fetch: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m operating.case_mail",
        description="Counts-only mail evidence for purchase-order cases (read-only).")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--state-dir", required=True, type=Path)
    scan.add_argument("--mail-capture-scope", type=Path, default=None,
                      help="plx-mail-capture-scope/v1 file: its mailboxes inside the case mail "
                           "scope are captured once into --state-dir (GET-only, as "
                           "--capture-principal) before the cases are built")
    scan.add_argument("--capture-principal", default="svc:mail-capture")
    scan.add_argument("--capture-page-budget", type=int, default=None,
                      help="delta pages per folder for this run (a bounded live check); a "
                           "folder stopped by it is incomplete and resumes on the next run")
    scan.add_argument("--mirror-audience", required=True, type=Path)
    scan.add_argument("--live-audience", required=True, type=Path)
    scan.add_argument("--mail-scope", type=Path, default=None)
    scan.add_argument("--principal", action="append", required=True)
    scan.add_argument("--withheld-principal", action="append", default=[],
                      help="a second reader (a subset of --principal) checked for withheld "
                           "markers on the same cases")
    scan.add_argument("--tenant", default="plx")
    scan.add_argument("--max-cases", type=int, default=2)
    scan.add_argument("--attach-pdf-originals", action="store_true")
    scan.add_argument("--attachment-max-bytes", type=int, default=DEFAULT_ATTACHMENT_BYTES)
    scan.add_argument("--max-staleness", type=float, default=None)
    scan.add_argument("--base-url-env", default="PLX_WORKBENCH_API_URL")
    scan.add_argument("--key-env", default="PLX_WORKBENCH_API_KEY")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from .build_steps import ReviewStoreUnreadable
    from .case_runtime import CaseRuntime, build_live_readers
    from .case_workspace_live import project_state
    from .host import projection_path
    from integrations.capture_mail import CaptureError, CaptureScope, capture_into
    from .ingestion import IngestError, load_live_audience

    def fail(code: str, status: int = 2) -> int:
        print(json.dumps({"error": code}), file=sys.stderr)
        return status

    if not 1 <= args.max_cases <= 5:
        return fail("invalid_max_cases")
    try:
        scope = (MailScope.from_file(args.mail_scope) if args.mail_scope
                 else MailScope.builtin())
        audience = load_live_audience(args.live_audience, env)
    except (MailScopeError, IngestError) as exc:
        return fail(exc.code)
    principals = frozenset(args.principal)
    if not set(args.withheld_principal) <= principals:
        return fail("withheld_principal_not_held")
    base_url, key = env.get(args.base_url_env), env.get(args.key_env)
    client = None
    if base_url and key:
        client = (WorkbenchReadApiClient(base_url, key, fetch=workbench_fetch)
                  if workbench_fetch is not None else WorkbenchReadApiClient(base_url, key))
    live = build_live_readers(audience, env, graph_sender=graph_sender, bc_sender=bc_sender)
    from integrations.people_exclusion import PeopleExclusion
    # The captured mail a case cites is re-checked against the exclusion this run reads.
    runtime = CaseRuntime(args.state_dir, client=client, audience=args.mirror_audience,
                          tenant=args.tenant, live=live, mail_scope=scope,
                          people_exclusion=PeopleExclusion.from_env(env))
    actor = Actor(args.tenant, principals, "restricted")
    output: dict[str, Any] = {"schema": COUNTS_SCHEMA, "cases": [], "capture": None}
    transport = getattr(live, "graph_transport", None)
    sent_before = getattr(transport, "requests_sent", 0)
    cache = directory_cache(runtime)
    sources: dict[str, dict[str, Any]] = {}
    ran: dict[str, Any] | None = None
    cache.__enter__()
    try:
        if args.mail_capture_scope is not None:
            try:
                capture_scope = CaptureScope.from_file(args.mail_capture_scope)
                if live.mail is None:
                    raise CaptureError("mail_reader_not_configured")
                ran = capture_into(
                    args.state_dir, capture_scope, live.mail,
                    Actor(args.tenant, frozenset({args.capture_principal}), "restricted"),
                    allow_address=lambda address: scope.allows([address]),
                    page_budget=args.capture_page_budget)
            except CaptureError as exc:
                return fail(exc.code)
            output["capture"] = {
                "mailboxes_in_scope": ran["mailboxes_in_scope"],
                "mailboxes_outside_scope": ran["mailboxes_outside_scope"],
                "complete": ran["complete"], "folders": ran["folders"],
                "folders_complete": ran["folders_complete"], "reasons": ran["reasons"],
                "messages_seen": ran["counts"]["messages_seen"],
                "versions_new": ran["counts"]["versions_new"],
                "attachments_captured": ran["counts"]["attachments_captured"],
                "attachment_lists_incomplete": ran["counts"]["attachment_lists_incomplete"],
                "pages": ran["counts"]["pages"]}
        # The PO and supplier rows go into the mirror, the projection is built from it
        # and the mail capture, and its stored mention links say which purchase orders
        # the captured mail names (data, never a hardcoded case): at most --max-cases,
        # strongest link first.
        sources = read_relations(runtime, actor, max_staleness=args.max_staleness)
        started = time.monotonic()
        try:
            built = project_state(args.state_dir, projection_path(args.state_dir),
                                  tenant=args.tenant,
                                  internal_domains=sorted(scope.internal_domains))
        except ReviewStoreUnreadable:
            return fail("review_store_unreadable")
        output["projection"] = {
            "seconds": round(time.monotonic() - started, 3),
            "links": built["counts"].get("links", 0), **{
                k: v for k, v in built["counts"].items()
                if k in ("links.lead", "links.corroborated", "links.reviewed")}}
        survey, picks = mail_survey(runtime, actor, projection_path(args.state_dir),
                                    capture_index(runtime, actor, args.state_dir,
                                                  mail_scope=scope),
                                    max_picks=args.max_cases)
        po = sources["po_source"]
        survey.update(po_rows_scanned=po["rows_scanned"], po_scan_status=po["status"],
                      relations={part: {k: entry[k] for k in ("status", "rows_scanned",
                                                              "complete")}
                                 for part, entry in sources.items()})
        output["survey"] = survey
        injected = any(x is not None for x in (graph_sender, bc_sender, workbench_fetch))
        audience_set = set(principals)
        for po_citation in picks:
            case_id = runtime.cases.open(actor, "Which mail, attachments and people belong "
                                         "to this purchase order?", roots=[po_citation],
                                         audience=sorted(audience_set))
            report = case_mail_evidence(runtime, actor, case_id,
                                        mail_capture_dir=args.state_dir, record=True,
                                        attach_pdf_originals=args.attach_pdf_originals,
                                        attachment_max_bytes=args.attachment_max_bytes)
            summary = counts_only(report, evidence_label=evidence_label(report, injected))
            summary["roundtrip"] = roundtrip(runtime, actor, report)
            if args.withheld_principal:
                other = Actor(args.tenant, frozenset(args.withheld_principal), "restricted")
                seen = case_mail_evidence(runtime, other, case_id,
                                          mail_capture_dir=args.state_dir)
                view = runtime.cases.get(other, case_id)
                summary["other_reader"] = {
                    "messages_visible": seen["counts"]["messages_visible"],
                    "withheld_markers": len(seen["withheld"]["markers"]),
                    "findings_withheld": len(view["findings"]["withheld"]),
                    "findings_supported_visible": len(view["findings"]["supported"])}
            output["cases"].append(summary)
    finally:
        cache.__exit__(None, None, None)
        runtime.close()
    # Every Graph request of the run (token requests included): a cost bound
    # for the live check, never a content signal.
    output["graph_requests_sent"] = getattr(transport, "requests_sent", 0) - sent_before
    # Mail is a product source only when it reached a case: the run is
    # no-source-read unless some case read mail for the reader (a PO relation read,
    # or captured mail no case cites, is not case mail evidence).
    labels = {c["evidence_label"] for c in output["cases"]}
    output["evidence_label"] = ("live-read" if "live-read" in labels else
                                "injected-transport" if "injected-transport" in labels else
                                "no-source-read")
    # The Portal PO reads the Portal failed and the mail reads Graph refused outright
    # (capture_mail.refused_parts: every in-scope mailbox answered 403 or 401, the host's
    # grant is missing): the case part of the run did not happen.
    refused = list(ran.get("blocked_upstream") or []) if ran else []
    output["blocked_upstream"] = blocked_upstream(sources) + refused
    survey_read = output.get("survey") or {}
    output["source_read"] = {
        "mail_messages": survey_read.get("messages_scanned", 0),
        "po_relation": survey_read.get("po_scan_status", "not_read"),
        "cases_with_mail": sum(1 for c in output["cases"]
                               if c["counts"]["messages_visible"] > 0),
        **unread_inputs({"mail": survey_read.get("messages_scanned", 0),
                         "po_relation": survey_read.get("po_rows_scanned", 0)},
                        [name for name, stopped in (
                            ("mail", bool(refused)),
                            ("po_relation", any(p["part"] == "po_source"
                                                for p in output["blocked_upstream"])))
                         if stopped])}
    output["totals"] = {
        "cases": len(output["cases"]),
        "messages_visible": sum(c["counts"]["messages_visible"] for c in output["cases"]),
        "attachments_cited": sum(c["counts"]["attachments_cited"] for c in output["cases"]),
        "findings_recorded": sum(c["counts"]["findings_recorded"] for c in output["cases"]),
        "findings_not_recorded": sum(c["counts"]["findings_not_recorded"]
                                     for c in output["cases"]),
        "findings_refused": {code: sum(c["findings_refused"].get(code, 0)
                                       for c in output["cases"])
                             for code in sorted({k for c in output["cases"]
                                                 for k in c["findings_refused"]})},
        "people": sum(c["counts"]["people"] for c in output["cases"]),
        "roundtrip_checked": sum(c["roundtrip"]["checked"] for c in output["cases"]),
        "roundtrip_mismatch": sum(c["roundtrip"]["checked"] - c["roundtrip"]["bytes_match"]
                                  - c["roundtrip"]["body_match"] for c in output["cases"]),
        "outside_scope": sum(c["roundtrip"]["outside_scope"]
                             + c["counts"]["messages_outside_scope"] for c in output["cases"]),
        "other_reader_messages_visible": sum((c.get("other_reader") or {}).get(
            "messages_visible", 0) for c in output["cases"])}
    print(json.dumps(output, indent=1, sort_keys=True, default=str))
    return 0


__all__ = ["COUNTS_SCHEMA", "CaseMailError", "LIMITATIONS", "SCHEMA", "blocked_upstream",
           "case_mail_evidence", "capture_index", "counts_only", "directory_cache",
           "evidence_label", "mail_survey", "main", "person_reviews", "read_relations",
           "roundtrip", "unread_inputs"]


if __name__ == "__main__":
    raise SystemExit(main())
