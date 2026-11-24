"""``plx.page``: the named SQL reads a page's numbers come from, run through the one door
``plx.sql`` uses (``operating.ontology_tools_sql.reader_door`` and ``scoped_read``): one
READ ONLY transaction as the NOLOGIN role ``plx_page_reader`` over the person's reader-scoped
views ``<biz schema>_page.<view>``, the reader's scope set as transaction-local settings, the
role and scope re-checked after the read. Rows and cells are scoped inside Postgres (audience
keys, marking sets, hidden documents); this module decides nothing about permissions. Unlike
``plx.sql``'s views these keep Freightcom rows (``ai_ok`` false): the owner's no-AI rule binds
the model's path only, and a person may see them. That is why this is never a model tool.

A page asks for reads by name (``QUERIES``), each with its bound parameters; free SQL is
never accepted here (the model's free SQL goes through ``plx.sql``'s checker). Every read of
one call runs in the same transaction, so a page's numbers agree with each other.

Its own connections: a small pool per store (``PAGE_POOL`` connections, read only), never
the store connection ``plx.sql`` and the rest of the host share under one lock, so a page
never waits behind an Ask's query.

``ids``: the record a returned row's ``handle`` opens (object id and type), read after the
scoped read for those handles only (the row was already shown to this reader), so a page
can link to the record; the guarded views keep their object ids internal.

Not a model tool: it is registered on the host table (``ontology_host.tool_table``) for the
API's page endpoints only, and no agent catalog lists it."""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from operating.measures import where_sql
from operating.po_pilot import bc_pilot_po_sql, bc_pilot_record_sql

TOOL_NAME = "plx.page"
FILES_STATE = "plx.reader_files_state"   # "" | gate_error | index_error: the files kind failed, said
FILES_OK = "plx.reader_files_ok"   # the linked files this reader's item gate allows (JSON array)
MAX_READS = 12
MAX_ROWS = 500
MAX_OFFSET = 20000
LOCK_MS = 100          # the longest a page read waits for a table lock (then: not ready)
LIVE_FALLBACK = frozenset({"record_page", "plan_section"})   # reads whose page has a live path
PAGE_SECONDS = 5.0      # a page read's statement limit (a page answers in < 2 s)
PAGE_POOL = 4           # connections per store (PLX_PAGE_POOL)
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
POOL_WAIT = 2.0         # seconds a page waits for a free connection, then "busy"


@dataclass(frozen=True)
class Query:
    sql: str                      # over the guarded views (search_path is the door's schema)
    params: tuple[str, ...] = ()  # the bound parameters it takes, all required
    rows: int = 1                 # at most this many rows returned
    with_day: bool = False        # prefixed with the ``d`` CTE of ``as_of`` (``_DAY``)
    workers: int | None = None    # at most this many parallel workers (None: the server's)

    def text(self) -> str:
        return (_DAY + self.sql) if self.with_day else self.sql


# a customer-side row of the account: the customer's own (``handle``) or any store of the
# chain (``chain``, the table's own roll-up column); '' matches nothing
_CUSTOMER = ("(customer_handle = NULLIF(%(handle)s, '') "
             "OR customer_chain = NULLIF(%(chain)s, ''))")
# ``as_of`` (an ISO day, the business day the page is read for) and the year windows
_DAY = ("WITH d AS (SELECT a.day, date_trunc('year', a.day)::date AS start, "
        "(date_trunc('year', a.day) - interval '1 year')::date AS last_start, "
        "(a.day - interval '1 year')::date AS last_day FROM (SELECT %(as_of)s::date AS day) a) ")
# this year to date, the same days a year before, and the whole of last year
_YEARS = ("sum({amount}) FILTER (WHERE {day} BETWEEN d.start AND d.day) AS this_year, "
          "count(*) FILTER (WHERE {day} BETWEEN d.start AND d.day AND {amount} IS NULL) "
          "AS this_year_hidden, "
          "sum({amount}) FILTER (WHERE {day} BETWEEN d.last_start AND d.last_day) "
          "AS last_year_to_date, "
          "count(*) FILTER (WHERE {day} BETWEEN d.last_start AND d.last_day AND {amount} "
          "IS NULL) AS last_year_to_date_hidden, "
          "sum({amount}) FILTER (WHERE {day} >= d.last_start AND {day} < d.start) "
          "AS last_year, "
          "count(*) FILTER (WHERE {day} >= d.last_start AND {day} < d.start AND {amount} "
          "IS NULL) AS last_year_hidden")


# a party's name as a key: case, punctuation and a trailing note (a parenthesis, "- EFT")
# set aside: "EZ CHEMICALS (EFT)" and "EZ Chemicals" are one key
_NAME_KEY = ("regexp_replace(lower(regexp_replace(regexp_replace({col}, '\\(.*$', ''), "
             "'\\s*-\\s*eft\\y.*$', '', 'i')), '[^a-z0-9]+', '', 'g')")

# an alert rule read only while its last finished run is recent (``alerts.STALE_MINUTES``)
def _fresh() -> str:
    from operating.pg.alerts import STALE_MINUTES
    return (f"rule IN (SELECT r.rule FROM alert_runs r WHERE r.ok_at > now() - "
            f"interval '{int(STALE_MINUTES)} minutes')")


_FRESH = _fresh()


def _impact_top() -> "Query":
    """The open alerts of the rules that state a money impact (``Rule.impact``), the biggest
    first: per alert the sums over its own invoice lines (the topic is
    ``item|unit|currency`` of one customer) in the last window and the one before, the
    rule's own impact expression over them. A rule's window and cost-sanity rule are the
    rule's own (``alerts.PRICE_DAYS``, ``alerts.COST_LOOKS_WRONG_SQL``)."""
    from operating.pg.alerts import COST_LOOKS_WRONG_SQL, PRICE_DAYS, RULES
    cases = " ".join(f"WHEN '{name}' THEN {rule.impact}" for name, rule in RULES.items()
                     if rule.impact)
    days = int(PRICE_DAYS)
    return Query(
        "WITH a AS (SELECT rule, kind, record_table, handle, topic, why, since, first_seen "
        "FROM alerts WHERE state = 'open' AND rule = ANY(string_to_array(%(rules)s, ',')) "
        "AND " + _FRESH + "), "
        "l AS (SELECT a.rule, a.handle, a.topic, l.quantity, l.revenue, l.margin, "
        "l.margin_basis, l.revenue_currency AS cur, l.invoice_date > current_date - "
        f"{days} AS recent, {COST_LOOKS_WRONG_SQL} AS wrong "
        "FROM a JOIN sales_invoice_lines l ON l.customer_handle = a.handle "
        "AND l.item_code || '|' || l.quantity_unit || '|' || l.revenue_currency = a.topic "
        "WHERE l.pilot IS NOT TRUE AND l.quantity > 0 AND l.revenue > 0 AND "
        f"l.invoice_date > current_date - {2 * days} AND l.invoice_date <= current_date), "
        "s AS (SELECT rule, handle, topic, max(cur) AS cur, "
        "sum(-margin) FILTER (WHERE recent AND NOT wrong AND margin_basis IN "
        "('same_currency', 'cost_converted_at_invoice_rate')) AS below, "
        "sum(quantity) FILTER (WHERE recent) AS q, sum(revenue) FILTER (WHERE recent) AS rev, "
        "sum(revenue) FILTER (WHERE NOT recent) / nullif(sum(quantity) FILTER "
        "(WHERE NOT recent), 0) AS prior_price FROM l GROUP BY rule, handle, topic), "
        "t AS (SELECT a.*, s.cur, CASE s.rule " + cases + " END AS impact FROM a "
        "LEFT JOIN s ON s.rule = a.rule AND s.handle = a.handle AND s.topic = a.topic), "
        "n AS (SELECT t.*, row_number() OVER (PARTITION BY rule ORDER BY impact DESC NULLS "
        "LAST, since, handle, topic) AS place FROM t) "
        "SELECT rule, kind, record_table, handle, topic, why, since, first_seen, "
        "(SELECT r.label FROM records r WHERE r.handle = n.handle ORDER BY r.record_id "
        "LIMIT 1) AS subject, cur, impact FROM n WHERE place <= %(limit)s "
        "ORDER BY rule, place", ("rules", "limit"), MAX_ROWS)


# The typed text, lower-cased, as an expression of the statement's own parameter (never a
# column of a one-row CTE joined to every branch: a CTE read by every branch is materialized,
# and a CTE scan keeps the whole statement in one process; with the parameter each branch is
# its own parallel scan. Live 11-22, the owner's door: 345 ms -> 150-200 ms a search.)
Q = "lower(btrim(%(q)s))"


def _rank(exact: str, prefix: str, name: str | None = None) -> str:
    """0 a number or code equal to the text, 1 a name equal to it, 2 a number or code
    starting with it, 3 a name starting with it, 4 a word of the name starting with it, 5
    a name holding it, 9 no match. ``exact``/``prefix``: SQL tests on ``Q``."""
    out = f"CASE WHEN {exact} THEN 0 "
    if name:
        out += f"WHEN lower({name}) = {Q} THEN 1 "
    out += f"WHEN {prefix} THEN 2 "
    if name:
        out += (f"WHEN strpos(lower({name}), {Q}) = 1 THEN 3 "
                f"WHEN strpos(' ' || lower({name}), ' ' || {Q}) > 0 THEN 4 "
                f"WHEN strpos(lower({name}), {Q}) > 0 THEN 5 ")
    return out + "ELSE 9 END"


def _cols(alias: str, view: str) -> list[str]:
    return [f"{alias}.{c}" for c in NUMBER_LEGS[view]]


def _may(prefix: str, name: str) -> str:
    """What every row ``_rank`` ranks below 9 passes (its exact test implies its prefix one;
    each name test implies the name holding the text), tested before the rank: a row
    failing it reads the name lower-cased once, never four times. Same rows."""
    return f"({prefix} OR strpos(lower({name}), {Q}) > 0)"


def _num(col: str) -> tuple[str, str]:
    return f"lower({col}) = {Q}", f"strpos(lower({col}), {Q}) = 1"


# A number leg's rows narrowed first by ranges the guarded views let reach a column's
# ``text_pattern_ops`` index (``~>=~``/``~<~`` are leakproof; ``lower()`` is not, so
# ``lower(col) = q`` alone reads every row). A row the leg can show has ``lower(col)``
# equal to or starting with the typed text ``Q``, so ``col`` starts with a spelling of Q's
# head whose every character lower-cases to Q's: an ASCII letter is itself or its upper
# case (and 'i', 'k' also U+0130, U+212A, the only other characters whose ``lower()`` is
# ASCII under the store's C.UTF-8 ctype, checked over every code point), any other ASCII
# character only itself. The head is Q's first two characters in each case spelling (at
# most 3 x 3 = 9 ranges, usually 4: 'po', 'pO', 'Po', 'PO') followed by the run of
# digits and separators after them; a first character outside ASCII, or an empty text,
# narrows nothing (the leg reads every row, as before). The tests that rank a row are
# unchanged, so the rows are the same; a table without the index reads as before.
_C1, _C2 = f"substr({Q}, 1, 1)", f"substr({Q}, 2, 1)"
_ASCII = "ascii({}) BETWEEN 1 AND 127"


def _spellings(c: str) -> tuple[tuple[str, str], ...]:
    """(spelling, when it is one of its own) of a character of ``Q``: itself; its upper
    case when that differs; U+0130 / U+212A for 'i' / 'k'."""
    return ((c, "true"), (f"upper({c})", f"upper({c}) <> {c}"),
            (f"(CASE {c} WHEN 'i' THEN chr(304) ELSE chr(8490) END)", f"{c} IN ('i', 'k')"))


def _heads() -> str:
    """One row (``k``) of the narrowing's inputs: ``k.on_``, the heads ``k.h0``..``h8``
    and whether each is a spelling of its own (``k.g0``..``g8``; a repeat is left out).
    Joined as a FROM-less subquery, it is flattened into the leg and its expressions,
    of the statement's parameter only, fold to constants for the plan: an arm whose
    ``g`` is false drops out of it."""
    two = _ASCII.format(_C2)
    rest = (f"(CASE WHEN {two} THEN coalesce(substring(substr({Q}, 3) "
            "from '^[0-9 ./_#-]*'), '') ELSE '' END)")
    cols = [f"{_ASCII.format(_C1)} AS on_"]
    n = 0
    for a, ga in _spellings(_C1):
        for b, gb in _spellings(_C2):
            cols.append(f"(CASE WHEN {_ASCII.format(_C1)} THEN {a} || (CASE WHEN {two} THEN "
                        f"{b} ELSE '' END) || {rest} ELSE '' END) AS h{n}")
            cols.append(f"({ga} AND {'true' if gb == 'true' else f'{two} AND {gb}'}) AS g{n}")
            n += 1
    return f"(SELECT {', '.join(cols)}) k"


_HEADS = _heads()


def _narrow(*cols: str) -> str:
    """True for a row whose ``cols`` (any) start with one of ``k``'s heads (``k.on_``
    false: every row). The upper bound is the head with its last character's successor:
    exactly the strings starting with the head, bytewise (UTF-8 keeps code point order)."""
    arms = [f"(k.g{n} AND {c} ~>=~ k.h{n} AND {c} ~<~ (left(k.h{n}, -1) || "
            f"chr(ascii(right(k.h{n}, 1)) + 1)))" for c in cols for n in range(9)]
    return f"(NOT k.on_ OR {' OR '.join(arms)})"


def _codes(code: str, codes: str) -> tuple[str, str]:
    """A party's own code or any system's code in ``codes`` ('sage:A1, bc:A1')."""
    return (f"(lower({code}) = {Q} OR strpos(lower({codes}) || ',', ':' || {Q} || ',') > 0)",
            f"(strpos(lower({code}), {Q}) = 1 OR strpos(lower({codes}), ':' || {Q}) > 0)")


# Record kinds found by their own number through the guarded ``records`` view (a document's
# label is its number, with or without the kind in front: "Receipt RCP10002648", a lot's
# "S220001"). Sales and supplier invoices are read from their own views, whose number
# columns are typed; every kind here is one record page (``object_url``).
NUMBERED_RECORDS = {"shipment": "Shipment", "receipt": "Receipt", "lot": "Lot",
                    "credit_note": "SalesCreditMemo", "purchase_invoice": "PurchaseInvoice"}
# Search kinds whose results open the record page the Ask opens (the record's object), not
# an account or order page.
OBJECT_KINDS = ("product", "batch", "sales_invoice", "supplier_invoice", *NUMBERED_RECORDS)


def _label_number(word: str) -> tuple[str, str]:
    """A record's label read as its number, by the operators the guarded view lets reach
    an index (``texteq``, ``text_pattern_ge``/``lt``: leakproof; ``lower()`` is not): the
    label is the number or its kind word and the number, spelt as typed, upper or lower
    case. Exact is equality with one of those, a prefix a range over them."""
    # the typed text as the statement's own parameters (constants to the planner), never a
    # column of ``k``: only then does the guarded view let the test reach the index
    forms = ["btrim(%(q)s)", "upper(btrim(%(q)s))", "lower(btrim(%(q)s))"]
    forms += [f"('{word} ' || {f})" for f in forms]
    exact = f"r.label = ANY(ARRAY[{', '.join(forms)}])"
    ranges = " OR ".join(f"(r.label ~>=~ {f} AND r.label ~<~ ({f} || chr(1114111)))"
                         for f in forms)
    return exact, f"({ranges})"


# The legs found by number only, and the number columns each is narrowed by (``_narrow``).
NUMBER_LEGS = {"sales_orders": ("order_number", "customer_po"), "purchase_orders": ("po_number",),
               "batches": ("batch_code", "lot_number"),
               "sales_invoices": ("invoice_number", "customer_po"),
               "supplier_invoices": ("document_number",)}


def _find_sql() -> str:
    """One ranked list over every kind of record: parties and products by name or any
    system's code, orders, POs, invoices, supplier invoices and batches by number (never
    by their party's name, so a name finds the party once), and shipments, receipts, lots,
    credit notes by the number their label carries (guarded ``records`` view). A chain (two
    or more accounts under one chain) is one result. Exact numbers rank first across kinds;
    a BC pilot row ranks after a system-of-record row of the same rank (``po_pilot``'s one
    rule). No ``LIKE``: the text is never a pattern."""
    cx, cp = _codes("c.customer_code", "c.codes")
    sx, sp = _codes("s.supplier_code", "s.codes")
    ox, op_ = _num("o.order_number")
    cpx, cpp = _num("o.customer_po")
    px, pp = _num("p.po_number")
    ix, ip = _num("i.item_code")
    bx, bp = _num("b.batch_code")
    lx, lp = _num("b.lot_number")
    vx, vp = _num("v.invoice_number")
    vcx, vcp = _num("v.customer_po")
    dx, dp = _num("d.document_number")
    no = "false"
    parts = [
        ("customer", "c.handle", "c.name", "c.customer_code", "c.chain", "NULL::text", "NULL::bigint",
         _rank(cx, cp, "c.name"), no, f"customers c WHERE {_may(cp, 'c.name')}"),
        ("chain", "NULL::text", "g.chain", "g.chain_code", "NULL::text", "NULL::text", "g.n",
         _rank(f"lower(g.chain_code) = {Q}", f"strpos(lower(g.chain_code), {Q}) = 1", "g.chain"),
         no, "(SELECT chain, chain_code, count(*) AS n FROM customers WHERE chain IS NOT NULL "
         "GROUP BY 1, 2 HAVING count(*) > 1) g WHERE "
         f"{_may(f'strpos(lower(g.chain_code), {Q}) = 1', 'g.chain')}"),
        ("supplier", "s.handle", "s.name", "s.supplier_code", "NULL::text", "s.status", "NULL::bigint",
         _rank(sx, sp, "s.name"), no, f"suppliers s WHERE {_may(sp, 's.name')}"),
        ("sales_order", "o.handle", "o.order_number", "o.customer_po", "o.customer", "o.status",
         "NULL::bigint", f"LEAST({_rank(ox, op_)}, CASE WHEN {cpx} THEN 2 ELSE 9 END)",
         no, f"sales_orders o, {_HEADS} WHERE {_narrow(*_cols('o', 'sales_orders'))}"),
        ("purchase_order", "p.handle", "p.po_number", "p.vendor_number", "p.supplier", "p.status",
         "NULL::bigint", _rank(px, pp), bc_pilot_po_sql("p"),
         f"purchase_orders p, {_HEADS} WHERE {_narrow(*_cols('p', 'purchase_orders'))}"),
        ("product", "i.handle", "i.name", "i.item_code", "i.category", "i.active", "NULL::bigint",
         _rank(ix, ip, "i.name"), no, f"products i WHERE {_may(ip, 'i.name')}"),
        ("batch", "b.handle", "b.batch_code", "b.lot_number", "b.product", "b.status", "NULL::bigint",
         f"LEAST({_rank(bx, bp)}, {_rank(lx, lp)})", no, f"batches b, {_HEADS} WHERE {_narrow(*_cols('b', 'batches'))}"),
        ("sales_invoice", "v.handle", "v.invoice_number", "v.customer_po", "v.customer",
         "v.status", "NULL::bigint",
         f"LEAST({_rank(vx, vp)}, CASE WHEN {vcx} THEN 2 ELSE 9 END)",
         "coalesce(v.pilot, false)",
         f"sales_invoices v, {_HEADS} WHERE {_narrow(*_cols('v', 'sales_invoices'))}"),
        ("supplier_invoice", "d.handle", "d.document_number", "d.po_number", "d.supplier",
         "d.document_type", "NULL::bigint", _rank(dx, dp), "coalesce(d.pilot, false)",
         f"supplier_invoices d, {_HEADS} WHERE {_narrow(*_cols('d', 'supplier_invoices'))}"),
    ]
    for kind, type_ in NUMBERED_RECORDS.items():
        rx, rp = _label_number(type_)
        parts.append((kind, "r.handle", "r.label", "r.system", "r.type", "NULL::text",
                      "NULL::bigint", _rank(rx, rp), bc_pilot_record_sql("r.system"),
                      f"(SELECT * FROM records WHERE type = '{type_}' AND ({rx.replace('r.', '')} OR "
                      f"{rp.replace('r.', '')})) r"))
    union = " UNION ALL ".join(
        f"SELECT * FROM (SELECT '{kind}' AS kind, {h} AS handle, {t} AS title, {c} AS code, "
        f"{ctx} AS context, {st} AS status, {n} AS members, {r} AS rank, {pilot} AS pilot "
        f"FROM {frm}) x WHERE rank < 9"
        for kind, h, t, c, ctx, st, n, r, pilot, frm in parts)
    # under two characters: no result (``search`` asks for none; the statement holds it too)
    return (f"SELECT kind, handle, title, code, context, status, members, rank FROM ({union}) "
            "hit WHERE length(btrim(%(q)s)) >= 2 ORDER BY rank, pilot, CASE kind WHEN 'chain' THEN 0 WHEN 'customer' THEN 1 "
            "WHEN 'supplier' THEN 2 ELSE 3 END, length(title), title, handle "
            "LIMIT %(limit)s")


FIND_SQL = _find_sql()

# A name spelt close to what was typed ("Croda" typed "Crodda"), asked only when ``find`` found
# nothing: parties and products whose name is close by pg_trgm (``public.``: the page role's
# search path holds no extension schema). A short word needs the whole name close (a four-letter
# typo is a word of many other names); a longer one may be a close word inside the name. Rank 6
# so the result reads as a close spelling, never an exact find.
_WHOLE = "public.similarity(lower({n}), lower(btrim(%(q)s)))"
_WORD = "public.word_similarity(lower(btrim(%(q)s)), lower({n}))"
FIND_NEAR_SQL = (
    "SELECT kind, handle, title, code, context, status, members, rank FROM ("
    "SELECT *, row_number() OVER (PARTITION BY kind, lower(title) ORDER BY title, code, handle) "
    "AS same FROM (SELECT kind, handle, title, code, context, status, members, 6 AS rank, "
    "whole, word FROM ("
    + " UNION ALL ".join(
        f"SELECT '{kind}' AS kind, {h} AS handle, {n} AS title, {c} AS code, {ctx} AS context, "
        f"NULL::text AS status, NULL::bigint AS members, {_WHOLE.format(n=n)} AS whole, "
        f"{_WORD.format(n=n)} AS word FROM {frm}"
        for kind, h, n, c, ctx, frm in (
            ("customer", "handle", "name", "customer_code", "chain", "customers"),
            ("supplier", "handle", "name", "supplier_code", "NULL::text", "suppliers"),
            ("product", "handle", "name", "item_code", "category", "products")))
    + ") near WHERE length(btrim(%(q)s)) >= 4 AND (whole >= 0.3 OR (length(btrim(%(q)s)) >= 5 "
    "AND word >= 0.6))) hit) numbered WHERE same <= 2 "   # a name many records share: two of them
    "ORDER BY greatest(whole, word) DESC, length(title), title, handle LIMIT %(limit)s")


# Each read is one statement over the guarded views; parameters bind as %(name)s.
QUERIES: dict[str, Query] = {
    # Home / My work / Watching: the late purchase orders (the PO view's own verdict)
    "late_po_count": Query(
        "SELECT count(DISTINCT handle) AS late FROM purchase_orders WHERE "
        + where_sql("late_purchase_orders")),
    "late_po_list": Query(
        "SELECT po_number, supplier, days_late, promised_date, requested_date, total, "
        "total_currency, purchaser, line_count, handle, withheld FROM purchase_orders "
        "WHERE " + where_sql("late_purchase_orders") + " "
        "ORDER BY days_late DESC NULLS LAST, po_number, handle "
        "LIMIT %(limit)s OFFSET %(offset)s", ("limit", "offset"), MAX_ROWS),
    # the open purchase orders a reader may see, late first then newest (the warm-up reads their
    # pages ahead: a first open of an order nobody opened since a refresh was 12-24 s cold)
    "open_po_list": Query(
        "SELECT po_number, handle, withheld FROM purchase_orders WHERE status = 'open' "
        "ORDER BY (verdict = 'late') DESC NULLS LAST, order_date DESC NULLS LAST, po_number, "
        "handle LIMIT %(limit)s", ("limit",), MAX_ROWS),
    # the sidebar's late POs by supplier: each supplier's count, most first, and how many
    # suppliers there are (a hidden supplier cell is no supplier: never named or counted)
    "late_po_suppliers": Query(
        "SELECT supplier, late, count(*) OVER () AS suppliers FROM (SELECT supplier, "
        "count(*) AS late FROM purchase_orders WHERE " + where_sql("late_purchase_orders")
        + " AND supplier IS NOT NULL GROUP BY supplier) s ORDER BY late DESC, supplier LIMIT %(limit)s",
        ("limit",), MAX_ROWS),
    # customer orders past their required date (open only, the order view's days_late)
    "late_order_count": Query(
        "SELECT count(DISTINCT handle) AS late FROM sales_orders WHERE "
        + where_sql("late_orders")),
    "late_order_list": Query(
        "SELECT order_number, customer, customer_po, days_late, required_date, order_date, "
        "value, value_currency, salesperson, handle, withheld FROM sales_orders "
        "WHERE " + where_sql("late_orders") + " ORDER BY days_late DESC, order_number, "
        "handle LIMIT %(limit)s OFFSET %(offset)s", ("limit", "offset"), MAX_ROWS),
    # the SQL alert rules' open alerts (``operating.pg.alerts``; the guarded ``alerts`` view
    # shows one only when the reader holds every row it was computed from, as the door is
    # now). ``rules``: the alert types asked for, comma-separated (the page sends the ones on
    # and routed for the reader). Only a rule whose last finished run is within
    # ``alerts.STALE_MINUTES`` (``_FRESH``) is read: an older one's items are never shown
    # (the page says "unavailable since", from ``alert_runs``), in the same snapshot.
    "alerts_count": Query(
        "SELECT rule, count(*) AS open, min(since) AS oldest FROM alerts WHERE state = 'open' "
        "AND rule = ANY(string_to_array(%(rules)s, ',')) AND " + _FRESH + " GROUP BY rule "
        "ORDER BY rule", ("rules",), 64),
    # ``subject``: the alerted record's own name (its ``records`` row as this reader may see
    # it; none when they may not), so a row is titled "BE FIT NOW", never "Portal customer"
    "alerts_open": Query(
        "SELECT rule, kind, record_table, handle, topic, why, since, first_seen, "
        "(SELECT r.label FROM records r WHERE r.handle = alerts.handle ORDER BY r.record_id "
        "LIMIT 1) AS subject FROM alerts "
        "WHERE state = 'open' AND rule = ANY(string_to_array(%(rules)s, ',')) AND " + _FRESH +
        " ORDER BY since, rule, handle, topic LIMIT %(limit)s OFFSET %(offset)s",
        ("rules", "limit", "offset"), MAX_ROWS),
    # the alerts that state a money impact, biggest first (Home's Financials)
    "alerts_impact_top": _impact_top(),
    # per rule its last finished run and last run (no reason text)
    "alert_runs": Query("SELECT rule, ok_at, run_at, status FROM alert_runs ORDER BY rule",
                        (), 64),
    # Home's and the late list's late POs as the po_late alert rule found them: the alert
    # joined to the order's own row (both guarded views: a row only when the reader may see
    # the alert AND the order; its cells as the order view masks them)
    "alerts_late_po_count": Query(
        "SELECT count(*) AS late FROM alerts a JOIN purchase_orders p ON p.handle = a.handle "
        "WHERE a.rule = 'po_late' AND a.state = 'open' AND NOT " + bc_pilot_po_sql("p")
        + " AND " + _FRESH.replace("rule", "a.rule", 1)),
    "alerts_late_po_list": Query(
        "SELECT p.po_number, p.supplier, p.days_late, p.promised_date, p.requested_date, "
        "p.total, p.total_currency, p.purchaser, p.line_count, p.handle, p.withheld "
        "FROM alerts a JOIN purchase_orders p ON p.handle = a.handle "
        "WHERE a.rule = 'po_late' AND a.state = 'open' AND NOT " + bc_pilot_po_sql("p")
        + " AND " + _FRESH.replace("rule", "a.rule", 1)
        + " ORDER BY p.days_late DESC NULLS LAST, p.po_number, p.handle "
        "LIMIT %(limit)s OFFSET %(offset)s", ("limit", "offset"), MAX_ROWS),
    "alerts_late_po_suppliers": Query(
        "SELECT supplier, late, count(*) OVER () AS suppliers FROM (SELECT p.supplier, "
        "count(*) AS late FROM alerts a JOIN purchase_orders p ON p.handle = a.handle "
        "WHERE a.rule = 'po_late' AND a.state = 'open' AND p.supplier IS NOT NULL AND NOT "
        + bc_pilot_po_sql("p") + " AND " + _FRESH.replace("rule", "a.rule", 1) + " GROUP BY p.supplier) s "
        "ORDER BY late DESC, supplier LIMIT %(limit)s", ("limit",), MAX_ROWS),
    "views_as_of": Query("SELECT view, built_at FROM view_state ORDER BY view", (), 64),
    # ---- the one search box: every kind of business record by name or number --------
    # two parallel workers at most: measured on the live box at load ~42 (owner's door,
    # 11-22): one search 0 workers p95 453 ms, 2 -> 209, 4 -> 163; four at once 2 -> p95 320
    # ms, 4 -> 299 but max 685 ms (workers contend). Two keeps most of the gain at half the CPU.
    "find": Query(FIND_SQL, ("q", "limit"), 50, workers=2),
    "find_near": Query(FIND_NEAR_SQL, ("q", "limit"), 20),
    # ---- the account page: a customer (``handle``) or a chain (``chain``: every store
    # account under it, rolled up). The other parameter is '' (matches nothing).
    "customer_accounts": Query(
        "SELECT name, customer_code, codes, currency, salesperson, credit_limit, "
        "credit_limit_currency, blocked, chain, chain_code, chain_basis, email, phone, "
        "contact_person, handle, withheld FROM customers WHERE handle = NULLIF(%(handle)s, '') "
        "OR chain = NULLIF(%(chain)s, '') ORDER BY name, handle LIMIT 500",
        ("handle", "chain"), 500),
    "customer_chain_size": Query(
        "SELECT count(*) AS accounts FROM customers WHERE chain = (SELECT chain FROM "
        "customers WHERE handle = %(handle)s)", ("handle",)),
    "customer_open_orders": Query(
        "SELECT order_number, customer, customer_po, order_date, required_date, days_late, "
        "value, value_currency, salesperson, line_count, handle, withheld FROM sales_orders "
        f"WHERE status = 'open' AND {_CUSTOMER} ORDER BY required_date NULLS LAST, "
        "order_number, handle LIMIT %(limit)s", ("handle", "chain", "limit"), MAX_ROWS),
    "customer_open_totals": Query(
        "SELECT value_currency AS currency, count(*) AS orders, sum(value) AS value, "
        "count(*) FILTER (WHERE value IS NULL) AS unvalued, "
        "count(*) FILTER (WHERE " + where_sql("late_orders") + ") AS late FROM sales_orders "
        f"WHERE status = 'open' AND {_CUSTOMER} GROUP BY 1 ORDER BY 1 NULLS LAST",
        ("handle", "chain"), 64),
    "customer_owed": Query(
        "SELECT amount_due_currency AS currency, aging_bucket, count(*) AS documents, "
        "sum(amount_due) AS amount, count(*) FILTER (WHERE amount_due IS NULL) AS hidden "
        f"FROM customer_balances WHERE {_CUSTOMER} GROUP BY 1, 2 ORDER BY 1 NULLS LAST, 2",
        ("handle", "chain"), 64),
    "customer_invoices": Query(
        "SELECT invoice_number, customer, invoice_date, due_date, order_number, total, "
        "total_currency, remaining, remaining_currency, handle, withheld FROM sales_invoices "
        f"WHERE NOT COALESCE(pilot, false) AND {_CUSTOMER} ORDER BY invoice_date DESC "
        "NULLS LAST, invoice_number DESC, handle LIMIT %(limit)s",
        ("handle", "chain", "limit"), MAX_ROWS),
    "customer_sales_years": Query(
        "SELECT subtotal_currency AS currency, " + _YEARS.format(day="invoice_date",
                                                                  amount="subtotal")
        + f" FROM sales_invoices, d WHERE NOT COALESCE(pilot, false) AND {_CUSTOMER} "
        "AND invoice_date >= d.last_start GROUP BY 1 ORDER BY 1 NULLS LAST",
        ("handle", "chain", "as_of"), 64, with_day=True),
    # ---- the supplier's account page (``handle``)
    "supplier_account": Query(
        "SELECT name, supplier_code, codes, currency, status, blocked, supplier_type, "
        "incoterms, lead_time_days, email, phone, website, handle, withheld FROM suppliers "
        "WHERE handle = %(handle)s", ("handle",)),
    "supplier_open_pos": Query(
        "SELECT po_number, supplier, order_date, requested_date, promised_date, verdict, "
        "days_late, total, total_currency, purchaser, line_count, handle, withheld "
        "FROM purchase_orders WHERE status = 'open' AND supplier_handle = %(handle)s "
        "ORDER BY requested_date NULLS LAST, po_number, handle LIMIT %(limit)s",
        ("handle", "limit"), MAX_ROWS),
    # other supplier accounts by the same name (a BC payment vendor "X (EFT)" beside the
    # purchasing vendor "X" the POs name): listed beside the account, never merged into it
    "supplier_same_name": Query(
        "WITH me AS (SELECT " + _NAME_KEY.format(col="name") + " AS k FROM suppliers "
        "WHERE handle = %(handle)s) SELECT s.name, s.supplier_code, s.handle, s.withheld, "
        "(SELECT count(*) FROM purchase_orders p WHERE p.status = 'open' AND "
        "p.supplier_handle = s.handle) AS open_pos FROM suppliers s, me "
        "WHERE s.handle <> %(handle)s AND me.k <> '' AND "
        + _NAME_KEY.format(col="s.name") + " = me.k ORDER BY s.name, s.handle LIMIT 10",
        ("handle",), 10),
    "supplier_open_totals": Query(
        "SELECT total_currency AS currency, count(*) AS orders, sum(total) AS value, "
        "count(*) FILTER (WHERE total IS NULL) AS unvalued, "
        "count(*) FILTER (WHERE " + where_sql("late_purchase_orders") + ") AS late "
        "FROM purchase_orders WHERE status = 'open' AND supplier_handle = %(handle)s GROUP BY 1 "
        "ORDER BY 1 NULLS LAST", ("handle",), 64),
    "supplier_owed": Query(
        "SELECT amount_due_currency AS currency, aging_bucket, count(*) AS documents, "
        "sum(amount_due) AS amount, count(*) FILTER (WHERE amount_due IS NULL) AS hidden "
        "FROM supplier_balances WHERE supplier_handle = %(handle)s GROUP BY 1, 2 "
        "ORDER BY 1 NULLS LAST, 2", ("handle",), 64),
    "supplier_invoices": Query(
        "SELECT document_number, document_type, document_date, due_date, po_number, "
        "original_amount, original_amount_currency, amount_due, amount_due_currency, paid, "
        "handle, withheld FROM supplier_invoices WHERE NOT COALESCE(pilot, false) AND "
        "supplier_handle = %(handle)s ORDER BY document_date DESC NULLS LAST, "
        "document_number DESC, handle LIMIT %(limit)s", ("handle", "limit"), MAX_ROWS),
    "supplier_buys_years": Query(
        "SELECT total_currency AS currency, " + _YEARS.format(day="order_date",
                                                               amount="total")
        + " FROM purchase_orders, d WHERE status IN ('open', 'closed') AND "
        "supplier_handle = %(handle)s AND order_date >= d.last_start GROUP BY 1 "
        "ORDER BY 1 NULLS LAST", ("handle", "as_of"), 64, with_day=True),
    # ---- the order pages: header, then its lines
    # total_basis / lines_total read by name from the row (NULL from a table built before
    # them, until the views' next rebuild: the page then shows the stated total only)
    "purchase_order": Query(
        "SELECT po_number, supplier, vendor_number, order_date, requested_date, "
        "promised_date, status, status_reason, verdict, verdict_reason, days_late, total, "
        "total_currency, to_jsonb(p) ->> 'total_basis' AS total_basis, "
        "(to_jsonb(p) ->> 'lines_total')::numeric AS lines_total, "
        "to_jsonb(p) ->> 'lines_total_currency' AS lines_total_currency, purchaser, "
        "line_count, supplier_handle, source_systems, handle, withheld "
        "FROM purchase_orders p WHERE handle = %(handle)s", ("handle",)),
    "purchase_order_lines": Query(
        "SELECT item_code, description, ordered_quantity, received_quantity, "
        "invoiced_quantity, uom, unit_price, unit_price_currency, line_total, "
        "line_total_currency, expected_date, handle, withheld FROM purchase_order_lines "
        "WHERE po_handle = %(handle)s ORDER BY item_code NULLS LAST, description, handle "
        "LIMIT %(limit)s", ("handle", "limit"), MAX_ROWS),
    # ---- the precomputed page of a record (``operating.pg.record_pages``, the served store's
    # ``record`` items): every item of the merged record's page in one indexed statement; a cell
    # the reader may not read comes back with ``payload`` NULL and ``payload`` in ``withheld``
    "record_page": Query(
        "SELECT section, seq, payload, payload::text AS payload_text, src_handle, "
        "source_systems, handle, withheld "
        "FROM page_items WHERE page_kind = 'record' AND page_key = %(record_id)s "
        "ORDER BY section, seq "
        "LIMIT %(limit)s", ("record_id", "limit"), 2000),
    # ---- one stored plan section of a record (``operating.pg.plan_sections``): the late
    # section's answer, served only to a reader who holds the classes of every record it cites
    "plan_section": Query(
        "SELECT section, payload::text AS payload_text, comm_handles, handle, built_at, "
        "src_stamp, \"current\" FROM plan_section_items WHERE record_id = %(record_id)s AND section = %(section)s "
        "LIMIT 2", ("record_id", "section"), 2),
    "sales_order": Query(
        "SELECT order_number, customer, customer_code, customer_po, order_date, "
        "required_date, status, status_reason, order_type, days_late, value, value_currency, "
        "salesperson, line_count, customer_chain, customer_handle, source_systems, "
        "to_jsonb(s) ->> 'open_state' AS open_state, handle, "
        "withheld FROM sales_orders s WHERE handle = %(handle)s", ("handle",)),
    "sales_order_lines": Query(
        "SELECT item_code, item_name, description, ordered_quantity, shipped_quantity, "
        "outstanding_quantity, uom, unit_price, unit_price_currency, line_amount, "
        "line_amount_currency, handle, withheld FROM sales_order_lines "
        "WHERE order_handle = %(handle)s ORDER BY item_code NULLS LAST, description, handle "
        "LIMIT %(limit)s", ("handle", "limit"), MAX_ROWS),
    # ---- the item's page (``handle``): its row, its stock from the stock view, its batches
    "product": Query(
        "SELECT name, item_code, item_type, category, uom, active, blocked, unit_cost, "
        "unit_cost_currency, unit_price, unit_price_currency, handle, withheld FROM products "
        "WHERE handle = %(handle)s", ("handle",)),
    "product_stock": Query(
        "SELECT s.on_hand, s.on_hand_unit, s.as_of, s.handle, s.withheld FROM stock s "
        "WHERE s.item_code = (SELECT p.item_code FROM products p WHERE p.handle = %(handle)s) "
        "AND s.item_code IS NOT NULL", ("handle",)),
    "product_batches": Query(
        "SELECT b.batch_code, b.lot_number, b.status, b.quality_status, b.scheduled_date, "
        "b.planned_quantity, b.produced_quantity, b.handle, b.withheld FROM batches b WHERE "
        "b.item_code = (SELECT p.item_code FROM products p WHERE p.handle = %(handle)s) AND "
        "b.item_code IS NOT NULL ORDER BY b.scheduled_date DESC NULLS LAST, b.batch_code, "
        "b.handle LIMIT %(limit)s", ("handle", "limit"), MAX_ROWS),
    # ---- the supplier invoice's page (``handle``): one row of the supplier invoices view
    "supplier_invoice": Query(
        "SELECT supplier, supplier_code, supplier_handle, document_number, document_type, "
        "document_date, due_date, original_amount, original_amount_currency, amount_due, "
        "amount_due_currency, paid, po_number, description, vendor_group, payee_class, pilot, "
        "handle, withheld FROM supplier_invoices WHERE handle = %(handle)s", ("handle",)),
    # ---- the batch's page (``handle``)
    "batch": Query(
        "SELECT batch_code, lot_number, product, item_code, status, quality_status, on_hold, "
        "scheduled_date, realized_date, released_date, planned_quantity, produced_quantity, "
        "production_line, po_number, qc_ph_24h, qc_viscosity_24h, qc_tpc_yeast, tested_by, "
        "checked_by, handle, withheld FROM batches WHERE handle = %(handle)s", ("handle",)),
}


class PageReadError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _wanted(arguments: Mapping[str, Any]) -> list[tuple[str, Query, dict[str, Any]]]:
    """The reads asked for, each known and with exactly its parameters (fail closed)."""
    reads = arguments.get("reads") if isinstance(arguments, Mapping) else None
    if not isinstance(arguments, Mapping) or set(arguments) - {"reads", "ids"} \
            or not isinstance(reads, list) or not reads or len(reads) > MAX_READS \
            or not isinstance(arguments.get("ids", False), bool):
        raise PageReadError("invalid_request", f"send {{\"reads\": [{{\"name\": ...}}], "
                            f"\"ids\": false}} (1 to {MAX_READS} reads)")
    out = []
    for read in reads:
        if not isinstance(read, Mapping) or set(read) - {"name", "params"}:
            raise PageReadError("invalid_request", "a read is {name, params}")
        q = QUERIES.get(read.get("name"))   # type: ignore[arg-type]
        if q is None:
            raise PageReadError("unknown_read", f"no page read named {read.get('name')!r}")
        params = read.get("params") or {}
        if not isinstance(params, Mapping) or set(params) != set(q.params):
            raise PageReadError("invalid_request", f"{read['name']} takes {list(q.params)}")
        for k, v in params.items():
            if k == "limit" and (not isinstance(v, int) or isinstance(v, bool)
                                 or not 1 <= v <= q.rows):
                raise PageReadError("invalid_request", f"limit is 1..{q.rows}")
            if k == "offset" and (not isinstance(v, int) or isinstance(v, bool)
                                  or not 0 <= v <= MAX_OFFSET):
                raise PageReadError("invalid_request", f"offset is 0..{MAX_OFFSET}")
            if k == "as_of" and not (isinstance(v, str) and _ISO_DAY.match(v)):
                raise PageReadError("invalid_request", "as_of is a day (YYYY-MM-DD)")
            if not isinstance(v, (str, int)) or isinstance(v, bool) \
                    or (isinstance(v, str) and len(v) > 200):
                raise PageReadError("invalid_request", f"{k} is a string or an integer")
        out.append((read["name"], q, dict(params)))
    return out


_POOLS: dict[str, Any] = {}
_POOLS_LOCK = threading.Lock()


def _read_only(conn: Any) -> None:
    # as the store connection: autocommit, each read its own explicit READ ONLY transaction
    conn.autocommit = True
    conn.read_only = True


def pool_for(dsn: str) -> Any:
    """The page pool of the store at ``dsn`` (made on first use, kept for the process)."""
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            from operating.pg.pool import Pool
            size = os.environ.get("PLX_PAGE_POOL", "")
            pool = _POOLS[dsn] = Pool(
                dsn, max_size=int(size) if size.isdigit() and 0 < int(size) <= 32
                else PAGE_POOL, timeout=POOL_WAIT, configure=_read_only, connect_timeout=5,
                statement_timeout_ms=int(PAGE_SECONDS * 1000))
        return pool


def _seconds() -> float:
    from operating import call_limits
    left = call_limits.seconds_left()
    if left is not None and left <= 0.5:
        raise PageReadError("timeout", "no time is left in this call for the page's reads")
    return PAGE_SECONDS if left is None else max(0.5, min(PAGE_SECONDS, left - 0.5))


def _ids(pg: Any, handles: list[str]) -> dict[str, dict[str, str]]:
    """The record each handle (already shown to the reader) opens: its current version's
    object, one read (``object_versions_by_handle``)."""
    if not handles:
        return {}
    out: dict[str, dict[str, str]] = {}
    with pg.transaction():
        pg.execute("SET TRANSACTION READ ONLY")
        pg.execute(f"SET LOCAL statement_timeout = {int(PAGE_SECONDS * 1000)}")
        for handle, oid, otype in pg.execute(
                "SELECT v.l1_handle, o.object_id, o.object_type FROM ontology.object_versions v "
                "JOIN ontology.objects o ON o.object_id = v.object_id "
                "WHERE v.l1_handle = ANY(%s) AND v.superseded_at IS NULL "
                "ORDER BY v.l1_handle, o.object_id", (handles,)).fetchall():
            out.setdefault(handle, {"object_id": oid, "object_type": otype})
    return out


class PageReads:
    """The reads over ``base`` (the host's ``OntologyTools``) through ``plx.sql``'s door,
    on the page pool's own connections."""

    def __init__(self, base: Any) -> None:
        self.base = base
        self.decided = threading.local()   # the door ``decide_door`` made on this thread

    def _dsn(self) -> str:
        conn = getattr(getattr(self.base, "store", None), "conn", None)
        dsn = getattr(conn, "dsn", None)
        # never ``conn._pg``: opening the store's live connection takes its lock
        if getattr(conn, "postgres", False) is not True or not isinstance(dsn, str):
            raise PageReadError("not_served", "page reads read the Postgres store only")
        return dsn

    def _allowed_files(self, dsn: str, actor: Any) -> tuple[list[str], str]:
        """(the linked files this reader's item gate lets through, sorted; a status): the status
        is '' or ``gate_error`` (the gate failed on at least one file: those are not allowed, the
        page says the files kind failed, never "0 files") or ``index_error`` (the linked files
        could not be listed). Every failure is logged."""
        import logging
        from operating.pg import sql_views
        log = logging.getLogger("operating.page_reads")
        try:
            with pool_for(dsn).connection() as pg:
                with pg.transaction():
                    handles = [r[0] for r in pg.execute(
                        f"SELECT DISTINCT comm_handle FROM {sql_views.person_schema()}.record_comms "
                        "WHERE comm_view = 'sharepoint_files'").fetchall()]
        except Exception as exc:  # noqa: BLE001 - said to the reader as files `failed`, and logged
            log.warning("linked files not listed: %s", type(exc).__name__)
            return [], "index_error"
        ahead = getattr(self.base.item_gate, "prefetch", None)
        if handles and callable(ahead):
            try:
                ahead(actor, handles)
            except Exception as exc:  # noqa: BLE001 - a read ahead decides nothing: logged, and the
                # gate below decides each file itself (an error there is the typed gate_error)
                log.warning("item gate read-ahead failed: %s", type(exc).__name__)
        allowed, failed = [], 0
        for h in handles:
            got = self.base._item_decision(actor, h)
            if got is None:
                failed += 1
            elif got:
                allowed.append(h)
        if failed:
            log.warning("item gate failed on %d of %d linked files", failed, len(handles))
        return sorted(allowed), "gate_error" if failed else ""

    def _decide_door(self, actor: Any) -> dict[str, Any]:
        """The reader's door as a page read decides it (``actor``: the gated actor the host
        passes, under the policy the host pinned for this call), checked in a read
        transaction at the current stamp; kept on this thread for ``grant_ahead``."""
        from operating.ontology_tools_sql import SqlTool, reader_door, scoped_read
        door = reader_door(self.base, actor)
        # the communications views' six settings (``SqlTool._comms_settings``), decided here with
        # the door and held with it: a GET never decides them (the owner's first decision reads the
        # Teams windows for a minute and more)
        from operating.pg import business_comms as bc
        tool = SqlTool(self.base)
        comms = tool._comms_settings(self.base._scope(actor, self.base.clock()), actor)
        # SharePoint files: a FULL reader's are listed by the door alone (as plx.sql does,
        # business_comms.FILES_FULL). Any other reader's go through the host's item gate (the real
        # rule, one definition: ``_item_allowed``) over every file the index links, decided HERE,
        # ahead, with the door: the allowed handles are the seventh setting and the page keeps
        # only those (``served.comms_sql``). No item gate on the host: files stay off, said.
        dsn = self._dsn()
        if tool._files_reader(actor):
            comms = {**comms, bc.FILES_SETTING: bc.FILES_FULL, FILES_OK: "[]", FILES_STATE: ""}
        elif getattr(self.base, "item_gate", None) is not None:
            allowed, state = self._allowed_files(dsn, actor)
            comms = {**comms, bc.FILES_SETTING: bc.FILES_SEARCH, FILES_OK: json.dumps(allowed),
                     FILES_STATE: state}
        else:
            comms = {**comms, bc.FILES_SETTING: "", FILES_OK: "[]", FILES_STATE: ""}
        dsn = self._dsn()
        stamp = door_stamp(dsn)   # read before: a stamp moved since makes the door re-decided
        with pool_for(dsn).connection() as pg:
            scoped_read(pg, door, lambda pg: None, 0, person=True)   # no statement limit
        self.decided.value = (door, dsn, stamp, comms)
        return {"door": "decided"}

    def call(self, arguments: Mapping[str, Any], actor: Any, *, door: Any = None,
             dsn: str | None = None) -> dict[str, Any]:
        """``door``/``dsn``: the reader's door decided ahead (``operator_api.doors``, by
        ``_decide_door``: the same ``reader_door`` this decides otherwise) with its store, read
        on the caller's thread; both None: the door decided here from the host's policy for
        ``actor``. One without the other is refused."""
        from operating.ontology_tools_sql import ToolError, reader_door, scoped_read
        import psycopg
        from operating.pg.pool import PoolTimeout
        try:
            if (door is None) != (dsn is None):
                raise PageReadError("invalid_request", "a door ahead comes with its store")
            if isinstance(arguments, Mapping) and "resolve" in arguments:
                return self._resolve(arguments, dsn)
            if isinstance(arguments, Mapping) and arguments.get("decide_door"):
                if door is not None:
                    raise PageReadError("invalid_request", "a door ahead is not decided again")
                return self._decide_door(actor)
            wanted = _wanted(arguments)
            dsn = dsn if dsn is not None else self._dsn()
            seconds = _seconds()

            def work(pg: Any) -> dict[str, Any]:
                got: dict[str, Any] = {}
                server = None   # the server's own parallel workers, read once if needed
                for name, q, params in wanted:
                    if q.workers is not None or server is not None:
                        if server is None:
                            (server,) = pg.execute("SELECT current_setting("
                                                   "'max_parallel_workers_per_gather')").fetchone()
                        pg.execute("SELECT set_config('max_parallel_workers_per_gather', %s, "
                                   "true)", (server if q.workers is None else str(q.workers),))
                    cur = pg.execute(q.text(), params)
                    cols = [d.name for d in cur.description or ()]
                    rows = cur.fetchmany(q.rows)
                    got[name] = {"columns": cols, "rows": [list(r) for r in rows]}
                return got

            with pool_for(dsn).connection() as pg:
                # a person's page: the person-only views (Freightcom kept; no model reads
                # this tool's answers), the reader's door decided in the read's own
                # transaction on this connection (never the store's own)
                # a read that has a live path (the record page's items, a plan section's
                # stored row) never waits on a refresh's lock (it holds one on a swapped table
                # for minutes): past LOCK_MS the door answers "not ready" and the page reads
                # the live path. Every other read has none and keeps the longer wait.
                has_live_path = any(name in LIVE_FALLBACK for name, _, _ in wanted)
                reads = scoped_read(pg, door if door is not None else reader_door(
                                        self.base, actor), work, seconds,
                                    person=True,
                                    **({"lock_ms": LOCK_MS} if has_live_path else {}))
                out: dict[str, Any] = {"reads": reads}
                if arguments.get("ids"):
                    handles = sorted({row[r["columns"].index("handle")]
                                      for r in reads.values() if "handle" in r["columns"]
                                      for row in r["rows"]} - {None})
                    out["ids"] = _ids(pg, handles)
            return out
        except PageReadError as exc:
            return {"error": {"code": exc.code, "message": exc.message}}
        except ToolError as exc:
            return {"error": {"code": exc.code, "message": exc.message[:600]}}
        except PoolTimeout:
            return {"error": {"code": "busy", "message": "every page connection is in use"}}
        except psycopg.Error as exc:   # the store could not be reached or refused
            return {"error": {"code": "unavailable", "message": type(exc).__name__}}


    def _resolve(self, arguments: Mapping[str, Any], dsn: str | None = None
                 ) -> dict[str, Any]:
        """``{"resolve": {"view": v, "object_id": id}}``: the handle of ``v``'s row read
        from that record (``handle`` None when there is none). Read from the lineage the
        build keeps, outside the door: the handle is for the page's own read through the
        door next, which shows the row only when the reader may read it; never returned to
        a reader (a page that finds no row says "not found" either way)."""
        from operating.pg import business_views as bv
        ask = arguments.get("resolve")
        if set(arguments) != {"resolve"} or not isinstance(ask, Mapping) \
                or set(ask) != {"view", "object_id"} or ask.get("view") not in RESOLVABLE \
                or not isinstance(ask.get("object_id"), str) \
                or not 0 < len(ask["object_id"]) <= 700:
            raise PageReadError("invalid_request", "send {\"resolve\": {\"view\": one of "
                                f"{sorted(RESOLVABLE)}, \"object_id\": ...}}}}")
        with pool_for(dsn or self._dsn()).connection() as pg:
            with pg.transaction():
                pg.execute("SET TRANSACTION READ ONLY")
                pg.execute(f"SET LOCAL statement_timeout = {int(PAGE_SECONDS * 1000)}")
                got = pg.execute(
                    f"SELECT handle FROM {bv.SCHEMA}.lineage WHERE view = %s AND "
                    "object_ids @> ARRAY[%s]::text[] ORDER BY handle LIMIT 1",
                    (ask["view"], ask["object_id"])).fetchone()
        return {"handle": got[0] if got else None}


# the record pages a record id can open (``PageReads._resolve``)
RESOLVABLE = frozenset({"purchase_orders", "sales_orders", "customers", "suppliers", "products",
                        "batches", "supplier_invoices"})


def register(table: dict[str, dict[str, Any]], base: Any) -> PageReads:
    reads = PageReads(base)
    if TOOL_NAME in table:
        raise ValueError(f"tool already registered: {TOOL_NAME}")
    table[TOOL_NAME] = {"name": TOOL_NAME, "handler": lambda arguments, actor: reads.call(
        arguments, actor), "reads": reads}
    return reads


def grant_ahead(view: Any) -> tuple[Any, str, int | None, dict[str, str]]:
    """A worker job (``operator_api.doors``): the door of ``view``'s reader decided now by the
    exact call a page read decides it in (``plx.page`` with ``decide_door``: the host's gated
    actor and pinned policy, then ``_decide`` in a read transaction at the current stamp, which
    keeps it for the request), with the store's dsn, the stamp it was decided at and the communications views' six settings. The
    ``PageReads`` is the host's own (``view.access.host.table``): the view's wrapped tool table
    holds the handler only."""
    table = getattr(getattr(getattr(view, "access", None), "host", None), "table", None) or {}
    reads = (table.get(TOOL_NAME) or {}).get("reads")
    if not isinstance(reads, PageReads):
        raise PageReadError("not_served", "this reader's host serves no page reads")
    reads.decided.value = None
    out = view._call(TOOL_NAME, {"decide_door": True})
    if isinstance(out, Mapping) and "error" in out:
        raise PageReadError(str(out["error"].get("code")), str(out["error"].get("message")))
    got = getattr(reads.decided, "value", None)
    if got is None:
        raise PageReadError("not_served", "the door was not decided")
    return got


def door_stamp(dsn: str) -> int | None:
    """The door's stamp now (one indexed read of the one-row ``door_state``)."""
    from operating.pg import business_views as bv, door
    with pool_for(dsn).connection() as pg:
        with pg.transaction():
            pg.execute("SET TRANSACTION READ ONLY")
            return door.stamp(pg, bv.SCHEMA)


# Records' lists: one set of reads per business view, from its own metadata
# (``operating.record_lists``), never written per view
from operating import record_lists as _record_lists  # noqa: E402

QUERIES.update(_record_lists.queries())

__all__ = ["TOOL_NAME", "QUERIES", "Query", "PageReads", "PageReadError", "pool_for",
           "register"]
