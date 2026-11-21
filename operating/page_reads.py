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

import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping

TOOL_NAME = "plx.page"
MAX_READS = 12
MAX_ROWS = 500
MAX_OFFSET = 20000
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


def _rank(exact: str, prefix: str, name: str | None = None) -> str:
    """0 a number or code equal to the text, 1 a name equal to it, 2 a number or code
    starting with it, 3 a name starting with it, 4 a word of the name starting with it, 5
    a name holding it, 9 no match. ``exact``/``prefix``: SQL tests on ``k.q``."""
    out = f"CASE WHEN {exact} THEN 0 "
    if name:
        out += f"WHEN lower({name}) = k.q THEN 1 "
    out += f"WHEN {prefix} THEN 2 "
    if name:
        out += (f"WHEN strpos(lower({name}), k.q) = 1 THEN 3 "
                f"WHEN strpos(' ' || lower({name}), ' ' || k.q) > 0 THEN 4 "
                f"WHEN strpos(lower({name}), k.q) > 0 THEN 5 ")
    return out + "ELSE 9 END"


def _num(col: str) -> tuple[str, str]:
    return f"lower({col}) = k.q", f"strpos(lower({col}), k.q) = 1"


def _codes(code: str, codes: str) -> tuple[str, str]:
    """A party's own code or any system's code in ``codes`` ('sage:A1, bc:A1')."""
    return (f"(lower({code}) = k.q OR strpos(lower({codes}) || ',', ':' || k.q || ',') > 0)",
            f"(strpos(lower({code}), k.q) = 1 OR strpos(lower({codes}), ':' || k.q) > 0)")


def _find_sql() -> str:
    """One ranked list over every kind of record: parties and products by name or any
    system's code, orders, POs and batches by number (never by their party's name, so a
    name finds the party once). A chain (two or more accounts under one chain) is one
    result. No ``LIKE``: the text is never a pattern."""
    cx, cp = _codes("c.customer_code", "c.codes")
    sx, sp = _codes("s.supplier_code", "s.codes")
    ox, op_ = _num("o.order_number")
    cpx, cpp = _num("o.customer_po")
    px, pp = _num("p.po_number")
    ix, ip = _num("i.item_code")
    bx, bp = _num("b.batch_code")
    lx, lp = _num("b.lot_number")
    parts = [
        ("customer", "c.handle", "c.name", "c.customer_code", "c.chain", "NULL::text", "NULL::bigint",
         _rank(cx, cp, "c.name"), "customers c"),
        ("chain", "NULL::text", "g.chain", "g.chain_code", "NULL::text", "NULL::text", "g.n",
         _rank("lower(g.chain_code) = k.q", "strpos(lower(g.chain_code), k.q) = 1", "g.chain"),
         "(SELECT chain, chain_code, count(*) AS n FROM customers WHERE chain IS NOT NULL "
         "GROUP BY 1, 2 HAVING count(*) > 1) g"),
        ("supplier", "s.handle", "s.name", "s.supplier_code", "NULL::text", "s.status", "NULL::bigint",
         _rank(sx, sp, "s.name"), "suppliers s"),
        ("sales_order", "o.handle", "o.order_number", "o.customer_po", "o.customer", "o.status",
         "NULL::bigint", f"LEAST({_rank(ox, op_)}, CASE WHEN {cpx} THEN 2 ELSE 9 END)",
         "sales_orders o"),
        ("purchase_order", "p.handle", "p.po_number", "p.vendor_number", "p.supplier", "p.status",
         "NULL::bigint", _rank(px, pp), "purchase_orders p"),
        ("product", "i.handle", "i.name", "i.item_code", "i.category", "i.active", "NULL::bigint",
         _rank(ix, ip, "i.name"), "products i"),
        ("batch", "b.handle", "b.batch_code", "b.lot_number", "b.product", "b.status", "NULL::bigint",
         f"LEAST({_rank(bx, bp)}, {_rank(lx, lp)})", "batches b"),
    ]
    union = " UNION ALL ".join(
        f"SELECT * FROM (SELECT '{kind}' AS kind, {h} AS handle, {t} AS title, {c} AS code, "
        f"{ctx} AS context, {st} AS status, {n} AS members, {r} AS rank FROM {frm}, k) x"
        f" WHERE rank < 9" for kind, h, t, c, ctx, st, n, r, frm in parts)
    return ("WITH k AS (SELECT lower(btrim(%(q)s)) AS q WHERE length(btrim(%(q)s)) >= 2) "
            f"SELECT kind, handle, title, code, context, status, members, rank FROM ({union}) "
            "hit ORDER BY rank, CASE kind WHEN 'chain' THEN 0 WHEN 'customer' THEN 1 "
            "WHEN 'supplier' THEN 2 ELSE 3 END, length(title), title, handle "
            "LIMIT %(limit)s")


FIND_SQL = _find_sql()


# Each read is one statement over the guarded views; parameters bind as %(name)s.
QUERIES: dict[str, Query] = {
    # Home / My work / Watching: the late purchase orders (the PO view's own verdict)
    "late_po_count": Query(
        "SELECT count(*) AS late FROM purchase_orders WHERE verdict = 'late'"),
    "late_po_list": Query(
        "SELECT po_number, supplier, days_late, promised_date, requested_date, total, "
        "total_currency, purchaser, line_count, handle, withheld FROM purchase_orders "
        "WHERE verdict = 'late' ORDER BY days_late DESC NULLS LAST, po_number, handle "
        "LIMIT %(limit)s OFFSET %(offset)s", ("limit", "offset"), MAX_ROWS),
    # the sidebar's late POs by supplier: each supplier's count, most first, and how many
    # suppliers there are (a hidden supplier cell is no supplier: never named or counted)
    "late_po_suppliers": Query(
        "SELECT supplier, late, count(*) OVER () AS suppliers FROM (SELECT supplier, "
        "count(*) AS late FROM purchase_orders WHERE verdict = 'late' AND supplier IS NOT "
        "NULL GROUP BY supplier) s ORDER BY late DESC, supplier LIMIT %(limit)s",
        ("limit",), MAX_ROWS),
    # customer orders past their required date (open only, the order view's days_late)
    "late_order_count": Query(
        "SELECT count(*) AS late FROM sales_orders WHERE status = 'open' AND days_late > 0"),
    "late_order_list": Query(
        "SELECT order_number, customer, customer_po, days_late, required_date, order_date, "
        "value, value_currency, salesperson, handle, withheld FROM sales_orders "
        "WHERE status = 'open' AND days_late > 0 ORDER BY days_late DESC, order_number, "
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
    # per rule its last finished run and last run (no reason text)
    "alert_runs": Query("SELECT rule, ok_at, run_at, status FROM alert_runs ORDER BY rule",
                        (), 64),
    # Home's and the late list's late POs as the po_late alert rule found them: the alert
    # joined to the order's own row (both guarded views: a row only when the reader may see
    # the alert AND the order; its cells as the order view masks them)
    "alerts_late_po_count": Query(
        "SELECT count(*) AS late FROM alerts a JOIN purchase_orders p ON p.handle = a.handle "
        "WHERE a.rule = 'po_late' AND a.state = 'open' AND " + _FRESH.replace("rule", "a.rule", 1)),
    "alerts_late_po_list": Query(
        "SELECT p.po_number, p.supplier, p.days_late, p.promised_date, p.requested_date, "
        "p.total, p.total_currency, p.purchaser, p.line_count, p.handle, p.withheld "
        "FROM alerts a JOIN purchase_orders p ON p.handle = a.handle "
        "WHERE a.rule = 'po_late' AND a.state = 'open' AND " + _FRESH.replace("rule", "a.rule", 1)
        + " ORDER BY p.days_late DESC NULLS LAST, p.po_number, p.handle "
        "LIMIT %(limit)s OFFSET %(offset)s", ("limit", "offset"), MAX_ROWS),
    "alerts_late_po_suppliers": Query(
        "SELECT supplier, late, count(*) OVER () AS suppliers FROM (SELECT p.supplier, "
        "count(*) AS late FROM alerts a JOIN purchase_orders p ON p.handle = a.handle "
        "WHERE a.rule = 'po_late' AND a.state = 'open' AND p.supplier IS NOT NULL AND "
        + _FRESH.replace("rule", "a.rule", 1) + " GROUP BY p.supplier) s "
        "ORDER BY late DESC, supplier LIMIT %(limit)s", ("limit",), MAX_ROWS),
    "views_as_of": Query("SELECT view, built_at FROM view_state ORDER BY view", (), 64),
    # ---- the one search box: every kind of business record by name or number --------
    "find": Query(FIND_SQL, ("q", "limit"), 50),
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
        "count(*) FILTER (WHERE days_late > 0) AS late FROM sales_orders "
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
        "count(*) FILTER (WHERE verdict = 'late') AS late FROM purchase_orders "
        "WHERE status = 'open' AND supplier_handle = %(handle)s GROUP BY 1 "
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
    "sales_order": Query(
        "SELECT order_number, customer, customer_code, customer_po, order_date, "
        "required_date, status, status_reason, order_type, days_late, value, value_currency, "
        "salesperson, line_count, customer_chain, customer_handle, source_systems, handle, "
        "withheld FROM sales_orders WHERE handle = %(handle)s", ("handle",)),
    "sales_order_lines": Query(
        "SELECT item_code, item_name, description, ordered_quantity, shipped_quantity, "
        "outstanding_quantity, uom, unit_price, unit_price_currency, line_amount, "
        "line_amount_currency, handle, withheld FROM sales_order_lines "
        "WHERE order_handle = %(handle)s ORDER BY item_code NULLS LAST, description, handle "
        "LIMIT %(limit)s", ("handle", "limit"), MAX_ROWS),
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

    def _dsn(self) -> str:
        conn = getattr(getattr(self.base, "store", None), "conn", None)
        dsn = getattr(conn, "dsn", None)
        # never ``conn._pg``: opening the store's live connection takes its lock
        if getattr(conn, "postgres", False) is not True or not isinstance(dsn, str):
            raise PageReadError("not_served", "page reads read the Postgres store only")
        return dsn

    def call(self, arguments: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        from operating.ontology_tools_sql import ToolError, reader_door, scoped_read
        import psycopg
        from operating.pg.pool import PoolTimeout
        try:
            if isinstance(arguments, Mapping) and "resolve" in arguments:
                return self._resolve(arguments)
            wanted = _wanted(arguments)
            dsn = self._dsn()
            seconds = _seconds()

            def work(pg: Any) -> dict[str, Any]:
                got: dict[str, Any] = {}
                for name, q, params in wanted:
                    cur = pg.execute(q.text(), params)
                    cols = [d.name for d in cur.description or ()]
                    rows = cur.fetchmany(q.rows)
                    got[name] = {"columns": cols, "rows": [list(r) for r in rows]}
                return got

            with pool_for(dsn).connection() as pg:
                # a person's page: the person-only views (Freightcom kept; no model reads
                # this tool's answers), the reader's door decided in the read's own
                # transaction on this connection (never the store's own)
                reads = scoped_read(pg, reader_door(self.base, actor), work, seconds,
                                    person=True)
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


    def _resolve(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
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
        with pool_for(self._dsn()).connection() as pg:
            with pg.transaction():
                pg.execute("SET TRANSACTION READ ONLY")
                pg.execute(f"SET LOCAL statement_timeout = {int(PAGE_SECONDS * 1000)}")
                got = pg.execute(
                    f"SELECT handle FROM {bv.SCHEMA}.lineage WHERE view = %s AND "
                    "object_ids @> ARRAY[%s]::text[] ORDER BY handle LIMIT 1",
                    (ask["view"], ask["object_id"])).fetchone()
        return {"handle": got[0] if got else None}


# the record pages a record id can open (``PageReads._resolve``)
RESOLVABLE = frozenset({"purchase_orders", "sales_orders", "customers", "suppliers"})


def register(table: dict[str, dict[str, Any]], base: Any) -> PageReads:
    reads = PageReads(base)
    if TOOL_NAME in table:
        raise ValueError(f"tool already registered: {TOOL_NAME}")
    table[TOOL_NAME] = {"name": TOOL_NAME, "handler": lambda arguments, actor: reads.call(
        arguments, actor)}
    return reads


__all__ = ["TOOL_NAME", "QUERIES", "Query", "PageReads", "PageReadError", "pool_for",
           "register"]
