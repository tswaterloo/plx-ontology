"""``python3 -m operating.embed records --state D --type T [--type T ...] [--dry]``: index the
served records of some types that the search index holds no record text for.

Why: with Postgres the only store (``PLX_ONTOLOGY_PG=only``) no generation pass runs again
(``follow`` reports ``generation_retired``), and a delta layer chunks only the objects it
changed. A record text rule or kind that lands later (SharePoint list items' ``list_item``
chunks, line descriptions, company names, a new type's text) so never reaches a record no
layer has touched since. This pass reads those records from the Postgres ontology and
chunks them by the same rule (``operating.chunking.records.RecordChunker``: markings,
audience units and the people exclusion as a generation pass applies them).

What it writes: only the chunks of objects that hold no record-text row yet (any kind
``RECORD_KINDS`` names); an object that holds one is left to the layer that wrote it. It
deletes nothing but the rows of a unit the people exclusion withholds (as every pass does).
Slices of ``SLICE`` objects, each under the follower's lock (``search/follow.json.lock``),
so a layer run never writes beside it. Refused (exit 2): the exclusion unavailable, a type
the rule indexes no text of, another follower holding the lock past ``--wait-seconds``.
Prints one JSON report (counts only, never text)."""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from operating.chunking.permissions import unit_row
from operating.chunking.projection import Counts

from . import config
from . import store as st
from .shards import WRITE_BATCH, _exclusion

# what ``RecordChunker`` writes (``records.record_kind``, ``records.WORDS_KIND``)
RECORD_KINDS = ("portal_text", "bc_text", "record_text", "mc_task", "list_item",
                "record_words")
SLICE = 2000
LOCK = Path("search") / "follow.json.lock"


def record_pass(store: Any, conn: Any, exclusion: Any, types: Sequence[str], *,
                dry: bool = False, lock: Any = None, chunker: Any = None) -> dict[str, Any]:
    """Chunk and (unless ``dry``) write the records of ``types`` read from ``store`` (a
    SQLite-dialect connection) that hold no record-text row in ``conn``'s index. ``lock()``:
    a context manager yielding whether this run may write a slice now."""
    from operating.chunking.records import RecordChunker
    if exclusion.unavailable:
        return {"refused": f"people_exclusion_unavailable:{exclusion.unavailable}"}
    counts = Counts()
    chunker = chunker or RecordChunker(store, exclusion, counts)
    asked = sorted(set(types))
    unknown = [t for t in asked if t not in chunker.plan]
    if unknown or not asked:
        return {"refused": "type_not_indexed:" + ",".join(unknown or ["<none>"])}
    whole = dict(chunker.plan)
    started = time.monotonic()
    writer = None if dry else st.ChunkWriter(conn, unit_of=lambda key: unit_row(key, exclusion))
    report: dict[str, Any] = {"dry": dry, "types": {}}
    for object_type in asked:
        ids = [r[0] for r in store.execute(
            "SELECT object_id FROM objects WHERE object_type = ? ORDER BY object_id",
            (object_type,)).fetchall()]
        done = {"objects": len(ids), "already_indexed": 0, "chunked_objects": 0,
                "chunks": 0, "inserted": 0, "updated": 0, "unchanged": 0}
        for start in range(0, len(ids), SLICE):
            part = ids[start:start + SLICE]
            with (lock() if lock is not None else contextlib.nullcontext(True)) as mine:
                if not mine:
                    report["refused"] = "another_follow_running"
                    report["types"][object_type] = done
                    return report
                indexed = {r[0] for r in conn.execute(
                    "SELECT DISTINCT object_id FROM search.chunks WHERE object_id = ANY(%s) "
                    "AND kind = ANY(%s)", (part, list(RECORD_KINDS))).fetchall()}
                conn.commit()
                done["already_indexed"] += len(indexed)
                todo = frozenset(part) - indexed
                if not todo:
                    continue
                chunker.plan = {object_type: whole[object_type]}   # this type's records only
                made, objects = [], set()
                for chunk in chunker.record_chunks(None, objects=todo):
                    if chunk.object_id not in todo:
                        continue
                    made.append(chunk)
                    objects.add(chunk.object_id)
                done["chunks"] += len(made)
                done["chunked_objects"] += len(objects)
                if writer is None:
                    continue
                before = (writer.inserted, writer.updated, writer.unchanged)
                for at in range(0, len(made), WRITE_BATCH):
                    writer.write(made[at:at + WRITE_BATCH])
                done["inserted"] += writer.inserted - before[0]
                done["updated"] += writer.updated - before[1]
                done["unchanged"] += writer.unchanged - before[2]
        report["types"][object_type] = done
    if writer is not None:
        report["withheld_units_deleted"] = writer.delete_units(counts.withheld)
    report["skipped"] = dict(counts.as_dict().get("skipped") or {})
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.embed records",
                                description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True)
    p.add_argument("--type", action="append", required=True, dest="types")
    p.add_argument("--dry", action="store_true", help="chunk and count, write nothing")
    p.add_argument("--wait-seconds", type=float, default=600.0,
                   help="how long a slice waits for a running follower")
    args = p.parse_args(argv)
    from operating.pg import ontology_reader
    from operating.pg.sqlite_compat import PgCompatConnection
    from .backfill import waited
    state = Path(args.state)
    values = config.settings()
    exclusion = _exclusion(state, values)
    store = PgCompatConnection(ontology_reader.dsn(), statement_timeout_ms=0)
    try:
        conn = st.connect(values.get("PLX_PG_DSN") or config.DEFAULT_DSN)
    except Exception:
        store.close()
        raise
    try:
        st.require_schema(conn)
        report = record_pass(store, conn, exclusion, args.types, dry=args.dry,
                             lock=lambda: waited(state / LOCK, args.wait_seconds))
    except st.SchemaMissing as missing:
        report = {"refused": str(missing)}
    finally:
        conn.close()
        store.close()
    print(json.dumps(report, indent=1, sort_keys=True))
    return 2 if report.get("refused") else 0


if __name__ == "__main__":
    sys.exit(main())
