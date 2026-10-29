"""Configuration of the Postgres platform: DSN and the embedding model's dimension.

``PLX_PG_DSN`` names the database (default ``postgresql:///plx``: the local unix socket,
peer auth as the OS user). ``PLX_EMBED_MODEL`` names the embedding model (default
``BAAI/bge-m3``); its dimension comes from ``EMBED_DIMS``, or from ``PLX_EMBED_DIM`` for a
model not listed. A model with no known dimension is refused (no guessed vector size).
"""
from __future__ import annotations

import os
from typing import Mapping

DEFAULT_DSN = "postgresql:///plx"
DEFAULT_EMBED_MODEL = "BAAI/bge-m3"
# Output dimension of each embedding model the platform knows. halfvec HNSW indexes take
# up to 4000 dimensions, so every model here is indexable.
EMBED_DIMS: Mapping[str, int] = {
    "BAAI/bge-m3": 1024,
    "intfloat/e5-large-v2": 1024,
    "e5-large-v2": 1024,
    "openai:text-embedding-3-large": 3072,
    "openai:text-embedding-3-small": 1536,
}
MAX_HALFVEC_INDEX_DIM = 4000


def dsn(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("PLX_PG_DSN") or DEFAULT_DSN


def embed_model(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("PLX_EMBED_MODEL") or DEFAULT_EMBED_MODEL).strip()


def embed_dim(model: str | None = None, env: Mapping[str, str] | None = None) -> int:
    """Dimension of ``model`` (default the configured one). ``PLX_EMBED_DIM`` overrides
    only for a model ``EMBED_DIMS`` does not list, and must agree when it does."""
    env = os.environ if env is None else env
    model = model or embed_model(env)
    raw = (env.get("PLX_EMBED_DIM") or "").strip()
    override = None
    if raw:
        if not raw.isdigit():
            raise ValueError(f"embed_dim_invalid:{raw!r}")
        override = int(raw)
    known = EMBED_DIMS.get(model)
    if known is not None and override is not None and override != known:
        raise ValueError(f"embed_dim_conflicts_with_model:{model}:{known}!={override}")
    dim = known if known is not None else override
    if dim is None:
        raise ValueError(f"embed_dim_unknown_for_model:{model}")
    if not 1 <= dim <= MAX_HALFVEC_INDEX_DIM:
        raise ValueError(f"embed_dim_out_of_range:{dim}")
    return dim
