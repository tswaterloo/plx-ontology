"""Search configuration: the ranking weights per intent (``weights.json``), the owner map
hidden counts name, and the database connection.

The weights live in ranking, never inside the vectors (RISHI-DIRECTION §2.5). A weights
file that does not validate is refused (``WeightsError``): a search never runs on weights
it half-read.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

WEIGHTS_PATH = Path(__file__).with_name("weights.json")
INTENTS = ("status", "why", "who", "how_much", "compare", "find_document", "policy",
           "timeline", "other")
RETRIEVERS = ("exact_id", "graph", "dense", "bm25", "fuzzy")
FINAL_TERMS = ("w_fused", "w_rerank", "w_exact", "w_authority", "w_recency")
FUSIONS = ("rrf", "weighted")
DEFAULT_DSN = "postgresql:///plx"
# Hidden matches are named by owner or team only when an operator mapped the audience
# unit to one; anything else is "another team" (fail closed: never the unit itself).
UNKNOWN_OWNER = "another team"


class WeightsError(ValueError):
    pass


@dataclass(frozen=True)
class IntentWeights:
    retrievers: Mapping[str, float]
    w_fused: float
    w_rerank: float
    w_exact: float
    w_authority: float
    w_recency: float
    half_life_days: float

    def as_dict(self) -> dict[str, Any]:
        return {"retrievers": dict(self.retrievers), "w_fused": self.w_fused,
                "w_rerank": self.w_rerank, "w_exact": self.w_exact,
                "w_authority": self.w_authority,
                "w_recency": self.w_recency, "half_life_days": self.half_life_days}


@dataclass(frozen=True)
class Weights:
    fusion: str
    rrf_k: float
    candidates_per_retriever: int
    rerank_top: int
    rerank_timeout_s: float
    intents: Mapping[str, IntentWeights]

    def for_intent(self, intent: str | None) -> IntentWeights:
        """An unknown or missing intent reads the ``default`` weights."""
        return self.intents.get(intent or "", self.intents["default"])


def _number(value: Any, where: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
            low <= value <= high):
        raise WeightsError(f"weights_invalid:{where}")
    return float(value)


def parse_weights(raw: Mapping[str, Any]) -> Weights:
    if not isinstance(raw, Mapping) or raw.get("schema") != "plx-search-weights/v1":
        raise WeightsError("weights_invalid:schema")
    allowed = {"schema", "notes", "fusion", "rrf_k", "candidates_per_retriever",
               "rerank_top", "rerank_timeout_s", "intents", "tuned"}
    unknown = set(raw) - allowed
    if unknown:
        raise WeightsError(f"weights_invalid:unknown_keys:{sorted(unknown)}")
    if raw.get("fusion") not in FUSIONS:
        raise WeightsError("weights_invalid:fusion")
    intents_raw = raw.get("intents")
    if not isinstance(intents_raw, Mapping) or "default" not in intents_raw:
        raise WeightsError("weights_invalid:intents")
    intents: dict[str, IntentWeights] = {}
    for name, spec in intents_raw.items():
        if name != "default" and name not in INTENTS:
            raise WeightsError(f"weights_invalid:intent:{name}")
        if not isinstance(spec, Mapping) or set(spec) != {"retrievers", "half_life_days",
                                                          *FINAL_TERMS}:
            raise WeightsError(f"weights_invalid:intent_keys:{name}")
        retrievers = spec["retrievers"]
        if not isinstance(retrievers, Mapping) or set(retrievers) != set(RETRIEVERS):
            raise WeightsError(f"weights_invalid:retrievers:{name}")
        intents[name] = IntentWeights(
            {r: _number(retrievers[r], f"{name}.{r}", 0, 100) for r in RETRIEVERS},
            *(_number(spec[t], f"{name}.{t}", 0, 1) for t in FINAL_TERMS),
            _number(spec["half_life_days"], f"{name}.half_life_days", 0.1, 100000))
    return Weights(
        raw["fusion"], _number(raw.get("rrf_k"), "rrf_k", 1, 1000),
        int(_number(raw.get("candidates_per_retriever"), "candidates", 1, 500)),
        int(_number(raw.get("rerank_top"), "rerank_top", 0, 200)),
        _number(raw.get("rerank_timeout_s"), "rerank_timeout_s", 0, 60),
        intents)


_CACHE: dict[str, tuple[float, Weights]] = {}


def load_weights(path: str | Path | None = None) -> Weights:
    """The weights at ``path`` (default ``PLX_SEARCH_WEIGHTS`` or the shipped file),
    re-read when the file changes."""
    target = Path(path or os.environ.get("PLX_SEARCH_WEIGHTS") or WEIGHTS_PATH)
    stamp = target.stat().st_mtime
    cached = _CACHE.get(str(target))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise WeightsError("weights_invalid:json") from exc
    weights = parse_weights(raw)
    _CACHE[str(target)] = (stamp, weights)
    return weights


def load_owners(path: str | Path | None = None) -> dict[str, str]:
    """The operator's audience-unit -> owner/team map (``PLX_SEARCH_OWNERS``: a JSON
    object of exact keys or trailing-``*`` prefixes). Absent or unreadable: empty, so
    every hidden match is "another team"."""
    target = path or os.environ.get("PLX_SEARCH_OWNERS")
    if not target:
        return {}
    try:
        raw = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {k: v.strip() for k, v in raw.items()
            if isinstance(k, str) and k and isinstance(v, str) and v.strip()}


def owner_of(audience_key: str, owners: Mapping[str, str]) -> str:
    """The owner or team an operator mapped ``audience_key`` to (exact key, then the
    longest ``*`` prefix), else ``UNKNOWN_OWNER``."""
    if audience_key in owners:
        return owners[audience_key]
    best = None
    for pattern, owner in owners.items():
        if pattern.endswith("*") and audience_key.startswith(pattern[:-1]):
            if best is None or len(pattern) > len(best[0]):
                best = (pattern, owner)
    return best[1] if best else UNKNOWN_OWNER


def dsn() -> str:
    return os.environ.get("PLX_PG_DSN") or DEFAULT_DSN
