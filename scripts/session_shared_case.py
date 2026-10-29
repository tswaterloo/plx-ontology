"""Live check ``session.shared_case`` (the acceptance bar): concurrent sessions on one case, on a
fixture case store in a temporary directory, through the case store's own ``finding`` write,
``get`` read and ``read_tail`` open (the functions the MCP case tools call). Offline, no
client; prints counts and booleans only (no finding text, case id or session reference).
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ontology.core import Actor
from operating.cases import CaseStore, CitationError

ACTOR = Actor("plx", frozenset({"fixture"}))
ROW = "fixture://row/1@cap-1"
SESSIONS = 4
PER_SESSION = 10


def _resolver(handle: str, actor: Actor) -> dict[str, Any]:
    if handle != ROW:
        raise CitationError("unresolvable")
    return {"row": {"qty": 1}, "capture": "cap-1"}


def _writer(args: tuple[str, str, str, float]) -> int:
    path, case_id, session, start = args
    store = CaseStore(path, citation_resolver=_resolver)
    while time.time() < start:
        pass
    created = 0
    for n in range(PER_SESSION):
        created += not store.finding(ACTOR, case_id, f"shared {n}", "fact", "supported",
                                     [ROW])["merged"]
        store.finding(ACTOR, case_id, f"own {session} {n}", "interpretation", "unknown")
    return created


def measure() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.db"
        store = CaseStore(path, citation_resolver=_resolver)
        case_id = store.open(ACTOR, "Fixture question")
        first = store.finding(ACTOR, case_id, "Quantity is one", "fact", "supported", [ROW])
        second = store.finding(ACTOR, case_id, " quantity IS one ", "fact", "supported", [ROW])
        store.read_tail(ACTOR, case_id, "early")
        start = time.time() + 1.0
        with ProcessPoolExecutor(max_workers=SESSIONS) as pool:
            created = sum(pool.map(_writer, [(str(path), case_id, f"s{n}", start)
                                             for n in range(SESSIONS)]))
        view = store.get(ACTOR, case_id)
        statements = [f["statement"] for bucket in view["findings"].values() for f in bucket]
        expected = 1 + PER_SESSION + SESSIONS * PER_SESSION
        resumed = store.read_tail(ACTOR, case_id, "early")
        third = store.read_tail(ACTOR, case_id, "third")
        return {
            "schema": "plx-session-shared-case/v2", "blocked_reason": None,
            "sessions": SESSIONS + 3, "findings_expected": expected,
            "findings_stored": len(statements), "findings_distinct": len(set(statements)),
            "created_once_each": created == PER_SESSION, "chain_ok": view["integrity"]["chain_ok"],
            "duplicate_merged": bool(second["merged"] and not first["merged"]
                                     and second["finding_id"] == first["finding_id"]),
            "tail_read_on_open": len(third["tail"]) > 0 and third["unread_total"] == expected
                                 and not third["resumed"],
            "resumed_unread": resumed["resumed"] and resumed["unread_total"] == expected - 1}


def main() -> int:
    try:
        out = measure()
    except Exception as exc:  # noqa: BLE001 - the fixture could not be built or run
        out = {"schema": "plx-session-shared-case/v2", "blocked_reason": type(exc).__name__}
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
