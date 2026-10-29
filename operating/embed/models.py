"""Embedding providers behind one switch (``PLX_EMBED_MODEL``).

* ``BAAI/bge-m3`` (default; local, dense 1024): sentence-transformers on CPU in bfloat16
  (the Xeon's AMX units: ~5x fp32 throughput, cosine to the fp32 vector >= 0.998 measured),
  ``PLX_EMBED_THREADS`` torch threads (default: the physical cores).
* ``openai:text-embedding-3-large`` (approved by the owner 2026-10-29, off until credits;
  ``-small`` too):
  remote, so it runs only with ``PLX_EMBED_ALLOW_REMOTE=1`` set in ``plx.env`` or the
  environment, and ``OPENAI_API_KEY``. Batches of at most ``OPENAI_MAX_INPUTS`` inputs and
  ``OPENAI_MAX_TOKENS`` estimated tokens, retry with backoff on 429/5xx (Retry-After
  honoured), the one allowlisted sender (``operating.embed_sender``). ``dimensions``
  shortens the vector (Matryoshka) when the store's column is narrower.
* ``intfloat/e5-large-v2`` (local, 1024): ``passage: `` / ``query: `` prefixes.

Every vector is L2-normalised (cosine = dot). Nothing here logs text or the key.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np

OPENAI_MODELS = {"text-embedding-3-large": 3072, "text-embedding-3-small": 1536}
LOCAL_MODELS = {"BAAI/bge-m3": 1024, "intfloat/e5-large-v2": 1024}
OPENAI_MAX_INPUTS = 2048
OPENAI_MAX_TOKENS = 250_000          # per request (the API's limit is 300k)
OPENAI_MAX_INPUT_TOKENS = 8000       # per input (8191); longer inputs are cut
OPENAI_PRICE_PER_MTOK = {"text-embedding-3-large": 0.13, "text-embedding-3-small": 0.02}
LOCAL_MAX_CHARS = 6000               # ~1500 tokens: chunks are short; a cap, not a window


class RemoteRefused(RuntimeError):
    """Remote embedding asked for without ``PLX_EMBED_ALLOW_REMOTE=1`` or a key."""


def estimate_tokens(text: str) -> int:
    """A conservative token estimate (3 characters a token; cl100k averages ~4)."""
    return len(text) // 3 + 1


@dataclass
class Embedder:
    name: str
    dim: int
    _encode: Callable[[Sequence[str]], np.ndarray]
    remote: bool = False

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out = np.asarray(self._encode(list(texts)), dtype=np.float32)
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


def _local(model: str, settings: Mapping[str, str]) -> Embedder:
    os.environ.setdefault("HF_HOME", settings.get("HF_HOME") or "/data/models")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "true")
    import torch
    from sentence_transformers import SentenceTransformer
    threads = int(settings.get("PLX_EMBED_THREADS") or max(1, (os.cpu_count() or 8) // 2))
    torch.set_num_threads(threads)
    st = SentenceTransformer(model, device="cpu")
    st.max_seq_length = 512
    if settings.get("PLX_EMBED_PRECISION", "bf16") == "bf16":
        st = st.to(torch.bfloat16)
    prefix = "passage: " if model.startswith("intfloat/e5") else ""
    batch = int(settings.get("PLX_EMBED_BATCH") or 16)

    def encode(texts: Sequence[str]) -> np.ndarray:
        cut = [prefix + t[:LOCAL_MAX_CHARS] for t in texts]
        with torch.inference_mode():
            out = st.encode(cut, batch_size=batch, normalize_embeddings=True,
                            convert_to_tensor=True, show_progress_bar=False)
            return out.float().cpu().numpy()

    return Embedder(model, LOCAL_MODELS[model], encode)


def _openai(model: str, settings: Mapping[str, str], dimensions: int | None,
            post: Callable[..., bytes] | None = None,
            sleep: Callable[[float], None] | None = None) -> Embedder:
    if settings.get("PLX_EMBED_ALLOW_REMOTE") != "1":
        raise RemoteRefused("remote_embedding_not_allowed: set PLX_EMBED_ALLOW_REMOTE=1")
    key = settings.get("OPENAI_API_KEY")
    if not key:
        raise RemoteRefused("openai_api_key_absent")
    from operating import embed_sender
    send = post or embed_sender.post_embeddings
    dim = dimensions or OPENAI_MODELS[model]

    def request(batch: Sequence[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": model, "input": list(batch), "encoding_format": "float"}
        if dimensions:
            body["dimensions"] = dimensions
        payload = json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        delay = 2.0
        for attempt in range(8):
            try:
                raw = send(embed_sender.API_URL, headers, payload, 120.0)
                data = json.loads(raw)
                rows = sorted(data["data"], key=lambda r: r["index"])
                if len(rows) != len(batch):
                    raise RuntimeError("openai_embedding_count_mismatch")
                return [r["embedding"] for r in rows]
            except embed_sender.TransportError as error:
                retryable = (error.status in (429, 500, 502, 503, 504)
                             or error.status is None and str(error) == "network_error") \
                    and error.error_code != "insufficient_quota"   # no credits: no retry
                if not retryable or attempt == 7:
                    raise
                (sleep or time.sleep)(error.retry_after if error.retry_after else delay)
                delay = min(delay * 2, 60.0)
        raise RuntimeError("unreachable")

    def encode(texts: Sequence[str]) -> np.ndarray:
        out: list[list[float]] = []
        batch: list[str] = []
        tokens = 0
        for text in texts:
            text = text[: OPENAI_MAX_INPUT_TOKENS * 3] or " "
            cost = estimate_tokens(text)
            if batch and (len(batch) >= OPENAI_MAX_INPUTS or tokens + cost > OPENAI_MAX_TOKENS):
                out.extend(request(batch))
                batch, tokens = [], 0
            batch.append(text)
            tokens += cost
        if batch:
            out.extend(request(batch))
        return np.asarray(out, dtype=np.float32)

    return Embedder(f"openai:{model}" + (f"@{dimensions}" if dimensions else ""), dim, encode,
                    remote=True)


def load(settings: Mapping[str, str], *, column_dim: int | None = None,
         post: Callable[..., bytes] | None = None) -> Embedder:
    """The configured embedder. A remote model shortens its vectors to ``column_dim`` when
    the column is narrower; a local model narrower than the column is zero-padded by the
    store (cosine unchanged), never truncated."""
    name = settings.get("PLX_EMBED_MODEL") or "BAAI/bge-m3"
    if name.startswith("openai:"):
        model = name.split(":", 1)[1]
        if model not in OPENAI_MODELS:
            raise ValueError(f"embed_model_unknown:{name}")
        wanted = settings.get("PLX_EMBED_DIMENSIONS")
        dims = int(wanted) if wanted else (column_dim if column_dim and column_dim <
                                           OPENAI_MODELS[model] else None)
        return _openai(model, settings, dims, post=post)
    if name in LOCAL_MODELS:
        return _local(name, settings)
    raise ValueError(f"embed_model_unknown:{name}")


_WORKER: Embedder | None = None


def worker_init(settings: Mapping[str, str]) -> None:
    """A worker process's model (``python3 -m operating.embed embed --workers N``)."""
    global _WORKER
    _WORKER = load(settings)


def worker_encode(texts: Sequence[str]) -> np.ndarray:
    if _WORKER is None:
        raise RuntimeError("worker_not_initialised")
    return _WORKER.encode(texts)


def cost_estimate(texts_chars: int, model: str) -> dict[str, Any]:
    tokens = texts_chars // 4
    price = OPENAI_PRICE_PER_MTOK.get(model.split(":", 1)[-1].split("@")[0])
    return {"tokens_estimate": tokens,
            "usd_estimate": round(tokens / 1e6 * price, 2) if price is not None else 0.0}


__all__ = ["Embedder", "LOCAL_MODELS", "OPENAI_MODELS", "RemoteRefused", "cost_estimate",
           "estimate_tokens", "load"]
