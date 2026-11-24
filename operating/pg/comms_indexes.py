"""Indexes of the communications tables that only make reads faster (Fable spec 15.3 step 3,
11-23 Ask run): no column, row or view changes, so no comms fingerprint moves.

Read by the table build (``business_comms.write_table`` creates them on the new table) and by
``generic_records.ensure_indexes`` (adds any the live table lacks, ``CONCURRENTLY``, one at a
time, after the disk check). One list: a second copy would drift."""
from __future__ import annotations

# (table, method, key), as ``generic_records.INDEXES``
INDEXES = (
    # a subject / name / folder found by a word fragment (``ILIKE '%croda%'``, and the OR of
    # it with a party's GIN): a trigram index, never a read of every row in the window
    ("emails", "gin", "subject gin_trgm_ops"),
    ("attachments", "gin", "name gin_trgm_ops"),
    ("sharepoint_files", "gin", "name gin_trgm_ops"),
    ("sharepoint_files", "gin", "path gin_trgm_ops"),
    # a sender's domain or a mailbox's mail newest first, in a window
    ("emails", "btree", "sender_domain, received_at"),
    ("emails", "btree", "mailbox, received_at"),
)


def for_table(table: str) -> tuple[tuple[str, str], ...]:
    """(method, key) of each index ``table`` holds."""
    return tuple((m, k) for t, m, k in INDEXES if t == table)
