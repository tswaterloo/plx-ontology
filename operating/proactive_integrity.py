"""Integrity alerts a finance or operations lead acts on: stock written down by hand again and
again, open orders and purchases with accounts their own system blocks, general-ledger
batches that failed to post, and recurring tasks (calibration, cleaning, checks) that miss a
whole cycle. Run by ``operating.proactive_engine`` beside its other rules (each in its own
window, ``DETECTOR_SECONDS``), set-based over the whole store with index reads only. Nothing
here writes; every figure is a cited value or a sum of cited values of one item.

* ``inventory_adjustment``: Business Central item ledger entries of the two adjustment types
  (``ADJUST_TYPES``), grouped by their document. A document that moves ``BULK_ITEMS`` or
  more items one way is a bulk load or issue (the opening-balance load, a count import),
  and a document moving several items both ways is a conversion posted through the
  adjustment journal (assembly, batch production, repack): both are counted, never raised.
  What is left is a manual adjustment. An item is raised when it was adjusted down by hand
  on ``REPEAT_DOCS`` or more documents (``repeated``), or once by at least ``LARGE_SHARE`` of
  everything that ever came into it (``large``: receipts, outputs and positive entries of
  the ledger). Quantities are in the item's base unit (one item, never summed across
  items). Costs carry no currency on the ledger, so no money is stated; the entries are
  cited (finance-marked). A write-down reversed by a manual adjustment up of the same
  quantity cancels out (``reversed_pairs``). The ledger names no person, so the person or role who adjusted
  is ``person_not_served`` (counted), never guessed.
* ``blocked_account_activity``: a customer or supplier its own system blocks (``bc_blocked``:
  ``All``, ``Ship`` or ``Invoice`` for a customer, ``All`` for a supplier; a payment-only
  block never stops a purchase and is counted) that has open orders: customer orders by the
  one sales-order definition, purchase orders by the one purchase-order status definition,
  read on one record of each identity cluster and tied to the party (or a settled twin of
  it) by a settled link. The balance it states is in the event only (finance-marked), with
  its currency or not at all. Credit limits are set on no account, so exposure against the
  account's own history is ``aged_balance_worsening``'s. The claim rests on no closure
  being recorded: ``REQUIRES_GRANT``.
* ``gl_posting_errors``: Sage general-ledger posting errors grouped by the error batch
  posting moved them into; an error batch still open (``batchstat`` of that batch,
  ``OPEN_BATCH``; a status word this rule does not know is ``status_unmapped``, never open)
  whose errors were logged within ``GL_RECENT_DAYS`` is raised, one alert per cause (the
  error class most of a batch's errors share: one fix, such as opening the next fiscal
  year, settles every batch of it). The counts give the errors per
  month and per error class (``counts.by_month``, ``counts.by_class``) for the trend.
  Finance-marked.
* ``recurring_task_missed``: Planner tasks of one plan and one title (a date written into
  the title folded out, ``TITLE_DATE``; any other number keeps two series apart) with at least
  ``MIN_CYCLE_TASKS`` due dates form a recurring series; its cycle is the median gap between
  consecutive due dates. Planner sets a recurring task's due date on its schedule and most
  completions land within the week of it (measured on the live store), so a task done after
  its due date but before the next occurrence fell due is on time for its cycle; one done
  after the next was due, or still open then, missed a cycle. One alert per series with a
  missed cycle within ``CYCLE_RECENT_DAYS``. People are cleared first (assignees, creator,
  completer and the task's audience; an exclusion that is not loaded blocks the rule).

Fixture runs are ``isolated-fixture``; no rule names a record, a customer or an item.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
import json
import re
import statistics
from typing import Any, Callable, Iterable, Mapping, Sequence

from .proactive_business import _dec, _one, _tick
from .proactive_ops import (_assignee_ids, _audiences, _blocked_people, _cleared, _clip,
                            _instant, _plural)
from .proactive_stock import _touch

ADJUST_RULE = "inventory_adjustment"
BLOCKED_RULE = "blocked_account_activity"
GL_RULE = "gl_posting_errors"
CYCLE_RULE = "recurring_task_missed"
RULES = (ADJUST_RULE, BLOCKED_RULE, GL_RULE, CYCLE_RULE)
FINANCE = '["finance"]'

LEDGER = "ItemLedgerEntry"
LEDGER_ITEM = "ItemLedgerEntry.for_item"
LEDGER_PROPS = ("entry_type", "document_number", "item_number", "quantity")
ADJUST_TYPES = {"positive adjmt.": 1, "negative adjmt.": -1}
OTHER_TYPES = frozenset({"purchase", "sale", "transfer", "consumption", "output",
                         "assembly consumption", "assembly output"})
BULK_ITEMS = 20          # one document moving this many items one way is a load or an issue
REPEAT_DOCS = 3          # manual write-downs of one item on this many documents: repeated
LARGE_SHARE = Decimal("0.5")

PARTIES = {"Customer": ("SalesOrder", "SalesOrder.ordered_by", "Customer.same_record_as",
                        "bc_balance_due", frozenset({"all", "ship", "invoice"})),
           "Supplier": ("PurchaseOrder", "PurchaseOrder.ordered_from",
                        "Supplier.same_record_as", "bc_balance", frozenset({"all"}))}
NOT_BLOCKED = frozenset({"", "false", "no", "0"})
KNOWN_BLOCKS = frozenset({"all", "ship", "invoice", "payment"})
PARTY_PROPS = ("bc_blocked", "name", "erp_code", "code", "currency", "bc_balance_due",
               "bc_balance")

GL_ERROR = "RelSageGlPerr"
GL_BATCH = "RelSageGlBatch"
GL_PROPS = ("audtdate", "errdesc", "batchnbr", "errbatch")
OPEN_BATCH = frozenset({"1"})              # Sage GL batch status: open
SETTLED_BATCH = frozenset({"3", "4"})      # deleted, posted
GL_RECENT_DAYS = 90

TASK = "PlannerTask"
TASK_PROPS = ("title", "plan_id", "plan_title", "due_at", "completed_at", "assignees",
              "created_by", "completed_by")
MIN_CYCLE_TASKS = 3
# a date written into a recurring task's title (``... - 10-05-2026``): folded out of its
# series key; any other number (an instrument, a line) keeps series apart
TITLE_DATE = re.compile(r"\b(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b")
CYCLE_RECENT_DAYS = 90


def _word(raw: Any) -> str:
    """A Business Central enum word, its XML escapes (``_x0020_``) decoded, folded."""
    text = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), str(raw or ""))
    return " ".join(text.split()).lower()


def _value(props: Mapping[str, Any], name: str) -> tuple[Any, Mapping[str, Any] | None]:
    row = _one(props.get(name))
    return (row.get("value") if row else None), row


def _fmt(n: Decimal) -> str:
    text = format(n.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


# --------------------------------------------------------------------- adjustments

def inventory_adjustment(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                         result: Callable, settled: Sequence[str], skips: Callable
                         ) -> dict[str, Any]:
    skipped = skips()
    counts: Counter = Counter()
    found = st.values([LEDGER], LEDGER_PROPS)
    product_of: dict[str, str] = {}
    for link in _touch(st, found, [LEDGER_ITEM], settled):
        product_of.setdefault(link["from_object"], link["to_object"])
    docs: dict[str, list[tuple]] = defaultdict(list)
    inflow: dict[str, Decimal] = defaultdict(Decimal)
    rows_of: dict[str, list] = {}
    for oid, entry in sorted(found.items()):
        _tick(st)
        props = entry["props"]
        cause = [(oid, FINANCE)]
        kind, kind_row = _value(props, "entry_type")
        word = _word(kind)
        item, item_row = _value(props, "item_number")
        qty_raw, qty_row = _value(props, "quantity")
        qty = _dec(qty_raw) if qty_row else None
        if word not in ADJUST_TYPES and word not in OTHER_TYPES:
            skipped.note("entry_type_unmapped", [oid], cause)
            continue
        if not str(item or "").strip():
            skipped.note("item_unknown", [oid], cause)
            continue
        if qty is None:
            skipped.note("quantity_unreadable", [oid], cause)
            continue
        item = str(item).strip()
        if qty > 0:
            inflow[item] += qty
        if word not in ADJUST_TYPES:
            continue
        if (qty > 0) != (ADJUST_TYPES[word] > 0) and qty != 0:
            skipped.note("sign_disagrees_with_type", [oid], cause)
            continue
        number, number_row = _value(props, "document_number")
        if not str(number or "").strip():
            skipped.note("document_unknown", [oid], cause)
            continue
        rows_of[oid] = [r for r in (kind_row, item_row, qty_row, number_row) if r]
        docs[str(number).strip()].append((oid, item, qty))
    counts["person_not_served"] = sum(len(v) for v in docs.values())
    down: dict[str, list[tuple]] = defaultdict(list)
    up: dict[str, Decimal] = defaultdict(Decimal)
    ups: dict[str, list[Decimal]] = defaultdict(list)
    for number, entries in sorted(docs.items()):
        items = {e[1] for e in entries}
        signs = {e[2] > 0 for e in entries if e[2] != 0}
        if len(signs) == 1 and len(items) >= BULK_ITEMS:
            counts["bulk_load_entries" if True in signs else "bulk_issue_entries"] += \
                len(entries)
            counts["bulk_documents"] += 1
            continue
        if len(signs) > 1 and len(items) > 1:
            counts["conversion_entries"] += len(entries)
            counts["conversion_documents"] += 1
            continue
        counts["manual_entries"] += len(entries)
        counts["manual_documents"] += 1
        for oid, item, qty in entries:
            if qty < 0:
                down[item].append((number, oid, qty))
            else:
                up[item] += qty
                ups[item].append(qty)
    gaps = []
    for item, every in sorted(down.items()):
        _tick(st)
        # a write-down reversed by a manual adjustment up of the same quantity cancels out
        undo = Counter(ups.get(item, ()))
        entries = []
        for e in every:
            if undo[-e[2]] > 0:
                undo[-e[2]] -= 1
                counts["reversed_pairs"] += 1
            else:
                entries.append(e)
        if not entries:
            continue
        numbers = sorted({e[0] for e in entries})
        total = -sum((e[2] for e in entries), Decimal(0))
        largest = max(-e[2] for e in entries)
        came_in = inflow.get(item, Decimal(0))
        repeated = len(numbers) >= REPEAT_DOCS
        large = came_in > 0 and largest >= LARGE_SHARE * came_in
        if not repeated and not large:
            continue
        counts["repeated" if repeated else "large"] += 1
        oids = [e[1] for e in entries]
        anchor = product_of.get(oids[-1]) or oids[-1]
        share = f", the largest {_fmt((largest / came_in * 100).quantize(Decimal(1)))}% " \
            "of all that came in" \
            if came_in > 0 else ""
        made = gap(
            ADJUST_RULE, ADJUST_RULE, anchor,
            "Product" if anchor != oids[-1] else LEDGER, f"{item}|down",
            summary=f"Item {_clip(item, 40)} was adjusted down by hand "
                    f"{_plural(len(entries), 'time')} on {_plural(len(numbers), 'document')} "
                    f"({_fmt(total)} in its base unit{share}; "
                    f"{_fmt(up.get(item, Decimal(0)))} adjusted up by hand)",
            missing="Why was the stock written down, and is it loss, damage, a counting "
                    "error or a posting that belongs elsewhere?",
            why="Manual negative adjustments in the item ledger (bulk loads and conversions "
                "posted through the adjustment journal set aside), each entry cited.",
            priority="high" if large else "medium", about=(LEDGER, "quantity"),
            groups=[cites(rows_of[o], FINANCE) for o in oids[-20:]],
            event={"orders_affected": len(numbers)})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"entries": len(found), **dict(sorted(counts.items()))}
    return out


# --------------------------------------------------------------------- blocked accounts

def _clusters(links: Iterable[Mapping[str, Any]], members: Iterable[str]) -> dict[str, str]:
    """member -> the least id of its settled identity cluster."""
    parent = {m: m for m in members}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for link in links:
        a, b = find(link["from_object"]), find(link["to_object"])
        if a != b:
            parent[max(a, b)] = min(a, b)
    return {m: find(m) for m in list(parent)}


def blocked_account_activity(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                             result: Callable, settled: Sequence[str], skips: Callable,
                             order_state: Mapping[str, Callable[[], Callable[[str], Any]]],
                             absence: Callable[[Sequence[str]], list[str]] | None = None
                             ) -> dict[str, Any]:
    skipped = skips()
    counts: Counter = Counter()
    gaps = []
    for party_type, (order_type, order_link, twin_link, balance_prop, stops) in \
            PARTIES.items():
        found = st.values([party_type], PARTY_PROPS)
        blocked: dict[str, tuple] = {}
        for oid, entry in sorted(found.items()):
            _tick(st)
            raw, row = _value(entry["props"], "bc_blocked")
            if row is None:
                continue
            word = _word(raw)
            if word in NOT_BLOCKED:
                continue
            if word not in KNOWN_BLOCKS:
                skipped.note("blocked_unmapped", [oid], [(oid, "[]")])
                continue
            counts[f"{party_type.lower()}_blocked"] += 1
            if word not in stops:
                counts[f"{party_type.lower()}_block_not_stopping_orders"] += 1
                continue
            blocked[oid] = (word, row)
        if not blocked:
            continue
        # the blocked parties' settled twins (by the link table's end indexes, a few hops)
        twins: dict[str, Mapping] = {}
        reach = set(blocked)
        for _ in range(3):
            fresh = [l for l in _touch(st, reach, [twin_link], settled)
                     if l["link_id"] not in twins]
            if not fresh:
                break
            twins.update((l["link_id"], l) for l in fresh)
            reach |= {e for l in fresh for e in (l["from_object"], l["to_object"])}
        root = _clusters(twins.values(), reach)
        family: dict[str, set] = defaultdict(set)
        for member, top in root.items():
            family[top].add(member)
        open_orders: dict[str, dict[str, Mapping]] = defaultdict(dict)
        wanted = {root.get(b, b) for b in blocked}
        touched = [l for l in _touch(st, reach, [order_link], settled)
                   if l["to_object"] in reach]
        # order record -> (its cluster, status class); read only when an order is tied
        state = order_state[order_type]() if touched else None
        for link in touched:
            _tick(st)
            top = root.get(link["to_object"], link["to_object"])
            if top not in wanted:
                continue
            read = state(link["from_object"])
            if read is None:
                counts["order_not_read"] += 1
                continue
            order_top, cls = read
            if cls == "open":
                # one order per order cluster: its twins share one verdict
                open_orders[top].setdefault(order_top, link)
            elif cls == "unknown":
                counts["order_status_unknown"] += 1
        for oid, (word, block_row) in sorted(blocked.items()):
            top = root.get(oid, oid)
            orders = {link["from_object"]: link
                      for link in (open_orders.get(top) or {}).values()}
            if not orders:
                continue
            props = found[oid]["props"]
            name, name_row = _value(props, "name")
            code, code_row = _value(props, "erp_code")
            if code_row is None:
                code, code_row = _value(props, "code")
            label = _clip(name or code or "", 50)
            order_rows = st.values([order_type], ("order_number", "po_number"),
                                   objects=sorted(orders))
            order_groups = []
            for order in sorted(orders):
                rows = [r for p in ("order_number", "po_number")
                        for r in ((order_rows.get(order) or {}).get("props") or {}).get(p, ())]
                if rows:
                    order_groups.append(cites(rows, orders[order]["markings"]))
            if not order_groups:
                skipped.note("orders_uncited", [oid], [(oid, "[]")])
                continue
            balance, balance_row = _value(props, balance_prop)
            currency, currency_row = _value(props, "currency")
            amount = _dec(balance) if balance_row else None
            event: dict[str, Any] = {"orders_affected": len(order_groups)}
            if amount is not None and str(currency or "").strip():
                event["amount"] = {
                    "value": _fmt(amount), "currency": str(currency).strip(),
                    "basis": balance_prop, "handle": balance_row["l1_handle"],
                    "currency_handle": currency_row["l1_handle"],
                    "markings": (cites([balance_row], FINANCE)
                                 or [(None, None, '["unreadable"]')])[0][2]}
            what = "customer" if party_type == "Customer" else "supplier"
            orders_word = "customer order" if party_type == "Customer" else "purchase order"
            made = gap(
                BLOCKED_RULE, BLOCKED_RULE, oid, party_type, f"{party_type}|blocked",
                summary=f"Blocked {what} {label} ({_word(block_row['value'])}) has "
                        f"{_plural(len(order_groups), 'open ' + orders_word)}",
                missing=f"Should these orders go ahead while the {what} is blocked, or "
                        "should the block be lifted?",
                why=f"The {what}'s own system blocks it, and its orders are open by the "
                    "one order status definition (the block and each order cited).",
                priority="high", about=(party_type, "bc_blocked"),
                groups=[cites([block_row], FINANCE), *order_groups[:20]],
                extra=cites([r for r in (name_row, code_row) if r]), event=event)
            if made is not None:
                made["requires"] = absence([order_type]) if absence is not None else []
            counts[f"{party_type.lower()}_with_open_orders"] += 1
            gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = dict(sorted(counts.items()))
    return out


# --------------------------------------------------------------------- GL posting errors

def _sage_day(raw: Any) -> date | None:
    text = str(raw or "").strip()
    if not re.fullmatch(r"\d{8}", text) or text == "00000000":
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _error_class(text: Any) -> str:
    """An error description with its numbers and codes folded out (``account #``)."""
    flat = re.sub(r"[A-Z0-9]*\d[A-Z0-9-]*", "#", " ".join(str(text or "").split()))
    return flat[:60].lower() or "unstated"


def gl_posting_errors(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                      result: Callable, settled: Sequence[str], skips: Callable
                      ) -> dict[str, Any]:
    skipped = skips()
    counts: Counter = Counter()
    by_month: Counter = Counter()
    by_class: Counter = Counter()
    found = st.values([GL_ERROR], GL_PROPS)
    batches = st.values([GL_BATCH], ("batchid", "batchstat"))
    batch_of: dict[str, tuple] = {}
    for oid, entry in batches.items():
        bid, _ = _value(entry["props"], "batchid")
        stat, stat_row = _value(entry["props"], "batchstat")
        key = str(bid or "").strip()
        if key:
            batch_of.setdefault(key, []).append((oid, str(stat or "").strip(), stat_row))
    groups: dict[str, list] = defaultdict(list)
    for oid, entry in sorted(found.items()):
        _tick(st)
        props = entry["props"]
        day = _sage_day(_value(props, "audtdate")[0])
        if day is None:
            skipped.note("date_unknown", [oid], [(oid, FINANCE)])
            continue
        by_month[day.strftime("%Y-%m")] += 1
        by_class[_error_class(_value(props, "errdesc")[0])] += 1
        target = str(_value(props, "errbatch")[0] or "").strip() or \
            str(_value(props, "batchnbr")[0] or "").strip()
        if not target:
            skipped.note("batch_unknown", [oid], [(oid, FINANCE)])
            continue
        groups[target].append((oid, day, props))
    recent = as_of - timedelta(days=GL_RECENT_DAYS)
    gaps = []
    by_cause: dict[str, list] = defaultdict(list)
    for target, errors in sorted(groups.items()):
        _tick(st)
        held = batch_of.get(target) or []
        if len(held) != 1:
            skipped.note("batch_not_found" if not held else "batch_ambiguous",
                         [e[0] for e in errors], [(e[0], FINANCE) for e in errors])
            continue
        (batch_oid, stat, stat_row), = held
        if stat_row is None:
            skipped.note("status_unknown", [batch_oid], [(batch_oid, FINANCE)])
            continue
        if stat in SETTLED_BATCH:
            counts["errors_in_settled_batches"] += len(errors)
            continue
        if stat not in OPEN_BATCH:
            skipped.note("status_unmapped", [batch_oid], [(batch_oid, FINANCE)])
            continue
        counts["errors_in_open_batches"] += len(errors)
        last = max(e[1] for e in errors)
        if last < recent:
            counts["open_batches_older"] += 1
            continue
        classes = Counter(_error_class(_value(e[2], "errdesc")[0]) for e in errors)
        counts["open_batches_recent"] += 1
        by_cause[classes.most_common(1)[0][0]].append((last, batch_oid, stat_row, errors))
    # one alert per cause: the open batches whose errors are mostly of one class (one fix,
    # such as opening the next fiscal year, settles them all)
    for cause, held in sorted(by_cause.items()):
        held.sort(key=lambda h: (h[0], h[1]))
        last, newest = held[-1][0], held[-1][1]
        entries = sum(len(h[3]) for h in held)
        made = gap(
            GL_RULE, GL_RULE, newest, GL_BATCH, f"open|{cause}",
            summary=f"{_plural(len(held), 'general-ledger batch').replace('batchs', 'batches')} still open with "
                    f"{entries} entr{'y' if entries == 1 else 'ies'} that failed to post, "
                    f"mostly \"{_clip(cause, 50)}\" (last {last.isoformat()})",
            missing="Can the entries be corrected (period, year, account) and posted before "
                    "close?",
            why="Sage logged posting errors for these entries and the batches they were "
                "moved into are still open (each batch status and error cited).",
            priority="high" if entries >= 10 else "medium",
            about=(GL_BATCH, "batchstat"),
            groups=[cites([h[2]], FINANCE) for h in held[-10:]]
            + [cites([r for p in ("errdesc", "audtdate") for r in e[2].get(p, ())], FINANCE)
               for h in held[-10:] for e in h[3][:2]],
            event={"days": (as_of - last).days, "orders_affected": len(held)})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"errors": len(found), **dict(sorted(counts.items())),
                     "by_month": dict(sorted(by_month.items())[-24:]),
                     "by_class": dict(by_class.most_common(10))}
    return out


# --------------------------------------------------------------------- recurring tasks

def _person(raw: Any) -> list[str] | None:
    """The id a creator or completer value states; None: unreadable (never cleared)."""
    if raw in (None, ""):
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw.strip()[:1] in "{[\"" else raw
    except (TypeError, ValueError):
        return None
    if isinstance(value, Mapping):
        user = value.get("user") if isinstance(value.get("user"), Mapping) else value
        ident = user.get("id") or user.get("user_id")
        return [ident] if isinstance(ident, str) and ident else None
    return [value] if isinstance(value, str) and value.strip() else None


def _people_at(st: Any, tasks: Iterable[str], link_type: str) -> dict[str, list[str]]:
    """task -> the ids of the people it links, every tier (a lead link to an excluded person
    still keeps the task from being judged), read by the link table's end indexes at the
    tasks; a person stated by no id is "" (never cleared)."""
    ties = [r for r in st.touching(sorted(set(tasks)), [link_type]) if not r[7]]
    found = st.values(["Person"], ("object_id", "upn"), objects=sorted({r[4] for r in ties}))
    out: dict[str, list[str]] = defaultdict(list)
    for r in ties:
        props = (found.get(r[4]) or {}).get("props") or {}
        row = _one(props.get("object_id")) or _one(props.get("upn"))
        out[r[3]].append(str(row["value"]) if row else "")
    return out


def recurring_task_missed(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                          result: Callable, skips: Callable, people: Any,
                          settled: Sequence[str]) -> dict[str, Any]:
    blocked = _blocked_people(people, result)
    if blocked is not None:
        return blocked
    skipped = skips()
    counts: Counter = Counter()
    found = st.values([TASK], TASK_PROPS)
    linked = _people_at(st, found, "PlannerTask.with_person")
    audience = _audiences(st, found)
    series: dict[tuple, list] = defaultdict(list)
    for oid, entry in sorted(found.items()):
        _tick(st)
        props = entry["props"]
        ids = [*linked[oid]]
        for name, read in (("assignees", _assignee_ids), ("created_by", _person),
                           ("completed_by", _person)):
            raw, row = _value(props, name)
            got = read(raw) if row is not None else []
            if got is None:
                ids = None
                break
            ids.extend(got)
        if ids is None or not people.unit_clear(audience.get(oid, "")) \
                or not _cleared(people, ids):
            skipped.note("person_not_cleared", [oid], [(oid, "[]")])
            continue
        due_raw, due_row = _value(props, "due_at")
        due = _instant(due_raw) if due_row else None
        if due is None:
            counts["undated"] += 1
            continue
        done_raw, done_row = _value(props, "completed_at")
        done = _instant(done_raw) if done_row and str(done_raw or "").strip() else None
        plan = str(_value(props, "plan_id")[0] or _value(props, "plan_title")[0] or "")
        title = " ".join(TITLE_DATE.sub(" ", str(_value(props, "title")[0] or ""))
                         .split()).lower().strip(" -:|")
        series[(plan, title)].append((due.date(), done.date() if done else None, oid,
                                      [r for r in (due_row, done_row) if r]))
    gaps = []
    recent = as_of - timedelta(days=CYCLE_RECENT_DAYS)
    for (plan, title), tasks in sorted(series.items()):
        _tick(st)
        tasks.sort(key=lambda t: (t[0], t[2]))
        dues = sorted({t[0] for t in tasks})
        if len(dues) < MIN_CYCLE_TASKS:
            counts["one_off_tasks"] += len(tasks)
            continue
        counts["series"] += 1
        cycle = int(statistics.median([(b - a).days for a, b in zip(dues, dues[1:])]))
        if cycle <= 0:
            counts["series_without_cycle"] += 1
            continue
        missed = []
        for due, done, oid, rows in tasks:
            next_due = due + timedelta(days=cycle)
            counts["tasks_in_series"] += 1
            if done is not None and done > due:
                counts["done_after_due"] += 1
            if (done is not None and done > next_due) or (done is None and next_due < as_of):
                counts["missed_cycle"] += 1
                if next_due >= recent:
                    missed.append((due, done, oid, rows))
            elif done is not None and done > due:
                counts["done_within_cycle"] += 1
        if not missed:
            continue
        first = found[missed[-1][2]]["props"]
        title_shown = _clip(_value(first, "title")[0], 60) or "untitled"
        plan_shown = _clip(_value(first, "plan_title")[0], 40)
        still_open = sum(1 for m in missed if m[1] is None)
        made = gap(
            CYCLE_RULE, CYCLE_RULE, missed[-1][2], TASK, f"{plan}|{title}",
            summary=f"Recurring task {title_shown}" + (f" in {plan_shown}" if plan_shown
                                                       else "")
                    + f" (every {_plural(cycle, 'day')}) missed "
                    + f"{_plural(len(missed), 'cycle')} in the last {CYCLE_RECENT_DAYS} days"
                    + (f", {still_open} still open" if still_open else ""),
            missing="Was the check done and not recorded, or was it skipped?",
            why="Each occurrence was completed after the next one fell due, or is still open "
                "past it (each due date and completion cited); completions within the cycle "
                "are not counted as missed.",
            priority="high" if still_open else "medium", about=(TASK, "due_at"),
            groups=[cites(m[3]) for m in missed[-20:]],
            event={"days": (as_of - missed[-1][0]).days})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"tasks": len(found), **dict(sorted(counts.items()))}
    return out
