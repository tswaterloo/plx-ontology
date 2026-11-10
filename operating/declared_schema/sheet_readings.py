"""Declared schema of the sheet_readings store (see ``operating.declared_schema``)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_key TEXT PRIMARY KEY, reader TEXT NOT NULL, status TEXT NOT NULL,
    kinds TEXT NOT NULL, tables INTEGER NOT NULL, rows INTEGER NOT NULL,
    seconds REAL NOT NULL, read_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    handle TEXT PRIMARY KEY, file_key TEXT NOT NULL, origin TEXT NOT NULL,
    name TEXT NOT NULL, object_id TEXT, object_type TEXT, audience_key TEXT,
    markings TEXT, received_at TEXT, participants TEXT NOT NULL DEFAULT '[]',
    seen_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sources_by_file ON sources(file_key);
CREATE TABLE IF NOT EXISTS tables (
    file_key TEXT NOT NULL, table_no INTEGER NOT NULL, kind TEXT NOT NULL,
    sheet TEXT NOT NULL, header TEXT NOT NULL, roles TEXT NOT NULL, facts TEXT NOT NULL,
    PRIMARY KEY (file_key, table_no));
CREATE TABLE IF NOT EXISTS readings (
    file_key TEXT NOT NULL, table_no INTEGER NOT NULL, seq INTEGER NOT NULL,
    kind TEXT NOT NULL, cell TEXT NOT NULL, fields TEXT NOT NULL,
    PRIMARY KEY (file_key, table_no, seq));
CREATE INDEX IF NOT EXISTS readings_by_kind ON readings(kind, file_key);
"""

STORE = "sheet_readings"
STEPS = ((1, "initial", (SCHEMA,)),)
