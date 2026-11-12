"""Build steps whose links depend on the whole store, kept right over Postgres.

A delta layer (``ontology/delta.py``) is built from the changed records and their closure,
not from the whole store, and ``operating.pg.ontology_delta.apply_layer`` replaces every
link with an end at an owned object by the layer's. That is right for a link derived from
its two ends, and wrong for an allocation over every open line of an item: demand pegging
(``ontology.demand_pegging``, link prefix ``peg:``) lets each open customer order line take
open purchase order supply in date order, so its result for one line depends on every other
line of the item, which the layer does not hold. On 11-12 the catch-up layer replaced the
28 peg links touching its objects by the 2 its closure could compute (47+21 served before,
22+18 after; a full-store recompute over the same data gives 47+21 again).

So a layer apply leaves the links of the steps listed here (:data:`WHOLE_STEPS`, by link id
prefix) alone (neither deleted nor copied from the layer), and after the layers of a cycle
:func:`recompute` brings them up to date:

* **Pending** is read, never marked: an object of the step's input types
  (``ontology.step_migrate.STEPS``) some layer replaced (``ontology.applied_owned``) after
  the last run's mark (``store_meta`` ``whole_steps``: the newest ``applied_owned`` row it
  covered). Writing a mark in each layer's transaction moved the data version past what the
  traverse-facts fold accepts and made every host read the facts in full (review of
  4258858296).
* **Incremental** (the usual run): only the part of the allocation those objects can move
  (``operating.pg.peg_scope``), by the step's own code over a connection that reads only
  that part, in one transaction under the delta's lock (``ontology_migrate.LOCK``):
  seconds, where the whole store was 4 to 11 minutes under that lock.
* **Whole store** when there is no mark (after a load, or never run), the last whole run
  is older than ``PLX_WHOLE_STEPS_FULL_HOURS`` (6), or more than
  ``PLX_WHOLE_STEPS_SCOPE_MAX`` (2,000) objects changed (a catch-up): computed off the lock over one
  snapshot, then swapped in under the lock with an incremental run for the layers applied
  since that snapshot. It restores what the incremental run cannot see (a twin link a layer
  removed whose other end it does not own).
* Only links that changed are written (by id: gone, new, or other content). Their ends are
  then recorded as a layer of their own (``delta:whole-steps-<mark>-<ms>``, its objects in
  ``applied_owned``), so every reader of the traverse facts folds the change like any layer
  (``fold_layer_facts`` / ``catch_up_pg_facts`` / ``_carried``) instead of reading the
  version in full, and the kept facts are folded here at once. A run that changed nothing
  moves no data version.

A failed run changes nothing (its transaction rolls back) and the mark stays, so the next
cycle runs it again even with no new layer.

``python3 -m operating.pg.whole_steps [--full] [--dsn DSN]``: one run now (``--full``: the
whole store), its report on stdout.

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

STATE = "whole_steps"          # store_meta key: {"through": seq, "full_at": epoch seconds}
LAYER_PREFIX = "whole-steps-"   # the layers recording a run's changed link ends
FULL_HOURS = 6.0


def _peg_prefix() -> tuple[str, str]:
    from ontology import demand_pegging as dp
    return dp.PREFIX, dp._END


def _peg_declared() -> dict[str, Any] | None:
    from ontology import demand_pegging as dp
    from ontology.schema import load_schema
    cfg = dp.rules()
    links = load_schema().link_types
    return {"cfg": cfg, "links": links} if dp._declared(links, cfg) else None


def _peg_full(conn: Any, ctx: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    from ontology import demand_pegging as dp
    rows, counts = dp.peg_links(conn, ctx["cfg"])
    return rows, dict(counts)


def _peg_scoped(cur: Any, conn: Any, ctx: Mapping[str, Any], touched: set[str],
                served: Mapping[str, Mapping[str, Any]]
                ) -> tuple[list[dict[str, Any]], dict[str, int], Callable[[Mapping[str, Any]], bool]]:
    from operating.pg import peg_scope
    part = peg_scope.scope(cur, conn, ctx["cfg"], ctx["links"], touched, served)
    rows, counts, replaced = peg_scope.scoped_links(cur, conn, part, ctx["cfg"], served)
    counts.update({"scope_objects": len(part["universe"]), "scope_items": len(part["items"]),
                   "scope_rounds": part["rounds"]})
    return rows, counts, replaced


class Step:
    """A whole-store step: its link id range, whether the schema declares it (its context
    or None), its whole-store and its scoped computation, and the link types its scoped
    run reads at both ends (``peg_scope.ScopedConnection``)."""

    def __init__(self, prefix: Callable[[], tuple[str, str]],
                 declared: Callable[[], dict[str, Any] | None], full: Callable[..., Any],
                 scoped: Callable[..., Any],
                 both_ends: Callable[[Mapping[str, Any]], Sequence[str]]) -> None:
        self.prefix, self.declared, self.full, self.scoped = prefix, declared, full, scoped
        self.both_ends = both_ends

    def inputs(self, name: str) -> tuple[str, ...]:
        """The object types the step reads (``step_migrate.STEPS``: its declared inputs)."""
        from ontology import step_migrate
        return tuple(sorted(step_migrate.STEPS[name].inputs(None)))


WHOLE_STEPS: Mapping[str, Step] = {
    "supply_pegging": Step(_peg_prefix, _peg_declared, _peg_full, _peg_scoped,
                           lambda ctx: (ctx["cfg"]["item_identity_link"],)),
}


def ranges() -> list[tuple[str, str]]:
    """The link id ranges ``[start, end)`` of the whole-store steps."""
    return [step.prefix() for step in WHOLE_STEPS.values()]


def pg_excluded() -> str:
    """A Postgres condition true for a link row of no whole-store step (constants only)."""
    return " AND ".join(f"NOT (link_id COLLATE \"C\" >= '{a}' AND link_id COLLATE \"C\" < '{b}')"
                        for a, b in ranges()) or "TRUE"


def sqlite_excluded() -> str:
    """The same condition for a layer's (SQLite) ``links``."""
    return " AND ".join(f"NOT (link_id >= '{a}' AND link_id < '{b}')"
                        for a, b in ranges()) or "1"


def _types() -> list[str]:
    return sorted({t for name, step in WHOLE_STEPS.items() for t in step.inputs(name)})


def _state(cur: Any) -> dict[str, Any]:
    cur.execute("SELECT value FROM ontology.store_meta WHERE key = %s", (STATE,))
    row = cur.fetchone()
    try:
        held = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        return {}
    return held if isinstance(held, dict) and isinstance(held.get("through"), int) else {}


def _touched(cur: Any, through: int | None, *, limit: int | None = None) -> list[str]:
    """The objects of the steps' input types a layer (not a run's own) replaced after
    ``through`` (all recorded when None)."""
    cur.execute("SELECT to_regclass('ontology.applied_owned') IS NOT NULL")
    if not cur.fetchone()[0]:
        return []
    cur.execute("SELECT DISTINCT object_id FROM ontology.applied_owned WHERE seq > %s AND "
                "NOT starts_with(layer, %s) AND split_part(object_id, ':', 1) = ANY(%s)"
                + (f" LIMIT {int(limit)}" if limit else ""),
                (int(through or 0), LAYER_PREFIX, _types()))
    return [r[0] for r in cur.fetchall()]


def _newest(cur: Any) -> int:
    cur.execute("SELECT to_regclass('ontology.applied_owned') IS NOT NULL")
    if not cur.fetchone()[0]:
        return 0
    cur.execute("SELECT coalesce(max(seq), 0) FROM ontology.applied_owned")
    return int(cur.fetchone()[0])


def pending(dsn: str) -> bool:
    """A layer replaced an input of a whole-store step since its last run."""
    import psycopg
    with psycopg.connect(dsn, connect_timeout=10) as pg, pg.cursor() as cur:
        return bool(_touched(cur, _state(cur).get("through"), limit=1))


SCOPE_MAX = 2000


def _scope_max() -> int:
    try:
        return max(1, int(os.environ.get("PLX_WHOLE_STEPS_SCOPE_MAX") or SCOPE_MAX))
    except ValueError:
        return SCOPE_MAX


def _full_due(state: Mapping[str, Any], now: float) -> bool:
    if "through" not in state:
        return True
    try:
        hours = float(os.environ.get("PLX_WHOLE_STEPS_FULL_HOURS") or FULL_HOURS)
    except ValueError:
        hours = FULL_HOURS
    return now - float(state.get("full_at") or 0) >= hours * 3600


def _normal(rows: Sequence[Mapping[str, Any]], folder: Path, name: str) -> dict[str, tuple]:
    """The rows as the store writes them (``StoreBuilder.add``), by link id, every column."""
    from ontology import store_ddl
    from ontology.pg_step_inplace import _local_store
    from operating.pg.ontology_load import _clean
    local = _local_store(folder / f"{name}.sqlite")
    try:
        for i in range(0, len(rows), 5000):
            local.add("links", list(rows[i:i + 5000]))
        local.connection.commit()
        cols = ", ".join(store_ddl.LINK_COLUMNS)
        return {r[0]: tuple(_clean(v) for v in r)
                for r in local.connection.execute(f"SELECT {cols} FROM links")}
    finally:
        local.abort()


def _same(a: tuple, b: Mapping[str, Any]) -> bool:
    from ontology import store_ddl
    from operating.pg.ontology_load import _clean
    return all(_text(x) == _text(_clean(b.get(c))) for x, c in zip(a, store_ddl.LINK_COLUMNS))


def _text(value: Any) -> Any:
    return None if value is None else str(value)


def _write(cur: Any, new: Mapping[str, tuple], served: Mapping[str, Mapping[str, Any]],
           replaced: Callable[[Mapping[str, Any]], bool]) -> tuple[dict[str, int], set[str]]:
    """Make the served rows ``replaced`` selects the ``new`` ones (every new row must be
    one it selects): only gone, new and changed rows are written. Their counts, and the
    ends of every row written or removed."""
    from ontology import store_ddl
    from operating.pg.ontology_delta import _copy
    cols = store_ddl.LINK_COLUMNS
    outside = [lid for lid, row in new.items() if not replaced(dict(zip(cols, row)))]
    if outside:
        raise ValueError(f"whole_step_row_outside_its_part:{len(outside)}")
    mine = {lid for lid, row in served.items() if replaced(row)}
    gone = sorted(mine - set(new))
    changed = sorted(lid for lid in set(new) & mine if not _same(new[lid], served[lid]))
    added = sorted(set(new) - mine)
    if set(added) & set(served):   # a served row of another part: never two of one id
        raise ValueError("whole_step_row_of_another_part")
    ends = {x for lid in gone + changed for x in (served[lid]["from_object"],
                                                  served[lid]["to_object"])}
    ends |= {x for lid in changed + added for x in (new[lid][2], new[lid][3])}
    if gone or changed:
        cur.execute("DELETE FROM ontology.link_rows WHERE link_id = ANY(%s)", (gone + changed,))
    if changed or added:
        _copy(cur, "link_rows", cols, (new[lid] for lid in changed + added), keys={})
    return {"gone": len(gone), "changed": len(changed), "added": len(added),
            "kept": len(mine) - len(gone) - len(changed)}, ends


def _compute_full(dsn: str, folder: Path) -> tuple[dict[str, Any], int]:
    """Every declared step's rows over one snapshot of the whole store, off the lock, and
    the newest ``applied_owned`` row that snapshot holds."""
    from ontology.pg_step_inplace import SplitConnection, _local_store
    from operating.pg.sqlite_compat import PgCompatConnection
    pg = PgCompatConnection(dsn, statement_timeout_ms=0)
    cur = pg._pg.cursor()
    out: dict[str, Any] = {}
    try:
        cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cur.execute("SET LOCAL jit = off")
        through = _newest(cur)
        for name, step in sorted(WHOLE_STEPS.items()):
            ctx = step.declared()
            if ctx is None:
                out[name] = None
                continue
            t = time.monotonic()
            local = _local_store(folder / f"{name}-read.sqlite")
            try:
                rows, counts = step.full(SplitConnection(local.connection, pg), ctx)
            finally:
                local.abort()
            out[name] = {"rows": rows, "counts": counts,
                         "seconds": round(time.monotonic() - t, 1)}
        cur.execute("COMMIT")
    finally:
        pg.close()
    return out, through


def recompute(dsn: str, *, full: bool | None = None, work: str | Path | None = None,
              now: float | None = None) -> dict[str, Any]:
    """One run (module docstring): ``{"pending": False}`` when nothing is to be done, else
    its report. ``full``: True a whole-store run, False never one, None when due. Raises
    on failure (nothing is changed then and the mark stays)."""
    import psycopg
    from ontology.pg_step_inplace import _local_store
    from operating.pg import ontology_migrate as om, peg_scope
    from operating.pg.sqlite_compat import PgCompatConnection

    started = time.monotonic()
    now = time.time() if now is None else now
    with psycopg.connect(dsn, connect_timeout=10) as pg0, pg0.cursor() as cur0:
        state = _state(cur0)
        most = _scope_max()
        many = len(_touched(cur0, state.get("through"), limit=most + 1))
        if not many and not full:
            return {"pending": False}
    # a part as large as a catch-up's is slower than the whole store (15,925 replaced
    # objects: 467 s scoped, 220 s whole, 11-12)
    whole = (_full_due(state, now) or many > most) if full is None else full
    folder = Path(tempfile.mkdtemp(prefix=".whole-steps-", dir=work))
    report: dict[str, Any] = {"mode": "full" if whole else "incremental", "steps": {}}
    try:
        computed, base = _compute_full(dsn, folder) if whole else ({}, None)
        pg = PgCompatConnection(dsn, statement_timeout_ms=0)
        cur = pg._pg.cursor()
        try:
            cur.execute("BEGIN")
            t_lock = time.monotonic()
            cur.execute(om.LOCK)
            cur.execute("SET LOCAL jit = off")
            state = _state(cur)
            newest = _newest(cur)
            since = base if whole else state.get("through")
            touched = set(_touched(cur, since))
            ends: set[str] = set()
            for name, step in sorted(WHOLE_STEPS.items()):
                ctx = step.declared()
                if ctx is None:
                    report["steps"][name] = {"declared": False}
                    continue
                t = time.monotonic()
                start, end = step.prefix()
                out: dict[str, Any] = {}
                if whole:
                    served = peg_scope.served_peg_links(cur, start, end)
                    new = _normal(computed[name]["rows"], folder, name)
                    out["full"], got = _write(cur, new, served, lambda r: True)
                    out["full"]["counts"] = computed[name]["counts"]
                    out["full"]["seconds"] = computed[name]["seconds"]
                    ends |= got
                if touched:
                    served = peg_scope.served_peg_links(cur, start, end)
                    local = _local_store(folder / f"{name}-scoped.sqlite")
                    try:
                        conn = peg_scope.ScopedConnection(local.connection, pg,
                                                          step.both_ends(ctx))
                        conn.ends = {k: (v.get("from"), v.get("to"))
                                     for k, v in ctx["links"].items()}
                        rows, counts, replaced = step.scoped(cur, conn, ctx, touched, served)
                    finally:
                        local.abort()
                    new = _normal(rows, folder, name)
                    bad = [lid for lid in new if not (start <= lid < end)]
                    if bad:
                        raise ValueError(f"whole_step_link_outside_prefix:{name}:{len(bad)}")
                    out["incremental"], got = _write(cur, new, served, replaced)
                    out["incremental"]["counts"] = counts
                    ends |= got
                out["seconds"] = round(time.monotonic() - t, 1)
                report["steps"][name] = out
            mark = {"through": newest,
                    "full_at": now if whole else float(state.get("full_at") or 0)}
            cur.execute("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s) ON "
                        "CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (STATE, json.dumps(mark)))
            report["touched"] = len(touched)
            report["layer"] = _record(cur, ends, newest, now) if ends else None
            cur.execute("COMMIT")
            report["lock_seconds"] = round(time.monotonic() - t_lock, 1)
        except BaseException:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            pg.close()
    finally:
        for f in folder.glob("*"):
            f.unlink(missing_ok=True)
        folder.rmdir()
    if report["layer"]:
        report["facts_kept"] = _fold_facts(dsn)
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def _record(cur: Any, ends: set[str], newest: int, now: float) -> str:
    """The changed links' ends recorded as a layer of their own (module docstring)."""
    from operating.pg import ontology_migrate
    name = f"{LAYER_PREFIX}{newest}-{int(now * 1000)}"
    owned = sorted(ends)
    ontology_migrate.log_owned(cur, name, owned)
    cur.execute("SELECT value FROM ontology.load_state WHERE name = 'generation'")
    row = cur.fetchone()
    digest = (row[0] or {}).get("content_digest") if row else None
    cur.execute("INSERT INTO ontology.load_state (name, value) VALUES (%s, %s)",
                (f"delta:{name}", json.dumps({"owned": len(owned), "content_digest": digest,
                                              "whole_steps": True})))
    cur.execute("INSERT INTO ontology.load_state (name, value) VALUES ('delta_layers', %s) "
                "ON CONFLICT (name) DO UPDATE SET value = ontology.load_state.value || "
                "excluded.value, updated_at = now()", (json.dumps([name]),))
    return name


def _fold_facts(dsn: str) -> bool:
    """The traverse facts of the version the run made, folded from the kept ones over its
    changed links' ends (``catch_up_pg_facts``); False when none were kept to fold."""
    try:
        from operating import ontology_tools_traverse
        return bool(ontology_tools_traverse.catch_up_pg_facts(dsn))
    except Exception:  # noqa: BLE001 - a host then folds or reads the version itself
        return False


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.whole_steps")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--full", action="store_true", help="the whole store now")
    args = parser.parse_args(argv)
    report = recompute(args.dsn or config.dsn(), full=True if args.full else None)
    print(json.dumps(report, sort_keys=True, default=str), flush=True)
    return 0


__all__ = ["LAYER_PREFIX", "STATE", "WHOLE_STEPS", "pending", "pg_excluded", "ranges",
           "recompute", "sqlite_excluded"]


if __name__ == "__main__":
    sys.exit(main())
