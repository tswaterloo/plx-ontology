"""Ask answers of the app API: shortcut questions, late and at-risk order answers, profit
and margin answers, claim and permission wording, trails' text checks and the question
understanding the answer shows.

Moved from ``operator_api.backend`` by concern (no behaviour change); every name is
re-exported there."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
import secrets
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote

from . import models as m
from . import presentation
from .test_records import hidden_words
from .serving import (FAMILY_OWNER, HOST_READ_SCHEMES, LATE_COUNT_LIMIT, LATE_LIST_LIMIT,
    MAX_FACTS, MAX_SOURCES_PER_FACT, object_url, open_url, plain, record_words, system_of,
    teams)


def aged_to_today(inbox: Mapping[str, Any], today: Optional[date] = None) -> dict[str, Any]:
    """The inbox's days late counted to today: the engine counted each order's days to the
    business day it ran (``run_at``), the trail and the record's page count them to today,
    so one order shows one count everywhere. ``days_as_of`` names the day counted to."""
    from operating.po_status import business_today

    stated = inbox.get("as_of") or inbox.get("run_at")   # the day the engine counted to
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(stated)):
            ran = date.fromisoformat(str(stated))
        else:
            ran = business_today(datetime.fromisoformat(str(stated).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return dict(inbox)
    today = today or business_today()
    shift = (today - ran).days
    if shift <= 0:
        return {**inbox, "days_as_of": ran.isoformat()}
    gaps = [{**g, "days_late": int(g["days_late"]) + shift}
            if isinstance(g, Mapping) and isinstance(g.get("days_late"), int) else g
            for g in inbox.get("gaps") or ()]
    return {**inbox, "gaps": gaps, "days_as_of": today.isoformat()}


def without_tests(inbox: Mapping[str, Any], labels: Mapping[str, str],
                  suppliers: Mapping[str, str], *, named: frozenset[str] = frozenset()
                  ) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """The reader's gap inbox without test and placeholder records
    (``operator_api.test_records``: a "TEST" supplier, a UAT- number, the
    deployment's list), judged on the record's label and its supplier's name; the late
    count drops each late order it leaves out."""
    from .test_records import is_test

    def test(gap: Mapping[str, Any]) -> bool:
        oid = gap.get("object_id")
        return isinstance(oid, str) and is_test((labels.get(oid),), named,
                                                (suppliers.get(oid),))
    gaps = list(inbox.get("gaps") or ())
    kept = [g for g in gaps if not test(g)]
    if len(kept) == len(gaps):
        return dict(inbox), dict(labels), dict(suppliers)
    dropped_late = {g.get("object_id") for g in gaps if g not in kept
                    and g.get("days_late") is not None}
    total = inbox.get("late_total")
    return ({**inbox, "gaps": kept, "tests_hidden": len(gaps) - len(kept),
             **({"late_total": max(0, int(total) - len(dropped_late))}
                if isinstance(total, int) else {})}, dict(labels), dict(suppliers))


def late_total(inbox: Mapping[str, Any]) -> int:
    """How many late purchase orders the reader's inbox lists (each order once)."""
    return len({g.get("object_id") for g in inbox.get("gaps") or ()
                if g.get("days_late") is not None and isinstance(g.get("object_id"), str)})


def supplier_of(view: Any, oid: str, card: Mapping[str, Any]) -> Optional[str]:
    """The supplier of a purchase order as this reader reads it: the order's own vendor
    name (a BC order carries it), else the display of the supplier its settled
    ``ordered_from`` link reaches (one supplier only: two are not picked between); None
    when neither is readable."""
    names = {plain(v.get("value")) for v in card.get("values") or ()
             if isinstance(v, Mapping) and v.get("property") == "vendor_name"} - {None, ""}
    if len(names) == 1:
        return str(next(iter(names)))[:200]
    if names:
        return None
    try:
        found = view._call("plx.around", {
            "from": oid, "tier_floor": "verified", "limit": 3,
            "path": [{"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]})
    except Exception:  # noqa: BLE001 - no supplier shown rather than a wrong one
        return None
    shown = {plain(o.get("display")) for o in found.get("objects") or ()
             if isinstance(o, Mapping)} - {None, ""}
    return str(next(iter(shown)))[:200] if len(shown) == 1 else None


def attention_items(inbox: Mapping[str, Any], labels: Mapping[str, str],
                    limit: int = LATE_LIST_LIMIT, *, suppliers: Mapping[str, str] | None = None
                    ) -> tuple[list[m.LatePo], list[m.AtRisk]]:
    """Home's late purchase orders and at-risk items from the reader's gap inbox, in the
    inbox's order (longest late first); the late-PO answer reads the same list with the
    same limit, so both show the same orders and the same count."""
    late: list[m.LatePo] = []
    risk: list[m.AtRisk] = []
    suppliers = suppliers or {}
    for gap in inbox.get("gaps") or ():
        oid, kind = gap.get("object_id"), str(gap.get("object_type") or "Object")
        if not isinstance(oid, str):
            continue
        label = labels.get(oid) or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind).capitalize()
        sources = [m.Source(system=system_of(h), handle=h, label=record_words(h),
                            open_url=open_url(h))
                   for h in (gap.get("citations") or ())[:3] if isinstance(h, str)]
        reason = str(gap.get("summary") or (gap.get("follow_up") or {}).get("why")
                     or "Needs attention")[:300]
        if gap.get("kind") == "conflicting_quantity" and rounded_alike(reason):
            continue   # 1257 and 1257.1666...: one quantity rounded two ways, no risk
        if gap.get("kind") == "conflicting_quantity":
            # an inbox built before the summary rounded its values: "(1257, 1257.1666 box)"
            # reads "(1257 box and 1257.17 box)", as the gap engine now writes it
            reason = re.sub(r"\((-?[\d.]+(?:, -?[\d.]+)+) ([^()\d][^()]*)\)$",
                            _quantities_in_words, reason)
        # "AssemblyOrder.ordered_boxes: ..." reads "Assembly order ordered boxes: ..."
        reason = re.sub(r"^([A-Z][A-Za-z]+)\.([a-z_]+)(?=:)", lambda f: (
            re.sub(r"(?<=[a-z])(?=[A-Z])", " ", f.group(1)).capitalize() + " "
            + f.group(2).replace("_", " ")), reason)
        if gap.get("days_late") is not None and any(p.object_id == oid for p in late):
            continue   # one order, one line
        if gap.get("days_late") is not None and len(late) < limit:
            amount, suspect = late_amount(gap)
            late.append(m.LatePo(object_id=oid, label=label, days_late=int(gap["days_late"]),
                                 supplier=suppliers.get(oid), reason=reason,
                                 due_date=need_date(gap), amount=amount,
                                 amount_suspect=suspect,
                                 confirmation=list_confirmation(gap),
                                 open_url=object_url(kind, oid), sources=sources))
        elif gap.get("days_late") is None and len(risk) < limit:
            risk.append(m.AtRisk(object_id=oid, label=label, kind=str(gap.get("kind")),
                                 reason=reason, open_url=object_url(kind, oid),
                                 sources=sources))
    return late, risk


# A late order's confirmation as the late list's own entry says it (the proactive engine's
# ``late_or_unconfirmed_po``): unconfirmed_po is raised only when no supplier confirmation
# is linked to the order at all. A late_po (overdue) has a link, firm or a lead, that this
# reader may not be allowed to see: nothing is said (the order's page decides it as the
# reader).
LIST_CONFIRMATION = {"unconfirmed_po": "none_found"}
# What the engine's late gap says of the order's confirmations, as this reader may read
# them (``proactive_engine._shown`` drops the word when the reader may not open one of the
# confirmations): linked to any copy of the order, found but unverified, or none at all.
CONFIRMATION_WORDS = frozenset({"none_found", "found_unverified", "needs_check", "confirmed"})


def list_confirmation(gap: Mapping[str, Any]) -> Optional[str]:
    said = (gap.get("follow_up") or {}).get("confirmation")
    if said in CONFIRMATION_WORDS:
        return str(said)
    return LIST_CONFIRMATION.get(str(gap.get("kind")))


_NEED_DATE = re.compile(r"\b(?:need date|needed by|confirmed date|lead-time due date|"
                        r"lead time on|due by) (\d{4}-\d{2}-\d{2})\b")


def need_date(gap: Mapping[str, Any]) -> Optional[str]:
    """A late order's requested-by date as the gap engine wrote it into its summary
    ("past its need date 2026-06-24"); None when the summary names no valid date."""
    found = _NEED_DATE.search(str(gap.get("summary") or ""))
    if not found:
        return None
    try:
        return date.fromisoformat(found.group(1)).isoformat()
    except ValueError:
        return None


def late_amount(gap: Mapping[str, Any]) -> tuple[Optional[m.Money], Optional[str]]:
    """A late order's total and currency as the inbox shows it to this reader (the
    engine drops a figure the reader may not open), with why it is suspect when it is
    (a suspect total is shown as such, never sorted or banded as fact)."""
    stated = (gap.get("event") or {}).get("amount")
    if not isinstance(stated, Mapping):
        return None, None
    money = _decimal_money(stated.get("value"), stated.get("currency"))
    if money is None:
        return None, None
    suspect = stated.get("suspect")
    return money, (str(suspect)[:300] if suspect else None)


def rounded_alike(reason: str) -> bool:
    """Whether a conflicting-quantity gap's values (its summary's "(1257, 1257.1666 box)"
    or "(1257 box and 1257.17 box)") are one quantity, rounded or kept as an integer
    (``answer_conflicts.same_quantity``, the gap's values being quantities)."""
    from operating.answer_conflicts import same_quantity

    found = re.search(r"\(([^()]*)\)\s*$", reason or "")
    if not found:
        return False
    numbers = re.findall(r"-?\d+(?:\.\d+)?", found.group(1))
    return len(numbers) >= 2 and same_quantity(numbers, quantity=True) is not None


def _quantities_in_words(found: re.Match) -> str:
    from decimal import Decimal, InvalidOperation
    from operating.proactive_gaps import shown_numbers
    try:
        numbers = [Decimal(n) for n in found.group(1).split(", ")]
    except InvalidOperation:
        return found.group(0)
    return "(" + " and ".join(f"{n} {found.group(2)}" for n in shown_numbers(numbers)) + ")"


def ask_shortcut(request: m.AskRequest) -> Optional[str]:
    """The Ask's route ahead of the fast path and the agent: ``late_list`` (the reader's
    late purchase orders, from the gap inbox), ``profit`` (a customer's or an order's
    margin, ``operating.customer_profit``), else None. One decision, used by ``ask`` and
    by the scoreboard (``scripts.eval_live_ops``), so the scoreboard measures the route
    an employee's question takes."""
    intent = picked_question(request) or request.question
    if not request.object_id and late_list_question(request.question):
        return "late_list"
    if not request.object_id and (orders_at_risk_question(request.question)
                                  or late_for_target(request.question)):
        return "at_risk"
    if (profit_question(intent) or order_margin_question(intent)) and (
            not request.object_id or customer_object(request.object_id)):
        return "profit"
    return None


def read_shortcut(request: m.AskRequest, reading: Any) -> Optional[str]:
    """The ready answer the front door's model picked for the question (``late_list``,
    ``at_risk``, ``profit``: ``operating.front_door.RECIPES``), under the same conditions
    on the record the request is about as ``ask_shortcut``'s; None: the fast path and the
    agent answer."""
    recipe = getattr(reading, "recipe", None)
    if recipe in ("late_list", "at_risk") and not request.object_id:
        return recipe
    if recipe == "profit" and (not request.object_id or customer_object(request.object_id)):
        return recipe
    return None


_AT_RISK = re.compile(r"\bat[\s-]+risk\b|\bheld\s+up\b|\bhold(?:s|ing)?\s+up\b|"
                      r"\baffect(?:ed|s)?\b|\bimpact(?:ed|s)?\b|\bdelay(?:ed|s)?\b|"
                      r"\bblock(?:ed|s)?\b", re.IGNORECASE)
_CUSTOMER_ORDERS = re.compile(r"\b(?:customer|sales|client)(?:'s)?\s+orders?\b|\bSOs\b",
                              re.IGNORECASE)
_LATE_SUPPLY = re.compile(r"\blate\b|\bslipp\w*|\bPOs?\b|\bpurchase\s+orders?\b|"
                          r"\bmaterials?\b|\bsuppl(?:y|ies|ier)\b", re.IGNORECASE)
# "what's late for SO 470", "which materials are late for Acme", "late materials on X"
_LATE_FOR = re.compile(
    r"(?:\bwhat(?:'s|\s+is|\s+are|s)?|\bwhich\s+(?:materials?|POs?|purchase\s+orders?|"
    r"items?|components?)(?:\s+(?:is|are))?|\bany(?:thing)?|\blate\s+(?:materials?|POs?|"
    r"purchase\s+orders?|items?|components?))\s+(?:late\s+|holding\s+up\s+)?"
    r"(?:for|on|holding\s+up)\s+(?P<target>[^?]{2,80}?)\s*[?.!]*$", re.IGNORECASE)


def orders_at_risk_question(question: str) -> bool:
    """Whether ``question`` asks which customer orders late supply puts at risk ("which
    customer orders are at risk from late POs", "sales orders held up by late materials")."""
    q = question or ""
    return bool(_CUSTOMER_ORDERS.search(q) and _AT_RISK.search(q) and _LATE_SUPPLY.search(q)
                and (_LATE_WORD.search(q) or _SLIP_WORD.search(q)))


_SLIP_WORD = re.compile(r"\bdelay(?:ed|s)?\b|\bslipp\w*|\bwaiting\b", re.IGNORECASE)
_LATE_WORD = re.compile(r"\blate\b|\bheld\s+up\b|\bholding\s+up\b|\bat[\s-]+risk\b|"
                        r"\boverdue\b|\bbehind\b", re.IGNORECASE)


def late_for_target(question: str) -> Optional[str]:
    """The order or customer a "what's late for X" question names (``X``), else None."""
    q = (question or "").strip()
    found = _LATE_FOR.search(q)
    if not found or not _LATE_WORD.search(q):   # "what is on SO 470" is not about lateness
        return None
    target = re.sub(r"^(?:the\s+)?(?:customer|client|order|sales\s+order)\s+", "",
                    found.group("target").strip(), flags=re.IGNORECASE)
    target = re.sub(r"\s+(?:right\s+now|now|today|rn)$", "", target, flags=re.IGNORECASE)
    return target.strip() or None


def late_list_question(question: str) -> bool:
    """Whether ``question`` asks only for the list of late purchase orders, typos and chat
    spellings included ("wat r the late pos", "any late pos rn?"); a qualified question (a
    supplier, a negation, a time frame, a record) goes to the agent, which reads and the
    gate checks (``mcp_portfolio.late_list_words``, plx.find's home mode reads the same)."""
    from operating.mcp_portfolio import late_list_words
    return late_list_words(question)


def _base(view: Any) -> Mapping[str, Any]:
    try:
        return view._base()
    except Exception:  # noqa: BLE001 - freshness then reads the ledger alone
        return {}


def words_of(code: Any) -> str:
    """A gap's snake_case subject or reason in words."""
    return re.sub(r"[_\s]+", " ", str(code or "")).strip()[:300]


LATE_RULE = "late_or_unconfirmed_po"


def late_rule_behind(inbox: Mapping[str, Any]) -> tuple[bool, bool]:
    """(the late-PO rule is re-running over newer data, its list is not known for the
    data served: re-running or not run)."""
    refreshing = inbox.get("refreshing")
    behind = LATE_RULE in refreshing if isinstance(refreshing, (list, tuple, set)) \
        else bool(refreshing)
    return behind, behind or LATE_RULE in (inbox.get("not_run") or {})


def late_answer(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                inbox: Mapping[str, Any], late: Sequence[m.LatePo], conversation: str,
                ms: Callable[[], int]) -> m.AnswerPayload:
    """The late-PO list as an answer: one fact per late purchase order (its gap's own
    evidence, each source decided for this reader again), longest late first."""
    as_of = inbox.get("days_as_of") or (inbox.get("run_at") if isinstance(
        inbox.get("run_at"), str) else None)
    facts = []
    gaps = [g for g in inbox.get("gaps") or () if isinstance(g, Mapping)
            and isinstance(g.get("object_id"), str)]
    days_of = {g["object_id"]: (g.get("event") or {}).get("days") for g in gaps
               if g.get("kind") == "order_at_risk"}
    holds: dict[str, list[str]] = {}      # late PO -> the customer orders it holds up
    for order, pos in held_up_by(gaps).items():
        for po_id in pos:
            holds.setdefault(po_id, []).append(order)
    impact = {g["object_id"]: g for g in gaps if g.get("kind") == "po_impact"}
    labels: dict[str, str] = {}
    for po in late:
        sources = [backend._source(view, s.handle) for s in po.sources]
        orders = sorted(holds.get(po.object_id) or (), key=lambda o: -int(days_of.get(o) or 0))
        held = ""
        if orders:
            held = " Holds up customer order" + ("s " if len(orders) > 1 else " ") + ", ".join(
                f"{_display_of(view, o, labels) or 'an order'}"
                + (f" ({days_of[o]} days at risk)" if days_of.get(o) is not None else "")
                for o in orders[:4]) + "."
            sources += [backend._source(view, h) for h in (impact.get(po.object_id) or {}).get(
                "citations") or () if isinstance(h, str)][:MAX_CHAIN_SOURCES]
        elif po.object_id in impact:
            # an older finding that names no order: what it found, without guessing which
            event = impact[po.object_id].get("event") or {}
            n, days = event.get("orders_affected"), event.get("days")
            if isinstance(n, int) and n > 0:
                held = (f" Holds up {n} customer order{'s' if n != 1 else ''}"
                        + (f" (worst {days} days at risk)" if days is not None else "") + ".")
        reason = po.reason if re.search(r"[.!?]$", po.reason) else po.reason + "."
        facts.append(m.Fact(text=f"{po.label}: {po.days_late} days late. {reason}{held}"[:1000],
                            sources=sources, check="computed_from_cited_records"))
    when = f" as of {as_of[:10]}" if as_of else ""
    total = len(late)
    # the late-PO rule's findings are of older data than served (being re-checked), or
    # the rule did not run: no list is known, so "none is late" would be a false answer
    behind, unknown = late_rule_behind(inbox)
    if inbox.get("status") != "read":
        direct = ("The late-order check has not run for your records yet, so I can't list "
                  "late purchase orders.")
        outcome = "not_found"
    elif not late and unknown:
        checked = inbox.get("checked_on")
        direct = (("The late-order check is being re-run on the newest data"
                   + (f" (last checked {str(checked)[:10]})" if isinstance(checked, str)
                      else "")) if behind else
                  "The late-order check did not run on the newest data") + \
            ", so I can't list late purchase orders yet."
        outcome = "not_found"
    elif not late:
        direct = f"No purchase order you can see is late{when}."
        outcome = "answered"
    else:
        total = max(len(late), int(inbox.get("late_total") or late_total(inbox)))
        named = "; ".join(f"{po.label}" + (f" from {po.supplier}" if po.supplier else "")
                          + f" ({po.days_late} days)" for po in late[:3])
        # the list's order said without a second figure beside the late count: "12 of 23
        # are late" would read as a second answer to "how many are late"
        shown = ("All of them are listed below, oldest due date first." if total == len(late)
                 else "The first one is listed below." if len(late) == 1
                 else f"The first {len(late)} are listed below, oldest due date first.")
        checked = inbox.get("checked_on")
        last = f" ({str(checked)[:10]})" if isinstance(checked, str) else ""
        direct = (f"{total} purchase order{'s' if total != 1 else ''} you can see "
                  f"{'are' if total != 1 else 'is'} late"
                  + (f" at the last completed late-order check{last}" if unknown else when)
                  + f". Longest late: {named}. {shown}"
                  + (" The check is re-running on newer data now, so this can change."
                     if unknown else ""))
        outcome = "answered"
    return m.AnswerPayload(
        answer_id=secrets.token_hex(12), question=request.question, outcome=outcome,
        direct_answer=direct[:4000], facts=facts[:MAX_FACTS], not_found=[],
        hidden=m.Hidden(count=0, by_owner=[], message=None), conflicts=[],
        freshness=backend.freshness(inbox),
        subjects=[m.ObjectRef(object_id=po.object_id, type="PurchaseOrder",
                              label=po.label[:200], open_url=po.open_url)
                  for po in late[:MAX_FACTS]],
        did_you_mean=[], conversation_id=conversation, latency_ms=ms(),
        list_total=total if late and total > len(late) else None)


ORDER_TWIN_LINK = "SalesOrder.same_record_as"
AT_RISK_KINDS = ("order_at_risk", "po_impact")
MAX_CHAIN_SOURCES = 12


def at_risk_gaps(view: Any) -> Optional[list[Mapping[str, Any]]]:
    """The reader's ``order_at_risk`` and ``po_impact`` gaps, read by their kinds (past the
    inbox's first page); None for a workspace that cannot read by kind."""
    try:
        got = view.gap_inbox(limit=LATE_COUNT_LIMIT, kinds=AT_RISK_KINDS, include_off=True)
    except TypeError:
        return None
    return [g for g in got.get("gaps") or () if isinstance(g, Mapping)]


def held_up_by(gaps: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Customer order -> the late purchase orders holding it up, joined from the chain ends
    the reader's own ``order_at_risk`` and ``po_impact`` gaps name (their events' ``pos``
    and ``orders``); an end is kept only when the reader's inbox shows that record's own
    gap, so a record the reader may not see is never named."""
    shown = {g.get("object_id") for g in gaps}
    feeds: dict[str, set[str]] = {}
    for gap in gaps:
        event = gap.get("event") if isinstance(gap.get("event"), Mapping) else {}
        if gap.get("kind") == "order_at_risk":
            feeds.setdefault(gap["object_id"], set()).update(
                p for p in event.get("pos") or () if p in shown)
    for gap in gaps:
        event = gap.get("event") if isinstance(gap.get("event"), Mapping) else {}
        if gap.get("kind") == "po_impact":
            for order in event.get("orders") or ():
                if order in feeds:
                    feeds[order].add(gap["object_id"])
    return feeds


def _name_forms(name: str) -> tuple[str, ...]:
    """A customer's display and, when it carries one, the name before its parenthetical
    ("Acme Supply (ACME CO.)": also "Acme Supply") and the name inside it."""
    found = re.match(r"^\s*(.+?)\s*\(([^()]+)\)\s*$", name or "")
    return (name, found.group(1), found.group(2)) if found else (name,)


def _display_of(view: Any, oid: str, cache: dict[str, str]) -> str:
    if oid not in cache:
        card = view._call("plx.object", {"object_id": oid}) or {}
        cache[oid] = (plain((card.get("object") or {}).get("display")) or "") \
            if not card.get("error") else ""
    return cache[oid]


def _linked(view: Any, oid: str, link: str, direction: str) -> list[str]:
    got = view._call("plx.around", {"from": oid, "limit": 20, "path": [
        {"link_type": link, "direction": direction}]}) or {}
    return [o["object_id"] for o in got.get("objects") or ()
            if isinstance(o, Mapping) and isinstance(o.get("object_id"), str)]


def at_risk_answer(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                   inbox: Mapping[str, Any], conversation: str, ms: Callable[[], int]
                   ) -> Optional[m.AnswerPayload]:
    """The customer orders at risk from late purchase orders the reader may see, worst
    first: each order with its customer, its days at risk, the late PO(s) its chain starts
    from and the chain's cited records as its sources. A PO or an order is named only when
    the reader's inbox shows that record's own gap (a chain end the reader may not see is
    never named). With an order or customer named (``late_for_target``), only that one's;
    None when it has none (the agent path walks its materials live)."""
    from operating.customer_profit import normal_name

    gaps = [g for g in inbox.get("gaps") or () if isinstance(g, Mapping)
            and isinstance(g.get("object_id"), str)]
    risky = [g for g in gaps if g.get("kind") == "order_at_risk"]
    feeds = held_up_by(gaps)
    target = late_for_target(request.question)
    if target and not risky:
        return None      # nothing at risk to narrow: the next path reads the record named
    labels: dict[str, str] = {}
    held: dict[str, list[str]] = {}

    def customers_of(oid: str) -> list[str]:
        """The customers of the order and its twins, read once per order when needed."""
        if oid not in held:
            held[oid] = [c for o in [oid, *_linked(view, oid, ORDER_TWIN_LINK, "forward"),
                                     *_linked(view, oid, ORDER_TWIN_LINK, "reverse")]
                         for c in _linked(view, o, CUSTOMER_ORDERS, "forward")]
        return held[oid]
    if target:
        # the order or customer as the at-risk orders' own labels read (exact after
        # normalising): no search; else the reader's plx.find names it
        number = re.sub(r"^(?:s\.?o\.?|sales\s+order|customer\s+order|order)\s*(?:no\.?|"
                        r"number|#)?\s*", "", target, flags=re.IGNORECASE).strip().casefold()
        direct = [g for g in risky if number and (
            _display_of(view, g["object_id"], labels).strip().casefold() == number
            or normal_name(target) in {
                normal_name(form) for c in customers_of(g["object_id"])
                for form in _name_forms(_display_of(view, c, labels))})]
        if direct:
            risky = direct
            target = None
    if target:
        found = view._call("plx.find", {"query": target, "limit": 8}) or {}
        hits = [c["object"] for c in found.get("candidates") or ()
                if isinstance(c.get("object"), Mapping)]
        orders = {h["object_id"] for h in hits if h.get("object_type") == "SalesOrder"}
        orders |= {t for o in list(orders) for d in ("forward", "reverse")
                   for t in _linked(view, o, ORDER_TWIN_LINK, d)}
        wanted = normal_name(target)
        named = {h["object_id"] for h in hits if h.get("object_type") == "Customer"
                 and wanted and wanted in {normal_name(plain(h.get("display"))),
                                           normal_name(_display_of(view, h["object_id"],
                                                                   labels))}}
        risky = [g for g in risky if g["object_id"] in orders
                 or named.intersection(customers_of(g["object_id"]))]
        if not risky:
            return None
    risky.sort(key=lambda g: (-int((g.get("event") or {}).get("days") or 0),
                              g["object_id"]))
    as_of = inbox.get("days_as_of") or (inbox.get("run_at") if isinstance(
        inbox.get("run_at"), str) else None)
    when = f" as of {as_of[:10]}" if as_of else ""
    facts, subjects, named_lines = [], [], []
    for gap in risky[:MAX_FACTS]:
        oid = gap["object_id"]
        label = "Customer order " + (_display_of(view, oid, labels) or "").strip()
        who = sorted({n for c in customers_of(oid) if (n := _display_of(view, c, labels))})
        pos = sorted(feeds.get(oid) or ())
        po_words = ", ".join(f"PO {_display_of(view, p, labels) or p}" for p in pos)
        days = (gap.get("event") or {}).get("days")
        head = (f"{label}" + (f" ({' / '.join(who)})" if who else "")
                + (f" is {days} days at risk" if days is not None else " is at risk")
                + (f" from late {po_words}" if po_words else ""))
        why = str((gap.get("follow_up") or {}).get("why") or "")
        text = f"{head}. {gap.get('summary') or ''}. {why}".replace("..", ".")
        handles = [h for h in dict.fromkeys(gap.get("citations") or ())
                   if isinstance(h, str)][:MAX_CHAIN_SOURCES]
        facts.append(m.Fact(text=text[:1000], check="computed_from_cited_records",
                            sources=[backend._source(view, h) for h in handles]))
        subjects.append(m.ObjectRef(object_id=oid, type="SalesOrder", label=label[:200],
                                    open_url=object_url("SalesOrder", oid)))
        named_lines.append(head)
    status = (inbox.get("detectors") or {}).get("orders_at_risk")
    not_found = []
    if status == "truncated":
        not_found.append(m.NotFound(
            what="The last check of late purchase orders against customer orders did not "
                 "walk every purchase order (it is bounded), so more orders may be at risk",
            where_it_would_live="Purchasing: the late purchase orders list"))
    if not risky:
        if inbox.get("status") != "read" or status in (None, "not_run", "blocked", "failed",
                                                       "not_configured"):
            direct = ("The check of late purchase orders against customer orders has not "
                      "run for your records, so I can't say which orders are at risk.")
            outcome = "not_found"
        else:
            direct = (f"No customer order you can see is at risk from a late purchase "
                      f"order{when}" + (" in the orders the check covered" if status ==
                                        "truncated" else "") + ".")
            outcome = "answered"
    else:
        n = len(risky)
        direct = (f"{n} customer order{'s' if n != 1 else ''} you can see "
                  f"{'are' if n != 1 else 'is'} at risk from late purchase orders{when}: "
                  + "; ".join(named_lines[:3]) + (f"; and {n - 3} more" if n > 3 else "")
                  + ". Days at risk are a lower bound (no assembly lead time is recorded); "
                  "each chain's records are cited below.")
        outcome = "partial" if not_found else "answered"
    return m.AnswerPayload(
        answer_id=secrets.token_hex(12), question=request.question, outcome=outcome,
        direct_answer=direct[:4000], facts=facts, not_found=not_found,
        hidden=m.Hidden(count=0, by_owner=[], message=None), conflicts=[],
        freshness=backend.freshness(inbox), subjects=subjects, did_you_mean=[],
        conversation_id=conversation, latency_ms=ms())


# How the app words a did-you-mean pick (app/src/app/(app)/ask/page.tsx ``onPick``): the
# picked record goes as the request's ``object_id``.
PICK_PREFIX = "i mean "


def picked_question(request: m.AskRequest) -> Optional[str]:
    """The question a did-you-mean pick answers: with a picked record (``object_id``) and
    the pick's wording, the person's latest earlier question that is not itself a pick;
    else None (not a pick, or nothing earlier to carry)."""
    if not request.object_id or not request.question.strip().casefold().startswith(
            PICK_PREFIX):
        return None
    earlier = [turn.text.strip() for turn in request.history if turn.role == "user"
               and turn.text.strip()
               and not turn.text.strip().casefold().startswith(PICK_PREFIX)]
    return earlier[-1] if earlier else None


def customer_object(object_id: str) -> bool:
    return str(object_id).split(":", 1)[0] == "Customer"


def profit_question(question: str) -> bool:
    from operating.customer_profit import profit_question as asks
    return asks(question)


def order_margin_question(question: str) -> bool:
    from operating.customer_profit import order_margin_question as asks
    return asks(question)


def _money(amount: Any, currency: Any) -> Optional[m.Money]:
    if amount is None or not isinstance(currency, str) or not _CURRENCY.match(currency):
        return None
    from operating.customer_profit import _plain
    return m.Money(amount=_plain(amount), currency=currency)


def _decided_sources(backend: "WorkspaceBackend", view: Any, handles: Iterable[Any]
                     ) -> list[m.Source]:
    """Sources for the handles this reader may open now (a withheld one is left out,
    never named)."""
    handles = [h for h in dict.fromkeys(handles) if isinstance(h, str)][:MAX_SOURCES_PER_FACT]
    decided = view.access.decide(handles) if handles else {}
    return [backend._source(view, h) for h in handles if decided.get(h) is None]


# Same-named Business Central cards with orders a margin answer reads together at most
# (more: each is offered as a choice).
MAX_CARDS_TOGETHER = 4


def with_orders(view: Any, customers: Sequence[Mapping[str, Any]]
                ) -> list[Mapping[str, Any]]:
    """The customers of ``customers`` that may have sales orders for this reader: an order
    it reads, any it may not (a ``withheld`` count) or a read that failed. A card with no
    order at all has no margin to state, so it is never a choice beside one that has."""
    out = []
    for customer in customers:
        got = view._call("plx.around", {
            "from": customer["object_id"], "limit": 1,
            "path": [{"link_type": CUSTOMER_ORDERS, "direction": "reverse"}]}) or {}
        withheld = got.get("withheld")
        hidden = any(isinstance(v, int) and v > 0 for v in withheld.values()) \
            if isinstance(withheld, Mapping) else bool(withheld)
        if got.get("error") or got.get("objects") or hidden \
                or (got.get("completeness") or {}).get("next_cursor"):
            out.append(customer)
    return out or list(customers)


def cards_linked(view: Any, ids: Sequence[str]) -> bool:
    """Whether an identity link (``same_record_as``, as this reader reads it) makes these
    cards one record; a failed read is no link."""
    from operating.why_trail import same_records
    try:
        return len(ids) > 1 and same_records(view._call, list(ids))
    except Exception:  # noqa: BLE001 - undecided: shown apart
        return False


def customer_choices(view: Any, candidates: Sequence[Mapping[str, Any]]
                     ) -> list[m.ObjectRef]:
    """Each customer of ``candidates`` (at most 8) as a choice named by its customer number
    as this reader reads it."""
    choices = []
    for c in candidates[:8]:
        detail = customer_hint(view, c["object_id"]) or [c.get("lineage") or "Customer"]
        choices.append(m.ObjectRef(
            object_id=c["object_id"], type="Customer",
            label=f"{plain(c.get('display')) or c['object_id']} ({' · '.join(detail)})"[:200],
            open_url=object_url("Customer", c["object_id"])))
    return choices


def profit_choices(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                   candidates: Sequence[Mapping[str, Any]], conversation: str,
                   ms: Callable[[], int], *, unlinked: bool = False) -> m.AnswerPayload:
    """Several customers match the name: each a choice with its customer number (read as
    this reader), never merged or picked. ``unlinked``: they share the exact name and no
    identity link makes them one record, so their margins are not added together."""
    choices = customer_choices(view, candidates)
    direct = (f"Did you mean {choices[0].label}?" if len(choices) == 1 else
              f"{len(choices)} customers have that exact name and nothing links them as "
              "one customer, so their margins are not added together. Which did you mean?"
              if unlinked else
              "That name matches more than one customer. Which did you mean?")
    return m.AnswerPayload(
        answer_id=secrets.token_hex(12), question=request.question, outcome="partial",
        direct_answer=direct,
        hidden=m.Hidden(count=0, by_owner=[], message=None), freshness=backend.freshness(),
        did_you_mean=choices, conversation_id=conversation, latency_ms=ms())


def customer_hint(view: Any, oid: str) -> list[str]:
    """What tells two customers of one name apart, as this reader reads them: the
    customer number, its city when the record carries one, and its last order's date
    with its order count."""
    card = view._call("plx.object", {"object_id": oid,
                                     "properties": ["erp_code", "city", "namecity"]}) or {}
    values = {v.get("property"): plain(v.get("value")) for v in card.get("values") or ()
              if isinstance(v, Mapping)}
    out = [x for x in (values.get("erp_code"), values.get("city") or values.get("namecity"))
           if x]
    try:
        handle, orders = _related(view, oid, CUSTOMER_ORDERS, pages=1)
        if handle is not None and orders:
            dates = sorted(str(plain((g.get("key") or {}).get("order_date")) or "") for g in
                           _grouped(view, handle, {"op": "count"}, ["order_date"]) or ())
            last = next((d for d in reversed(dates) if d), None)
            total = next((int(g["value"]) for g in _grouped(view, handle, {"op": "count"})
                          or () if str(g.get("value") or "").isdigit()), None)
            count = f"{total} orders" if total is not None else f"{len(orders)}+ orders"
            out.append(f"last order {last[:10]}, {count}" if last else count)
    except (KeyError, LookupError):   # a host serving no traversal or aggregate tools
        pass
    return out


GAP_OWNER = {"LINE_NOT_INVOICED": "Finance: post the order's invoice in Business Central",
             "COST_NOT_YET_ACTUAL": "Finance: the cost is booked once the shipment is invoiced",
             "COST_NOT_VALUED": "Finance: the item's cost in Business Central (its unit cost "
                                "or cost adjustment)",
             "NO_QUOTE_LINK": "Sales: the Portal quote the order was converted from",
             "MATERIAL_SOURCE_NOT_LINKED": "Operations: the item's bill of materials",
             "LABOUR_OVERHEAD_NOT_RECORDED": "Finance: production cost reports outside the "
                                             "BC API"}


def _blocking_words(blocking: Sequence[tuple[str, str]]) -> str:
    """"2 invoiced orders have no cost value in BC (SO-1, SO-2)", one clause per reason."""
    by: dict[str, list[str]] = {}
    for number, why in blocking:
        by.setdefault(why, []).append(number)
    verbs = {"has ": "have ", "is ": "are ", "rests ": "rest "}
    out = []
    for why, numbers in by.items():
        many = len(numbers) != 1
        said = next((verbs[k] + why[len(k):] for k in verbs
                     if many and why.startswith(k)), why)
        out.append(f"{len(numbers)} invoiced order{'s' if many else ''} {said} ("
                   + ", ".join(numbers[:6]) + (", ..." if len(numbers) > 6 else "") + ")")
    return "; ".join(out)


def profit_answer(backend: "WorkspaceBackend", view: Any, request: m.AskRequest,
                  profit: Mapping[str, Any], conversation: str,
                  ms: Callable[[], int]) -> m.AnswerPayload:
    """``operating.customer_profit.profitability`` as the contract's answer: the totals and
    the worst orders as facts, the cost breakdown, the drivers, the trail, the gaps as not
    found and the withheld orders as a count."""
    from operating.customer_profit import (MARGIN_BASIS, MARGIN_NOTE, NO_MARGIN_YET,
                                           SAGE_MARGIN_NOTE, money_words, open_words,
                                           not_read_words, read_words, _plain)

    blocking_words = _blocking_words

    def excluded_words(totals: Mapping[str, Any]) -> str:
        n = len(totals.get("excluded") or ())
        open_ = profit.get("open_orders") or {}
        value = (" (" + " and ".join(money_words(v, c) for c, v in open_["value"].items())
                 + " before tax" + ("" if open_.get("valued") == n else
                                    f", over the {open_.get('valued')} that state a value")
                 + ")") if n and open_.get("value") else ""
        return (f" {n} order{'s' if n != 1 else ''} not yet invoiced, excluded{value}." if n
                else "")

    cust = profit["customer"]
    totals = profit["totals"]
    cur = totals.get("currency")
    breakdown = profit.get("breakdown")
    customer_ref = m.ObjectRef(object_id=cust["object_id"], type="Customer",
                               label=str(cust["name"])[:200],
                               open_url=object_url("Customer", cust["object_id"]))

    def order_ref(order: Mapping[str, Any]) -> m.ObjectRef:
        return m.ObjectRef(object_id=order["object_id"], type="SalesOrder",
                           label=str(order["number"])[:200],
                           open_url=object_url("SalesOrder", order["object_id"]))

    def order_handles(order: Mapping[str, Any]) -> list[str]:
        return [h for line in order["lines"] for h in line.invoice_handles[:1]
                + line.ledger_handles[:1]] + ([order["handle"]] if order["handle"] else [])

    facts: list[m.Fact] = []
    period = profit["period"]["words"]
    mixed = sorted({o["currency"] for o in profit["orders"] if o["decided"] and o["currency"]}
                   ) if any(g.get("code") == "MIXED_CURRENCY" for g in profit.get("gaps") or ()
                            ) else []
    counted, in_period, read = totals["counted"], totals["orders"], totals["read"]
    exact = profit.get("coverage", {}).get("count_exact", True)
    basis = profit["period"]["basis"]
    # Not every order of the period read (the read limit or the time budget): the answer
    # says first that it is partial, and each order's figure is worded as one of those read,
    # never as the customer's figure.
    partial = (f"Partial answer: {read} of {'' if exact else 'at least '}{in_period} orders "
               "read." if not totals.get("complete") else None)
    if not totals.get("complete"):
        headline = (f"I can't give a customer total for {cust['name']} in {period}: it has "
                    f"{'' if exact else 'at least '}{in_period} orders in the period and "
                    f"only {read_words(profit.get('coverage') or {}, read)} were read "
                    f"({not_read_words(in_period, read, exact)}); {basis}.")
    elif totals.get("blocking"):
        # No verdict and no customer total while an invoiced order of the
        # period has no checked margin; the checked orders are still ranked (partial).
        headline = (f"I can't give a verdict for {cust['name']} in {period}: "
                    + blocking_words(totals["blocking"]) + "."
                    + excluded_words(totals)
                    + ((" The checked order is ranked below (partial)." if counted == 1
                        else f" The {counted} checked orders are ranked below (partial).")
                       if counted else ""))
    elif totals.get("margin") is not None:
        verdict = "profitable" if totals["margin"] > 0 else "not profitable"
        headline = (f"{cust['name']} is {verdict} in {period}: revenue "
                    f"{money_words(totals['revenue'], cur)}, item cost "
                    f"{money_words(totals['booked_cost'], cur)}, margin {MARGIN_BASIS}: "
                    f"{money_words(totals['margin'], cur)}"
                    + (f" ({_plain(totals['margin_pct'])}%)" if totals.get("margin_pct")
                       is not None else "")
                    + f" over {counted} invoiced order{'s' if counted != 1 else ''}; "
                    f"{basis}." + excluded_words(totals) + " " + MARGIN_NOTE)
        decided = [o for o in profit["orders"] if o["decided"]]
        facts.append(m.Fact(text=headline, check="computed_from_cited_records",
                            sources=_decided_sources(backend, view, [
                                h for o in decided for h in order_handles(o)])))
    elif in_period and totals.get("excluded") and not counted:
        # nothing invoiced: said plainly, with what is open and why there is no margin yet
        open_ = profit.get("open_orders")
        said = open_words(open_)
        headline = (f"{cust['name']} has no margin yet in {period}: none of its "
                    f"{in_period} order{'s' if in_period != 1 else ''} you can see is "
                    "invoiced" + (f" ({said})" if said else "") + ". " + NO_MARGIN_YET)
        if open_ and open_.get("value"):
            facts.append(m.Fact(
                text=f"Open orders not yet invoiced: {said}. " + "; ".join(open_["steps"]),
                check="computed_from_cited_records",
                sources=_decided_sources(backend, view, open_["handles"])))
    elif mixed:
        # every invoiced order has a checked margin, but in different currencies: nothing
        # is converted, so no single total and no verdict; each order in its own currency
        headline = (f"I can't give a verdict for {cust['name']} in {period}: its orders are "
                    f"in different currencies ({' and '.join(mixed)}), so no single total. "
                    f"The {counted} checked order{'s are' if counted != 1 else ' is'} ranked "
                    "below within each currency." + excluded_words(totals))
    elif in_period:
        headline = (f"I can't state a margin for {cust['name']} in {period}: none of its "
                    f"{in_period} order{'s' if in_period != 1 else ''} you can see has a "
                    "checked margin yet (what is missing is listed below)."
                    + excluded_words(totals))
    else:
        headline = f"{cust['name']} has no order you can see in {period}."
    for order in [o for o in profit["orders"] if o["decided"]][:5]:
        loss = order["margin"] < 0
        oc = order["currency"]          # the order's own checked currency
        text = (f"Order {order['number']}"
                + (f" (partial answer: one of {read} of {'' if exact else 'at least '}"
                   f"{in_period} orders read)" if partial else "")
                + f": revenue {money_words(order['revenue'], oc)}, "
                f"item cost {money_words(order['booked_cost'], oc)}, "
                + (f"loss {MARGIN_BASIS} {money_words(-order['margin'], oc)}" if loss else
                   f"margin {MARGIN_BASIS} {money_words(order['margin'], oc)}") + ".")
        facts.append(m.Fact(text=text, check="computed_from_cited_records",
                            sources=_decided_sources(backend, view, order_handles(order))))
    for driver in profit.get("drivers") or ():
        facts.append(m.Fact(text=driver["text"], check="computed_from_cited_records",
                            sources=_decided_sources(backend, view, driver["handles"])))
    facts = [f for f in facts if f.sources][:MAX_FACTS]

    worst = next((o for o in profit["orders"] if o["decided"]), None)
    parts: list[str] = []          # the focus order and why: after the headline, or first
    if breakdown is not None and breakdown["order"].margin is not None:
        order = breakdown["order"]
        lost = order.margin < 0
        worst_word = ("Worst of the checked orders" if totals.get("blocking") else
                      "Worst" if totals.get("complete") else
                      f"Worst of {read_words(profit.get('coverage') or {}, read)} read")
        parts.append(f"{worst_word if worst and worst['object_id'] == order.object_id else 'Order'}"
                     f" {order.number}: revenue {money_words(order.revenue, order.currency)}, "
                     f"item cost {money_words(order.cost, order.currency)}, "
                     + (f"loss {MARGIN_BASIS} {money_words(-order.margin, order.currency)}"
                        if lost else
                        f"margin {MARGIN_BASIS} {money_words(order.margin, order.currency)}")
                     + (f" ({_plain(order.margin * 100 / order.revenue)}%)." if order.revenue
                        else "."))
        if totals.get("margin") is None:
            parts.append(SAGE_MARGIN_NOTE if order.sage else MARGIN_NOTE)
    elif breakdown is not None:
        order = breakdown["order"]
        why = ", ".join(dict.fromkeys(words_of(c).lower() for c, _ in order.gaps))[:300]
        parts.append(f"{order.number}: no margin is stated" + (f" ({why})." if why else "."))
    drivers = [d["text"] for d in profit.get("drivers") or ()]
    if drivers:
        parts.append("Why: " + " ".join(drivers[:3]))
    # the order asked about first, then its customer; else the customer first
    parts = parts + [headline] if profit.get("focus") else [headline] + parts
    if partial:
        parts.insert(0, partial)
    items = profit.get("items") or {}
    products = profit.get("products") or []
    if len(products) > 1:
        parts.append("By product, worst first" + ("" if all(p["complete"] for p in products)
                                                  else " (checked orders only)") + ": "
                     + "; ".join(product_words(p, items) for p in products[:3]) + ".")
    action = profit.get("next_action")
    if action:
        parts.append(f"Suggested next step: {action['text']} (a suggestion only; nothing was "
                     "changed.)")
    direct = " ".join(parts)

    not_found: list[m.NotFound] = []
    seen_codes: set[str] = set()
    gaps = list(profit.get("gaps") or ())
    gaps += [{"code": g["code"], "detail": f"{o['number']}: {g['detail']}"}
             for o in profit["orders"] if not o["decided"] for g in o["gaps"][:2]]
    gaps += list((breakdown or {}).get("gaps") or ())
    for gap in gaps:
        key = f"{gap['code']}:{gap['detail']}"
        if key in seen_codes:
            continue
        seen_codes.add(key)
        not_found.append(m.NotFound(
            what=words_of(gap["code"]).capitalize() + ": " + str(gap["detail"])[:400],
            where_it_would_live=GAP_OWNER.get(gap["code"], "Finance (Business Central owners)")))
    withheld = int(profit.get("withheld") or 0)
    messages = []
    if withheld:
        messages.append(f"The figures of {withheld} order{'s' if withheld != 1 else ''} of "
                        "this customer are withheld from you; they are not in the totals.")
    if profit.get("orders_trimmed"):
        messages.append("This customer has orders you can't open; they are not in the "
                        "figures.")
    # Orders the tools trimmed are never counted (plx.around never reveals how many).
    hidden = m.Hidden(count=None if profit.get("orders_trimmed") else withheld,
                      by_owner=[m.HiddenOwner(owner_or_team=FAMILY_OWNER["BC"],
                                              count=withheld)] if withheld else [],
                      message=" ".join(messages) or None)

    view_model = profitability_model(backend, view, profit)
    steps, hidden_steps = [], 0
    for step in profit.get("trail") or ():
        handle = step.get("handle")
        source = None
        if isinstance(handle, str):
            if view.access.decide([handle]).get(handle) is not None:
                hidden_steps += 1      # a step resting on a record the reader can't open
                continue
            source = backend._source(view, handle)
        check = {"cited": "read_from_record"}.get(
            step.get("check"), step.get("check") or "computed_from_cited_records")
        steps.append(m.TrailStep(
            kind="verdict" if step["label"] == "Driver" else "claim", at=step.get("date"),
            title=str(step["value"] if step["label"] == "Driver" else step["label"])[:300],
            detail=(str(step["value"])[:600] if step.get("value") and step["label"] != "Driver"
                    else None), source=source, check=check))
    trail = m.EvidenceTrail(
        kind="claims", title=f"Evidence for {cust['name']}'s margin in {period}"[:200],
        steps=steps[:40], hidden_steps=hidden_steps,
        hidden_message=(f"{hidden_steps} more step{'s' if hidden_steps != 1 else ''} in "
                        "places you can't open.") if hidden_steps else None) if steps else None
    subjects = [customer_ref] + ([order_ref({"object_id": breakdown["order"].object_id,
                                             "number": breakdown["order"].number})]
                                 if breakdown else [])
    outcome = ("answered" if totals.get("margin") is not None and not not_found
               else "partial" if facts else "not_found")
    return m.AnswerPayload(
        answer_id=secrets.token_hex(12), question=request.question, outcome=outcome,
        direct_answer=direct[:4000], facts=facts, not_found=not_found[:10], hidden=hidden,
        freshness=backend.freshness(_base(view)), subjects=subjects,
        conversation_id=conversation, latency_ms=ms(), trail=trail, profitability=view_model)


def profitability_model(backend: "WorkspaceBackend", view: Any,
                        profit: Mapping[str, Any]) -> m.Profitability:
    """``customer_profit.profitability`` as the contract's ``Profitability``: totals, the
    ranking, margin per product, the focus order's breakdown and drivers, the next step;
    every source decided for this reader."""
    from operating.customer_profit import (MARGIN_BASIS, MARGIN_NOTE, not_read_words,
                                          read_words, _plain)

    cust, totals = profit["customer"], profit["totals"]
    cur = totals.get("currency")
    period = profit["period"]["words"]
    basis = profit["period"]["basis"]
    breakdown = profit.get("breakdown")
    counted, in_period, read = totals["counted"], totals["orders"], totals["read"]
    exact = profit.get("coverage", {}).get("count_exact", True)
    mixed = sorted({o["currency"] for o in profit["orders"] if o["decided"] and o["currency"]}
                   ) if any(g.get("code") == "MIXED_CURRENCY" for g in profit.get("gaps") or ()
                            ) else []
    withheld = int(profit.get("withheld") or 0)
    drivers = [d["text"] for d in profit.get("drivers") or ()]
    items = profit.get("items") or {}
    products = profit.get("products") or []
    action = profit.get("next_action")
    customer_ref = m.ObjectRef(object_id=cust["object_id"], type="Customer",
                               label=str(cust["name"])[:200],
                               open_url=object_url("Customer", cust["object_id"]))

    def order_ref(order: Mapping[str, Any]) -> m.ObjectRef:
        return m.ObjectRef(object_id=order["object_id"], type="SalesOrder",
                           label=str(order["number"])[:200],
                           open_url=object_url("SalesOrder", order["object_id"]))

    def blocking_words(blocking: Sequence[tuple[str, str]]) -> str:
        return _blocking_words(blocking)

    return m.Profitability(
        test_records_hidden=hidden_words(int(profit.get("test_orders_skipped") or 0)),
        customer=customer_ref, period_start=profit["period"]["start"],
        period_end=profit["period"]["end"], period=period,
        revenue=_money(totals.get("revenue"), cur),
        booked_cost=_money(totals.get("booked_cost"), cur),
        margin=_money(totals.get("margin"), cur),
        margin_pct=_plain(totals["margin_pct"]) if totals.get("margin_pct") is not None
        else None, orders_in_period=in_period, orders_counted=counted, orders_read=read,
        orders_in_period_exact=exact,
        ranking=(f"partial: the {counted} checked orders only; "
                 + blocking_words(totals["blocking"]) if totals.get("blocking") else
                 "worst first within each currency; orders are in different currencies "
                 "(" + " and ".join(mixed) + "), so no single total" if mixed else
                 "worst first, every invoiced order of the period"
                 + (f"; {len(totals['excluded'])} not yet invoiced, excluded"
                    if totals.get("excluded") else "")
                 if totals.get("complete") else
                 f"partial: worst of {read_words(profit.get('coverage') or {}, read)}; "
                 + not_read_words(in_period, read, exact)),
        margin_basis=MARGIN_BASIS, margin_note=MARGIN_NOTE,
        verdict=(None if totals.get("margin") is None else
                 "profitable" if totals["margin"] > 0 else "not_profitable"),
        orders_excluded=len(totals.get("excluded") or ()),
        orders_more=max(0, len(profit["orders"]) - 30), period_basis=basis,
        withheld_orders=withheld,
        orders=[m.OrderMargin(order=order_ref(o), date=(o["dates"] or [None])[-1],
                              revenue=_money(o["revenue"], o["currency"]),
                              booked_cost=_money(o["booked_cost"], o["currency"]),
                              margin=_money(o["margin"], o["currency"]),
                              margin_pct=_plain(o["margin_pct"]) if o["margin_pct"]
                              is not None else None,
                              gap=None if o["decided"] else "; ".join(
                                  words_of(g["code"]).lower() for g in o["gaps"][:2])
                              or "not stated",
                              gap_code=None if o["decided"] or not o["gaps"] else
                              str(o["gaps"][0]["code"])[:60])
                for o in (sorted(profit["orders"], key=lambda o: (
                    not o["decided"], o["currency"] or "", o["margin"] or 0))
                          if mixed else profit["orders"])[:30]],
        products=[product_margin(backend, view, p, items) for p in products[:30]],
        breakdown=cost_breakdown(backend, view, breakdown, drivers,
                                 why=profit.get("why") or ()) if breakdown else None,
        drivers=[profit_driver(backend, view, d) for d in profit.get("customer_drivers")
                 or ()],
        next_action=m.NextAction(text=action["text"], rule=action["rule"])
        if action else None)


def profit_driver(backend: "WorkspaceBackend", view: Any, row: Mapping[str, Any]
                  ) -> m.ProfitDriver:
    """``customer_profit.customer_drivers``' row as the contract's ``ProfitDriver``, its
    sources decided for this reader again (a withheld one left out, never named)."""
    return m.ProfitDriver(
        kind=row["kind"], text=str(row["text"])[:600], established=bool(row["established"]),
        amount=_money(row.get("amount"), row.get("currency")) if row.get("established")
        else None,
        where_it_would_live=str(row["where"])[:300] if row.get("where") else None,
        sources=_decided_sources(backend, view, row.get("handles") or ()))


def product_words(row: Mapping[str, Any], items: Mapping[str, Any]) -> str:
    """"item X: margin on item cost only 1,357.80 CAD (90.52%) on 60 PCS over 1 order"."""
    from operating.customer_profit import MARGIN_BASIS, item_label, money_words, _plain

    cur = row["currency"]
    return (f"{item_label(items.get(row['item'], {}))}: margin {MARGIN_BASIS} "
            f"{money_words(row['margin'], cur)}"
            + (f" ({_plain(row['margin_pct'])}%)" if row["margin_pct"] is not None else "")
            + f" on {_plain(row['quantity'], Decimal('1'))} {row['unit'] or 'units'} over "
            f"{len(row['orders'])} order{'s' if len(row['orders']) != 1 else ''}")


def product_margin(backend: "WorkspaceBackend", view: Any, row: Mapping[str, Any],
                   items: Mapping[str, Any]) -> m.ProductMargin:
    from operating.customer_profit import item_label, unit_words, _plain

    cur, unit = row["currency"], row["unit"]
    low, high = row["low_unit_cost"], row["high_unit_cost"]
    return m.ProductMargin(
        item=item_label(items.get(row["item"], {}))[:200],
        quantity=_plain(row["quantity"], Decimal("0.0001")).rstrip("0").rstrip("."),
        unit=unit, revenue=_money(row["revenue"], cur), booked_cost=_money(row["cost"], cur),
        margin=_money(row["margin"], cur),
        margin_pct=_plain(row["margin_pct"]) if row["margin_pct"] is not None else None,
        orders=len(row["orders"]),
        unit_cost_range=(unit_words(low, cur, unit) if low == high else
                         f"{unit_words(low, cur, unit)} to {unit_words(high, cur, unit)}"),
        complete=row["complete"], sources=_decided_sources(backend, view, row["handles"]))


def cost_breakdown(backend: "WorkspaceBackend", view: Any, breakdown: Mapping[str, Any],
                   drivers: Sequence[str], why: Sequence[str] = ()) -> m.CostBreakdown:
    """The focus order's lines, cost sources (item ledger, credit memos, returns, purchase
    orders, the quote) and drivers, each source decided for this reader again."""
    from operating.customer_profit import unit_words, _plain, _material_words

    order = breakdown["order"]
    cur = order.currency
    lines = []
    for row in breakdown["lines"]:
        line = row["line"]
        lines.append(m.CostLine(
            label=row["label"][:200], quantity=_plain(line.quantity, Decimal("0.0001")).rstrip(
                "0").rstrip(".") if line.quantity else None, unit=line.unit,
            revenue=_money(line.revenue, line.currency),
            booked_cost=_money(line.cost, line.currency),
            margin=_money(line.margin, line.currency),
            unit_price=_plain(row["unit_price"], Decimal("0.0001"))
            if row["unit_price"] is not None else None,
            unit_cost=_plain(row["unit_cost"], Decimal("0.0001"))
            if row["unit_cost"] is not None else None,
            share_of_cost_pct=str(row["share_of_cost"]) if row["share_of_cost"] is not None
            else None,
            sources=_decided_sources(backend, view, line.invoice_handles + line.ledger_handles)))
    sources = [m.CostSource(kind=s["kind"], label=s["label"][:200], detail=s["detail"][:300],
                            amount=_money(s["amount"], s["currency"]),
                            sources=_decided_sources(backend, view, s["handles"]))
               for s in breakdown["sources"]]
    for got in breakdown["materials"]:
        sources.append(m.CostSource(
            kind="material_po", label=f"Purchase order {got['po_number']}"[:200],
            detail=got["label"][:300], per_unit=_material_words(got)[:200],
            sources=_decided_sources(backend, view, got["handles"])))
    quote = breakdown.get("quote")
    if quote:
        for row in quote["lines"]:
            detail = (f"quoted {unit_words(row['quoted_price'], quote['currency'], row['uom'])}"
                      f", invoiced {unit_words(row['actual_price'], order.currency, row['uom'])}")
            sources.append(m.CostSource(kind="quote", label=f"Quote {quote['number']}"[:200],
                                        detail=detail[:300], sources=_decided_sources(
                                            backend, view, row["handles"] + [quote["handle"]])))
    return m.CostBreakdown(
        order=m.ObjectRef(object_id=order.object_id, type="SalesOrder", label=order.number,
                          open_url=object_url("SalesOrder", order.object_id)),
        date=order.dates[-1].isoformat() if order.dates else None,
        revenue=_money(order.revenue, cur), booked_cost=_money(order.cost, cur),
        margin=_money(order.margin, cur),
        margin_pct=_plain(order.margin_pct) if order.margin_pct is not None else None,
        lines=lines[:20], cost_sources=sources[:30],
        drivers=[d[:600] for d in drivers][:6], why=[w[:300] for w in why][:2],
        gaps=[m.NotFound(what=words_of(g["code"]).capitalize() + ": " + g["detail"][:400],
                         where_it_would_live=GAP_OWNER.get(g["code"],
                                                           "Finance (Business Central owners)"))
              for g in breakdown["gaps"]][:12])


STILL_WORKING = ("I'm still working on this: the time limit ({seconds} s) was reached "
                 "before an answer passed its checks. Ask again, or narrow the question "
                 "(name the record or the field you need).")
PARTIAL_AT_LIMIT = ("Partial answer (the time limit was reached while it was being "
                    "finished; only what passed the checks is shown): ")


# Why a claim was not shown that is a permission (``agent_answer``): the reply then says
# only the shown facts.
PERMISSION_DROPS = frozenset({"not_granted", "item_refused", "withheld"})
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def permission_withheld(dropped: Mapping[str, int]) -> bool:
    return any(dropped.get(reason) for reason in PERMISSION_DROPS)


def claim_words(claim: Mapping[str, Any]) -> list[str]:
    """The words a claim states: its phrase, text, each value's quote and value string, and
    an ISO date value also as written in words (November 14, 14 November)."""
    out: list[str] = []
    for item in [claim.get("phrase"), claim.get("text")] + [
            part for value in claim.get("value_claims") or () if isinstance(value, Mapping)
            for part in (value.get("quote"), value.get("value"))]:
        item = plain(item) if not isinstance(item, str) else item
        if not isinstance(item, str) or len(item.strip()) < 3:
            continue
        out.append(item.strip().rstrip("."))
        date = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", item.strip())
        if date and 1 <= int(date.group(2)) <= 12:
            month, day = MONTHS[int(date.group(2)) - 1], str(int(date.group(3)))
            out += [f"{month} {day}", f"{day} {month}", f"{month[:3]} {day}"]
    return out


def without_claim_words(text: str, claims: Sequence[Mapping[str, Any]]) -> str:
    """``text`` without each sentence, list item or line containing any of ``claims``'
    words (``claim_words``, case-folded, white space collapsed)."""
    from operating.answer_gate import _sentences

    words = [" ".join(w.casefold().split()) for c in claims for w in claim_words(c)]
    if not text or not words:
        return text
    pieces = _sentences(text)
    kept = [(sentence, gap) for sentence, gap in pieces
            if not any(w and w in " ".join(sentence.casefold().split()) for w in words)]
    if len(kept) == len(pieces):
        return text
    return presentation.tidy("".join(sentence + gap for sentence, gap in kept))


def claim_refusal(decided: Mapping[str, Any]) -> Optional[str]:
    """Why a gate-passed claim is not shown to this reader (``view.access.decide`` of its
    citations): the first refusal's reason, or None. A captured original or SharePoint
    file (``HOST_READ_SCHEMES``) whose item re-check this app's runtime cannot make stays:
    the reader's own host gate admitted it."""
    from operator_api.workspace.object_workspace import ITEM_REFUSED
    return next((marker.get("reason") or "withheld" for handle, marker in decided.items()
                 if marker is not None and not (handle.startswith(HOST_READ_SCHEMES)
                                                and marker.get("reason") == ITEM_REFUSED)),
                None)


# Why the assistant could not work a question (``agent_ask.FAILURES``), as said to the reader.
UNAVAILABLE_WORDS = {"usage_limit": "its usage limit has been reached",
                     "sign_in": "it could not sign in",
                     "timeout": "it ran out of time",
                     "error": "it stopped with an error"}
RECORDS_BELOW = ("I couldn't confirm a direct answer, but the records read for your "
                 "question are below.")


def trail_shows(trail: Optional[m.EvidenceTrail]) -> bool:
    """Whether an answer's trail shows anything read from the records."""
    return trail is not None and bool(
        trail.steps or trail.materials or any(t.steps or t.verdict for t in trail.trails))


def unavailable_answer(reason: str, trail: Optional[m.EvidenceTrail], facts: Sequence[m.Fact],
                       subjects: Sequence[m.ObjectRef], late: Sequence[Any]
                       ) -> tuple[str, str]:
    """(direct answer, outcome) when the assistant was unavailable: said first, then what
    the records read without it show (each PO trail's verdict, the late list's facts),
    never a statement that nothing was found."""
    said = [f"The assistant is unavailable right now ({reason})."]
    shown = trail_shows(trail) or bool(facts) or bool(late)
    if shown:
        said.append("Here is what the records show.")
        for po in (trail.trails if trail is not None else [])[:3]:
            if po.verdict:
                said.append(f"{po.subject.label}: {po.verdict.split(' Rule:')[0].strip()}")
        if not (trail and trail.trails) and facts:
            said.append(facts[0].text)
    elif subjects:
        said.append("The records your question names are linked below; ask again later "
                    "for a full answer.")
    else:
        said.append("Ask again later, or narrow the question (name the record or the field "
                    "you need).")
    return " ".join(said), ("partial" if shown or subjects else "abstained")


RECORD_LINKED = ("I couldn't confirm an answer from what you can see; the record your "
                 "question names is linked below.")


def business_choices(ambiguous: Iterable[Any]) -> tuple[list[m.ObjectRef],
                                                        list[m.ObjectRef]]:
    """(did-you-mean choices, records promoted to the answer's subjects), per ambiguous
    mention: a mapping or plumbing candidate is never a choice; a mention left with one
    business record names it (promoted, linked), never "not found"; two or more stay a
    choice."""
    choices: list[m.ObjectRef] = []
    promoted: list[m.ObjectRef] = []
    for item in ambiguous:
        if not isinstance(item, Mapping):
            continue
        refs = [m.ObjectRef(object_id=c["object_id"], type=str(c.get("object_type") or "Object"),
                            label=candidate_title(item, c),
                            open_url=object_url(c.get("object_type") or "Object",
                                                c["object_id"]))
                for c in item.get("candidates") or ()
                if isinstance(c, Mapping) and isinstance(c.get("object_id"), str)
                and not internal_type(c.get("object_type"))]
        (promoted if len(refs) == 1 else choices).extend(refs)
    return choices, promoted


def told_apart(view: Any, choices: list[m.ObjectRef]) -> list[m.ObjectRef]:
    """Choices that read alike ("PO100040" twice) each say their system and their own
    values (``preread.choice_label``: "PO100040 in Business Central (New Directions
    Aromatics, ...)") as this reader reads them; a label that cannot be read stays."""
    from . import preread

    titles = [c.label for c in choices]
    if len(set(titles)) == len(titles):
        return choices
    out = []
    with view._request():
        for choice in choices:
            if titles.count(choice.label) < 2:
                out.append(choice)
                continue
            try:
                label = preread.choice_label(lambda name, args: view._call(name, dict(args)),
                                             choice.label, choice.object_id)
            except Exception:  # noqa: BLE001 - the plain title rather than none
                label = choice.label
            out.append(choice.model_copy(update={"label": label[:200]}))
    return out


def internal_type(object_type: Any) -> bool:
    """A mapping or plumbing type (``BcEntityMapping``): never a record a person picks."""
    return bool(re.search(r"(?:Mapping|EntityMap|Map|Binding|Mirror|Lineage)$",
                          str(object_type or "")))


def candidate_title(item: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    """A did-you-mean choice's title: the record's own display (its number or name), else
    what the question said; its kind is shown beside it, never inside it."""
    return (plain(candidate.get("display")) or plain(candidate.get("label"))
            or str(item.get("mention") or "Record"))[:200]


def session_roots(events: Sequence[Mapping[str, Any]], known: set[str],
                  limit: int = 4, same: Callable[[list[str]], bool] = lambda ids: False
                  ) -> list[m.ObjectRef]:
    """The records the agent session itself settled on, in the order it first used them,
    when the question's own resolution named none: a ``plx.find`` that resolved to one
    record, or to records ``same`` finds are one thing in several systems (an ambiguous one
    names none: never a guess), and the record a
    ``plx.object``, ``plx.timeline`` or ``plx.around`` call started from. The session read
    them as this reader; the trail reads each again through the reader's view."""
    from operating.why_trail import identifier
    from operator_api.agent_ask import plain_tree

    labels: dict[str, str] = {}
    order: list[str] = []
    for event in events:
        if event.get("is_error"):
            continue
        name, arguments = event.get("name"), event.get("arguments") or {}
        if name == "plx.find":
            result = plain_tree(event.get("result")) if isinstance(event.get("result"),
                                                                   Mapping) else {}
            found = [c.get("object") for c in result.get("candidates") or ()
                     if isinstance(c, Mapping) and isinstance(c.get("object"), Mapping)]
            ids = [f.get("object_id") for f in found]
            if found and all(isinstance(i, str) for i in ids) and (
                    len(ids) == 1 or len(ids) <= 4 and same(ids)):
                oid = found[0]["object_id"]
                labels.setdefault(oid, identifier(found[0].get("display")) or "")
                order.append(oid)
        elif name in ("plx.object", "plx.timeline", "plx.around") and isinstance(
                arguments, Mapping):
            oid = arguments.get("object_id") or arguments.get("from")
            if isinstance(oid, str):
                order.append(oid)
    out: list[m.ObjectRef] = []
    for oid in order:
        if ":" not in oid or oid.startswith("set:") or oid in known:
            continue
        known.add(oid)
        kind = oid.split(":", 1)[0]
        out.append(m.ObjectRef(object_id=oid, type=kind, label=labels.get(oid) or kind,
                               open_url=object_url(kind, oid)))
        if len(out) >= limit:
            break
    return out


# A sentence saying some material may be outside the reader's access (not a count).
ACCESS_CAVEAT = re.compile(
    r"not (?:permitted|allowed) to read|(?:I'm|I am) (?:not )?allowed to read|restricted from "
    r"(?:your|my) view|hidden from (?:my|your) access|(?:my|your) access (?:doesn't|does not) "
    r"cover|restrict(?:s)? access by item|(?:can't|cannot) open could still|outside (?:my|"
    r"your) access|restricted to (?:me|you)|(?:mail|files|sources) you can't open"
    r"|(?:withheld|hidden) from (?:this reader|you|me)\b", re.I)


def trail_reads_all(view: Any) -> bool:
    from .trail import reads_every_item
    return reads_every_item(view)


def without_access_caveats(text: str) -> str:
    """``text`` without its sentences that only say some material may be outside the
    reader's access, paragraph breaks kept."""
    out = []
    for para in (text or "").split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", para)
        kept = [s for s in sentences if not ACCESS_CAVEAT.search(s)]
        if kept or not para.strip():
            out.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# A sentence saying no purchase order is linked or was found (the materials table then
# lists some: the sentence is replaced by the table's own count).
NO_PURCHASE_ORDERS = re.compile(
    r"(?:n't|\bnot) (?:find|see) any (?:\w+ ){0,2}purchase[- ]orders?\b"
    r"|\bno (?:links?|records?) (?:to|of) any purchase[- ]orders?\b"
    r"|\b(?:found )?no purchase[- ]orders? (?:is |are |was |were )?(?:linked|found|recorded)\b"
    r"|\bfound no purchase[- ]orders?\b"
    r"|\bpurchase[- ]orders?\b[^.]*\b(?:found nothing|none (?:is|are|was|were) linked)\b",
    re.I)


def materials_lead(trail: m.EvidenceTrail, direct: str) -> str:
    """The reply led by the materials table's own count (the walk the agent was given),
    with any sentence saying no purchase order is linked or found left out: the reply never
    contradicts the table under it."""
    sentences = re.split(r"(?<=[.!?])\s+", direct or "")
    kept = [s for s in sentences if not NO_PURCHASE_ORDERS.search(s)]
    if len(kept) == len(sentences):
        return direct
    rest = " ".join(kept).strip()
    return (trail.title.rstrip(".") + "." + ("\n\n" + rest if rest else ""))[:4000]


# A sentence saying a late-listed PO was received, closed or complete: shown beside the
# late list's fact, never dropped.
LATENESS_CONTRARY = re.compile(
    r"\b(?:fully received|(?:was|were|been|is|are) (?:fully )?received|closed|marked "
    r"complete|is complete|completed|delivered in full)\b", re.I)
# A sentence only saying the PO's lateness could not be decided (no checked value in it).
LATENESS_HEDGE = re.compile(
    r"\b(?:couldn't|could not|can't|cannot|unable to)\s+(?:be\s+)?(?:confirm|decide|"
    r"determine|establish|tell|say)\w*\b[^.]*\b(?:late|lateness|overdue)\b"
    r"|\blateness\b[^.]*\b(?:can't|cannot|couldn't|could not)\b"
    r"|\bnot\s+(?:calling|call|counting|treating|say(?:ing)?)\s+(?:it|this|the "
    r"(?:po|order|purchase order))\s+(?:as\s+)?(?:late|overdue)\b", re.I)
# A sentence that negates or hedges what it states (not a source saying the PO arrived).
NEGATED = re.compile(r"\b(?:couldn't|could not|can't|cannot|unable to|not|no|never|"
                     r"isn't|wasn't|hasn't|haven't|aren't|weren't)\b", re.I)
LATENESS_GAP = re.compile(r"\blate(?:ness)?\b|\bverdict\b|\boverdue\b", re.I)
MAX_LATE_LOOKUPS = 60
# A question about where a PO stands (its lateness is then part of the answer).
STATUS_QUESTION = re.compile(r"\b(?:status|late|lateness|overdue|behind|delay\w*|where|"
                             r"when|update|arriv\w*|deliver\w*|receiv\w*|eta|due|stuck|"
                             r"standing|progress)\b", re.I)


def _masked(value: str, names: Sequence[str]) -> str:
    """``value`` with each of ``names`` (a record's own number) taken out."""
    for name in names:
        value = value.replace(name, " ")
    return value


def _po_key(text: Any) -> str:
    """A PO reference for matching: letters and digits upper-cased, a leading PO dropped
    (``PO#106001``, ``po 106001`` and ``106001`` all read ``106001``)."""
    key = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
    return key[2:] if key.startswith("PO") and len(key) > 2 else key


def named_late_pos(backend: "WorkspaceBackend", view: Any, question: str,
                   found: Mapping[str, Any]) -> list[tuple[m.LatePo, Optional[str]]]:
    """The late purchase orders of this reader's gap inbox (the list Home shows, as the
    reader) that ``question`` names: by a found root's object id or by a PO identifier
    the question writes equal to the record's display label. (late PO, inbox run_at)."""
    from ontology.identifiers import find_identifiers

    if not STATUS_QUESTION.search(question or ""):
        return []
    named = {_po_key(mention.normalized or mention.text)
             for mention in find_identifiers(question) if "po" in str(mention.kind).lower()}
    # only a purchase order root can be a late purchase order (an item or customer root
    # the resolver found is never looked up in the late list)
    roots = {r.get("object_id") for r in found.get("roots") or () if isinstance(r, Mapping)
             and str(r.get("object_type") or r.get("object_id")).startswith("PurchaseOrder")}
    if not named and not roots:
        return []
    inbox = view.gap_inbox()
    if inbox.get("status") != "read":
        return []
    inbox = aged_to_today(inbox, backend.today() if backend.today else None)
    run_at = inbox.get("days_as_of") or (inbox.get("run_at") if isinstance(
        inbox.get("run_at"), str) else None)
    late_gaps = [g for g in inbox.get("gaps") or () if g.get("days_late") is not None
                 and isinstance(g.get("object_id"), str)][:MAX_LATE_LOOKUPS]
    labels: dict[str, str] = {}
    for gap in late_gaps:
        oid = gap["object_id"]
        if oid in roots or oid in labels:
            continue
        card = view._call("plx.object", {"object_id": oid})
        labels[oid] = plain((card.get("object") or {}).get("display")) or ""
    wanted = [g for g in late_gaps if g["object_id"] in roots
              or (labels.get(g["object_id"]) and _po_key(labels[g["object_id"]]) in named)]
    if not wanted:
        return []
    for gap in wanted:
        if gap["object_id"] not in labels:
            card = view._call("plx.object", {"object_id": gap["object_id"]})
            labels[gap["object_id"]] = plain((card.get("object") or {}).get("display")) or ""
    late, _risk = attention_items({"gaps": wanted}, labels, limit=MAX_FACTS)
    seen: set[str] = set()
    return [(po, run_at) for po in late if not (po.object_id in seen or seen.add(po.object_id))]


def hidden_message(total: int, owners: Sequence[m.HiddenOwner]) -> str:
    head = f"{total} more item{'s' if total != 1 else ''} match, but you don't have access."
    names = [o.owner_or_team for o in owners if o.named]
    if names:
        return f"{head} Ask the owners of {names[0]}."
    return f"{head} They are in other people's mail, chats or files."


def hidden_from_counts(counts: Mapping[str, Any]) -> m.Hidden:
    """the ``hidden_counts`` result as the payload's notice."""
    if counts.get("count") is None:
        return m.Hidden(count=None, by_owner=[], message=None)
    owners = [m.HiddenOwner(owner_or_team=str(o["owner_or_team"]), count=int(o["count"]),
                            named=bool(o.get("named", True)))
              for o in counts.get("by_owner") or () if int(o.get("count") or 0) > 0]
    total = int(counts["count"])
    return m.Hidden(count=total, by_owner=owners,
                    message=hidden_message(total, owners) if total else None)


def visible_understanding(view: Any, event: m.UnderstandingEvent) -> m.UnderstandingEvent:
    """Every candidate and resolution re-read by this reader (``plx.object``): one it
    cannot open leaves the event, unnamed and uncounted."""
    seen: dict[str, bool] = {}

    def visible(object_id: str) -> bool:
        if object_id not in seen:
            card = view._call("plx.object", {"object_id": object_id})
            seen[object_id] = not card.get("error")
        return seen[object_id]
    for entity in event.entities:   # inside the caller's request (one grant)
        entity.candidates = [c for c in entity.candidates[:8] if visible(c.object_id)]
        if entity.resolved is not None and not visible(entity.resolved.object_id):
            entity.resolved = None
    return event


def normalize(text: str) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", folded.lower())).strip()


def missing_item(what: Any, who: Any, why: Any) -> m.NotFound:
    team = teams(who or ())
    where = f"Ask {team}" if team else "Not on any record you can see"
    if isinstance(why, str) and why and not re.fullmatch(r"[a-z_]+", why):
        where = f"{where}. {why}"[:400]
    return m.NotFound(what=str(what or "An answer")[:300], where_it_would_live=where)


def dedupe_not_found(items: Sequence[m.NotFound]) -> list[m.NotFound]:
    seen, out = set(), []
    for item in items:
        if item.what in seen:
            continue
        seen.add(item.what)
        out.append(item)
    return out


def understanding_event(got: Any) -> m.UnderstandingEvent:
    """the ``Understanding`` (object or mapping) as the event."""
    def get(obj: Any, key: str, default: Any = None) -> Any:
        return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)
    intent = get(got, "intent", "other")
    intents = m.UnderstandingEvent.model_fields["intent"].annotation.__args__
    entities = []
    for entity in get(got, "entities", []) or []:
        cands = [m.Candidate(object_id=str(get(c, "object_id")), type=str(get(c, "type") or
                                                                            "Object"),
                             label=str(get(c, "label") or get(c, "object_id"))[:200],
                             score=get(c, "score"))
                 for c in get(entity, "candidates", []) or [] if get(c, "object_id")]
        resolved = get(entity, "resolved")
        ref = next((c for c in cands if c.object_id == resolved), None)
        entities.append(m.UnderstoodEntity(
            text=str(get(entity, "text") or "")[:200],
            resolved=m.ObjectRef(object_id=ref.object_id, type=ref.type, label=ref.label,
                                 open_url=object_url(ref.type, ref.object_id)) if ref else None,
            ambiguous=bool(get(entity, "ambiguous", False)), candidates=cands[:8]))
    return m.UnderstandingEvent(intent=intent if intent in intents else "other",
                                entities=entities,
                                time_frame=str(get(got, "time_frame"))[:200]
                                if get(got, "time_frame") else None,
                                rewrites=[str(r)[:300] for r in get(got, "rewrites", []) or []][:5])


CUSTOMER_ORDERS = "SalesOrder.ordered_by"
def _decimal_money(value: Any, currency: Any) -> Optional[m.Money]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not _DECIMAL.match(value.strip()) \
            or not isinstance(currency, str) or not _CURRENCY.match(currency.strip()):
        return None
    # a sum of binary-float source values ("352956.480000000003") reads in cents
    amount = Decimal(value.strip())
    if amount.as_tuple().exponent < -2:
        amount = amount.quantize(Decimal("0.01"))
    return m.Money(amount=str(amount), currency=currency.strip())


def _related(view: Any, oid: str, link_type: str, *, filters: Sequence[Mapping] = (),
             pages: int = 4) -> tuple[Optional[str], dict[str, str]]:
    """(the set of records ``link_type`` links to ``oid`` in reverse, as the reader reads
    it; their object id -> display, up to ``pages`` pages)."""
    arguments: dict[str, Any] = {"from": oid, "limit": 50,
                                 "path": [{"link_type": link_type, "direction": "reverse"}]}
    if filters:
        arguments["filters"] = list(filters)
    shown: dict[str, str] = {}
    handle = None
    for _ in range(pages):
        got = view._call("plx.around", arguments)
        if got.get("error"):
            break
        handle = handle or got.get("set")
        for obj in got.get("objects") or ():
            if isinstance(obj, Mapping) and isinstance(obj.get("object_id"), str) \
                    and plain(obj.get("display")):
                shown[obj["object_id"]] = str(plain(obj.get("display")))
        cursor = (got.get("completeness") or {}).get("next_cursor")
        if not cursor:
            break
        arguments["cursor"] = cursor
    return handle, shown


def _grouped(view: Any, handle: str, measure: Mapping[str, str],
             group_by: Sequence[str] = ()) -> Optional[list[Mapping[str, Any]]]:
    """``plx.aggregate`` over a set; a sum mixing currencies is read again per currency.
    None when it cannot be read for this reader (the refusal stands; ``_refusal`` says
    why)."""
    return _aggregate_read(view, handle, measure, group_by)[0]


def _aggregate_read(view: Any, handle: str, measure: Mapping[str, str],
                    group_by: Sequence[str] = ()
                    ) -> tuple[Optional[list[Mapping[str, Any]]], Optional[str]]:
    """(the groups, or None; the refusal's code when refused)."""
    arguments: dict[str, Any] = {"set": handle, "measure": dict(measure)}
    if group_by:
        arguments["group_by"] = list(group_by)
    got = view._call("plx.aggregate", arguments)
    if got.get("error") and not group_by and measure.get("op") == "sum":
        got = view._call("plx.aggregate", {**arguments, "group_by": ["currency"]})
    if got.get("error"):
        return None, str((got.get("error") or {}).get("code") or "refused")
    return [g for g in got.get("groups") or () if isinstance(g, Mapping)], None


_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")
