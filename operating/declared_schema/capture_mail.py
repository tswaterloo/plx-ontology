"""Declared schema of the capture_mail store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mailboxes (
    mailbox_id TEXT PRIMARY KEY,
    upn TEXT NOT NULL,
    identities TEXT NOT NULL,
    resources TEXT NOT NULL,
    source_acl TEXT NOT NULL,
    first_captured_at TEXT NOT NULL,
    identity_checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    mailbox_id TEXT NOT NULL REFERENCES mailboxes(mailbox_id),
    folder_key TEXT NOT NULL,
    folder_id TEXT,
    window_days INTEGER,
    received_since TEXT,
    next_url TEXT,
    delta_url TEXT,
    round_kind TEXT,
    round_started_at TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    last_complete_at TEXT,
    last_run_at TEXT,
    rounds_complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mailbox_id, folder_key)
);
-- One row per validity interval of a message version. A version that comes
-- back after a removal gets a new row (same l1_handle, new interval), so
-- as_known_at intervals never overlap and a removal gap stays a gap.
CREATE TABLE IF NOT EXISTS messages (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    l1_handle TEXT NOT NULL,
    mailbox_id TEXT NOT NULL REFERENCES mailboxes(mailbox_id),
    message_id TEXT NOT NULL,
    change_key TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    record TEXT NOT NULL CHECK (json_valid(record)),
    digest TEXT NOT NULL,
    body_status TEXT NOT NULL,
    body_sha256 TEXT REFERENCES blobs(sha256),
    attachments_status TEXT NOT NULL DEFAULT 'complete',
    attachment_list_attempts INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    superseded_at TEXT,
    removed_reason TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_id ON messages(mailbox_id, message_id);
CREATE INDEX IF NOT EXISTS messages_by_handle ON messages(l1_handle);
CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    message_handle TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    l1_handle TEXT UNIQUE,
    mailbox_id TEXT NOT NULL,
    metadata TEXT NOT NULL CHECK (json_valid(metadata)),
    content_sha256 TEXT,
    content_status TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    text_status TEXT,
    text_sha256 TEXT REFERENCES blobs(sha256),
    PRIMARY KEY (message_handle, attachment_id)
);
-- Every reading of an attachment's text, append-only: a re-extraction (OCR arrived, a
-- failed OCR fixed) is a new row, never an update, so a citation to an earlier reading
-- keeps resolving to that reading. attachments.text_status/text_sha256 is the pointer
-- to the newest.
CREATE TABLE IF NOT EXISTS attachment_text_versions (
    l1_handle TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    text_status TEXT NOT NULL,
    text_sha256 TEXT REFERENCES blobs(sha256),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (l1_handle, version)
);
CREATE TRIGGER IF NOT EXISTS attachment_text_versions_no_update
BEFORE UPDATE ON attachment_text_versions
BEGIN SELECT RAISE(ABORT, 'attachment_text_versions_immutable'); END;
CREATE TRIGGER IF NOT EXISTS attachment_text_versions_no_delete
BEFORE DELETE ON attachment_text_versions
BEGIN SELECT RAISE(ABORT, 'attachment_text_versions_immutable'); END;
CREATE TABLE IF NOT EXISTS message_events (
    l1_handle TEXT NOT NULL,
    event TEXT NOT NULL,
    at TEXT NOT NULL
);
-- The directory department of a listed mailbox (JSON of the listing's value), recorded
-- before each directory-wide run so the deny list decides on it at read.
CREATE TABLE IF NOT EXISTS mailbox_directory (
    mailbox_id TEXT PRIMARY KEY,
    department TEXT,
    listed_at TEXT NOT NULL
);
-- The last complete tenant listing the run's people guard was bound with (replaced whole
-- each run; ``people_exclusion.tenant_listing_rows``): each address of a listed user the
-- exclusion does not name with its object id (kind user), and each group address (kind
-- group). Nothing about an excluded person. Its read time is meta ``tenant_listing_at``.
CREATE TABLE IF NOT EXISTS tenant_listing (
    address TEXT PRIMARY KEY,
    object_id TEXT,
    kind TEXT NOT NULL
);
"""

STORE = "capture_mail"
STEPS = ((1, "initial", (_DDL,)),)


# Applied when a mailbox's folder listing and priority aggregates are first kept (not at
# open), so a fresh store holds them only then, as before.
_FOLDER_LISTING_DDL = """CREATE TABLE IF NOT EXISTS mailbox_folders (
    mailbox_id TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    total INTEGER,
    listed_at TEXT NOT NULL,
    PRIMARY KEY (mailbox_id, folder_id))"""
# Messages a capture round listed but the people guard could not decide (no complete groups
# listing, or a list whose members cannot be read): their ids only, re-read by a later
# round once the guard can decide them (``MailCapture._retry_undecided``). Nothing else
# of such a message is kept. Applied when the first such message is held (not at open).
# Messages an excluded person sent or received, by the SHA-256 of their internetMessageId
# only (``graph_read.withheld_message_key``): no mailbox stores any copy of one (made with
# the first one remembered).
_WITHHELD_DDL = """CREATE TABLE IF NOT EXISTS people_withheld (
    message_key TEXT PRIMARY KEY,
    withheld_at TEXT NOT NULL)"""
# The L1 handles (messages, attachments) a capture purge deleted whose derived copies (the
# ontology stores, Postgres, search) are still to go: ``operating.mail_exclusion_purge``
# removes those and forgets the handles. Handles only (opaque ids, never an address).
# Made with the first purge that deletes anything.
_PURGED_DDL = """CREATE TABLE IF NOT EXISTS purged_handles (
    l1_handle TEXT PRIMARY KEY,
    purged_at TEXT NOT NULL)"""
_UNDECIDED_DDL = """CREATE TABLE IF NOT EXISTS people_undecided (
    mailbox_id TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    held_at TEXT NOT NULL,
    tried_at TEXT,
    PRIMARY KEY (mailbox_id, message_id))"""
_PRIORITY_DDL = (
    "CREATE TABLE IF NOT EXISTS priority_folders (mailbox_id TEXT NOT NULL, folder_id TEXT "
    "NOT NULL, oldest TEXT, newest TEXT, PRIMARY KEY (mailbox_id, folder_id))",
    "CREATE TABLE IF NOT EXISTS priority_senders (mailbox_id TEXT NOT NULL, folder_id TEXT "
    "NOT NULL, sender TEXT NOT NULL, automated INTEGER NOT NULL, n INTEGER NOT NULL, "
    "PRIMARY KEY (mailbox_id, folder_id, sender))")
