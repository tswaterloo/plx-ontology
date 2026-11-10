"""Declared schema of the incremental projection objects file (see ``operating.declared_schema``)."""

STORE = "project_objects"
STEPS = ((1, "initial", """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS bindings (binding TEXT PRIMARY KEY, key TEXT,
                basis TEXT, objects INTEGER);
            CREATE TABLE IF NOT EXISTS objects (binding TEXT, object_id TEXT, print BLOB,
                part TEXT, PRIMARY KEY (binding, object_id)) WITHOUT ROWID;
"""),)
