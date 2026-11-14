"""The byte-order key collation, kept under its first name for callers written against it
(the helper itself is :mod:`ontology.pg_byte_order`, in the lowest layer, so the in-place
step code shares it without an upward import)."""
from ontology.pg_byte_order import BYTE, key_collate

__all__ = ["BYTE", "key_collate"]
