"""Late-order findings and their drafts: the late order rule, the vendor-facing draft text,
and who may judge lateness or a promise date."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
import json
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from . import copy_gate, drafts
from .copy_gate import say_date
from . import po_status
from . import proactive_gaps as pg

from .proactive_engine_config import (
    CONFIRMATION_LINK, CONFIRMATION_TYPE, CONFIRMED_DATE, CONFIRMING, DRAFT_LABEL,
    DRAFT_STATUS, LATENESS_GRAPH, MAX_DRAFT_CITATIONS, MAX_DRAFT_LINES, MAX_DRAFT_TEXT,
    SETTLED,
)
from .proactive_engine_findings import _cites, _day, _gap, _markings, _parts, _result, _Skips
from .proactive_engine_purchasing import _DUE_WORDS, _po_source
from .proactive_engine_quantities import _card_of, _unreceived_draft

if TYPE_CHECKING:
    from .proactive_engine_config import Cite
    from .proactive_engine import _Store


def _late_orders(st: _Store, as_of: date, only: str | None = None
                 ) -> tuple[list[dict[str, Any]], "_Skips"] | None:
    """Each order with late lines (``pg._late_line``) and the facts its supplier draft
    states, every fact as ``(value, Cite)``: its number, its supplier's name, its
    confirmed delivery date and each late line. ``only``: that one order (the console's
    re-derivation). None when the schema does not declare the rule."""
    order_type = pg._late_target()
    if order_type is None:
        return None
    line_props = (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED, pg.LINE_ITEM)
    orders_of: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in st.links((pg.LINE_LINK,), CONFIRMING):
        orders_of[link["from_object"]].append((link["to_object"], link["markings"]))
    wanted = None if only is None else sorted(
        line for line, reached in orders_of.items() if any(o == only for o, _m in reached))
    lines = st.values((pg.LINE_TYPE,), line_props, objects=wanted)
    skipped = _Skips()
    late: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = defaultdict(list)
    for line_id, entry in sorted(lines.items()):
        found = pg._late_line(_card_of(line_id, pg.LINE_TYPE, entry["props"]), as_of)
        if found is None:
            if any(len(entry["props"].get(p, ())) > 1 for p in line_props[:3]):
                skipped.note("line_value_disputed", [line_id], _parts(
                    [r for p in line_props[:3] for r in entry["props"].get(p, ())]))
            continue
        reached = {order for order, _marks in orders_of.get(line_id, [])
                   if order.startswith(f"{order_type}:")}
        if len(reached) != 1:
            # unlinked: nothing beyond the line; ambiguous: each order and its link
            skipped.note("line_order_unlinked" if not reached else "line_order_ambiguous",
                         [line_id], [(order, _markings(m)) for order, m in
                                     orders_of.get(line_id, []) if order in reached])
            continue
        (order,) = reached
        if only is not None and order != only:
            continue
        marks = _markings(*[m for o, m in orders_of[line_id] if o == order])
        late[order].append((found, entry, marks))
    # A BC Draft never received against
    # dates nothing, so its lines are never late (a Draft any line of which records a
    # received quantity keeps its lines' dates)
    lines_of_order: dict[str, list[str]] = defaultdict(list)
    for line_id, reached in orders_of.items():
        for order, _m in reached:
            if order in late:
                lines_of_order[order].append(line_id)
    for order in sorted(o for o in late
                        if _unreceived_draft(st, o, lines_of_order[o], lines)):
        skipped.note("draft_dates_nothing", [order],
                     [(line["line_id"], m) for line, _e, m in late[order]])
        del late[order]
    if not late:
        return [], skipped
    picked = sorted(late)
    numbers = st.values((order_type,), (pg.ORDER_NUMBER,), objects=picked)
    supplier_type = pg._supplier_target()
    suppliers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if supplier_type is not None:
        for link in st.links((pg.SUPPLIER_LINK,), CONFIRMING):
            if link["from_object"] in late:
                suppliers[link["from_object"]].append((link["to_object"], link["markings"]))
    names = st.values((supplier_type,), (pg.SUPPLIER_NAME,), objects=sorted(
        {t for found in suppliers.values() for t, _m in found})) if supplier_type else {}
    confirms: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    from operating.pg.own_issued import hidden_ids
    own = hidden_ids()   # PLX's own order confirmations held as a supplier's: no promise
    for link in st.links((CONFIRMATION_LINK,), CONFIRMING):
        if link["to_object"] in late and link["from_object"] not in own:
            confirms[link["to_object"]].append((link["from_object"], link["markings"],
                                                link["tier"] in SETTLED))
    dated = st.values((CONFIRMATION_TYPE,), (CONFIRMED_DATE,), objects=sorted(
        {c for found in confirms.values() for c, _m, _s in found}))
    out = []
    for order in picked:
        facts: dict[str, Any] = {"order": order, "order_type": order_type, "number": None,
                                 "supplier": None, "confirmed": [], "corroborated": [],
                                 "unresolved": []}
        card = _card_of(order, order_type, (numbers.get(order) or {"props": {}})["props"])
        number = pg._one_text(card, pg.ORDER_NUMBER)
        if number is None:
            facts["unresolved"].append("order_number:not_in_record")
        else:
            facts["number"] = (number[0], _cites([r for r in numbers[order]["props"][
                pg.ORDER_NUMBER] if r["l1_handle"] == number[1]])[0])
        if supplier_type is None:
            facts["unresolved"].append("supplier:link_undeclared")
        else:
            reached = {t: m for t, m in suppliers.get(order, [])}
            if len(reached) != 1 or not next(iter(reached)).startswith(f"{supplier_type}:"):
                # none, two, or one that is not a supplier record: no one supplier
                facts["unresolved"].append("supplier:not_linked" if not reached
                                           else "supplier:ambiguous")
            else:
                ((one, link_marks),) = reached.items()
                card = _card_of(one, supplier_type, (names.get(one) or {"props": {}})["props"])
                name = pg._one_text(card, pg.SUPPLIER_NAME)
                if name is None:
                    facts["unresolved"].append("supplier:name_not_in_record")
                else:
                    facts["supplier"] = (name[0], _cites([
                        r for r in names[one]["props"][pg.SUPPLIER_NAME]
                        if r["l1_handle"] == name[1]], link_marks)[0])
        for confirmation, link_marks, settled in sorted(confirms.get(order, [])):
            rows = (dated.get(confirmation) or {"props": {}})["props"].get(CONFIRMED_DATE, [])
            days = {_day(r["value"]) for r in rows}
            if len(days) != 1 or None in days:
                facts["unresolved"].append("confirmed_date:unreadable")
                continue
            # only a settled confirmation is told to the supplier as theirs; a corroborated
            # one stays an internal note
            facts["confirmed" if settled else "corroborated"].append(
                (next(iter(days)).isoformat(), _cites(rows[:1], link_marks)[0]))
        facts["lines"] = []
        for line, entry, marks in late[order]:
            props = entry["props"]

            def cite(prop: str, handle: str | None) -> list[Cite]:
                return _cites([r for r in props.get(prop, []) if r["l1_handle"] == handle],
                              marks)
            facts["lines"].append({
                **line, "date_cite": cite(pg.LINE_DATE, line["date_handle"]),
                "quantity_cites": cite(pg.LINE_ORDERED, line["ordered"][2])
                + cite(pg.LINE_RECEIVED, line["received"][2]),
                "item_cite": cite(pg.LINE_ITEM, line["item"][1]) if line.get("item") else [],
                "evidence": {prop: _cites([r for r in props[prop]
                                           if r["l1_handle"] in line["handles"]], marks)
                             for prop in (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED)}})
        out.append(facts)
    return out, skipped


def _worded(facts: Mapping[str, Any], roles: Sequence[str],
            readable: Callable[[Sequence[Cite]], set[str]] | None = None
            ) -> dict[str, Any] | None:
    """The supplier draft of one order's facts as a reader may read them: a line is used
    only cited whole (its date and both quantities), a withheld item name is left out, a
    withheld number, supplier or confirmation is named in ``unresolved`` (a withheld
    confirmation drops the confirmed date: the reader cannot see whether it agrees). None
    when no late line is left."""
    every = [c for line in facts["lines"] for c in (*line["date_cite"],
                                                    *line["quantity_cites"],
                                                    *line["item_cite"])]
    every += [f[1] for f in (facts["number"], facts["supplier"]) if f is not None]
    every += [c for _day_text, c in (*facts["confirmed"], *facts["corroborated"])]
    every += [facts["due"][2]] if facts.get("due") else []
    shown = {c[0] for c in every} if readable is None else readable(every)
    opens = lambda cites: bool(cites) and all(c[0] in shown for c in cites)  # noqa: E731
    lines = []
    for line in facts["lines"]:
        if not opens(line["date_cite"] + line["quantity_cites"]):
            continue
        lines.append({**line, "item": line["item"] if opens(line["item_cite"]) else None})
    if not lines:
        return None
    unresolved = list(facts["unresolved"])
    told: dict[str, Any] = {}
    for name in ("number", "supplier"):
        fact = facts[name]
        if fact is not None and not opens([fact[1]]):
            unresolved.append(f"{'order_number' if name == 'number' else name}:withheld")
            fact = None
        told[name] = None if fact is None else (fact[0], fact[1][0])
    confirmed = facts["confirmed"]
    told["confirmed"] = None
    told["internal_notes"] = []
    if confirmed and not opens([c for _d, c in confirmed]):
        unresolved.append("confirmed_date:withheld")
    elif len({d for d, _c in confirmed}) > 1:
        unresolved.append("confirmed_date:conflict")
    elif confirmed and "confirmed_date:unreadable" not in unresolved:
        told["confirmed"] = (confirmed[0][0], confirmed[0][1][0])
    corroborated = facts["corroborated"]
    if (corroborated and told["confirmed"] is None and len({d for d, _c in corroborated}) == 1
            and opens([c for _d, c in corroborated])):
        told["internal_notes"] = [{"note": "confirmed_date:corroborated_only",
                                   "value": corroborated[0][0],
                                   "citation": corroborated[0][1][0]}]
    due = facts.get("due")
    told["due"] = (due[0], due[1], due[2][0]) if due is not None and opens([due[2]]) else None
    told["unresolved"] = unresolved
    return late_order_draft(lines, told, roles)


def _amount(number: Decimal, unit: str | None) -> str:
    shown = format(number.normalize(), "f")
    return f"{shown} {pg._clip(' '.join(unit.split()), 20)}" if unit and unit.strip() else shown


def late_order_draft(lines: Sequence[Mapping[str, Any]], facts: Mapping[str, Any],
                     roles: Sequence[str]) -> dict[str, Any]:
    """The supplier message asking it to confirm delivery of one order's late lines, worded
    from cited facts only, never sent (``DRAFT_STATUS``, ``sent`` False). It names the
    purchase order, each late line (its item, ordered and received quantities) and the
    governing date: the supplier's confirmed delivery date when exactly one is readable
    (``date_basis`` ``confirmed``), else each line's expected receipt date worded "our
    expected date" (``expected``). Its citations are a side list: each stated fact one
    ``facts`` entry with its handle, no marker in the text; at most ``MAX_DRAFT_LINES``
    lines and ``MAX_DRAFT_CITATIONS`` handles (the catalog's draft citation bound)."""
    citations: list[str] = []
    listed: list[dict[str, Any]] = []

    def fact(name: str, value: str, handle: str, **extra: Any) -> None:
        if handle not in citations:
            citations.append(handle)
        listed.append({"fact": name, "value": value, **extra, "citation": handle})

    number, supplier, confirmed = facts.get("number"), facts.get("supplier"), \
        facts.get("confirmed")
    if supplier is not None:
        to = {"kind": "supplier", "name": supplier[0], "citation": supplier[1]}
        to_text = supplier[0]
        fact("supplier", supplier[0], supplier[1])
    else:
        role = roles[0] if roles else None
        to = {"kind": "answerer_role", "role": role,
              "reason": "the order's supplier name is not readable here"}
        to_text = (f"{role} (answerer role; the order's supplier name is not readable here)"
                   if role else "(no supplier name or answerer role is known)")
    if number is not None:
        fact("order_number", number[0], number[1])
        order_text, subject = (f"purchase order {number[0]}",
                               f"{number[0]}: please confirm the delivery date")
    else:
        order_text, subject = ("our purchase order (its number is not readable here)",
                               "Our purchase order: please confirm the delivery date")
    due = facts.get("due")
    if confirmed is not None:
        fact("confirmed_delivery_date", confirmed[0], confirmed[1])
        basis = (f"Your confirmation gives the delivery date {say_date(confirmed[0])}, and these lines "
                 "are not fully received:\n")
    elif due is not None:
        fact("due_date", due[0], due[2], basis=due[1])
        words = {"requested": "our requested date", "vendor_eta": "your confirmed date",
                 "lead_time": "the order's lead time, due"}[due[1]]
        basis = f"These lines are past {words} {say_date(due[0])} and not fully received:\n"
    else:
        basis = "These lines are past our expected date and not fully received:\n"
    # one composer (``drafts``): the sender's voice (no greeting name for a firm, no fixed
    # sign-off) and the copy gate; the late lines follow as record lines
    ask = f"Can you confirm the delivery date for {order_text}?"
    composed = drafts.compose(
        "supplier_date_ask", [ask, basis.rstrip("\n").rstrip(":") + ":"],
        subject=subject[:60], specific=False,
        facts=[("n", number[0] if number else None)]
        + [(f["fact"], f["value"]) for f in listed])
    opening = (composed["body"] or ask) + "\n"
    head = f"{DRAFT_LABEL}\nTo: {to_text}\nSubject: {subject}\n\n{opening}"
    tail = ""
    ordered = sorted(lines, key=lambda line: (line["day"], str(line.get("line_id"))))
    body, shown = "", 0
    for line in ordered[:MAX_DRAFT_LINES]:
        mark = len(listed), list(citations)
        line_id = str(line.get("line_id") or "")
        item = line.get("item")
        if item:
            fact("line_item", item[0], item[1], line_id=line_id)
        name = item[0] if item else "a line (the record names no item)"
        when = ""
        if confirmed is None and due is None:
            day = line["day"].isoformat()
            fact("expected_receipt", day, line["date_handle"], line_id=line_id)
            when = f"our expected date {say_date(day)}, "
        (q_o, u_o, h_o), (q_r, u_r, h_r) = line["ordered"], line["received"]
        fact("ordered_quantity", format(q_o.normalize(), "f"), h_o, line_id=line_id, unit=u_o)
        fact("received_quantity", format(q_r.normalize(), "f"), h_r, line_id=line_id, unit=u_r)
        row = (f"- {name}: {when}ordered {_amount(q_o, u_o)}, "
               f"received {_amount(q_r, u_r)}\n")
        if (len(head) + len(body) + len(row) + len(tail) + 60 > MAX_DRAFT_TEXT
                or len(citations) > MAX_DRAFT_CITATIONS):
            del listed[mark[0]:]   # this line does not fit: neither its text nor its facts
            citations[:] = mark[1]
            break
        body += row
        shown += 1
    if shown < len(ordered):
        body += f"- The other {len(ordered) - shown} late lines are not listed here\n"
    copy_check = copy_gate.check(opening + body, surface="draft_body")
    return {"label": DRAFT_LABEL, "status": DRAFT_STATUS, "sent": False, "to": to,
            "copy_check": copy_check, "subject": pg._clip(subject, 200), "text": pg._clip((head + body + tail).rstrip("\n"),
                                                                 MAX_DRAFT_TEXT),
            "date_basis": "confirmed" if confirmed is not None else
            ("due" if due is not None else "expected"),
            "internal_notes": [dict(n) for n in facts.get("internal_notes") or ()],
            "facts": listed, "citations": citations, "lines_late": len(ordered),
            "lines_listed": shown, "unresolved": list(facts.get("unresolved") or ())}


def _vendor_facing(src: po_status.StoreSource, po: po_status.PoRead,
                   facts: dict[str, Any]) -> None:
    """The order number the supplier knows (the number on the document we sent: a
    FileMaker-era order's FileMaker PO number, held by its FileMaker and Portal copies),
    in place of a copy's own internal number, when exactly one is readable and cited."""
    row = po_status.vendor_facing(
        (r.get("value"), r) for m in (*po.members, *po.date_twins)
        for r in src.props(m).get(po_status.VENDOR_NUMBER_PROP, []))
    if row is not None:
        facts["number"] = (str(row["value"]).strip(), _cites([row])[0])


def _late_order(st: _Store, as_of: date) -> dict[str, Any]:
    """``late_order``: late lines (``pg._late_line``) grouped by their one order, each gap
    with its supplier draft (``late_order_draft``) as the store holds the facts."""
    name = "late_order"
    found = _late_orders(st, as_of)
    if found is None:
        return _result([], status="not_configured", reasons=["rule_not_declared"])
    orders, skipped = found
    gaps = []
    src = _po_source(st)
    for facts in orders:
        # the order is late only by the one purchase-order definition (a receipt in any
        # copy closes it; a default date dates nothing): a late line of an order that
        # rule does not call late is no claim (one rule, never a second copy of it)
        po = po_status.read(src, facts["order"], as_of)
        if po.verdict is None or po.verdict[0] != po_status.LATE:
            skipped.note("order_not_late", [facts["order"]],
                         [(facts["order"], _markings(*po.identity_marks))])
            continue
        _vendor_facing(src, po, facts)
        by_lines = all(isinstance(r, Mapping) and r.get("property") == pg.LINE_DATE
                       for r in po.need.governing)
        due_cites = _cites(po.need.governing, *po.need.marks)
        if due_cites and not by_lines:
            # dated by its header or a labelled source: the draft states that one date
            facts["due"] = (po.need.hi.isoformat(), po.need.basis, due_cites[0])
        # a line whose own date is a default (``po_status``'s date provenance) is never
        # stated as its expected date
        defaulted = {(r.get("object_id"), str(r.get("value"))) for r in po.need.defaulted
                     if isinstance(r, Mapping)}
        facts["lines"] = [line for line in facts["lines"] if not by_lines
                          or (line.get("line_id"), line["day"].isoformat()) not in defaulted]
        if not facts["lines"]:
            skipped.note("order_not_late", [facts["order"]],
                         [(facts["order"], _markings(*po.identity_marks))])
            continue
        rows = facts["lines"]
        groups = [[c for line in rows for c in line["evidence"][prop]]
                  for prop in (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED)]
        extra = [c for line in rows for c in line["item_cite"]]
        extra += [f[1] for f in (facts["number"], facts["supplier"]) if f is not None]
        extra += [c for _d, c in facts["confirmed"]]
        count = len(rows)
        noun = "line" if count == 1 else "lines"
        due = po.need.hi.isoformat()
        gap = _gap(
            "late_order", name, facts["order"], facts["order_type"], "late_lines",
            summary=f"{count} {noun} not fully received past {_DUE_WORDS[po.need.basis]} "
                    f"{due}",
            missing="Confirm a new delivery date for the lines not yet received",
            why=pg._clip(f"The order is past {_DUE_WORDS[po.need.basis]} {due} and {count} "
                         f"{noun} still have a received quantity below the ordered "
                         "quantity in every copy of the order, as cited."),
            priority="high", about=(pg.LINE_TYPE, pg.LINE_DATE), groups=groups,
            extra=list({c[0]: c for c in extra}.values()))
        if gap is not None:
            draft = _worded(facts, gap["follow_up"]["who_can_answer"])
            if draft is not None:
                gap["follow_up"]["draft"] = draft
            gaps.append(gap)
    return _result(gaps, not_evaluated=skipped)


def _lateness_audiences(st: _Store) -> list[str]:
    """Every audience a reader needs for ``plx.process`` to decide a purchase order's
    lateness for it, as the tool decides a source is withheld (a Portal relation's
    ``api:`` key, a BC relation's stored ``bc:<company>/`` keys or, none stored, the
    family-wide ``bc:*``; else ``<family>:*`` and the family's stored keys;
    item-ACL sources are unproven for every reader alike): over each binding the
    ``po_delivery_window`` graph names and each binding of the link types it walks, plus
    the audience of every stored record of those bindings."""
    from ontology.schema import load_schema
    from operating.ontology_tools_policy import _AUDIENCE_SOURCE, _ITEM_ACL_AUDIENCES

    schema = load_schema()
    strings: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            strings.add(node)
        elif isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(LATENESS_GRAPH.read_text(encoding="utf-8")))
    bindings = {s for s in strings if s in schema.bindings}
    for link in (schema.link_types[s] for s in strings if s in schema.link_types):
        for lb in link.get("bindings", ()):
            bindings |= {lb.get("from_binding"), lb.get("to_binding"),
                         (lb.get("via") or {}).get("binding")}
    bindings.discard(None)
    row = st.meta("audience_keys")
    stored = set(json.loads(row)) if row else set()
    stored |= st.audience_keys()
    prefix_of = {source: prefix for prefix, source in _AUDIENCE_SOURCE.items()}
    need: set[str] = set()
    for bid in sorted(bindings):
        if bid not in schema.bindings:
            raise ValueError(f"lateness_binding_unknown:{bid}")   # fail closed
        tid, binding = schema.bindings[bid]
        source, relation = binding["source"], binding["relation"]
        prefix = prefix_of.get(source)
        if source == "portal_api":
            need.add(f"api:{relation}")
        elif source == "bc":
            # per company where the store holds that relation's companies; else the tool
            # asks whether the grant covers the whole family: only a family-wide pattern
            # grants the literal ``bc:*`` key
            company = {k for k in stored if k.startswith("bc:") and k.endswith(f"/{relation}")}
            need |= company or {"bc:*"}
        elif prefix is not None and prefix not in _ITEM_ACL_AUDIENCES:
            need |= {f"{prefix}:*"} | {k for k in stored if k.startswith(f"{prefix}:")}
        prefix_id = f"{tid}:{bid}:"
        need |= {r[0] for r in st.conn.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_type = ? AND "
            "substr(object_id, 1, ?) = ?", (tid, len(prefix_id), prefix_id))}
    return sorted(need)


# The audience prefixes of the record systems a lateness verdict decides on.
_DECIDING_AUDIENCES = ("api:", "bc:")


def _reader_judges_lateness(st: _Store, may_read: Callable[[str], bool] | None) -> bool:
    """Whether the run principal (``may_read``: its grant; None: not checked) reads every
    record-system audience (Portal ``api:``, ``bc:``: the status, lines, requested-by dates
    and receipts a verdict decides on) the ``po_delivery_window`` graph reads that the
    store holds records of (``_lateness_audiences``; a source with no record here blinds
    no verdict). A supplier promise read from a document source (mail, SharePoint: item
    permissions, gated per message) never decides late and is a stated caveat
    (``promises_unproven``), so it is not required here."""
    if may_read is None:
        return True
    row = st.meta("audience_keys")
    held = set(json.loads(row)) if row else set()
    held |= st.audience_keys()
    return all(may_read(key) for key in _lateness_audiences(st)
               if key in held and key.startswith(_DECIDING_AUDIENCES))


# A gap whose date is a supplier's promise says, to a reader who may not read every source
# a promise sits in (or when the run's own reader could not see every promise), that a
# later promise it cannot see may exist.
PROMISE_CAVEAT = "later_promise_may_exist"
PROMISE_CAVEAT_TEXT = " (a later promise could sit in mail you cannot open)"


def _promise_audiences(st: _Store) -> list[str]:
    """Every audience a supplier's promise may sit in: the stored supplier confirmations'
    and the mailboxes' (a promise is read from the carrying message too)."""
    row = st.meta("audience_keys")
    keys = {k for k in (json.loads(row) if row else ()) if isinstance(k, str)}
    keys |= st.audience_keys()
    found = {r[0] for r in st.conn.execute(
        "SELECT DISTINCT audience_key FROM objects WHERE object_type = ?",
        (po_status.CONFIRMATION_TYPE,))}
    return sorted(found | {k for k in keys if k.startswith("mail:")})


def _promise_caveat(gap: dict[str, Any] | None, unless: Sequence[str], always: bool) -> None:
    """Record on ``gap`` the audiences a reader must read for the promise it rests on to
    be every promise there is (``read_inbox`` adds the caveat for any other reader;
    ``always``: the run's own reader could not see every promise either)."""
    if gap is not None:
        gap["follow_up"]["promise_caveat"] = {"unless": sorted(set(unless)),
                                              "always": bool(always)}


def _caveat_all(gap: dict[str, Any] | None, *, unless: Sequence[str]) -> None:
    _promise_caveat(gap, unless, False)


def _line_cites(verdict: Any, chain: Any) -> list[Mapping[str, Any]]:
    """The cited per-item promise the chain's line is judged by, when its own promise
    decides (``impact_chain.Late.own_promise``)."""
    own = verdict.own_promise(chain.line)
    return list(own.cites) if own is not None else []


def _line_event(own: Any, chain: Any, required: date | None) -> dict[str, Any]:
    """The evidence trail of a per-line verdict: the PO line, how it was matched to the
    confirmation's item, its promised date and the order's required date."""
    if own is None or own.promised is None:
        return {}
    return {"line": chain.line, "matched_by": own.matched_by,
            "line_promised": own.promised.isoformat(),
            **({"required": required.isoformat()} if required is not None else {})}
