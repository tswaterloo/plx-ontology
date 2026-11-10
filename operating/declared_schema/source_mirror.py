"""Declared schema of the source_mirror store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS mirror_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS upstream_catalog (
  id INTEGER PRIMARY KEY CHECK (id = 1), fetched_at TEXT NOT NULL,
  relations_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capture (
  capture_id INTEGER PRIMARY KEY AUTOINCREMENT, relation TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('lazy_fill','full_traversal','incremental')),
  reason TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
  since_sent TEXT, catalog_revision TEXT NOT NULL, columns_json TEXT NOT NULL,
  page_count INTEGER NOT NULL, rows_received INTEGER NOT NULL,
  keys_new INTEGER NOT NULL, keys_changed INTEGER NOT NULL,
  keys_unchanged INTEGER NOT NULL, keys_absent INTEGER NOT NULL,
  health_before_json TEXT NOT NULL, health_after_json TEXT NOT NULL,
  atomic_upstream_snapshot INTEGER NOT NULL DEFAULT 0 CHECK (atomic_upstream_snapshot = 0),
  deletions_proven INTEGER NOT NULL DEFAULT 0 CHECK (deletions_proven = 0));
CREATE TABLE IF NOT EXISTS relation_state (
  relation TEXT PRIMARY KEY, catalog_revision TEXT NOT NULL,
  catalog_entry_json TEXT NOT NULL, key_columns_json TEXT NOT NULL,
  columns_json TEXT NOT NULL, since_column TEXT, since_mode TEXT,
  key_unique INTEGER NOT NULL, last_full_capture_id INTEGER NOT NULL,
  last_full_traversal_at TEXT NOT NULL, last_capture_id INTEGER NOT NULL,
  last_refresh_at TEXT NOT NULL, last_refresh_mode TEXT NOT NULL,
  last_read_seconds REAL NOT NULL, watermark TEXT, watermark_raw TEXT,
  row_count INTEGER NOT NULL, absent_count INTEGER NOT NULL,
  null_since_rows INTEGER NOT NULL, future_since_rows INTEGER NOT NULL,
  health_before_json TEXT NOT NULL, health_after_json TEXT NOT NULL,
  identifier_columns_json TEXT NOT NULL,
  atomic_upstream_snapshot INTEGER NOT NULL DEFAULT 0 CHECK (atomic_upstream_snapshot = 0),
  deletions_proven INTEGER NOT NULL DEFAULT 0 CHECK (deletions_proven = 0));
CREATE TABLE IF NOT EXISTS refresh_failure (
  relation TEXT PRIMARY KEY, failed_at TEXT NOT NULL, error TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS row_current (
  relation TEXT NOT NULL, key_json TEXT NOT NULL, run_index INTEGER NOT NULL,
  run_length INTEGER NOT NULL, row_json TEXT NOT NULL, row_digest TEXT NOT NULL,
  capture_id INTEGER NOT NULL, first_seen_capture INTEGER NOT NULL,
  first_seen_at TEXT NOT NULL, last_seen_capture INTEGER NOT NULL,
  last_seen_at TEXT NOT NULL, absent_since_capture INTEGER,
  missing_from_source_since TEXT, missing_basis TEXT, lineage TEXT, lineage_basis TEXT,
  deleted_in_source TEXT, deleted_basis TEXT,
  PRIMARY KEY (relation, key_json, run_index));
CREATE TABLE IF NOT EXISTS row_removal (
  relation TEXT NOT NULL, key_json TEXT NOT NULL,
  basis TEXT NOT NULL CHECK (basis IN ('missing_from_source','source_soft_delete')),
  since TEXT NOT NULL, until TEXT, replaced_basis TEXT, replaced_by TEXT, until_basis TEXT,
  CHECK (until IS NULL OR until >= since));
CREATE INDEX IF NOT EXISTS idx_row_removal_key ON row_removal(relation, key_json);
CREATE UNIQUE INDEX IF NOT EXISTS idx_row_removal_open ON row_removal(relation, key_json, basis)
  WHERE until IS NULL;
CREATE TABLE IF NOT EXISTS relation_soft_delete (
  relation TEXT PRIMARY KEY, columns_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS row_version (
  relation TEXT NOT NULL, key_json TEXT NOT NULL, capture_id INTEGER NOT NULL,
  run_index INTEGER NOT NULL, run_length INTEGER NOT NULL,
  row_json TEXT NOT NULL, row_digest TEXT NOT NULL,
  last_seen_capture INTEGER NOT NULL, last_seen_at TEXT NOT NULL,
  superseded_by_capture INTEGER NOT NULL, lineage TEXT, lineage_basis TEXT,
  PRIMARY KEY (relation, key_json, capture_id, run_index));
CREATE TABLE IF NOT EXISTS identifier_index (
  norm_value TEXT NOT NULL, relation TEXT NOT NULL, column_name TEXT NOT NULL,
  key_json TEXT NOT NULL, run_index INTEGER NOT NULL,
  PRIMARY KEY (norm_value, relation, column_name, key_json, run_index));
CREATE INDEX IF NOT EXISTS idx_identifier_key ON identifier_index(relation, key_json);
CREATE TABLE IF NOT EXISTS capture_state (
  capture_id INTEGER PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('in_progress','complete','abandoned')),
  note TEXT);
CREATE TABLE IF NOT EXISTS sync_progress (
  relation TEXT PRIMARY KEY, capture_id INTEGER NOT NULL, mode TEXT NOT NULL,
  reason TEXT NOT NULL, since_sent TEXT, catalog_revision TEXT NOT NULL,
  columns_json TEXT NOT NULL, cursor TEXT, page_size INTEGER NOT NULL,
  pages INTEGER NOT NULL, rows_received INTEGER NOT NULL, keys_new INTEGER NOT NULL,
  keys_changed INTEGER NOT NULL, keys_unchanged INTEGER NOT NULL,
  max_run_length INTEGER NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  resumed INTEGER NOT NULL, health_before_json TEXT NOT NULL, owner TEXT,
  merge_json TEXT);
CREATE TABLE IF NOT EXISTS stage_row (
  relation TEXT NOT NULL, capture_id INTEGER NOT NULL, key_json TEXT NOT NULL,
  run_index INTEGER NOT NULL, run_length INTEGER NOT NULL, row_json TEXT NOT NULL,
  row_digest TEXT NOT NULL, lineage TEXT, lineage_basis TEXT,
  PRIMARY KEY (capture_id, key_json, run_index)) WITHOUT ROWID;
-- The keys a full traversal saw with their content unchanged: they are not staged and
-- their rows are not written (``_apply_page``); only this key is, until ``_finish``.
CREATE TABLE IF NOT EXISTS stage_seen (
  relation TEXT NOT NULL, capture_id INTEGER NOT NULL, key_json TEXT NOT NULL,
  PRIMARY KEY (capture_id, key_json)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS absence_scan (
  scan_id INTEGER PRIMARY KEY AUTOINCREMENT, relation TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('in_progress','complete','abandoned')),
  started_at TEXT NOT NULL, finished_at TEXT, catalog_revision TEXT NOT NULL,
  cursor TEXT, page_size INTEGER NOT NULL, pages INTEGER NOT NULL,
  keys_seen INTEGER NOT NULL, keys_missing INTEGER, keys_returned INTEGER,
  keys_unmirrored INTEGER, resumed INTEGER NOT NULL, health_before_json TEXT NOT NULL,
  health_after_json TEXT);
CREATE INDEX IF NOT EXISTS idx_absence_scan_relation ON absence_scan(relation, scan_id);
CREATE TABLE IF NOT EXISTS absence_seen (
  scan_id INTEGER NOT NULL, key_json TEXT NOT NULL,
  PRIMARY KEY (scan_id, key_json)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS relation_lineage (
  relation TEXT PRIMARY KEY, rules_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS persisted_relation (
  relation TEXT PRIMARY KEY, freshness_json TEXT NOT NULL, attempted_at TEXT NOT NULL);
"""

_CHANGE_DDL = """
-- Per-relation change counters (decision 2026-11-01-mirror-relation-marks): every
-- write that changes what a projection reads of a relation (its rows, removals,
-- the state and finish time of a capture its rows name) moves
-- its counter, whatever path writes it, a migration at writer open included; ''
-- moves on a mirror_meta change. change_capture: the captures rows name. A build
-- whose marks are the ones it fingerprinted under reads the relation's fingerprint
-- back (ontology.project.MirrorCaptures.relation_marks).
CREATE TABLE IF NOT EXISTS relation_change (relation TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS change_capture (capture_id INTEGER PRIMARY KEY);
CREATE TRIGGER IF NOT EXISTS change_row_current_insert AFTER INSERT ON row_current BEGIN INSERT INTO relation_change (relation, n) VALUES (NEW.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; INSERT OR IGNORE INTO change_capture VALUES (NEW.capture_id); END;
CREATE TRIGGER IF NOT EXISTS change_row_current_delete AFTER DELETE ON row_current BEGIN INSERT INTO relation_change (relation, n) VALUES (OLD.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_row_current_update AFTER UPDATE ON row_current WHEN OLD.relation IS NOT NEW.relation OR OLD.key_json IS NOT NEW.key_json OR OLD.run_index IS NOT NEW.run_index OR OLD.run_length IS NOT NEW.run_length OR OLD.row_json IS NOT NEW.row_json OR OLD.row_digest IS NOT NEW.row_digest OR OLD.capture_id IS NOT NEW.capture_id OR OLD.missing_from_source_since IS NOT NEW.missing_from_source_since BEGIN INSERT INTO relation_change (relation, n) VALUES (OLD.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; INSERT INTO relation_change (relation, n) VALUES (NEW.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; INSERT OR IGNORE INTO change_capture VALUES (NEW.capture_id); END;
CREATE TRIGGER IF NOT EXISTS change_row_version_insert AFTER INSERT ON row_version BEGIN INSERT INTO relation_change (relation, n) VALUES (NEW.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; INSERT OR IGNORE INTO change_capture VALUES (NEW.capture_id); INSERT OR IGNORE INTO change_capture VALUES (NEW.superseded_by_capture); END;
CREATE TRIGGER IF NOT EXISTS change_row_version_delete AFTER DELETE ON row_version BEGIN INSERT INTO relation_change (relation, n) VALUES (OLD.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_row_version_update AFTER UPDATE ON row_version WHEN OLD.relation IS NOT NEW.relation OR OLD.key_json IS NOT NEW.key_json OR OLD.capture_id IS NOT NEW.capture_id OR OLD.run_index IS NOT NEW.run_index OR OLD.run_length IS NOT NEW.run_length OR OLD.row_json IS NOT NEW.row_json OR OLD.row_digest IS NOT NEW.row_digest OR OLD.last_seen_at IS NOT NEW.last_seen_at OR OLD.superseded_by_capture IS NOT NEW.superseded_by_capture BEGIN INSERT INTO relation_change (relation, n) VALUES (OLD.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; INSERT INTO relation_change (relation, n) VALUES (NEW.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; INSERT OR IGNORE INTO change_capture VALUES (NEW.capture_id); INSERT OR IGNORE INTO change_capture VALUES (NEW.superseded_by_capture); END;
CREATE TRIGGER IF NOT EXISTS change_row_removal_insert AFTER INSERT ON row_removal BEGIN INSERT INTO relation_change (relation, n) VALUES (NEW.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1;  END;
CREATE TRIGGER IF NOT EXISTS change_row_removal_delete AFTER DELETE ON row_removal BEGIN INSERT INTO relation_change (relation, n) VALUES (OLD.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_row_removal_update AFTER UPDATE ON row_removal WHEN OLD.relation IS NOT NEW.relation OR OLD.key_json IS NOT NEW.key_json OR OLD.basis IS NOT NEW.basis OR OLD.since IS NOT NEW.since OR OLD.until IS NOT NEW.until OR OLD.replaced_basis IS NOT NEW.replaced_basis OR OLD.replaced_by IS NOT NEW.replaced_by BEGIN INSERT INTO relation_change (relation, n) VALUES (OLD.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; INSERT INTO relation_change (relation, n) VALUES (NEW.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1;  END;
CREATE TRIGGER IF NOT EXISTS change_capture_state_insert AFTER INSERT ON capture_state WHEN EXISTS (SELECT 1 FROM change_capture c WHERE c.capture_id = NEW.capture_id) BEGIN INSERT INTO relation_change (relation, n) VALUES (COALESCE((SELECT relation FROM capture WHERE capture_id = NEW.capture_id), ''), 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_capture_insert AFTER INSERT ON capture WHEN EXISTS (SELECT 1 FROM change_capture c WHERE c.capture_id = NEW.capture_id) BEGIN INSERT INTO relation_change (relation, n) VALUES (NEW.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_mirror_meta_insert AFTER INSERT ON mirror_meta BEGIN INSERT INTO relation_change (relation, n) VALUES ('', 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_capture_state_delete AFTER DELETE ON capture_state WHEN EXISTS (SELECT 1 FROM change_capture c WHERE c.capture_id = OLD.capture_id) BEGIN INSERT INTO relation_change (relation, n) VALUES (COALESCE((SELECT relation FROM capture WHERE capture_id = OLD.capture_id), ''), 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_capture_delete AFTER DELETE ON capture WHEN EXISTS (SELECT 1 FROM change_capture c WHERE c.capture_id = OLD.capture_id) BEGIN INSERT INTO relation_change (relation, n) VALUES (OLD.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_mirror_meta_delete AFTER DELETE ON mirror_meta BEGIN INSERT INTO relation_change (relation, n) VALUES ('', 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_capture_state_update AFTER UPDATE ON capture_state WHEN EXISTS (SELECT 1 FROM change_capture c WHERE c.capture_id = NEW.capture_id) BEGIN INSERT INTO relation_change (relation, n) VALUES (COALESCE((SELECT relation FROM capture WHERE capture_id = OLD.capture_id), ''), 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; INSERT INTO relation_change (relation, n) VALUES (COALESCE((SELECT relation FROM capture WHERE capture_id = NEW.capture_id), ''), 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_capture_update AFTER UPDATE ON capture WHEN EXISTS (SELECT 1 FROM change_capture c WHERE c.capture_id = NEW.capture_id) AND (OLD.finished_at IS NOT NEW.finished_at OR OLD.relation IS NOT NEW.relation) BEGIN INSERT INTO relation_change (relation, n) VALUES (OLD.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; INSERT INTO relation_change (relation, n) VALUES (NEW.relation, 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
CREATE TRIGGER IF NOT EXISTS change_mirror_meta_update AFTER UPDATE ON mirror_meta WHEN OLD.value IS NOT NEW.value OR OLD.name IS NOT NEW.name BEGIN INSERT INTO relation_change (relation, n) VALUES ('', 1) ON CONFLICT(relation) DO UPDATE SET n = n + 1; END;
"""

STORE = "source_mirror"
STEPS = ((1, "initial", (_DDL, _CHANGE_DDL,)),)
