"""Two small sqlite helpers shared by ``ontology.delta`` and the modules split out of it."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Sequence


def _ro(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _chunks(items: Sequence[Any], size: int = 500) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]
