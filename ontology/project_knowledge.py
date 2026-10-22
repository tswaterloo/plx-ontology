"""The brain knowledge capture as a projection source (source family ``brain``, GOV-13).

``KnowledgeCaptures`` reads the versioned knowledge capture
(``integrations.knowledge_source``, kind ``brain`` of ``integrations.capture_store``) the
way ``ontology.project_bc.BcCaptures`` reads the BC capture: the file directly, read-only,
in one read transaction, after checking the tables and columns it reads. Relation
``items``: one record per retained version of every captured knowledge item, keyed by the
brain item id, cited by its capture handle ``brain://<namespaceKey>/<id>@<generation>-
<capture id>`` (resolves offline to exactly this version), with audience key
``brain:<namespaceKey>``: the unit ``NamespaceAuthority.grants`` grants per reader, so a
reader sees only the namespaces their entitlement names.

``capture_gap``: ``source_not_captured`` when no namespace was ever captured,
``source_capture_incomplete`` when a namespace's latest run did not reach the brain's
listing total. Stdlib only; never a source read (the capture is L1).
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote

from integrations.capture_store import FORMAT, REMOVALS_COLUMN, make_handle, removals_of
from integrations.knowledge_source import BRAIN_KIND, SOURCE
from ontology.project import CapturedRecord, ProjectionError, _Relations
from ontology.project_bc import INCOMPLETE, NOT_CAPTURED

RELATION = "items"


class KnowledgeCaptures:
    """Every retained version of a knowledge capture store file
    (``integrations.knowledge_source.store_path``), read-only."""

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
            raise ProjectionError("knowledge_capture_missing", str(self.path))
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._snapshot = False
        try:
            for table, columns in self._NEEDED.items():
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if not columns <= have:
                    raise ProjectionError("knowledge_capture_layout_unsupported",
                                          f"{table} lacks {sorted(columns - have)}")
            meta = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("format") != FORMAT or not meta.get("generation"):
                raise ProjectionError("knowledge_capture_layout_unsupported",
                                      "not a plx-capture-store/v1 file")
        except BaseException:
            self._conn.close()
            raise
        self.generation = meta["generation"]
        self._runs = self._conn.execute(
            "SELECT r.* FROM runs r JOIN (SELECT unit, MAX(run_id) AS run_id FROM runs "
            "WHERE kind = ? GROUP BY unit) l ON l.run_id = r.run_id ORDER BY r.unit",
            (SOURCE,)).fetchall()

    def _end_snapshot(self) -> None:
        if self._snapshot:
            self._snapshot = False
            self._conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self._end_snapshot()
        finally:
            self._conn.close()

    def __enter__(self) -> "KnowledgeCaptures":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def capture_gap(self, relation: str) -> str | None:
        if relation != RELATION or not self._runs:
            return NOT_CAPTURED
        return None if all(r["state"] == "complete" for r in self._runs) else INCOMPLETE

    def describe(self) -> dict[str, Any]:
        """Freshness facts (no knowledge content)."""
        if not self._runs:
            return {"generation": f"knowledge-capture/{self.generation}", "relations": {}}
        last = self._conn.execute("SELECT MAX(capture_id) FROM versions WHERE kind = ?",
                                  (SOURCE,)).fetchone()[0]
        complete = self.capture_gap(RELATION) is None
        return {"generation": f"knowledge-capture/{self.generation}", "relations": {RELATION: {
            "last_refresh_at": min(r["updated_at"] for r in self._runs),
            "last_capture_id": last,
            "last_full_traversal_at": (min(r["finished_at"] for r in self._runs)
                                       if complete else None)}}}

    def records(self, relations: Sequence[str]) -> Mapping[str, Iterable[CapturedRecord]]:
        self._end_snapshot()
        self._conn.execute("BEGIN")  # one snapshot for every relation
        self._snapshot = True

        def stream(relation: str) -> Iterator[CapturedRecord]:
            if not self._snapshot:
                raise ProjectionError("capture_snapshot_closed", relation)
            if relation != RELATION:
                return
            for row in self._conn.execute(
                    f"SELECT *, {REMOVALS_COLUMN} FROM versions "
                    "WHERE kind = ? ORDER BY path, recorded_at, "
                    "capture_id", (SOURCE,)):
                path = BRAIN_KIND.canonical_path([unquote(p) for p in row["path"].split("/")])
                yield CapturedRecord(
                    source=SOURCE, relation=relation, record_key=path[1],
                    row=json.loads(row["payload"])["row"],
                    l1_handle=make_handle(BRAIN_KIND, path, self.generation, row["capture_id"]),
                    field_prefix="row", digest=row["digest"], recorded_at=row["recorded_at"],
                    superseded_at=row["superseded_at"], last_seen_at=row["last_seen_at"],
                    audience_key=row["unit"],
                    missing_from_source_since=row["missing_from_source_since"],
                    removals=removals_of(row),
                    capture_lineage=row["lineage"])

        return _Relations(relations, stream)


__all__ = ["KnowledgeCaptures", "RELATION"]
