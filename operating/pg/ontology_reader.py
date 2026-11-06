"""The read tools' store on Postgres: ``StoreReader`` over the ``ontology`` schema.

``PLX_ONTOLOGY_PG=1`` makes a case host (``operating.ontology_host.CaseHostTools``) read
``plx.find``, ``plx.object``, ``plx.around``, ``plx.timeline``, ``plx.read``'s store reads,
``plx.query`` (which composes those), the rest of the tool table and the proactive
engine's store reads (``operating.proactive_engine._connect``) from Postgres
(``PLX_PG_DSN``) instead of the served SQLite generation and its row-delta overlay: the
Postgres ontology takes every delta layer in place (``operating.pg.ontology_delta``), so
it is the current store and no overlay applies.

:class:`PgStoreReader` is ``StoreReader`` with its connection a
``sqlite_compat.PgCompatConnection``: every query, with the reader's permission
predicates (audience keys, marking sets, value markings, the as-known-at time, the item
gate) in it, runs on Postgres as written. The word index (``value_text``, ``chunk_text``:
SQLite FTS5) has no Postgres counterpart yet: word matches are read from the SQLite
generation named by ``text_store`` (the last one, kept read-only), else none match.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Sequence

from operating.ontology_tools import StoreReader
from operating.pg.sqlite_compat import PgCompatConnection

ENV = "PLX_ONTOLOGY_PG"


def enabled(env: Any = None) -> bool:
    return (os.environ if env is None else env).get(ENV) == "1"


def dsn() -> str:
    from operating.pg import config
    return config.dsn()


def served_digest(store_path: str | Path) -> str | None:
    """The content digest of the SQLite generation served at ``store_path``."""
    try:
        conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM store_meta WHERE key = 'content_digest'"
                               ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def serves(store_path: str | Path, target: str | None = None) -> bool:
    """Whether a host serving the generation at ``store_path`` reads Postgres: the switch
    is on and Postgres holds that very generation whole (``ready``). Between a new
    generation's publish and its swap into Postgres (``ontology_swap``) the host reads
    the SQLite generation and its overlay, so no reader sees an older generation than
    the one served."""
    if not enabled():
        return False
    digest = served_digest(store_path)
    return digest is not None and ready(target, digest=digest)


def ready(target: str | None = None, *, digest: str | None = None) -> bool:
    """Whether the Postgres ontology holds a whole generation (every table of
    ``ontology_load.ORDER`` recorded loaded): a load in progress (``--replace`` empties
    the tables first) or an unreachable database is not read; the host serves the SQLite
    generation instead."""
    if os.environ.get("PLX_ONTOLOGY_PG_PARTIAL") == "1":
        return True   # diagnostics only: read a partial load
    try:
        import psycopg
        from operating.pg.ontology_load import ORDER
        with psycopg.connect(target or dsn(), connect_timeout=5) as pg:
            done = {row[0] for row in pg.execute(
                "SELECT substr(name, 7) FROM ontology.load_state WHERE name LIKE 'table:%%'")}
            generation = pg.execute("SELECT value FROM ontology.load_state WHERE name = "
                                    "'generation'").fetchone()
        if generation is None:
            return False
        if digest is not None and (generation[0] or {}).get("content_digest") != digest:
            return False
        deferred = set((generation[0] or {}).get("deferred") or ())
        return set(ORDER) - deferred <= done
    except Exception:  # noqa: BLE001 - not ready: the SQLite generation serves
        return False


class PgStoreReader(StoreReader):
    """``StoreReader`` over Postgres (module docstring)."""

    def __init__(self, conn: PgCompatConnection, text_reader: StoreReader | None = None
                 ) -> None:
        super().__init__(conn)   # type: ignore[arg-type]
        self.text_reader = text_reader
        if text_reader is not None:
            conn.mirror = text_reader.conn

    @classmethod
    def open(cls, dsn: str | Path | None = None, *, text_store: str | Path | None = None
             ) -> "PgStoreReader":
        from operating.pg import config
        text = None
        if text_store is not None and Path(text_store).is_file():
            text = StoreReader.open(text_store)
        reader = cls(PgCompatConnection(str(dsn) if dsn else config.dsn()), text)
        try:
            row = reader.conn.execute("SELECT value FROM load_state WHERE name = "
                                      "'generation'").fetchone()
            reader.deferred = frozenset(json.loads(row[0]).get("deferred") or ()) \
                if row and isinstance(row[0], str) else frozenset(
                    (row[0] or {}).get("deferred") or ()) if row else frozenset()
        except (sqlite3.Error, ValueError, AttributeError):
            reader.deferred = frozenset()
        return reader

    deferred: frozenset = frozenset()

    def object_chunks(self, scope: Any, object_ids: Sequence[str]) -> list[Any]:
        """The document chunks of these objects: from the SQLite generation while the
        load deferred ``text_chunks`` (``ontology_load.DEFERRABLE``), else Postgres."""
        if "text_chunks" in self.deferred and self.text_reader is not None:
            with self.text_reader.lock:
                return self.text_reader.object_chunks(scope, object_ids)
        return super().object_chunks(scope, object_ids)

    @property
    def word_conn(self) -> Any:
        """The connection word-index queries run on (``value_text``, ``chunk_text`` and the
        value-cell spans that narrow them): the SQLite generation's, with every function
        registered on this reader's connection; this one when there is none."""
        return self.text_reader.conn if self.text_reader is not None else self.conn

    # The word index lives in the SQLite generation (module docstring).
    def text_matches(self, scope: Any, words: str, typed_properties: Sequence[str] | None,
                     after: int, batch: int, **kwargs: Any) -> list[Any]:
        if self.text_reader is None:
            return []
        with self.text_reader.lock:
            return self.text_reader.text_matches(scope, words, typed_properties, after,
                                                 batch, **kwargs)

    def chunk_matches(self, scope: Any, words: str, object_type: str | None, after: int,
                      batch: int) -> list[Any]:
        if self.text_reader is None:
            return []
        with self.text_reader.lock:
            return self.text_reader.chunk_matches(scope, words, object_type, after, batch)

    def holds_text_of(self, schemes: Sequence[str]) -> bool:
        try:
            with self.lock:
                for scheme in schemes:
                    if self.conn.execute(
                            "SELECT 1 FROM text_fields WHERE citation >= ? AND citation < ? "
                            "LIMIT 1", (f"{scheme}://", f"{scheme}:/0")).fetchone():
                        return True
            return False
        except sqlite3.OperationalError:
            return True

    def close(self) -> None:
        self.conn.close()
        if self.text_reader is not None:
            self.text_reader.conn.close()


__all__ = ["ENV", "PgStoreReader", "enabled"]
