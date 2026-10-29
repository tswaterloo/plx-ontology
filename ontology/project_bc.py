"""The Business Central capture store as a projection source (source family ``bc``).

``BcCaptures`` reads the versioned BC capture (``integrations.capture_store``, written by
``integrations.bc_read.BcCapture``) the way :class:`ontology.project.MirrorCaptures` reads
the Portal mirror: the file directly, read-only, in one read transaction, after checking
the tables and columns it reads (``bc_capture_layout_unsupported``). BC is therefore
projected on its own, with or without a Portal mirror: a BC-only build has every Portal
binding reported ``source_not_captured`` (a gap, never an empty source).

Relation = BC entity set (``purchaseOrders``, ``purchaseOrderLines``, ...). One record per
retained version of every record of that set in every captured company:

* ``record_key`` ``<company-id>/<record-id>`` (BC ids are unique only within a company);
* ``l1_handle`` the capture handle ``bc://<environment>/<company-id>/<entitySet>/<id>@
  <generation>-<capture-id>`` (``capture_store.make_handle``), which resolves offline to
  exactly this version (``CaptureStore.resolve``); ``row`` is the captured BC record's
  fields with BC's own API names, so ``row.<field>`` claims check unchanged;
* ``capture_lineage`` the capture's ``bc_<book>`` (the projector's company scope: a link
  between two BC records needs the same book);
* ``audience_key`` the capture unit ``bc:<company-id>/<entitySet>`` (``bc_unit``), the
  key the tools' ``ReadPolicy`` grants per company and entity set;
* ``recorded_at`` / ``superseded_at`` / ``last_seen_at`` / ``missing_from_source_since``
  as the capture store holds them (``superseded_at`` is the capture that replaced it).

Completeness is explicit. :meth:`capture_gap` tells the projector, per relation, what the
capture does not hold: ``source_not_captured`` when no company's unit of the set was ever
captured (the binding is not projected), ``source_capture_incomplete`` when some unit's
latest run did not finish its traversal (``in_progress``, ``partial`` other than a clean
incremental read, ``failed``) or a company captured for other sets has no unit of this
one (what was captured is still projected); :meth:`whole_scopes` names the companies
whose unit of a set is settled, so what stands in for a record of that set is held per
company (``stands_for``). :meth:`describe` gives the same facts as freshness:
``last_full_traversal_at`` only when every company's unit of the set is settled, so a
family with any incomplete or uncaptured bound set is ``partial`` and the tools never
turn a missing BC record into ``absent_in_source``.

A capture unit is one company's whole entity set, never a set filtered to one record, so
the only scoped claim a unit's runs prove is per company: ``describe`` lists, per entity
set, one ``company=<company-id>`` claim per captured company (``complete`` when its unit
is settled, ``read_at`` its latest run's update). ``plx.process`` reads it for a hop from a
record of that company (BC links never cross companies), so one company's failed unit
leaves the other companies' roots decidable; it never makes the family complete.

Stdlib only; never a source read (the capture is L1).
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote

from integrations.capture_store import (
    BC_FIELD_PAGES,
    BC_KIND,
    FORMAT,
    REMOVALS_COLUMN,
    make_handle,
    removals_of,
)
from ontology.project import (CapturedRecord, ProjectionError, RowSample, _Relations,
                              _sampled)

SOURCE = "bc"
NOT_CAPTURED = "source_not_captured"
INCOMPLETE = "source_capture_incomplete"
# A finished delta read (``bc_read.INCREMENTAL``) settles its unit: the unit had a full
# traversal before and the delta read every change since.
_INCREMENTAL = "incremental"


class BcCaptures:
    """Every retained version of a BC capture store file, read-only (``source = "bc"``)."""

    source = SOURCE
    sample: RowSample | None = None   # a sample of its record queries (``RowSample``)
    _NEEDED = {
        "meta": {"key", "value"},
        "runs": {"run_id", "kind", "unit", "state", "reason", "started_at", "finished_at",
                 "updated_at"},
        "versions": {"capture_id", "kind", "path", "unit", "digest", "lineage", "payload",
                     "run_id",
                     "recorded_at", "superseded_at", "last_seen_at",
                     "missing_from_source_since"},
        "removals": {"kind", "path", "since", "until"},
    }

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        if not self.path.is_file():
            raise ProjectionError("bc_capture_missing", str(self.path))
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._snapshot = False
        try:
            for table, columns in self._NEEDED.items():
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if not columns <= have:
                    raise ProjectionError("bc_capture_layout_unsupported",
                                          f"{table} lacks {sorted(columns - have)}")
            meta = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("format") != FORMAT or not meta.get("generation"):
                raise ProjectionError("bc_capture_layout_unsupported",
                                      "not a plx-capture-store/v1 file")
        except BaseException:
            self._conn.close()
            raise
        self.generation = meta["generation"]
        self._units = self._unit_states()
        self._passes = self._pass_starts()

    def _end_snapshot(self) -> None:
        if self._snapshot:
            self._snapshot = False
            self._conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self._end_snapshot()
        finally:
            self._conn.close()

    def __enter__(self) -> "BcCaptures":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- completeness ----------------------------------------------------------

    def _unit_states(self) -> dict[str, dict[str, dict[str, Any]]]:
        """``{entity set: {company id: unit state}}`` from each unit's latest run:
        ``settled`` (a complete traversal, or a clean delta read after one), the time of
        its last full traversal, and the last refresh (the latest run's update)."""
        out: dict[str, dict[str, dict[str, Any]]] = {}
        latest = self._conn.execute(
            "SELECT r.* FROM runs r JOIN (SELECT unit, MAX(run_id) AS run_id FROM runs "
            "WHERE kind = ? GROUP BY unit) l ON l.run_id = r.run_id ORDER BY r.unit",
            (SOURCE,)).fetchall()
        for run in latest:
            company, _, entity_set = str(run["unit"]).partition(":")[2].partition("/")
            full = self._conn.execute(
                "SELECT MAX(finished_at) FROM runs WHERE kind = ? AND unit = ? "
                "AND state = 'complete'", (SOURCE, run["unit"])).fetchone()[0]
            settled = run["state"] == "complete" or (
                run["state"] == "partial" and run["reason"] == _INCREMENTAL and full)
            out.setdefault(entity_set, {})[company] = {
                "settled": bool(settled), "full_at": full if settled else None,
                "refreshed_at": run["updated_at"]}
        return out

    def _pass_starts(self) -> dict[int, str]:
        """run id -> start of the capture pass it belongs to. The store records runs per
        unit, not passes, so a pass is derived per company: its runs in run order, a new
        pass beginning at a run of a unit the current pass already ran (one capture call
        runs each unit of a company once; a resumed run keeps its id), started at its
        first run's ``started_at``."""
        out: dict[int, str] = {}
        passes: dict[str, tuple[str, set[str]]] = {}
        for run in self._conn.execute("SELECT run_id, unit, started_at FROM runs "
                                      "WHERE kind = ? ORDER BY run_id", (SOURCE,)):
            company = str(run["unit"]).partition(":")[2].partition("/")[0]
            start, seen = passes.get(company, (run["started_at"], set()))
            if run["unit"] in seen:
                start, seen = run["started_at"], set()
            seen.add(run["unit"])
            passes[company] = (start, seen)
            out[run["run_id"]] = start
        return out

    def _companies(self) -> set[str]:
        return {company for units in self._units.values() for company in units}

    def capture_gap(self, relation: str) -> str | None:
        """What the capture does not hold of one entity set: ``source_not_captured`` (no
        unit of it), ``source_capture_incomplete`` (a unit not settled, or a captured
        company without a unit of it), else None."""
        units = self._units.get(relation)
        if not units:
            return NOT_CAPTURED
        if set(units) != self._companies() or not all(u["settled"] for u in units.values()):
            return INCOMPLETE
        return None

    def whole_scopes(self, relation: str) -> frozenset[str]:
        """The companies (``scope_of``) whose unit of one entity set is settled: where the
        capture holds that set whole, whatever another company's unit is."""
        return frozenset(company for company, unit in self._units.get(relation, {}).items()
                         if unit["settled"])

    @staticmethod
    def scope_of(record: CapturedRecord) -> str:
        """The company a record of this capture was read in (its ``record_key`` prefix)."""
        return record.record_key.partition("/")[0]

    def describe(self) -> dict[str, Any]:
        """Freshness facts (no business data) per captured entity set: the stalest unit
        refresh, the newest capture id, the oldest last full traversal only when the
        set is complete in every captured company (else null), and the per-company
        claims (``scopes``, module docstring)."""
        relations: dict[str, dict[str, Any]] = {}
        for entity_set, units in sorted(self._units.items()):
            last = self._conn.execute(
                "SELECT MAX(capture_id) FROM versions WHERE kind = ? AND unit IN "
                "(SELECT value FROM json_each(?))",
                (SOURCE, json.dumps([f"bc:{c}/{entity_set}" for c in sorted(units)])
                 )).fetchone()[0]
            relations[entity_set] = {
                "last_refresh_at": min(u["refreshed_at"] for u in units.values()),
                "last_capture_id": last,
                "last_full_traversal_at": (min(u["full_at"] for u in units.values())
                                           if self.capture_gap(entity_set) is None else None),
                "scopes": [{"scope": f"company={company}", "company_id": company,
                            "complete": unit["settled"], "read_at": unit["refreshed_at"]}
                           for company, unit in sorted(units.items())]}
        return {"generation": f"bc-capture/{self.generation}", "relations": relations}

    # -- records ---------------------------------------------------------------

    def _record(self, relation: str, row: sqlite3.Row) -> CapturedRecord:
        path = BC_KIND.canonical_path([unquote(p) for p in str(row["path"]).split("/")])
        payload = json.loads(row["payload"])
        return CapturedRecord(
            source=SOURCE, relation=relation, record_key=f"{path[1]}/{path[3]}",
            row=payload["row"], l1_handle=make_handle(BC_KIND, path, self.generation,
                                                      row["capture_id"]),
            field_prefix="row", digest=row["digest"], recorded_at=row["recorded_at"],
            superseded_at=row["superseded_at"], last_seen_at=row["last_seen_at"],
            audience_key=row["unit"],
            missing_from_source_since=row["missing_from_source_since"],
            removals=removals_of(row),
            capture_lineage=row["lineage"], pass_started_at=self._passes.get(row["run_id"]))

    def records(self, relations: Sequence[str]) -> Mapping[str, Iterable[CapturedRecord]]:
        self._end_snapshot()
        self._conn.execute("BEGIN")  # one snapshot for every relation
        self._snapshot = True

        def stream(relation: str) -> Iterator[CapturedRecord]:
            if not self._snapshot:
                raise ProjectionError("capture_snapshot_closed", relation)
            units = [f"bc:{company}/{relation}"
                     for company in sorted(self._units.get(relation, ()))]
            for unit in units:
                for row in _sampled(self.sample, self._conn.execute(
                        f"SELECT *, {REMOVALS_COLUMN} FROM versions "
                        "WHERE kind = ? AND unit = ? "
                        "ORDER BY path, recorded_at, capture_id", (SOURCE, unit))):
                    yield self._record(relation, row)

        return _Relations(relations, stream)

    def field_page_records(self, relation: str, as_known_at: str | None = None
                           ) -> list[CapturedRecord]:
        """The versions of one field-page relation (``capture_store.BC_FIELD_PAGES``, e.g.
        ``purchaseOrderReferences``) current at ``as_known_at`` (store instant text, as
        ``Projector.cut``; None: now), in every company: recorded by then, not superseded
        and not missing from the source by then (instants compared as the projector does,
        ``ontology.store.instant``). A field page is no projected binding: a
        ``post_project`` step reads it (``ontology.identity_scoring.your_reference_step``).
        Never opens or ends the projection's snapshot."""
        from ontology.store import instant
        if relation not in BC_FIELD_PAGES:
            raise ProjectionError("bc_field_page_unknown", relation)
        cut = instant(as_known_at) if as_known_at is not None else None
        out = []
        for company in sorted(self._units.get(relation, ())):
            for row in self._conn.execute(
                    f"SELECT *, {REMOVALS_COLUMN} FROM versions WHERE kind = ? AND unit = ? "
                    "ORDER BY path, recorded_at, capture_id",
                    (SOURCE, f"bc:{company}/{relation}")):
                ended = [instant(row[c]) for c in ("superseded_at", "missing_from_source_since")
                         if row[c] is not None]
                if cut is None:
                    current = not ended
                else:
                    current = instant(row["recorded_at"]) <= cut and all(e > cut for e in ended)
                if current:
                    out.append(self._record(relation, row))
        return out

__all__ = ["BcCaptures", "INCOMPLETE", "NOT_CAPTURED"]
