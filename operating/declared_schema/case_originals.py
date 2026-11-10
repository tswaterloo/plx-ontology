"""Declared schema of the case_originals store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS original (
  original_id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  tenant TEXT NOT NULL,
  classification TEXT NOT NULL,
  audience_json TEXT NOT NULL,
  filename TEXT NOT NULL,
  media_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  attached_by TEXT NOT NULL,
  attached_at TEXT NOT NULL,
  extraction_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS original_no_update BEFORE UPDATE ON original
BEGIN SELECT RAISE(ABORT, 'original_is_immutable'); END;
CREATE TABLE IF NOT EXISTS reextraction (
  original_id TEXT NOT NULL REFERENCES original(original_id),
  version INTEGER NOT NULL CHECK (version >= 2),
  extracted_at TEXT NOT NULL,
  extracted_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  local_ocr INTEGER NOT NULL,
  extraction_json TEXT NOT NULL,
  PRIMARY KEY (original_id, version)
);
CREATE TRIGGER IF NOT EXISTS reextraction_no_update BEFORE UPDATE ON reextraction
BEGIN SELECT RAISE(ABORT, 'extraction_is_immutable'); END;
CREATE TRIGGER IF NOT EXISTS reextraction_no_delete BEFORE DELETE ON reextraction
BEGIN SELECT RAISE(ABORT, 'extraction_is_immutable'); END;
CREATE TABLE IF NOT EXISTS original_source (
  original_id TEXT PRIMARY KEY REFERENCES original(original_id),
  provenance_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS original_source_no_update BEFORE UPDATE ON original_source
BEGIN SELECT RAISE(ABORT, 'original_is_immutable'); END;
CREATE TRIGGER IF NOT EXISTS original_source_no_delete BEFORE DELETE ON original_source
BEGIN SELECT RAISE(ABORT, 'original_is_immutable'); END;
"""

STORE = "case_originals"
STEPS = ((1, "initial", (_DDL,)),)
