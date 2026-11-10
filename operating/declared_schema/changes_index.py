"""Declared schema of the changes_index store (see ``operating.declared_schema``)."""

_INDEX_DDL = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE ver (object_type TEXT NOT NULL, object_id TEXT NOT NULL, "
    "audience TEXT NOT NULL, seq INTEGER NOT NULL, handle TEXT NOT NULL, "
    "recorded TEXT NOT NULL, superseded TEXT, start_us INTEGER NOT NULL, end_us INTEGER, "
    "markings TEXT NOT NULL, latest TEXT NOT NULL, digest TEXT NOT NULL, created TEXT)",
    "CREATE TABLE removed (object_id TEXT PRIMARY KEY)",
    "CREATE TABLE audiences (object_type TEXT NOT NULL, audience TEXT NOT NULL, "
    "PRIMARY KEY (object_type, audience))",
)

_INDEX_INDEXES = (
    "CREATE INDEX ver_start ON ver(object_type, start_us)",
    "CREATE INDEX ver_end ON ver(object_type, end_us)",
    "CREATE INDEX ver_object ON ver(object_id, seq)",
    "CREATE INDEX ver_audience ON ver(object_type, audience, latest, markings, start_us)",
)

STEPS = ((1, "initial", (_INDEX_DDL, _INDEX_INDEXES,)),)
