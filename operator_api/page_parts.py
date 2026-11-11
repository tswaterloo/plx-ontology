"""Parts of a record page: page stamps and inbox state, section plans, customer summary,
plan runs (late, landed cost, people), order state, links and their evidence, and the
source payload of a cited row.

Moved from ``operator_api.backend`` by concern (no behaviour change); every name is
re-exported there."""

from __future__ import annotations

import contextvars
import hashlib
import functools
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from . import models as m
from . import presentation
from .serving import (MAX_SOURCES_PER_FACT, citation_handle, object_url, plain,
    record_words, system_of)
from .answers import (CUSTOMER_ORDERS, _CURRENCY, _DECIMAL, _aggregate_read,
    _decimal_money, _grouped, _related, hidden_from_counts, words_of)
from .suggest import (system_word)


def _delta_layers(view: Any) -> Any:
    """The delta layers the reader's host answers with (``delta_layer_stamp``), so a kept
    page is new with each delta cycle; a host that cannot say keys nothing kept."""
    stamp = getattr(view.access, "delta_layer_stamp", None)
    return stamp() if callable(stamp) else None


def overlay_stamp(view: Any) -> Any:
    """The row-delta overlay the reader's host answers with now (inside its ``_request``);
    None without one."""
    stamp = getattr(getattr(view, "access", None), "overlay_stamp", None)
    try:
        return stamp() if callable(stamp) else None
    except Exception:  # noqa: BLE001 - an unreadable stamp keys nothing kept
        return ("unreadable", time.monotonic())


def watcher_stamp(view: Any) -> Optional[tuple]:
    """The AI watcher's state beside the store (a run, a dismissal or its switch is a new
    key; ``operating.ai_watcher.stamp``), or None without a store."""
    from operating import ai_watcher
    path = getattr(getattr(view, "store_status", None), "store_path", None)
    return None if path is None else ai_watcher.stamp(Path(path))


def inbox_stamp(view: Any) -> Optional[tuple]:
    """The gap inbox's content now, but for the readers' "seen" marks (``inbox_content``),
    or None when the workspace names no store. A "seen" mark changes no list, count or
    card but the briefing's, whose key also holds the reader's own last visit
    (``last_visit_of``): one reader's visit never makes every reader's Home read again."""
    from operating.proactive_engine import inbox_path
    path = getattr(getattr(view, "store_status", None), "store_path", None)
    return None if path is None else inbox_state(inbox_path(path))


def inbox_state(inbox: Path) -> tuple:
    """``inbox_content`` of the inbox file as it is now."""
    out = []
    for file in (inbox, inbox.with_name(inbox.name + "-wal")):
        try:
            found = os.stat(file)
            out.append((found.st_mtime_ns, found.st_size))
        except OSError:
            out.append(None)
    return inbox_content(inbox, tuple(out))


# What a "seen" mark writes (``proactive_engine.record_visit``): left out of the inbox's
# content stamp.
INBOX_VISIT_TABLES = frozenset({"visits"})
_INBOX_CONTENT: dict[tuple, tuple] = {}   # (inbox, file state) -> its content stamp
_INBOX_CONTENT_LOCK = threading.Lock()


def inbox_content(inbox: Path, files: tuple) -> tuple:
    """A digest of every table of the inbox but the "seen" marks, read once per file
    state ``files`` (the file's and its write-ahead log's change time and size); the file
    state itself when there is no inbox or it cannot be read (a change is then a new
    key, as any write is)."""
    if files[0] is None:
        return files
    key = (str(inbox), files)
    with _INBOX_CONTENT_LOCK:
        held = _INBOX_CONTENT.get(key)
    if held is not None:
        return held
    try:
        conn = sqlite3.connect(f"file:{inbox}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return files
    try:
        digest = hashlib.blake2b(digest_size=16)
        conn.execute("BEGIN")   # one snapshot of every table
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            if row[0] not in INBOX_VISIT_TABLES]
        for table in tables:
            digest.update(b"\0table\0" + table.encode())
            rows = sorted(repr(tuple(row)) for row in conn.execute(
                'SELECT * FROM "' + table.replace('"', '""') + '"'))
            for row in rows:
                digest.update(row.encode() + b"\0")
        stamp = ("content", digest.hexdigest())
    except sqlite3.Error:
        return files
    finally:
        conn.close()
    with _INBOX_CONTENT_LOCK:
        if len(_INBOX_CONTENT) >= 64:
            _INBOX_CONTENT.clear()
        _INBOX_CONTENT[key] = stamp
    return stamp


def identity_of(view: Any) -> Optional[tuple]:
    """The reader's identity in the sources (``ObjectWorkspace.identity``: addresses and
    object ids), for the briefing's key: an address confirmed or removed is a new key.
    None when the view names none."""
    get = getattr(view, "identity", None)
    if not callable(get):
        return None
    try:
        found = get()
    except Exception:  # noqa: BLE001 - undecidable: a key of its own, read again
        return ("unreadable", time.monotonic())
    return (tuple(sorted(getattr(found, "addresses", ()))),
            tuple(sorted(getattr(found, "object_ids", ()))), getattr(found, "basis", None))


def last_visit_of(view: Any) -> tuple:
    """("visit", when this reader last marked its briefing seen): the briefing's own key
    part (its "new since your visit" marks and counts read it)."""
    from operating.proactive_engine import inbox_path, last_visit, reader_key as visitor
    path = getattr(getattr(view, "store_status", None), "store_path", None)
    if path is None:
        return ("visit", None)
    return ("visit", last_visit(inbox_path(path), visitor(view.actor)))


def _search_marked(view: Any, oid: str) -> None:
    """KeyError (not found, as any record the reader may not see) when the document's
    search chunks carry a marking the reader does not hold (a pay stub, an offer letter,
    a payroll-labelled file: ``operating.search.document_marks``)."""
    from operating.search import document_marks
    if not document_marks.enabled():
        return
    try:
        grant = getattr(view.access, "grant", None)
        held = frozenset(grant()[1]) if callable(grant) else frozenset()
    except Exception:  # noqa: BLE001 - undecidable: nothing held (fail closed)
        held = frozenset()
    if oid in document_marks.withheld([oid], held):
        raise KeyError("object_not_found")


def merged(page: m.ObjectPage, section: m.ObjectSection) -> m.ObjectPage:
    """``page`` with ``section`` merged as the app merges it (a status, the not-found
    items the trail restates, and the section's own parts)."""
    update: dict[str, Any] = {}
    if section.section == "late":
        if section.status is not None:
            update["status"] = section.status
        update["late_lines"] = section.late_lines
    elif section.section == "people":
        update["people"] = section.people
    elif section.section == "landed_cost":
        update["landed_cost"] = section.landed_cost
    elif section.section == "margin":
        update["profitability"] = section.profitability
    elif section.section == "knows":
        update["knows"] = section.knows
    elif section.section == "trail":
        update["trail"] = section.trail
        if section.not_found is not None:
            update["not_found"] = section.not_found
        if section.proposals is not None:
            update["proposals"] = section.proposals
    return page.model_copy(update=update)


IMPACT_MODES = ("what_if", "why")


def impact_change_words(change: Mapping[str, Any]) -> str:
    kind = change.get("kind")
    if kind == "delay":
        return f"delayed {change.get('days')} days"
    if kind == "price_change":
        return f"price changed {change.get('percent')}%"
    return {"out_of_stock": "out of stock", "customer_lost": "customer lost"}.get(
        str(kind), str(kind))


# The plan (``ontology/plans``) each section runs, if any.
SECTION_PLANS = {"late": "late_po", "people": "people_on_record",
                 "landed_cost": "landed_cost"}
# A page's sections that run a plan (read in order, in one read) and the rest (each read
# on its own, on the section pool when it serves).
PLAN_SECTIONS = tuple(s for s in m.PAGE_SECTIONS if s in SECTION_PLANS)
POOL_SECTIONS = tuple(s for s in m.PAGE_SECTIONS if s not in SECTION_PLANS)


# The records whose page has a trail section: a purchase order's lateness trail, and the
# materials walk of a customer order or a customer (``trail.for_page``).
TRAIL_TYPES = ("PurchaseOrder", "SalesOrder", "Customer")


def plans_before(section: str) -> tuple[str, ...]:
    """The plans of the page's sections read before ``section`` (``m.PAGE_SECTIONS``
    order) by the page's full read."""
    names = list(m.PAGE_SECTIONS)
    before = names[:names.index(section)] if section in names else []
    return tuple(SECTION_PLANS[n] for n in before if n in SECTION_PLANS)


# What a customer's record is summed and listed from: (link from the record walked in
# reverse, its date property, what the sum is called). Each total is the records' own
# ``subtotal`` (excluding tax) per currency over the last ``CUSTOMER_DAYS``.
CUSTOMER_SUMS = (("SalesInvoice.billed_to", "invoice_date", "Invoiced (excluding tax)"),
                 ("SalesOrder.ordered_by", "order_date", "Ordered (excluding tax)"))
CUSTOMER_DAYS = 365
CUSTOMER_ROWS = 5
_CLOSED = re.compile(r"clos|cancel|complet|shipped|invoic|deliver|void|posted|fulfilled",
                     re.IGNORECASE)


def _cited(backend: "WorkspaceBackend", view: Any, citations: Iterable[Any],
           limit: int = 3) -> list[m.Source]:
    """The first ``limit`` cited records the reader may open, as sources."""
    handles = list(dict.fromkeys(c.get("l1_handle") for c in citations or ()
                                 if isinstance(c, Mapping) and isinstance(c.get("l1_handle"),
                                                                          str)))
    decided = view.access.decide(handles) if handles else {}
    return [backend._source(view, h) for h in handles if decided.get(h) is None][:limit]


def page_knows(view: Any, oid: str) -> Optional[m.PersonKnows]:
    """A person's page section from ``plx.aggregate`` mode ``who_knows`` read as this
    reader (``operating.who_knows``: the people exclusion first, counts only); None when
    the reader may not see the person or the mode refused."""
    got = view._call("plx.aggregate", {"mode": "who_knows", "person": oid,
                                       "measure": {"op": "count"}, "object_type": "Person"})
    if not isinstance(got, Mapping) or got.get("error"):
        return None
    found = [p for p in got.get("people") or () if isinstance(p, Mapping)
             and p.get("person") == oid]
    if not found:
        return None
    one = found[0]

    def records(kind: str) -> list[m.KnownRecord]:
        out = []
        for r in one.get(kind) or ():
            rid = str(r.get("object_id") or "")
            # the record's name arrives as source text in its envelope (shown, never run)
            label = r.get("label")
            label = label.get("untrusted_text") if isinstance(label, Mapping) else label
            out.append(m.KnownRecord(object_id=rid, label=str(label or "")[:120],
                                     url=object_url(rid.split(":", 1)[0], rid),
                                     messages=int(r.get("messages") or 0)))
        return out
    waiting = one.get("waiting_on_them")
    return m.PersonKnows(messages=int(one.get("messages") or 0), customers=records("customers"),
                         suppliers=records("suppliers"), items=records("items"),
                         waiting_on_them=waiting if isinstance(waiting, int) else None,
                         complete=bool(got.get("complete")))


def _refusal(code: Optional[str]) -> str:
    """Why a sum is not shown, as the tool said it: a unit or currency mix, or amounts
    this reader may not read (a marking it does not hold), never one for the other."""
    words = words_of(code)
    if re.search(r"unit|currenc|mix|role|not.numeric", words):
        return f"the amounts could not be summed ({words})"
    if re.search(r"withheld|marking|grant|access|permission|inaccessible", words):
        return "the amounts are not shown to you (they need access you do not hold)"
    return f"the amounts could not be read ({words or 'refused'})"


def customer_summary(backend: "WorkspaceBackend", view: Any, oid: str
                     ) -> tuple[m.CustomerSummary, dict[str, str], dict[str, str]]:
    """A customer's revenue, open and recent orders, contacts and copies in other systems,
    from what its own record links to as this reader reads it (``plx.around`` then
    ``plx.aggregate``: exact Decimal sums per currency, each with the records summed).
    Never a figure read from prose and never another record's links: a copy in another
    system is listed to open, not added in. Also returns the orders' displays by object
    id and their statuses by number (the timeline names them)."""
    today = backend.today() if backend.today is not None else date.today()
    since = (today - timedelta(days=CUSTOMER_DAYS)).isoformat()
    period = f"last 12 months (since {since})"
    out = m.CustomerSummary()
    for link, when, label in CUSTOMER_SUMS:
        handle, shown = _related(view, oid, link, filters=[
            {"property": when, "op": "gte", "value": since}], pages=1)
        if handle is None or not shown:
            continue
        groups, refused = _aggregate_read(view, handle, {"property": "subtotal", "op": "sum"})
        if groups is None:
            out.not_established.append(f"{label}, {period}: {_refusal(refused)}")
            continue
        for group in groups:
            money = _decimal_money(group.get("value"), group.get("currency"))
            cites = group.get("inputs_citations") or ()
            if money is None:
                continue
            system = system_of(next((c.get("l1_handle") for c in cites
                                     if isinstance(c, Mapping)), "") or "")
            out.revenue.append(m.CustomerAmount(
                label=label, period=period, money=money, count=int(group.get("inputs") or 0),
                system=system, sources=_cited(backend, view, cites)))
    handle, orders = _related(view, oid, CUSTOMER_ORDERS)
    status: dict[str, str] = {}
    if handle is not None and orders:
        # the set's own size (a count over the whole set), never the rows one read
        # returned; unknown, it is said as "at least"
        counted = _grouped(view, handle, {"op": "count"})
        total = next((int(g["value"]) for g in counted or ()
                      if str(g.get("value") or "").isdigit()), None)
        if total is not None:
            out.orders_total = total
        else:
            out.orders_at_least = len(orders)
        by_number = {label: obj for obj, label in orders.items()}

        def rows_of(filters: Sequence[Mapping[str, Any]]) -> list[m.OrderRow]:
            """The orders matching ``filters``, each with its date, status and total
            (two small reads over the narrowed set: a result is bounded in size)."""
            narrowed, _ = _related(view, oid, CUSTOMER_ORDERS, filters=filters, pages=1)
            if narrowed is None:
                return []
            states = _grouped(view, narrowed, {"op": "count"}, ["status", "order_number"])
            for g in states or ():
                key = g.get("key") or {}
                status[str(plain(key.get("order_number")))] = \
                    words_of(plain(key.get("status"))).capitalize()
            found = []
            groups, _ = _aggregate_read(view, narrowed, {"property": "subtotal", "op": "sum"},
                                        ["order_date", "order_number"])
            if groups is None:   # amounts this reader may not read: the orders, unpriced
                groups = [{**g, "value": None, "inputs_citations": ()} for g in _grouped(
                    view, narrowed, {"op": "count"}, ["order_date", "order_number"]) or ()]
            for group in groups:
                key = {k: plain(v) for k, v in (group.get("key") or {}).items()}
                number = key.get("order_number")
                if not isinstance(number, str) or not number:
                    continue
                target = by_number.get(number)
                found.append(m.OrderRow(
                    object_id=target or number, label=number,
                    open_url=object_url("SalesOrder", target or number),
                    date=key.get("order_date") if isinstance(key.get("order_date"), str)
                    else None,
                    status=status.get(number) or None,
                    amount=_decimal_money(group.get("value"), group.get("currency")),
                    sources=_cited(backend, view, group.get("inputs_citations") or (), 1)))
            return sorted(found, key=lambda r: (r.date or "", r.label), reverse=True)

        for days in (90, CUSTOMER_DAYS, None):   # the latest orders, in a bounded read
            window = [] if days is None else [{"property": "order_date", "op": "gte",
                                               "value": (today - timedelta(days=days))
                                               .isoformat()}]
            recent = rows_of(window)
            if len(recent) >= CUSTOMER_ROWS or days is None:
                break
        out.recent_orders = recent[:CUSTOMER_ROWS]
        counts = [g for g in _grouped(view, handle, {"op": "count"}, ["status"]) or ()
                  if plain((g.get("key") or {}).get("status"))]
        if counts:
            states = {plain((g.get("key") or {}).get("status")): int(g.get("value") or 0)
                      for g in counts}
            open_states = sorted(str(k) for k in states if k and not _CLOSED.search(str(k)))
            out.open_orders = sum(states[k] for k in open_states)
            if open_states:
                out.open_list = rows_of([{"property": "status", "op": "in",
                                          "value": open_states[:50]}])[:CUSTOMER_ROWS]
        else:
            out.not_established.append("Open orders: no order status is recorded")
    elif not out.revenue:
        out.not_established.append("Orders: none linked to this customer record")
    kind = oid.split(":", 1)[0]
    twins = [obj for direction in ("forward", "reverse") for obj in view._call(
        "plx.around", {"from": oid, "tier_floor": "verified", "limit": 10, "path": [
            {"link_type": f"{kind}.same_record_as", "direction": direction}]}
    ).get("objects") or ()]
    seen: set[str] = set()
    for obj in twins:
        if not isinstance(obj, Mapping) or obj.get("object_id") in seen:
            continue
        seen.add(str(obj.get("object_id")))
        if isinstance(obj, Mapping) and isinstance(obj.get("object_id"), str) \
                and obj["object_id"] != oid:
            out.also_in.append(m.ObjectRef(
                object_id=obj["object_id"], type=str(obj.get("object_type") or "Customer"),
                label=f"{plain(obj.get('display')) or obj['object_id']} "
                      f"({system_word(obj['object_id'])})"[:200],
                open_url=object_url(str(obj.get("object_type") or "Customer"),
                                    obj["object_id"])))
    out.not_established.append("Contacts: none captured for this customer's record")
    return out, orders, {n: st for n, st in status.items() if st}


def label_timeline(view: Any, page: m.ObjectPage, labels: Mapping[str, str],
                   status: Mapping[str, str], events: Sequence[Mapping[str, Any]]) -> None:
    """Each timeline row names the record it is about (an order's number) and, when
    known, that record's status: "Required" alone says nothing."""
    about_of = []
    for event in events:
        others = [o.get("object_id") for o in event.get("objects") or ()
                  if isinstance(o, Mapping) and o.get("object_id") != page.object_id]
        about_of.append(next((o for o in others if isinstance(o, str)), None))
    labels, read_status = dict(labels), {}
    for oid in list(dict.fromkeys(o for o in about_of if o and not labels.get(o)))[:40]:
        card = view._call("plx.object", {"object_id": oid, "properties": ["status"]}) or {}
        labels[oid] = plain((card.get("object") or {}).get("display"))
        read_status[oid] = next((plain(v.get("value")) for v in card.get("values") or ()
                                 if isinstance(v, Mapping) and v.get("property") == "status"),
                                None)
    for row, about in zip(page.timeline, about_of):
        if about is None:
            continue
        name = labels.get(about)
        if not name or name == about:
            continue   # no display to name it by: the row stays as it was
        kind = words_of(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", about.split(":", 1)[0])).lower()
        row.title = f"{row.title}: {kind} {name}"[:200]
        state = status.get(name) or words_of(read_status.get(about)).capitalize()
        if state:
            row.summary = f"Status: {state}"


def page_handles(value: Any) -> set[str]:
    """Every source handle a page payload shows."""
    found: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, m.Source):
            found.add(item.handle)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif hasattr(item, "model_fields"):
            for name in type(item).model_fields:
                walk(getattr(item, name))
    walk(value)
    return found


PAGE_PLANS = ("late_po", "landed_cost", "people_on_record")
_PLANS_RUN: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "plx_page_plans", default=None)


@contextmanager
def plans_once() -> Iterator[None]:
    """One page read (``object_page``, ``object_section``, ``page_sections``): each plan
    over a record runs once in it, in the page's order, and every section after it reuses
    that run (``plan_runs``), as one ``plan_runs`` over the page's plans would; a read
    inside another is part of it. Never kept past the read."""
    if _PLANS_RUN.get() is not None:
        yield
        return
    token = _PLANS_RUN.set({})
    try:
        yield
    finally:
        _PLANS_RUN.reset(token)


def _entries(ran: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    answer = (ran.get("investigation") or {}).get("answer")
    return [e for e in answer if isinstance(e, Mapping)] if isinstance(answer, list) else []


def money_of(entry: Mapping[str, Any]) -> Optional[m.Money]:
    """A computed entry's amount as the plan computed it: its ``value`` (a decimal string,
    full precision) in its ``currency`` or ``unit`` when that is a currency code; a
    per-unit amount (``USD/ea``), a quantity or text is no money (None)."""
    value = entry.get("value")
    currency = entry.get("currency") if isinstance(entry.get("currency"), str) \
        else entry.get("unit")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not _DECIMAL.match(value.strip()) \
            or not isinstance(currency, str) or not _CURRENCY.match(currency):
        return None
    if entry.get("unit") not in (None, currency):
        return None   # a currency beside another unit: a rate, never a total
    return m.Money(amount=value.strip(), currency=currency)


def entry_sources(backend: "WorkspaceBackend", view: Any, entry: Mapping[str, Any],
                  by_name: Mapping[str, Mapping[str, Any]], depth: int = 0) -> list[m.Source]:
    """The cited records a computed entry rests on: its own citation, else its inputs'
    (each re-decided for this reader; one they may not open is left out)."""
    handles: list[str] = []
    own = citation_handle(entry.get("citation"))
    if isinstance(own, str):
        handles.append(own)
    elif depth < 3:
        for name in entry.get("inputs") or ():
            inner = by_name.get(name)
            if inner is not None:
                handles += [s.handle for s in entry_sources(backend, view, inner, by_name,
                                                            depth + 1)]
    handles = list(dict.fromkeys(handles))[:MAX_SOURCES_PER_FACT]
    decided = view.access.decide(handles) if handles else {}
    return [backend._source(view, h) for h in handles if decided.get(h) is None]


def landed_cost_of(backend: "WorkspaceBackend", view: Any,
                   ran: Mapping[str, Any]) -> m.LandedCost:
    """The landed-cost plan's own computed entries (``operating.landed_cost`` through
    ``plx.investigate``): ``landed_cost`` the total, ``landed_cost_line_<n>`` the lines,
    never a figure read out of prose. No computed total: not established."""
    entries = _entries(ran)
    by_name = {str(e.get("name")): e for e in entries}
    computed = {n: e for n, e in by_name.items() if e.get("kind") == "computed"}
    total_entry = computed.get("landed_cost")
    total = money_of(total_entry) if total_entry is not None else None
    lines = []
    for name in sorted((n for n in computed if re.fullmatch(r"landed_cost_line_\d+", n)),
                       key=lambda n: int(n.rsplit("_", 1)[1])):
        amount = money_of(computed[name])
        if amount is not None:
            lines.append(m.LandedCostLine(
                label=f"Line {name.rsplit('_', 1)[1]}", amount=amount,
                sources=entry_sources(backend, view, computed[name], by_name)))
    verdict = ran.get("verdict") or {}
    answerability = ((ran.get("investigation") or {}).get("answerability") or {}).get("state")
    status = ("computed" if total is not None and answerability == "answerable"
              else "partial" if total is not None or lines else "not_established")
    return m.LandedCost(status=status, total=total, lines=lines[:20],
                        missing=[str(f.get("missing"))[:300]
                                 for f in verdict.get("follow_ups") or ()
                                 if f.get("missing")][:10])


def people_of(backend: "WorkspaceBackend", view: Any, ran: Mapping[str, Any]
              ) -> Optional[list[m.Person]]:
    """The people plan's people (``operating.people_graph``: excluded people never reach
    the answer), each named by the label its cited entry holds and the roles the engine
    recorded; one whose citation the reader may not open is left out. None when the plan
    gave no person entries."""
    from operating.people_graph import ROLE_WEIGHTS

    people = []
    for entry in _entries(ran):
        value = entry.get("value")
        steps = entry.get("steps") or []
        roles_step = next((s for s in steps if isinstance(s, str)
                           and s.startswith("roles: ")), None)
        if entry.get("kind") != "computed" or not isinstance(value, Mapping) \
                or roles_step is None:
            continue
        handle = (value.get("origin") or {}).get("citation")
        if not isinstance(handle, str) or view.access.decide([handle]).get(handle) is not None:
            continue
        roles = [part.split(" ", 1)[0] for part in roles_step[len("roles: "):].split(", ")]
        roles = [r for r in roles if r in ROLE_WEIGHTS]
        name = plain(value)
        if name and roles:
            people.append(m.Person(name=name[:120], role=", ".join(roles),
                                   sources=[backend._source(view, handle)]))
    return people[:20] or None


def page_hidden(backend: "WorkspaceBackend", view: Any, oid: str) -> Optional[m.Hidden]:
    """the ``hidden_counts`` of the search chunks about this record the reader may
    not see; None (the section is omitted) without Postgres or chunks."""
    pool = backend.pg()
    if pool is None:
        return None
    from operating.pg.permissions import hidden_counts, reader_scope
    try:
        with pool.connection() as conn:
            if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM search.chunks)").fetchone()[0]:
                return None
            gated, policy = view.access.reader_policy()
            scope = reader_scope(conn, policy, gated)
            return hidden_from_counts(hidden_counts(conn, scope, object_ids=[oid]))
    except Exception:  # noqa: BLE001 - no count rather than a wrong one
        return None


def late_of(backend: "WorkspaceBackend", view: Any, ran: Mapping[str, Any]
            ) -> tuple[Optional[m.ObjectStatus], list[m.LateLine]]:
    """The late-PO plan's status (None when it established none) and its cited lines
    the reader may open."""
    late = ran.get("verdict")
    if not isinstance(late, Mapping) or late.get("state") in (None, "not_established"):
        return None, []
    cited = [(str(f.get("text") or "")[:300], f["citation"])
             for f in late.get("facts") or ()
             if isinstance(f, Mapping) and isinstance(f.get("citation"), str)
             and f.get("openable")]
    lines = [m.LateLine(line=text, sources=[backend._source(view, h)])
             for text, h in cited][:20]
    return m.ObjectStatus(state=str(late.get("state")), text=str(late.get("text"))[:600],
                          sources=[backend._source(view, h) for _, h in cited][:4]), lines


# What a missing stage is called, and words a message about it uses (a reminder about a
# "Billing Invoice" is about the vendor invoice). Matched on the trail's step titles.
STAGE_WORDS = (("vendor invoice", r"\binvoice|\bbilling\b"),
               ("vendor payment", r"\bpayment|\bremittance|\bpaid\b"),
               ("receipt", r"\breceipt|\breceived\b|\bpick ?up\b|\bdelivered\b"),
               ("supplier confirmation", r"\bconfirm"),
               ("vendor credit", r"\bcredit (?:memo|note)\b"))


def reconciled_not_found(items: Sequence[m.NotFound], trail: Optional[m.EvidenceTrail]
                         ) -> list[m.NotFound]:
    """Not-found items said against what the trail shows: a stage no system of record
    holds, while messages on the trail talk about it (a vendor invoice the reminders
    mention), reads "not on record; N messages below mention it (not confirmed)", never
    a bare "not found" beside those messages."""
    steps = [step for t in (trail.trails if trail is not None else ()) for step in t.steps] + \
        list(trail.steps if trail is not None else ())
    out = []
    for item in items:
        what = item.what.lower()
        rule = next((words for stage, words in STAGE_WORDS if what.startswith(stage)), None)
        mentions = [step for step in steps if rule and step.kind not in ("verdict", "due_date")
                    and re.search(rule, step.title, re.I)] if rule else []
        if mentions:
            owner = (item.where_it_would_live or "").split(". ", 1)[0].strip().rstrip(".")
            many = len(mentions) != 1
            said = (f"Not on record in the systems searched; {len(mentions)} "
                    f"message{'s' if many else ''} in Evidence mention{'' if many else 's'} "
                    "it (not confirmed as this order's)")
            item = item.model_copy(update={
                "where_it_would_live": f"{owner}. {said}" if owner.lower().startswith("ask")
                else said})
        out.append(item)
    return out


MAX_ORDER_COPIES = 6


def order_state(backend: Any, view: Any, oid: str) -> Optional[m.Fact]:
    """The customer order's one state across its records (``sales_order_status``): this
    record and its settled identity copies in every system (the Sage order a FileMaker
    order is entered as, a BC posted copy), each by its own status word or code, and a
    settled shipment or invoice linked to any of them; read as the reader, so a copy it may
    not open says nothing. The same definition the late-order rule uses, so the record's
    pages and the briefing never disagree. None when nothing could be read."""
    from operating import sales_order_status as so

    members = [oid]
    try:
        for link in so.twin_links():
            for direction in ("forward", "reverse"):
                got = view._call("plx.around", {
                    "from": oid, "tier_floor": "verified", "limit": MAX_ORDER_COPIES,
                    "path": [{"link_type": link, "direction": direction}]}) or {}
                members += [o["object_id"] for o in got.get("objects") or ()
                            if isinstance(o, Mapping) and isinstance(o.get("object_id"), str)
                            and o["object_id"] not in members]
        states, handles, closing = [], [], []
        for member in members[:MAX_ORDER_COPIES]:
            own: list[str] = []
            card = view._call("plx.object", {"object_id": member,
                                             "properties": list(so.READ_PROPS)}) or {}
            props: dict[str, list] = {}
            for value in card.get("values") or ():
                if isinstance(value, Mapping) and value.get("property") in so.READ_PROPS:
                    props.setdefault(value["property"], []).append(plain(value.get("value")))
                    handle = citation_handle(value.get("citation"))
                    if isinstance(handle, str):
                        own.append(handle)
            handles += own
            fulfilled = False
            for link in so.FULFILLED_LINKS:
                got = view._call("plx.around", {
                    "from": member, "tier_floor": "verified", "limit": 1,
                    "path": [{"link_type": link, "direction": "reverse"}]}) or {}
                fulfilled = fulfilled or bool(got.get("objects"))
            states.append(so.record_status(props, binding=member.split(":")[1]
                                           if member.count(":") >= 2 else None,
                                           fulfilled=fulfilled))
            if states[-1].cls == so.CLOSED:
                closing += own   # the record that closes the order is what is cited
    except Exception:  # noqa: BLE001 - a read that fails states nothing, the page stands
        return None
    if not handles:
        return None
    order = so.order_status(states)
    words = {so.CLOSED: "closed (shipped, invoiced, completed or cancelled in at least one of "
                        "its records)",
             so.OPEN: "open in every record that states a status",
             so.DRAFT: "a draft, not yet placed",
             so.UNKNOWN: "not decided: a record's status is missing or not one the rules "
                         "read"}[order.cls]
    copies = len(states)
    return m.Fact(text=f"Order state across its {copies} record{'' if copies == 1 else 's'}: "
                       f"{words}",
                  sources=[backend._source(view, h) for h in dict.fromkeys(
                      closing if order.cls == so.CLOSED and closing else handles)][:4],
                  check="read_from_record")


def page_import_days(backend: Any, oid: str, kind: str,
                     values: Sequence[Mapping[str, Any]]) -> set[str]:
    """The page's date properties (``active_population.PAGE_DATE_PROPS``) whose value is a
    bulk import day of this record's source (many of its records share that load day):
    marked on the page and never said as the record's business date."""
    from operating import active_population as active
    from operating.po_status import binding_of

    store = getattr(backend, "store_path", None)
    wanted = [v for v in values if str(v.get("property")) in active.PAGE_DATE_PROPS]
    if store is None or not wanted or kind not in ("PurchaseOrder", "SalesOrder"):
        return set()
    try:
        days = active.store_import_days(store, kind)
    except Exception:  # noqa: BLE001 - an unreadable store marks nothing, the page stands
        return set()
    binding = binding_of(oid) or ""
    return {str(v.get("property")) for v in wanted
            if active.day_of(plain(v.get("value"))) in days.get(
                (binding, str(v.get("property"))), frozenset())}


def sales_order_view(label: str, values: Sequence[Mapping[str, Any]],
                     related: Sequence[m.ObjectRef], facts: list[m.Fact],
                     currency: Optional[str]
                     ) -> tuple[str, list[m.Fact], list[m.NotFound]]:
    """What a customer order's page says about itself, from the same records the briefing
    cites: a plain summary (the customer, the order date, the total when it is a real
    figure) instead of the bare kind, with the customer named, and what the records do
    not hold said plainly (``not_found``). A header total of zero is not shown as a fact:
    an invoiced order's header often reads 0.00, and its invoiced figures are in the
    margin section (cost lines, profit, when this reader may read them)."""
    by_name = {str(v.get("property") or ""): plain(v.get("value")) for v in values}
    customer = next((r for r in related if r.type == "Customer"), None)
    name = customer.label if customer else (
        by_name.get("customer_name") or by_name.get("bill_to_name") or None)
    total_names = [n for n in by_name if re.fullmatch(r"(?:total|total_amount|subtotal)", n)]
    shown, zero = None, set()
    for n in total_names:
        try:
            amount = Decimal(str(by_name[n]).replace(",", ""))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if amount == 0:
            zero.add(presentation.field_label(n))
        elif shown is None:
            shown = _decimal_money(str(amount), currency)
    facts = [f for f in facts if not (f.text.split(":", 1)[0] in zero and re.fullmatch(
        r"[^:]+:\s*0+(?:\.0+)?(?:\s*[A-Z]{3})?", f.text))]
    ordered = by_name.get("order_date")
    parts = [f"Customer order {label}" + (f" for {name}" if name else "")]
    if isinstance(ordered, str) and re.match(r"\d{4}-\d{2}-\d{2}", ordered) \
            and not any("import date" in f.text for f in facts
                        if f.text.startswith(presentation.field_label("order_date"))):
        parts.append(f"ordered {ordered[:10]}")
    if shown is not None:
        parts.append(f"total {shown.amount} {shown.currency}")
    summary = "; ".join(parts) + "."
    absent = []
    if not name:
        absent.append(m.NotFound(
            what="The customer of this order",
            where_it_would_live="No customer is linked to this order in the records you can "
                                "open."))
    if shown is None:
        absent.append(m.NotFound(
            what="The order total",
            where_it_would_live="The order header holds no amount; see the margin section "
                                "for invoiced revenue, cost lines and profit when you may "
                                "read them."))
    return summary, facts, absent


# The records at a link's other end the page shows as the order's or customer's shipments
# and invoices (``LinkGroup.role``), by type: any link kind that reaches one of them, now or
# once a new link kind is declared, lands there with no page code of its own.
FULFILMENT_TYPES = frozenset({"Shipment", "ShipRelease", "DeliveryReceipt", "SalesInvoice",
                              "SalesCreditMemo", "Payment"})
# Records shown per link group, and evidence sources per record.
LINK_RECORDS = 8
LINK_EVIDENCE = 3
# Words for navigation names that do not read as words on their own.
_LINK_WORDS = {"same_record_as": "Same record in another system",
               "erp_order": "ERP order", "fm_orders": "FileMaker orders",
               "bc_invoices": "BC invoices"}


@functools.lru_cache(maxsize=1)
def _schema_links() -> dict[str, tuple[str, str, str, str]]:
    """link type -> (from type, name, to type, reverse name) as the schema declares it;
    empty when the schema cannot be read (each group then reads its link type's words)."""
    try:
        from ontology.schema import load_schema
        links = load_schema().link_types
    except Exception:   # a schema that cannot be read names no link; never fails a page
        return {}
    return {str(k): (str(v.get("from") or ""), str(v.get("name") or ""),
                     str(v.get("to") or ""), str(v.get("reverse_name") or ""))
            for k, v in links.items() if isinstance(v, Mapping)}


def link_words(name: str) -> str:
    """'sales_invoices' -> 'Sales invoices'; a known name its own words."""
    if name in _LINK_WORDS:
        return _LINK_WORDS[name]
    text = re.sub(r"\s+", " ", re.sub(r"[_.]+", " ", name)).strip()
    return (text[:1].upper() + text[1:]) if text else "Linked"


def link_groups(backend: "WorkspaceBackend", view: Any, oid: str, around: Mapping[str, Any],
                *, own: Iterable[str] = ()) -> list[m.LinkGroup]:
    """The records one hop from ``oid``, one group per link kind the workspace walked (one
    ``plx.around`` hop per link type the record's card lists, read as this reader), named
    by the schema's navigation name. A hop is read with its leads (``with_leads``): the
    records reached only through lead or disputed links (``possibly_related``) follow the
    solid ones, each marked ``possible`` and never in the group's ``count``. Each record
    carries the link's tier and its evidence: the cited rows, mail and documents the link
    was read from that this reader may open, mail and documents first and the page's own
    rows (``own``) left out; a citation the reader may not open is never shown. Generic by
    link kind: a link kind declared later shows with no code of its own."""
    from operating.ontology_tools_traverse import with_leads

    schema = _schema_links()
    own = set(own)
    found: list[tuple[Mapping[str, Any], Mapping[str, Any], list[tuple[Mapping[str, Any], bool, list[str]]], int]] = []
    wanted: list[str] = []
    for hop in around.get("hops") or ():
        result = hop.get("result") if isinstance(hop, Mapping) else None
        if not isinstance(result, Mapping) or result.get("error"):
            continue
        solid_ids = {o.get("object_id") for o in result.get("objects") or ()
                     if isinstance(o, Mapping)}
        apart = result.get("possibly_related")
        leads = apart.get("count") if isinstance(apart, Mapping) else None
        read = with_leads(result)
        seen: set[str] = set()
        objects = []
        for o in read.get("objects") or ():
            if isinstance(o, Mapping) and isinstance(o.get("object_id"), str) \
                    and o["object_id"] != oid and o["object_id"] not in seen:
                seen.add(o["object_id"])
                objects.append(o)
        if not objects:
            continue
        by_other: dict[str, list[str]] = {}
        for edge in read.get("edges") or ():
            if not isinstance(edge, Mapping):
                continue
            other = edge.get("to") if edge.get("from") == oid else edge.get("from")
            for reason in edge.get("reason") or ():
                for c in (reason.get("citations") or ()) if isinstance(reason, Mapping) else ():
                    h = c.get("l1_handle") if isinstance(c, Mapping) else None
                    if isinstance(other, str) and isinstance(h, str) and h not in own:
                        by_other.setdefault(other, []).append(h)
        # the solid records first, then the possible ones, as many as the group shows
        solid = [o for o in objects if o["object_id"] in solid_ids]
        possible = [o for o in objects if o["object_id"] not in solid_ids]
        shown = []
        for o in (solid + possible)[:LINK_RECORDS]:
            handles = list(dict.fromkeys(by_other.get(o["object_id"], ())))
            # mail and documents first: what a person wrote is the evidence a reader wants
            handles.sort(key=lambda h: system_of(h) not in ("Email", "Teams", "File"))
            shown.append((o, o["object_id"] not in solid_ids, handles))
            wanted.extend(handles)
        n_leads = leads if isinstance(leads, int) and not isinstance(leads, bool) \
            and leads >= len(possible) else len(possible)
        found.append((hop, read, shown, n_leads))
    wanted = list(dict.fromkeys(wanted))
    decided = view.access.decide(wanted) if wanted else {}
    out: list[m.LinkGroup] = []
    for hop, read, shown, n_leads in found:
        tiers_of = {}
        for edge in read.get("edges") or ():
            if isinstance(edge, Mapping):
                for end in ("from", "to"):
                    if isinstance(edge.get(end), str) and edge.get(end) != oid:
                        tiers_of[edge[end]] = edge.get("tier")
        link_type = str(hop.get("link_type") or "")
        direction = "reverse" if hop.get("direction") == "reverse" else "forward"
        declared = schema.get(link_type)
        if declared:
            name = declared[1] if direction == "forward" else declared[3]
        else:
            name = link_type.split(".", 1)[-1]
        other_type = str(shown[0][0].get("object_type") or "") or (
            (declared[2] if direction == "forward" else declared[0]) if declared else "Object")
        records = []
        for o, is_lead, handles in shown:
            kind_of = str(o.get("object_type") or other_type or "Object")
            evidence = []
            for h in handles:
                if decided.get(h) is not None:
                    continue   # a citation this reader may not open is never shown
                evidence.append(backend._source(view, h, openable=view._can_open(h)))
                if len(evidence) >= LINK_EVIDENCE:
                    break
            tier = tiers_of.get(o["object_id"])
            records.append(m.LinkRef(
                object_id=o["object_id"], type=kind_of,
                label=presentation.record_label(plain(o.get("display")), kind_of,
                                                o["object_id"])[:200],
                open_url=object_url(kind_of, o["object_id"]),
                tier=tier if isinstance(tier, str) else ("lead" if is_lead else None),
                possible=is_lead, evidence=evidence))
        solid_shown = sum(1 for r in records if not r.possible)
        listed = hop.get("listed_count")
        total = (read.get("completeness") or {}).get("total") \
            if isinstance(read.get("completeness"), Mapping) else None
        count = next((n for n in (total, listed) if isinstance(n, int)
                      and not isinstance(n, bool) and n >= solid_shown), None)
        if count is None and solid_shown == 0:
            count = 0
        role = ("same_record" if name == "same_record_as" or link_type.endswith(
                    ".same_record_as")
                else "customer" if other_type == "Customer" and direction == "forward"
                else "fulfilment" if other_type in FULFILMENT_TYPES else "related")
        out.append(m.LinkGroup(link_type=link_type[:120], direction=direction, name=name[:80],
                               words=link_words(name)[:80], type=other_type[:80], role=role,
                               count=count, possible=n_leads, records=records))
    return out


def walk_whole(page: Mapping[str, Any]) -> bool:
    """Whether the record's link walk read everything there is for this reader to read:
    no link type left unwalked (the hop cap), no hop that errored or came back cut, and
    nothing on the card or a hop withheld by permission. Only a whole walk may say a link
    reaches nothing."""
    from operating.ontology_tools_traverse import PERMISSION_LOSSES

    around = page.get("around")
    if not isinstance(around, Mapping) or around.get("truncated"):
        return False
    card = page.get("card") if isinstance(page.get("card"), Mapping) else {}
    gaps = list(card.get("gaps") or ())
    for hop in around.get("hops") or ():
        result = hop.get("result") if isinstance(hop, Mapping) else None
        if not isinstance(result, Mapping) or result.get("error"):
            return False
        done = result.get("completeness")
        if isinstance(done, Mapping) and (done.get("truncated") or done.get("complete") is False):
            return False
        gaps.extend(result.get("gaps") or ())
    return not any(not isinstance(g, Mapping) or g.get("reason") in PERMISSION_LOSSES
                   for g in gaps)


def fulfilment_absence(page: Mapping[str, Any], links: Sequence[m.LinkGroup]
                       ) -> Optional[m.NotFound]:
    """What a customer order's page says when it shows no confirmed shipment or invoice:
    that none is linked only when the walk was whole (``walk_whole``) and no possible one
    was reached; possibly related ones when only lead links reach them; else that none was
    found in what the reader can see, never that there is none."""
    groups = [g for g in links if g.role == "fulfilment"]
    if any(not r.possible for g in groups for r in g.records):
        return None
    what = "Shipments and invoices of this order"
    if groups:
        n = sum(g.possible or len(g.records) for g in groups)
        return m.NotFound(what=what, where_it_would_live=(
            f"None confirmed; {n} possibly related (suggested links, not confirmed) "
            "shown under Shipments and invoices."))
    if walk_whole(page):
        return m.NotFound(what=what, where_it_would_live=(
            "No shipment or invoice is linked to this order in the records you can open."))
    return m.NotFound(what=what, where_it_would_live=(
        "None found in what you can see: part of this order's links could not be read for "
        "you, so this is not absence."))


# A receipt event of an order (its goods received or posted).
RECEIPT_EVENTS = frozenset({"Receipt.posted", "Receipt.received", "Receipt.fm_received",
                            "Receipt.sage_received"})


def record_fields(row: Mapping[str, Any], limit: int = 60) -> dict[str, str]:
    """A record's fields as the source page shows them. A nested object's fields are
    shown under their dotted path (``custom_fields.fmPoNumber``), never dropped: a Portal
    purchase order carries the FileMaker PO number it was migrated from there, and
    without it the page of the record an answer about that number cites shows only the
    Portal's own number, as if the citation opened another order. Lists stay out."""
    out: dict[str, str] = {}

    def add(prefix: str, value: Any, depth: int) -> None:
        if len(out) >= limit:
            return
        if isinstance(value, Mapping) and "untrusted_text" not in value:
            if depth >= 2:
                return
            for key, item in value.items():
                add(f"{prefix}.{key}", item, depth + 1)
        elif not isinstance(value, list):
            out[prefix[:80]] = plain(value) or ""
    for key, value in row.items():
        add(str(key), value, 0)
    return out


def source_view(view: Any, handle: str, text_offset: int) -> m.SourceView:
    got = view.source(handle, text_offset=text_offset)   # KeyError: not this reader's
    return source_payload(handle, got, text_offset)


def source_payload(handle: str, got: Mapping[str, Any], text_offset: int) -> m.SourceView:
    """A read source (``view.source`` or the reader's host's ``plx.read``) as the
    contract's source view."""
    text = None
    for key in ("text", "body", "content"):
        if got.get(key) is not None:
            text = plain(got[key]) if not isinstance(got[key], str) else got[key]
            break
    row = got.get("row") or got.get("fields") or got.get("values")
    fields = record_fields(row) if isinstance(row, Mapping) else None
    page = got.get("page") or {}
    following = page.get("next_text_offset") if isinstance(page, Mapping) else \
        got.get("next_text_offset")
    title = plain(got.get("title")) if got.get("title") else None
    if title is None:
        # a message or file titled by its own subject or name (an email's subject line)
        for key in ("subject", "row.subject", "title", "row.title", "name", "row.name"):
            for source in (got, fields or {}):
                value = source.get(key) if isinstance(source, Mapping) else None
                if title is None and value not in (None, "") and not isinstance(value, list):
                    title = (plain(value) or "")[:300] or None
    if handle.startswith("original://") and isinstance(text, str):
        # A captured original's page reads as its text, titled by its file name; the
        # receipt's fields (hash, byte count) are no reading matter.
        name = (fields or {}).get("row.filename") or (fields or {}).get("filename")
        number = page if isinstance(page, int) and not isinstance(page, bool) else None
        title = title or (f"{name}, page {number}" if name and number else name)
        fields = None
    return m.SourceView(handle=handle, system=system_of(handle), label=record_words(handle),
                        kind="text" if text is not None and not fields else "record",
                        title=title,
                        text=text[:200_000] if isinstance(text, str) else None,
                        fields=fields or None,
                        as_of=got.get("captured_at") if isinstance(got.get("captured_at"),
                                                                  str) else None,
                        text_offset=text_offset,
                        next_offset=following if isinstance(following, int) else None,
                        changed_since=got.get("status") == "changed_since")
