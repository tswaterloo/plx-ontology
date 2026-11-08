"""Two of the business's largest pains as alerts, run by ``operating.proactive_engine`` beside
its other rules (each in its own time window, ``DETECTOR_SECONDS``):

* ``customer_chase`` (customers chasing order status). An inbound customer message (mail, or
  a Teams message) asks where an order is: a chase phrase (``chase_phrase``: where is, status
  of, ETA, tracking, following up, still waiting) in the message's own text (quoted history
  removed) or, for a message that is no reply, its subject, and a mention link from the
  message to one of an OPEN customer order's records (``SalesOrder``) or to the customer PO it
  references. Two kinds:

  - ``customer_chasing_order``: the same order chased ``REPEAT_CHASES`` or more times within
    ``CHASE_DAYS`` of the run's day, or chased once while it is past its requested date. Its
    playbook (``follow_up.draft``) is a status reply worded from cited values only (order
    number, status, lines shipped and open, shipment and tracking when a settled shipment is
    linked, the requested date, a quality hold on a batch allocated to its lines as the reason
    for delay), never from the customer's text; and ``follow_up.notify``: tell the account's
    rep. Nothing is sent: a person copies, edits and sends it behind the confirm click.
  - ``tell_before_they_ask``: an open order whose requested date falls within
    ``DUE_SOON_DAYS`` and that is not ready to ship, for a customer who chased any order within
    ``CHASE_HISTORY_DAYS``: a status note before they ask.

  Inbound: an external sender (no tenant domain). Customer: a settled ``Email.sent_by_customer``
  link to the order's customer, or the sender's address (or, for a non-public domain, its
  domain) on the customer's record. A Teams message counts only from an outside sender (a
  user not in the tenant's captured directory and not linked to a staff ``Person``; with no
  directory, none counts) and is always a lead (no customer link exists for it). A chase
  not attributed so, or one tied to the order by a lead mention only, is a lead
  (``Lead, not firm``, low priority). Every chase an alert counts is cited and its mailbox
  required. People first, fail closed
  (``people``: ``operating.chunking.permissions.Exclusion``): with the guard unavailable or no
  tenant domain known the rule is ``blocked``; a message in a mailbox or chat the exclusion does
  not clear is dropped unread; a Teams sender not cleared is dropped. Messages are deduplicated
  by internet message id across mailboxes. An alert stores message ids and subjects cut to
  ``SUBJECT_CHARS`` characters, never a body. The order's openness rests on no shipment or
  invoice being recorded: ``REQUIRES_GRANT`` (every audience of orders, shipments, invoices,
  and of the messages cited).

* ``quality_holds``. Two kinds:

  - ``quality_hold_aging``: a batch (``Batch.quality_status``) or a lot (``Lot.qc_status``) on
    hold or in quarantine for ``HOLD_DAYS`` or more, worst first (orders waiting, then days):
    its item, quantity and the open customer orders waiting on it (``BatchAllocation``). A
    batch states no hold date: its age is counted from the earliest order allocated to it, a
    lot's from its receipt; neither known: ``not_evaluated`` (``hold_age_unknown``). A
    cancelled batch is never a hold.
  - ``reject_trend``: per item and per supplier, rejected batches and lots within
    ``REJECT_DAYS`` of the run's day, at least ``MIN_REJECTS`` and more than in the
    ``REJECT_DAYS`` before. Playbook: notify QC; for a supplier's rejects (a rejected lot it
    supplied) a draft complaint to the supplier, worded from cited values only.

Every read is bounded by an index: the open orders' records, the links touching them, the
messages those links name. Fixture runs are ``isolated-fixture``; no rule names a record, a
customer or an item.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.units import unit_token

from .proactive_business import _day, _dec, _one, _text, _tick

CHASE = "customer_chase"
CHASING = "customer_chasing_order"
TELL_FIRST = "tell_before_they_ask"
HOLDS = "quality_holds"
HOLD_AGING = "quality_hold_aging"
REJECT_TREND = "reject_trend"
RULES = (CHASE, HOLDS)

CHASE_DAYS = 14
REPEAT_CHASES = 2
DUE_SOON_DAYS = 3
CHASE_HISTORY_DAYS = 180
SUBJECT_CHARS = 60
MAX_NAMED_ORDERS = 4          # a message naming more orders is a report, not a chase
HOLD_DAYS = 14
HOLD_HIGH_DAYS = 30
REJECT_DAYS = 90
MIN_REJECTS = 2

SO = "SalesOrder"
SO_LINE_OF = "SalesOrderLine.line_of"
SO_LINE_ITEM = "SalesOrderLine.for_item"
ORDERED_BY = "SalesOrder.ordered_by"
CUSTOMER_PO = "SalesOrder.references_customer_po"
SHIPS = "Shipment.ships"
SENT_BY_CUSTOMER = "Email.sent_by_customer"
CUSTOMER_TWINS = "Customer.same_record_as"
MESSAGE_TYPES = ("Email", "TeamsChatMessage", "TeamsChannelMessage")
MENTIONS_ORDER = tuple(f"{t}.mentions_sales_order" for t in MESSAGE_TYPES)
MENTIONS_CPO = tuple(f"{t}.mentions_customer_purchase_order" for t in MESSAGE_TYPES)
EMAIL_PROPS = ("subject", "sender", "sent_at", "received_at", "body_preview",
               "internet_message_id")
TEAMS_PROPS = ("subject", "from", "created_at", "message_type", "deleted_at")
CUSTOMER_MAIL = ("email", "email_2", "email_3", "bc_email")
PUBLIC_MAIL = frozenset({"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
                         "icloud.com", "aol.com", "msn.com", "yahoo.ca", "hotmail.ca",
                         "me.com", "protonmail.com", "shaw.ca", "rogers.com", "sympatico.ca"})
AUTOMATED = re.compile(r"(?:^|[._+-])(?:no-?reply|do-?not-?reply|notifications?|mailer-daemon|"
                       r"postmaster|bounce|alerts?)(?:[._+-]|@)", re.I)
READY_WORDS = ("ready", "picked", "packed", "staged")
SETTLED_MENTION = ("verified", "reviewed", "corroborated")

BATCH = "Batch"
LOT = "Lot"
BATCH_PROPS = ("quality_status", "status", "batch_code", "batch_number", "lot_number",
               "planned_quantity", "produced_quantity")
HELD = frozenset({"on-hold", "on hold", "hold", "on_hold", "quarantine", "quarantined"})
REJECTED = frozenset({"rejected", "reject"})
VOID_BATCH = frozenset({"cancelled", "canceled", "void", "voided", "deleted"})
BATCH_ITEM = "Batch.makes_bulk"
BATCH_TWINS = "Batch.same_record_as"
ALLOCATES = "BatchAllocation.allocates"
ALLOC_LINE = "BatchAllocation.for_order_line"
LOT_ITEM = "Lot.of_item"
LOT_RECEIPT = "Lot.received_in"
RECEIPT_LINE_LOT = "ReceiptLine.received_lot"
RECEIPT_SUPPLIER = "Receipt.supplied_by"

# What a customer asking where an order is says, by family (any case, one sentence).
CHASE_FAMILIES = {
    "where_is": re.compile(r"\bwhere(?:'s|\s+is|\s+are)\b[^.?!\n]{0,40}\b(?:order|po|p\.o\.|"
                           r"shipment|delivery|goods|product|pallets?|it)\b", re.I),
    "status": re.compile(r"\b(?:order|po|shipment|delivery)\s+status\b|\bstatus\s+(?:of|on|"
                         r"update|for)\b|\bany\s+(?:update|news)\b|\bupdate\s+on\b", re.I),
    "eta": re.compile(r"\b(?:eta|tracking\s+(?:number|info|#|link)|when\s+(?:will|can|should|"
                      r"do|does)\s+(?:we|i|it|this|the\s+order)\b[^.?!\n]{0,30}\b(?:receive|get|"
                      r"expect|ship|arrive|deliver)|expected\s+(?:ship|delivery|arrival)\s+date|"
                      r"(?:has|did)\s+(?:it|this|the\s+order|our\s+order)\s+(?:ship|shipped|left))\b",
                      re.I),
    "follow_up": re.compile(r"\b(?:following\s+up|follow(?:ing)?[- ]up\s+on|checking\s+in|"
                            r"chasing|still\s+waiting|still\s+haven'?t|have\s+not\s+(?:yet\s+)?"
                            r"received|haven'?t\s+(?:yet\s+)?received|not\s+yet\s+received)\b",
                            re.I),
}
_REPLY = re.compile(r"^\s*(?:\[[^\]]{1,30}\]\s*)*(?:re|aw|sv|antw)\s*(?:\[\d+\])?\s*[:：]", re.I)
_FORWARD = re.compile(r"^\s*(?:\[[^\]]{1,30}\]\s*)*(?:fw|fwd|wg|tr)\s*[:：]", re.I)
_OID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def chase_phrase(text: Any) -> str | None:
    """The first chase family the message's own text says (quoted history removed); None."""
    from ontology.mentions import QuotedRanges

    if not isinstance(text, str) or not text.strip():
        return None
    quoted = QuotedRanges(text)
    own = "".join(ch for i, ch in enumerate(text) if not quoted.contains(i))
    for name, pattern in CHASE_FAMILIES.items():
        if pattern.search(own):
            return name
    return None


def _instant(raw: Any) -> datetime | None:
    try:
        text = str(raw).strip()
        return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None


def _touch(st: Any, ids: Iterable[str], types: Sequence[str], *, end: str | None = None
           ) -> list[dict[str, Any]]:
    """The current links of ``types`` with an end among ``ids`` (by the end indexes), records
    not removed only; ``end``: only those whose ``from_object``/``to_object`` is one."""
    wanted = set(ids)
    out = []
    for row in st.touching(sorted(wanted), types) if wanted else ():
        link = {"link_id": row[1], "link_type": row[2], "from_object": row[3],
                "to_object": row[4], "tier": row[5], "markings": row[6]}
        if row[7] or (end is not None and link[end] not in wanted):
            continue
        out.append(link)
    return out


def _addr(raw: Any) -> str:
    text = str(raw or "").strip().casefold()
    found = re.search(r"<([^>]+)>", text)
    return (found.group(1) if found else text).strip()


def _words(entry: Mapping[str, Any] | None, *props: str) -> str | None:
    for prop in props:
        row = _one(((entry or {}).get("props") or {}).get(prop))
        if row and row.get("value") not in (None, "", "None"):
            return str(row["value"]).strip()
    return None


def _row(entry: Mapping[str, Any] | None, *props: str) -> Mapping[str, Any] | None:
    for prop in props:
        row = _one(((entry or {}).get("props") or {}).get(prop))
        if row and row.get("value") not in (None, "", "None"):
            return row
    return None


# --------------------------------------------------------------------------- customer chase

def _message_text(st: Any, handles: Mapping[str, str]) -> dict[str, str]:
    """Teams message -> its body text (the text index's first chunks, by the message's
    version handle: an index probe each), read here and never kept."""
    from ontology.text_index import chunk_text

    out: dict[str, str] = {}
    items = sorted(handles.items())
    for start in range(0, len(items), 400):
        part = dict(items[start:start + 400])
        try:
            rows = st.conn.execute(
                "SELECT f.object_id, k.body FROM text_fields f JOIN text_chunks k ON "
                "k.field_id = f.field_id WHERE f.citation IN (SELECT value FROM json_each(?)) "
                "AND f.field_path = 'row.body_text' AND k.start = 0",
                (json.dumps(sorted(set(part.values()))),)).fetchall()
        except Exception:  # noqa: BLE001 - no text index in this store: subjects only
            return out
        for oid, body in rows:
            if oid in part:
                try:
                    out[oid] = chunk_text(body)
                except Exception:  # noqa: BLE001 - an unreadable chunk says nothing
                    continue
    return out


class _Chases:
    """Every cleared inbound chase of the orders asked for, by order cluster."""

    def __init__(self, st: Any, people: Any, as_of: date, skipped: Counter) -> None:
        self.st, self.people, self.as_of, self.skipped = st, people, as_of, skipped
        self.domains = tuple(getattr(people, "internal", ()) or ())
        # the tenant's people (the captured directory's object ids): a Teams sender among
        # them is staff, never the customer
        self.staff = frozenset(str(v).lower() for v in
                               (getattr(people, "directory", None) or {}).values())
        self.staff_sent: frozenset[str] = frozenset()

    def internal(self, address: str) -> bool:
        domain = address.rsplit("@", 1)[1] if "@" in address else ""
        return bool(domain) and any(domain == d or domain.endswith("." + d)
                                    for d in self.domains)

    def read(self, root_of: Mapping[str, str], customer_of: Mapping[str, str | None],
             addresses: Mapping[str, set[str]], since: date) -> dict[str, list[dict]]:
        """order cluster -> its chases sent on or after ``since`` (``root_of``: order record
        -> cluster; ``customer_of``: cluster -> customer cluster; ``addresses``: customer
        cluster -> its addresses and private domains)."""
        st = self.st
        named: dict[str, set[str]] = defaultdict(set)
        settled: dict[tuple[str, str], bool] = {}
        for link in _touch(st, root_of, MENTIONS_ORDER, end="to_object"):
            order = root_of[link["to_object"]]
            named[link["from_object"]].add(order)
            key = (link["from_object"], order)
            settled[key] = settled.get(key, False) or link["tier"] in SETTLED_MENTION
        cpo_orders: dict[str, set[str]] = defaultdict(set)
        for link in _touch(st, root_of, [CUSTOMER_PO], end="from_object"):
            cpo_orders[link["to_object"]].add(root_of[link["from_object"]])
        for link in _touch(st, cpo_orders, MENTIONS_CPO, end="to_object"):
            for order in cpo_orders[link["to_object"]]:
                named[link["from_object"]].add(order)
                key = (link["from_object"], order)
                settled[key] = settled.get(key, False) or link["tier"] in SETTLED_MENTION
        if not named:
            return {}
        audience = {oid: key for oid, key in self._audiences(sorted(named))}
        cleared = {}
        for oid, key in audience.items():
            if key not in cleared:
                cleared[key] = bool(self.people.unit_clear(key))
        mails = sorted(o for o in named if o.startswith("Email:") and cleared.get(audience.get(o)))
        teams = sorted(o for o in named if not o.startswith("Email:")
                       and cleared.get(audience.get(o)))
        self.skipped["message_not_cleared"] += len(named) - len(mails) - len(teams)
        values = st.values(["Email"], EMAIL_PROPS, objects=mails) if mails else {}
        values.update(st.values(["TeamsChatMessage", "TeamsChannelMessage"], TEAMS_PROPS,
                                objects=teams) if teams else {})
        handles = {v["object_id"]: v["l1_handle"] for v in st.versions(objects=sorted(values))}
        # a Teams message tied to a staff Person by its sender link is ours
        self.staff_sent = frozenset(l["from_object"] for l in _touch(
            st, teams, [f"{t}.sent_by_person" for t in MESSAGE_TYPES[1:]], end="from_object"))
        texts = _message_text(st, {o: handles[o] for o in teams if o in handles})
        by_customer = self._sent_by_customer(mails)
        seen: set[tuple[str, str]] = set()
        out: dict[str, list[dict]] = defaultdict(list)
        for oid in sorted(values):
            _tick(st)
            entry = values[oid]
            orders = named[oid]
            if len(orders) > MAX_NAMED_ORDERS:
                self.skipped["names_many_orders"] += 1
                continue
            chase = self._chase(oid, entry, texts.get(oid))
            if chase is None or chase["day"] < since:
                continue
            for order in sorted(orders):
                if (order, chase["dedupe"]) in seen:
                    continue   # another mailbox's copy of the same message
                seen.add((order, chase["dedupe"]))
                who = customer_of.get(order)
                known = addresses.get(who or "", set())
                sender = chase["sender"]
                attributed = chase["channel"] == "mail" and (who is not None and (
                    who in by_customer.get(oid, ()) or sender in known
                    or (sender.rsplit("@", 1)[-1] in known
                        and sender.rsplit("@", 1)[-1] not in PUBLIC_MAIL)))
                out[order].append({**chase, "audience": audience[oid],
                                   "handle": handles.get(oid),
                                   "lead": not (attributed and settled.get((oid, order)))})
        return out

    def _audiences(self, ids: Sequence[str]) -> Iterable[tuple[str, str]]:
        for start in range(0, len(ids), 500):
            yield from self.st.conn.execute(
                "SELECT object_id, audience_key FROM objects WHERE object_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(list(ids[start:start + 500])),))

    def _sent_by_customer(self, mails: Sequence[str]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for link in _touch(self.st, mails, [SENT_BY_CUSTOMER], end="from_object"):
            if link["tier"] in ("verified", "reviewed"):
                out[link["from_object"]].add(link["to_object"])
        return out

    def _chase(self, oid: str, entry: Mapping[str, Any], text: str | None) -> dict | None:
        """One message as a chase (its channel, day, sender, family, subject cut short), or
        None (counted by reason) when it is ours, automated, a forward or asks nothing."""
        teams = not oid.startswith("Email:")
        subject = _words(entry, "subject") or ""
        if teams:
            raw = _words(entry, "from") or ""
            if _words(entry, "deleted_at"):
                return None
            try:
                said = json.loads(raw)
            except ValueError:
                said = None
            sender = str(said.get("id") or "").strip().lower() if isinstance(said, dict) \
                else ""
            if not _OID.fullmatch(sender) or said.get("kind") != "user":
                self.skipped["sender_unknown"] += 1   # an app, a bot or no sender
                return None
            if not self.people.identity_clear(sender):
                self.skipped["sender_not_cleared"] += 1
                return None
            if not self.staff:
                self.skipped["teams_sender_not_decided"] += 1   # no directory: fail closed
                return None
            if sender in self.staff or oid in self.staff_sent:
                return None   # our own staff relaying or asking: never a customer's chase
            when = _instant(_words(entry, "created_at"))
            body = text or ""
            dedupe = oid
        else:
            sender = _addr(_words(entry, "sender"))
            if "@" not in sender:
                self.skipped["sender_unknown"] += 1
                return None
            if self.internal(sender):
                if not self.people.identity_clear(sender):
                    self.skipped["sender_not_cleared"] += 1
                return None   # ours: never a customer's chase
            if AUTOMATED.search(sender):
                self.skipped["automated_sender"] += 1
                return None
            when = _instant(_words(entry, "sent_at", "received_at"))
            body = _words(entry, "body_preview") or ""
            dedupe = (_words(entry, "internet_message_id") or oid).strip().casefold()
        if when is None:
            self.skipped["message_undated"] += 1
            return None
        if _FORWARD.match(subject):
            self.skipped["forward_not_judged"] += 1
            return None
        family = chase_phrase(body) or (None if _REPLY.match(subject) else
                                        chase_phrase(subject))
        if family is None:
            return None
        return {"id": oid, "channel": "teams" if teams else "mail", "day": when.date(),
                "sender": sender, "family": family, "dedupe": dedupe,
                "subject": (subject if subject != "None" else "")[:SUBJECT_CHARS]}


def _customers(st: Any, records: Sequence[str], settled: Sequence[str]
               ) -> tuple[dict[str, str], dict[str, set[str]]]:
    """order record -> its customer cluster (a settled ``ordered_by``), and customer cluster
    -> the addresses and private domains its records state."""
    from . import po_status

    by = {l["from_object"]: l["to_object"] for l in _touch(st, records, [ORDERED_BY],
                                                           end="from_object")
          if l["tier"] in settled}
    twins = [(l["from_object"], l["to_object"]) for l in _touch(st, set(by.values()),
                                                                [CUSTOMER_TWINS])
             if l["tier"] in settled]
    root = po_status.clusters(twins, set(by.values()) | {e for p in twins for e in p})
    values = st.values(["Customer"], CUSTOMER_MAIL, objects=sorted(root))
    addresses: dict[str, set[str]] = defaultdict(set)
    for oid, entry in values.items():
        for rows in (entry.get("props") or {}).values():
            for r in rows:
                for part in re.split(r"[;,\s]+", str(r.get("value") or "")):
                    address = _addr(part)
                    if "@" in address:
                        addresses[root.get(oid, oid)].update(
                            {address, address.rsplit("@", 1)[1]})
    return {m: root.get(c, c) for m, c in by.items()}, addresses


def customer_chase(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                   settled: Sequence[str], skips: Callable, people: Any,
                   orders: Callable[[], tuple[dict[str, Any], list[Any]]],
                   absence: Callable[[Sequence[str]], list[str]]) -> dict[str, Any]:
    """``customer_chasing_order`` and ``tell_before_they_ask`` (module doc)."""
    if people is None or getattr(people, "unavailable", None):
        cause = getattr(people, "unavailable", None) or "exclusion_not_loaded"
        return result([], status="blocked", reasons=["people_exclusion_unavailable", cause])
    if not tuple(getattr(people, "internal", ()) or ()):
        return result([], status="blocked", reasons=["tenant_domains_unknown"])
    skipped = skips()
    counts: Counter = Counter()
    found, verdicts = orders()
    open_orders = [v for v in verdicts if v.cls == "open"]
    root_of = {m: v.top for v in open_orders for m in v.group}
    by_top = {v.top: v for v in open_orders}
    customer_rec, addresses = _customers(st, sorted(root_of), settled)
    customer_of: dict[str, str | None] = {}
    for v in open_orders:
        whos = {customer_rec[m] for m in v.group if m in customer_rec}
        customer_of[v.top] = next(iter(whos)) if len(whos) == 1 else None
    chases = _Chases(st, people, as_of, skipped)
    recent = chases.read(root_of, customer_of, addresses,
                         as_of - timedelta(days=CHASE_DAYS))
    # the customers with an order due soon and not ready: their chase history, every order
    due_soon = [v for v in open_orders if v.wanted is not None and v.wanted.day is not None
                and 0 <= (v.wanted.day - as_of).days <= DUE_SOON_DAYS
                and not _ready(found, v.group)]
    history: dict[str, list[dict]] = defaultdict(list)
    soon_customers = {customer_of[v.top] for v in due_soon if customer_of.get(v.top)}
    if soon_customers:
        theirs = [l["from_object"] for l in _touch(st, _members_of(st, soon_customers, settled),
                                                   [ORDERED_BY], end="to_object")
                  if l["tier"] in settled]
        past_root = {m: m for m in theirs}
        past_customer = {m: customer_rec.get(m) or _one_customer(st, m, settled)
                         for m in theirs}
        past = chases.read(past_root, past_customer, addresses,
                           as_of - timedelta(days=CHASE_HISTORY_DAYS))
        for record, found_chases in past.items():
            who = past_customer.get(record)
            if who in soon_customers:
                history[who].extend(c for c in found_chases
                                    if not c["lead"] and c["channel"] == "mail")
    requires_base = absence(("SalesOrder", "Shipment", "SalesInvoice"))
    gaps = []
    for top, found_chases in sorted(recent.items()):
        _tick(st)
        verdict = by_top[top]
        unique = sorted({c["dedupe"]: c for c in found_chases}.values(),
                        key=lambda c: (c["day"], c["id"]))
        counts["chased_orders"] += 1
        late_days = (as_of - verdict.wanted.day).days if verdict.wanted is not None and \
            verdict.wanted.day is not None else None
        late = late_days is not None and late_days > 0
        if len(unique) < REPEAT_CHASES and not late:
            counts["chased_once"] += 1
            continue
        facts = _order_facts(st, found, verdict, settled, cites)
        is_lead = all(c["lead"] for c in unique)
        number = facts["number"] or "this order"
        first, last = unique[0]["day"], unique[-1]["day"]
        said = (f"chased {len(unique)} times between {first.isoformat()} and "
                f"{last.isoformat()}" if len(unique) > 1 else f"chased on {last.isoformat()}")
        made = gap(
            CHASING, CHASE, top, SO, f"chase|{last.isoformat()}",
            summary=("Lead, not firm: " if is_lead else "") + f"Customer {said} about order "
                    f"{number}" + (f", {late_days} days past its requested date" if late
                                   else ""),
            missing="Send the customer the order's status (drafted from the order's records; "
                    "nothing is sent until you send it) and tell the account's rep",
            why=f"{len(unique)} inbound message(s) ask where the order is (a chase phrase in "
                f"their own text) and name it by a mention link"
                + (f"; its requested date {verdict.wanted.day.isoformat()} has passed"
                   if late else "") + " (each message and the order's status cited).",
            priority=("high" if late or len(unique) >= 3 else "medium") if not is_lead
            else "low",
            about=(SO, "status"),
            groups=[*facts["groups"], *[cites(st.versions(objects=[c["id"]]))
                                        for c in unique]],
            event={"days": late_days if late else (as_of - first).days,
                   "orders_affected": 1, "chases": len(unique),
                   "messages": [{"id": c["id"], "channel": c["channel"],
                                 "sent": c["day"].isoformat(), "subject": c["subject"]}
                                for c in unique]})
        if made is None:
            skipped["chase_uncited"] += 1
            continue
        made["requires"] = sorted({*requires_base, *(c["audience"] for c in unique)})
        made["follow_up"]["draft"] = _status_draft(facts, as_of)
        made["follow_up"]["notify"] = {"role": "account_rep", "status": "draft_not_sent",
                                       "text": f"The customer chased order {number} "
                                               f"{len(unique)} time(s); the status reply "
                                               "is drafted on the alert."}
        gaps.append(made)
    for verdict in due_soon:
        _tick(st)
        who = customer_of.get(verdict.top)
        # each chase once (one copy per message), by date; every one counted is cited and
        # its mailbox required, so a reader is never told of a chase it may not open
        before = sorted({c["dedupe"]: c for c in sorted(
            history.get(who or "", []), key=lambda c: (c["day"], c["id"]),
            reverse=True)}.values(), key=lambda c: (c["day"], c["id"]))
        if not before:
            continue
        counts["due_soon_chasers"] += 1
        facts = _order_facts(st, found, verdict, settled, cites)
        number = facts["number"] or "this order"
        due = verdict.wanted.day
        made = gap(
            TELL_FIRST, CHASE, verdict.top, SO, f"due|{due.isoformat()}",
            summary=f"Order {number} is due {due.isoformat()} and not ready to ship; this "
                    f"customer chased {len(before)} time(s) in the "
                    f"last {CHASE_HISTORY_DAYS} days",
            missing="Tell the customer the order's status before they ask (drafted; nothing "
                    "is sent until you send it)",
            why=f"The order is open, not marked ready, with its requested date within "
                f"{DUE_SOON_DAYS} days, and the customer has chased orders by mail before "
                "(the order's status and date cited, and each earlier chase).",
            priority="medium", about=(SO, "required_date"),
            groups=[*facts["groups"], *[cites(st.versions(objects=[c["id"]]))
                                        for c in before]],
            event={"days": (due - as_of).days, "orders_affected": 1, "chases": len(before),
                   "messages": [{"id": c["id"], "channel": c["channel"],
                                 "sent": c["day"].isoformat(), "subject": c["subject"]}
                                for c in before[-3:]]})
        if made is None:
            continue
        made["requires"] = sorted({*requires_base, *(c["audience"] for c in before)})
        made["follow_up"]["draft"] = _status_draft(facts, as_of)
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"open_orders": len(open_orders), "chased_orders":
                     counts["chased_orders"], "chased_once": counts["chased_once"],
                     "due_soon_not_ready": len(due_soon),
                     "due_soon_chasers": counts["due_soon_chasers"]}
    return out


def _members_of(st: Any, customers: set[str], settled: Sequence[str]) -> set[str]:
    twins = _touch(st, customers, [CUSTOMER_TWINS])
    return set(customers) | {e for l in twins if l["tier"] in settled
                             for e in (l["from_object"], l["to_object"])}


def _one_customer(st: Any, record: str, settled: Sequence[str]) -> str | None:
    whos = {l["to_object"] for l in _touch(st, [record], [ORDERED_BY], end="from_object")
            if l["tier"] in settled}
    return next(iter(whos)) if len(whos) == 1 else None


def _ready(found: Mapping[str, Any], group: Sequence[str]) -> bool:
    words = [str(r.get("value") or "").lower() for m in group
             for p in ("status", "fm_status", "bc_status")
             for r in (found.get(m) or {}).get("props", {}).get(p, ())]
    return any(w in text for text in words for w in READY_WORDS)


def _order_facts(st: Any, found: Mapping[str, Any], verdict: Any,
                 settled: Sequence[str], cites: Callable) -> dict[str, Any]:
    """What a status reply may say of one order, each fact with its cited rows: number,
    status, requested date, lines (item, ordered, shipped, open), a settled shipment's
    carrier and tracking, and a quality hold on a batch allocated to its lines."""
    group = verdict.group
    gov = min(group)
    entry = found.get(gov) or {}
    number_row = _row(entry, "order_number", "fm_erp_code")
    status_row = _row(entry, "status", "fm_status", "bc_status")
    date_rows = [r for m in group for r in (found.get(m) or {}).get("props", {})
                 .get("required_date", ())]
    groups = [cites([r for r in (number_row, status_row) if r] or
                    [{"l1_handle": v["l1_handle"], "object_id": v["object_id"],
                      "markings": v["markings"]} for v in st.versions(objects=[gov])])]
    if date_rows:
        groups.append(cites(date_rows))
    line_links = _touch(st, group, [SO_LINE_OF], end="to_object")
    line_ids = sorted({l["from_object"] for l in line_links if l["tier"] in settled})
    lines_v = st.values(["SalesOrderLine"], ("ordered_quantity", "shipped_quantity",
                                             "outstanding_quantity", "sage_item_code"),
                        objects=line_ids) if line_ids else {}
    items = {l["from_object"]: l["to_object"] for l in _touch(st, line_ids, [SO_LINE_ITEM],
                                                              end="from_object")
             if l["tier"] in settled}
    item_v = st.values(["Product"], ("code", "name"), objects=sorted(set(items.values()))) \
        if items else {}
    lines = []
    cited: list[Any] = []
    for line in line_ids[:12]:
        e = lines_v.get(line)
        ordered = _row(e, "ordered_quantity")
        if ordered is None:
            continue
        shipped = _row(e, "shipped_quantity")
        open_q = _row(e, "outstanding_quantity")
        item_row = _row(item_v.get(items.get(line, "")), "code", "name") or _row(
            e, "sage_item_code")
        unit = unit_token(ordered.get("unit")) or "units"
        o = _dec(ordered["value"])
        s = _dec(shipped["value"]) if shipped else None
        left = _dec(open_q["value"]) if open_q else (o - s if o is not None and s is not None
                                                     else None)
        lines.append({"item": str(item_row["value"]) if item_row else None,
                      "ordered": _text(o) if o is not None else None,
                      "shipped": _text(s) if s is not None else None,
                      "open": _text(left) if left is not None else None, "unit": unit})
        cited += [r for r in (ordered, shipped, open_q, item_row) if r]
    if cited:
        groups.append(cites(cited))
    shipments = sorted({l["from_object"] for l in _touch(st, group, [SHIPS], end="to_object")
                        if l["tier"] in settled})
    ship = None
    if shipments:
        sv = st.values(["Shipment"], ("ship_date", "carrier", "tracking_number",
                                      "delivery_date"), objects=shipments)
        e = sv.get(shipments[-1])
        rows = {p: _row(e, p) for p in ("ship_date", "carrier", "tracking_number",
                                        "delivery_date")}
        ship = {p: str(r["value"]) for p, r in rows.items() if r}
        if any(rows.values()):
            groups.append(cites([r for r in rows.values() if r]))
    holds = []
    allocs = _touch(st, line_ids, [ALLOC_LINE], end="to_object")
    batches = {l["to_object"] for l in _touch(st, {a["from_object"] for a in allocs},
                                              [ALLOCATES], end="from_object")}
    if batches:
        bv = st.values([BATCH], ("quality_status", "batch_code", "batch_number"),
                       objects=sorted(batches))
        for b, e in sorted(bv.items()):
            q = _row(e, "quality_status")
            if q and str(q["value"]).strip().lower() in HELD:
                code = _row(e, "batch_code", "batch_number")
                holds.append(str(code["value"]) if code else "a batch")
                groups.append(cites([r for r in (q, code) if r]))
    return {"number": str(number_row["value"]) if number_row else None,
            "status": str(status_row["value"]) if status_row else None,
            "due": verdict.wanted.day if verdict.wanted is not None else None,
            "lines": lines, "shipment": ship, "holds": holds, "groups": groups,
            "handles": sorted({c[0] for g in groups for c in g})}


def _status_draft(facts: Mapping[str, Any], as_of: date) -> dict[str, Any]:
    """The status reply a person may copy, edit and send: cited values only."""
    number = facts["number"] or "your order"
    body = [f"Thank you for checking on order {number}."]
    if facts["status"]:
        body.append(f"Its status is {facts['status']}.")
    for line in facts["lines"][:8]:
        what = line["item"] or "an item"
        parts = [f"{line['ordered']} {line['unit']} ordered"] if line["ordered"] else []
        if line["shipped"] is not None:
            parts.append(f"{line['shipped']} shipped")
        if line["open"] is not None:
            parts.append(f"{line['open']} still open")
        body.append(f"- {what}: {', '.join(parts)}.")
    ship = facts.get("shipment") or {}
    if ship:
        said = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(ship.items()))
        body.append(f"Shipment: {said}.")
    else:
        body.append("It has not shipped yet.")
    due = facts.get("due")
    if due is not None:
        body.append(f"The requested date is {due.isoformat()}"
                    + (" and has passed; we will confirm a new date." if due < as_of else "."))
    if facts.get("holds"):
        body.append("It is waiting on a quality check of " + ", ".join(facts["holds"][:3])
                    + " before it can ship.")
    return {"kind": "customer_status_reply", "status": "draft_not_sent", "sent": False,
            "to": "customer", "subject": f"Order {number}: status",
            "body": "\n".join(body), "citations": facts["handles"]}


# --------------------------------------------------------------------------- quality holds

def quality_holds(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                  settled: Sequence[str], skips: Callable) -> dict[str, Any]:
    """``quality_hold_aging`` and ``reject_trend`` (module doc)."""
    from . import po_status
    from . import sales_order_status as so_status

    skipped = skips()
    counts: Counter = Counter()
    batches = st.values([BATCH], BATCH_PROPS)
    lots = st.values([LOT], ("qc_status", "lot_number"))

    def word(entry: Mapping[str, Any], prop: str) -> str:
        return (_words(entry, prop) or "").lower()
    held_b = {b for b, e in batches.items() if word(e, "quality_status") in HELD
              and word(e, "status") not in VOID_BATCH}
    rej_b = {b for b, e in batches.items() if word(e, "quality_status") in REJECTED
             and word(e, "status") not in VOID_BATCH}
    held_l = {l for l, e in lots.items() if word(e, "qc_status") in HELD}
    rej_l = {l for l, e in lots.items() if word(e, "qc_status") in REJECTED}
    counts["batches_held"], counts["lots_held"] = len(held_b), len(held_l)
    twins = [(l["from_object"], l["to_object"]) for l in _touch(st, held_b | rej_b,
                                                                [BATCH_TWINS])
             if l["tier"] in settled]
    b_root = po_status.clusters(twins, held_b | rej_b | {e for p in twins for e in p})
    # items, waiting orders (allocations), receipts and suppliers: by the end indexes
    b_item = {l["from_object"]: l for l in _touch(st, held_b | rej_b, [BATCH_ITEM],
                                                  end="from_object") if l["tier"] in settled}
    l_item = {l["from_object"]: l for l in _touch(st, held_l | rej_l, [LOT_ITEM],
                                                  end="from_object") if l["tier"] in settled}
    item_ids = {l["to_object"] for l in (*b_item.values(), *l_item.values())}
    items = st.values(["Product"], ("code", "name"), objects=sorted(item_ids)) \
        if item_ids else {}
    allocs = _touch(st, held_b | rej_b, [ALLOCATES], end="to_object")
    alloc_line = {l["from_object"]: l["to_object"] for l in _touch(
        st, {a["from_object"] for a in allocs}, [ALLOC_LINE], end="from_object")}
    line_order = {l["from_object"]: l["to_object"] for l in _touch(
        st, set(alloc_line.values()), [SO_LINE_OF], end="from_object")
        if l["tier"] in settled}
    orders = sorted(set(line_order.values()))
    ov = st.values([SO], ("order_number", "order_date", "status", "fm_status", "bc_status",
                          "bc_fully_shipped", "sage_complete"), objects=orders) if orders else {}
    shipped = {l["to_object"] for l in _touch(st, orders, [SHIPS], end="to_object")
               if l["tier"] in settled}
    waiting: dict[str, dict[str, Any]] = defaultdict(dict)   # batch -> order -> entry
    for a in allocs:
        order = line_order.get(alloc_line.get(a["from_object"], ""))
        if order is not None and order in ov:
            waiting[a["to_object"]][order] = ov[order]
    rl = _touch(st, held_l | rej_l, [RECEIPT_LINE_LOT], end="to_object")
    rl_v = st.values(["ReceiptLine"], ("received_quantity",),
                     objects=sorted({l["from_object"] for l in rl})) if rl else {}
    lot_qty = defaultdict(list)
    for l in rl:
        row = _row(rl_v.get(l["from_object"]), "received_quantity")
        if row:
            lot_qty[l["to_object"]].append(row)
    l_receipt = {l["from_object"]: l["to_object"] for l in _touch(
        st, held_l | rej_l, [LOT_RECEIPT], end="from_object") if l["tier"] in settled}
    receipts = st.values(["Receipt"], ("received_date", "receipt_number"),
                         objects=sorted(set(l_receipt.values()))) if l_receipt else {}
    supplier = {l["from_object"]: l["to_object"] for l in _touch(
        st, set(l_receipt.values()), [RECEIPT_SUPPLIER], end="from_object")
        if l["tier"] in settled}
    suppliers = st.values(["Supplier"], ("name", "code"), objects=sorted(set(
        supplier.values()))) if supplier else {}

    def open_order(order: str, entry: Mapping[str, Any]) -> bool:
        state = so_status.record_status(entry.get("props") or {},
                                        binding=po_status.binding_of(order),
                                        fulfilled=order in shipped)
        return state.cls != so_status.CLOSED

    def item_of(link: Mapping[str, Any] | None) -> tuple[str | None, Any]:
        if link is None:
            return None, None
        return link["to_object"], _row(items.get(link["to_object"]), "code", "name")

    def batch_day(b: str) -> tuple[date | None, list]:
        days = [(d, r) for e in waiting.get(b, {}).values()
                for r in [_row(e, "order_date")] if r for d in [_day(r["value"])] if d]
        if not days:
            return None, []
        first = min(days, key=lambda dr: dr[0])
        return first[0], [first[1]]

    def lot_day(l: str) -> tuple[date | None, list]:
        r = _row(receipts.get(l_receipt.get(l, "")), "received_date")
        d = _day(r["value"]) if r else None
        return (d, [r]) if d else (None, [])

    gaps = []
    seen: set[str] = set()
    holds = []
    for b in sorted(held_b):
        top = b_root.get(b, b)
        if top in seen:
            continue
        seen.add(top)
        day, day_rows = batch_day(b)
        e = batches[b]
        status = _row(e, "quality_status")
        if day is None:
            skipped.note("hold_age_unknown", [b], [(b, "[]")])
            continue
        age = (as_of - day).days
        if age < HOLD_DAYS:
            continue
        open_w = sorted(o for o, oe in waiting.get(b, {}).items() if open_order(o, oe))
        item, item_row = item_of(b_item.get(b))
        qty = _row(e, "produced_quantity", "planned_quantity")
        code = _row(e, "batch_code", "batch_number", "lot_number")
        holds.append((len(open_w), age, "batch", b, status, day_rows, item_row, qty, code,
                      [_row(ov[o], "order_number") for o in open_w]))
    for l in sorted(held_l):
        day, day_rows = lot_day(l)
        status = _row(lots[l], "qc_status")
        if day is None:
            skipped.note("hold_age_unknown", [l], [(l, "[]")])
            continue
        age = (as_of - day).days
        if age < HOLD_DAYS:
            continue
        item, item_row = item_of(l_item.get(l))
        qtys = lot_qty.get(l, [])
        qty = qtys[0] if len(qtys) == 1 else None
        holds.append((0, age, "lot", l, status, day_rows, item_row, qty,
                      _row(lots[l], "lot_number"), []))
    holds.sort(key=lambda h: (-h[0], -h[1], h[3]))
    for n_open, age, kind, oid, status, day_rows, item_row, qty, code, numbers in holds:
        _tick(st)
        name = str(code["value"]) if code else f"this {kind}"
        what = f" of {item_row['value']}" if item_row else ""
        amount = (f", {_text(_dec(qty['value']))} {unit_token(qty.get('unit')) or 'units'}"
                  if qty and _dec(qty["value"]) is not None else "")
        waits = (f"; {n_open} open order(s) wait on it"
                 + (f" ({', '.join(str(r['value']) for r in numbers[:3] if r)})"
                    if any(numbers) else "") if n_open else "")
        basis = "its earliest allocated order" if kind == "batch" else "its receipt"
        made = gap(
            HOLD_AGING, HOLDS, oid, BATCH if kind == "batch" else LOT, f"hold|{kind}",
            summary=f"{kind.capitalize()} {name}{what}{amount} on {status['value']} "
                    f"{age} days (counted from {basis}){waits}",
            missing="Decide release or reject with QC; the orders waiting on it are held up "
                    "until then",
            why=f"The {kind}'s quality status is {status['value']} and {basis} is dated "
                f"{age} days ago (no hold date is stored); cited.",
            priority="high" if n_open or age >= HOLD_HIGH_DAYS else "medium",
            about=(BATCH if kind == "batch" else LOT,
                   "quality_status" if kind == "batch" else "qc_status"),
            groups=[cites([status, *([code] if code else [])]), cites(day_rows),
                    *([cites([item_row])] if item_row else []),
                    *([cites([qty])] if qty else []),
                    *([cites([r for r in numbers if r])] if any(numbers) else [])],
            event={"days": age, "orders_affected": n_open})
        if made is None:
            continue
        made["follow_up"]["notify"] = {"role": "quality_control", "status": "draft_not_sent",
                                       "text": f"{kind.capitalize()} {name} has been on hold "
                                               f"{age} days; decide release or reject."}
        gaps.append(made)
    # reject trends: per item and per supplier, the recent window against the one before
    rejects = []   # (day, item, item row, supplier, supplier row, status rows, day rows)
    for b in sorted(rej_b):
        day, rows = batch_day(b)
        if day is None:
            skipped.note("reject_undated", [b], [(b, "[]")])
            continue
        item, item_row = item_of(b_item.get(b))
        rejects.append((day, item, item_row, None, None,
                        [_row(batches[b], "quality_status"), None], rows))
    for l in sorted(rej_l):
        day, rows = lot_day(l)
        if day is None:
            skipped.note("reject_undated", [l], [(l, "[]")])
            continue
        item, item_row = item_of(l_item.get(l))
        sup = supplier.get(l_receipt.get(l, ""))
        rejects.append((day, item, item_row, sup,
                        _row(suppliers.get(sup or ""), "name", "code"),
                        [_row(lots[l], "qc_status"), _row(lots[l], "lot_number")], rows))
    for axis in ("item", "supplier"):
        groups: dict[str, list] = defaultdict(list)
        for r in rejects:
            key = r[1] if axis == "item" else r[3]
            if key is not None:
                groups[key].append(r)
        for key, found in sorted(groups.items()):
            _tick(st)
            recent = [r for r in found if 0 <= (as_of - r[0]).days <= REJECT_DAYS]
            before = [r for r in found if REJECT_DAYS < (as_of - r[0]).days <= 2 * REJECT_DAYS]
            if len(recent) < MIN_REJECTS or len(recent) <= len(before):
                continue
            label = next((r[2] if axis == "item" else r[4] for r in recent
                          if (r[2] if axis == "item" else r[4])), None)
            name = str(label["value"]) if label else f"this {axis}"
            counts[f"trend_{axis}"] += 1
            made = gap(
                REJECT_TREND, HOLDS, key, "Product" if axis == "item" else "Supplier",
                f"rejects|{axis}",
                summary=f"{len(recent)} rejects for {axis} {name} in the last {REJECT_DAYS} "
                        f"days, up from {len(before)} in the {REJECT_DAYS} days before",
                missing="Find the cause with QC" + (" and raise it with the supplier"
                                                    if axis == "supplier" else ""),
                why="Batches or lots rejected by QC, each dated by its receipt or its "
                    "earliest allocated order and cited, the earlier window's too.",
                priority="high" if len(recent) >= 2 * MIN_REJECTS else "medium",
                about=(LOT if axis == "supplier" else BATCH, "qc_status"),
                # each reject's status and its day are their own groups: a reader who
                # may not open the rejected record never sees the trend
                groups=[g for r in (*recent, *before) for g in (
                    cites([x for x in r[5] if x]), cites(r[6]))]
                + ([cites([label])] if label else []),
                event={"days": REJECT_DAYS, "rejects": len(recent), "before": len(before)})
            if made is None:
                continue
            made["follow_up"]["notify"] = {"role": "quality_control",
                                           "status": "draft_not_sent",
                                           "text": f"Rejects are rising for {axis} {name}."}
            if axis == "supplier":
                made["follow_up"]["draft"] = {
                    "kind": "supplier_quality_complaint", "status": "draft_not_sent",
                    "sent": False, "to": "supplier",
                    "subject": f"Quality rejects: {len(recent)} lots in {REJECT_DAYS} days",
                    "body": f"Our QC rejected {len(recent)} of your lots received in the last "
                            f"{REJECT_DAYS} days (lots "
                            + ", ".join(str(x["value"]) for r in recent for x in r[5][1:2]
                                        if x) + "). Please tell us the cause and the "
                            "corrective action, and how you will credit or replace them.",
                    "citations": sorted({c[0] for g in made["groups"] for c in g})}
            gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"batches_held": counts["batches_held"], "lots_held": counts["lots_held"],
                     "aging": len(holds), "rejects": len(rejects),
                     "trend_item": counts["trend_item"],
                     "trend_supplier": counts["trend_supplier"]}
    return out
