"""The local models search runs: the query embedder (the same model the chunks were
embedded with, ``PLX_EMBED_MODEL``) and the cross-encoder reranker
(``PLX_RERANK_MODEL``, default ``BAAI/bge-reranker-v2-m3``).

Both load lazily from the local model cache only (``HF_HOME``, default
``/data/models``; never a download at query time), once per process. The reranker runs in
bfloat16 on CPU (AMX: 50 pairs in about 2 s on this host against 5 s in float32) under a
time box: a rerank that does not finish in time is dropped whole and the query is ranked
without it (stated in ``SearchResult.notes``).

``PLX_RERANK=off`` turns the reranker off. The query embedder is the (``operating.embed.models.load``) when present, so a query is embedded by the model, name
and guards the chunks were; the local fallback refuses ``openai:*``.
"""
from __future__ import annotations

import concurrent.futures as cf
import inspect
import os
import threading
from typing import Any, Callable, Protocol, Sequence

DEFAULT_EMBED_MODEL = "BAAI/bge-m3"
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
RERANK_MAX_LENGTH = 384
RERANK_BATCH = 10


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    """``score``: each passage's relevance to the query in 0..1."""
    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


def _cache_dir() -> str:
    return os.path.join(os.environ.get("HF_HOME") or "/data/models", "hub")


def embed_model_name() -> str:
    return os.environ.get("PLX_EMBED_MODEL") or DEFAULT_EMBED_MODEL


class LocalEmbedder:
    """sentence-transformers over a cached local model; vectors L2-normalised. An
    ``e5`` model gets its ``query: `` prefix."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or embed_model_name()
        if self.model.startswith("openai:"):
            raise RuntimeError("embed_model_not_local:" + self.model)
        self._st: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._st is None:
                from sentence_transformers import SentenceTransformer
                self._st = SentenceTransformer(self.model, device="cpu",
                                               cache_folder=_cache_dir(),
                                               local_files_only=True)
            return self._st

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        prefix = "query: " if "e5" in self.model.lower() else ""
        vectors = self._load().encode([prefix + t for t in texts], batch_size=8,
                                      normalize_embeddings=True, convert_to_numpy=True)
        return [list(map(float, v)) for v in vectors]


class LocalReranker:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("PLX_RERANK_MODEL") or DEFAULT_RERANK_MODEL
        self._ce: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._ce is None:
                import torch
                from sentence_transformers import CrossEncoder
                self._ce = CrossEncoder(self.model, max_length=RERANK_MAX_LENGTH,
                                        device="cpu", cache_folder=_cache_dir(),
                                        local_files_only=True,
                                        model_kwargs={"torch_dtype": torch.bfloat16})
            return self._ce

    @property
    def loaded(self) -> bool:
        return self._ce is not None

    def warm(self) -> None:
        self._load()

    def score(self, query: str, passages: Sequence[str],
              should_stop: Callable[[], bool] | None = None) -> list[float]:
        """Relevance per passage, in batches of ``RERANK_BATCH``; ``should_stop`` is
        asked between batches (a timed-out call stops within one batch, so the worker is
        free for the next query)."""
        if not passages:
            return []
        model = self._load()
        out: list[float] = []
        for start in range(0, len(passages), RERANK_BATCH):
            if should_stop is not None and should_stop():
                raise TimeoutError("rerank_cancelled")
            batch = passages[start:start + RERANK_BATCH]
            # The model's own activation (sigmoid): relevance in 0..1, as ``Reranker`` says.
            scores = model.predict([(query, p) for p in batch], batch_size=len(batch),
                                   convert_to_numpy=True)
            out.extend(float(x) for x in scores)
        return out


class TimeBoxed:
    """Runs a reranker on one worker thread with a time box. A call made while an
    earlier one is still running (it timed out) is refused at once (``busy``)."""

    def __init__(self, reranker: Reranker) -> None:
        self.reranker = reranker
        self._pool = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="plx-rerank")
        self._running: cf.Future | None = None
        self._lock = threading.Lock()

    def warm(self, wait: bool = False) -> None:
        """Load the model on the worker (a host calls this at start): the first query
        never pays the load inside its time box."""
        warm = getattr(self.reranker, "warm", None)
        if warm is None:
            return
        with self._lock:
            if self._running is None or self._running.done():
                self._running = self._pool.submit(warm)
            future = self._running
        if wait:
            future.result()

    def score(self, query: str, passages: Sequence[str], timeout: float
              ) -> tuple[list[float] | None, str | None]:
        if getattr(self.reranker, "loaded", True) is False:
            self.warm()   # loads in the background; this query ranks without it
            return None, "reranker_loading"
        stop = threading.Event()
        with self._lock:
            if self._running is not None and not self._running.done():
                return None, "reranker_busy"
            if "should_stop" in inspect.signature(self.reranker.score).parameters:
                future = self._pool.submit(self.reranker.score, query, list(passages),
                                           stop.is_set)
            else:
                future = self._pool.submit(self.reranker.score, query, list(passages))
            self._running = future
        try:
            return future.result(timeout=timeout), None
        except cf.TimeoutError:
            stop.set()   # the worker stops after its current batch
            return None, "reranker_timeout"
        except Exception as exc:  # noqa: BLE001 - the query ranks without it
            return None, "reranker_error:" + type(exc).__name__


_DEFAULTS: dict[str, Any] = {}
_DEFAULTS_LOCK = threading.Lock()


def _default(name: str, factory: Callable[[], Any]) -> Any:
    with _DEFAULTS_LOCK:
        if name not in _DEFAULTS:
            try:
                _DEFAULTS[name] = factory()
            except Exception:  # noqa: BLE001 - not installed / not cached: off
                _DEFAULTS[name] = None
        return _DEFAULTS[name]


class LaneBEmbedder:
    """the configured embedder (``operating.embed.models.load``: the same switch,
    model name and guards the chunks were embedded under, a remote model only where its
    owner switch allows it) used for the query."""

    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder
        self.model = embedder.name
        # the ``encode`` writes passages (an e5 model's ``passage: `` prefix); an e5
        # query needs ``query: `` alone, so it is embedded by the same local model here.
        self._query = (LocalEmbedder(self.model)
                       if "e5" in self.model.lower()
                       and getattr(embedder, "encode_query", None) is None else None)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._query is not None:
            return self._query.embed(texts)
        query = getattr(self._embedder, "encode_query", None)
        vectors = query(list(texts)) if query is not None else self._embedder.encode(
            list(texts))
        return [list(map(float, v)) for v in vectors]


def default_embedder() -> Embedder | None:
    """the configured embedder when it ships (``operating.embed.models.load``),
    else the local model of ``PLX_EMBED_MODEL``; None when neither is available (the
    dense retriever is then skipped and the result says so)."""
    def make() -> Any:
        try:
            from operating.embed.models import load  # type: ignore[import-not-found]
        except ImportError:
            return LocalEmbedder()
        return LaneBEmbedder(load(os.environ))
    return _default("embedder:" + embed_model_name(), make)


def default_reranker() -> TimeBoxed | None:
    if (os.environ.get("PLX_RERANK") or "on").lower() in ("off", "0", "false", "no"):
        return None
    return _default("reranker", lambda: TimeBoxed(LocalReranker()))


_WARMING: dict[str, threading.Thread] = {}


def warm(*, wait: bool = False, timeout: float | None = None) -> dict[str, Any]:
    """Load the query embedder and the reranker of this process now (a host or API calls
    this at start, an eval before it measures), so no query pays a load or ranks without
    the reranker because it was still loading (``reranker_loading``). ``wait``: block
    until both are loaded (at most ``timeout`` s); else they load on a background thread.
    Returns what is ready: ``{"embedder": bool, "reranker": bool | None}`` (None: the
    reranker is switched off). A model that cannot load stays off, as at query time."""
    def run() -> None:
        embedder = default_embedder()
        if embedder is not None:
            try:
                embedder.embed(["warm"])
            except Exception:  # noqa: BLE001 - dense is skipped at query time, stated
                pass
        reranker = default_reranker()
        if reranker is not None:
            try:
                reranker.warm(wait=True)
            except Exception:  # noqa: BLE001 - the query ranks without it, stated
                pass
    with _DEFAULTS_LOCK:
        thread = _WARMING.get("models")
        if thread is None:
            thread = threading.Thread(target=run, name="plx-search-warm", daemon=True)
            _WARMING["models"] = thread
            thread.start()
    if wait:
        thread.join(timeout)
    off = (os.environ.get("PLX_RERANK") or "on").lower() in ("off", "0", "false", "no")
    reranker = _DEFAULTS.get("reranker")
    return {"embedder": _DEFAULTS.get("embedder:" + embed_model_name()) is not None
            and not thread.is_alive(),
            "reranker": None if off else bool(
                reranker is not None and getattr(reranker.reranker, "loaded", True))}
