"""The collation a byte-order range read of an ontology key names (GROWTH-AUDIT-1111 F7).

In ``ontology`` (the lowest layer) so the in-place step code and the ``operating.pg``
readers share it without an upward import.

Key ranges (``link_id``, ``object_id``) are cut in byte order: the bounds come from Python
(``sorted(..., key=str.encode)``) or from step prefixes, and a range read must return each
row in exactly one part. Where the database's default collation already is byte order
(libc ``C``/``POSIX``/``C.UTF-8``: code point order, which for UTF-8 is byte order;
builtin ``C``/``C.UTF-8``), the plain key indexes (``link_rows_pkey``,
``object_versions_by_object``) serve the range without a ``COLLATE "C"``, and their
byte-order twins (``link_rows_by_id``, ``object_versions_by_object_c``, 10.7 GB on the
served database) are dropped by ``migrations/0018``. Anywhere else, or when it cannot be
told, the read names ``COLLATE "C"`` (correct; unindexed once the twins are gone)."""
from __future__ import annotations

from typing import Any
import weakref

BYTE = ' COLLATE "C"'
# provider ('c' libc, 'b' builtin) -> default collations ordering text by bytes
_BYTE_ORDER = {"c": {"C", "POSIX", "C.UTF-8", "C.utf8"}, "b": {"C", "C.UTF-8"}}
# the key columns a range reads, each of the database's default collation (oid 100)
_KEYS = (("ontology.link_rows", "link_id"), ("ontology.object_versions", "object_id"),
         ("ontology.events", "event_id"))
_KNOWN: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()


def key_collate(target: Any) -> str:
    """``""`` when a range predicate on the ontology keys is in byte order as written
    (the database's default collation is byte order and the key columns use it), else
    ``' COLLATE "C"'``. ``target``: a psycopg connection or cursor (read with queries
    that cannot fail, so a caller's transaction is never aborted); cached per
    connection."""
    conn = getattr(target, "connection", target)
    try:
        return _KNOWN[conn]
    except (KeyError, TypeError):
        pass
    row = target.execute(
        "SELECT d.datlocprovider::text, d.datcollate, to_jsonb(d) ->> 'datlocale', "
        "(SELECT count(*) FROM pg_attribute a WHERE a.attnum > 0 AND NOT a.attisdropped "
        "AND (a.attrelid, a.attname) IN (" + ", ".join(
            f"(to_regclass('{table}'), '{column}')" for table, column in _KEYS)
        + ") AND a.attcollation = 100) FROM pg_database d "
        "WHERE d.datname = current_database()").fetchone()
    out = BYTE
    if row is not None:
        provider, collate, locale, plain = row
        name = locale if provider == "b" else collate
        if name in _BYTE_ORDER.get(provider, ()) and plain == len(_KEYS):
            out = ""
    try:
        _KNOWN[conn] = out
    except TypeError:
        pass   # not weakly referable: asked again next time
    return out
