"""Record document pack: for one record (a sales or purchase order, an invoice, a shipment,
a lot, a customer PO), the documents a customer or supplier usually asks for (invoice,
packing slip, bill of lading, proof of delivery, certificate of analysis, order
confirmation, tracking, spec or safety sheet, statement), found in the captured mail
attachments, SharePoint and originals around the record and its own transaction records
(``transaction_paths``: the schema's links between its type and an invoice, shipment,
receipt, lot or confirmation type; never a party or another order; a lot's or batch's
other documents only when they name this record), with each
one's source, and a reply drafted to the record's party that lists them. The draft is a card
for ``operating.card_actions.ActionDesk`` (``alert_playbooks.card``): it is sent only through
its preview, an explicit confirm by the signed-in person, the people-exclusion guard and
the live-send gate. This module never sends and never writes (``sent``: False, ``writes``: 0).

Permissions (fail closed): every read is the reader's own tool call (``call``:
``plx.object``, ``plx.around``, ``plx.documents`` as the reader), so every audience, marking
and item rule is the primitives'; on top, each document is decided on its own citation
(``decide``: handles -> a reason when the reader may not open it, as the briefs decide), so
a document the reader may not open is neither listed, counted nor named in the draft, and a
decision that fails withholds every document. A family no readable document matches is
listed as ``missing`` (the draft says it will follow), never guessed.

Classification is by the document's own title words (``FAMILIES``), never by a record or a
party: no rule names a case. Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from . import alert_playbooks as ap

SCHEMA = "plx-record-pack/v1"
MAX_DOCS = 50          # documents one source read lists (plx.documents' page)
MAX_LINKED = 12        # linked records whose documents are read too
MAX_PER_FAMILY = 3
MAX_TITLE = 120
SOURCES = ["graph_mail", "sharepoint", "original"]

# What a document's title says it is (first match wins; ``\b`` word bounds, any case).
FAMILIES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("coa", "Certificate of analysis", re.compile(
        r"certificate\s+of\s+analysis|\bc\.?\s?o\.?\s?a\b|\bc\s+of\s+a\b|\bcofa\b", re.I)),
    ("bol", "Bill of lading", re.compile(r"bill\s+of\s+lading|\bb\.?o\.?l\b|\bb/l\b", re.I)),
    ("pod", "Proof of delivery", re.compile(r"proof\s+of\s+delivery|\bpod\b|delivery\s+"
                                            r"receipt|signed\s+delivery", re.I)),
    ("packing_slip", "Packing slip", re.compile(r"packing\s+(?:slip|list)|pack\s+list|"
                                                r"\bpacklist\b|\bpacking\b", re.I)),
    ("invoice", "Invoice", re.compile(r"\binvoice|\binv\b|\binv[-_ #]?\d", re.I)),
    ("order_confirmation", "Order confirmation", re.compile(
        r"(?:order|po|p\.o\.)\s+(?:confirmation|acknowledg)|\bconfirmation\b|"
        r"\backnowledg(?:e)?ment\b", re.I)),
    ("tracking", "Tracking", re.compile(r"\btracking\b|\bwaybill\b|\bpro\s*#", re.I)),
    ("certificate", "Certificate", re.compile(r"\bcertificate\b|\bcert\b|\bkosher\b|"
                                              r"\bhalal\b|\borganic\b|\bgfsi\b|\bsqf\b|"
                                              r"\bbrc\b|\bcoi\b", re.I)),
    ("spec_sds", "Spec or safety sheet", re.compile(r"\bsds\b|\bmsds\b|safety\s+data|"
                                                    r"spec(?:ification)?\s+sheet|\btds\b|"
                                                    r"\bspecification\b", re.I)),
    ("statement", "Statement", re.compile(r"\bstatement\b", re.I)),
)
LABELS = {key: label for key, label, _ in FAMILIES}
# What a party usually asks for, per record type (the order the draft lists them in).
EXPECTED: dict[str, tuple[str, ...]] = {
    "SalesOrder": ("order_confirmation", "invoice", "packing_slip", "bol", "coa", "tracking"),
    "CustomerPurchaseOrder": ("order_confirmation", "invoice", "packing_slip", "bol", "coa"),
    "SalesInvoice": ("invoice", "packing_slip", "bol", "pod"),
    "Shipment": ("bol", "packing_slip", "pod", "tracking", "coa"),
    "PurchaseOrder": ("order_confirmation", "invoice", "packing_slip", "bol", "coa"),
    "SupplierInvoice": ("invoice", "packing_slip", "bol"),
    "Receipt": ("packing_slip", "bol", "coa"),
    "Lot": ("coa", "spec_sds", "certificate"),
    "Batch": ("coa", "spec_sds"),
}
PACK_TYPES = frozenset(EXPECTED)
# Which side of the business a pack is for: a customer's records never reach a supplier's
# (a supplier invoice or confirmation states our costs) and the other way round; a lot's
# pack is its certificates and sheets only.
SIDE = {"SalesOrder": "customer", "SalesInvoice": "customer", "Shipment": "customer",
        "CustomerPurchaseOrder": "customer", "PurchaseOrder": "supplier",
        "SupplierInvoice": "supplier", "Receipt": "supplier", "Lot": "lot", "Batch": "lot"}
# The transaction record types a pack of each side reads documents around (never a party,
# never an order, never the other side's records); the record's own twin always.
SIDE_TYPES = {"customer": frozenset({"SalesInvoice", "Shipment", "CustomerPurchaseOrder",
                                     "Lot", "Batch"}),
              "supplier": frozenset({"SupplierInvoice", "SupplierConfirmation", "Receipt",
                                     "Lot", "Batch"}),
              "lot": frozenset()}
TRANSACTION_TYPES = frozenset().union(*SIDE_TYPES.values())
# The linked record types whose own record is itself a document of a side's pack.
RECORD_FAMILY = {"customer": {"SalesInvoice": "invoice", "Shipment": "packing_slip"},
                 "supplier": {"SupplierInvoice": "invoice",
                              "SupplierConfirmation": "order_confirmation"},
                 "lot": {}}
# The customer link of a customer-side record (a sheet made for another customer is not
# this one's).
CUSTOMER_LINK = {"SalesOrder": "SalesOrder.ordered_by", "SalesInvoice": "SalesInvoice.billed_to",
                 "Shipment": "Shipment.shipped_to"}
SHARED_TYPES = frozenset({"Lot", "Batch"})
LOT_FAMILIES = frozenset({"coa", "spec_sds", "certificate"})
CONFIRMED = ("verified", "reviewed", "corroborated")

Call = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
Decide = Callable[[Sequence[str]], Mapping[str, Any]]


class PackRefused(Exception):
    """The record is not one a pack is made for, or the reader cannot open it."""


def family_of(title: Any) -> str | None:
    text = title if isinstance(title, str) else ""
    for key, _, pattern in FAMILIES:
        if pattern.search(text):
            return key
    return None


def _type_of(oid: str) -> str:
    return oid.split(":", 1)[0] if ":" in oid else ""


def _text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    if not isinstance(value, str):
        return None
    flat = " ".join(value.split())
    return flat[:MAX_TITLE] if flat else None


def _page(oid: str) -> str:
    return f"/object/{quote(_type_of(oid), safe='')}/{quote(oid, safe='')}"


def _call(call: Call, tool: str, args: Mapping[str, Any]) -> Mapping[str, Any] | None:
    try:
        found = call(tool, args)
    except Exception:  # noqa: BLE001 - an unreadable source is a gap, never a guess
        return None
    return found if isinstance(found, Mapping) else None


def transaction_paths(otype: str, link_types: Mapping[str, Any] | None = None
                      ) -> list[tuple[str, str]]:
    """The record type's own transaction links, from the schema's declared link types: a
    link between it and a transaction record type (``TRANSACTION_TYPES``: its invoices,
    shipments, receipts, lots, confirmations), or to its own twin (``same_record_as``).
    A party (customer, supplier) or another order is never one, so their documents are
    never read for a pack."""
    if link_types is None:
        from ontology.schema import load_schema
        link_types = load_schema().link_types
    allowed = SIDE_TYPES.get(SIDE.get(otype, "lot"), frozenset())
    out = []
    for name, spec in sorted(link_types.items()):
        start, end = spec.get("from"), spec.get("to")
        if start == otype and (end in allowed or (
                end == otype and name.endswith(".same_record_as"))):
            out.append((name, "forward"))
        elif end == otype and start in allowed:
            out.append((name, "reverse"))
    return out


def _linked(call: Call, oid: str, paths: Sequence[tuple[str, str]]) -> list[str]:
    """The records the record's own transaction links reach, one hop, as the reader reads
    them (settled links only)."""
    out: list[str] = []
    for link_type, direction in paths:
        found = _call(call, "plx.around", {"from": oid, "limit": 50, "path": [
            {"link_type": link_type, "direction": direction}]})
        for edge in (found or {}).get("edges") or ():
            if not isinstance(edge, Mapping) or edge.get("tier") not in ("verified",
                                                                          "reviewed"):
                continue
            if edge.get("link_type") not in (None, link_type) or oid not in (
                    edge.get("from"), edge.get("to")):
                continue
            other = edge.get("to") if edge.get("from") == oid else edge.get("from")
            allowed = SIDE_TYPES.get(SIDE.get(_type_of(oid), "lot"), frozenset())
            if isinstance(other, str) and other != oid and other not in out \
                    and _type_of(other) in allowed | {_type_of(oid)}:
                out.append(other)
    return out[:MAX_LINKED]


def _names(doc: Mapping[str, Any], object_id: str, number: str | None) -> bool:
    """Whether a document names this record: a mention resolved to it, or its number in the
    document's title or snippet."""
    for mention in doc.get("mentions") or ():
        if isinstance(mention, Mapping) and mention.get("object_id") == object_id \
                and mention.get("tier") in CONFIRMED:   # a lead mention names nothing
            return True
    if not number or len(number) < 3:
        return False
    text = " ".join(filter(None, (_text(doc.get("title")), _text(doc.get("snippet")))))
    return re.search(r"(?<![0-9A-Za-z])" + re.escape(number) + r"(?![0-9A-Za-z])", text,
                     re.I) is not None


def _documents(call: Call, oid: str) -> tuple[list[Mapping[str, Any]], bool]:
    found = _call(call, "plx.documents", {"object_id": oid, "sources": SOURCES,
                                          "limit": MAX_DOCS})
    if found is None or not isinstance(found.get("documents"), list):
        return [], False
    return [d for d in found["documents"] if isinstance(d, Mapping)], True


def build(call: Call, held: Iterable[str], object_id: str, *,
          decide: Decide | None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The pack for ``object_id`` as this reader reads it (module doc). ``PackRefused`` for a
    type no pack is made for, or a record the reader cannot open."""
    otype = _type_of(object_id)
    if otype not in PACK_TYPES:
        raise PackRefused("not_packable")
    held = frozenset(held)
    reader = ap._Reader(call, held)
    if reader.object(object_id) is None:
        raise PackRefused("not_found")
    own = reader.number(object_id)
    gaps: list[dict[str, str]] = []
    side = SIDE.get(otype, "lot")
    customers = reader.linked(object_id, CUSTOMER_LINK[otype]) if otype in CUSTOMER_LINK \
        else []
    customer = customers[0] if len(customers) == 1 else None
    records = [object_id] + _linked(call, object_id, transaction_paths(otype))
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    for oid in records:
        docs, ok = _documents(call, oid)
        if not ok:
            gaps.append({"code": "documents_unread", "object_id": oid})
        for d in docs:
            family = family_of(_text(d.get("title")))
            # a lot or batch serves many orders: its own certificates and sheets belong to
            # every one of them, anything else only when it names this record; a lot's own
            # pack is its certificates and sheets only
            if (_type_of(oid) in SHARED_TYPES or side == "lot") and family not in \
                    LOT_FAMILIES and (side == "lot" or not _names(
                        d, object_id, own[0] if own else None)):
                continue
            # a spec sheet made for a customer: only that customer's pack
            if family == "spec_sds":
                named = {m.get("object_id") for m in d.get("mentions") or ()
                         if isinstance(m, Mapping) and m.get("tier") in CONFIRMED
                         and _type_of(str(m.get("object_id") or "")) == "Customer"}
                if named and not (customer and customer in named):
                    continue
            candidates.append((oid, d))
    # every document decided on its own citation before it is listed or counted
    handles = sorted({str(d.get("citation")) for _, d in candidates
                      if isinstance(d.get("citation"), str) and d.get("citation")})
    if decide is None:
        refused: Mapping[str, Any] | None = None
    else:
        try:
            refused = decide(handles) if handles else {}
        except Exception:  # noqa: BLE001 - an undecidable reader sees no document
            refused = None
    if refused is None:
        candidates = []
        if handles:
            gaps.append({"code": "item_access_unchecked", "object_id": object_id})
    found: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for via, doc in candidates:
        handle = doc.get("citation")
        # allowed only when decided and None (a handle the decision skipped is refused)
        if not isinstance(handle, str) or not handle or handle in seen or (
                refused is not None and decide is not None
                and (handle not in refused or refused[handle] is not None)):
            continue
        title = _text(doc.get("title"))
        family = family_of(title)
        if family is None or doc.get("kind") not in ("attachment", "sharepoint_file",
                                                     "original", "email"):
            continue
        if doc.get("kind") == "email" and family not in ("tracking",):
            continue   # a message is a document of the pack only for its tracking line
        seen.add(handle)
        rows = found.setdefault(family, [])
        if len(rows) < MAX_PER_FAMILY:
            rows.append({"family": family, "label": LABELS[family], "title": title,
                         "kind": doc.get("kind"), "date": doc.get("date"),
                         "via": via, "source": {"handle": handle}, "citation": handle})
    # linked records that are themselves the document (a posted invoice, a shipment)
    for oid in records[1:]:
        family = RECORD_FAMILY[side].get(_type_of(oid))
        number = reader.number(oid) if family else None
        if not family or number is None:
            continue
        rows = found.setdefault(family, [])
        if len(rows) < MAX_PER_FAMILY:
            rows.append({"family": family, "label": LABELS[family],
                         "title": f"{_type_of(oid)} {number[0]}"[:MAX_TITLE],
                         "kind": "record", "date": None, "via": oid, "url": _page(oid),
                         "source": {"handle": number[1]}, "citation": number[1]})
    expected = EXPECTED[otype]
    order = list(expected) + sorted(k for k in found if k not in expected)
    documents = [row for key in order for row in found.get(key, ())]
    missing = [key for key in expected if key not in found]
    playbook = _draft(reader, object_id, otype, own, order, found, missing)
    links = [{"system": "Workbench", "url": _page(object_id), "status": "ready",
              "reason": None}]
    links += ap.source_links(object_id, own[0] if own else None, ap.load_links(),
                             environ or {})
    return {"schema": SCHEMA, "object_id": object_id, "object_type": otype,
            "label": own[0] if own else otype, "label_source": own[1] if own else None,
            "documents": documents, "families": {k: len(v) for k, v in found.items()},
            "expected": list(expected), "missing": missing,
            "missing_labels": [LABELS[k] for k in missing],
            "records_read": len(records), "links": links, "draft": playbook["email"],
            "card": ap.card(playbook), "draft_missing": playbook["missing"],
            "gaps": gaps, "sent": False, "writes": 0,
            "basis": "Documents around the record and its settled linked records that you "
                     "may open, sorted by the words of their titles; nothing is sent."}


def _draft(reader: Any, oid: str, otype: str, own: tuple[str, str] | None,
           order: Sequence[str], found: Mapping[str, list[dict[str, Any]]],
           missing: Sequence[str]) -> dict[str, Any]:
    """The reply to the record's party, as a playbook ``alert_playbooks.card`` turns into a
    card: worded from the record's number, the listed documents' family labels and titles
    the reader may read; addressed only when one settled party's address is readable."""
    why: list[str] = []
    role, link = ap.PARTY.get(otype, (None, None))
    parties = reader.linked(oid, link) if link else []
    party = parties[0] if len(parties) == 1 else None
    to = None
    name = None
    if party:
        number = reader.number(party)
        name = number[0] if number else None
        address = reader.value(party, ap.PARTY_ADDRESSES.get(_type_of(party), ()))
        if address and ap._ADDRESS.fullmatch(address[1]):
            to = {"address": address[1].lower(), "source": address[2]}
    if role is None:
        why.append("the record has no outside party type")
    elif party is None:
        why.append(f"no single settled {role} is linked to the record for you")
    elif to is None:
        why.append(f"no {role} address you may read is recorded")
    number = own[0] if own else "the record"
    lines = []
    for key in order:
        for row in found.get(key, ()):
            lines.append(f"- {row['label']}: {row['title']}")
    body = (f"Hello{' ' + name if name else ''},\n\n"
            + (f"Please find the documents for {number}:\n" + "\n".join(lines) + "\n"
               if lines else f"We are gathering the documents for {number}.\n")
            + (f"\nTo follow: {', '.join(LABELS[k] for k in missing)}.\n" if missing else "")
            + "\nThank you.")
    email = {"channel": "email", "party": role, "to": to,
             "subject": f"Documents for {number}"[:200], "status": "draft_not_sent",
             "body": body, "attach": [row["citation"] for key in order
                                      for row in found.get(key, ()) if row["kind"] != "record"]}
    return {"id": "pack:" + oid,
            "summary": f"Documents for {number}"[:200], "email": email, "teams": None,
            "change": None, "facts": [], "missing": why}
