"""The learning ledger's lessons into each person's memory (``operator_api.memory``), from the
offline distil job: thumbs and corrections on Ask answers become core-memory facts and
preferences the Ask reads (``operating.learning``: a corrected answer is a ``fact`` lesson,
answers rated up or down by system a ``source`` preference, draft style a preference).

``operating`` never imports ``operator_api`` (tools/layer_contract.py): this module registers
the writer on ``learning.MEMORY_FACTORY`` and runs the learning command line.

**Whose memory.** A lesson is per person (the Entra object id). Memory items are keyed by
the reader key the person's own Ask sessions use (``Reader.key``: their principals), which
an object id alone does not rebuild (it carries their groups), so the writer takes the key
the person's own memory already uses (``memory.log``/``memory.item`` rows whose key names
``employee:<oid>``; exactly one, or the newest when their groups changed) and writes nothing
for a person who has never used the Ask (``no_reader_key``). The memory store applies its
own exclusion, finance and pay checks on every write (``Memory.remember``); the learning
ledger already dropped excluded people and finance signals before (fail closed both ways).

    python3 -m operator_api.learning_memory distil --state-dir DIR [--store STORE]
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Optional, Sequence

from operating import learning

DSN_ENV = "PLX_PG_DSN"


class OfflineWriter:
    """``remember_for(person_oid, **item)`` over a ``Memory``: the item under the reader key
    the person's memory uses (module doc), None when they have none or it is refused."""

    def __init__(self, memory: Any, keys: Optional[Callable[[str], Optional[str]]] = None
                 ) -> None:
        self.memory = memory
        self._keys = keys or self._key_from_memory
        self.skipped: dict[str, int] = {}

    def _key_from_memory(self, oid: str) -> Optional[str]:
        like = f"%employee:{oid}%"
        with self.memory.connect() as conn:
            rows = conn.execute(
                "SELECT reader, max(at) FROM (SELECT reader, at FROM memory.log WHERE reader "
                "LIKE %s UNION ALL SELECT reader, at FROM memory.item WHERE reader LIKE %s) x "
                "GROUP BY reader ORDER BY max(at) DESC", (like, like)).fetchall()
        for key, _at in rows:
            if learning.person_id(key) == oid:
                return str(key)
        return None

    def remember_for(self, person: str, **item: Any) -> Optional[int]:
        oid = learning.person_id(person)
        if not oid or "employee:" in oid or "," in oid:
            self.skipped["not_a_person_id"] = self.skipped.get("not_a_person_id", 0) + 1
            return None
        try:
            key = self._keys(oid)
        except Exception:  # noqa: BLE001 - the memory unreadable: nothing written
            key = None
        if not key:
            self.skipped["no_reader_key"] = self.skipped.get("no_reader_key", 0) + 1
            return None
        from operator_api.memory import KeyReader
        return self.memory.remember(KeyReader(key), **item)


def factory(environ: Optional[dict] = None) -> Optional[Callable[[], Any]]:
    """The ``learning.MEMORY_FACTORY`` for this process: the memory over the env DSN, None
    when no Postgres is configured or the memory store is not deployed."""
    env = os.environ if environ is None else environ
    dsn = env.get(DSN_ENV)
    if not dsn:
        return None

    def make() -> Any:
        from operator_api.memory import Memory
        return OfflineWriter(Memory.for_dsn(dsn, sync=True))
    return make


def main(argv: Sequence[str] | None = None) -> int:
    learning.MEMORY_FACTORY = factory()
    return learning.main(argv)


if __name__ == "__main__":
    sys.exit(main())
