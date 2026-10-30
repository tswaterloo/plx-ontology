"""Fusion of the retrievers' ranked lists and the final score.

RRF (Cormack et al. 2009): ``sum_r w_r / (k + rank_r)``; the weighted-sum option is
``sum_r w_r * minmax(score_r)``. The fused score (normalised to 0..1 by the best
candidate), the cross-encoder's relevance (0..1), the source authority (system
of record 4 .. chat 1, as 0..1) and the recency (half-life decay of ``occurred_at``) are
then combined with the intent's weights, with the exact-id signal. A missing date is None
and contributes nothing. When no candidate of the query was reranked, the rerank weight
moves to the fused score; reranked and unreranked candidates of one query are never
compared (``core.rank``).
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Mapping, Sequence

from operating.search.config import IntentWeights


def rrf(lists: Mapping[str, Sequence[tuple[str, float]]], weights: Mapping[str, float],
        k: float) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, ranked in lists.items():
        weight = float(weights.get(name, 0.0))
        if weight <= 0:
            continue
        for rank, (chunk_id, _score) in enumerate(ranked, start=1):
            out[chunk_id] = out.get(chunk_id, 0.0) + weight / (k + rank)
    return out


def weighted(lists: Mapping[str, Sequence[tuple[str, float]]],
             weights: Mapping[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, ranked in lists.items():
        weight = float(weights.get(name, 0.0))
        if weight <= 0 or not ranked:
            continue
        scores = [s for _, s in ranked]
        low, high = min(scores), max(scores)
        for chunk_id, score in ranked:
            norm = 1.0 if high == low else (score - low) / (high - low)
            out[chunk_id] = out.get(chunk_id, 0.0) + weight * norm
    return out


def fuse(lists: Mapping[str, Sequence[tuple[str, float]]], intent: IntentWeights,
         method: str, k: float) -> list[tuple[str, float]]:
    scores = (rrf(lists, intent.retrievers, k) if method == "rrf"
              else weighted(lists, intent.retrievers))
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def authority_signal(authority: int | None) -> float:
    """System of record 4 -> 1.0, approved document 3, email 2, chat 1 -> 0.0."""
    if not isinstance(authority, int) or not 1 <= authority <= 4:
        return 0.0
    return (authority - 1) / 3.0


def recency_signal(occurred_at: dt.datetime | None, now: dt.datetime,
                   half_life_days: float) -> float | None:
    if occurred_at is None:
        return None
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=dt.timezone.utc)
    age = max((now - occurred_at).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age / half_life_days)


def final_score(fused_norm: float, rerank: float | None, authority: float,
                recency: float | None, intent: IntentWeights, *, exact: float | None = None,
                reranked_query: bool = True) -> float:
    """``exact``: the exact-id signal (1.0 the resolved object's own text, 0.9 a text
    naming it, 0.7 an id-shaped word of the question), so the record asked about leads a
    status question. ``reranked_query`` False (no candidate of the query was reranked:
    reranker off, busy or timed out): the rerank weight moves to the fused score, so the
    retrievers' agreement, not authority and recency, decides the order."""
    w_fused = intent.w_fused + (0.0 if reranked_query else intent.w_rerank)
    score = w_fused * fused_norm + intent.w_authority * authority
    if rerank is not None:
        score += intent.w_rerank * rerank
    if exact is not None:
        score += intent.w_exact * exact
    if recency is not None:
        score += intent.w_recency * recency
    return score
