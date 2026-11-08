"""The local models search runs: the query embedder (the same model the chunks were
embedded with, ``PLX_EMBED_MODEL``) and the cross-encoder reranker
(``PLX_RERANK_MODEL``, default ``BAAI/bge-reranker-v2-m3``).

Both load lazily from the local model cache only, in one process per host user
(``operating.search.model_server``, ``PLX_SEARCH_MODELS=shared``, the default) or in this
process (``local``) (``HF_HOME``, default
``/data/models``; never a download at query time), once per process. The reranker runs in
bfloat16 on CPU (AMX: 50 pairs in about 2 s on this host against 5 s in float32) under a
time box: a rerank that does not finish in time is dropped whole and the query is ranked
without it (stated in ``SearchResult.notes``).

``PLX_RERANK_ONNX`` (a directory with ``model.onnx`` and its tokenizer) runs the same
cross-encoder exported to ONNX and quantised to int8 instead (``OnnxReranker``).
``PLX_RERANK=off`` turns the reranker off. The query embedder is the (``operating.embed.models.load``) when present, so a query is embedded by the model, name
and guards the chunks were; the local fallback refuses ``openai:*``.
"""
from __future__ import annotations

import concurrent.futures as cf
import inspect
import os
import threading
import time
from typing import Any, Callable, Protocol, Sequence

DEFAULT_EMBED_MODEL = "BAAI/bge-m3"
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
RERANK_MAX_LENGTH = 256   # tokens: the 800-character passages (weights.json) fit
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
              should_stop: Callable[[], bool] | None = None,
              sink: list[float] | None = None) -> list[float]:
        """Relevance per passage, in batches of ``RERANK_BATCH``; ``should_stop`` is
        asked between batches (a timed-out call stops within one batch, so the worker is
        free for the next query); ``sink`` receives each batch's scores as it ends (what
        a timed-out call finished still counts)."""
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
            if sink is not None:
                sink.extend(float(x) for x in scores)
        return out


class OnnxReranker:
    """The same cross-encoder exported to ONNX and quantised to int8
    (``PLX_RERANK_ONNX``: a directory holding ``model.onnx`` and the tokenizer files),
    run by ONNX Runtime on ``PLX_RERANK_THREADS`` threads: on this host's AMX cores about
    1.6x faster than the bfloat16 model, with the same order (Spearman 0.993 on 240
    real query-passage pairs), so more candidates fit the time box."""

    def __init__(self, folder: str, threads: int | None = None) -> None:
        self.folder = folder
        self.threads = threads
        self._session: Any = None
        self._tokenizer: Any = None
        self._lock = threading.Lock()

    def _load(self) -> tuple[Any, Any]:
        with self._lock:
            if self._session is None:
                # onnxruntime posts OneDS telemetry to Microsoft from native code unless
                # this is set before its import; overridden, never defaulted (as doc_convert).
                os.environ["ORT_DISABLE_TELEMETRY"] = "1"
                import onnxruntime as ort
                from transformers import AutoTokenizer
                options = ort.SessionOptions()
                options.intra_op_num_threads = int(
                    self.threads or os.environ.get("PLX_RERANK_THREADS")
                    or max(1, (os.cpu_count() or 8) // 2))
                self._tokenizer = AutoTokenizer.from_pretrained(self.folder,
                                                                local_files_only=True)
                self._session = ort.InferenceSession(os.path.join(self.folder, "model.onnx"),
                                                     options,
                                                     providers=["CPUExecutionProvider"])
            return self._session, self._tokenizer

    @property
    def loaded(self) -> bool:
        return self._session is not None

    def warm(self) -> None:
        self._load()

    def score(self, query: str, passages: Sequence[str],
              should_stop: Callable[[], bool] | None = None,
              sink: list[float] | None = None) -> list[float]:
        import numpy as np
        if not passages:
            return []
        session, tokenizer = self._load()
        out: list[float] = []
        for start in range(0, len(passages), RERANK_BATCH):
            if should_stop is not None and should_stop():
                raise TimeoutError("rerank_cancelled")
            batch = list(passages[start:start + RERANK_BATCH])
            enc = tokenizer([query] * len(batch), batch, padding=True, truncation=True,
                            max_length=RERANK_MAX_LENGTH, return_tensors="np")
            logits = session.run(None, {
                "input_ids": enc["input_ids"].astype(np.int64),
                "attention_mask": enc["attention_mask"].astype(np.int64)})[0][:, 0]
            batch_scores = [float(x) for x in 1.0 / (1.0 + np.exp(-logits))]
            out.extend(batch_scores)
            if sink is not None:
                sink.extend(batch_scores)
        return out


# Reranks run side by side, up to this many (inference is read-only on the loaded model):
# one at a time, a second search reranking beside the first got no rerank at all.
RERANK_CONCURRENCY = 3


class TimeBoxed:
    """Runs a reranker on up to ``workers`` worker threads, each call with a time box. A
    call made while every worker still runs an earlier one (they timed out, or are
    busy) is refused at once (``busy``)."""

    def __init__(self, reranker: Reranker, workers: int = RERANK_CONCURRENCY) -> None:
        self.reranker = reranker
        self.workers = max(1, workers)
        self._pool = cf.ThreadPoolExecutor(max_workers=self.workers,
                                           thread_name_prefix="plx-rerank")
        self._running: cf.Future | None = None   # the warm-up's
        self._calls: set[cf.Future] = set()
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
        """The scores of ``passages`` (best candidates first), or None and why. Past
        ``timeout`` the scores of the batches already finished come back (a prefix, with
        ``reranker_partial``): on a busy host the top candidates are still reranked."""
        if getattr(self.reranker, "loaded", True) is False:
            self.warm()   # loads in the background; this query ranks without it
            return None, "reranker_loading"
        stop = threading.Event()
        done: list[float] = []
        with self._lock:
            self._calls = {call for call in self._calls if not call.done()}
            warming = self._running is not None and not self._running.done()
            if len(self._calls) + warming >= self.workers:
                return None, "reranker_busy"
            takes = inspect.signature(self.reranker.score).parameters
            if "sink" in takes:
                future = self._pool.submit(self.reranker.score, query, list(passages),
                                           stop.is_set, done)
            elif "should_stop" in takes:
                future = self._pool.submit(self.reranker.score, query, list(passages),
                                           stop.is_set)
            else:
                future = self._pool.submit(self.reranker.score, query, list(passages))
            self._calls.add(future)
        try:
            return future.result(timeout=timeout), None
        except cf.TimeoutError:
            stop.set()   # the worker stops after its current batch
            finished = list(done)
            if finished:
                return finished, f"reranker_partial:{len(finished)}/{len(passages)}"
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


def shared() -> bool:
    """Whether this process asks the host's one model process
    (``operating.search.model_server``) instead of loading its own copy:
    ``PLX_SEARCH_MODELS`` unset or ``shared`` (``local``: load here)."""
    return (os.environ.get("PLX_SEARCH_MODELS") or "shared").strip().lower() != "local"


class SharedEmbedder:
    """The query embedder of the host's model process. ``model`` is the name it embeds
    with (None while no server answers: the dense retriever is skipped, stated)."""

    RETRY_SECONDS = 60.0   # a server that did not answer is not waited for again sooner

    def __init__(self) -> None:
        self._model: str | None = None
        self._missing_since: float | None = None

    @property
    def model(self) -> str | None:
        if self._model is None:
            if self._missing_since is not None and \
                    time.monotonic() - self._missing_since < self.RETRY_SECONDS:
                return None   # no query waits out a server start again meanwhile
            from operating.search import model_server
            ping = model_server.ensure()
            self._model = (ping or {}).get("embed_model") or None
            self._missing_since = None if self._model else time.monotonic()
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        from operating.search import model_server
        if self.model is None:
            raise RuntimeError("model_server_unavailable")
        try:
            out = model_server.request({"op": "embed", "texts": list(texts)}, 60.0)
        except ConnectionError:
            # the server ended (it exits when idle): started again in the background, and
            # this query ranks without it, as while none answers
            self._model, self._missing_since = None, time.monotonic()
            threading.Thread(target=model_server.ensure, daemon=True,
                             name="plx-model-server-start").start()
            raise RuntimeError("model_server_unavailable") from None
        if not out.get("ok"):
            raise RuntimeError(str(out.get("problem") or "embed_failed"))
        return out["vectors"]


class SharedReranker:
    """The reranker of the host's model process, with ``TimeBoxed``'s interface: the time
    box and the one worker are the server's (a busy or timed-out rerank comes back
    without scores, the query ranks without it)."""

    @property
    def reranker(self) -> "SharedReranker":
        return self   # ``warm`` reads ``reranker.loaded``

    @property
    def loaded(self) -> bool:
        from operating.search import model_server
        try:
            return bool(model_server.request({"op": "ping"}, 5.0).get("reranker"))
        except (OSError, ValueError):
            return False

    def warm(self, wait: bool = False) -> None:
        from operating.search import model_server
        if wait:
            model_server.ensure()
        else:
            threading.Thread(target=model_server.ensure, daemon=True).start()

    def score(self, query: str, passages: Sequence[str], timeout: float
              ) -> tuple[list[float] | None, str | None]:
        from operating.search import model_server
        try:
            out = model_server.request({"op": "rerank", "query": query,
                                        "passages": list(passages), "timeout": timeout},
                                       timeout + 5.0)
        except (OSError, ValueError) as exc:
            if isinstance(exc, ConnectionError):   # ended (idle): started again for later
                self.warm()
            return None, "reranker_unavailable:" + type(exc).__name__
        if not out.get("ok"):
            return None, "reranker_error:" + str(out.get("problem"))[:40]
        return out.get("scores"), out.get("problem")


def local_embedder() -> Embedder | None:
    """This process's own embedder (the model server's, or ``PLX_SEARCH_MODELS=local``):
    the configured embedder when it ships (``operating.embed.models.load``), else
    the local model of ``PLX_EMBED_MODEL``; None when neither is available."""
    def make() -> Any:
        try:
            from operating.embed.models import load  # type: ignore[import-not-found]
        except ImportError:
            return LocalEmbedder()
        return LaneBEmbedder(load(os.environ))
    return _default("embedder:" + embed_model_name(), make)


def local_reranker() -> TimeBoxed | None:
    if (os.environ.get("PLX_RERANK") or "on").lower() in ("off", "0", "false", "no"):
        return None
    folder = os.environ.get("PLX_RERANK_ONNX")
    if folder and os.path.isfile(os.path.join(folder, "model.onnx")):
        return _default("reranker", lambda: TimeBoxed(OnnxReranker(folder)))
    return _default("reranker", lambda: TimeBoxed(LocalReranker()))


def default_embedder() -> Any:
    """The query embedder search uses: the host's shared model process (``shared``),
    else this process's own (``local_embedder``); None when none is available (the dense
    retriever is then skipped and the result says so)."""
    if shared():
        return _default("shared-embedder", SharedEmbedder)
    return local_embedder()


def default_reranker() -> Any:
    """The reranker search uses: the shared model process's, else this process's own;
    None when switched off (``PLX_RERANK=off``)."""
    if (os.environ.get("PLX_RERANK") or "on").lower() in ("off", "0", "false", "no"):
        return None
    if shared():
        return _default("shared-reranker", SharedReranker)
    return local_reranker()


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
    if shared():
        embedder = _DEFAULTS.get("shared-embedder")
        reranker = _DEFAULTS.get("shared-reranker")
        return {"embedder": embedder is not None and embedder._model is not None
                and not thread.is_alive(),
                "reranker": None if off else bool(reranker is not None and reranker.loaded)}
    reranker = _DEFAULTS.get("reranker")
    return {"embedder": _DEFAULTS.get("embedder:" + embed_model_name()) is not None
            and not thread.is_alive(),
            "reranker": None if off else bool(
                reranker is not None and getattr(reranker.reranker, "loaded", True))}
