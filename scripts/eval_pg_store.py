"""The generative evaluation's store reads (``scripts.eval_generative.GenStore``) over the
served Postgres ontology, as it stood at a stated time.

``PgGenStore`` answers every read the question generator makes (``evals.generative.Source``:
record picks, a record's values, versions and firm links, name lookups, the people guard's
clearance) from the ``ontology`` schema that ``operating.pg.ontology_reader`` serves, with
the same rows the SQLite generation's views give (migrations 0007/0008: ``object_values``
read from its tables, ``link_rows`` column for column). ``as_of`` (an ISO UTC time, ``...Z``) reads the rows
known then: a version, value source or link recorded at or before it and not superseded by
it, an object first recorded by it. Postgres keeps every version's history (a delta layer
replaces an object's rows with the layer's, history included), so a time at or after the
SQLite generation's commit reads that generation's rows, and a later one the newer
versions.

Read-only by construction: the session is ``default_transaction_read_only`` (any write is
refused by the server) with a statement timeout; no temporary table, no function is made.
Stdlib and psycopg.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from evals import graph_walks as gw
from scripts import eval_generative as eg
from scripts import eval_graph_walks as egw

STATEMENT_TIMEOUT_MS = 120_000
_NAMED_TYPES_MAX = egw.Store.MAX_NAMED


def connect(dsn: str, statement_timeout_ms: int = STATEMENT_TIMEOUT_MS) -> Any:
    """A read-only autocommit session on the ontology (writes refused by the server)."""
    import psycopg
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10, options=(
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={int(statement_timeout_ms)} -c search_path=ontology"))
    if conn.execute("SHOW default_transaction_read_only").fetchone()[0] != "on":
        conn.close()
        raise eg.Refused("postgres_session_not_read_only")
    return conn


def current(alias: str, as_of: str | None) -> str:
    """The predicate of a row of ``alias`` current at ``as_of`` (now: not superseded)."""
    if as_of is None:
        return f"{alias}.superseded_at IS NULL"
    return (f"{alias}.recorded_at <= %(as_of)s AND ({alias}.superseded_at IS NULL OR "
            f"{alias}.superseded_at > %(as_of)s)")


class PgGenStore(eg.GenStore):
    """``GenStore`` over Postgres at ``as_of`` (module docstring)."""

    def __init__(self, conn: Any, samples: Mapping[str, Sequence[Sequence[str]]],
                 guard: Any, own: frozenset[str], skipped_boxes: Iterable[str],
                 as_of: str | None = None) -> None:
        # egw.Store's state, with a Postgres session in place of the SQLite file
        self.conn = conn
        self.samples = {t: [tuple(x) for x in rows] for t, rows in samples.items()}
        self.guard, self.own = guard, own
        self.skipped_boxes = {b.casefold() for b in skipped_boxes if b}
        self._names: dict[str, dict[str, list[str]] | None] = {}
        self._cleared: dict[str, bool] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self._by_prop: dict[tuple[str, str], dict[str, list[str]] | None] = {}
        self.as_of = as_of

    def _q(self, sql: str, **params: Any) -> list[tuple]:
        return self.conn.execute(sql, {"as_of": self.as_of, **params}).fetchall()

    def _known(self) -> str:
        return "" if self.as_of is None else " AND first_recorded_at <= %(as_of)s"

    # ---- picks
    def count(self, object_type: str, binding: str = "") -> int:
        key = (object_type, binding)
        if key not in self._counts:
            lo, hi = self._range(object_type, binding)
            self._counts[key] = self._q(
                "SELECT count(*) FROM objects WHERE object_id >= %(lo)s AND object_id < %(hi)s"
                + self._known(), lo=lo, hi=hi)[0][0]
        return self._counts[key]

    def objects(self, object_type: str, k: int, rng: Any, binding: str = "") -> list[str]:
        from evals import generative as gen
        n = self.count(object_type, binding)
        if not n:
            return []
        lo, hi = self._range(object_type, binding)
        out: list[str] = []
        for offset in gen.draw_offsets(n, k, rng):
            rows = self._q("SELECT object_id FROM objects WHERE object_id >= %(lo)s AND "
                           "object_id < %(hi)s" + self._known() + " ORDER BY object_id "
                           "LIMIT 1 OFFSET %(off)s", lo=lo, hi=hi, off=offset)
            if rows and rows[0][0] not in out:
                out.append(rows[0][0])
        return out

    def all_of(self, object_type: str, binding: str) -> list[str]:
        lo, hi = self._range(object_type, binding)
        return [r[0] for r in self._q(
            "SELECT object_id FROM objects WHERE object_id >= %(lo)s AND object_id < %(hi)s"
            + self._known() + " ORDER BY object_id", lo=lo, hi=hi)]

    # ---- a record's rows
    def rows(self, ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for chunk in self._chunks(ids, 400):
            for oid, prop, vtype, value, unit, currency, marks in self._q(
                    "SELECT o.object_id, f.property, k.value_type, c.value, k.unit, c.currency, "
                    "k.markings FROM object_versions o JOIN value_sources s ON s.ver = o.ver "
                    "JOIN value_cells c ON c.src = s.src JOIN value_fields f ON f.field = "
                    "c.field JOIN value_kinds k ON k.kind = c.kind WHERE o.object_id = "
                    "ANY(%(ids)s) AND " + current("s", self.as_of), ids=list(chunk)):
                out.setdefault(oid, []).append(
                    {"property": prop, "value_type": vtype, "value": value, "unit": unit,
                     "currency": currency, "markings": marks})
        return out

    def versions(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        out = {}
        for chunk in self._chunks(ids):
            for oid, handle, marks in self._q(
                    "SELECT o.object_id, o.l1_handle, o.markings FROM object_versions o WHERE "
                    "o.object_id = ANY(%(ids)s) AND " + current("o", self.as_of),
                    ids=list(chunk)):
                out[oid] = (handle, marks)
        return out

    def step(self, ids: Sequence[str], link_type: str, direction: str
             ) -> list[tuple[str, str]]:
        near, far = ("from_object", "to_object") if direction == "out" else \
            ("to_object", "from_object")
        out: list[tuple[str, str]] = []
        for chunk in self._chunks(ids):
            out += self._q(
                f"SELECT l.{near}, l.{far} FROM link_rows l WHERE l.{near} = ANY(%(ids)s) AND "
                "l.link_type = %(lt)s AND l.tier = ANY(%(firm)s) AND "
                + current("l", self.as_of), ids=list(chunk), lt=link_type,
                firm=list(egw.FIRM))
        return [(a, b) for a, b in out if not gw.mirror_type(gw.object_type(b))]

    # ---- name indexes
    def _index(self, object_type: str, props: Sequence[str]) -> dict[str, list[str]] | None:
        if self.count(object_type) > _NAMED_TYPES_MAX:
            return None
        lo, hi = self._range(object_type, "")
        index: dict[str, list[str]] = {}
        for oid, value in self._q(
                "SELECT o.object_id, c.value FROM object_versions o JOIN value_sources s ON "
                "s.ver = o.ver JOIN value_cells c ON c.src = s.src WHERE o.object_id >= %(lo)s "
                "AND o.object_id < %(hi)s AND c.field = ANY(ARRAY(SELECT field FROM "
                "value_fields WHERE property = ANY(%(props)s))) AND "
                + current("s", self.as_of), lo=lo, hi=hi, props=list(props)):
            text = " ".join(str(value or "").split()).casefold()
            if text:
                index.setdefault(text, []).append(oid)
        return index

    def lookup(self, object_type: str, label: str) -> list[str] | None:
        if object_type not in self._names:
            self._names[object_type] = self._index(object_type, gw.NAME_PROPS)
            while len(self._names) > self.MAX_INDEXES:
                self._names.pop(next(iter(self._names)))
        index = self._names[object_type]
        if index is None:
            return None
        return sorted(set(index.get(" ".join(label.split()).casefold(), [])))

    def lookup_prop(self, object_type: str, prop: str, value: str) -> list[str] | None:
        key = (object_type, prop)
        if key not in self._by_prop:
            self._by_prop[key] = self._index(object_type, [prop])
            while len(self._by_prop) > self.MAX_INDEXES:
                self._by_prop.pop(next(iter(self._by_prop)))
        index = self._by_prop[key]
        if index is None:
            return None
        return sorted(set(index.get(" ".join(value.split()).casefold(), [])))

    # ---- what changed after a time (the truth pass's explanation of a changed fact)
    def changed_after(self, since: str | Mapping[str, str], handles: Sequence[str],
                      default: str | None = None) -> dict[str, str]:
        """handle -> why its record differs from the base store (``since``: the time the
        base store had seen records up to, one for all or per object type, ``default``
        for a type not named): ``new_version`` (a version or value recorded or superseded
        after it), ``new_link`` (a firm link at the record recorded or superseded after
        it), ``version_gone`` (the cited version is no longer held); when none of these,
        ``new_record_of_type`` (a record of a cited record's type first recorded after
        it). Handles of records unchanged since are left out."""
        def seen(oid: str) -> str:
            if isinstance(since, str):
                return since
            return since.get(gw.object_type(oid)) or default or min(since.values())

        out: dict[str, str] = {}
        want = sorted(set(handles))
        held: dict[str, str] = {}
        for chunk in self._chunks(want):
            for handle, oid in self._q("SELECT l1_handle, object_id FROM object_versions "
                                       "WHERE l1_handle = ANY(%(h)s)", h=list(chunk)):
                held[handle] = oid
        for handle in want:
            if handle not in held:
                out[handle] = "version_gone"
        ids = sorted(set(held.values()))
        latest: dict[str, tuple[str, str]] = {}

        def note(rows: Iterable[tuple], why: str) -> None:
            for oid, recorded, superseded in rows:
                last = max(recorded or "", superseded or "")
                if last > seen(oid) and oid not in latest:
                    latest[oid] = (last, why)

        for chunk in self._chunks(ids):
            note(self._q("SELECT object_id, max(recorded_at), max(superseded_at) FROM "
                         "object_versions WHERE object_id = ANY(%(ids)s) GROUP BY object_id",
                         ids=list(chunk)), "new_version")
            note(self._q("SELECT o.object_id, max(s.recorded_at), max(s.superseded_at) FROM "
                         "object_versions o JOIN value_sources s ON s.ver = o.ver WHERE "
                         "o.object_id = ANY(%(ids)s) GROUP BY o.object_id", ids=list(chunk)),
                 "new_version")
            for end in ("from_object", "to_object"):
                note(self._q(f"SELECT l.{end}, max(l.recorded_at), max(l.superseded_at) FROM "
                             f"link_rows l WHERE l.{end} = ANY(%(ids)s) AND l.tier = "
                             f"ANY(%(firm)s) GROUP BY l.{end}", ids=list(chunk),
                             firm=list(egw.FIRM)), "new_link")
        for handle, oid in held.items():
            if oid in latest:
                out[handle] = latest[oid][1]
        if not out:
            # nothing it cites changed: a record of the same type recorded since (one more
            # with the same name, one more in a count) is the remaining newer version
            for object_type in sorted({gw.object_type(o) for o in held.values()}):
                lo, hi = self._range(object_type, "")
                if self._q("SELECT 1 FROM objects WHERE object_id >= %(lo)s AND object_id < "
                           "%(hi)s AND first_recorded_at > %(since)s LIMIT 1", lo=lo, hi=hi,
                           since=seen(object_type + ":")):
                    out.update({h: "new_record_of_type" for h, o in held.items()
                                if gw.object_type(o) == object_type})
        return out
