"""Declared schema of the labour_hours store (see ``operating.declared_schema``)."""

_DDL = """
CREATE TABLE IF NOT EXISTS labour_week (
    department TEXT NOT NULL, week TEXT NOT NULL, run TEXT NOT NULL DEFAULT '',
    hours TEXT NOT NULL, overtime_hours TEXT, overtime_basis TEXT, headcount_days INTEGER,
    week_basis TEXT NOT NULL, markings TEXT NOT NULL CHECK (markings = '["hr"]'),
    sources TEXT NOT NULL CHECK (json_valid(sources)),
    PRIMARY KEY (department, week, run));
CREATE TABLE IF NOT EXISTS labour_build (built_at TEXT NOT NULL, coverage TEXT NOT NULL);
"""

STORE = "labour_hours"
STEPS = ((1, "initial", (_DDL,)),)
