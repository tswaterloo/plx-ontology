"""Declared schema of the feedback store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, at TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('answer','alert','page','record','list','ui','general')),
  reader TEXT NOT NULL, reader_name TEXT, by_admin INTEGER NOT NULL DEFAULT 0,
  rating INTEGER, verdict TEXT, note TEXT, expected TEXT,
  page TEXT, object_type TEXT, object_id TEXT, label TEXT,
  item_id TEXT, answer_id TEXT, question TEXT, answer TEXT,
  context TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','accepted','fixed','wont_fix')),
  status_by TEXT, status_at TEXT);
CREATE INDEX IF NOT EXISTS feedback_at ON feedback (at);
CREATE INDEX IF NOT EXISTS feedback_reader ON feedback (reader, at);
"""

STORE = "feedback"
STEPS = ((1, "initial", (_DDL,)),)
