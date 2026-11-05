"""Chained fast answers: the owner's questions that follow one record to the next (a
purchase order to its vendor, the vendor's rep and the vendor's mail; a vendor to each of
its orders' lateness), answered by the same deterministic reads and answer gate as
``operating.fast_answer`` (its module doc: recognise, resolve, read, gate, fail closed).

* **A purchase order's follow-up** (``po_followup``: "PO#1 is late, what do we do next and
  who do we chase", "who is the vendor, how late is it, what did they last email us"):
  the order's status and lateness as ``fast_answer.po_status`` reads them; its vendor
  (the Business Central order's own ``vendorName``, else the supplier it is ordered from);
  the vendor's rep on the FileMaker order (``contactrep``, ``emailrep``, ``phonerep``);
  the vendor's latest message naming the order (mail from the rep's domains, else from
  the supplier's addresses); and, when the question asks what to do, a proposed next step
  built only from those cited facts. Nothing is ever sent or changed: the answer proposes.
* **A vendor's confirmation** (``po_confirmed``): the vendor's first message naming the
  order whose subject is an order confirmation (``CONFIRMATION``), its day and subject.
* **The original behind an order** (``po_first_mail``): the first message naming the
  order, from its vendor or one of us, on or after the day the order was created.
* **Anything late from a vendor** (``vendor_late``): every purchase order of the vendor's
  cards of that exact name, each judged by its own receipt verdict (``plx.process``).
  "Nothing is late" is said only when every order's verdict was read, none is late and
  none is unsettled; any order whose lateness the records leave open makes the answer
  partial and names it, never "nothing late".

A mail search never proves absence: a fact searched for and not found is said as not found
in the mail the reader can see (partial), or the question goes to the agent; a withheld
match or a capture scan cut short declines.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable, Mapping, Optional, Sequence

from . import fast_answer as fa
from .customer_profit import normal_name
from .fast_answer import Ask, Draft, Reads, Unanswerable, plain

FM_PO = "PurchaseOrder:portal.fm_generated_purchase_order:"
BC_PO_RECORD = "/purchaseOrders/"
# A phone said as one figure the reply gate reads whole ("416-679-1215"); any other form
# (an extension, a country prefix) is left unsaid, never broken into loose figures.
PHONE = re.compile(r"\(?\d{3}\)?[-. ]?\d{3}[-.]\d{4}")
CONTACT_FIELDS = ("row.contactrep", "row.emailrep", "row.phonerep", "row.emailcsr")
# Windows (days from the order's creation) the first-mail search reads in turn, the last
# unbounded: the first window holding a message from the vendor or us holds the first one.
FIRST_MAIL_WINDOWS = (7, 21, 45, 90, 180, 365, 730, None)
MAX_FIRST_MAIL_PAGES = 20   # mail pages one first-mail answer reads across its windows
MAX_SENDERS_READ = 16       # oldest messages one first-mail answer reads (sender, text)
MAX_NAMING_READ = 30        # newest messages one latest-mail answer reads (subject, opening)
NEWEST_PAGE = 25            # messages one page of a latest-mail search lists
MAX_MAIL_PAGES = 6          # pages of one mail search read for the oldest message, at most
FIRST_MAIL_PAGE = 8         # messages one page of a first-mail search lists (each page's
                            # mentions are resolved: a small page is a fast one)
MAX_ORDER_PAGES = 4         # pages of one supplier card's order listing, at most
# A verdict's own gap that the shared late-list rule decides is never late
# (``po_status.NEVER_LATE_SKIPS``: no requested-by date; a status no record settles).
NEVER_LATE_GAPS = frozenset({"no_due_date", "status_unknown"})
MAX_NAMED_LATE = 6          # late orders one vendor answer names (more: counted, first named)
MAX_VENDOR_ORDERS = 60      # purchase orders one vendor answer judges, each read
# The supplier cards whose orders a vendor answer judges: Business Central's and the
# Portal's (FileMaker's own supplier card lists the FileMaker copies of the Portal's
# FileMaker-era orders, judged on the Portal's record).
VENDOR_SYSTEMS = ("bc.vendors", "portal.app_supplier")
MAX_VENDOR_CARDS = 3        # same-named supplier cards (one per system) one answer reads
# A vendor's order confirmation, by its subject ("Order Confirmation for PO ...",
# "Confirmation for your P.O. # ...", "PO123 - order confirmed", "Order acknowledgement");
# never a pricing confirmation or a request to confirm.
CONFIRMATION = re.compile(
    r"\border\s+(?:confirmation|confirmed|conf|acknowledg(?:e)?ment)\b|"
    r"\bconfirmation\s+(?:for|of)\s+(?:your\s+)?(?:p\.?\s?o\b|purchase|order)|"
    r"\b(?:po|p\.o\.)\W*\d+\W+(?:-\s*)?(?:order\s+)?confirmed\b", re.I)
NOT_CONFIRMATION = re.compile(r"\bpre-?confirmation\b|\bpric(?:e|ing)\s+confirmation\b|"
                              r"\bplease\s+confirm\b|\bcan you confirm\b", re.I)
# What a follow-up question asks for beyond the order's status: a proposal is made only
# when it asks what to do.
ASKS_NEXT = re.compile(r"\b(next|what (?:should|do|can|would) (?:we|i) do|chase|call|"
                       r"contact|ping|reach|follow)\b")


# --------------------------------------------------------------------------- reads


def _card_handles(card: Mapping[str, Any]) -> list[str]:
    """Every record handle an object card's values (and its conflicts' values) cite."""
    out = []
    values = list(card.get("values") or ())
    for conflict in card.get("conflicts") or ():
        if isinstance(conflict, Mapping):
            values += list(conflict.get("values") or ())
    for value in values:
        cite = value.get("citation") if isinstance(value, Mapping) else None
        handle = cite.get("l1_handle") if isinstance(cite, Mapping) else None
        if isinstance(handle, str) and handle not in out:
            out.append(handle)
    display = (card.get("object") or {}).get("display")
    origin = display.get("origin") if isinstance(display, Mapping) else None
    if isinstance(origin, Mapping) and isinstance(origin.get("citation"), str):
        out.append(origin["citation"])
    return out


def _bc_order_record(reads: Reads, root: str) -> Optional[str]:
    """The Business Central purchase order record the order's card holds (one), or None."""
    card = reads.call("plx.object", {"object_id": root,
                                     "properties": ["po_number", "bc_status"]})
    records = {h.split("@", 1)[0]: h for h in _card_handles(card)
               if h.startswith("bc://") and BC_PO_RECORD in h}
    if len(records) > 1:
        raise Unanswerable("bc_order_not_one")
    return next(iter(records.values()), None)


def _supplier_object(reads: Reads, root: str) -> Optional[Mapping[str, Any]]:
    """The one supplier the order is ordered from, or None."""
    around = reads.call("plx.around", {"from": root, "limit": 5, "path": [
        {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]})
    suppliers = [o for o in around.get("objects") or () if o.get("object_type") == "Supplier"]
    return suppliers[0] if len(suppliers) == 1 and not fa._cut(around) else None


def _supplier(reads: Reads, root: str) -> Optional[str]:
    found = _supplier_object(reads, root)
    return None if found is None else str(found["object_id"])


def _vendor(reads: Reads, draft: Draft, root: str) -> tuple[str, Optional[str]]:
    """The order's vendor, said and cited: (its name, the supplier object it is ordered
    from or None). Business Central's own order names it (``vendorName``); an order with
    no Business Central record, by the one supplier it is ordered from."""
    supplier = _supplier_object(reads, root)
    handle = _bc_order_record(reads, root)
    if handle is not None:
        row = reads.row(handle, need=("row.vendorName",))
        name = row.get("row.vendorName")
        if isinstance(name, str) and name.strip():
            return draft.value("vendor", name.strip(), row["citation"], "row.vendorName"), \
                (supplier or {}).get("object_id")
    if supplier is None:
        raise Unanswerable("vendor_not_one")
    return fa._said(draft, "vendor", {"object": supplier}), supplier["object_id"]


def _fm_record(reads: Reads, root: str) -> Optional[str]:
    """The FileMaker order record of the order (its own, or the one record it is the same
    record as), or None."""
    if root.startswith(FM_PO):
        objects = [{"object_id": root, **(reads.object(root).get("object") or {})}]
    else:
        around = reads.call("plx.around", {"from": root, "limit": 10, "path": [
            {"link_type": "PurchaseOrder.same_record_as", "direction": "forward"}]})
        if fa._withheld(around):
            raise Unanswerable("fm_order_withheld")
        objects = [o for o in around.get("objects") or ()
                   if str(o.get("object_id")).startswith(FM_PO)]
    handles = set()
    for obj in objects:
        display = obj.get("display")
        origin = display.get("origin") if isinstance(display, Mapping) else None
        if isinstance(origin, Mapping) and isinstance(origin.get("citation"), str):
            handles.add(origin["citation"])
    return next(iter(handles)) if len(handles) == 1 else None


def _domain(address: Any) -> Optional[str]:
    text = str(plain(address) or "").strip()
    if not re.fullmatch(r"[^@\s]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
        return None
    domain = text.rsplit("@", 1)[1].casefold()
    return None if fa._RESERVED_TLD.search(domain) else domain


def _contact(reads: Reads, draft: Draft, root: str, *, say: bool
             ) -> tuple[dict[str, str], list[str]]:
    """The vendor's rep on the FileMaker order (said and cited when ``say``) and the mail
    domains of the rep and the vendor's service address: ({name, email, phone}, domains)."""
    handle = _fm_record(reads, root)
    if handle is None:
        return {}, []
    row = reads.row(handle, need=CONTACT_FIELDS)
    said: dict[str, str] = {}
    for key, path in (("name", "row.contactrep"), ("email", "row.emailrep"),
                      ("phone", "row.phonerep")):
        value = plain(row.get(path))
        if isinstance(value, str) and value.strip() and (key != "email" or _addresses(value)) \
                and (key != "phone" or PHONE.fullmatch(value.strip())):
            said[key] = draft.value(f"vendor_contact_{key}", value.strip(), row["citation"],
                                    path) if say else value.strip()
    domains = [_domain(a) for path in ("row.emailrep", "row.emailcsr")
               for a in _addresses(row.get(path))]
    return said, list(dict.fromkeys(d for d in domains if d))


# An address in a contact field ("sales@x.com (Sumera)", "a@x.com; b@x.com").
ADDRESS = re.compile(r"[^@\s;,()<>]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _addresses(field: Any) -> list[str]:
    """The addresses a contact field holds when it holds only addresses (separated by
    ``;``, ``,``, ``/`` or spaces, each perhaps with a note in brackets, as a FileMaker rep
    field is typed), each one valid (``_domain``); [] for anything else (free text with
    an address inside is no contact address)."""
    text = str(plain(field) or "")
    found = ADDRESS.findall(text)
    rest = re.sub(r"\([^()]*\)", " ", ADDRESS.sub(" ", text))
    if not found or re.search(r"[^;,/\s]", rest) or not all(_domain(a) for a in found):
        return []
    return found


def _vendor_mail(reads: Reads, draft: Draft, number: str, domains: Sequence[str],
                 supplier: Optional[str], unread: list[str], *, pages: int = 1,
                 since: Optional[str] = None,
                 newest: Optional[Callable[[Mapping[str, Any]], bool]] = None
                 ) -> list[Mapping[str, Any]]:
    """The vendor's messages naming the order (``number``'s digits): mail sent from the
    rep's domains, else from the supplier's own addresses (``participant``), newest first,
    ``pages`` pages at most of each search. ``newest`` (whether a listed message names the
    order): only the latest message naming it is wanted, so a search's pages (date order,
    newest first) are read only until one holds a message a person sent from them that
    names the order and is newer than the page's oldest entry (no later page can hold one
    as new), ``MAX_MAIL_PAGES`` at most. A withheld match or a scan cut
    short declines; mailboxes outside the reader's grants are noted in ``unread``."""
    query = re.sub(r"\D", "", number) or number
    needles = [f"@{d}" for d in domains] or ([supplier] if supplier else [])
    if not needles:
        raise Unanswerable("vendor_mail_unknown")
    found: list[Mapping[str, Any]] = []
    for needle in needles:
        arguments: dict[str, Any] = {"participant": needle, "query": query,
                                     "sources": ["graph_mail"], "limit": 10}
        if since:
            arguments["since"] = since
        limit = MAX_MAIL_PAGES if newest is not None else pages
        if newest is not None:
            arguments["limit"] = NEWEST_PAGE   # copies and our own replies fill pages
        for _page in range(limit):
            result = reads.call("plx.documents", arguments)
            if fa._withheld(result):
                raise Unanswerable("mail_withheld")
            _scan_cut(result, draft, unread)
            unread += fa._unread_sources(result, draft)
            listed = list(result.get("documents") or ())
            found += [d for d in listed if d.get("direction") == "from_them"]
            cursor = (result.get("completeness") or {}).get("next_cursor")
            if not isinstance(cursor, str) or not cursor:
                break
            if newest is not None and _latest_settled(listed, newest):
                break
            if _page == limit - 1:
                raise Unanswerable("mail_pages_cut")   # the one asked may be past the pages
            arguments = dict(arguments, cursor=cursor)
    return found


def _person_mail(doc: Mapping[str, Any]) -> bool:
    """A listed message a person sent from them (no automatic notice)."""
    return doc.get("direction") == "from_them" and doc.get("kind") == "email" \
        and not fa._AUTOMATIC.search(str(plain(doc.get("title")) or ""))


def _latest_settled(listed: Sequence[Mapping[str, Any]],
                    names: Callable[[Mapping[str, Any]], bool]) -> bool:
    """Whether a page of a participant search (date order, newest first) settles the
    latest message from them naming the order: it holds one a person sent from them that
    ``names`` the order and is strictly newer than the page's oldest entry, so every
    later page is older (a tie at the page's end may continue on the next page and is
    read there). An entry with no instant settles nothing."""
    stamps = [fa._instant(d.get("date")) for d in listed]
    if not listed or any(at is None for at in stamps):
        return False
    oldest = min(stamps)
    return any(at > oldest and _person_mail(d) and names(d)
               for at, d in sorted(zip(stamps, listed), key=lambda x: x[0], reverse=True))


class _Naming:
    """Whether a listed message is about order ``po`` (``_names_order``: its subject or
    opening names it; a digit run the search matched in another number, "200184722",
    does not). Each message (its copies one) is read at most once, ``MAX_NAMING_READ`` at most (more:
    ``Unanswerable``)."""

    def __init__(self, reads: Reads, po: str) -> None:
        self.reads, self.po = reads, po
        self.seen: dict[str, bool] = {}
        self.read = 0

    def __call__(self, doc: Mapping[str, Any]) -> bool:
        handle = str(doc.get("citation"))
        # copies of one message in several mailboxes (one subject, one instant) are read once
        copy = f"{plain(doc.get('title'))}\x00{fa._instant(doc.get('date'))}"
        if copy in self.seen:
            return self.seen[copy]
        if handle not in self.seen:
            if self.read >= MAX_NAMING_READ:
                raise Unanswerable("senders_cut")
            self.read += 1
            row = self.reads.row(handle, need=MESSAGE_TEXT)
            self.seen[handle] = self.seen[copy] = _names_order(row, self.po)
        return self.seen[handle]


def _newest_naming(found: Sequence[Mapping[str, Any]], names: "_Naming"
                   ) -> Optional[Mapping[str, Any]]:
    """The newest message a person sent of ``found`` that names the order, or None; two
    naming it at that instant with different subjects is ``Unanswerable`` (copies of one
    message are that message: the first by citation stands for it)."""
    mail = [(fa._instant(d.get("date")), d) for d in found if _person_mail(d)]
    if any(at is None for at, _d in mail):
        raise Unanswerable("message_time_unread")
    mail.sort(key=lambda x: x[0], reverse=True)
    for at, doc in mail:
        if names(doc):
            tied = [d for t, d in mail if t == at and names(d)]
            if len({plain(d.get("title")) for d in tied}) != 1:
                raise Unanswerable("messages_tie")
            return min(tied, key=lambda d: str(d.get("citation")))
    return None


SCAN_CUT_SAID = ("mail captured since the last full refresh was not all searched, so a "
                 "message there is not shown")


def _scan_cut(result: Mapping[str, Any], draft: Draft, unread: list[str]) -> None:
    """A capture scan its time bound stopped (``searched.capture_scan_cut``): the newest
    captures were not all read. Said as a gap (the answer is partial), never hidden."""
    if (result.get("searched") or {}).get("capture_scan_cut") and SCAN_CUT_SAID not in unread:
        draft.gaps.append({"state": "unknown", "reason": "newer_mail_cut",
                           "subject": "capture:graph_mail"})
        draft.answerability = "partial"
        unread.append(SCAN_CUT_SAID)


def _say_mail(reads: Reads, draft: Draft, doc: Mapping[str, Any], lead: str, tag: str
              ) -> str:
    """``lead`` and the message's day and subject, each read on its record and cited as
    ``<tag>_date`` and ``<tag>_subject``: the sentence's day."""
    row = reads.row(str(doc.get("citation")), need=("row.subject", "row.received_at"))
    stamp = str(row.get("row.received_at") or row.get("row.sent_at") or "")
    path = "row.received_at" if row.get("row.received_at") else "row.sent_at"
    subject = row.get("row.subject")
    if not fa._DAY.match(stamp) or not isinstance(subject, str) or not subject.strip() \
            or len(subject) > 300:
        raise Unanswerable("message_unread")
    day = draft.value(f"{tag}_date", stamp[:10], row["citation"], path)
    quoted = draft.value(f"{tag}_subject", subject.strip(), row["citation"], "row.subject")
    # a stop inside the subject ("P.O. # 4867") never ends the reply's sentence there
    shown = re.sub(r"([.!?;])[ \t]+", "\\1\u00a0", quoted)
    # mail captured since the served projection was built (``operating.mail_topup``)
    fresh = " (newer than the last full refresh)" if doc.get("captured_after_refresh") \
        is True else ""
    draft.sentences.append(f"{lead} on {day}{fresh}, subject \"{shown}\".")
    return day


def _earliest(found: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The oldest message a person sent of ``found``; two at its instant with different
    subjects, or none, is ``Unanswerable`` (copies of one message are that message)."""
    mail = [(fa._instant(d.get("date")), d) for d in found if d.get("kind") == "email"
            and not fa._AUTOMATIC.search(str(plain(d.get("title")) or ""))]
    mail = [(at, d) for at, d in mail if at is not None]
    if not mail:
        raise Unanswerable("no_message_read")
    first = min(at for at, _d in mail)
    tied = [d for at, d in mail if at == first]
    if len({plain(d.get("title")) for d in tied}) != 1:
        raise Unanswerable("messages_tie")
    return min(tied, key=lambda d: str(d.get("citation")))


def _order(reads: Reads, ask: Ask) -> tuple[str, Mapping[str, Any]]:
    """The purchase order the question names (as ``po_status`` resolves it): (root, its
    find candidate)."""
    found = reads.find(ask.mention)
    picked = fa._po_root(reads, ask, found, ("portal.app_purchase_order",
                                             "bc.purchase_orders",
                                             "portal.fm_generated_purchase_order"))
    return picked["object"]["object_id"], picked


# --------------------------------------------------------------------------- recipes


def po_followup(reads: Reads, ask: Ask, draft: Draft) -> None:
    """A purchase order's story and whom to chase (module doc): its status and lateness,
    its vendor, the vendor's rep, the vendor's latest message naming it and, when asked,
    a proposed next step from those facts. Proposes only: nothing is sent or changed."""
    fa.po_status(reads, Ask("po_status", ask.mention, ask.system), draft)
    if not draft.roots:
        return     # no such order: said by po_status, nothing more to follow
    root = draft.roots[-1]["object_id"]
    po = next((v["value"] for v in draft.values if v["name"] == "po_number"), ask.mention)
    late = any("overdue" in s or "after its due date" in s for s in draft.sentences[:1])
    vendor, supplier = _vendor(reads, draft, root)
    contact, domains = _contact(reads, draft, root, say=True)
    draft.sentences.append(f"The vendor is {vendor}.")
    if contact:
        parts = [contact.get("name"), contact.get("email"), contact.get("phone")]
        draft.sentences.append("Their rep on the FileMaker order is "
                               + ", ".join(p for p in parts if p) + ".")
    unread: list[str] = []
    names = _Naming(reads, po)
    found = _vendor_mail(reads, draft, po, domains, supplier, unread, newest=names)
    last = _newest_naming(found, names)
    if last is not None:
        _say_mail(reads, draft, last, f"The vendor's latest message naming {po} you can "
                  "see was sent", "last_message")
    else:
        draft.sentences.append(f"I found no message from the vendor naming {po} in the "
                               "mail you can see.")
        draft.gaps.append({"state": "unknown", "reason": "no_vendor_message_found",
                           "subject": "source:graph_mail"})
        draft.answerability = "partial"
    if ask.asks_next:
        whom = contact.get("name") or contact.get("email") or vendor
        how = contact.get("email") or contact.get("phone")
        if late:
            step = (f"Proposed next step (nothing has been sent): ask {whom}"
                    + (f" ({how})" if how and how != whom else "")
                    + f" for a firm delivery date on {po}")
            step += ", replying on their latest message." if last is not None else "."
        else:
            step = (f"Proposed next step (nothing has been sent): no chase is needed on "
                    f"what the records show; {whom} is the contact if the date moves.")
        draft.sentences.append(step)
    if unread:
        draft.sentences.append("Not settled: " + "; ".join(dict.fromkeys(unread)) + ".")
        draft.answerability = "partial"


def po_vendor_last_mail(reads: Reads, ask: Ask, draft: Draft) -> None:
    """The vendor's latest message naming a purchase order ("what's the last thing the
    vendor said about PO#1"): the newest message a person sent from the vendor's mail
    (the rep's and service domains on the FileMaker order, else the supplier's own
    addresses, as ``po_followup`` reads them) that names the order (``_Naming``), its day
    and subject cited. None found declines: the agent searches wider."""
    root, picked = _order(reads, ask)
    po = fa._said(draft, "po_number", picked, ask.mention)
    supplier = _supplier(reads, root)
    _contact_said, domains = _contact(reads, draft, root, say=False)
    unread: list[str] = []
    names = _Naming(reads, po)
    found = _vendor_mail(reads, draft, po, domains, supplier, unread, newest=names)
    last = _newest_naming(found, names)
    if last is None:
        raise Unanswerable("no_message_read")   # the agent searches wider
    _say_mail(reads, draft, last, f"The vendor's latest message naming {po} you can see "
              "was sent", "message")
    if unread:
        draft.sentences.append("Not settled: " + "; ".join(dict.fromkeys(unread)) + ".")
        draft.answerability = "partial"
    draft.roots.append({"object_id": root, "object_type": "PurchaseOrder",
                        "mention": ask.mention})


def po_confirmed(reads: Reads, ask: Ask, draft: Draft) -> None:
    """Whether the vendor confirmed a purchase order, and when: the first message from
    the vendor naming it whose subject is an order confirmation. When no message at all
    names the order (none from the vendor's mail, none the projection links to the
    order), that is said: no confirmation is on record in the mail the reader can see
    (partial: mail captured since the last refresh is not linked yet). Any other miss
    declines (a vendor's messages with no confirmation subject may still confirm it)."""
    root, picked = _order(reads, ask)
    po = fa._said(draft, "po_number", picked, ask.mention)
    supplier = _supplier(reads, root)
    _contact_said, domains = _contact(reads, draft, root, say=False)
    unread: list[str] = []
    found = _vendor_mail(reads, draft, po, domains, supplier, unread, pages=MAX_MAIL_PAGES)
    confirming = [d for d in found if CONFIRMATION.search(str(plain(d.get("title")) or ""))
                  and not NOT_CONFIRMATION.search(str(plain(d.get("title")) or ""))]
    if not confirming:
        # a message of theirs naming the order may confirm it in its text; one the search
        # matched on a digit run alone does not name it (a message naming it anywhere in
        # its text is linked to the order, read next)
        names = _Naming(reads, po)
        if any(_mentions(d, po) or names(d) for d in found) \
                or _linked_mail(reads, draft, root, unread, po):
            raise Unanswerable("no_confirmation_found")
        draft.sentences.append(
            f"I found no message naming {po} in the mail you can see, none from the vendor "
            "and none linked to the order, so no vendor confirmation is on record.")
        draft.gaps.append({"state": "unknown", "reason": "no_vendor_message_found",
                           "subject": "source:graph_mail"})
        draft.answerability = "partial"
        _newer_unsearched(draft, unread)
        draft.sentences.append("Not settled: " + "; ".join(dict.fromkeys(unread)) + ".")
        draft.roots.append({"object_id": root, "object_type": "PurchaseOrder",
                            "mention": ask.mention})
        return
    first = _earliest(confirming)
    _say_mail(reads, draft, first, f"Yes, the vendor confirmed {po}: their first order "
              "confirmation you can see was sent", "confirmation")
    if unread:
        draft.sentences.append("Not settled: " + "; ".join(dict.fromkeys(unread)) + ".")
        draft.answerability = "partial"
    draft.roots.append({"object_id": root, "object_type": "PurchaseOrder",
                        "mention": ask.mention})


LINKED_READ = 10   # messages linked to an order read for whether one names it
ORDER_KINDS = frozenset({"petra_po", "bc_document_no"})   # kinds an order's number is read as


def _linked_mail(reads: Reads, draft: Draft, root: str, unread: list[str], po: str) -> bool:
    """Whether a message linked to the order (``plx.documents`` ``object_id``: the
    projection's resolved mentions of it) names the order itself (``_mentions``). A
    message linked only through another record (a lead twin's number, an attachment)
    does not. A withheld match, a listing its scan cap cut or more linked messages than
    ``LINKED_READ`` count as naming it (nothing is said absent on a search read in
    part)."""
    result = reads.call("plx.documents", {"object_id": root, "sources": ["graph_mail"],
                                          "limit": LINKED_READ})
    completeness = result.get("completeness") or {}
    if fa._withheld(result) or completeness.get("reason") == "scan_cap" \
            or completeness.get("next_cursor"):
        return True
    _scan_cut(result, draft, unread)
    unread += fa._unread_sources(result, draft)
    digits = re.sub(r"\D", "", po)

    def names(doc: Mapping[str, Any]) -> bool:
        # the order's number as any order kind reads it (Business Central's bare 100109)
        return _mentions(doc, po) or any(
            isinstance(m, Mapping) and m.get("identifier_kind") in ORDER_KINDS
            and re.sub(r"\D", "", str(m.get("normalized") or "")) == digits
            for m in doc.get("mentions") or ())
    return any(names(d) for d in result.get("documents") or ())


def po_first_mail(reads: Reads, ask: Ask, draft: Draft) -> None:
    """The original behind a purchase order: the first message naming it, from its vendor
    (the rep's domains) or one of us, on or after the day the order was created. The
    messages are the mail the projection links to the order (its resolved mentions,
    ``plx.documents`` ``object_id``: a few linked rows, never a number's whole text
    search), read in windows from that day, each to its end (``MAX_MAIL_PAGES``), else
    declined. Mail captured since the last full refresh is not linked yet and is not
    searched: said as a gap (the answer is partial), never hidden."""
    root, picked = _order(reads, ask)
    po = fa._said(draft, "po_number", picked, ask.mention)
    card = reads.call("plx.object", {"object_id": root,
                                     "properties": ["portal_created_at", "order_date"]})
    # the day the order's record was created (the Portal's), else its order date
    days = next((sorted({str(v[0])[:10] for v in fa._object_values(card, prop)
                         if fa._DAY.match(str(v[0] or ""))})
                 for prop in ("portal_created_at", "order_date")
                 if any(fa._DAY.match(str(v[0] or "")) for v in fa._object_values(card, prop))),
                [])
    if len(days) != 1:
        raise Unanswerable("created_unread")
    _contact_said, domains = _contact(reads, draft, root, say=False)
    if not domains:
        raise Unanswerable("vendor_mail_unknown")
    unread: list[str] = []
    first = None
    begin = date.fromisoformat(days[0])
    pages_left = MAX_FIRST_MAIL_PAGES
    start = begin
    for window in FIRST_MAIL_WINDOWS:
        # Windows follow one another (a day is in one of them): a window read whole with
        # no message from the vendor or us leaves the first one in a later window.
        arguments: dict[str, Any] = {"object_id": root, "sources": ["graph_mail"],
                                     "limit": FIRST_MAIL_PAGE, "since": start.isoformat()}
        if window is not None:
            end = begin + timedelta(days=window)
            arguments["until"] = end.isoformat()
            start = end + timedelta(days=1)
        found: list[Mapping[str, Any]] = []
        for page in range(MAX_MAIL_PAGES):
            if pages_left <= 0:
                raise Unanswerable("mail_pages_cut")
            pages_left -= 1
            result = reads.call("plx.documents", arguments)
            if fa._withheld(result):
                raise Unanswerable("mail_withheld")
            completeness = result.get("completeness") or {}
            if completeness.get("reason") == "scan_cap":
                raise Unanswerable("mail_scan_cut")   # links past the cap were never read
            _scan_cut(result, draft, unread)
            unread += fa._unread_sources(result, draft)
            found += list(result.get("documents") or ())
            cursor = completeness.get("next_cursor")
            if not isinstance(cursor, str) or not cursor:
                break
            if page == MAX_MAIL_PAGES - 1:
                raise Unanswerable("mail_pages_cut")   # the oldest may be past the pages
            arguments = dict(arguments, cursor=cursor)
        first = _first_from(reads, found, domains, po)
        if first is not None:
            break
    if first is None:
        raise Unanswerable("no_first_message")
    _say_mail(reads, draft, first, f"The first message naming {po} you can see was sent",
              "first_message")
    _newer_unsearched(draft, unread)
    if unread:
        draft.sentences.append("Not settled: " + "; ".join(dict.fromkeys(unread)) + ".")
        draft.answerability = "partial"
    draft.roots.append({"object_id": root, "object_type": "PurchaseOrder",
                        "mention": ask.mention})


NEWER_UNSEARCHED_SAID = ("mail captured since the last full refresh is not linked to "
                         "records yet and was not searched, so a message there is not shown")


def _newer_unsearched(draft: Draft, unread: list[str]) -> None:
    """A search of the mail linked to a record (``object_id``) reads the served generation
    only: mail captured since it was built is not searched. Said as a gap (the answer is
    partial), never hidden."""
    if SCAN_CUT_SAID in unread or NEWER_UNSEARCHED_SAID in unread:
        return
    draft.gaps.append({"state": "unknown", "reason": "newer_mail_cut",
                       "subject": "capture:graph_mail"})
    draft.answerability = "partial"
    unread.append(NEWER_UNSEARCHED_SAID)


def _po_keys(po: str) -> set[str]:
    """The normalised forms a mention of order ``po`` reads as."""
    return {fa._norm_id(po), "PO" + re.sub(r"\D", "", po)}


def _mentions(doc: Mapping[str, Any], po: str) -> bool:
    """Whether a listed message names the order itself (one of its mentions, shown or
    counted in its ``mention_summary``, reads as the order's number), not only a digit
    run the search matched or a link through another record."""
    want = _po_keys(po)
    summary = doc.get("mention_summary")
    counted = summary.get("identifiers") if isinstance(summary, Mapping) else None
    return any(isinstance(m, Mapping) and fa._norm_id(m.get("normalized")) in want
               for m in [*(doc.get("mentions") or ()), *(counted or ())])


# The text a message is about an order in: its subject and its opening.
MESSAGE_TEXT = ("row.subject", "row.body_preview")


def _names_order(row: Mapping[str, Any], po: str) -> bool:
    """Whether a message is about order ``po``: the shared identifier grammar
    (``ontology.mentions.extract``) reads the order's number in its own subject or opening
    (``MESSAGE_TEXT``, as read). A message naming it only further down (a payment
    statement listing many orders, an older thread quoted below, another order's
    confirmation mentioning it in passing) or linked to it only through an attachment or
    another record is not about it."""
    from ontology import mentions
    want = _po_keys(po)
    text = "\n".join(str(row.get(path)) for path in MESSAGE_TEXT
                     if isinstance(row.get(path), str))
    return any(fa._norm_id(m.get("normalized")) in want for m in mentions.extract(text))


def _first_from(reads: Reads, found: Sequence[Mapping[str, Any]], domains: Sequence[str],
                po: str) -> Optional[Mapping[str, Any]]:
    """The oldest message of ``found`` about order ``po`` (``_names_order``) that a person
    sent from the vendor's domains or from one of us (the sender's domain is the mailbox
    owner's); None when none is. Messages are read oldest first, ``MAX_SENDERS_READ`` at
    most (more unread: declines). A tie at the oldest instant with different subjects,
    or a sender unread, declines."""
    mail = [(fa._instant(d.get("date")), d) for d in found if d.get("kind") == "email"
            and not fa._AUTOMATIC.search(str(plain(d.get("title")) or ""))]
    mail = sorted(((at, d) for at, d in mail if at is not None),
                  key=lambda x: (x[0], str(x[1].get("citation"))))
    rows: dict[str, Mapping[str, Any]] = {}

    def read(doc: Mapping[str, Any]) -> Mapping[str, Any]:
        handle = str(doc.get("citation"))
        if handle not in rows:
            if len(rows) >= MAX_SENDERS_READ:
                raise Unanswerable("senders_cut")
            rows[handle] = reads.row(handle, need=("row.from", "row.mailbox_upn",
                                                   "row.received_at", *MESSAGE_TEXT))
        return rows[handle]

    for at, doc in mail:
        row = read(doc)
        if not fa._DAY.match(str(plain(row.get("row.received_at")) or "")):
            continue    # a copy kept as sent (no receipt): the message as received counts
        if not _names_order(row, po):
            continue    # linked to the order without being about it
        sender, owner = _domain(row.get("row.from")), _domain(row.get("row.mailbox_upn"))
        if sender is None:
            raise Unanswerable("sender_unread")
        if sender in domains or sender == owner:
            subject = plain(doc.get("title"))
            for t, other in mail:
                if t == at and plain(other.get("title")) != subject \
                        and _names_order(read(other), po):
                    raise Unanswerable("messages_tie")
            return doc
    return None


def _vendor_cards(reads: Reads, name: str) -> list[Mapping[str, Any]]:
    """The supplier cards whose normalised name is the name asked: at most one per
    system binding (two of one system are a did-you-mean)."""
    found = reads.find(name, "Supplier")
    want = normal_name(name)
    if not want:
        raise Unanswerable("no_name")
    named = [c for c in fa._candidates(found, ("Supplier",))
             if normal_name(str(plain((c.get("object") or {}).get("display")) or "")) == want
             and fa._binding(c).startswith(VENDOR_SYSTEMS)]
    if len(fa._candidates(found, ("Supplier",))) >= fa.FIND_LIMIT and not named:
        raise Unanswerable("supplier_find_full")
    by_system: dict[str, list[Mapping[str, Any]]] = {}
    for c in named:
        by_system.setdefault(fa._binding(c), []).append(c)
    for system, cards in by_system.items():
        if len({c["object"]["object_id"] for c in cards}) > 1:
            raise fa._Choice(fa.Choices(name, [
                {"object_id": c["object"]["object_id"], "object_type": "Supplier",
                 "display": c["object"].get("display")} for c in cards]))
    cards = [cs[0] for cs in by_system.values()]
    if not cards:
        raise Unanswerable("supplier_not_resolved")
    if len(cards) > MAX_VENDOR_CARDS:
        raise Unanswerable("too_many_supplier_cards")
    return sorted(cards, key=lambda c: (not fa._binding(c).startswith("bc."),
                                        fa._binding(c)))


def _twin_groups(reads: Reads, picks: Sequence[Mapping[str, Any]]
                 ) -> list[list[Mapping[str, Any]]]:
    """``picks`` (each with its ``object``) grouped into one group per order: records an
    established identity link joins (``why_trail``'s twins, as the reader reads them);
    a link that cannot be read joins nothing (the records are then named apart)."""
    import time
    from . import why_trail
    from .ontology_tools import default_schema
    ids = [str(p["object"]["object_id"]) for p in picks]
    parent = {oid: oid for oid in ids}

    def root(oid: str) -> str:
        while parent[oid] != oid:
            oid = parent[oid]
        return oid
    run = why_trail._Run(reads.call, time.monotonic() + why_trail.BUDGET_SECONDS)
    schema = default_schema()
    for oid in ids:
        try:
            twins = {t for t, _ in why_trail._twins(run, oid, "PurchaseOrder", schema)}
        except Exception:  # noqa: BLE001 - unread: joined to nothing
            continue
        for twin in twins & set(ids):
            parent[root(twin)] = root(oid)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for pick, oid in zip(picks, ids):
        groups.setdefault(root(oid), []).append(pick)
    return list(groups.values())


def _listed(reads: Reads, arguments: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every purchase order a ``plx.around`` lists, its pages followed
    (``MAX_ORDER_PAGES``); a withheld or still cut listing declines."""
    out: list[Mapping[str, Any]] = []
    arguments = dict(arguments)
    for page in range(MAX_ORDER_PAGES):
        around = reads.call("plx.around", arguments)
        if fa._withheld(around):
            raise Unanswerable("orders_withheld")
        out += [o for o in around.get("objects") or () if o.get("object_type") == "PurchaseOrder"]
        if len(out) > MAX_VENDOR_ORDERS:
            raise Unanswerable("too_many_orders")
        completeness = around.get("completeness") or {}
        cursor = completeness.get("next_cursor")
        if not completeness.get("truncated"):
            return out
        if not isinstance(cursor, str) or not cursor or page == MAX_ORDER_PAGES - 1:
            raise Unanswerable("orders_cut")   # every open order is judged, or no answer
        arguments = dict(arguments, cursor=cursor)
    raise Unanswerable("orders_cut")


def _open_orders(reads: Reads, card: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The supplier card's purchase orders that may be open by the shared status rule
    (``po_status``): on Business Central's card, the orders not fully received; on the
    Portal's, those whose header word is an issued word other than Complete (the words
    read from a grouped count of every order of the card, so a stored spelling is never
    missed). Every other order is closed, cancelled, a draft or of a word that never
    makes an order late; none of them can be late."""
    from . import po_status as ps
    path = [{"link_type": "PurchaseOrder.ordered_from", "direction": "reverse"}]
    base = {"from": card["object"]["object_id"], "limit": 50, "path": path}
    if fa._binding(card).startswith("bc."):
        return _listed(reads, dict(base, filters=[
            {"property": "bc_fully_received", "op": "eq", "value": False}]))
    first = reads.call("plx.around", dict(base, limit=1))
    if fa._withheld(first) or not isinstance(first.get("set"), str):
        raise Unanswerable("orders_withheld")
    counted = reads.call("plx.aggregate", {"set": first["set"], "measure": {"op": "count"},
                                           "group_by": ["status"]})
    if counted.get("partial") or fa._withheld(counted) \
            or (counted.get("completeness") or {}).get("complete") is False:
        raise Unanswerable("statuses_uncounted")
    issued = ps.ISSUED_WORDS - ps.COMPLETE_WORDS
    words = []
    for group in counted.get("groups") or ():
        raw = plain((group.get("key") or {}).get("status"))
        if isinstance(raw, str) and ps.word(raw) in issued and raw not in words:
            words.append(raw)
    if not words:
        return []
    return _listed(reads, dict(base, filters=[{"property": "status", "op": "in",
                                               "value": words}]))


def vendor_late(reads: Reads, ask: Ask, draft: Draft) -> None:
    """Whether anything from a vendor is late (module doc): each purchase order of the
    vendor's cards judged by its own receipt verdict; late ones named with their due
    dates; "nothing is late" only when every verdict was read and none is late or open."""
    cards = _vendor_cards(reads, ask.mention)
    # the name read on the card itself (an ambiguous find binds no evidence)
    shown = reads.object(cards[0]["object"]["object_id"]).get("object") or {}
    named = fa._said(draft, "vendor", {"object": shown}, ask.mention)
    orders: dict[str, Mapping[str, Any]] = {}
    for card in cards:
        for obj in _open_orders(reads, card):
            orders.setdefault(str(obj["object_id"]), obj)
        if len(orders) > MAX_VENDOR_ORDERS:
            raise Unanswerable("too_many_orders")
    late, open_ok, undated, unsettled = [], [], [], []
    for oid, obj in orders.items():
        process, verdict = fa._receipt_verdict(reads, oid, required=False)
        if verdict is None:
            continue      # not open by the shared definition: nothing to be late
        if verdict.get("late") is True and verdict.get("measured_against") != "received":
            late.append((obj, verdict))
        elif verdict.get("late") is False and verdict.get("state") in ("not_due",
                                                                       "not_applicable") \
                or verdict.get("late") is False and verdict.get("measured_against") \
                == "received":
            open_ok.append((obj, verdict))
        elif verdict.get("late") is not True and isinstance(verdict.get("gap"), Mapping) \
                and verdict["gap"].get("reason") in NEVER_LATE_GAPS:
            undated.append((obj, verdict))   # never late by the shared late-list rule
        else:
            unsettled.append((obj, verdict))
    numbers: dict[str, str] = {}

    def number(obj: Mapping[str, Any], tag: str) -> str:
        oid = str(obj["object_id"])
        if oid not in numbers:
            numbers[oid] = fa._said(draft, f"{tag}_{len(numbers) + 1}", {"object": obj})
        return numbers[oid]

    def due(verdict: Mapping[str, Any], tag: str) -> Optional[str]:
        value = fa._date_value(verdict, verdict.get("governing_date"))
        if value is None:
            return None
        cite = value["citation"]
        return draft.value(f"{tag}_due_{len(numbers)}", str(verdict["governing_date"]),
                           cite["l1_handle"], cite["field_path"])

    def grouped(found: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
                ) -> list[list[Mapping[str, Any]]]:
        """One order's records of several systems (a Portal order and its Business
        Central twin, joined by an identity link) are one order, named once."""
        picks = [{"object": obj, "verdict": verdict} for obj, verdict in
                 sorted(found, key=lambda x: (str(x[1].get("governing_date") or "9999"),
                                              str(plain(x[0].get("display")))))]
        return _twin_groups(reads, picks)

    if late:
        said, groups = [], grouped(late)
        for group in groups[:MAX_NAMED_LATE]:
            pos = [number(pick["object"], "late_po") for pick in group]
            when = next((d for d in (due(pick["verdict"], "late_po") for pick in group
                                     if pick["verdict"].get("late") is True) if d), None)
            po = pos[0] + (f" (also {', '.join(pos[1:])})" if pos[1:] else "")
            said.append(f"{po}, due {when}" if when else po)
        draft.sentences.append(
            f"Yes, from {named} " + ("this purchase order is overdue: " if len(groups) == 1
                                     else "these purchase orders are overdue: "
                                     if len(groups) <= MAX_NAMED_LATE else
                                     "more purchase orders than one answer names are "
                                     "overdue, the longest overdue first: ")
            + "; ".join(said) + ("; and others." if len(groups) > MAX_NAMED_LATE else "."))
    if unsettled:
        names = [" / ".join(number(pick["object"], "open_po") for pick in group)
                 for group in grouped(unsettled)]
        reasons: list[str] = []
        for _obj, verdict in unsettled:
            reasons += fa._gaps_of(verdict, {}, draft)
        draft.sentences.append(
            ("Lateness can't be settled from the records for " if not late else
             "Also, lateness can't be settled from the records for ")
            + ", ".join(names) + (": " + "; ".join(dict.fromkeys(reasons)) if reasons else "")
            + ".")
        draft.gaps.append({"state": "unknown", "reason": "lateness_unsettled",
                           "subject": "PurchaseOrder.late"})
        draft.answerability = "partial"
    if not late and not unsettled:
        upcoming = []
        for obj, verdict in open_ok:
            if verdict.get("state") == "not_due":
                po = number(obj, "open_po")
                when = due(verdict, "open_po")
                upcoming.append(f"{po} (due {when})" if when else po)
        draft.sentences.append(
            f"Nothing is late from {named}: none of their purchase orders you can see is "
            "overdue." + (" Still open and not yet due: " + "; ".join(upcoming) + "."
                          if upcoming else " None is open past its due date."))
    if undated:
        names = [" / ".join(number(pick["object"], "undated_po") for pick in group)
                 for group in grouped(undated)[:MAX_NAMED_LATE]]
        draft.sentences.append(
            ("These open orders have" if len(undated) > 1 else "This open order has")
            + " no requested-by date on record, so the shared late rule never counts "
            + ("them" if len(undated) > 1 else "it") + " late: " + ", ".join(names)
            + (" and others" if len(grouped(undated)) > MAX_NAMED_LATE else "") + ".")
    draft.roots += [{"object_id": c["object"]["object_id"], "object_type": "Supplier",
                     "mention": ask.mention} for c in cards]


RECIPES = {"po_followup": po_followup, "po_confirmed": po_confirmed,
           "vendor_last_mail": po_vendor_last_mail,
           "po_first_mail": po_first_mail, "vendor_late": vendor_late}
