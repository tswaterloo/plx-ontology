"""Declared schema of the per-person digest report store (``operating.digests.ReportStore``)."""

STORE = "digest_reports"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS report (owner TEXT NOT NULL, rid TEXT NOT NULL, ran_at REAL NOT NULL, text TEXT NOT NULL, PRIMARY KEY (owner, rid))",
)),)
