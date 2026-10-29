"""Postgres 17 + pgvector platform of the Workbench search (schema ``search``).

- ``config``: DSN (``PLX_PG_DSN``, default ``postgresql:///plx``) and embedding dimension.
- ``pool``: a small connection pool; every connection gets the pgvector 0.8 iterative scan
  defaults (``search_defaults``).
- ``migrate``: ordered SQL migrations (``migrations/NNNN_*.sql``).
- ``schema_check``: the database against the contract.
- ``permissions``: a reader's ReadPolicy as the SQL permission filter (fail closed), the
  people exclusion, and hidden-match counts that name only shared owners.
- ``dense``: permission-filtered k-NN (HNSW iterative scan, exact fallback).

CLI: ``python3 -m operating.pg migrate|status|check|set-embedding``.
"""
from operating.pg.config import dsn
from operating.pg.pool import Pool, connect, search_defaults

__all__ = ["Pool", "connect", "dsn", "search_defaults"]
