"""Card evidence of the proactive engine: orders at risk from late POs, the kept verdicts, card
rules and documents, the step chains behind a card and the money amounts on it."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore

from . import po_status
from . import proactive_gaps as pg
from . import suspect_money
from . import order_value

from .proactive_engine_config import CARD_ALL, SETTLED, TIME_BUDGET, UNREADABLE
from .proactive_engine_findings import _cites, _coverage, _gap, _markings, _result, _Skips
from .proactive_engine_purchasing import _ATTESTS, _po_source
from .proactive_engine_sales import _customer_orders
from .proactive_engine_drafts import (
    _lateness_audiences, _line_cites, _line_event, _promise_audiences, _promise_caveat,
)
from .proactive_engine_inbox import reader_key
from .proactive_engine_budget import TimeBudgetSpent
from .proactive_engine_population import ORDER_TYPES

if TYPE_CHECKING:
    from .proactive_engine_config import Cite
    from .proactive_engine import _Store


def _orders_at_risk(st: _Store, as_of: date, call: Callable | None, bound: int,
                    first: Iterable[str] = (), many: Callable | None = None,
                    cache: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``orders_at_risk``: one gap per customer order a late PO puts at risk, and per late
    PO one ``po_impact`` gap (the orders at risk it feeds, never money) and, when every one
    of those orders has a stated margin in one currency, one ``po_impact_margin`` gap with
    the margin at stake whose every figure is its own finance-marked citation group (only a
    reader holding finance, who may open every figure, sees it). ``first``: the purchase
    orders the late rules found, walked first within ``bound``. Each gap's event names the
    chain's ends its groups cite (``pos``: the late PO an order's worst chain starts from;
    ``orders``: the orders a PO's impact reaches), so a reader's list joins a late PO to
    the customer orders it holds up only among gaps that reader may see. ``many``: the
    run's process pool (``run(pool=)``): every PO reaching an assembly is asked, not
    ``bound`` of them; one the time left did not reach is ``po_not_checked``.
    ``cache``: the last runs' decided verdicts (``event_cache``: PO -> fingerprint and
    lateness entry); on the pool a PO whose fingerprint
    (``proactive_events.verdict_fingerprints``: everything its verdict can read, the day
    and the reader's grants) is unchanged keeps its verdict, every other PO is asked, and
    a pass cut at its window keeps the verdicts it read, so a pass spans runs."""
    if call is None:
        return _result([], status="not_configured", reasons=["tool_caller_not_given"])
    from . import impact_chain as ic

    verdicts = _VerdictCache(st, as_of, call, many, cache)
    try:
        found = ic.scan(st, call, bound=None if many is not None else bound,
                        margin_values=ic.store_margin_values(st),
                        landed_values=ic.investigate_landed_values(call),
                        first=frozenset(first),
                        call_many=verdicts.ask if verdicts.reader else many)
    except TimeBudgetSpent:
        if not verdicts.kept:
            raise
        # cut at its window's end after the verdicts: they stay for the next run (a pass
        # spans runs), nothing judged on a part
        cut = _result([], status="truncated", reasons=[TIME_BUDGET])
        cut["cache"], cut["roots"] = verdicts.kept, verdicts.roots
        return cut
    risky = found["assessment"].at_risk
    requires = _lateness_audiences(st) if risky else []
    late = found["late"]
    handles = {v["citation"]["l1_handle"] for order in risky
               for v in late[order.worst].cites}
    # per line: the confirmation's cited per-item promise of the line each chain leaves by
    handles |= {v["citation"]["l1_handle"] for order in risky for chain in order.chains
                for v in _line_cites(late[chain.po], chain)}
    handles |= {ref for order in risky for ref in order.required_from}
    records = {r for order in risky for chain in order.chains if chain.po == order.worst
               for r in chain.records}
    by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    impact_versions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for version in st.versions(objects=records | {r for order in risky for chain in order.chains
                                                  for r in chain.records}):
        impact_versions[version["object_id"]].append(version)
        if version["object_id"] in records:
            by_object[version["object_id"]].append(version)
    handles |= {v["citation"]["l1_handle"] for order in risky for chain in order.chains
                for v in late[chain.po].cites}
    handles |= {r for margin in found["margins"].values() for f in margin.figures
                for r in (f.refs or (f.ref,))}
    handles |= {ref for order in risky for chain in order.chains for ref in chain.refs}
    held = {v["l1_handle"]: v for v in st.versions(handles=handles)}

    def cite_value(fig: Any) -> Cite:
        """A margin figure's citation at its value's own markings (finance) joined with its
        record's: a reader who does not hold finance never sees the money claim."""
        version = held.get(fig.ref)   # a handle the store does not hold: shown to nobody
        return ((fig.ref, version["object_id"], _markings(version["markings"], fig.markings))
                if version else (fig.ref, None, UNREADABLE))

    def cite(handle: str) -> Cite:
        version = held.get(handle)   # a handle the store does not hold: shown to nobody
        return ((handle, version["object_id"], _markings(version["markings"])) if version
                else (handle, None, UNREADABLE))

    unless = _promise_audiences(st) if risky else []
    gaps = []
    skipped: Counter[str] = Counter()
    for order in risky:
        chain = order.chain_of(order.worst)
        verdict = late[order.worst]
        own = verdict.own_promise(chain.line)
        into: dict[str, list[str]] = defaultdict(list)
        for edge in chain.edges:
            into[edge.frm].append(edge.markings)
            into[edge.to].append(edge.markings)
        # each lateness value its own group: a reader who may not open one of them never
        # sees the verdict that rests on it
        groups = [[cite(v["citation"]["l1_handle"])] for v in late[order.worst].cites]
        # the line's own promise: the confirmation's cited per-item date, its own group
        groups += [[cite(v["citation"]["l1_handle"])] for v in _line_cites(verdict, chain)]
        groups += [[(v["l1_handle"], record, _markings(v["markings"], *into[record]))
                    for v in by_object.get(record, ())] for record in chain.records]
        groups.append([cite(ref) for ref in order.required_from])
        # a lead's own basis (the planned date it was paired by) is cited too
        groups += [[cite(ref)] for ref in chain.refs]
        pos = len({c.po for c in order.chains})
        noun = "purchase order" if pos == 1 else "purchase orders"
        # the day measured stands for "not received yet", never for a date the material
        # comes: the PO's own due date is stated, and the event holds no material date
        pending = order.material_how.startswith("not received as of")
        due = verdict.governing
        material = (("its material is not received yet"
                     + (f" (the purchase order was due {due.isoformat()})" if due else ""))
                    if pending else
                    f"material {order.material.isoformat()} ({order.material_how})")
        firm = chain.tier == "firm"
        how = ("an assembly order made or allocated for this order (recorded links, each "
               "cited)" if firm else
               f"an assembly order tied to this order only by a lead ({ic.CHAIN_BASES[chain.basis]}; "
               "each record it rests on cited)")
        gap = _gap(
            "order_at_risk", "orders_at_risk", order.key, "SalesOrder", "at_risk",
            summary=("" if firm else "Lead, not firm: ")
                    + f"{order.days} days at risk from {pos} late {noun} (days at risk is a "
                    "lower bound: no assembly lead time is recorded)",
            missing="Confirm whether the order's required date still holds, or when the "
                    "late purchase order's material will arrive"
                    + ("" if firm else ", and whether this assembly order is made for it"),
            why=pg._clip(f"The item of a late purchase order is consumed by {how}: "
                         f"{material}, against the required date "
                         f"{order.required.isoformat()}."),
            priority=("high" if order.days >= 7 else "medium") if firm else
                     ("medium" if order.days >= 7 else "low"),
            about=("SalesOrder", "required_date"), groups=groups,
            event={"days": order.days, "orders_affected": 1, "pos": [order.worst],
                   **({"material_pending": True,
                       **({"due": due.isoformat()} if due else {})} if pending else
                      {"material": order.material.isoformat()}),
                   **({"required": order.required.isoformat()} if order.required else {}),
                   **_line_event(own, chain, order.required)})
        if gap is None:
            skipped["order_at_risk_uncited"] += 1
        else:
            gap["requires"] = requires
            if verdict.rests_on_promise(chain.line):
                _promise_caveat(gap, unless, verdict.unseen_promise)
            gaps.append(gap)
    by_key = {order.key: order for order in risky}
    for impact in found["po_impacts"]:
        orders = [by_key[k] for k in impact["orders"]]
        if not orders:
            continue
        po = impact["po"]
        # the PO's lateness values, then every record of each chain from it, each its own
        # group: a reader who may not open one never sees the claim
        groups = [[cite(v["citation"]["l1_handle"])] for v in late[po].cites]
        for order in orders:
            chain = order.chain_of(po)
            groups += [[cite(v["citation"]["l1_handle"])] for v in _line_cites(late[po], chain)]
            into: dict[str, list[str]] = defaultdict(list)
            for edge in chain.edges:
                into[edge.frm].append(edge.markings)
                into[edge.to].append(edge.markings)
            groups += [[(v["l1_handle"], record, _markings(v["markings"], *into[record]))
                        for v in impact_versions.get(record, ())] for record in chain.records]
            groups.append([cite(ref) for ref in order.required_from])
        n = len(orders)
        noun = "sales order" if n == 1 else "sales orders"
        worst = max(o.days for o in orders)
        missing = ("Confirm when the late purchase order's material will arrive, or whether "
                   "the sales orders' required dates still hold")
        leads = sum(1 for o in orders if o.chain_of(po).tier != "firm")
        groups += [[cite(ref)] for o in orders for ref in o.chain_of(po).refs]
        why = pg._clip(f"An item of this purchase order is consumed by assembly orders made or "
                       f"allocated for {n} {noun} at risk (recorded links, each cited"
                       + (f"; {leads} of them tied by a lead only, not firm: inferred by "
                          "item and date or mentioned in a document" if leads else "")
                       + f"); the worst is {worst} days at risk (a lower bound: no assembly "
                       "lead time is recorded).")
        priority = "high" if worst >= 7 else "medium"
        plain = _gap("po_impact", "orders_at_risk", po, "PurchaseOrder", "impact",
                     summary=f"affects {n} {noun}, worst {worst} days at risk",
                     missing=missing, why=why, priority=priority,
                     about=("PurchaseOrder", "lateness"), groups=groups,
                     event={"days": worst, "orders_affected": n,
                            "orders": [o.key for o in orders]})
        if plain is None:
            skipped["po_impact_uncited"] += 1
            continue
        plain["requires"] = requires
        promised = any(late[po].rests_on_promise(o.chain_of(po).line) for o in orders)
        if promised:
            _promise_caveat(plain, unless, late[po].unseen_promise)
        gaps.append(plain)
        margin = impact["margin"]
        if margin is None or impact["orders_with_margin"] != n:
            continue   # the money claim needs every order's margin: never a part as the whole
        figures = [f for order in orders for f in found["margins"][order.key].figures]
        # a landed-cost engine figure: each cited value it was computed from is its own
        # finance-marked group (a reader who may not open one never sees the claim)
        value_groups = [[cite_value(f)] if not f.refs else [cite_value(ic.Figure(
            f.amount, f.currency, f.unit, ref, f.object_id, f.markings))] for f in figures
            for ref in (f.refs or (f.ref,))]
        money = _gap("po_impact_margin", "orders_at_risk", po, "PurchaseOrder", "margin",
                     summary=f"affects {n} {noun}, {margin['amount']} {margin['currency']} "
                             f"margin at stake ({margin['basis']}), worst {worst} days at "
                             "risk",
                     missing=missing, why=pg._clip(why + " " + ic.MARGIN_BASIS + "."),
                     priority=priority, about=("PurchaseOrder", "lateness"),
                     groups=groups + value_groups,
                     event={"days": worst, "orders_affected": n,
                            "amount": {"value": str(margin["amount"]),
                                       "currency": margin["currency"],
                                       "basis": "margin_at_stake"}})
        if money is None:
            skipped["po_impact_margin_uncited"] += 1
        else:
            money["requires"] = requires
            if promised:
                _promise_caveat(money, unless, late[po].unseen_promise)
            gaps.append(money)
    if found["lateness_undetermined"]:
        skipped["lateness_undetermined"] = found["lateness_undetermined"]
    if found["pos_not_checked"]:
        skipped["po_not_checked"] = found["pos_not_checked"]
    unread = found.get("pos_not_checked_unread", 0)
    failed = found.get("pos_not_checked_failed", 0)
    cut_by_bound = found["pos_not_checked"] - unread - failed
    result = _result(gaps, status="truncated" if found["truncated"] else "complete",
                     reasons=(["impact_bound"] if cut_by_bound else [])
                     + ([TIME_BUDGET] if unread else [])
                     + (["worker_failed"] if failed else []),
                     not_evaluated=skipped)
    total = found.get("pos_reaching_assembly", found["pos_checked"] + found["pos_not_checked"])
    result["coverage"] = _coverage(
        total, total - found["pos_not_checked"] - found["lateness_undetermined"],
        {"lateness_undetermined": found["lateness_undetermined"],
         **({"impact_bound": cut_by_bound} if cut_by_bound else {}),
         **({TIME_BUDGET: unread} if unread else {}),
         **({"worker_failed": failed} if failed else {})},
        unit="purchase_orders_reaching_an_assembly")
    if verdicts.reader:
        result["cache"], result["roots"] = verdicts.kept, verdicts.roots
        result.setdefault("counts", {})["verdicts_reused"] = verdicts.hits
    result["finding"] = {"orders_at_risk": len(risky),
                         "orders_at_risk_by_tier": found["orders_at_risk_by_tier"],
                         "late_pos": found["late_pos_with_orders_at_risk"],
                         "text": ic.headline(len(risky),
                                             found["late_pos_with_orders_at_risk"])}
    return result


# A verdict is kept only when it is decided (``VERDICT_KEPT``) and whole: no error, its
# own lateness entry, the answer not cut; any other is asked again next run. (A vendor
# promise the reader cannot see, ``complete`` false on it, is decided by the records and
# the grants the fingerprint holds: kept.)
VERDICT_KEPT = frozenset({"late", "not_due", "on_time", "received"})


def _verdict_keeps(po: str, answer: Any) -> dict[str, Any] | None:
    """The part of a ``po_delivery_window`` answer ``impact_chain._verdict`` reads (the
    PO's own lateness entry) when it may be reused, else None."""
    if not isinstance(answer, Mapping) or "error" in answer:
        return None
    entry = next((e for e in answer.get("lateness") or () if isinstance(e, Mapping)
                  and e.get("root") == po), None)
    if entry is None or entry.get("state") not in VERDICT_KEPT \
            or answer.get("truncated") or answer.get("complete") is False:
        return None
    try:   # JSON only (the stored reading): anything else is asked again
        kept = json.loads(json.dumps({"lateness": [entry]}, sort_keys=True))
    except (TypeError, ValueError):
        return None
    return kept if kept == {"lateness": [entry]} else None


class _VerdictCache:
    """``orders_at_risk``'s cross-run verdict cache over the run's pool (``many``):
    ``ask`` answers a PO whose fingerprint is unchanged from its kept verdict and asks the
    pool for the others in their order (the late ones first stay first). ``reader`` None
    (no pool, or a caller whose reader or grants cannot be read): nothing is reused."""

    def __init__(self, st: _Store, as_of: date, call: Any, many: Callable | None,
                 cache: Mapping[str, Any] | None) -> None:
        self.st, self.as_of, self.many = st, as_of, many
        self.prior = cache if isinstance(cache, Mapping) else {}
        self.reader = _margin_reader(call) if many is not None else None
        self.kept: dict[str, Any] = {}
        self.roots: list[str] = []
        self.hits = 0

    def ask(self, tool: str, arguments: Sequence[Mapping[str, Any]]) -> list[Any]:
        from . import proactive_events as ev

        arguments = list(arguments)
        if tool != "plx.process" or any(a.get("process") != "po_delivery_window"
                                        for a in arguments):
            return list(self.many(tool, arguments))
        pos = [a.get("from") for a in arguments]
        self.roots = list(dict.fromkeys(p for p in pos if isinstance(p, str)))
        prints = ev.verdict_fingerprints(self.st, self.roots, ev.verdict_context(
            self.st, self.reader, self.as_of.isoformat(), self.st.path.parent.parent))
        out: list[Any] = [None] * len(arguments)
        misses = []
        for n, po in enumerate(pos):
            held = self.prior.get(po)
            if isinstance(held, Mapping) and isinstance(held.get("answer"), Mapping) \
                    and po in prints and held.get("fp") == prints[po]:
                out[n] = held["answer"]
                self.hits += 1
            else:
                misses.append(n)
        if misses:
            for n, answer in zip(misses, self.many(tool, [arguments[n] for n in misses])):
                out[n] = answer
        for po, answer in zip(pos, out):
            keep = _verdict_keeps(po, answer) if po in prints else None
            if keep is not None:
                self.kept[po] = {"fp": prints[po], "answer": keep}
        return out


LATE_FOUND_KINDS = frozenset({"late_po", "late_order", "unconfirmed_po", "eta_slipped"})
IMPACT_KINDS = ("order_at_risk", "po_impact", "po_impact_margin")


def _late_found(results: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """The purchase orders this run's earlier rules found late, unconfirmed or slipping
    (their gaps' subjects): the impact walk asks their verdicts first."""
    return {str(gap.get("object_id")) for result in results.values()
            for gap in result.get("gaps") or ()
            if gap.get("kind") in LATE_FOUND_KINDS and gap.get("object_id")}


def _card_documents(st: _Store, as_of: date, limit: int) -> list[str]:
    """The attesting documents ``card_rules`` reads, newest first (at most ``limit``): only
    those whose card could raise a gap (``proactive_gaps.card_candidates``, one set-based
    pass over the current links and values, a superset; the card stays the judge). Before,
    the newest ``CARD_BOUND`` of all ~176k documents were read one card each and a pass
    never finished (11-13: 216 s for 500 cards, truncated)."""
    documents = [name for name, _field in pg._roster()[0]]
    wanted = sorted(pg.card_candidates(st.conn, as_of))
    return [row[0] for row in st.conn.execute(
        "SELECT o.object_id FROM objects o WHERE o.object_type IN (SELECT value FROM "
        "json_each(?)) AND o.object_id IN (SELECT value FROM json_each(?)) AND NOT "
        + ostore.removed_at_sql("o.object_id")
        + " ORDER BY o.first_recorded_at DESC, o.object_id LIMIT ?",
        (json.dumps(documents), json.dumps(wanted), limit))]


def _card_rules(st: _Store, as_of: date, call: Callable | None, bound: int,
                many: Callable[[str, Sequence[Any]], list[Any]] | None = None
                ) -> dict[str, Any]:
    """``missing_in_record`` and attested conflicts on the attesting documents' cards (the
    newest ``bound``). ``many``: the run's process pool reads the cards side by side (one
    the time left did not reach is not read: the budget marks the rule truncated)."""
    if call is None:
        return _result([], status="not_configured", reasons=["tool_caller_not_given"])
    # on the run's pool every candidate is read (the window decides, as orders_at_risk
    # asks every PO); one by one, at most ``bound``
    ids = _card_documents(st, as_of, (bound if many is None else CARD_ALL) + 1)
    unread: Counter[str] = Counter()
    skipped = _Skips()
    found: list[tuple[dict[str, Any], list[list[str]]]] = []
    asked = [{"object_id": object_id, "response_format": "detailed"} for object_id in ids[:bound]]
    pooled = many("plx.object", asked) if many is not None else None
    for n, object_id in enumerate(ids[:bound]):
        if pooled is None:
            answer = call("plx.object", asked[n])
        else:
            from . import proactive_pool
            answer = pooled[n]
            if answer is proactive_pool.NOT_READ:
                continue   # the time left did not reach it (``Budget.pooled`` noted it)
        error = answer.get("error") if isinstance(answer, Mapping) else {"code": "error"}
        if error:
            code = error.get("code") if isinstance(error, Mapping) else None
            unread[code if isinstance(code, str) else "error"] += 1
            skipped.note(f"unread_{code if isinstance(code, str) else 'error'}", [object_id])
            continue
        conflicts = answer.get("conflicts") if isinstance(answer.get("conflicts"), list) else []
        card = {"object": answer.get("object"), "leads": answer.get("leads"), "values": [],
                "conflicts": [c for c in conflicts if isinstance(c, Mapping) and c.get("tier")]}
        found.extend(pg._detect(card, as_of))
    held = {v["l1_handle"]: v for v in st.versions(handles={
        h for gap, requires in found for h in gap["citations"]})}

    def cite(handle: str) -> Cite:
        version = held.get(handle)   # a handle the store does not hold: shown to nobody
        return ((handle, version["object_id"], _markings(version["markings"])) if version
                else (handle, None, UNREADABLE))

    gaps = []
    seen: set[tuple[str, str, frozenset[str]]] = set()
    repeats = 0
    for gap, requires in found:
        follow = gap["follow_up"]
        records: frozenset[str] = frozenset()
        if gap["kind"] == pg.KINDS[0]:
            # the records the document attests (every empty read's own record): one gap
            # per (records, field) however many documents state it (the newest document
            # is read first and kept; 11-13 judging: one order raised up to 4 times, once
            # per assembly packet), and the order among them carried on the follow-up so
            # the active-population filter leaves a closed or historic order out
            records = frozenset(held[h]["object_id"] for group in requires[1:]
                                for h in group if h in held)
            if records:
                if (gap["kind"], gap["summary"], records) in seen:
                    repeats += 1
                    continue
                seen.add((gap["kind"], gap["summary"], records))
        made = _gap(
            gap["kind"], "card_rules", gap["object_id"], gap["object_type"],
            gap["id"], summary=gap["summary"], missing=follow["missing"], why=follow["why"],
            priority=follow["priority"], about=(gap["object_type"], ""),
            groups=[[cite(h) for h in group] for group in requires],
            extra=[cite(h) for h in gap["citations"]])
        orders = sorted(r for r in records if r.split(":", 1)[0] in ORDER_TYPES)
        if made is not None and orders:
            made["follow_up"][_ATTESTS] = orders[0]
        gaps.append(made)
    status = "truncated" if len(ids) > bound or unread else "complete"
    out = _result(gaps, status=status,
                  reasons=(["card_bound"] if len(ids) > bound else [])
                  + [f"unread:{code}" for code in sorted(unread)],
                  not_evaluated=skipped)
    out["counts"] = {"cards": len(ids[:bound]), "repeats_merged": repeats}
    return out


# The cited amount an event about a record of these types is ranked by (the record's own
# total and its currency, both one value across its sources): (amount, currency) property.
AMOUNT_OF = {"PurchaseOrder": ("total_cost", "currency"),
             "SalesOrder": ("total_amount", "currency"),
             "Quote": ("total_amount", "currency"),
             "PurchaseInvoice": ("total_amount", "currency")}


# A sales order's value derived where its header states none (``operating.order_value``):
# the amount's basis, as the briefing words it.
DERIVED_BASIS = {order_value.INVOICES: "order_value_invoices",
                 order_value.ORDER_LINES: "order_value_lines"}


def _sheets(store_path: Any, given: Any) -> Path | None:
    """The sheet readings file the run reads (``proactive_sheets.readings_path``), None
    when there is none: the sheet rules are then ``blocked`` and stock reads no sheet."""
    from . import proactive_sheets as sheet_rules
    path = Path(given) if given is not None else sheet_rules.readings_path(store_path)
    return path if path.is_file() else None


def _order_state(st: _Store, as_of: date) -> dict[str, Callable[[], Callable[[str], Any]]]:
    """Per order type, a reader of one order record's (cluster, status class) by the one
    status definition of its type: customer orders from the run's one order read
    (``_customer_orders``), purchase orders read only for the clusters asked about."""
    def customer() -> Callable[[str], Any]:
        state = {m: (v.top, v.cls) for v in _customer_orders(st)[1] for m in v.group}
        return state.get

    def purchase() -> Callable[[str], Any]:
        src = _po_source(st)
        top_of = {m: top for top, members in src.clusters.items() for m in members}
        read: dict[str, str] = {}

        def one(member: str) -> Any:
            top = top_of.get(member)
            if top is None:
                return None
            if top not in read:
                read[top] = po_status.read(src, top, as_of).status.cls
            return top, read[top]
        return one
    return {"SalesOrder": customer, "PurchaseOrder": purchase}


def _ops_steps(st: _Store, as_of: date, people: Callable[[], Any],
               sources: Mapping[str, Any] | None
               ) -> dict[str, Callable[[], dict[str, Any]]]:
    """The operations alerts (``operating.proactive_ops``) as engine steps; ``sources``:
    overrides of what the schema declares (``proactive_ops.declared_sources``)."""
    from . import proactive_ops as po
    from . import inbox_triage, label_changes, label_revisions, rd_projects
    from . import proactive_threads as threads

    given = dict(sources or {})
    common = {"gap": _gap, "cites": _cites, "result": _result, "skips": _Skips}
    tasks = lambda: st.memo("ops:mc_tasks", lambda: po.mc_tasks(   # noqa: E731
        st, as_of, people=people(), settled=SETTLED, **common))
    return {
        "mc_task_stuck": lambda: tasks()[po.MC_STUCK],
        "mc_approval_pending": lambda: po.mc_approval_pending(
            st, as_of, people=people(), settled=SETTLED, **common),
        "portal_approval_pending": lambda: po.portal_approval_pending(
            st, as_of, people=people(), settled=SETTLED, **common),
        "planner_task_overdue": lambda: po.planner_task_overdue(
            st, as_of, people=people(), settled=SETTLED, **common),
        "aged_balance_worsening": lambda: po.aged_balance_worsening(
            st, as_of, settled=SETTLED, **common),
        "production_late": lambda: po.production_late(
            st, as_of, settled=SETTLED, sources=given.get("production_due"), **common),
        "assembly_damage_spike": lambda: po.assembly_damage_spike(
            st, as_of, settled=SETTLED, dates=given.get("damage_date"), **common),
        "teams_unanswered_ask": lambda: po.teams_unanswered_ask(
            st, as_of, people=people(), text_props=given.get("ask_text"), **common),
        # mail threads waiting on someone (``operating.proactive_threads``)
        "approvals_waiting": lambda: threads.approvals_waiting(
            st, as_of, people=people(), **common),
        "chase_escalation": lambda: threads.chase_escalation(
            st, as_of, people=people(), **common),
        "party_chases": lambda: threads.party_chases(st, as_of, people=people(), **common),
        "internal_chases": lambda: threads.internal_chases(st, as_of, people=people(),
                                                           **common),
        "inbox_triage": lambda: inbox_triage.inbox_triage(st, as_of, people=people(), **common),
        "label_changes": lambda: label_changes.label_changes(
            st, as_of, people=people(), **common),
        "label_revisions": lambda: label_revisions.label_revisions(
            st, as_of, people=people(), **common),
        "rd_projects": lambda: rd_projects.rd_projects(st, as_of, people=people(), **common)}


# A cached margin reading is kept only when it is whole: decided (a figure stated) and
# read from every answer page; any other reading is read again next run (a refused or
# partly read run may not repeat).
_MARGIN_TRANSIENT = frozenset({"ORDER_MARGIN_NOT_READ", "ANSWER_PAGES_UNREAD",
                               "ORDER_LINES_NOT_ALL_SHOWN"})


def _margin_keeps(order: Any, cp: Any) -> bool:
    return (isinstance(order, cp.Order) and order.margin is not None
            and not {code for code, _detail in order.gaps} & _MARGIN_TRANSIENT)


def _margin_reader(call: Any) -> str | None:
    """The reader a margin reading was read as, with what it is granted: tenant,
    principals, clearance and the decided policy's audience patterns and markings it holds
    (``host.stored_policy``), so a grant added or withdrawn reads every order again. None
    when the caller names no reader or its policy cannot be read: then nothing is reused."""
    actor = getattr(call, "actor", None)
    host = getattr(call, "host", None)
    if actor is None or not getattr(actor, "principals", None) or host is None:
        return None
    try:
        policy = host.stored_policy(actor)
        mine = frozenset(actor.principals)
        granted = sorted(p for p, who in policy.audiences.items() if mine & frozenset(who))
        held = sorted(m for m, who in policy.markings.items() if mine & frozenset(who))
        whole = bool(getattr(policy, "unrestricted", False))
    except Exception:  # noqa: BLE001 - a policy not read: nothing reused
        return None
    grants = hashlib.sha256(json.dumps([granted, held, whole]).encode()).hexdigest()
    return f"{reader_key(actor)}|{getattr(actor, 'clearance', '')}|{grants}"


def currency_word(raw: Any) -> str | None:
    """A currency cell as its ISO 4217 code by the ontology's one table
    (``ontology.units.currency_code``): ``CAD``, the Portal's ``CAD$``/``USD$``, ``C$``,
    ``US$``; None for anything no rule reads or a code ISO 4217 does not list (``CDN``,
    ``$``): such an amount is never stated nor ranked."""
    from ontology.units import ISO_4217, currency_code

    code, _ = currency_code(str(raw)) if raw not in (None, "") else (None, None)
    return code if code in ISO_4217 else None


def _suspects(st: _Store, object_type: str, objects: Sequence[str] = ()) -> dict[str, Any]:
    """``suspect_money.suspects`` of ``objects``, judged once over every record of the type
    that states a total (its medians are populations: an incremental pass compares them
    with the last fold's, ``_Store.population``). Once the run's time is spent no new check
    is issued: the caller says the total could not be checked."""
    if st.interrupt is not None and st.interrupt():
        raise sqlite3.OperationalError("interrupted: the run's time is spent")
    every = st.population(f"suspects:{object_type}", lambda: suspect_money.suspects(
        st, object_type, sorted(st.values([object_type], AMOUNT_OF[object_type][:1]))))
    return {oid: every[oid] for oid in objects if oid in every}


def _with_amounts(st: _Store, results: Mapping[str, Mapping[str, Any]]) -> None:
    """Each gap about a record ``AMOUNT_OF`` names that states no amount gets the record's
    own cited total with its cited currency: both citations join the gap as extra (they
    never decide who sees the gap) and the amount names them, so a reader shown the gap
    sees the figure only when it may open both (``_shown``). A total missing, disputed
    across sources, not a decimal, or without one 3-letter currency gives none. A total
    implausible against its lines or history (``operating.suspect_money``), or one that
    could not be checked, carries ``suspect`` (why): the briefing never ranks by it."""
    from . import proactive_events as ev

    for object_type in AMOUNT_OF:   # every run reads the same populations, gaps or none
        try:
            _suspects(st, object_type)
        except Exception:  # noqa: BLE001 - checked again below, per gap
            pass
    wanted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results.values():
        for gap in result["gaps"]:
            event = gap["follow_up"].get("event") or {}
            if gap["object_type"] in AMOUNT_OF and "amount" not in event:
                wanted[gap["object_type"]].append(gap)
    for object_type, gaps in wanted.items():
        amount, currency = AMOUNT_OF[object_type]
        try:
            checked = _suspects(st, object_type, sorted({g["object_id"] for g in gaps}))
        except Exception:  # noqa: BLE001 - an unchecked total is never stated as fact
            checked = None
        try:
            found = st.values([object_type], (amount, currency),
                              objects=sorted({g["object_id"] for g in gaps}))
        except sqlite3.OperationalError:
            continue   # interrupted at the deadline: no amount stated, none ranked
        derived: dict[str, Any] = {}
        disputed: set[str] = set()
        if object_type == order_value.TYPE:
            try:   # a header stating no value: what its invoices or lines derive
                worth = order_value.for_store(st, sorted({g["object_id"] for g in gaps}))
            except sqlite3.OperationalError:
                worth = {}
            derived = {oid: got for oid, got in worth.items() if got.derived}
            # a header total its own lines contradict ranks nothing (``order_value``)
            disputed = {oid for oid, got in worth.items()
                        if got.reason == order_value.HEADER_DISAGREES}
        for gap in gaps:
            if gap["object_id"] in disputed:
                continue
            got = derived.get(gap["object_id"])
            if got is not None:
                rows = list(got.rows)
                marks = _markings(*(r.get("markings") for r in rows))
                gap["extra"] = list(dict.fromkeys([*gap["extra"], *_cites(rows)]))
                gap["follow_up"]["event"] = {
                    **(gap["follow_up"].get("event") or {}),
                    "amount": {"value": str(got.amount), "currency": got.currency,
                               "basis": DERIVED_BASIS[got.basis],
                               "handle": rows[0]["l1_handle"],
                               "currency_handle": rows[0]["l1_handle"], "markings": marks}}
                continue
            props = (found.get(gap["object_id"]) or {}).get("props") or {}
            total, code = ev._one(props.get(amount)), ev._one(props.get(currency))
            value = ev._decimal(total["value"]) if total else None
            word = currency_word(code["value"]) if code else None
            if value is None or value <= 0 or word is None:
                # no figure, none entered yet (a total of 0: an order not priced), or a
                # currency no rule reads: nothing stated or ranked
                continue
            extra = _cites([total, code])
            stated: dict[str, Any] = {
                "value": str(value), "currency": word, "basis": f"{amount}",
                "handle": total["l1_handle"], "currency_handle": code["l1_handle"],
                # the figure's own markings (a value may be marked apart from its record)
                "markings": _markings(total.get("markings"), code.get("markings"))}
            found_of = None if checked is None else checked.get(gap["object_id"]) or {}
            if found_of is None:
                stated["suspect"] = "its plausibility could not be checked"
            elif found_of.get("suspect"):
                # implausible against its history: shown as suspect, never ranked; the
                # reason's ratio band only for a reader who may see every peer it rests on.
                # Checked first: an outlier is never shown as a convention
                finding = found_of["suspect"]
                stated.update(suspect=finding["reason"], suspect_plain=finding["plain"],
                              suspect_scope=finding["scope"])
            if found_of and found_of.get("lines"):
                # checked against its own lines and not simply theirs (the Portal's unit-
                # price convention, a disagreement, or no priced line): never ranked; what
                # the lines say is stated only to a reader who may open every one of them
                lines = found_of["lines"]
                rows = lines.get("rows")
                line_cites = _cites(rows) if rows else []
                extra = [*extra, *line_cites]
                stated["lines"] = {
                    "verdict": lines["verdict"],
                    **({"lines_sum": lines["lines_sum"]} if "lines_sum" in lines else {}),
                    "handles": None if rows is None else [c[0] for c in line_cites],
                    "markings": lines["markings"]}
            gap["extra"] = list(dict.fromkeys([*gap["extra"], *extra]))
            gap["follow_up"]["event"] = {**(gap["follow_up"].get("event") or {}),
                                         "amount": stated}
