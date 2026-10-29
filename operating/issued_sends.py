"""Issued PO PDF -> sent message -> supplier reply -> served PO status, for one reader.

The Portal keeps the exact bytes of every PO PDF it issues, and the Workbench must say
*which* render left PLX, *when*, whether the supplier answered and whether the served
record moved on, and say so only from what this reader may see now. Mirrored upstream
rules (plx-customer-portal@4af6957 unless a line names a later commit, each pinned in
the upstream pin file):

* ``portal/src/lib/mrp/purchase-order-document.ts@4af6957:94-96`` names a PO PDF
  ``{poNumber}.pdf`` or ``{poNumber} - Customs.pdf``; ``buildPurchaseOrderPdf`` returns
  that name with the bytes (``:466-470``).
* Two send paths (``portal/src/lib/mrp/api-handlers/purchase-order-send.ts@4af6957``).
  The *hub* path (``:123-163``) enqueues ``purchase-order.sent``, which stores the PDF
  under ``SIGNED_PO`` / ``CUSTOMS``
  (``portal/src/lib/email-hub/types/purchase-order-sent.ts@4af6957:158-171``) and sends
  it from ``purchase-order@`` through Graph, keeping a copy in that mailbox's Sent Items
  (``portal/src/lib/email-hub/graph-transport.ts@4af6957:52``); after a real production
  send the hook sets the gold PO to ``ORDERED`` unless it is already past it
  (``purchase-order-sent.ts:64-92``). The *legacy* path (``:85-121``, while the type is
  OFF) keeps the render first as ``ISSUED_PO`` / ``ISSUED_CUSTOMS`` and then sends it
  through Resend from ``noreply@``
  (``portal/src/lib/mrp/purchase-order-legacy-send.ts@4af6957:14,47-60``): no mailbox
  keeps a copy and it never sets ``ORDERED``. A repeated send with the dialog's send id
  reaches the vendor once (``:41-46``); a send still in flight answers 409
  ``SEND_IN_PROGRESS`` (``:61-71``) like any failed send (``:72-75``), with the render
  already kept. The PDF issue route ``POST .../pdf/issue`` (the page's save or print)
  keeps the same ``ISSUED_*`` render with no send at all; the ``GET .../pdf`` preview
  files nothing (``portal/src/lib/mrp/api-handlers/purchase-order-pdf.ts@595243f:36-98``).
  So an ``ISSUED_*`` render never proves a send, and a send missing from captured mail
  never proves the PO was not sent: it is a ``send_not_captured`` gap with its reasons.
* ``portal/src/lib/email-hub/email-redirect.ts@5cb95e1:50-85``: outside production every
  provider send goes to one test inbox with the subject ``[STAGING → <recipients>] ...``.
  Such a message is a test: counted, never evidence. Since TASK-2553 (b7f8cf2) an
  auth email the caller declares (password reset, invite, set password,
  verification, 2FA) reaches its real recipient as ``[STAGING] ...``: never a PO
  send, and not a redirect.
* Each render is a new SharePoint object ``{epochMs}-{uuid}-{fileName}``
  (``portal/src/lib/storage/issued-records.ts@595243f:185-196``,
  ``portal/src/lib/email-hub/attachments.ts@4af6957:104-108``), so a re-issue never
  replaces an earlier render; the issued-records name replaces ``<>:"/\\|?*#&`` with
  ``_`` (``PO#5280.pdf`` is kept as ``…-PO_5280.pdf``), so names and PO numbers are
  compared under that rule (``safe_key``). A re-issue whose bytes equal the current
  render's files nothing new and answers ``unchanged`` (``issued-records.ts@595243f:25-29,
  264-276``, TASK-2504), the legacy send included, so a newer render means new bytes.
  The document's one ``Attachment`` row keeps its id across re-issues: the id hashes the
  record, category and file name with ``#`` and ``&`` kept (``issued_attachment_id``,
  ``issued-records.ts@595243f:198-221``, b9b5009 TASK-2479), never the object name.
  Categories:
  ``portal/src/lib/storage/issued-record-categories.ts@4af6957:25-32,71-73``.

The chain, for one reader and only over what that reader may see now:

1. a *send*: a captured message in a mailbox's own Sent Items whose From is that
   mailbox (``integrations.capture_mail``, read as the reader); a redirected subject
   makes it ``test_redirect``; no recipient outside the internal domains makes it
   ``internal_only``; otherwise ``vendor``;
2. each vendor-send attachment named like a PO PDF is matched to captured SharePoint
   renders (``integrations.capture_sharepoint``, read as the reader) by its bytes:
   SHA-256 of the captured bytes, else SharePoint's QuickXorHash. A name without equal
   bytes is a lead, never a link;
3. *replies*: captured messages in the send's conversation from a domain the send went
   to, received after it (``reply_captured``; else ``no_reply_captured``, never "no
   reply");
4. the PO number names served ``public.app_purchase_order`` rows through the identifier
   space (``ontology/schema/identifiers.json`` ``petra_po``: every served carrier on the
   relation, compared by the kind's exact rule; the Portal's
   ``resolveGoldPoIdByNumber``, plx-customer-portal@f7e3f8c
   ``portal/src/app/api/mrp/receiving/open-po-lines/route.ts:268-291``, also matches the
   unserved ``app_bc_entity_map.bc_number``, a gap named in ``sources.po.lookup_gaps``,
   and a substring, never exact); one row: ``ordered_or_later``/``before_ordered``.

Every served PO and every rendered PO number without a chain is a gap: ``send_not_linked``
when a vendor send names it but its bytes match no visible render, else
``send_not_captured``; its reasons say what was and was not seen (``GAP_REASONS``).

Limits, stated in every report (``LIMITATIONS``). Nothing is written anywhere but the
capture stores a CLI run fills in its own state directory.

CLI (credentialed live check; counts only, never names, numbers, addresses or
citations)::

    python3 -m operating.issued_sends scan --state-dir D --live-audience L \\
        --mirror-audience M --principal staff:purchasing --internal-domain example.com
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from ontology import identifiers as ids
from ontology import mentions
from ontology.types import Actor
from operating.po_status import PAST_ORDERED


SCHEMA = "plx-issued-sends/v2"
COUNTS_SCHEMA = "plx-issued-sends-counts/v2"
PO_KIND = "petra_po"
PO_RELATION = "public.app_purchase_order"
SENT_FOLDER = "sentitems"
UPSTREAM = {
    "pdf_name": "portal/src/lib/mrp/purchase-order-document.ts@4af6957:94-96,466-470",
    "send_paths": "portal/src/lib/mrp/api-handlers/purchase-order-send.ts@4af6957:85-163",
    "legacy_send": "portal/src/lib/mrp/purchase-order-legacy-send.ts@4af6957:14,41-75",
    "pdf_route": "portal/src/lib/mrp/api-handlers/purchase-order-pdf.ts@595243f:36-98",
    "hub_send": "portal/src/lib/email-hub/types/purchase-order-sent.ts@4af6957:64-92,147-174",
    "sent_items": "portal/src/lib/email-hub/graph-transport.ts@4af6957:52",
    "redirect": "portal/src/lib/email-hub/email-redirect.ts@5cb95e1:50-85",
    "issued_object": "portal/src/lib/storage/issued-records.ts@595243f:25-29,185-221",
    "hub_object": "portal/src/lib/email-hub/attachments.ts@4af6957:104-108",
    "hub_columns": "portal/src/lib/email-hub/attachments.ts@f3e3a53:196-203,256-261; "
                   "portal/src/lib/sharepoint/attachment-metadata.ts@f3e3a53:114-123; "
                   "portal/src/lib/documents/document-types.ts@f3e3a53:31-170",
    "categories": "portal/src/lib/storage/issued-record-categories.ts@4af6957:25-32,71-73",
}
# The DocumentCategory each upstream path files a PO render under, and what that path
# says about a send: ISSUED_* is kept by the PDF issue route and before a legacy send (no
# mailbox copy, may have failed after it); SIGNED_PO / CUSTOMS by the hub at enqueue
# (Sent Items copy once relayed) and by a person's upload. Since TASK-2627 the hub also files
# each approved artwork file it attaches under SIGNED_PO (``po-artwork-files.ts@4243499:113-141``:
# ``<item>_v<n>_<file>``, never a whole PO token), so only a PO-PDF name is ever a render.
# Since TASK-2594 an upload writes the column choice of its category's document-types row,
# and since TASK-3105 (bc144cd) a finalize job writes the hub file's columns the same way
# (``hub_columns``): SIGNED_PO and CUSTOMS reach the column as OTHER. OTHER is also the
# column of every category without its own choice, so an OTHER PO-PDF render is read as
# hub_or_upload: a lead for a missing Sent Items copy, never proof of a hub send.
RENDER_ORIGINS = {
    "ISSUED_PO": "issued_record", "ISSUED_CUSTOMS": "issued_record",
    "SIGNED_PO": "hub_or_upload", "CUSTOMS": "hub_or_upload", "OTHER": "hub_or_upload"}
# purchase-order-sent.ts@4af6957:89-92 PAST_ORDERED (ORDERED included): the hook leaves
# these. The words live with the one PO status definition (operating/po_status.py).
ORDERED_OR_LATER = PAST_ORDERED
OUTCOMES = ("ordered_or_later", "before_ordered", "po_not_served", "po_scan_incomplete",
            "po_number_ambiguous", "po_relation_unread")
# A PO with no linked send: no send seen at all, or a vendor send naming it whose bytes
# match no visible render. Never "not sent".
GAP_KINDS = ("send_not_captured", "send_not_linked")
GAP_REASONS = (
    "mail_not_read",                # no mail capture was read for the reader
    "mail_incomplete",              # a visible mailbox's capture is incomplete or cut
    "renders_not_read",             # no SharePoint capture was read for the reader
    "renders_incomplete",           # the SharePoint index was cut
    "po_relation_unread",           # served PO rows were not read (render-only gaps)
    "no_render_captured",           # no render of this PO is visible
    "issued_record_only",           # every render is ISSUED_*: a save/print or a legacy send
    "hub_render_without_sent_copy",  # a SIGNED_PO/CUSTOMS/OTHER render, no Sent Items copy seen
    "render_category_unread",       # a render whose DocumentCategory was not captured
    "vendor_send_unlinked",         # a vendor send names it; its bytes match no render
    "test_redirect_only",           # a '[STAGING → ...]' test send names it
    "internal_send_only",           # a send inside the internal domains names it
    "status_ordered_or_later",      # the served status says it is past sent
    "po_not_served",                # a rendered PO number no served row carries
    "po_scan_incomplete",           # not among the served rows read, but the scan was cut
    "po_number_ambiguous",          # a rendered PO number several served rows carry
)
LIMITATIONS = (
    "the legacy send path (Resend from noreply@) keeps no copy in any mailbox, and a send "
    "missing from the captured mailboxes is a send_not_captured gap, never 'not sent'",
    "served rows are the staging primary replica; real sends come from production, so a "
    "vendor send's row may lag or be absent (po_not_served)",
    "issued-record folders come from Portal code defaults until app_storage_location is "
    "served; a render outside the captured folders is not seen",
    "a render is kept on every PDF save or print and before every legacy send attempt "
    "whose bytes differ from the current render's, so a newer render is no proof of a "
    "re-sent PO",
    "a byte match proves the sent attachment is that render; it does not prove delivery",
    "a reply is matched by conversation, sender domain and time; a reply outside the "
    "captured mailboxes (an admin reply-to elsewhere) is no_reply_captured",
    "a PO number the Portal finds only through a rule no served field answers "
    "(sources.po.lookup_gaps: a BC No. held only in app_bc_entity_map) is po_not_served",
)
MAX_PO_ROWS = 50_000
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# email-redirect.ts@5cb95e1:83; a reply or forward keeps the marker after its prefixes.
_REDIRECT = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)*\[STAGING\s*(?:→|->)", re.IGNORECASE)
# purchase-order-document.ts:94-96.
_PDF_NAME = re.compile(r"^(?P<stem>.+?)(?P<customs> - Customs)?\.pdf$", re.IGNORECASE)
# issued-records.ts:184-185, email-hub/attachments.ts@f3e3a53:116-120 (randomUUID ids).
_OBJECT_NAME = re.compile(r"^(?P<ms>\d{13})-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                          r"[0-9a-f]{4}-[0-9a-f]{12}-(?P<name>.+)$", re.IGNORECASE)
# issued-records.ts:186-188 safeName (a superset of the hub's attachments.ts@f3e3a53:119 rule).
_UNSAFE = re.compile(r'[<>:"/\\|?*#&]')
# issued-records.ts:204-206 identityName: safeName before ``#`` and ``&`` joined it.
_IDENTITY_UNSAFE = re.compile(r'[<>:"/\\|?*]')


def redirected_test_send(subject: Any) -> bool:
    """A non-production redirect (email-redirect.ts): a test message, never evidence."""
    return isinstance(subject, str) and bool(_REDIRECT.match(subject))


def safe_key(value: Any) -> str | None:
    """A file name or PO number as the Portal stores it in an object name, folded."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _UNSAFE.sub("_", value).strip().lower()


def issued_attachment_id(entity_type: str, entity_id: str, category: str,
                         file_name: str) -> str:
    """The Portal ``Attachment`` row id of one issued document (issuedAttachmentId,
    issued-records.ts@595243f:208-221): the same record, category and file name always
    give the same id, whatever the object-name rule strips, so every render of
    ``PO#5280.pdf`` moves one row although each object is stored as ``…-PO_5280.pdf``."""
    name = _IDENTITY_UNSAFE.sub("_", file_name).strip()
    key = "\0".join((entity_type, entity_id, category, name)).encode()
    return "issued_" + hashlib.sha256(key).hexdigest()[:32]


def _whole_po(stem: str) -> bool:
    tokens = mentions.extract(stem, kinds=("petra_po",), quoted=False)
    return len(tokens) == 1 and (tokens[0]["start"], tokens[0]["end"]) == (0, len(stem))


def po_pdf_name(name: Any) -> dict[str, str] | None:
    """``{po_number, variant, file_name}`` of a Portal PO PDF name (the display name, or a
    stored object name ``{epochMs}-{uuid}-{name}``); ``None`` for anything else. The stem
    must be one whole Petra PO token under the identifier grammar, as written or with the
    stored name's ``_`` read as the ``#`` it replaced (issued-records.ts safeName)."""
    if not isinstance(name, str):
        return None
    found = _OBJECT_NAME.match(name.strip())
    display = found["name"] if found else name.strip()
    parsed = _PDF_NAME.match(display)
    if parsed is None:
        return None
    stem = parsed["stem"].strip()
    if not (_whole_po(stem) or ("_" in stem and _whole_po(stem.replace("_", "#")))):
        return None
    return {"po_number": stem, "variant": "customs" if parsed["customs"] else "standard",
            "file_name": display}


def _domain(address: Any) -> str | None:
    if not isinstance(address, str) or address.count("@") != 1:
        return None
    return address.rsplit("@", 1)[1].strip().lower() or None


def _under(domain: str, internal: frozenset[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in internal)


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _issued_at(row: Mapping[str, Any]) -> datetime | None:
    """The Portal's issue time from the object name, else the file's creation time."""
    found = _OBJECT_NAME.match(str(row.get("name") or "").strip())
    if found:
        return datetime.fromtimestamp(int(found["ms"]) / 1000, tz=timezone.utc)
    return _moment(row.get("created_at"))


def _category(row: Mapping[str, Any]) -> str | None:
    """DocumentCategory captured for this very version, else ``None``
    (``doc_metadata_taxonomy.confirmed_category``)."""
    from .doc_metadata_taxonomy import confirmed_category

    return confirmed_category(row)


# --------------------------------------------------------------------------- reads


def read_sends(store: Any, actor: Actor, audience: Any, internal_domains: Iterable[str], *,
               limit: int = 5000) -> dict[str, Any]:
    """Messages this reader may see in the mail capture: sends (with their PO PDF
    attachments) and the other messages a reply may be (conversation, sender domain)."""
    index = store.index(actor, audience, limit=limit)
    internal = frozenset(d.strip().lower() for d in internal_domains
                         if isinstance(d, str) and d.strip())
    sent_folder: dict[str, str | None] = {}
    sends: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    scanned = 0
    for item in index["items"]:
        found = store.resolve(item["l1_handle"], actor, audience)
        if found is None:
            continue
        scanned += 1
        row = found["row"]
        mailbox = item["mailbox"]
        if mailbox not in sent_folder:
            state = store.folder_state(mailbox, SENT_FOLDER)
            sent_folder[mailbox] = state.get("folder_id") if state else None
        sender = row.get("from")
        own = (isinstance(sender, str) and isinstance(row.get("mailbox_upn"), str)
               and sender.strip().lower() == row["mailbox_upn"].strip().lower())
        redirected = redirected_test_send(row.get("subject"))
        if not own or sent_folder[mailbox] is None \
                or row.get("parent_folder_id") != sent_folder[mailbox]:
            others.append({"citation": item["l1_handle"],
                           "conversation_id": item.get("conversation_id"),
                           "from_domain": _domain(sender), "test_redirect": redirected,
                           "received_at": row.get("received_at")})
            continue
        recipients = {d for d in (_domain(a) for a in [*(row.get("to") or []),
                                                       *(row.get("cc") or [])]) if d}
        external = sorted(d for d in recipients if not _under(d, internal))
        kind = "test_redirect" if redirected else "vendor" if external else "internal_only"
        attachments = []
        for att in store.attachments(item["l1_handle"], actor, audience):
            parsed = po_pdf_name(att.get("name"))
            if parsed is None:
                continue
            entry: dict[str, Any] = {"citation": att.get("l1_handle"), **parsed}
            if kind == "vendor":   # hashes only where they can link a render
                # From the bytes, or the record a dropped original left (capture_mail).
                hashes = (store.attachment_hashes(att["l1_handle"], actor, audience)
                          if att.get("l1_handle") and att.get("content_sha256") else None)
                entry["sha256"] = hashes["sha256"] if hashes is not None else None
                entry["quick_xor"] = hashes["quick_xor"] if hashes is not None else None
            attachments.append(entry)
        sends.append({"citation": item["l1_handle"], "kind": kind,
                      "conversation_id": item.get("conversation_id"),
                      "external_domains": external if kind == "vendor" else [],
                      "sent_at": row.get("sent_at"), "attachments": attachments})
    return {"status": "read", "complete": bool(index.get("complete")),
            "truncated": bool(index.get("truncated")), "messages_scanned": scanned,
            "sends": sends, "others": others}


def read_renders(store: Any, actor: Actor, audience: Any, *, limit: int = 100_000
                 ) -> dict[str, Any]:
    """Captured SharePoint files this reader may see whose name is a Portal PO PDF."""
    index = store.index(actor, audience, limit=limit)
    renders = []
    for item in index["items"]:
        found = store.resolve(item["l1_handle"], actor, audience, text=False)
        if found is None:
            continue
        row = found["row"]
        parsed = po_pdf_name(row.get("name"))
        if parsed is None:
            continue
        hashes = row.get("source_hashes") or {}
        category = _category(row)
        renders.append({"citation": found["l1_handle"], **parsed,
                        "issued_at": _issued_at(row), "category": category,
                        "origin": RENDER_ORIGINS.get(category or "",
                                                     "unread" if category is None else "other"),
                        "sha256": row.get("content_sha256"),
                        "quick_xor": hashes.get("quickXorHash") if isinstance(hashes, Mapping)
                        else None})
    # Complete only when nothing was cut and every captured root is complete for this
    # reader (walked, bytes read, no current file withheld from it).
    complete = (not index.get("truncated")
                and bool(store.completeness(actor, audience).get("complete")))
    return {"status": "read", "complete": complete, "renders": renders}


# --------------------------------------------------------------------------- chain


def _replies(send: Mapping[str, Any],
             by_conversation: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[str]:
    """Messages in the send's conversation from a domain it went to, received after it."""
    conversation, sent = send.get("conversation_id"), _moment(send.get("sent_at"))
    if not conversation or sent is None:
        return []
    out = []
    for other in by_conversation.get(conversation, ()):
        received = _moment(other.get("received_at"))
        if (not other.get("test_redirect")
                and other.get("from_domain") in send["external_domains"]
                and received is not None and received >= sent):
            out.append(other["citation"])
    return out


def _po_carriers() -> tuple[tuple[str, str], ...]:
    """``(kind, field)`` of every served field the identifier space declares on
    ``PO_RELATION`` for a Petra PO number (``ontology/schema/identifiers.json``
    ``petra_po.carriers``: ``po_number``, ``custom_fields.fmPoNumber``,
    ``custom_fields.renumberedTo``). The Portal's own lookup and the rules it applies
    beyond these are ``petra_po.portal_lookup``."""
    return tuple((PO_KIND, carrier["field"]) for carrier in ids.carriers(PO_KIND)
                 if carrier.get("relation") == PO_RELATION and carrier.get("served") is True)


def _po_lookup_gaps() -> list[str]:
    """The Portal lookup rules no served carrier answers (``portal_lookup`` ``gap``)."""
    rules = ids.kind_spec(PO_KIND).get("portal_lookup", {}).get("rules", ())
    return [rule["rule"] for rule in rules if rule.get("workbench") == "gap"]


def _field_text(row: Mapping[str, Any], field: str) -> str | None:
    """Text at a column or at one key inside a JSON-object column (read as
    ``ontology.resolve`` reads a carrier cell: no float, bool or container)."""
    column, _, key = field.partition(".")
    value = row.get(column)
    if key:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        value = value.get(key) if isinstance(value, Mapping) else None
    if value is None or isinstance(value, (bool, dict, list, float)):
        return None
    text = str(value)
    return text if text.strip() else None


class _PoIndex:
    """Served PO rows by every number a carrier of the identifier space holds."""

    def __init__(self, items: Sequence[Mapping[str, Any]]) -> None:
        self.items = [dict(item) for item in items]
        self._by_text: dict[str, list[tuple[int, str, str, str]]] = {}
        carriers = _po_carriers()
        for index, item in enumerate(self.items):
            row = item.get("row") or {}
            for kind, field in carriers:
                stored = _field_text(row, field)
                if stored is not None:
                    self._by_text.setdefault(stored.strip().casefold(), []).append(
                        (index, kind, field, stored))

    def exact(self, number: str) -> dict[int, set[str]]:
        """Row index -> carrier fields whose value is ``number`` under the kind's exact
        rule (``ontology.identifiers.exact_compare``; the folded text only finds them)."""
        found: dict[int, set[str]] = {}
        for index, kind, field, stored in self._by_text.get(number.strip().casefold(), ()):
            if ids.exact_compare(kind, stored, number):
                found.setdefault(index, set()).add(field)
        return found


def _po_rows(index: Any, number: Any) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """``(subject, rows, rules)`` of a PO number: the served rows ``_PoIndex.exact`` finds
    through the identifier space (so a PDF named before a renumber still finds its row),
    a stored name's ``_`` also read as the ``#`` it replaced. ``subject`` is the row
    indexes, else the folded number: one key per PO, however many numbers name it."""
    if not isinstance(number, str) or not number.strip():
        return "", [], []
    found: dict[int, set[str]] = {}
    for token in {number, number.replace("_", "#")}:
        for i, fields in index.exact(token).items():
            found.setdefault(i, set()).update(fields)
    if not found:
        return safe_key(number) or "", [], []
    return (tuple(sorted(found)), [index.items[i] for i in sorted(found)],
            sorted(set().union(*found.values())))


def _po_outcome(po_scan: Mapping[str, Any], matched: list[dict[str, Any]]) -> str:
    if po_scan.get("status") != "read":
        return "po_relation_unread"
    if not matched:
        return "po_not_served" if po_scan.get("complete") else "po_scan_incomplete"
    if len(matched) > 1:
        return "po_number_ambiguous"
    status = (matched[0].get("row") or {}).get("status")
    ordered = isinstance(status, str) and status.strip().upper() in ORDERED_OR_LATER
    return "ordered_or_later" if ordered else "before_ordered"


def link_issued_sends(sends: Mapping[str, Any], renders: Mapping[str, Any],
                      po_scan: Mapping[str, Any]) -> dict[str, Any]:
    """The chain report from what one reader read (see the module docstring)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for render in renders.get("renders") or []:
        groups.setdefault(safe_key(render["file_name"]) or "", []).append(render)
    for members in groups.values():
        members.sort(key=lambda r: (r["issued_at"] or _EPOCH, r["citation"]))
    # Equal bytes issued twice name the newest of them (oldest first, so later wins).
    ordered = sorted((r for m in groups.values() for r in m),
                     key=lambda r: (r["issued_at"] or _EPOCH, r["citation"]))
    by_sha = {r["sha256"]: r for r in ordered if r["sha256"]}
    by_xor = {r["quick_xor"]: r for r in ordered if r["quick_xor"]}
    index = _PoIndex(po_scan.get("rows") or [])
    by_conversation: dict[str, list[Mapping[str, Any]]] = {}
    for other in sends.get("others") or []:
        if other.get("conversation_id"):
            by_conversation.setdefault(other["conversation_id"], []).append(other)
    counts: dict[str, Any] = {
        "messages_scanned": sends.get("messages_scanned", 0), "sends": 0,
        "sends_by_kind": {"vendor": 0, "test_redirect": 0, "internal_only": 0},
        "issued_attachments": 0, "attachment_bytes_unavailable": 0,
        "renders": len(renders.get("renders") or []), "render_names": len(groups),
        "renders_superseded": sum(len(m) - 1 for m in groups.values()),
        "renders_by_origin": {k: 0 for k in ("issued_record", "hub_or_upload", "other",
                                             "unread")},
        "byte_matches": {"sha256": 0, "quickXorHash": 0}, "name_only": 0,
        "unmatched": 0, "chains": 0, "chain_outcomes": {k: 0 for k in OUTCOMES},
        "sent_render": {"newest": 0, "superseded": 0},
        "replies": 0, "chain_replies": {"reply_captured": 0, "no_reply_captured": 0},
        "po_gaps": {k: 0 for k in GAP_KINDS}, "gap_reasons": {k: 0 for k in GAP_REASONS}}
    for render in renders.get("renders") or []:
        counts["renders_by_origin"][render["origin"]] += 1
    chains, leads = [], []
    sent_keys: set[Any] = set()      # PO subjects a byte-matched vendor send carried
    named_by: dict[Any, set[str]] = {}   # PO subject -> kinds of sends naming it
    for send in sends.get("sends") or []:
        counts["sends"] += 1
        counts["sends_by_kind"][send["kind"]] += 1
        for att in send["attachments"]:
            named_by.setdefault(_po_rows(index, att["po_number"])[0], set()).add(send["kind"])
        if send["kind"] != "vendor":
            continue
        replies = _replies(send, by_conversation)
        for att in send["attachments"]:
            counts["issued_attachments"] += 1
            if att["sha256"] is None:
                counts["attachment_bytes_unavailable"] += 1
            render, basis = by_sha.get(att["sha256"]) if att["sha256"] else None, "sha256"
            if render is None and att["quick_xor"]:
                render, basis = by_xor.get(att["quick_xor"]), "quickXorHash"
            name = safe_key(att["file_name"]) or ""
            if render is None:
                counts["name_only" if name in groups else "unmatched"] += 1
                if name in groups:
                    leads.append({"kind": "name_only", "send": send["citation"],
                                  "attachment": att["citation"],
                                  "renders": [r["citation"] for r in groups[name]]})
                continue
            counts["byte_matches"][basis] += 1
            group = groups[safe_key(render["file_name"]) or ""]
            sent_render = "newest" if render is group[-1] else "superseded"
            counts["sent_render"][sent_render] += 1
            # The attachment keeps the Portal's display name; the stored render may not.
            key, matched, rules = _po_rows(index, att["po_number"])
            sent_keys.add(key)
            outcome = _po_outcome(po_scan, matched)
            chain: dict[str, Any] = {
                "send": {"citation": send["citation"], "sent_at": send["sent_at"]},
                "attachment": att["citation"], "match": basis,
                "render": {"citation": render["citation"], "variant": render["variant"],
                           "category": render["category"], "origin": render["origin"],
                           "issued_at": render["issued_at"].isoformat()
                           if render["issued_at"] else None},
                "sent_render": sent_render,
                "newer_renders": [r["citation"] for r in group[group.index(render) + 1:]],
                "po_number": att["po_number"], "outcome": outcome,
                "replies": replies,
                "reply": "reply_captured" if replies else "no_reply_captured"}
            if outcome == "po_number_ambiguous":
                chain["po_candidates"] = [m.get("citation") for m in matched]
            elif matched:
                chain["po"] = {"citation": matched[0].get("citation"),
                               "status": (matched[0].get("row") or {}).get("status"),
                               "matched_by": rules}
            counts["chain_outcomes"][outcome] += 1
            counts["chain_replies"][chain["reply"]] += 1
            counts["replies"] += len(replies)
            chains.append(chain)
    counts["chains"] = len(chains)
    gaps = _gaps(sends, renders, po_scan, index, sent_keys, named_by)
    for gap in gaps:
        counts["po_gaps"][gap["kind"]] += 1
        for reason in gap["reasons"]:
            counts["gap_reasons"][reason] += 1
    return {"schema": SCHEMA, "evidence_role": "lead", "upstream": dict(UPSTREAM),
            "limitations": list(LIMITATIONS),
            "sources": {"mail": {k: sends.get(k) for k in ("status", "complete", "truncated")},
                        "sharepoint": {k: renders.get(k) for k in ("status", "complete")},
                        "po": {"status": po_scan.get("status"),
                               "rows_scanned": len(po_scan.get("rows") or []),
                               "complete": po_scan.get("complete", False),
                               "reasons": list(po_scan.get("reasons") or []),
                               "lookup_gaps": _po_lookup_gaps()}},
            "counts": counts, "chains": chains, "leads": leads, "gaps": gaps}


def _gaps(sends: Mapping[str, Any], renders: Mapping[str, Any], po_scan: Mapping[str, Any],
          index: Any, sent_keys: set[Any], named_by: Mapping[Any, set[str]]
          ) -> list[dict[str, Any]]:
    """One gap per served PO and per rendered PO number that no byte-matched vendor send
    carried: what was and was not seen, never a claim that it was not sent."""
    source_reasons = []
    if sends.get("status") != "read":
        source_reasons.append("mail_not_read")
    elif not sends.get("complete") or sends.get("truncated"):
        source_reasons.append("mail_incomplete")
    if renders.get("status") != "read":
        source_reasons.append("renders_not_read")
    elif not renders.get("complete"):
        source_reasons.append("renders_incomplete")
    rows: dict[Any, list[dict[str, Any]]] = {}
    for item in index.items:   # each served row under its own number's subject
        key, matched, _ = _po_rows(index, (item.get("row") or {}).get("po_number"))
        if key:
            rows[key] = matched
    by_po: dict[Any, list[dict[str, Any]]] = {}
    for render in renders.get("renders") or []:
        key, matched, _ = _po_rows(index, render["po_number"])
        rows.setdefault(key, matched)
        by_po.setdefault(key, []).append(render)
    keys = sorted((set(rows) | set(by_po)) - sent_keys - {""},
                  key=lambda k: (isinstance(k, str), k))
    gaps = []
    for key in keys:
        matched, rendered = rows.get(key, []), by_po.get(key, [])
        reasons = list(source_reasons)
        if not rendered:
            reasons.append("no_render_captured")
        else:
            origins = {r["origin"] for r in rendered}
            if origins == {"issued_record"}:
                reasons.append("issued_record_only")
            if "hub_or_upload" in origins:
                reasons.append("hub_render_without_sent_copy")
            if "unread" in origins:
                reasons.append("render_category_unread")
        kinds = named_by.get(key, set())
        if "vendor" in kinds:
            reasons.append("vendor_send_unlinked")
        if "test_redirect" in kinds:
            reasons.append("test_redirect_only")
        if "internal_only" in kinds:
            reasons.append("internal_send_only")
        gap: dict[str, Any] = {
            "kind": "send_not_linked" if "vendor" in kinds else "send_not_captured",
            "renders": [r["citation"] for r in rendered]}
        if po_scan.get("status") != "read":
            reasons.append("po_relation_unread")
        elif not matched:
            reasons.append("po_not_served" if po_scan.get("complete") else "po_scan_incomplete")
        elif len(matched) > 1:
            reasons.append("po_number_ambiguous")
            gap["po_candidates"] = [m.get("citation") for m in matched]
        else:
            status = (matched[0].get("row") or {}).get("status")
            if isinstance(status, str) and status.strip().upper() in ORDERED_OR_LATER:
                reasons.append("status_ordered_or_later")
            gap["po"] = {"citation": matched[0].get("citation"), "status": status}
        number = ((matched[0].get("row") or {}).get("po_number") if len(matched) == 1
                  else rendered[0]["po_number"] if rendered else None)
        gaps.append({"po_number": number, **gap,
                     "reasons": [r for r in GAP_REASONS if r in reasons]})
    return gaps


def issued_sends(actor: Actor, *, mail: Any = None, sharepoint: Any = None, audience: Any,
                 mirror: Any = None, internal_domains: Iterable[str] = ()) -> dict[str, Any]:
    """Read every source as ``actor`` (each store gates per reader) and link them.
    A source not given is ``not_captured`` (or ``not_configured`` for the mirror)."""
    sends = (read_sends(mail, actor, audience, internal_domains) if mail is not None else
             {"status": "not_captured", "complete": False, "truncated": False,
              "messages_scanned": 0, "sends": [], "others": []})
    renders = (read_renders(sharepoint, actor, audience) if sharepoint is not None else
               {"status": "not_captured", "complete": False, "renders": []})
    if mirror is not None:
        from integrations.source_mirror import scan_relation
        po_scan = scan_relation(mirror, actor, PO_RELATION, MAX_PO_ROWS, None)
    else:
        po_scan = {"status": "not_configured", "rows": [], "complete": False,
                   "reasons": ["not_configured"]}
    report = link_issued_sends(sends, renders, po_scan)
    if "upstream" in po_scan:
        report["sources"]["po"]["upstream"] = dict(po_scan["upstream"])
    return report


def blocked_upstream(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The Portal relation read the Portal failed: the live verifier's ``blocked_upstream``."""
    po = report["sources"]["po"]
    return ([{"part": "po_source", "source": "portal-workbench-api", **po["upstream"]}]
            if "upstream" in po else [])


def counts_only(report: Mapping[str, Any], *, evidence_label: str) -> dict[str, Any]:
    """The report without any value: numbers, booleans and enums only."""
    return {"schema": COUNTS_SCHEMA, "evidence_label": evidence_label,
            "counts": report["counts"], "sources": report["sources"],
            "blocked_upstream": blocked_upstream(report)}


def evidence_label(report: Mapping[str, Any], injected: bool) -> str:
    """``no-source-read`` unless mail or SharePoint was read for the reader; else
    ``injected-transport`` or ``live-read``."""
    counts = report["counts"]
    if counts["messages_scanned"] <= 0 and counts["renders"] <= 0:
        return "no-source-read"
    return "injected-transport" if injected else "live-read"


# --------------------------------------------------------------------------- CLI


def _capture_sharepoint(state_dir: Path, scope_path: Path, audience: Any, principal: str,
                        env: Mapping[str, str], sender: Any) -> dict[str, Any]:
    from integrations import capture_sharepoint as cs
    from integrations.graph_read import SharePointReader, build_graph, resolve_site_drive
    from integrations.live_source_guard import sharepoint_host_from_env

    scope = cs.CaptureScope.from_file(scope_path)
    api, transport = build_graph(env, sender=sender)
    host = sharepoint_host_from_env(env)
    readers: dict[str, Any] = {}

    def reader_for(drive_id: str) -> Any:
        if drive_id not in readers:
            readers[drive_id] = SharePointReader(api, audience, drive_id, transport=transport,
                                                 read_permissions=True,
                                                 max_acl_reads=cs.MAX_ACL_READS)
        return readers[drive_id]

    def site_drive(name: str) -> str:
        if host is None:
            raise ValueError("site_host_unconfigured")
        return resolve_site_drive(api, host, name)

    store = cs.SharePointCaptureStore(state_dir)
    try:
        run = cs.SharePointCapture(store, reader_for,
                                   Actor(audience.tenant, frozenset({principal}), "restricted"),
                                   site_drive=site_drive,
                                   max_content_bytes=scope.max_content_bytes).run(scope)
    finally:
        store.close()
    return {"complete": run["complete"], "complete_bound": run["complete_bound"],
            "bound_total": run["bound_total"],
            "reasons": ",".join(sorted(run["incomplete_reasons"]))}


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         graph_sender: Any = None, workbench_fetch: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m operating.issued_sends",
        description="Counts-only issued PDF -> sent message -> served status chain (read-only).")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--state-dir", required=True, type=Path)
    scan.add_argument("--live-audience", required=True, type=Path)
    scan.add_argument("--mirror-audience", required=True, type=Path)
    scan.add_argument("--principal", action="append", required=True)
    scan.add_argument("--tenant", default="plx")
    scan.add_argument("--internal-domain", action="append", default=[])
    scan.add_argument("--mail-capture-scope", type=Path, default=None,
                      help="capture this plx-mail-capture-scope/v1 into --state-dir first")
    scan.add_argument("--mail-capture-principal", default="svc:mail-capture")
    scan.add_argument("--capture-page-budget", type=int, default=None)
    scan.add_argument("--sharepoint-capture-scope", type=Path, default=None,
                      help="capture this plx-sharepoint-capture-scope/v1 into --state-dir first")
    scan.add_argument("--sharepoint-capture-principal", default="svc:sharepoint-capture")
    scan.add_argument("--base-url-env", default="PLX_WORKBENCH_API_URL")
    scan.add_argument("--key-env", default="PLX_WORKBENCH_API_KEY")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    from integrations import capture_mail as cm
    from integrations import capture_sharepoint as cs
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from .case_runtime import CaseRuntime, build_live_readers
    from .ingestion import IngestError, load_live_audience

    try:
        audience = load_live_audience(args.live_audience, env)
    except IngestError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 2
    captures: dict[str, Any] = {}
    refused: list[dict[str, Any]] = []
    try:
        if args.mail_capture_scope is not None:
            live = build_live_readers(audience, env, graph_sender=graph_sender)
            if live.mail is None:
                raise cm.CaptureError("mail_reader_not_configured")
            run = cm.capture_into(
                args.state_dir, cm.CaptureScope.from_file(args.mail_capture_scope), live.mail,
                Actor(args.tenant, frozenset({args.mail_capture_principal}), "restricted"),
                page_budget=args.capture_page_budget)
            captures["mail"] = {"complete": run["complete"],
                                "reasons": ",".join(run["reasons"])}
            refused = list(run["blocked_upstream"])
        if args.sharepoint_capture_scope is not None:
            captures["sharepoint"] = _capture_sharepoint(
                args.state_dir, args.sharepoint_capture_scope, audience,
                args.sharepoint_capture_principal, env, graph_sender)
    except (cm.CaptureError, cs.CaptureError, ValueError) as exc:
        print(json.dumps({"error": getattr(exc, "code", None) or "capture_failed"}),
              file=sys.stderr)
        return 2
    base_url, key = env.get(args.base_url_env), env.get(args.key_env)
    client = None
    if base_url and key:
        client = (WorkbenchReadApiClient(base_url, key, fetch=workbench_fetch)
                  if workbench_fetch is not None else WorkbenchReadApiClient(base_url, key))
    from integrations.people_exclusion import PeopleExclusion
    runtime = CaseRuntime(args.state_dir, client=client, audience=args.mirror_audience,
                          tenant=args.tenant, people_exclusion=PeopleExclusion.from_env(env))
    mail = cm.MailCaptureStore(args.state_dir) if cm.store_exists(args.state_dir) else None
    sharepoint = (cs.SharePointCaptureStore(args.state_dir)
                  if cs.store_exists(args.state_dir) else None)
    actor = Actor(args.tenant, frozenset(args.principal), "restricted")
    try:
        report = issued_sends(actor, mail=mail, sharepoint=sharepoint, audience=audience,
                              mirror=runtime.mirror if client is not None else None,
                              internal_domains=args.internal_domain)
    finally:
        for store in (mail, sharepoint):
            if store is not None:
                store.close()
        runtime.close()
    injected = graph_sender is not None or workbench_fetch is not None
    out = counts_only(report, evidence_label=evidence_label(report, injected))
    # Mail Graph refused outright (capture_mail.refused_parts: every in-scope mailbox
    # answered 403 or 401, the host's grant is missing) is blocked upstream, beside the Portal.
    out["blocked_upstream"] += refused
    from .case_mail import unread_inputs
    out["source_read"] = unread_inputs(
        {"mail": report["counts"]["messages_scanned"],
         "sharepoint": report["counts"]["renders"],
         "po": report["sources"]["po"]["rows_scanned"]},
        [name for name, stopped in (("mail", bool(refused)),
                                    ("po", "upstream" in report["sources"]["po"]))
         if stopped],
        required=["mail"])
    if captures:
        out["capture_runs"] = captures
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


__all__ = ["COUNTS_SCHEMA", "GAP_KINDS", "GAP_REASONS", "LIMITATIONS", "ORDERED_OR_LATER",
           "OUTCOMES", "RENDER_ORIGINS", "SCHEMA", "UPSTREAM", "blocked_upstream",
           "counts_only", "evidence_label", "issued_attachment_id", "issued_sends",
           "link_issued_sends", "main", "po_pdf_name", "read_renders", "read_sends",
           "redirected_test_send", "safe_key"]


if __name__ == "__main__":
    raise SystemExit(main())
