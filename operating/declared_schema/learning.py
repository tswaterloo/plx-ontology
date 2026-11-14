"""Declared schema of the learning ledger (``operating.learning.Ledger``,
``<state>/learning/learning.sqlite``; see ``operating.declared_schema``)."""

STORE = "learning"
STEPS = ((1, "initial", """
CREATE TABLE IF NOT EXISTS signals (ref TEXT PRIMARY KEY, kind TEXT NOT NULL,
  person TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS signals_person ON signals (person, at);
CREATE TABLE IF NOT EXISTS lessons (lesson_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
  who TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  text TEXT NOT NULL, confidence REAL NOT NULL, weight REAL NOT NULL, total REAL NOT NULL,
  seen INTEGER NOT NULL, first_at TEXT NOT NULL, last_at TEXT NOT NULL,
  evidence TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lessons_who ON lessons (scope, who);
CREATE TABLE IF NOT EXISTS forgotten (who TEXT NOT NULL, kind TEXT NOT NULL,
  key TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (who, kind, key));
CREATE TABLE IF NOT EXISTS runs (at TEXT PRIMARY KEY, summary TEXT NOT NULL);
"""),)
