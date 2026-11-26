"""QC entries prepared from the batch events (the ``qc_entry`` autopilot, dry run).

Measured 2026-09-01..10-20 on the FileMaker QC rows: of 128 ``Batch.tested`` entries the
event just before was a production event on the same batch (a task started 47, made 29,
scheduled 23, the schedule starting 13), a median 1 to 4 days earlier; of 126
``Batch.released`` checks the event before was the batch realized (54) or its results
entered (42), a median 0.6 to 0.7 days earlier. So the trigger is a record event on the
batch, not a message, and two entries follow it:

* ``test``: a batch made (``Batch.manufactured``) in the last ``window_days`` with no
  results entered yet: the tests to enter (the 24 h pH, the 24 h viscosity, the plate count
  / yeast and mould result: the ones its row still has empty) with the last results of the
  same product's earlier batches beside each, as what the entry is read against;
* ``release``: a batch whose results were entered (``Batch.tested``) in the window, not
  released yet: the results as entered, to check and release.

Whose step: the person whose first name (``tested_by`` / ``checked_by``) is on the latest
earlier batch of the same product that names exactly one cleared person
(``day_coverage.user_code_person``); none found: no owner (never a whole role). A to-do for
the person: nothing is written (FileMaker is read-only). Read-only on the store.

Evidence: isolated-fixture (``operating/tests/test_autopilot_qc_entry.py``); the measure
on a real day is in MASTER (live-unverified).
"""
from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import autopilot as ap
from . import autopilot_handlers
from . import autopilot_records
from .autopilot_receivables import _day, _due_ids, _event, _spec

HANDLER = "qc_entry"
HEAD = ("batch_code", "lot_number", "status", "quality_status", "scheduled_date",
        "realized_date", "production_line", "planned_quantity", "produced_quantity",
        "po_number", "tested_by", "checked_by")
SCHEMA = Path(__file__).resolve().parent.parent / "ontology" / "schema" / "domains" / "manufacturing.json"
RESULT_PREFIX = "qc_"    # the batch columns that hold a result are the schema's own ``qc_*``
PRODUCT_LINK = "Batch.makes_bulk"
LOOKBACK = 12            # earlier batches of the product read for the owner and the references
REFERENCES = 3           # earlier results shown beside each test
HISTORY_DAYS = 400       # a batch with a test event this far back counts as tested


_columns: list[str] | None = None


def result_columns() -> list[str]:
    """The batch columns that hold a QC result: the ``qc_*`` properties the ontology's Batch
    type declares (read from the schema, never named here). Empty when the schema is
    unreadable: no entry is then prepared (``tests_unknown``)."""
    global _columns
    if _columns is None:
        try:
            doc = json.loads(SCHEMA.read_text("utf-8"))
        except (OSError, ValueError):
            return []

        def batch(o: Any) -> Any:
            if isinstance(o, dict):
                if o.get("id") == "Batch" and isinstance(o.get("properties"), list):
                    return o["properties"]
                for v in o.values():
                    if (got := batch(v)) is not None:
                        return got
            elif isinstance(o, list):
                for v in o:
                    if (got := batch(v)) is not None:
                        return got
            return None
        props = batch(doc) or []
        _columns = [p["name"] for p in props if isinstance(p, dict)
                    and str(p.get("name", "")).startswith(RESULT_PREFIX)]
    return list(_columns)


def label(column: str) -> str:
    """A result column in words: ``qc_ph_24h`` -> ``ph 24h`` (the column's own name)."""
    return column[len(RESULT_PREFIX):].replace("_", " ")


def _kind(spec: Mapping[str, Any], default: int = 2) -> int:
    return int((spec.get("params") or {}).get("window_days", default))


def _product(ctx: Any, batch: str) -> str | None:
    found = sorted({o for o in ctx.linked(batch, (PRODUCT_LINK,)) if o.startswith("Product:")})
    return found[0] if len(found) == 1 else None


def _peers(ctx: Any, batch: str, product: str | None, before: date) -> list[tuple[date, str, dict]]:
    """Earlier batches of the same product that have results or a tester on them, newest
    first: (realized day, id, row) (a batch still waiting for its results shows nothing)."""
    if not product:
        return []
    ids = [o for o in ctx.linked(product, (PRODUCT_LINK,)) if o.startswith("Batch:") and o != batch]
    rows = ctx.read(ids, HEAD + tuple(result_columns()))
    dated = [(d, o, rows[o]) for o in ids if o in rows and
             (d := _day(autopilot_records._v(rows[o], "realized_date"))) and d < before
             and any(autopilot_records._v(rows[o], p).strip()
                     for p in ("tested_by", "checked_by", *result_columns()))]
    return sorted(dated, key=lambda x: (x[0], x[1]), reverse=True)[:LOOKBACK]


def keyer(ctx: Any, peers: Sequence[tuple[date, str, dict]], prop: str, people: Any,
          *, staff_rows: Sequence[Mapping[str, Any]] | None = None) -> str | None:
    """Who enters this product's QC: the person named by ``prop`` on its latest earlier batch
    that names exactly one cleared person; None when none does or the people exclusion is
    unavailable. When ``staff_rows`` is supplied, IT/software engineers and executives are
    never selected as QC keyers."""
    from .autopilot_roster import _non_operational_ids
    from .day_coverage import user_code_person
    if people is None or getattr(people, "unavailable", None):
        return None
    directory, _addresses_of, domains = ap._people_maps(people)
    blocked = _non_operational_ids(staff_rows)
    for _d, _o, row in peers:
        oid = user_code_person(autopilot_records._v(row, prop), directory, domains)
        if oid and oid.lower() not in blocked and people.identity_clear(oid):
            return oid
    return None


def due_events(st: Any, registry: Mapping[str, Any], as_of: date, people: Any = None
               ) -> list[dict[str, Any]]:
    """The ``record.due`` events of the day: a ``test`` entry for each batch made in the
    window with no results entered, a ``release`` entry for each batch whose results were
    entered in the window and not released."""
    spec = _spec(registry, HANDLER)
    if spec is None:
        return []
    window = _kind(spec)
    lo, far = as_of - timedelta(days=window - 1), as_of - timedelta(days=HISTORY_DAYS)
    ctx = autopilot_handlers.Ctx(st, as_of, float("inf"), ())
    tested = set(_due_ids(st, "Batch.tested", "Batch", far, as_of))
    released = set(_due_ids(st, "Batch.released", "Batch", far, as_of))
    todo = [("test", b, "qc_test") for b in _due_ids(st, "Batch.manufactured", "Batch", lo, as_of)
            if b not in tested]
    todo += [("release", b, "qc_release") for b in _due_ids(st, "Batch.tested", "Batch", lo, as_of)
             if b not in released]
    heads = ctx.read([b for _k, b, _s in todo], HEAD)
    out = []
    for kind, batch, _stage in todo:
        head = heads.get(batch) or {}
        code = autopilot_records._v(head, "batch_code").strip()
        if not code:
            continue
        product = _product(ctx, batch)
        peers = _peers(ctx, batch, product, as_of)
        prop = "tested_by" if kind == "test" else "checked_by"
        event = _event(HANDLER, batch, "Batch", as_of.isoformat(),
                       autopilot_records._types(st, [batch]).get(batch, ("", ""))[1],
                       [c["l1_handle"] for c in head.values() if isinstance(c, Mapping) and c.get("l1_handle")],
                       {"batch": code, "entry": kind}, owner_hint=keyer(ctx, peers, prop, people))
        event["event_id"] = ap._digest("record.due", HANDLER, batch, kind)
        out.append(event)
    return out


def h_qc_entry(ctx: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    batch = event["object_id"]
    kind = (event.get("fields") or {}).get("entry") or "test"
    head = ctx.read([batch], HEAD + tuple(result_columns())).get(batch) or {}
    code = autopilot_records._v(head, "batch_code").strip()
    if not code:
        return {"skip": "batch_unread"}
    product = _product(ctx, batch)
    name = ""
    if product:
        name = autopilot_records._v(ctx.read([product], ("name", "code")).get(product) or {}, "name").strip()
    ctx.cite(head, "batch_code")
    links = [{"label": "Open the batch", "href": autopilot_records.object_href(batch)}]
    if kind == "release":
        entered = [{"test": label(col), "value": autopilot_records._v(head, col).strip()}
                   for col in result_columns() if autopilot_records._v(head, col).strip()]
        if not entered:
            return {"skip": "no_results_entered"}
        return {"summary": f"Batch {code}{f' ({name})' if name else ''}: results are in, "
                           "check and release it.",
                "object": {"object_id": batch, "number": code},
                "entry": {"kind": "qc_release", "batch": code, "results": entered},
                "gaps": [], "dry_run": True, "links": links}
    peers = _peers(ctx, batch, product, ctx.as_of)
    lines, gaps = [], []
    # the product's tests: the result columns its earlier batches filled in (a product is
    # not tested for what it never had a result for)
    for col in result_columns():
        recent = [autopilot_records._v(r, col).strip() for _d, _o, r in peers
                  if autopilot_records._v(r, col).strip()][:REFERENCES]
        if autopilot_records._v(head, col).strip() or not recent:
            continue                          # entered already, or not one of its tests
        lines.append({"test": label(col), "recent": recent})
    if not lines:
        return {"skip": "tests_unknown" if not peers else "nothing_to_enter"}
    return {"summary": f"Batch {code}{f' ({name})' if name else ''} is made: "
                       f"{len(lines)} QC result(s) to enter.",
            "object": {"object_id": batch, "number": code},
            "entry": {"kind": "qc_results", "batch": code, "tests": lines},
            "gaps": gaps, "dry_run": True, "links": links}
