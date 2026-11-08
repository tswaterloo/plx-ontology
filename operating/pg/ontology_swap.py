"""A new generation loaded beside the served one and swapped in at once.

Readers on Postgres (``PLX_ONTOLOGY_PG``) keep reading ``ontology.*`` while a newly published
generation is loaded into a schema of its own (``ontology_next``, the same migrations);
``swap`` then moves its tables into ``ontology`` in one transaction (the old tables
dropped, the views made again over the new ones), so a reader sees the old generation or
the new one, never a half-loaded store. The delta layers applied to the old generation go
with it: the new one's layers apply after the swap (their base is the new generation).

The functions of ``ontology`` name their tables by name, so they read the moved tables;
the views are bound to tables and are made again from their own definitions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

NEXT = "ontology_next"
MIGRATIONS = ("0006_ontology.sql", "0007_ontology_compact.sql", "0008_ontology_read.sql",
              "0010_ontology_words.sql", "0011_ontology_word_order.sql")
# Data and state tables a generation consists of (sequences move with them).
TABLES = ("objects", "object_removals", "object_versions", "value_fields", "value_kinds",
          "value_sources", "value_cells", "identifiers", "events", "event_objects",
          "link_texts", "link_skeletons", "link_rows", "text_fields", "text_chunks",
          "packed_sources", "chunk_words", "store_meta", "load_state", "load_ranges")
# in dependency order; the values' word index moves with value_cells (its function is
# ``ontology.sq_words``, never the next schema's)
VIEWS = ("object_values_typed", "object_values", "links", "value_field_order", "value_words",
         "object_values_numbered")


def prepare_next(dsn: str, digest: str | None = None) -> bool:
    """``ontology_next`` made by the ontology migrations (renamed), empty; kept as it is
    when it already holds (part of) the generation ``digest`` (a resumed load). True when
    it was made anew."""
    import psycopg
    folder = Path(__file__).resolve().parent / "migrations"
    with psycopg.connect(dsn, autocommit=True) as pg:
        if digest is not None and pg.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname = %s", (NEXT,)).fetchone():
            held = pg.execute(f"SELECT value->>'content_digest' FROM {NEXT}.load_state "
                              "WHERE name = 'generation'").fetchone()
            if held and held[0] == digest:
                return False
        pg.execute(f"DROP SCHEMA IF EXISTS {NEXT} CASCADE")
        for name in MIGRATIONS:
            text = (folder / name).read_text(encoding="utf-8")
            pg.execute(text.replace("ontology.", f"{NEXT}.")
                       .replace("SCHEMA IF NOT EXISTS ontology;", f"SCHEMA IF NOT EXISTS {NEXT};")
                       .replace("SCHEMA IF NOT EXISTS ontology\n", f"SCHEMA IF NOT EXISTS {NEXT}\n"))
    return True


def swap(dsn: str, *, lock_timeout_ms: int = 60_000) -> dict[str, Any]:
    """Move ``ontology_next``'s tables into ``ontology`` in one transaction (module
    docstring); returns what moved."""
    import psycopg
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        cur.execute(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}")
        views = {}
        for view in VIEWS:
            cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", (f"ontology.{view}",))
            views[view] = cur.fetchone()[0]
        cur.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = "
                    "c.relnamespace WHERE n.nspname = %s AND c.relkind = 'S'", (NEXT,))
        sequences = [r[0] for r in cur.fetchall()]
        # index definitions a load keeps are qualified with its schema: none may move
        cur.execute(f"DELETE FROM {NEXT}.load_state WHERE name LIKE 'indexes:%%'")
        for table in TABLES:
            cur.execute(f"DROP TABLE IF EXISTS ontology.{table} CASCADE")
            cur.execute(f"ALTER TABLE {NEXT}.{table} SET SCHEMA ontology")
        for sequence in sequences:
            cur.execute(f"DROP SEQUENCE IF EXISTS ontology.{sequence}")
            cur.execute(f"ALTER SEQUENCE {NEXT}.{sequence} SET SCHEMA ontology")
        for view in VIEWS:
            cur.execute(f"CREATE OR REPLACE VIEW ontology.{view} AS {views[view]}")
        cur.execute(f"DROP SCHEMA {NEXT} CASCADE")
    return {"tables": len(TABLES), "sequences": len(sequences), "views": len(VIEWS)}
