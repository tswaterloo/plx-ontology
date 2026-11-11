"""Links added to a step input with their texts packed on every core.

``StoreBuilder.add("links", rows)`` packs each link's reason and evidence texts
(:func:`ontology.store.pack_link_texts`: two regex scans and a format check per link)
and interns the skeletons and slot texts, all in the builder's one process. Measured
11-10: the full-scale step input copy (~48M links) spent ~2.5 h at one core in that
packing. Here the packing runs in a pool of processes, a batch per task, and the
builder's process only interns the packed texts and inserts the rows, batch after batch
in the order given: the rows, row ids and interned ids ``add("links", ...)`` writes, the
same batches (each its own interning cache, as each ``add`` call has).

:class:`LinkWriter` takes the batches ``add`` would have been given; :meth:`LinkWriter.flush`
returns once every batch given is in the builder (before anything else reads or writes
the link tables). It adds through ``builder.add`` itself (one process, as before) when
the builder's ``_add_links``, ``pack_link_texts`` or ``_Interner`` is not the code this module follows
(:data:`ADD_LINKS_DIGEST`; a change there fails ``ontology.tests.test_link_pack`` until
:func:`add_packed` follows it), when the builder is not a plain ``StoreBuilder``, or with
fewer than two workers.

Why a module of its own: ``ontology.store``'s ``StoreBuilder`` is rows code and
derivation code (the kept fields' key, ``ontology.build_keys``); nothing either covers
imports this module.

Stdlib only.
"""

from __future__ import annotations

import collections
import hashlib
import inspect
import multiprocessing
from typing import Any, Mapping, Sequence

from ontology import store, store_ddl

# sha256 of the sources of ``StoreBuilder._add_links``, ``store.pack_link_texts`` and
# ``store._Interner`` (joined by newlines) that :func:`add_packed` follows.
ADD_LINKS_DIGEST = "86b18063803adef7ccac3786bf5b041b7c1c5699550a2479c6066b934241ef68"


def source_digest() -> str:
    return hashlib.sha256("\n".join(inspect.getsource(x) for x in (
        store.StoreBuilder._add_links, store.pack_link_texts, store._Interner)).encode()
        ).hexdigest()


def _pack(pairs: Sequence[tuple[Any, Any]]) -> list[Any]:
    """``pack_link_texts`` of each (reason, evidence) pair (a pool task)."""
    pack = store.pack_link_texts
    return [pack(r, e) for r, e in pairs]


class _Texts:
    """``store._Interner`` for one one-column table of texts (``link_texts`` or
    ``link_skeletons``): the same lookups, inserts and ids, its cache keyed by the text
    alone (for one ``add`` call only, as the interner's)."""

    def __init__(self, conn: Any, table: str) -> None:
        self.cache: dict[Any, int] = {}
        self._find = f"SELECT id FROM {table} WHERE text IS ? ORDER BY id LIMIT 1"
        self._make = f"INSERT INTO {table} (text) VALUES (?)"
        self.conn = conn

    def __call__(self, text: Any) -> int:
        found = self.cache.get(text)
        if found is None:
            hit = self.conn.execute(self._find, (text,)).fetchone()
            found = hit[0] if hit is not None else self.conn.execute(
                self._make, (text,)).lastrowid
            self.cache[text] = found
        return found


def add_packed(builder: Any, rows: Sequence[Mapping[str, Any]], packed: Sequence[Any]
               ) -> int:
    """``StoreBuilder._add_links(builder, rows)`` given ``packed``, each row's
    ``pack_link_texts(reason, evidence_handles)``: the same statements in the same order."""
    D = store_ddl
    if len(packed) != len(rows):
        raise ValueError("link_pack_count_differs")
    conn = builder.connection
    text_id, skeleton_id = _Texts(conn, "link_texts"), _Texts(conn, "link_skeletons")
    plain = [c for c in D.LINK_COLUMNS if c not in D.LINK_PACKED]
    empty = (None,) * D.LINK_SLOTS
    skeleton_of = D.skeleton_of
    out = []
    for row, got in zip(rows, packed):
        if got is not None:
            reason, evidence, texts, used = got
            slots = (*map(text_id, texts), *empty[len(texts):])
        else:   # stored as written: a skeleton without slots
            reason, evidence = (skeleton_of(t) if type(t) is str else t
                                for t in (row.get("reason"), row.get("evidence_handles")))
            slots, used = empty, 0
        out.append((*map(row.get, plain), skeleton_id(reason), skeleton_id(evidence),
                    used, *slots))
    names = (*plain, "reason_id", "evidence_id", "ek",
             *(f"s{k}" for k in range(D.LINK_SLOTS)))
    conn.executemany(
        f"INSERT INTO link_rows ({', '.join(names)}) VALUES "
        f"({', '.join('?' for _ in names)})", out)
    return len(out)


def follows(builder: Any) -> bool:
    """Whether ``builder`` adds links by the code :func:`add_packed` follows."""
    return (type(builder) is store.StoreBuilder
            and type(builder).add is store.StoreBuilder.add
            and source_digest() == ADD_LINKS_DIGEST)


class LinkWriter:
    """``builder.add("links", batch)`` for each batch given, in order, the packing done
    by ``workers`` processes (at most ``ahead`` batches packed or packing at once)."""

    def __init__(self, builder: Any, workers: int, *, ahead: int | None = None) -> None:
        self.builder = builder
        self.added = 0
        self._pool = None
        self._waiting: collections.deque = collections.deque()
        if workers >= 2 and follows(builder):
            # spawned, not forked: the builder's process holds SQLite connections and
            # threads a forked child must not inherit
            self._pool = multiprocessing.get_context("spawn").Pool(workers)
        self.ahead = ahead if ahead is not None else 2 * max(1, workers)
        self.parallel = self._pool is not None

    def add(self, rows: Sequence[Mapping[str, Any]]) -> None:
        rows = list(rows)
        if self._pool is None:
            self.added += self.builder.add("links", rows)
            return
        pairs = [(r.get("reason"), r.get("evidence_handles")) for r in rows]
        self._waiting.append((rows, self._pool.apply_async(_pack, (pairs,))))
        while len(self._waiting) > self.ahead:
            self._one()

    def _one(self) -> None:
        rows, task = self._waiting.popleft()
        self.added += add_packed(self.builder, rows, task.get())

    def flush(self) -> int:
        """Every batch given added; the links added so far."""
        while self._waiting:
            self._one()
        return self.added

    def close(self) -> None:
        self._waiting.clear()
        if self._pool is not None:
            self._pool.terminate()
            self._pool.join()
            self._pool = None

    def __enter__(self) -> "LinkWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if exc[0] is None:
                self.flush()
        finally:
            self.close()
