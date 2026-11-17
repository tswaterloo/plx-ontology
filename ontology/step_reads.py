"""Set-based reads shared by the post-project steps: one statement per batch of objects,
never one per object (NW-13).

A step read the rows of one object at a time: the pairs a person rejected (``contradicted``
links of an address), the leads of a document, the records a message names. At the
measured full scale (11-11, ``sender_parties``: 1,849 s, 1.65M reads, 62 % of the time in
Postgres reads) the round trips, not the rows, were the cost. Here each read takes a
batch of objects in one ``IN`` list and returns the rows grouped by object, in the order
an ordering key gives them, so the batched step writes what the per-object step wrote.

Stdlib only; the statements are the ones the store's two declared step-read indexes
(``operating/pg/migrations/0013_link_rows_step_reads.sql``) answer.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Iterator, Sequence

CHUNK = 500


def chunks(items: Sequence[Any], size: int | None = None) -> Iterator[Sequence[Any]]:
    size = size or CHUNK
    for start in range(0, len(items), size):
        yield items[start:start + size]


def query(conn: Any, sql: str, params: Sequence[Any]) -> list[Any]:
    from ontology import mentions

    return mentions._query(conn, sql, list(params)).fetchall()


def rejected_pairs(conn: Any, sources: Iterable[str]) -> dict[str, frozenset[tuple[str, str]]]:
    """source -> {(to_object, link_type)} of the links a person rejected (``contradicted``),
    one statement per :data:`CHUNK` sources. A source without one is absent."""
    found: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for part in chunks(sorted(set(sources))):
        for row in query(conn, "SELECT from_object, to_object, link_type FROM link_rows WHERE "
                               f"tier = 'contradicted' AND from_object IN "
                               f"({','.join('?' * len(part))})", part):
            found[row[0]].add((row[1], row[2]))
    return {k: frozenset(v) for k, v in found.items()}


def rows_by_object(conn: Any, objects: Sequence[str], sql: str, params: Sequence[Any] = (),
                   *, column: str = "from_object") -> dict[str, list[Any]]:
    """The rows of ``sql`` (a statement with its WHERE clause; ``AND <column> IN (...) ORDER
    BY link_id`` is appended) per object of ``objects``, one statement per :data:`CHUNK`
    objects. Objects without a row are absent."""
    out: dict[str, list[Any]] = defaultdict(list)
    for part in chunks(list(objects)):
        marks = ",".join("?" * len(part))
        for row in query(conn, f"{sql} AND {column} IN ({marks}) ORDER BY link_id",
                         [*params, *part]):
            out[row[column]].append(row)
    return out
