"""Declared schema of the page_counts store (see ``operating.declared_schema``)."""

STORE = "page_counts"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS page_counts (key TEXT PRIMARY KEY, "
                         "pages INTEGER NOT NULL)",
)),)
