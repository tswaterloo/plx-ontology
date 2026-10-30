"""The stable id of a registry gap."""
from __future__ import annotations

import hashlib


def gap_id(source: str, key: str) -> str:
    return "gap-" + hashlib.sha256(f"{source}\0{key}".encode()).hexdigest()[:12]
