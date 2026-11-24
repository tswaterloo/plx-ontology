"""Records' lists, from the business views' own metadata (``operating.pg.business_views``):
one spec per curated view, generated, never written per view. A view's columns (``Col``: name,
kind, its money's paired ``<col>_currency``) say what its list shows, how a cell reads (money,
date, number, yes/no, text), what it sorts and searches by, and which columns offer a filter.

The spec drives both ends: the named SQL reads ``operating.page_reads`` runs (``queries``:
one fixed statement per view, sort column and direction; filters, text and paging are bound
parameters, never spliced) and the API's list model (``operator_api.page_sql.record_list``).
A view the page door does not serve (the communications tables: each person's own door)
is not listed.

What a list shows of a view, by rule over its columns and never by its name:
- left out: the BC pilot flag, a "lines total" beside a total, a few columns curated out
  (``_OMIT``), the permission columns, ``source_systems``, the paired currency columns (shown
  inside the money), key columns (``*_handle``), lists, and explanations of how another
  column was decided (``*_basis``, ``*_note``, ``*_reason``) and how a party's copies were joined;
- kept, in the view's own order: its status columns and up to four other text columns, two dates, two money columns, two
  numbers and one yes/no column;
- filters: the view's status columns (``business_views.STATUS_COLS``) and its yes/no columns,
  each as the choices the data holds with how many each has;
- text search: the kept text columns, matched by position, never as a pattern;
- default order: the newest date column first, else the first column."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

# the words of a column name that read in capitals ("PO number", not "Po number")
_CAPS = frozenset({"po", "so", "qc", "ph", "uom", "id", "ar", "ap", "gl", "bc", "url"})
_TECHNICAL = re.compile(r"(_handles?|_basis|_note|_reason)$|^(merged_by|codes)$")
_HIDDEN = frozenset({"handle", "access_class", "col_class", "ai_ok", "source_systems",
                     # the BC pilot flag is a bookkeeping flag (every order's page says its own
                     # source); lines_total repeats total except where they disagree, which the
                     # order's page says
                     "pilot", "lines_total"})
# Left out of one view's list by curation (live 11-23): the purchase orders' requested date is
# filled on 62 of 11,695 orders (2 of the 109 open ones) and the promised date on 1, so both
# columns read blank everywhere; the order's page still shows them where on record. The gap is
# in the sources (the late verdict says "no requested date" for 107 open orders).
_OMIT = {"purchase_orders": frozenset({"requested_date", "promised_date"}),
         # the order's liveness is read through the late measures and its page, never as
         # list columns (they do not exist in a table built before they were added)
         "sales_orders": frozenset({"open_state", "settle_days", "last_activity"})}
KEEP = {"text": 4, "date": 2, "money": 2, "number": 2, "boolean": 1}
MAX_FILTER_CHOICES = 30
MAX_FILTER_VALUE = 80
MAX_SEARCH = 80


def label_of(name: str) -> str:
    """A column's name as its heading: ``po_number`` is "PO number"."""
    words = [("%" if w == "pct" else w.upper() if w in _CAPS else w) for w in name.split("_") if w]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


@dataclass(frozen=True)
class Column:
    name: str
    label: str
    kind: str            # text | date | money | number | boolean
    currency: str | None = None   # a money column's paired currency column


@dataclass(frozen=True)
class Filter:
    name: str
    label: str
    kind: str            # choice (a value of the column) | boolean


@dataclass(frozen=True)
class ListSpec:
    view: str
    title: str
    columns: tuple[Column, ...]
    search: tuple[str, ...]
    filters: tuple[Filter, ...]
    sort: str            # the default order's column
    direction: str       # asc | desc

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def filter(self, name: str) -> Filter | None:
        return next((f for f in self.filters if f.name == name), None)


def _kind(raw: str) -> str | None:
    """How a business column reads in a list (None: not listed)."""
    return {"text": "text", "date": "date", "money": "money", "numeric": "number",
            "integer": "number", "boolean": "boolean"}.get(raw)


def spec_of(view: Any) -> ListSpec | None:
    """The list spec of one business view, None for a view no list is made of."""
    from operating.pg import business_views as bv
    if getattr(view, "fill", None) is not None or not getattr(view, "listed", True):
        return None   # a communications table: read through a person's own door
    names = {c.name for c in view.cols}
    kept: list[Column] = []
    used = dict.fromkeys(KEEP, 0)
    for c in view.cols:
        kind = _kind(c.kind)
        # a status column is always shown (its filter is the way into the list)
        status = kind == "text" and c.name in bv.STATUS_COLS
        if kind is None or c.name in _HIDDEN or _TECHNICAL.search(c.name) \
                or c.name in _OMIT.get(view.name, ()) \
                or (used[kind] >= KEEP[kind] and not status):
            continue
        pair = f"{c.name}_currency" if kind == "money" and f"{c.name}_currency" in names \
            else None
        if kind == "money" and pair is None:
            continue   # an amount without its currency is never shown
        used[kind] += 0 if status else 1
        kept.append(Column(c.name, label_of(c.name), kind, pair))
    if not kept:
        return None
    dates = [c.name for c in kept if c.kind == "date"]
    filters = [Filter(c.name, label_of(c.name), "choice") for c in kept
               if c.kind == "text" and c.name in bv.STATUS_COLS]
    filters += [Filter(c.name, label_of(c.name), "boolean") for c in kept if c.kind == "boolean"]
    return ListSpec(
        view=view.name, title=label_of(view.name), columns=tuple(kept),
        search=tuple(c.name for c in kept if c.kind == "text"), filters=tuple(filters),
        sort=dates[0] if dates else kept[0].name, direction="desc" if dates else "asc")


@lru_cache(maxsize=1)
def specs() -> dict[str, ListSpec]:
    """Every business view's list spec, in the registry's order."""
    from operating.pg import business_views as bv
    out: dict[str, ListSpec] = {}
    for name, view in bv.VIEWS.items():
        got = spec_of(view)
        if got is not None:
            out[name] = got
    return out


def param(filter_name: str) -> str:
    return f"f_{filter_name}"


# A yes/no filter that IS a measured fact selects with the measure's own definition (one
# definition for the page's count, the list, Insights, alerts and Ask): the batches' "on hold"
# is not the raw flag, which was true for 2,069 of 2,613 cancelled batches on 11-22.
_AS_MEASURE = {("batches", "on_hold"): "batches_on_hold"}
# A status choice that IS a measured fact reads the measure's own definition too: the late
# purchase orders' verdict is "late" for a BC-only order the measure, Home and the alerts leave
# out (a BC pilot order), so the list's own late (2 on 11-23) disagreed with Home's 1. A row
# the measure leaves out reads "<value> (not counted)" in the list, its filter and its count.
_CHOICE_AS_MEASURE = {("purchase_orders", "verdict", "late"): "late_purchase_orders"}


def _shown(view: str, column: str) -> str:
    """The SQL for a column as the list reads it: the stored column, or its measured value."""
    from operating import measures
    for (v, c, value), measure in _CHOICE_AS_MEASURE.items():
        if (v, c) == (view, column):
            return (f"(CASE WHEN \"{c}\" = '{value}' AND NOT coalesce("
                    f"{measures.where_sql(measure)}, false) THEN '{value} (not counted)' "
                    f"ELSE \"{c}\" END)")
    return f'"{column}"'


def _where(spec: ListSpec) -> str:
    from operating import measures
    terms = []
    if spec.search:
        hay = " || ' ' || ".join("coalesce(\"%s\", '')" % c for c in spec.search)
        terms.append(f"(%(q)s = '' OR position(lower(%(q)s) IN lower({hay})) > 0)")
    for f in spec.filters:
        p = param(f.name)
        measure = _AS_MEASURE.get((spec.view, f.name))
        if measure is not None and f.kind == "boolean":
            terms.append(f"(%({p})s = '' OR coalesce({measures.where_sql(measure)}, false) = "
                         f"(%({p})s = 'yes'))")
            continue
        terms.append(f"(%({p})s = '' OR {_shown(spec.view, f.name)} = %({p})s)"
                     if f.kind == "choice" else
                     f"(%({p})s = '' OR \"{f.name}\" IS NOT DISTINCT FROM (%({p})s = 'yes'))")
    return " AND ".join(terms) or "true"


def params_of(spec: ListSpec, *extra: str) -> tuple[str, ...]:
    return ("q",) * bool(spec.search) + tuple(param(f.name) for f in spec.filters) + extra


def list_name(view: str, sort: str, direction: str) -> str:
    return f"list__{view}__{sort}__{direction}"


def queries() -> dict[str, Any]:
    """The named page reads every list takes: its rows (per sort column and direction), its
    count and the choices of its filters."""
    from operating.page_reads import MAX_ROWS, Query
    out: dict[str, Any] = {}
    for spec in specs().values():
        shown = {c.name: _shown(spec.view, c.name) for c in spec.columns}
        select = ", ".join(f'"{n}"' if x == f'"{n}"' else f'{x} AS "{n}"'
                           for n, x in shown.items()) + "".join(
            f', "{c.currency}"' for c in spec.columns if c.currency)
        where = _where(spec)
        for c in spec.columns:
            for direction in ("asc", "desc"):
                out[list_name(spec.view, c.name, direction)] = Query(
                    f"SELECT {select}, handle, withheld FROM {spec.view} WHERE {where} ORDER BY "
                    f'"{c.name}" {direction.upper()} NULLS LAST, handle LIMIT %(limit)s OFFSET '
                    "%(offset)s", params_of(spec, "limit", "offset"), MAX_ROWS)
        out[f"list__{spec.view}__count"] = Query(
            f"SELECT count(*) AS n FROM {spec.view} WHERE {where}", params_of(spec))
        choices = [f for f in spec.filters if f.kind == "choice"]
        if choices:
            out[f"list__{spec.view}__choices"] = Query(" UNION ALL ".join(
                f"(SELECT '{f.name}' AS col, {_shown(spec.view, f.name)}::text AS value, "
                f"count(*) AS n FROM {spec.view} WHERE \"{f.name}\" IS NOT NULL GROUP BY 2 "
                f"ORDER BY 3 DESC, 2 "
                f"LIMIT {MAX_FILTER_CHOICES})" for f in choices), (),
                MAX_FILTER_CHOICES * len(choices))
    return out
