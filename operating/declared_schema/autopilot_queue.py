"""Declared schema of the autopilot queue store (see ``operating.declared_schema``)."""

STORE = "autopilot_queue"
STEPS = ((1, "initial", """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, object_id TEXT NOT NULL,
                object_type TEXT NOT NULL, at TEXT, landed_at TEXT, seen_at TEXT NOT NULL,
                body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'new', reason TEXT,
                processed_at TEXT);
            CREATE INDEX IF NOT EXISTS events_state ON events(state);
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, autopilot TEXT NOT NULL,
                owner TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                audiences TEXT NOT NULL, markings TEXT NOT NULL, citations TEXT NOT NULL,
                due_at TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL,
                done_reason TEXT, done_at TEXT, prepare_ms INTEGER, latency_s REAL);
            CREATE INDEX IF NOT EXISTS items_owner ON items(owner, state);
            CREATE TABLE IF NOT EXISTS rate (owner TEXT, hour TEXT, n INTEGER,
                PRIMARY KEY (owner, hour));
            CREATE TABLE IF NOT EXISTS layers (layer TEXT PRIMARY KEY, base_digest TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"""),
 (2, "sent_inbound", """
            CREATE TABLE IF NOT EXISTS sent (owner TEXT NOT NULL, thread TEXT NOT NULL,
                counterpart TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY (owner, thread, counterpart, at));
            CREATE INDEX IF NOT EXISTS sent_counterpart ON sent(owner, counterpart, at);
            CREATE TABLE IF NOT EXISTS inbound (owner TEXT NOT NULL, thread TEXT NOT NULL,
                counterpart TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY (owner, thread, counterpart, at));
            CREATE INDEX IF NOT EXISTS inbound_counterpart ON inbound(owner, counterpart, at);"""),
 (3, "routine", """
            CREATE TABLE IF NOT EXISTS routine (owner TEXT NOT NULL, series TEXT NOT NULL,
                at TEXT NOT NULL, message TEXT NOT NULL, recipients TEXT NOT NULL,
                greeted INTEGER NOT NULL,
                PRIMARY KEY (owner, series, at));"""),)
