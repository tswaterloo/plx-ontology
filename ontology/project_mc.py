"""The Mission Control capture as a projection source (source family ``mission_control``).

``McCaptures`` reads the capture store ``integrations.mc_sources`` writes (MC tasks,
approval gates, task events, buckets and knowledge hits) the way
``ontology.project_bc.BcCaptures`` reads the BC capture: the file directly, read-only, in
one read transaction, after checking the tables and columns it reads
(``mc_capture_layout_unsupported``).

Relation = collection (``tasks``, ``approvals``, ``task_events``, ``buckets``,
``knowledge``). One record per retained version of every record of that
collection in every captured operator scope: ``record_key`` ``<scope>/<id>``, ``l1_handle``
the capture handle ``mc://<scope>/<collection>/<id>@<generation>-<capture-id>`` (it
resolves offline to exactly this version), ``row`` the captured record, ``audience_key``
``integrations.mc_sources.audience_key`` (``mc:<scope>/<collection>``, plus ``/<project>``
for the collections MC scopes per project, so a reader needs that project's grant too)
and the store's version times. A version without a valid project key (captured before
per-project keys) is not projected, nor is any unit of a collection outside
``COLLECTIONS`` (``brain_items``, retired for ``integrations.knowledge_source``:
devloop/DECISIONS/2026-10-08-gov13-one-brain-capture.md).

Completeness is explicit, as for BC: :meth:`capture_gap` is ``source_not_captured`` when
no scope's unit of a collection was ever captured and ``source_capture_incomplete`` when
some unit's latest run did not finish a full read (``knowledge`` always: a ranked search
is never an enumeration); :meth:`describe` gives ``last_full_traversal_at`` only when
every scope's unit is complete.

:meth:`documents` gives tasks (title, description), approval gates (reason) and knowledge
entries (title, snippet or summary) as ``ontology.mentions.Document`` values for the
mention links written into the same store generation (``ontology/schema.py``
``MC_MENTION_LINK_RULES``). Their authors are MC users and agents, not verified senders:
every mention is an external assertion and a lead.

Stdlib only; never a source read (the capture is L1).
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote

from integrations.capture_store import FORMAT, REMOVALS_COLUMN, make_handle, removals_of
from integrations.mc_sources import COLLECTIONS, KIND, MC_KIND, audience_key
from ontology.project import CapturedRecord, ProjectionError, _Relations

SOURCE = KIND
NOT_CAPTURED = "source_not_captured"
INCOMPLETE = "source_capture_incomplete"
# Text a mention is read from, per collection: (document kind, row fields).
_DOCUMENT_FIELDS = {"tasks": ("mc_task", ("title", "description")),
                    "approvals": ("mc_approval", ("reason",)),
                    "knowledge": ("knowledge_entry", ("title", "snippet"))}


class McCaptures:
    """Every retained version of an MC capture store file, read-only."""

    source = SOURCE
    _NEEDED = {
        "meta": {"key", "value"},
        "runs": {"run_id", "kind", "unit", "state", "finished_at", "updated_at"},
        "versions": {"capture_id", "kind", "path", "unit", "digest", "lineage", "payload",
                     "recorded_at", "superseded_at", "last_seen_at",
                     "missing_from_source_since"},
        "removals": {"kind", "path", "since", "until"},
    }

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        if not self.path.is_file():
            raise ProjectionError("mc_capture_missing", str(self.path))
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._snapshot = False
        try:
            for table, columns in self._NEEDED.items():
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if not columns <= have:
                    raise ProjectionError("mc_capture_layout_unsupported",
                                          f"{table} lacks {sorted(columns - have)}")
            meta = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("format") != FORMAT or not meta.get("generation"):
                raise ProjectionError("mc_capture_layout_unsupported",
                                      "not a plx-capture-store/v1 file")
        except BaseException:
            self._conn.close()
            raise
        self.generation = meta["generation"]
        self._units = self._unit_states()

    def _end_snapshot(self) -> None:
        if self._snapshot:
            self._snapshot = False
            self._conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self._end_snapshot()
        finally:
            self._conn.close()

    def __enter__(self) -> "McCaptures":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- completeness ----------------------------------------------------------

    def _unit_states(self) -> dict[str, dict[str, dict[str, Any]]]:
        """``{collection: {scope: state}}`` from each unit's latest run."""
        out: dict[str, dict[str, dict[str, Any]]] = {}
        latest = self._conn.execute(
            "SELECT r.* FROM runs r JOIN (SELECT unit, MAX(run_id) AS run_id FROM runs "
            "WHERE kind = ? GROUP BY unit) l ON l.run_id = r.run_id ORDER BY r.unit",
            (SOURCE,)).fetchall()
        for run in latest:
            scope, _, collection = str(run["unit"]).partition(":")[2].partition("/")
            if collection not in COLLECTIONS:
                continue
            full = self._conn.execute(
                "SELECT MAX(finished_at) FROM runs WHERE kind = ? AND unit = ? "
                "AND state = 'complete'", (SOURCE, run["unit"])).fetchone()[0]
            settled = run["state"] == "complete"
            out.setdefault(collection, {})[scope] = {
                "settled": settled, "full_at": full if settled else None,
                "refreshed_at": run["updated_at"]}
        return out

    def capture_gap(self, relation: str) -> str | None:
        units = self._units.get(relation)
        if not units:
            return NOT_CAPTURED
        return None if all(u["settled"] for u in units.values()) else INCOMPLETE

    def describe(self) -> dict[str, Any]:
        """Freshness facts (no business data) per captured collection."""
        relations: dict[str, dict[str, Any]] = {}
        for collection, units in sorted(self._units.items()):
            last = self._conn.execute(
                "SELECT MAX(capture_id) FROM versions WHERE kind = ? AND unit IN "
                "(SELECT value FROM json_each(?))",
                (SOURCE, json.dumps([f"mc:{s}/{collection}" for s in sorted(units)]))
            ).fetchone()[0]
            relations[collection] = {
                "last_refresh_at": min(u["refreshed_at"] for u in units.values()),
                "last_capture_id": last,
                "last_full_traversal_at": (min(u["full_at"] for u in units.values())
                                           if self.capture_gap(collection) is None else None)}
        return {"generation": f"mc-capture/{self.generation}", "relations": relations}

    # -- records ---------------------------------------------------------------

    def _versions(self, collection: str
                  ) -> Iterator[tuple[tuple[str, ...], sqlite3.Row, dict[str, Any], str]]:
        """(path, row, payload, audience key) of every version with a valid audience key."""
        for scope in sorted(self._units.get(collection, ())):
            for row in self._conn.execute(
                    f"SELECT *, {REMOVALS_COLUMN} FROM versions WHERE kind = ? AND unit = ? "
                    "ORDER BY path, recorded_at, capture_id",
                    (SOURCE, f"mc:{scope}/{collection}")):
                path = MC_KIND.canonical_path([unquote(p) for p in str(row["path"]).split("/")])
                payload = json.loads(row["payload"])
                try:
                    key = audience_key(path, payload)
                except ValueError:
                    continue  # no project key: never under the collection's audience alone
                yield path, row, payload, key

    def records(self, relations: Sequence[str]) -> Mapping[str, Iterable[CapturedRecord]]:
        self._end_snapshot()
        self._conn.execute("BEGIN")  # one snapshot for every relation
        self._snapshot = True

        def stream(relation: str) -> Iterator[CapturedRecord]:
            if not self._snapshot:
                raise ProjectionError("capture_snapshot_closed", relation)
            if relation not in COLLECTIONS:
                return
            for path, row, payload, key in self._versions(relation):
                yield CapturedRecord(
                    source=SOURCE, relation=relation, record_key=f"{path[0]}/{path[2]}",
                    row=payload["row"],
                    l1_handle=make_handle(MC_KIND, path, self.generation, row["capture_id"]),
                    field_prefix="row", digest=row["digest"], recorded_at=row["recorded_at"],
                    superseded_at=row["superseded_at"], last_seen_at=row["last_seen_at"],
                    audience_key=key,
                    missing_from_source_since=row["missing_from_source_since"],
                    removals=removals_of(row),
                    capture_lineage=row["lineage"])

        return _Relations(relations, stream)

    def documents(self) -> Iterator[Any]:
        """Every captured task, gate and knowledge version as an ``ontology.mentions.
        Document`` (author unverified: sender class ``unknown``)."""
        from ontology.mentions import Document, TextField

        for collection, (kind, names) in _DOCUMENT_FIELDS.items():
            for path, row, payload, _key in self._versions(collection):
                handle = make_handle(MC_KIND, path, self.generation, row["capture_id"])
                record = payload["row"]
                fields = tuple(TextField(handle, f"row.{name}", record[name]) for name in names
                               if isinstance(record.get(name), str) and record[name].strip())
                yield Document(handle, kind, fields, sender_class="unknown",
                               digest=row["digest"], recorded_at=row["recorded_at"],
                               superseded_at=row["superseded_at"],
                               date=record.get("updatedAt") or record.get("requestedAt"))


__all__ = ["INCOMPLETE", "McCaptures", "NOT_CAPTURED"]
