"""Evidence trail: the chain of custody behind an answer, as ordered, cited steps.

Vince's ask: beyond "late", show how it was determined and what effort there was: the
requested-by date and who set it, the PO issued, each supplier promise (the confirmation
that stipulated it), revisions, follow-up mail and Teams messages, receipts, then the
verdict with its rule in one sentence, whether the ETA is backed by a vendor
confirmation, and a suggested next step.

Every step is built from a cited value a generic tool returned **as the reader** (``call``
is the reader's tool table: ``plx.process``, ``plx.object``, ``plx.around``,
``plx.timeline``), never from free text: a step carries the value's own citation
(``handle``) and how it was checked (``check``). The verdict is ``plx.process``
``po_delivery_window``'s own lateness entry (``operating.process_lateness``), never
re-decided here. Nothing is computed beyond counting days between two cited dates.

* ``po_trail``: one purchase order's trail.
* ``materials``: a customer order, assembly or manufacturing order, or customer, walked to
  the purchase orders of the materials it consumes (``operating.impact_chain``'s firm links
  in reverse), each PO with its lateness verdict; where a link is not recorded the walk
  states the typed gap (``impact_chain.BREAKS``), never a guess.
* ``claims_trail``: the dated, gate-passed claims of an answer as steps.
* ``next_action``: a suggestion derived only from a trail's facts. Nothing is sent or
  written anywhere.

Permissions: the tools trim what the reader may not read before anything is counted;
what they say they withheld (``withheld`` counts, a withheld promise) is counted in
``hidden`` and never named. The app (``operator_api.backend``) decides every step's
handle again for the reader at its boundary and counts a refused one the same way.
Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from operating import impact_chain as ic

PROCESS = "po_delivery_window"
_GRAPH = Path(__file__).resolve().parent.parent / "ontology" / "schema" / "process" / \
    f"{PROCESS}.json"
FIRM = ic.FIRM_TIERS
MAX_TWINS = 4
MAX_MESSAGES = 8           # follow-up messages shown per PO (newest kept)
MAX_MESSAGE_ROOTS = 3      # the PO and its first twins whose messages are read
MAX_CONFIRMATIONS = 20
MAX_STEPS = 40
MAX_TEXT = 240
MAX_ORDERS = 5             # customer orders a materials walk starts from
MAX_ASSEMBLIES = 6
MAX_MATERIAL_POS = 60      # lateness verdicts one materials walk asks for
MAX_ROWS = 40              # rows the app shows (materials() returns every row)
MAX_PO_TRAILS = 3          # full trails in one materials answer
STALE_CONFIRMATION_DAYS = 7
# Step kinds in the order a PO trail states them (within a kind: by date, undated last).
KINDS = ("due_date", "po_issued", "confirmation", "revision", "lead", "follow_up",
         "shipped", "receipt", "verdict", "claim")
# ETA states: is the supplier's date backed by a confirmation the reader can see?
ETA_STATES = ("confirmed", "promises_disagree", "lead_only", "no_confirmation",
              "not_determined",
              "no_confirmation_found", "withheld", "not_applicable")
# The ontology's message object types (the follow-ups): mention links from them.
MESSAGE_TYPES = ("Email", "TeamsChatMessage", "TeamsChannelMessage")
MESSAGE_DATE = {"Email": ("sent_at", "received_at"), "TeamsChatMessage": ("created_at",),
                "TeamsChannelMessage": ("created_at",)}
ISSUE_EVENTS = ("PurchaseOrder.issued", "PurchaseOrder.sage_ordered",
                "PurchaseOrder.approved")
SHIPPED_EVENTS = ("PurchaseOrder.vendor_shipped",)
PERMISSION_REASONS = frozenset({"withheld_by_permission", "marking_withheld"})
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


class TrailCut(Exception):
    """The trail's time budget ran out: what was read stands, with a gap."""


# --------------------------------------------------------------------------- helpers

def _graph_props() -> dict[str, frozenset[str]]:
    """The receipt stage's lateness declaration as property names: the due-date tiers
    (precedence), the promise events and the receipt events (one place declares them)."""
    doc = json.loads(_GRAPH.read_text(encoding="utf-8"))
    spec = next(s["due"]["lateness"] for s in doc["stages"]
                if "lateness" in s.get("due", {}))
    name = lambda event: event.split(".", 1)[1]  # noqa: E731
    return {"due": frozenset(name(e) for tier in spec["precedence"] for e in tier),
            "promised": frozenset(name(e) for e in spec["promised"]["event_types"]),
            "received": frozenset(name(e) for e in spec["received"]["event_types"])}


PROPS = _graph_props()


def text_of(value: Any) -> str | None:
    if isinstance(value, Mapping):
        if "untrusted_text" in value:
            return str(value["untrusted_text"])[:MAX_TEXT]
        return None
    if value is None or isinstance(value, bool):
        return None
    return str(value)[:MAX_TEXT]


def day_of(value: Any) -> str | None:
    text = text_of(value)
    if text and _DAY.match(text):
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None
    return None


def handle_of(value: Mapping[str, Any]) -> str | None:
    cite = value.get("citation")
    handle = cite.get("l1_handle") if isinstance(cite, Mapping) else cite
    return handle if isinstance(handle, str) else None


def _days(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def _ok(result: Any) -> bool:
    return isinstance(result, Mapping) and "error" not in result


def _withheld_count(result: Mapping[str, Any]) -> int | None:
    """0, or None when the tool says this reader's grants may hide part of what it read
    (``completeness`` not complete for a permission): the tools trim before counting, so
    such a part is never counted or named, only flagged."""
    done = result.get("completeness") if isinstance(result, Mapping) else None
    if isinstance(done, Mapping) and done.get("complete") is False and done.get(
            "reason") in PERMISSION_REASONS:
        return None
    return 0


def step(kind: str, title: str, *, at: str | None = None, handle: str | None = None,
         check: str = "read_from_record", detail: str | None = None,
         **extra: Any) -> dict[str, Any]:
    out = {"kind": kind, "at": at, "title": title[:MAX_TEXT], "handle": handle,
           "check": check}
    if detail:
        out["detail"] = detail[:MAX_TEXT]
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def ordered(steps: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Steps by kind (``KINDS``), then date (undated last), keeping their read order."""
    indexed = list(enumerate(steps))
    indexed.sort(key=lambda p: (KINDS.index(p[1]["kind"]), p[1].get("at") is None,
                                p[1].get("at") or "", p[0]))
    return [dict(s) for _, s in indexed]


class _Budget:
    def __init__(self, seconds: float | None) -> None:
        self.deadline = None if seconds is None else time.monotonic() + seconds

    def check(self) -> None:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise TrailCut()


# --------------------------------------------------------------------------- PO trail

def po_trail(call: Callable[[str, Mapping[str, Any]], Any], oid: str, *,
             seconds: float | None = 30.0, label: str | None = None) -> dict[str, Any]:
    """One purchase order's evidence trail, read through ``call`` as the reader.

    Returns ``{"subject", "label", "state", "late", "days_late", "verdict", "eta",
    "steps", "hidden", "gaps", "next_action"}``; ``hidden`` is the number of steps the
    tools say they withheld from this reader (a withheld promise or receipt) and
    ``may_hide`` whether a tool says this reader's grants may hide part of what it read
    (never counted or named: the tools trim before counting)."""
    budget = _Budget(seconds)
    out: dict[str, Any] = {"subject": oid, "label": label, "state": None, "late": None,
                           "days_late": None, "verdict": None, "eta": None, "steps": [],
                           "hidden": 0, "may_hide": False, "gaps": [],
                           "next_action": None}
    hidden: list[int | None] = []
    proc = call("plx.process", {"from": oid, "process": PROCESS})
    entry = next((e for e in (proc.get("lateness") or ()) if isinstance(e, Mapping)
                  and e.get("root") == oid), None) if _ok(proc) else None
    if entry is None:
        out["gaps"].append({"reason": "lateness_unavailable",
                            "what": "Whether this purchase order is late",
                            "where": "plx.process po_delivery_window did not state a "
                                     "lateness entry for this record"})
        return out
    today = str((proc.get("meta") or {}).get("as_known_at") or "")[:10] or None
    steps: list[dict[str, Any]] = []
    values = [v for v in entry.get("values") or () if isinstance(v, Mapping)]

    # ---- the requested-by date and who set it (every cited due date, governing first)
    governing = entry.get("governing_date")
    top = min((v["authority_rank"] for v in values if v.get("property") in PROPS["due"]
               and isinstance(v.get("authority_rank"), int)), default=None)
    seen_due: set[tuple[str, str]] = set()
    for v in values:
        prop, day = v.get("property"), day_of(v.get("value"))
        if day is None or prop in PROPS["promised"] or prop in PROPS["received"] \
                or (prop, day) in seen_due:
            continue   # a promise or receipt is its own step; one step per date per field
        seen_due.add((prop, day))
        if prop in PROPS["due"]:
            governs = day == governing and v.get("authority_rank") == top
            steps.append(step("due_date", f"Requested by {day}", handle=handle_of(v),
                              date=day, governs=bool(governs),
                              detail=("the governing requested-by date" if governs else
                                      "another system's requested-by date (lower "
                                      "precedence)")))
        else:   # a line's expected date (it follows the supplier's promise when one is set)
            steps.append(step("due_date", f"Expected on a line by {day}",
                              handle=handle_of(v), date=day, governs=day == governing,
                              detail="a line's expected receipt date: the due date only "
                                     "when no header records a requested-by date"))

    # ---- the PO's own record events and its twins (issued, shipped), as the reader
    members = [oid]
    card: Any = None
    confirmations: dict[str, str] = {}
    read: list[tuple[str | None, str, str]] = []
    read_complete = False
    try:
        card = call("plx.object", {"object_id": oid})
        if _ok(card):
            for edge in card.get("identity") or ():
                other = edge.get("to") if edge.get("from") == oid else edge.get("from")
                if edge.get("tier") in FIRM and isinstance(other, str) and other not in members:
                    members.append(other)
            hidden.append(_withheld_count(card))
            if out["label"] is None:
                out["label"] = text_of((card.get("object") or {}).get("display"))
        members = members[:1 + MAX_TWINS]
        seen_events: set[tuple[str, str]] = set()
        for member in members:
            budget.check()
            got = call("plx.timeline", {"from": member,
                                        "event_types": list(ISSUE_EVENTS + SHIPPED_EVENTS),
                                        "limit": 20})
            if not _ok(got):
                continue
            hidden.append(_withheld_count(got))
            for event in got.get("events") or ():
                day = day_of(event.get("time"))
                kind = "shipped" if event.get("event_type") in SHIPPED_EVENTS else "po_issued"
                key = (kind, day or "")
                if day is None or key in seen_events:
                    continue
                seen_events.add(key)
                title = ("Vendor marked the PO shipped" if kind == "shipped" else
                         "PO approved" if event["event_type"].endswith(".approved") else
                         "PO issued")
                steps.append(step(kind, title, at=day, handle=handle_of(event)))

        # ---- when each supplier confirmation was issued (its own cited document date)
        dates, read, read_complete = _confirmations(call, proc, budget)
        confirmations.update(dates)
        if "confirmation_lead_only" in (entry.get("reasons") or ()):
            steps += _confirmation_leads(call, members[:1], budget, hidden)

        # ---- follow-ups: mail and Teams messages naming the PO (or a twin), newest last
        steps += _follow_ups(call, members, budget, hidden)
    except TrailCut:
        out["gaps"].append({"reason": "trail_cut", "what": "Part of this trail",
                            "where": "the trail's time limit was reached while reading the "
                                     "records and messages about this purchase order"})

    issued = min((s["at"] for s in steps if s["kind"] == "po_issued"), default=None)
    # ---- supplier promises, in their documents' order: a later differing one revises
    promised, promises_whole = promise_steps(entry, values, read, read_complete,
                                             confirmations)
    steps += promised
    if "promises_disagree" in (entry.get("reasons") or ()):
        out["gaps"].append({"reason": "dates_disagree", "what": "The vendor's delivery date",
                            "where": "the vendor's documents state different dates and none "
                                     "is later than the others"})
    elif not promises_whole:
        out["gaps"].append({"reason": "dates_cut", "what": "Every vendor promise date",
                            "where": "this purchase order has more dates than were read, so "
                                     "the promise history is not complete here"})
    for gap in entry.get("gaps") or ():
        if gap.get("reason") == "withheld" or gap.get("state") == "inaccessible":
            hidden.append(1)   # a promise or receipt the reader may not read: counted

    # ---- receipts and lines
    for v in values:
        if v.get("property") in PROPS["received"] and day_of(v.get("value")):
            steps.append(step("receipt", "Goods received", at=day_of(v["value"]),
                              handle=handle_of(v)))
    for line in entry.get("lines") or ():
        if line.get("state") != "partial":
            continue
        got = {x.get("quantity_role"): x for x in line.get("values") or ()
               if isinstance(x, Mapping)}
        o, r = got.get("ordered"), got.get("received")
        if o and r and o.get("unit") == r.get("unit"):
            unit = o.get("unit") or ""
            steps.append(step("receipt", f"Partial receipt: {text_of(r['value'])} of "
                                         f"{text_of(o['value'])} {unit}".strip(),
                              handle=handle_of(r)))

    # ---- the verdict, with its rule
    verdict, rule = verdict_sentence(entry)
    because = [s["handle"] for s in steps if s["kind"] == "due_date" and s.get("governs")
               and s.get("handle")][:1]
    steps.append(step("verdict", verdict, at=entry.get("measured_on"),
                      handle=because[0] if because else None,
                      check="computed_from_cited_records", detail=rule))
    out.update(state=entry.get("state"), late=entry.get("late"),
               measured_against=entry.get("measured_against"),
               days_late=entry.get("days_late"), verdict=verdict,
               at_risk=entry.get("at_risk"), today=today)
    out["eta"] = eta_check(entry, steps, promises_whole=promises_whole)
    out["steps"] = ordered(steps)[:MAX_STEPS]
    out["hidden"] = sum(h for h in hidden if h)
    out["may_hide"] = any(h is None for h in hidden)
    if entry.get("gap") and entry.get("state") == "undetermined":
        gap = entry["gap"]
        out["gaps"].append({"reason": str(gap.get("reason")),
                            "what": "Whether this purchase order is late",
                            "where": str(gap.get("detail") or gap.get("reason"))[:300]})
    out["next_action"] = next_action(out, issued=issued)
    return out


def is_settled(entry: Mapping[str, Any]) -> bool:
    """Received (on time or late) or not issued, cancelled or closed: nothing to chase."""
    return entry.get("state") in ("on_time", "not_applicable") or (
        entry.get("state") == "late" and entry.get("measured_against") == "received")


def verdict_sentence(entry: Mapping[str, Any]) -> tuple[str, str]:
    """The lateness entry's verdict in one plain sentence, and the rule applied."""
    state, gd = entry.get("state"), entry.get("governing_date")
    on, days = entry.get("measured_on"), entry.get("days_late")
    against = entry.get("measured_against")
    due = f"the requested-by date {gd}" if gd else "every requested-by date on record"
    rule = ("Rule: an open purchase order is late once today is past its requested-by "
            "date; a received one is late when it was received after it. A supplier "
            "promise never replaces the requested-by date.")
    if state == "late" and against == "received":
        return (f"Late: received on {on}, {days} days after {due}.", rule)
    if state == "late":
        return (f"Late: open on {on}, past {due} ({days} days).", rule)
    if state == "on_time":
        return (f"On time: received on {on}, by {due}.", rule)
    if state == "not_due":
        text = f"Not late yet: open on {on}, due by {due[4:] if gd else 'its date'}."
        if entry.get("at_risk"):
            latest = entry.get("revised_date") or entry.get("promised_date")
            text += f" At risk: the supplier's latest promise ({latest}) is after it."
        return (text, rule)
    if state == "not_applicable":
        return ("Not late: the purchase order is not issued, cancelled or closed, so "
                "nothing is due.", rule)
    gap = entry.get("gap") or {}
    return ("Lateness is not stated: " + str(gap.get("detail") or gap.get("reason")
                                             or "a date it needs is not proven")[:200]
            .rstrip(".") + ".", rule)


def eta_check(entry: Mapping[str, Any], steps: Sequence[Mapping[str, Any]], *,
              promises_whole: bool = True) -> dict[str, Any] | None:
    """Whether the ETA is backed by a vendor confirmation the reader can see, decided by
    the lateness entry's own fields and reasons (never by a cut date list): the latest
    promise's (or the lead's) source. A promise history not read whole is not decided."""
    reasons = set(entry.get("reasons") or ())
    promises = [s for s in steps if s["kind"] in ("confirmation", "revision")]
    leads = [s for s in steps if s["kind"] == "lead"]
    withheld = any(g.get("reason") == "withheld" and "promised" in str(g.get("subject"))
                   for g in entry.get("gaps") or ())
    latest = entry.get("revised_date") or entry.get("promised_date")
    if entry.get("state") == "not_applicable":
        return {"state": "not_applicable", "text": "No ETA is needed: nothing is due."}
    if (entry.get("gap") or {}).get("reason") in ("status_unknown", "status_uncited"):
        return None   # the promises were not read: nothing is said about them
    if "promises_disagree" in reasons:
        return {"state": "promises_disagree",
                "text": "Vendor documents state different dates and none is later than "
                        "the others: the ETA is not settled.",
                "handle": promises[-1]["handle"] if promises else None}
    if latest is not None:
        if not promises_whole or not promises or promises[-1]["date"] != latest:
            return {"state": "not_determined",
                    "text": "Not determined: this purchase order has more dates than were "
                            "read, so the vendor's confirmed date is not decided here."}
        moved = len([s for s in promises if s["kind"] == "revision"])
        text = f"Yes: the vendor confirmed {latest}"
        text += (f" (revised {moved} time{'s' if moved != 1 else ''})" if moved else "")
        return {"state": "confirmed", "text": text + ".", "handle": promises[-1]["handle"],
                "date": latest}
    if "confirmation_lead_only" in reasons:
        return {"state": "lead_only",
                "text": "Lead only: a document that looks like a confirmation names this "
                        "PO, but its link to the PO is not confirmed, so its date does "
                        "not count.", "handle": leads[0]["handle"] if leads else None}
    if withheld:
        return {"state": "withheld", "text": "A supplier promise exists that you can't "
                                             "open, so the ETA is not shown."}
    if "no_confirmation" in reasons:
        return {"state": "no_confirmation",
                "text": "No: there is no vendor confirmation on record for this PO."}
    if "no_confirmation_found" in reasons:
        return {"state": "no_confirmation_found",
                "text": "No vendor confirmation found in what you can see (mail and files "
                        "you can't open could still hold one)."}
    return {"state": "not_determined",
            "text": "Not determined: the record does not say whether the vendor confirmed "
                    "a date."}


def _confirmations(call: Callable, proc: Mapping[str, Any], budget: _Budget
                   ) -> tuple[dict[str, str], list[tuple[str | None, str, str]], bool]:
    """The confirmations the supplier_confirmation stage reached, each read on its own
    record: handle -> its document date, every promise event it states ``(document
    date, promised day, handle)`` in document order, and whether all were read."""
    dates: dict[str, str] = {}
    promises: list[tuple[str | None, str, str]] = []
    stage = next((s for s in proc.get("stages") or () if s.get("stage") ==
                  "supplier_confirmation"), {})
    ids = [e for e in stage.get("events") or () if isinstance(e, str)]
    complete = len(ids) <= MAX_CONFIRMATIONS
    for cid in ids[:MAX_CONFIRMATIONS]:
        budget.check()
        card = call("plx.object", {"object_id": cid})
        if not _ok(card):
            complete = False
            continue
        rows = [v for v in card.get("values") or () if isinstance(v, Mapping)]
        sent = next((day_of(v.get("value")) for v in rows
                     if v.get("property") == "document_date" and day_of(v.get("value"))),
                    None)
        got = call("plx.timeline", {"from": cid, "event_types": [
            f"SupplierConfirmation.{p}" for p in sorted(PROPS["promised"])], "limit": 20})
        if not _ok(got) or (got.get("completeness") or {}).get("complete") is False:
            complete = False
        for event in (got.get("events") or ()) if _ok(got) else ():
            day, handle = day_of(event.get("time")), handle_of(event)
            if day and handle:
                promises.append((sent, day, handle))
                if sent:
                    dates.setdefault(handle, sent)
        if sent:
            for v in rows:
                if handle_of(v):
                    dates.setdefault(handle_of(v), sent)
    promises.sort(key=lambda p: (p[0] is None, p[0] or ""))
    return dates, promises, complete


def promise_steps(entry: Mapping[str, Any], values: Sequence[Mapping[str, Any]],
                  read: Sequence[tuple[str | None, str, str]], read_complete: bool,
                  dates: Mapping[str, str]) -> tuple[list[dict[str, Any]], bool]:
    """The confirmation and revision steps, and whether they are the whole promise list.
    The lateness entry's own ``promised_date`` (first) and ``revised_date`` (latest) are
    the authority; its date list can be cut (``values_truncated``), so when it does not
    reach the latest promise each confirmation read on its own record is used instead,
    and only when it starts and ends where the entry says."""
    first = entry.get("promised_date")
    latest = entry.get("revised_date") or first
    listed = [(dates.get(handle_of(v) or ""), day_of(v["value"]), handle_of(v))
              for v in values if v.get("property") in PROPS["promised"]
              and day_of(v.get("value")) and handle_of(v)]
    cut = any(g.get("reason") == "values_truncated" for g in entry.get("gaps") or ())

    def whole(chain: Sequence[tuple]) -> bool:
        return bool(chain) and chain[0][1] == first and chain[-1][1] == latest

    if "promises_disagree" in (entry.get("reasons") or ()):
        # documents of one date that state different promises: each is shown as its own
        # statement, none is a revision of another (the gap says they disagree)
        chain = listed or list(read)
        out = [step("confirmation", f"A vendor document states delivery by {day}",
                    at=sent, handle=handle, date=day) for sent, day, handle in chain]
        return out, True
    if first is None:
        chain, complete = [], not listed
    elif whole(listed) and not cut:
        chain, complete = listed, True
    elif read and whole(read) and read_complete:
        chain, complete = list(read), True
    else:
        chain, complete = (listed if whole(listed) else read or listed), False
    out: list[dict[str, Any]] = []
    previous = None
    for sent, day, handle in chain:
        if previous is None:
            out.append(step("confirmation", f"Vendor confirmed delivery by {day}",
                            at=sent, handle=handle, date=day))
        elif day != previous:
            out.append(step("revision", f"Promise revised from {previous} to {day}",
                            at=sent, handle=handle, date=day))
        previous = day
    return out, complete


def _confirmation_leads(call: Callable, members: Sequence[str], budget: _Budget,
                        hidden: list[int | None]) -> list[dict[str, Any]]:
    """The lead-tier confirmations naming the PO (``SupplierConfirmation.confirms``
    below the tier floor): shown as leads with their own cited values, never counted."""
    out = []
    for member in members:
        budget.check()
        got = call("plx.around", {"from": member, "path": [
            {"link_type": "SupplierConfirmation.confirms", "direction": "reverse"}],
            "tier_floor": "lead", "limit": 5})
        if not _ok(got):
            continue
        hidden.append(_withheld_count(got))
        for obj in (got.get("objects") or ())[:3]:
            card = call("plx.object", {"object_id": obj["object_id"]})
            if not _ok(card):
                continue
            rows = [v for v in card.get("values") or () if isinstance(v, Mapping)
                    and handle_of(v)]
            sent = next((day_of(v.get("value")) for v in rows
                         if v.get("property") == "document_date"), None)
            if rows:
                out.append(step("lead", "A document that looks like a vendor confirmation "
                                        "names this PO (unconfirmed link)", at=sent,
                                handle=handle_of(rows[0]), check="lead"))
    return out


def _follow_ups(call: Callable, members: Sequence[str], budget: _Budget,
                hidden: list[int | None]) -> list[dict[str, Any]]:
    """Mail and Teams messages that name the PO or a twin (stored mention links, a lead:
    a machine-read reference), each dated and cited by its own record, newest kept."""
    found: list[tuple[str, str]] = []   # (message object id, type)
    for member in members[:MAX_MESSAGE_ROOTS]:
        for mtype in MESSAGE_TYPES:
            # asked even when the record lists no such link: linked_counts shows only what
            # this reader sees, and a path that may hide some must say so (may_hide)
            budget.check()
            link = f"{mtype}.mentions_{_snake(ic._type_of(member))}"
            got = call("plx.around", {"from": member, "path": [
                {"link_type": link, "direction": "reverse"}], "tier_floor": "lead",
                "limit": 50})
            if not _ok(got):
                continue
            hidden.append(_withheld_count(got))
            found += [(o["object_id"], mtype) for o in got.get("objects") or ()
                      if isinstance(o.get("object_id"), str)]
    found = list(dict.fromkeys(found))
    read: list[dict[str, Any]] = []
    copies: set[str] = set()
    for message, mtype in found[:4 * MAX_MESSAGES]:
        budget.check()
        props = list(MESSAGE_DATE[mtype]) + ["subject"] + (
            ["sender", "mailbox", "internet_message_id"] if mtype == "Email" else [])
        card = call("plx.object", {"object_id": message, "properties": props})
        if not _ok(card):
            continue
        rows = {v.get("property"): v for v in card.get("values") or ()
                if isinstance(v, Mapping)}
        dated = next((rows[p] for p in MESSAGE_DATE[mtype]
                      if p in rows and day_of(rows[p].get("value"))), None)
        if dated is None:
            continue
        copy = text_of((rows.get("internet_message_id") or {}).get("value"))
        if copy:
            if copy in copies:
                continue   # one message in several mailboxes counts once
            copies.add(copy)
        subject = text_of((rows.get("subject") or {}).get("value"))
        sender = text_of((rows.get("sender") or {}).get("value"))
        side = direction(sender, text_of((rows.get("mailbox") or {}).get("value")))
        where = "Teams message" if mtype != "Email" else (
            "Email we sent" if side == "internal" else
            "Email from the vendor side" if side == "external" else "Email")
        title = f"{where}: {subject}" if subject else f"{where} naming this PO"
        read.append(step("follow_up", title, at=(text_of(dated["value"]) or "")[:19] or None,
                         handle=handle_of(dated), check="lead",
                         sender=sender if mtype == "Email" else None,
                         direction=side if side in ("internal", "external") else None,
                         channel="email" if mtype == "Email" else "teams"))
    read.sort(key=lambda s: s.get("at") or "")
    if len(read) > MAX_MESSAGES:
        read = read[-MAX_MESSAGES:]
    return read


_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def address_of(text: str | None) -> str | None:
    found = _ADDRESS.search(text or "")
    return found.group(0).lower() if found else None


def direction(sender: str | None, mailbox: str | None) -> str | None:
    """``internal`` when the sender's address is at the captured mailbox's domain (we
    sent it), ``external`` when at another domain (the vendor side), else None."""
    a, b = address_of(sender), address_of(mailbox)
    if a is None or b is None:
        return None
    return "internal" if a.split("@")[1] == b.split("@")[1] else "external"


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# --------------------------------------------------------------------------- next action

def next_action(trail: Mapping[str, Any], *, issued: str | None = None
                ) -> dict[str, Any] | None:
    """A suggested next step from the trail's own facts (its steps, verdict and ETA
    check), first rule that applies. A suggestion only: nothing is sent or written.
    ``contact`` is the sender of the vendor-side message the step cites (the app shows
    it only when the reader may open that message)."""
    state, eta = trail.get("state"), trail.get("eta") or {}
    if state not in ("late", "not_due") or (state == "not_due" and not trail.get("at_risk")) \
            or trail.get("measured_against") == "received":
        return None   # received (late or not), on time, not due, undetermined: no chase
    if eta.get("state") == "not_determined":
        return None   # the promise history was not read whole: nothing to suggest on it
    steps = trail.get("steps") or ()
    today = trail.get("today")
    revisions = [s for s in steps if s["kind"] == "revision"]
    vendor = [s for s in steps if s["kind"] == "follow_up" and s.get("direction") ==
              "external" and s.get("sender")]
    contact = vendor[-1] if vendor else None
    who = "the vendor"
    last_message = max((s["at"] for s in steps if s["kind"] == "follow_up" and s.get("at")),
                       default=None)
    since_msg = _days(last_message, today)
    tail = (f" The last message about it was {since_msg} days ago." if since_msg
            is not None and since_msg > 0 else "")

    def out(rule: str, text: str) -> dict[str, Any]:
        return {"rule": rule, "text": text + tail, "contact": contact["sender"]
                if contact else None, "contact_handle": contact["handle"] if contact else None,
                "suggestion": True}

    if len(revisions) >= 2:
        return out("promise_moved", f"The promise moved {len(revisions)} times: confirm the "
                                    f"new date ({revisions[-1]['date']}) with {who}.")
    if eta.get("state") == "confirmed" and state == "late" and eta.get("date") and today \
            and eta["date"] < today and not any(s["kind"] == "receipt" for s in steps):
        return out("promise_passed", f"The vendor's confirmed date ({eta['date']}) has passed "
                                     f"with nothing received: ask {who} for a new date.")
    if eta.get("state") in ("no_confirmation", "no_confirmation_found"):
        days = _days(issued, today)
        lead = (f"No vendor confirmation in {days} days since the PO was issued"
                if days is not None and days >= STALE_CONFIRMATION_DAYS
                else "No vendor confirmation on record")
        return out("no_confirmation", f"{lead}: ask {who} to confirm the delivery date.")
    if eta.get("state") == "lead_only":
        return out("confirmation_lead_only", "A document that looks like the vendor's "
                                             "confirmation is not linked for sure: open it "
                                             "and confirm it belongs to this PO.")
    if eta.get("state") == "promises_disagree":
        return out("promises_disagree", f"The vendor's documents disagree on the date: ask "
                                        f"{who} which date stands.")
    if len(revisions) == 1:
        return out("promise_moved", f"The promise moved once, to {revisions[-1]['date']}: "
                                    f"confirm it still holds with {who}.")
    if any(s["kind"] == "receipt" and s["title"].startswith("Partial") for s in steps):
        return out("partial_receipt", f"Part of the order is received: ask {who} when the "
                                      "rest ships.")
    if eta.get("state") == "confirmed":
        return out("confirmed_late", f"The vendor confirmed {eta.get('date')}, after the "
                                     f"requested-by date: confirm it still holds with {who}.")
    return None


# --------------------------------------------------------------------------- claims trail

def claims_trail(claims: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The gate-passed claims of an answer that carry a dated value, as steps by date
    (each its claim's text, first citation and the gate's check)."""
    out = []
    for claim in claims:
        cites = [c for c in claim.get("citations") or () if isinstance(c, str)]
        day = next((day_of(v.get("value")) for v in claim.get("value_claims") or ()
                    if isinstance(v, Mapping) and day_of(v.get("value"))), None)
        if not cites or day is None:
            continue
        check = (claim.get("check") or {}).get("status") if isinstance(
            claim.get("check"), Mapping) else None
        out.append(step("claim", str(claim.get("text") or "")[:MAX_TEXT], at=day,
                        handle=cites[0], check=check or "gate_passed"))
    return sorted(out, key=lambda s: s["at"])[:MAX_STEPS]


# --------------------------------------------------------------------------- materials

ORDER_TO_ASSEMBLY = (
    ("allocation", [{"link_type": ic.ORDER_LINE, "direction": "reverse"},
                    {"link_type": ic.ALLOCATION_LINE, "direction": "reverse"},
                    {"link_type": ic.ALLOCATES, "direction": "forward"}]),
    ("order_line", [{"link_type": ic.ORDER_LINE, "direction": "reverse"},
                    {"link_type": ic.AO_LINE, "direction": "reverse"}]))
ASSEMBLY_TO_ITEM = [{"link_type": ic.COMPONENT_OF, "direction": "reverse"},
                    {"link_type": ic.USES_ITEM, "direction": "forward"}]
ITEM_TO_PO = [{"link_type": ic.LINE_ITEM, "direction": "reverse"},
              {"link_type": ic.PO_LINE, "direction": "forward"}]
CUSTOMER_ORDERS = [{"link_type": "SalesOrder.ordered_by", "direction": "reverse"}]
START_TYPES = ("SalesOrder", "AssemblyOrder", "Customer")


MAX_AROUND_PAGES = 10


def _around(call: Callable, root: str, path: list, hidden: list[int | None],
            limit: int = 50) -> Mapping[str, Any] | None:
    """One walk as the reader, every page of it (``completeness.next_cursor``): objects
    and edges of all pages together, so no reached record is lost to a page. Past
    ``MAX_AROUND_PAGES`` the answer stays marked truncated."""
    got = call("plx.around", {"from": root, "path": path, "tier_floor": "corroborated",
                              "limit": limit})
    if not _ok(got):
        return None
    got = dict(got)
    got["objects"], got["edges"] = list(got.get("objects") or ()), list(got.get("edges") or ())
    pages = 1
    cursor = (got.get("completeness") or {}).get("next_cursor")
    while cursor and pages < MAX_AROUND_PAGES:
        more = call("plx.around", {"from": root, "path": path, "tier_floor": "corroborated",
                                   "limit": limit, "cursor": cursor})
        if not _ok(more):
            break
        pages += 1
        got["objects"] += list(more.get("objects") or ())
        got["edges"] += list(more.get("edges") or ())
        got["completeness"] = more.get("completeness") or got["completeness"]
        cursor = (more.get("completeness") or {}).get("next_cursor")
    got["cut"] = bool(cursor)   # pages left unread: the walk says so (never silently)
    hidden.append(_withheld_count(got))
    return got


def _gap(reason: str, subject: str, got: Mapping[str, Any] | None) -> dict[str, Any]:
    """A typed break of the reverse walk (``impact_chain.BREAKS``): absent only when the
    reader's walk was complete; a lead-only link is ``link_lead_only``."""
    types, source = ic.BREAKS[reason]
    complete = bool(got) and (got.get("completeness") or {}).get("complete") is True
    lead = bool(got) and any((h.get("count") or 0) == 0 and any(
        t in ic.LEAD_TIERS and n for t, n in (h.get("tiers") or {}).items())
        for h in got.get("hops") or ())
    if lead:
        reason, source = "link_lead_only", ic.BREAKS["link_lead_only"][1]
    return {"reason": reason, "state": "absent_in_source" if complete and not lead
            else "unknown", "what": subject[:200], "where": source[:300]}


def _labels(got: Mapping[str, Any] | None) -> dict[str, str]:
    return {o["object_id"]: text_of(o.get("display")) or o["object_id"]
            for o in (got or {}).get("objects") or () if isinstance(o.get("object_id"), str)}


def _reached(got: Mapping[str, Any] | None, hop: int) -> bool:
    """Whether hop ``hop`` of a walk reached anything at the floor (the answer's edges
    are its last hop's only)."""
    hops = (got or {}).get("hops") or ()
    return len(hops) > hop and bool(hops[hop].get("count"))


def _edges(got: Mapping[str, Any] | None, link_type: str) -> list[tuple[str, str]]:
    return [(e["from"], e["to"]) for e in (got or {}).get("edges") or ()
            if e.get("link_type") == link_type and e.get("tier") in FIRM]


def _twin_paths(link_type: str, tail: list) -> list[list]:
    return [[{"link_type": link_type, "direction": d}] + tail for d in ("forward", "reverse")]


def materials(call: Callable[[str, Mapping[str, Any]], Any], root: str, root_type: str,
              *, seconds: float | None = 45.0, trails: int = MAX_PO_TRAILS
              ) -> dict[str, Any]:
    """The purchase orders of the materials a customer order (or an assembly or
    manufacturing order, or a customer's orders) consumes, each with its lateness verdict,
    walked over firm recorded links only, as the reader. Rows: one per (assembly order,
    item, PO). A link not recorded is a typed gap (``impact_chain.BREAKS``). The late POs
    (first ``trails``) get their full trail."""
    budget = _Budget(seconds)
    hidden: list[int | None] = []
    gaps: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    labels: dict[str, str] = {}
    verdicts: dict[str, Mapping[str, Any]] = {}
    pos_wanted: list[str] = []
    try:
        orders: list[str] = []
        assemblies: dict[str, str | None] = {}   # assembly -> the order it was reached from
        if root_type == "Customer":
            got = _around(call, root, CUSTOMER_ORDERS, hidden)
            labels.update(_labels(got))
            reached = [o["object_id"] for o in (got or {}).get("objects") or ()]
            orders = reached[:MAX_ORDERS]
            if not reached:
                gaps.append({"reason": "no_customer_order", "state": "unknown",
                             "what": "A customer order of this customer you can see",
                             "where": "the order's customer reference (Portal, FileMaker, "
                                      "Sage or Business Central order header)"})
            elif len(reached) > MAX_ORDERS or (got.get("completeness") or {}).get(
                    "next_cursor"):
                gaps.append({"reason": "orders_truncated", "state": "unknown",
                             "what": "This customer's other orders",
                             "where": f"only {MAX_ORDERS} of its orders were walked: name "
                                      "the order to see its materials"})
        elif root_type == "SalesOrder":
            orders = [root]
        elif root_type == "AssemblyOrder":
            assemblies[root] = None
        for order in orders:
            budget.check()
            last = None
            before = len(assemblies)
            for _name, path in ORDER_TO_ASSEMBLY:
                got = _around(call, order, path, hidden)
                last = got or last
                labels.update(_labels(got))
                for obj in (got or {}).get("objects") or ():
                    if obj.get("object_type") == "AssemblyOrder":
                        assemblies.setdefault(obj["object_id"], order)
            if len(assemblies) == before:
                gaps.append(_gap("assembly_order_not_linked_to_order_line",
                                 f"An assembly or manufacturing order for "
                                 f"{labels.get(order, 'this order')}", last))
        if len(assemblies) > MAX_ASSEMBLIES:
            gaps.append({"reason": "assemblies_truncated", "state": "unknown",
                         "what": f"{len(assemblies) - MAX_ASSEMBLIES} more assembly orders",
                         "where": "not walked: name one order to see all of its materials"})
        for assembly, order in list(assemblies.items())[:MAX_ASSEMBLIES]:
            budget.check()
            got = _around(call, assembly, ASSEMBLY_TO_ITEM, hidden)
            if got is not None and not _reached(got, 0):
                # the components may be recorded on the assembly order's twin (the FM AO
                # of a Portal manufacturing order)
                for path in _twin_paths(ic.AO_TWIN, ASSEMBLY_TO_ITEM):
                    twin = _around(call, assembly, path, hidden)
                    if _reached(twin, 1):
                        got = twin
                        break
            labels.update(_labels(got))
            name = labels.get(assembly, "the assembly order")
            if not (_reached(got, 0) or _reached(got, 1) and len(got["hops"]) == 3):
                gaps.append(_gap("component_without_assembly_order",
                                 f"The components of {name}", got))
                continue
            items = list(dict.fromkeys(to for _f, to in _edges(got, ic.USES_ITEM)))
            if not items:
                gaps.append(_gap("item_not_used_by_assembly_order",
                                 f"The items the components of {name} use", got))
                continue
            for item in items:
                budget.check()
                got = _around(call, item, ITEM_TO_PO, hidden)
                pairs = _edges(got, ic.PO_LINE)
                if not pairs:
                    for path in _twin_paths(ic.ITEM_TWIN, ITEM_TO_PO):
                        twin = _around(call, item, path, hidden)
                        if _edges(twin, ic.PO_LINE):
                            got, pairs = twin, _edges(twin, ic.PO_LINE)
                            break
                labels.update(_labels(got))
                if got is not None and got.get("cut"):
                    gaps.append({"reason": "walk_truncated", "state": "unknown",
                                 "what": f"More purchase orders for "
                                         f"{labels.get(item, 'an item')}",
                                 "where": "more pages of linked purchase order lines than "
                                          "were read"})
                for _line, po in pairs:
                    rows.append({"order": order, "assembly": assembly, "item": item,
                                 "po": po})
                    if po not in pos_wanted:
                        pos_wanted.append(po)
                if not pairs:
                    gaps.append(_gap("po_line_not_recorded" if _reached(got, 0) else
                                     "line_item_not_recorded",
                                     f"A purchase order line for {labels.get(item, 'an item')} "
                                     f"on {name}", got))
        for po in pos_wanted[:MAX_MATERIAL_POS]:
            budget.check()
            proc = call("plx.process", {"from": po, "process": PROCESS})
            entry = next((e for e in (proc.get("lateness") or ()) if e.get("root") == po),
                         None) if _ok(proc) else None
            if entry is not None:
                verdicts[po] = entry
        if len(pos_wanted) > MAX_MATERIAL_POS:
            gaps.append({"reason": "verdicts_truncated", "state": "unknown",
                         "what": f"The lateness of {len(pos_wanted) - MAX_MATERIAL_POS} more "
                                 "purchase orders", "where": "not read: narrow the question"})
    except TrailCut:
        gaps.append({"reason": "trail_cut", "state": "unknown",
                     "what": "The rest of the walk to the purchase orders",
                     "where": "the time limit was reached; what is shown was read in full"})
    unique: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    settled: set[str] = set()
    for row in rows:
        key = (row["item"], row["po"])   # one row per material and PO (first assembly)
        if key in seen:
            continue
        seen.add(key)
        entry = verdicts.get(row["po"])
        if entry is not None and is_settled(entry):
            settled.add(row["po"])   # received or closed: history, counted not listed
            continue
        row["state"] = entry.get("state") if entry else None
        row["days_late"] = entry.get("days_late") if entry else None
        row["verdict"] = verdict_sentence(entry)[0] if entry else None
        if entry is None:
            row["gap"] = "lateness_undetermined"
        unique.append(row)
    rank = {"late": 0, "not_due": 1, "undetermined": 2, None: 3}
    unique.sort(key=lambda r: (rank.get(r["state"], 3), -(r["days_late"] or 0)))
    if settled:
        gaps.append({"reason": "received_or_closed", "state": "note",
                     "what": f"{len(settled)} other purchase order"
                             f"{'s' if len(settled) != 1 else ''} for these materials",
                     "where": "already received, cancelled or closed by their own lateness "
                              "verdicts (not listed)"})
    late_pos = list(dict.fromkeys(r["po"] for r in unique if r["state"] == "late"))
    full = []
    for po in late_pos[:trails]:
        left = None if budget.deadline is None else budget.deadline - time.monotonic()
        if left is not None and left <= 0:
            break
        full.append(po_trail(call, po, seconds=left, label=labels.get(po)))
    return {"root": root, "root_type": root_type, "labels": labels, "rows": unique,
            "trails": full, "gaps": gaps,
            "hidden": sum(h for h in hidden if h), "may_hide": any(h is None for h in hidden),
            "late": len(late_pos), "checked": len(verdicts), "late_pos": late_pos,
            "checked_pos": sorted(verdicts)}
