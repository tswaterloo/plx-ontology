"""Find what a name names (Fable spec 16.4 F-R3, mechanism 3): before the Ask filters on a
name it reads every customer, supplier, product and order the words match, by kind, with
counts split by status and by blocked / active, so the answer says WHICH one it took when
more than one fits (a label held four times in products: two blocked legacy codes, two
active; one name that is a customer and a product).

One statement over the reader's own guarded views (it runs as ``plx.sql {"names": "<words>"}``
through the same door, result id, rows, profile and scope line as any statement: nothing is
read that ``plx.sql`` could not read, and a count is a reference like any other). It reads
the words of the person, never a list of them: every word must be held by one of a kind's
label columns.

ENTITIES is the one declaration of where each named kind lives (its view, the columns that
carry its name or number, the columns that carry its state); ``tests`` check every column
named here exists in the views. A kind added here is found by every question."""
from __future__ import annotations

import re
from typing import Any, Sequence

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# kind, view, label columns, state columns (text status / boolean blocked / boolean active)
ENTITIES = (
    ("customer", "customers", ("name", "customer_code", "codes"), (), ("blocked",), ()),
    ("supplier", "suppliers", ("name", "supplier_code", "codes"), ("status",), ("blocked",), ()),
    ("product", "products", ("name", "item_code"), (), ("blocked",), ("active",)),
    ("sales order", "sales_orders", ("order_number", "customer_po", "customer"), ("status",), (), ()),
    ("purchase order", "purchase_orders", ("po_number", "supplier_po_number", "supplier"), ("status",), (), ()),
)


class NamesInputError(ValueError):
    pass


def words(query: Any) -> list[str]:
    got = _WORD.findall(query if isinstance(query, str) else "")
    if not got:
        raise NamesInputError("names: send the words of the name to find")
    return list(dict.fromkeys(w.casefold() for w in got))


def _like(word: str) -> str:
    # the words are letters and digits only (``words``): nothing in them needs quoting
    return "'%" + word + "%'"


def _raw(column: str) -> str:
    """A state column as the view holds it, text as written ('(none)' where the view holds no
    value): what 'blocked' or 'active' means is the view's own word for it (``describe``),
    never read from the value here."""
    return f"COALESCE(CAST({column} AS text), '(none)')"


def statement(query: Any, entities: Sequence[tuple] = ENTITIES) -> str:
    """The one SELECT: per kind, the rows its view holds whose label columns hold every
    word, counted by status and by blocked / active, with one example label each and the kind's whole count on each row."""
    ws = words(query)
    parts = []
    for kind, view, labels, status, blocked, active in entities:
        match = " AND ".join("(" + " OR ".join(f"{c} ILIKE {_like(w)}" for c in labels) + ")"
                             for w in ws)
        state = _raw(status[0]) if status else "'-'"
        flag = _raw(blocked[0]) if blocked else "'-'"
        live = _raw(active[0]) if active else "'-'"
        parts.append(f"SELECT '{kind}' AS kind, {state} AS status, {flag} AS blocked, "
                     f"{live} AS active, count(*) AS n, CAST(sum(count(*)) OVER () AS bigint) AS "
                     f"kind_total, min({labels[0]}) AS example "
                     f"FROM {view} WHERE {match} GROUP BY 2, 3, 4")
    return "\nUNION ALL\n".join(parts) + "\nORDER BY 1, 2, 3, 4"


def scope(query: Any, generic: Any = None) -> str:
    """The scope line of a names result: the generic one reads the five kinds' conditions as
    one filter, so this says what the statement is: per kind, every word in one of its label
    columns, one row per status and state; the rows-shown tail of the generic line stays."""
    tail = str(generic).rsplit("; ", 1)[-1] if generic else ""
    kinds = "; ".join(f"{k} ({v}: {', '.join(l)})" for k, v, l, *_ in ENTITIES)
    return (f"names: every word of {' '.join(words(query))!r} held by one of a kind's label "
            f"columns, counted per kind by status and state as the view writes them (kind_total on "
            f"every row is the kind's whole count: sum n over a kind's rows or read kind_total); kinds "
            f"read: {kinds}" + (f"; {tail}" if tail else ""))
