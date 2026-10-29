"""Embedding of ``search.chunks``: ``python3 -m operating.embed run``.

``config`` (settings, never printed), ``models`` (the ``PLX_EMBED_MODEL`` switch: OpenAI
behind ``PLX_EMBED_ALLOW_REMOTE=1``, local bge-m3 / e5), ``store`` (Postgres writes,
content-hash incremental), ``__main__`` (the run). Chunking is ``operating.chunking``.
"""
