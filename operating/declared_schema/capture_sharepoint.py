"""Declared schema of the capture_sharepoint store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roots (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    round_started_at TEXT,
    last_run_at TEXT,
    last_complete_at TEXT,
    rounds_complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (drive_id, root_path)
);
-- One row per validity interval of a drive-item version. A version that comes back
-- after a gap (removal, a move with the same eTag) gets a new row under the same
-- handle, so as_known_at intervals never overlap.
CREATE TABLE IF NOT EXISTS items (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    l1_handle TEXT NOT NULL,
    drive_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    etag TEXT NOT NULL,
    root_path TEXT NOT NULL,
    path TEXT NOT NULL,
    record TEXT NOT NULL CHECK (json_valid(record)),
    resources TEXT NOT NULL CHECK (json_valid(resources)),
    source_acl TEXT NOT NULL CHECK (json_valid(source_acl)),
    digest TEXT NOT NULL,
    content_status TEXT NOT NULL,
    content_sha256 TEXT REFERENCES blobs(sha256),
    text_status TEXT NOT NULL,
    text_sha256 TEXT REFERENCES blobs(sha256),
    recorded_at TEXT NOT NULL,
    superseded_at TEXT,
    removed_reason TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_by_id ON items(drive_id, item_id);
CREATE INDEX IF NOT EXISTS items_by_handle ON items(l1_handle);
-- The visibility stamp (``visible_current``): bumped by every write that changes a column
-- a version's visibility or its listing is decided on, in that write's transaction,
-- whatever code makes it; a listing that re-reads equal permissions leaves it as it is.
INSERT OR IGNORE INTO meta VALUES ('visibility_stamp', '0');
CREATE TRIGGER IF NOT EXISTS items_visibility_insert AFTER INSERT ON items BEGIN
    UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'visibility_stamp';
END;
CREATE TRIGGER IF NOT EXISTS items_visibility_delete AFTER DELETE ON items BEGIN
    UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'visibility_stamp';
END;
CREATE TRIGGER IF NOT EXISTS items_visibility_update AFTER UPDATE OF version_id, l1_handle, drive_id, item_id, content_sha256, recorded_at, superseded_at, removed_reason, resources, source_acl ON items
    WHEN OLD.version_id IS NOT NEW.version_id OR OLD.l1_handle IS NOT NEW.l1_handle OR OLD.drive_id IS NOT NEW.drive_id OR OLD.item_id IS NOT NEW.item_id OR OLD.content_sha256 IS NOT NEW.content_sha256 OR OLD.recorded_at IS NOT NEW.recorded_at OR OLD.superseded_at IS NOT NEW.superseded_at OR OLD.removed_reason IS NOT NEW.removed_reason OR OLD.resources IS NOT NEW.resources OR OLD.source_acl IS NOT NEW.source_acl BEGIN
    UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'visibility_stamp';
END;
CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_events (
    l1_handle TEXT NOT NULL,
    event TEXT NOT NULL,
    at TEXT NOT NULL
);
-- Legacy: one change-feed cursor per drive. It only seeds root_cursors (every root of
-- the drive that was walked) and is then deleted.
CREATE TABLE IF NOT EXISTS drive_cursors (
    drive_id TEXT PRIMARY KEY,
    delta_url TEXT NOT NULL,
    stored_at TEXT NOT NULL
);
-- Each root's position in its drive's change feed (graph_read.SharePointReader
-- .drive_delta): the root has applied every change before delta_url, read at read_at
-- (NULL: unknown, the earliest). A NULL delta_url is no position (the root walks).
CREATE TABLE IF NOT EXISTS root_cursors (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    delta_url TEXT,
    read_at TEXT,
    PRIMARY KEY (drive_id, root_path)
);
-- Each root's permission position: every item permission change its drive's change
-- feed listed before delta_url (read at read_at; NULL: unknown, the earliest) has been
-- re-read for the root. No row: unknown (the root walks once it follows the feed).
-- reread_url/reread_at: the feed cursor and read time of the first run of a re-read the
-- time budget cut (NULL: none open).
CREATE TABLE IF NOT EXISTS root_permissions (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    delta_url TEXT,
    read_at TEXT,
    reread_url TEXT,
    reread_at TEXT,
    PRIMARY KEY (drive_id, root_path)
);
-- The folders the last full walk of a root listed, by item id, so a change-feed entry
-- is attributed to the root whose walked folder is its parent. Rebuilt by every full
-- walk of the root; root_walks.walked_at is when its last full walk ended.
CREATE TABLE IF NOT EXISTS walked_folders (
    drive_id TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT,
    parent_id TEXT,
    depth INTEGER NOT NULL,
    PRIMARY KEY (drive_id, folder_id)
);
-- The security class the scope gave a library when it was last planned; UNRESOLVED
-- for a restricted library it did not classify (served to no employee).
CREATE TABLE IF NOT EXISTS drive_classes (
    drive_id TEXT PRIMARY KEY,
    security_class TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS root_walks (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    walked_at TEXT NOT NULL,
    PRIMARY KEY (drive_id, root_path)
);
-- The position of a full walk the time budget cut, resumed by the root's next run:
-- started_at is when its first run started, delta_url the drive's change-feed cursor
-- read before it, position the folders left (the cut one first, with the children it
-- already holds) and the walk's gaps so far.
-- A text re-read of a version whose refetch failed for that file alone (a 403/423 on
-- the download, no download URL, bytes that differ from the item's size or hash): tries
-- so far, the last failure's fixed-vocabulary token and time, and when it may be tried
-- again (exponential backoff). gave_up_at: past REREAD_MAX_ATTEMPTS, a named gap.
-- A failure of the source as a whole (throttling, no connection, a 5xx on every
-- download, refused credentials) records no try, only last_at, so the version goes
-- behind the others next run.
CREATE TABLE IF NOT EXISTS text_reread_attempts (
    l1_handle TEXT NOT NULL,
    generation TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    last_error TEXT NOT NULL,
    last_at TEXT NOT NULL,
    next_at TEXT,
    gave_up_at TEXT,
    PRIMARY KEY (l1_handle, generation)
);
CREATE TABLE IF NOT EXISTS walk_positions (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    delta_url TEXT,
    position TEXT NOT NULL CHECK (json_valid(position)),
    saved_at TEXT NOT NULL,
    PRIMARY KEY (drive_id, root_path)
);
"""

STORE = "capture_sharepoint"
STEPS = ((1, "initial", (_DDL,)),)
