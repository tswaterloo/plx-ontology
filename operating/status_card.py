"""Status card: what is wrong with an order or a party, why, and what to do next.

A plain-language card computed from the ontology's evidence by rule (no model, no
guess), read through ``call(tool, arguments)`` with the reader's own tools, so it shows
only what that reader's tools answer:

* an order (any process-graph root type: a purchase, sales or assembly order): its
  record and its same-record copies at a tier ``plx.object`` merges (verified or
  reviewed; a lead copy is named once as a match to confirm and read into nothing, a
  contradicted one is ignored), each declared process (``plx.process``) on each copy,
  merged by stage (a stage recorded on any copy is recorded);
* a party (a type orders are placed with and invoices billed to: a supplier, a
  customer): its orders (each one's due-dated process), its balance and its invoices.

At most ``MAX_BLOCKERS`` blockers, ranked by ``KINDS``: ``late`` (the process's lateness
verdict, else for a stage without one its due date, says the stage is late), ``quantity_mismatch`` (a process quantity check disagrees),
``unpaid`` (an invoice past its due date with an amount remaining; basis
``lead`` when the invoice is linked only by a lead), ``not_recorded``
(a required stage, or an optional one a later due-dated stage still waits on, was not
found: never "proven absent"), ``missing_field`` (a field a document-field rule lets
be proposed, ``ontology.document_fields.comparisons``, empty on every copy) and
``no_need_date`` (the stage's due-date fields are empty on every copy, so lateness
cannot be decided). Each blocker carries its citations (``{handle, field}``) and a next
step (``who``: department names, never codes; ``what``). Facts are cited source values
(source text stays in ``value``, outside the card's own words); copies in other systems
are one line; every unknown is stated once, in plain words. ``readable(handles)`` (the
console's access decision) drops citations the reader may not open, and any claim left
uncited, with one unknown saying so.

``python3 -m operating.status_card`` is the live check's command
(``verify/live-checks/case.po_status.json``): one host capture cycle into a state
directory, then cards for up to ``--limit`` records of ``--object-type`` offline as
``operating.proactive_gaps`` does; counts only, never an identifier or a value. Evidence
label: that of the captures it reads (live-unverified until the verifier runs it).
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import functools
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import answerers
from .explanations import field_words, reason_words, type_words

SCHEMA = "plx-status-card/v1"
KINDS = ("late", "quantity_mismatch", "unpaid", "not_recorded", "missing_field",
         "no_need_date")
MAX_BLOCKERS = 5
MAX_COPIES = 3        # same-record copies read besides the record
MAX_ORDERS = 20       # a party's orders read
MAX_INVOICES = 10     # invoice reads per card
MAX_FACTS = 10
DUE_PROPERTY = "due_date"           # an invoice's due date (schema vocabulary)
OPEN_AMOUNT = "remaining_amount"    # the amount an invoice still has open
PRESENT = frozenset({"present", "out_of_order"})
NOT_FOUND = frozenset({"absence_unproven", "absent_in_source", "no_match", "no_event",
                       "missing_from_source"})
# A stage whose record is held but whose status no definition decides (``po_status``).
UNDECIDED_STATUS = frozenset({"status_unreadable"})
WITHHELD = frozenset({"withheld_by_permission", "marking_withheld", "audience_not_granted",
                      "policy_changed_since_handle", "partial_visibility",
                      "evidence_not_fully_visible", "undecided_by_withheld_value",
                      "no_visible_match"})
LINEAGE_SYSTEMS = {"fm_bronze": "FileMaker", "sage_bronze": "Sage", "silver_view": "Portal",
                   "gold_etl_from_fm": "Portal", "gold_etl_from_sage": "Portal",
                   "gold_native_staging": "Portal", "mail": "Mail",
                   "sharepoint": "SharePoint", "original": "an attached original",
                   "workbench": "the Workbench", "mission_control": "Mission Control",
                   "brain": "the knowledge base"}
PLAN_DIR = Path(__file__).resolve().parents[1] / "ontology" / "plans"
WITHHELD_TEXT = "Some of the evidence is outside your access, so it is not shown here."
UNCITED_TEXT = "Some of what was read carries no source to cite, so it is not known here."
# A party's line for one order rule seen on several orders; ``{names}`` names them.
ROLLUP_WHY = {
    "late": "Late against its due date for {names}.",
    "quantity_mismatch": "The recorded quantities differ for {names}.",
    "not_recorded": "Not found in the records read for {names}; that is not proof there "
                    "is none.",
    "no_need_date": "Empty for {names}, so whether that is late cannot be decided.",
}
MAX_NAMED = 5         # orders named in one rolled-up line
TEXT_FIELDS = ("headline", "title", "why", "what", "text")


def _schema() -> Any:
    from .ontology_tools import shared_schema
    return shared_schema()


@functools.lru_cache(maxsize=1)
def _graphs() -> tuple[dict[str, Any], ...]:
    from .ontology_tools_process import load_graphs
    return tuple(load_graphs().values())


def _props(type_id: str) -> dict[str, dict[str, Any]]:
    spec = _schema().object_types.get(type_id) or {}
    return {p["name"]: p for p in spec.get("properties") or ()}


def order_types() -> frozenset[str]:
    return frozenset(graph["root_type"] for graph in _graphs())


def _billing_types() -> list[str]:
    """Types with a due date and an open amount (invoices)."""
    return sorted(t for t in _schema().object_types
                  if {DUE_PROPERTY, OPEN_AMOUNT} <= set(_props(t)))


def party_types() -> frozenset[str]:
    """Types an order is placed with that an invoice is also billed to."""
    links = _schema().link_types.values()
    ordered = {link["to"] for link in links if link["from"] in order_types()}
    billed = {link["to"] for link in links if link["from"] in _billing_types()}
    return frozenset((ordered & billed) - order_types())


@functools.lru_cache(maxsize=1)
def _departments() -> dict[str, str]:
    from .employee_permissions import PORTAL_DEPARTMENT_CODES
    return {code: ("IT" if name == "it" else name.title().replace(" And ", " and "))
            for name, code in PORTAL_DEPARTMENT_CODES.items()}


def who_words(codes: Iterable[str]) -> list[str]:
    """Department names for answerer codes; anything undeclared as "the record's owner"."""
    names = [_departments().get(code, "the owner of the record") for code in codes]
    return list(dict.fromkeys(names)) or ["the owner of the record"]


def system_words(lineage: Any) -> str:
    text = str(lineage or "")
    if text.startswith("bc_") or text == "bc":
        return "Business Central"
    return LINEAGE_SYSTEMS.get(text, "another system")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _and(items: Sequence[str]) -> str:
    items = list(dict.fromkeys(items))
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _cite(citation: Any) -> list[dict[str, str]]:
    if isinstance(citation, Mapping) and citation.get("l1_handle"):
        return [{"handle": citation["l1_handle"], "field": citation.get("field_path") or ""}]
    return []


def _unique(citations: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    """Each cited (handle, field) once, in first-seen order."""
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for c in citations:
        seen.setdefault((c["handle"], c.get("field", "")), dict(c))
    return list(seen.values())


def _plain(value: Any) -> Any:
    return value.get("untrusted_text") if isinstance(value, Mapping) else value


def _decimal(raw: Any) -> Decimal | None:
    try:
        number = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


class _Copy:
    """One read record: the root or a same-record copy."""

    def __init__(self, object_id: str, answer: Mapping[str, Any]) -> None:
        self.object_id = object_id
        self.object = answer.get("object") or {}
        self.answer = answer
        self.system = system_words(self.object.get("lineage"))
        origin = (self.object.get("display") or {}).get("origin") or {}
        self.citations = ([{"handle": origin["citation"], "field": origin.get("field_path", "")}]
                          if origin.get("citation") else [])
        self.values: dict[str, list[dict[str, Any]]] = {}
        for value in answer.get("values") or ():
            self.values.setdefault(value.get("property"), []).append(value)

    def present(self, prop: str) -> list[dict[str, Any]]:
        return [v for v in self.values.get(prop, ()) if _plain(v.get("value")) not in (None, "")]

    def empty(self, prop: str) -> list[dict[str, Any]]:
        return [v for v in self.values.get(prop, ()) if _plain(v.get("value")) in (None, "")]


class _Card:
    def __init__(self, call: Callable[[str, dict[str, Any]], dict[str, Any]],
                 as_of: date) -> None:
        self.call, self.as_of = call, as_of
        self.blockers: list[dict[str, Any]] = []
        self.facts: list[dict[str, Any]] = []
        self.unknowns: dict[str, list[dict[str, str]]] = {}
        self.invoice_reads = 0

    def unknown(self, text: str | None, citations: Sequence[Mapping[str, str]] = ()) -> None:
        if text:
            known = self.unknowns.setdefault(text, [])
            known[:] = _unique([*known, *citations])

    def blocker(self, kind: str, key: str, title: str, why: str, citations: list,
                who: Iterable[str], what: str, basis: str = "proven",
                **extra: Any) -> None:
        if any(b["kind"] == kind and b["key"] == key for b in self.blockers):
            return
        self.blockers.append({"kind": kind, "key": key, "title": title, "why": why,
                              "basis": basis, "citations": _unique(citations),
                              "next_step": {"who": who_words(who), "what": what}, **extra})

    def fact(self, text: str, value: Any, citations: list, **extra: Any) -> None:
        self.facts.append({"text": text, "value": value, "citations": _unique(citations),
                           **extra})

    # ---- reads -------------------------------------------------------------

    def copies(self, root: _Copy) -> tuple[list[_Copy], list[_Copy]]:
        """The record's same-record copies: ``(merged, leads)``. Only a copy at a tier
        ``plx.object`` itself merges (verified or reviewed) is merged, its evidence read
        into the card; a lead (or any other weaker or unknown tier) is read only to be
        named as a match to confirm, never into blockers or facts; a contradicted one is
        not read at all."""
        from .ontology_tools import _IDENTITY_TIERS

        edges = [e for e in root.answer.get("identity") or ()
                 if str(e.get("link_type", "")).endswith(".same_record_as")
                 and e.get("from") == root.object_id and isinstance(e.get("to"), str)
                 and e.get("to") != root.object_id]
        strong = list(dict.fromkeys(e["to"] for e in edges if e.get("tier") in _IDENTITY_TIERS))
        weak = list(dict.fromkeys(e["to"] for e in edges
                                  if e.get("tier") not in _IDENTITY_TIERS
                                  and e.get("tier") != "contradicted"
                                  and e["to"] not in strong))
        found: tuple[list[_Copy], list[_Copy]] = ([], [])
        for ids, read in ((strong, found[0]), (weak, found[1])):
            for object_id in ids[:MAX_COPIES]:
                answer = self.call("plx.object", {"object_id": object_id,
                                                  "response_format": "detailed"})
                if answer.get("error"):
                    self.unknown("A copy of this record in another system could not be read.")
                    continue
                read.append(_Copy(object_id, answer))
            if len(ids) > MAX_COPIES:
                self.unknown("More copies in other systems exist than one card reads.")
        return found

    def around(self, object_id: str, link_type: str, direction: str) -> tuple[list[str], bool]:
        """Linked object ids; ``True`` when they were found only through weak links."""
        args = {"from": object_id, "path": [{"link_type": link_type, "direction": direction}],
                "limit": 50}
        answer = self.call("plx.around", args)
        found = [o["object_id"] for o in answer.get("objects") or () if o.get("object_id")]
        if found or not any(g.get("reason") == "below_tier_floor"
                            for g in answer.get("gaps") or ()):
            return found, False
        answer = self.call("plx.around", {**args, "tier_floor": "lead"})
        return [o["object_id"] for o in answer.get("objects") or () if o.get("object_id")], True

    # ---- an order ------------------------------------------------------------

    def order(self, type_id: str, copies: Sequence[_Copy], *, linked: bool = True
              ) -> dict[str, list[tuple[_Copy, dict[str, Any]]]]:
        """The order rules over ``copies``; the process reads by graph id. ``linked``
        also reads the order's invoices and its missing fields."""
        graphs = [g for g in _graphs() if g["root_type"] == type_id]
        results: dict[str, list[tuple[_Copy, dict[str, Any]]]] = {}
        for graph in graphs:
            for copy in copies:
                answer = self.call("plx.process", {"from": copy.object_id,
                                                   "process": graph["id"]})
                if not answer.get("error"):
                    results.setdefault(graph["id"], []).append((copy, answer))
        need = self.need_date(type_id, copies)
        undecided_qty = False
        for graph in graphs:
            runs = results.get(graph["id"]) or []
            if not runs:
                continue
            self.stages(type_id, graph, runs)
            self.due(graph, runs, need_known=need)
            for _copy, answer in runs:
                for check in answer.get("quantity_checks") or ():
                    if check.get("agree") is False:
                        self.mismatch(type_id, graph, check)
                    elif check.get("agree") is None:
                        undecided_qty = True
            for _copy, answer in runs:
                if any(g.get("reason") == "root_lineage_staging"
                       for g in answer.get("gaps") or ()):
                    self.unknown(reason_words("root_lineage_staging"), _copy.citations)
        if undecided_qty:
            self.unknown("Whether the quantities agree is not decided from the records you "
                         "can read.")
        if not linked:
            return results
        self.invoices([e for runs in results.values() for _c, answer in runs
                       for stage in answer.get("stages") or () if stage.get("state") in PRESENT
                       for e in stage.get("events") or ()])
        self.missing_fields(type_id, copies)
        return results

    def stages(self, type_id: str, graph: Mapping[str, Any],
               runs: Sequence[tuple[_Copy, Mapping[str, Any]]]) -> None:
        stages = graph["stages"]
        by_stage: dict[str, list[tuple[_Copy, Mapping[str, Any]]]] = {}
        authority: list[_Copy] = []   # copies whose root is no staging record
        for copy, answer in runs:
            for item in answer.get("stages") or ():
                by_stage.setdefault(item.get("stage"), []).append((copy, item))
            if not any(g.get("reason") == "root_lineage_staging"
                       for g in answer.get("gaps") or ()):
                authority.append(copy)
        present = {s["id"] for s in stages
                   if any(item.get("state") in PRESENT for _c, item in by_stage.get(s["id"], ()))}
        for index, stage in enumerate(stages):
            if stage["id"] in present:
                continue
            seen = by_stage.get(stage["id"], [])
            reasons = {(item.get("missing") or {}).get("reason") for _c, item in seen}
            label = stage.get("label") or field_words(stage["id"])
            waits = any("due" in later and later["id"] not in present
                        for later in stages[index + 1:])
            if stage.get("optional") and not waits:
                continue
            if reasons & NOT_FOUND:
                checked = [c for c, item in seen
                           if (item.get("missing") or {}).get("reason") in NOT_FOUND]
                self.blocker(
                    "not_recorded", stage["id"], f"No {label.lower()} found",
                    f"No {label.lower()} was found for this {type_words(type_id)} in "
                    f"{_and([c.system for c in checked])}; that is not proof there is none.",
                    [cite for c in checked for cite in c.citations],
                    [stage["owner_role"]], f"Find the {label.lower()} or record why there "
                    "is none.", basis="not_found")
            elif reasons & WITHHELD:
                self.unknown(f"Whether there is a {label.lower()} depends on evidence outside "
                             f"your access; {_and(who_words([stage['owner_role']]))} can "
                             "check it.")
            elif seen and reasons <= UNDECIDED_STATUS:
                # its record is held but its status word decides nothing (an unmapped PO
                # header word, or any status stage's unread word): not established, never
                # absent. The later steps still read
                # when a copy that is no staging record holds it (a staging record alone
                # proves no step).
                self.unknown(f"Whether the {label.lower()} step is done is not established: "
                             "its recorded status is not one this process decides; "
                             f"{_and(who_words([stage['owner_role']]))} can check it.",
                             [cite for c, _item in seen for cite in c.citations])
                if any(c in authority for c, _item in seen):
                    continue
            if not stage.get("optional"):
                break

    def due(self, graph: Mapping[str, Any], runs: Sequence[tuple[_Copy, Mapping[str, Any]]],
            need_known: bool) -> None:
        """``late`` from the process's verdict: a stage's ``lateness`` entry (its governing
        date by the declared precedence, po_delivery_window's receipt) where the answer
        has one, else its bare ``due`` entry (stages that declare no lateness)."""
        stages = {s["id"]: s for s in graph["stages"]}
        decided: set[str] = set()
        undecided: dict[str, set[str]] = {}
        for _copy, answer in runs:
            judged = {e.get("stage") for e in answer.get("lateness") or ()}
            verdicts = [(e, e.get("late"), _governing(e), e.get("days_late"))
                        for e in answer.get("lateness") or ()]
            verdicts += [(e, e.get("overdue"),
                          [v for v in e.get("due_by") or () if v.get("value")],
                          e.get("days_overdue"))
                         for e in answer.get("due") or () if e.get("stage") not in judged]
            for entry, late, dated, days in verdicts:
                stage = stages.get(entry.get("stage"))
                if stage is None:
                    continue
                if late is not None:
                    decided.add(stage["id"])
                if late is True:
                    self.late(stage, entry, dated, days)
                elif late is None and entry.get("state") != "not_applicable":
                    reason = (entry.get("gap") or {}).get("reason")
                    undecided.setdefault(stage["id"], set()).add(str(reason))
                else:
                    decided.add(stage["id"])
        for stage_id, reasons in undecided.items():
            if stage_id in decided:
                continue
            label = (stages[stage_id].get("label") or field_words(stage_id)).lower()
            if reasons & WITHHELD:
                self.unknown(f"The due date of the {label} is outside your access.")
            elif need_known:
                self.unknown(f"Whether the {label} is late is not established from the "
                             "records you can read.")

    def late(self, stage: Mapping[str, Any], entry: Mapping[str, Any],
             dated: Sequence[Mapping[str, Any]], days: Any) -> None:
        """One ``late`` blocker; its words are built once the reader's citations are
        known (``_late_words``), so a date shows only with its own citation."""
        label = stage.get("label") or field_words(stage["id"])
        dates = [{"value": str(_plain(v["value"]))[:10], "citations": _cite(v.get("citation"))}
                 for v in dated]
        others = [c for v in entry.get("values") or () for c in _cite(v.get("citation"))]
        how = ("received" if entry.get("measured_against") == "received" else
               "partly" if "partial_receipt" in (entry.get("reasons") or ()) else "open")
        spec = {"label": label, "dates": [d for d in dates if d["citations"]],
                "days": days if isinstance(days, int) and days > 0 else None, "how": how}
        self.blocker(
            "late", stage["id"], f"{label} is late", _late_words(spec, None),
            [c for d in dates for c in d["citations"]] + others,
            [stage["owner_role"]],
            f"Find out when the {label.lower()} will happen and record the new date."
            if how != "received" else
            f"Find out why the {label.lower()} came late and tell the supplier.",
            _late=spec)

    def need_date(self, type_id: str, copies: Sequence[_Copy]) -> bool:
        """The due-date fields of ``type_id``'s due-dated stages: a cited fact when any copy
        holds one, else a ``no_need_date`` blocker. ``False`` when none is held."""
        props = _props(type_id)
        events = _schema().event_types
        wanted: list[str] = []
        for graph in _graphs():
            for stage in graph["stages"]:
                spec = stage.get("due") or {}
                if spec.get("object_type") != type_id:
                    continue
                if spec.get("property") in props:
                    wanted.append(spec["property"])
                for event_id in spec.get("event_types") or ():
                    time = (events.get(event_id) or {}).get("time") or {}
                    wanted += [name for name, prop in props.items()
                               for src in prop.get("sources") or ()
                               if (src.get("binding"), src.get("path"))
                               == (time.get("binding"), time.get("path"))]
        held = False
        for prop in dict.fromkeys(wanted):
            values = [(c, v) for c in copies for v in c.present(prop)]
            for copy, value in values:
                held = True
                self.fact(f"{_cap(field_words(prop))} in {copy.system}",
                          _plain(value["value"]), _cite(value.get("citation")))
            empty = [(c, v) for c in copies for v in c.empty(prop)]
            if not values and empty:
                self.blocker(
                    "no_need_date", prop, f"No {field_words(prop)} recorded",
                    f"The {field_words(prop)} is empty in {_and([c.system for c, _ in empty])}, "
                    "so whether this is late cannot be decided.",
                    [c for _copy, v in empty for c in _cite(v.get("citation"))],
                    answerers.resolve([(type_id, prop, "")]),
                    f"Record the {field_words(prop)} on the {type_words(type_id)}.")
        return held

    def mismatch(self, type_id: str, graph: Mapping[str, Any], check: Mapping[str, Any]) -> None:
        roles = [str(r) for r in check.get("roles") or ()]
        sums: dict[tuple[str, str], Decimal] = {}
        for value in check.get("values") or ():
            number = _decimal(value.get("value"))
            if number is not None and value.get("quantity_role") in roles:
                key = (value["quantity_role"], str(value.get("unit") or ""))
                sums[key] = sums.get(key, Decimal(0)) + number
        said = "; ".join(f"{field_words(role)} {total} {unit}".strip()
                         for (role, unit), total in sorted(sums.items()))
        owners = [s["owner_role"] for s in graph["stages"] if s["id"] in roles] or \
            [graph["stages"][0]["owner_role"]]
        self.blocker(
            "quantity_mismatch", "|".join(roles), "Quantities do not agree",
            f"The {' and '.join(field_words(r) for r in roles)} quantities differ"
            + (f": {said}." if said else "."),
            [c for v in check.get("values") or () for c in _cite(v.get("citation"))],
            owners, "Check the quantities against the documents and correct the record.")

    def invoices(self, object_ids: Iterable[str], leads: Iterable[str] = ()) -> None:
        """An ``unpaid`` blocker per invoice past its due date with an amount open. One in
        ``leads`` was reached only through a lead link: said so, basis ``lead``."""
        billing = set(_billing_types())
        leads = set(leads)
        for object_id in dict.fromkeys(object_ids):
            if object_id.split(":", 1)[0] not in billing:
                continue
            if self.invoice_reads >= MAX_INVOICES:
                self.unknown("More invoices are linked than one card reads.")
                return
            self.invoice_reads += 1
            answer = self.call("plx.object", {"object_id": object_id,
                                              "response_format": "detailed"})
            if answer.get("error"):
                continue
            invoice = _Copy(object_id, answer)
            due = next((v for v in invoice.present(DUE_PROPERTY)), None)
            open_ = next((v for v in invoice.present(OPEN_AMOUNT)), None)
            if due is None or open_ is None:
                continue
            day, amount = _date(due["value"]), _decimal(_plain(open_["value"]))
            if day is None or amount is None or amount <= 0 or day >= self.as_of:
                continue
            type_id = invoice.object.get("object_type", "")
            currency = open_.get("currency")
            lead = object_id in leads
            if lead:
                self.unknown(reason_words("lead_tier_links_followed"))
            self.blocker(
                "unpaid", object_id, f"Unpaid {type_words(type_id)} past its due date",
                f"A {type_words(type_id)} was due {day.isoformat()} and {amount} "
                f"{currency or '(currency not recorded)'} is still open."
                + (" The link to it is a lead to confirm." if lead else ""),
                _cite(due.get("citation")) + _cite(open_.get("citation")),
                answerers.stage_owners(type_id),
                "Check the payment and follow up on what is still owed.",
                basis="lead" if lead else "proven")

    def missing_fields(self, type_id: str, copies: Sequence[_Copy]) -> None:
        from ontology.document_fields import comparisons
        links = _schema().link_types
        for rule in comparisons():
            prop = rule.get("property")
            if not rule.get("propose") or rule.get("path") or prop not in _props(type_id) \
                    or (links.get(rule["link_type"]) or {}).get("to") != type_id:
                continue
            if any(c.present(prop) for c in copies):
                continue
            empty = [(c, v) for c in copies for v in c.empty(prop)]
            if empty:
                self.blocker(
                    "missing_field", prop, f"{_cap(field_words(prop))} is missing",
                    f"The {type_words(type_id)} holds no {field_words(prop)} in "
                    f"{_and([c.system for c, _ in empty])}.",
                    [c for _copy, v in empty for c in _cite(v.get("citation"))],
                    answerers.resolve([(type_id, prop, "")]),
                    f"Add the {field_words(prop)} to the {type_words(type_id)}.")

    # ---- a party ---------------------------------------------------------------

    def party(self, type_id: str, copies: Sequence[_Copy]) -> None:
        links = _schema().link_types
        due_graphs = {g["root_type"]: g for g in _graphs()
                      if any("due" in s for s in g["stages"])}
        orders: dict[str, str] = {}
        leads: set[str] = set()     # orders reached only through weak links
        weak = False
        for link_id, link in sorted(links.items()):
            if link["to"] != type_id or link["from"] not in order_types():
                continue
            for copy in copies:
                found, lead = self.around(copy.object_id, link_id, "reverse")
                weak |= lead and bool(found)
                leads.update(found if lead else ())
                orders.update((oid, link["from"]) for oid in found)
        if weak:
            self.unknown(reason_words("lead_tier_links_followed"))
        if len(orders) > MAX_ORDERS:
            self.unknown("More orders are linked than one card reads.")
        open_by_type: dict[str, list[dict[str, str]]] = {}
        read_by_type: Counter[str] = Counter()
        merged: set[str] = set()
        for object_id, order_type in list(orders.items())[:MAX_ORDERS]:
            if object_id in merged:
                continue          # a confirmed copy of an order already read
            answer = self.call("plx.object", {"object_id": object_id,
                                              "response_format": "detailed"})
            if answer.get("error"):
                continue
            order = _Copy(object_id, answer)
            read_by_type[order_type] += 1
            sub = _Card(self.call, self.as_of)
            twins, _leads = sub.copies(order)    # a lead copy is not this order
            merged.update(t.object_id for t in twins)
            results = sub.order(order_type, [order, *twins], linked=False)
            graph = due_graphs.get(order_type)
            if graph is not None and graph["id"] in results:
                held = {item.get("stage") for _c, answer in results[graph["id"]]
                        for item in answer.get("stages") or () if item.get("state") in PRESENT}
                if any(s["id"] not in held for s in graph["stages"] if "due" in s):
                    open_by_type.setdefault(order_type, []).extend(order.citations)
            for found in sub.blockers:
                self.rollup(order_type, order, found, lead=object_id in leads)
        for order_type, count in sorted(read_by_type.items()):
            opened = open_by_type.get(order_type, [])
            self.fact(f"Open {type_words(order_type)}s of those read", f"{len(opened)} of {count}",
                      opened or [c for copy in copies for c in copy.citations])
        self.balance(type_id, copies)
        billed = [lid for lid, link in sorted(links.items())
                  if link["to"] == type_id and link["from"] in _billing_types()]
        invoices: dict[str, bool] = {}     # invoice id -> reached only by a lead link
        for lid in billed:
            for copy in copies:
                found, lead = self.around(copy.object_id, lid, "reverse")
                for oid in found:
                    invoices[oid] = invoices.get(oid, True) and lead
        self.invoices(invoices, leads=[oid for oid, lead in invoices.items() if lead])

    def rollup(self, order_type: str, order: _Copy, found: Mapping[str, Any], *,
               lead: bool = False) -> None:
        """``found``, one blocker of ``order``, onto the party: one line per rule and
        order type, counting orders and naming each by its label where the reader may
        read the label's citation (``_render``)."""
        key = f"{order_type}|{found['key']}"
        line = next((b for b in self.blockers
                     if b["kind"] == found["kind"] and b["key"] == key), None)
        if line is None:
            self.blocker(found["kind"], key, found["title"], "", [], [],
                         found["next_step"]["what"], basis=found["basis"])
            line = self.blocker_line(found["kind"], key)
            line.update(_type=order_type, _records=[], _who=[])
        label = _plain((order.object.get("display") or {}).get("untrusted_text"))
        line["_records"].append({"label": label if isinstance(label, str) and label else None,
                                 "label_citations": list(order.citations),
                                 "citations": list(found["citations"]), "lead": lead})
        line["_who"] = list(dict.fromkeys([*line["_who"], *found["next_step"]["who"]]))

    def blocker_line(self, kind: str, key: str) -> dict[str, Any]:
        return next(b for b in self.blockers if b["kind"] == kind and b["key"] == key)

    def balance(self, type_id: str, copies: Sequence[_Copy]) -> None:
        names = [name for name, prop in _props(type_id).items()
                 if prop.get("value_type") == "money" and "balance" in name]
        held = False
        for name in names:
            for copy in copies:
                for value in copy.present(name):
                    amount = _decimal(_plain(value["value"]))
                    if amount is None:
                        continue
                    held = True
                    currency = value.get("currency")
                    self.fact(f"{_cap(field_words(name))} in {copy.system}",
                              f"{amount} {currency}" if currency else str(amount),
                              _cite(value.get("citation")), amount=str(amount),
                              currency=currency)
                    if not currency:
                        self.unknown("The currency of the balance is not recorded.")
        if names and not held:
            owners = who_words(answerers.resolve([(type_id, names[0], "")])
                               or [c for t in _billing_types() for c in answerers.stage_owners(t)])
            self.unknown(f"No balance is recorded for this {type_words(type_id)} in what you "
                         f"can read; {_and(owners)} keeps it.",
                         [c for copy in copies for n in names
                          for v in copy.empty(n) for c in _cite(v.get("citation"))])

    def statuses(self, copies: Sequence[_Copy]) -> None:
        for copy in copies:
            for name, values in copy.values.items():
                if name == "status" or str(name).endswith("_status"):
                    for value in copy.present(name):
                        self.fact(f"{_cap(field_words(name))} in {copy.system}",
                                  _plain(value["value"]), _cite(value.get("citation")))


def _questions(type_id: str) -> list[dict[str, str]]:
    out = []
    for path in sorted(PLAN_DIR.glob("[!_]*.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if type_id in ((plan.get("root") or {}).get("object_types") or ()):
            for family in plan.get("families") or ():
                out.append({"family": family, "title": plan.get("title", "")})
    return out


def card(call: Callable[[str, dict[str, Any]], dict[str, Any]], object_id: Any, *,
         as_of: date | None = None,
         readable: Callable[[Sequence[str]], Any] | None = None) -> dict[str, Any]:
    """The status card of ``object_id`` read through ``call`` (module doc). A record the
    reader cannot read and one that does not exist read the same (``not_found``)."""
    day = as_of or datetime.now(timezone.utc).date()
    base = {"schema": SCHEMA, "object_id": object_id, "as_of": day.isoformat()}
    if not isinstance(object_id, str) or not object_id or len(object_id) > 700:
        return {**base, "status": "invalid_input"}
    answer = call("plx.object", {"object_id": object_id, "response_format": "detailed"})
    error = answer.get("error")
    if error:
        code = error.get("code") if isinstance(error, Mapping) else None
        if code in ("object_not_found", "not_found", "invalid_input"):
            return {**base, "status": "not_found"}
        return {**base, "status": "error", "error": {"code": str(code or "error")}}
    # The record read may answer under its canonical id; its copies are keyed by that id.
    read_id = (answer.get("object") or {}).get("object_id")
    root = _Copy(read_id if isinstance(read_id, str) and read_id else object_id, answer)
    type_id = str(root.object.get("object_type") or "")
    if type_id not in order_types() | party_types():
        return {**base, "status": "unsupported", "object_type": type_id}
    work = _Card(call, day)
    copies, leads = work.copies(root)
    every = [root, *copies]
    work.fact(f"{_cap(type_words(type_id))} in {root.system}",
              _plain((root.object.get("display") or {}).get("untrusted_text")), root.citations)
    work.statuses(every)
    if type_id in order_types():
        work.order(type_id, every)
    else:
        work.party(type_id, every)
    copy_line = None
    if copies or leads:
        said = []
        if copies:
            said.append(f"Also recorded in {_and([c.system for c in copies])}"
                        + (f" ({len(copies)} copies)" if len(copies) > 1 else "") + ".")
        if leads:
            said.append(f"{_and(list(dict.fromkeys(c.system for c in leads)))} may hold the "
                        f"same {type_words(type_id)}: a lead to confirm, not read into this "
                        "card.")
        copy_line = {"text": " ".join(said), "count": len(copies) + len(leads),
                     "citations": _unique(c for copy in [*copies, *leads]
                                          for c in copy.citations)}
    out = {**base, "status": "read", "object_type": type_id,
           "type_label": type_words(type_id),
           "label": _plain((root.object.get("display") or {}).get("untrusted_text")),
           "blockers": sorted(work.blockers, key=lambda b: KINDS.index(b["kind"])),
           "facts": work.facts[:MAX_FACTS], "copies": copy_line,
           "unknowns": [{"text": t, "citations": c} for t, c in work.unknowns.items()],
           "questions": _questions(type_id)}
    _restrict(out, readable)
    for blocker in out["blockers"]:
        blocker.pop("key")
        _render(blocker)
    out["more_blockers"] = max(0, len(out["blockers"]) - MAX_BLOCKERS)
    out["blockers"] = out["blockers"][:MAX_BLOCKERS]
    out["next_step"] = out["blockers"][0]["next_step"] if out["blockers"] else None
    count = len(out["blockers"]) + out["more_blockers"]
    out["headline"] = (("1 thing needs attention" if count == 1 else
                        f"{count} things need attention") if out["blockers"] else
                       "Nothing found that needs attention in what you can read; that is not "
                       "proof there is nothing.")
    return out


def card_for_reference(call: Callable[[str, dict[str, Any]], dict[str, Any]],
                       object_type: Any, reference: Any, *, as_of: date | None = None,
                       readable: Callable[[Sequence[str]], Any] | None = None
                       ) -> dict[str, Any]:
    """The card of the record of ``object_type`` that ``reference`` (a case root such as
    an order number) names, found with ``plx.find``: only a match ``plx.find`` would let
    make a root (``ROOT_REASONS``: an exact identifier or an alias), never display text
    or a lead. Identity stays conservative. No such match is ``not_found``. Several
    matches read as one record only when each other one is a verified or strong
    same-record copy of the first at a tier the card merges (verified or reviewed). Otherwise the answer is
    ``ambiguous`` with the count and no card, never a pick."""
    from operating.ontology_tools import _IDENTITY_TIERS, ROOT_REASONS

    day = as_of or datetime.now(timezone.utc).date()
    base = {"schema": SCHEMA, "reference": reference, "as_of": day.isoformat()}
    if (not isinstance(object_type, str) or not isinstance(reference, str)
            or not reference.strip() or len(reference) > 200):
        return {**base, "status": "invalid_input"}
    if object_type not in order_types() | party_types():
        return {**base, "status": "unsupported", "object_type": object_type}
    found = call("plx.find", {"query": reference.strip(), "object_type": object_type,
                              "limit": 20})
    error = found.get("error")
    if error:
        code = error.get("code") if isinstance(error, Mapping) else None
        if code in ("object_not_found", "not_found"):
            return {**base, "status": "not_found"}
        return {**base, "status": "error", "error": {"code": str(code or "error")}}
    ids = list(dict.fromkeys(
        obj["object_id"] for c in found.get("candidates") or ()
        if c.get("reason") in ROOT_REASONS and isinstance(obj := c.get("object"), Mapping)
        and isinstance(obj.get("object_id"), str) and obj.get("object_type") == object_type))
    if not ids:
        return {**base, "status": "not_found"}
    if len(ids) > 1:
        first = call("plx.object", {"object_id": ids[0], "response_format": "detailed"})
        same = {e.get("to") if e.get("from") == ids[0] else e.get("from")
                for e in first.get("identity") or ()
                if str(e.get("link_type", "")).endswith(".same_record_as")
                and e.get("tier") in _IDENTITY_TIERS
                and ids[0] in (e.get("from"), e.get("to"))}
        if not set(ids[1:]) <= same:
            return {**base, "status": "ambiguous", "object_type": object_type,
                    "matches": len(ids)}
    return {**card(call, ids[0], as_of=day, readable=readable), "reference": reference}


def _restrict(out: dict[str, Any], readable: Callable[[Sequence[str]], Any] | None) -> None:
    """Every claim keeps only citations ``readable`` allows (all when ``None``). A claim
    left with none is not shown: one plain line says evidence was withheld, and one says
    a claim had no source to cite at all."""
    for blocker in out["blockers"]:
        for record in blocker.get("_records") or ():
            blocker["citations"] = _unique([*blocker["citations"], *record["citations"],
                                            *record["label_citations"]])
    claims = [*out["blockers"], *out["facts"], *([out["copies"]] if out["copies"] else []),
              *out["unknowns"]]
    every = sorted({c["handle"] for item in claims for c in item["citations"]})
    allowed = set(every if readable is None else readable(every))
    lost = uncited = False
    said = {id(u) for u in out["unknowns"]}
    for item in claims:
        uncited |= id(item) not in said and not item["citations"]
        kept = [c for c in item["citations"] if c["handle"] in allowed]
        lost |= len(kept) != len(item["citations"])
        item["citations"] = kept
    for blocker in out["blockers"]:
        if "_records" not in blocker:
            continue
        records = []
        for record in blocker["_records"]:
            cited = [c for c in record["citations"] if c["handle"] in allowed]
            if not cited:
                continue
            named = [c for c in record["label_citations"] if c["handle"] in allowed]
            records.append({**record, "citations": cited, "label_citations": named,
                            "label": record["label"] if named else None})
        blocker["_records"] = records
        blocker["citations"] = _unique([c for r in records
                                        for c in (*r["citations"], *r["label_citations"])])
    out["blockers"] = [b for b in out["blockers"] if b["citations"]]
    out["facts"] = [f for f in out["facts"] if f["citations"]]
    if out["copies"] and not out["copies"]["citations"]:
        out["copies"] = None
    if lost:
        out["unknowns"].append({"text": WITHHELD_TEXT, "citations": []})
    if uncited:
        out["unknowns"].append({"text": UNCITED_TEXT, "citations": []})


def _governing(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The cited values of a lateness verdict that carry its governing date."""
    day = _date(entry.get("governing_date"))
    return [v for v in entry.get("values") or ()
            if day is not None and _date(_plain(v.get("value"))) == day]


def _late_words(spec: Mapping[str, Any], kept: Sequence[Mapping[str, str]] | None) -> str:
    """A late blocker's why. With ``kept`` (the citations the reader may open) a due
    date, and the days counted from it, show only when the date's own citation is kept."""
    keep = None if kept is None else {(c["handle"], c.get("field", "")) for c in kept}
    day = next((d["value"] for d in spec["dates"]
                if keep is None or any((c["handle"], c.get("field", "")) in keep
                                       for c in d["citations"])), None)
    days = spec["days"] if day else None
    label = spec["label"]
    if spec["how"] == "received":
        return (f"{label} was due {day or 'earlier'} and came "
                + (f"{days} days late." if days else "late."))
    return (f"{label} was due {day or 'earlier'}" + (f", {days} days ago," if days else "")
            + (" and is only partly recorded." if spec["how"] == "partly" else
               " and is not recorded yet."))


def _render(blocker: dict[str, Any]) -> None:
    """A blocker's words from the citations the reader kept; a party's rolled-up line
    from the orders the reader may see."""
    late = blocker.pop("_late", None)
    if late is not None:
        blocker["why"] = _late_words(late, blocker["citations"])
    records = blocker.pop("_records", None)
    order_type, who = blocker.pop("_type", None), blocker.pop("_who", [])
    if records is None:
        return
    words = type_words(order_type or "")
    n = len(records)
    labels = list(dict.fromkeys(r["label"] for r in records if r["label"]))[:MAX_NAMED]
    rest = n - len(labels)
    parts = labels + ([f"{rest} {'other ' if labels else ''}{words}{'s' if rest != 1 else ''}"]
                      if rest else [])
    blocker["title"] = f"{blocker['title']} on {n} {words}{'s' if n != 1 else ''}"
    blocker["why"] = ROLLUP_WHY.get(blocker["kind"], "Seen for {names}.").format(
        names=_and(parts)) + (f" The link to {'it' if n == 1 else 'them'} is a lead to "
                              "confirm." if any(r["lead"] for r in records) else "")
    if who:               # already department words, from each order's own line
        blocker["next_step"]["who"] = who


def _texts(item: Any) -> Iterable[str]:
    if isinstance(item, Mapping):
        for key, value in item.items():
            if key in TEXT_FIELDS and isinstance(value, str):
                yield value
            elif key == "who" and isinstance(value, list):
                yield from (str(v) for v in value)
            elif key not in ("value", "label", "citations", "object_id"):
                yield from _texts(value)
    elif isinstance(item, list):
        for value in item:
            yield from _texts(value)


def internal_codes(result: Mapping[str, Any]) -> list[str]:
    """Internal tokens in the card's own words (never its source values): snake_case codes,
    department codes, object ids, link types and tool names."""
    codes = sorted(code for code, name in _departments().items() if code != name)
    pattern = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b|\bplx\.[a-z]|"
                         r"\b[A-Z][A-Za-z0-9]+:[a-z_]+\.|\b[A-Z][a-z]+[A-Z][A-Za-z]*\.[a-z]|"
                         r"\b(?:" + "|".join(map(re.escape, codes)) + r")\b")
    return sorted({m.group(0) for text in _texts(result) for m in pattern.finditer(text)})


def cited_facts(result: Mapping[str, Any]) -> int:
    return sum(1 for item in [*(result.get("blockers") or ()), *(result.get("facts") or ()),
                              *([result["copies"]] if result.get("copies") else [])]
               if item.get("citations"))


def sample(call: Callable[[str, dict[str, Any]], dict[str, Any]], object_type: str,
           limit: int, as_of: date) -> dict[str, Any]:
    """Cards for up to ``limit`` records of ``object_type`` that hold its display field."""
    display = (_schema().object_types.get(object_type) or {}).get("display") or []
    found = call("plx.find", {"object_type": object_type, "limit": limit,
                              "filters": [{"property": display[0], "op": "present"}]}) \
        if display else {"error": {"code": "no_display_property"}}
    counts: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    unread: Counter[str] = Counter()
    if found.get("error"):
        unread[str((found["error"] or {}).get("code") or "error")] += 1
    ids = list(dict.fromkeys(i for c in found.get("candidates") or ()
                             if isinstance(i := (c.get("object") or {}).get("object_id"), str)))
    for object_id in ids[:limit]:
        result = card(call, object_id, as_of=as_of)
        if result["status"] != "read":
            unread[result["status"]] += 1
            continue
        counts["cards"] += 1
        counts["cards_with_cited_fact" if cited_facts(result) else
               "cards_without_cited_fact"] += 1
        counts["cards_with_internal_codes"] += bool(internal_codes(result))
        counts["cards_with_blocker"] += bool(result["blockers"])
        kinds.update(b["kind"] for b in result["blockers"])
    return {"object_type": object_type, "candidates": len(ids),
            **{key: counts[key] for key in ("cards", "cards_with_cited_fact",
                                            "cards_without_cited_fact",
                                            "cards_with_internal_codes",
                                            "cards_with_blocker")},
            "blockers_by_kind": dict(sorted(kinds.items())),
            "unread": dict(sorted(unread.items()))}


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None) -> int:
    from integrations.source_mirror import AudienceMap

    from . import case_workspace_live as live

    parser = argparse.ArgumentParser(prog="python3 -m operating.status_card",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--reader", action="append", default=[], metavar="P1,P2")
    parser.add_argument("--host-config", type=Path, default=None)
    parser.add_argument("--object-type", default="PurchaseOrder")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        audience = AudienceMap.from_file(args.mirror_audience)
        readers = [live._reader(text, args.tenant, args.clearance) for text in args.reader]
    except (OSError, ValueError) as error:
        parser.error(f"invalid input: {type(error).__name__}")
    if audience.tenant != args.tenant:
        parser.error("mirror audience tenant must equal --tenant")
    if not 1 <= args.limit <= 60:
        parser.error("--limit must be 1..60")
    from ontology.core import Actor

    as_of = args.as_of or datetime.now(timezone.utc).date()
    summary: dict[str, Any] = {"schema": SCHEMA, "evidence": live.MIRROR_EVIDENCE["label"],
                               "as_of": as_of.isoformat(), "capture": None}
    if args.host_config is not None:
        code, summary["capture"] = live._host_cycle(args, env, host_options or {})
        if code != 0:
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    investigator = Actor(args.tenant, frozenset(args.principal), args.clearance)
    runtime = live.offline_runtime(args.state_dir, args.mirror_audience, args.tenant,
                                   environ=env)
    projection = live.projection_file(args.state_dir, args.tenant)
    rendered = []
    try:
        live.project_state(args.state_dir, projection, tenant=args.tenant)
        summary["blocked_upstream"] = live.upstream_parts(live.portal_upstream(args.state_dir))
        for actor in (investigator, *readers):
            session = live._Session(runtime, actor, audience, projection,
                                    captures=args.state_dir)
            try:
                found = sample(session, args.object_type, args.limit, as_of)
            finally:
                session.close()
            rendered.append({"reader": sorted(actor.principals),
                             "investigator": actor is investigator, **found})
    finally:
        runtime.close()
    summary.update(rendered[0], readers=rendered)
    summary.pop("reader"), summary.pop("investigator")
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
