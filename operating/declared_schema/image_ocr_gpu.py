"""Declared schema of the GPU OCR gate's store (see ``operating.declared_schema``)."""

STORE = "image_ocr_gpu"
STEPS = ((1, "initial", (
    "CREATE TABLE IF NOT EXISTS plan (content_sha256 TEXT PRIMARY KEY, "
    "planned_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS readings (content_sha256 TEXT NOT NULL, "
    "engine_key TEXT NOT NULL, status TEXT NOT NULL, text TEXT, "
    "pages INTEGER NOT NULL, truncated INTEGER NOT NULL, "
    "read_at REAL NOT NULL, PRIMARY KEY (content_sha256, engine_key))",
    "CREATE TABLE IF NOT EXISTS refused (content_sha256 TEXT NOT NULL, "
    "engine_key TEXT NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL, "
    "PRIMARY KEY (content_sha256, engine_key))",
)),)
