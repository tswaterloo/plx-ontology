"""Duplicate clusters (``search.chunks.dup_group``): the same text in many places.

The same mail delivered to many mailboxes, one file in many folders, a forwarded copy:
exact duplicates share the hash of their normalised, case-folded body (``body_sha``);
near duplicates (a forward with one line added, a re-saved file) share a MinHash
signature band over word 5-gram shingles and are confirmed by the estimated Jaccard
similarity ``>= NEAR_JACCARD``. Each cluster of two or more chunks gets one id
(``d:`` + the smallest member body hash); a singleton has none.

A cluster never merges permissions: every copy keeps its own row, audience key,
markings and citation, so a reader who can read only one copy still finds it; search
shows one representative of a cluster among the copies the reader may read.
"""

from __future__ import annotations

import re
import zlib
from collections import defaultdict
from typing import Iterable, Sequence

import numpy as np

from . import body_sha

PERMUTATIONS = 64
BANDS = 16                  # 16 bands x 4 rows: pairs above ~0.5 Jaccard become candidates
NEAR_JACCARD = 0.85
NEAR_MIN_CHARS = 200        # shorter bodies are left to the exact hash
SHINGLE = 5
_PRIME = np.uint64((1 << 31) - 1)   # Mersenne prime: the mod mixes 32-bit inputs
_RNG = np.random.default_rng(20261029)
_A = _RNG.integers(1, (1 << 31) - 1, size=PERMUTATIONS, dtype=np.uint64)
_B = _RNG.integers(0, (1 << 31) - 1, size=PERMUTATIONS, dtype=np.uint64)
_WORD = re.compile(r"[0-9a-z]+")


def shingles(text: str) -> np.ndarray:
    words = _WORD.findall(text.casefold())
    if len(words) < SHINGLE:
        return np.zeros(0, dtype=np.uint64)
    grams = {" ".join(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}
    return np.fromiter((zlib.crc32(g.encode()) for g in grams), dtype=np.uint64,
                       count=len(grams))


def signature(text: str) -> np.ndarray | None:
    values = shingles(text)
    if values.size == 0:
        return None
    # (a*x + b) mod p over 32-bit x and 31-bit a, b: no uint64 overflow.
    hashed = (values[:, None] * _A[None, :] + _B[None, :]) % _PRIME
    return hashed.min(axis=0)


class _Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def groups(rows: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """chunk_id -> dup_group for every chunk in a cluster of two or more, from
    (chunk_id, body_sha, body) rows."""
    by_sha: dict[str, list[str]] = defaultdict(list)
    sigs: dict[str, np.ndarray] = {}
    for chunk_id, sha, body in rows:
        if sha not in by_sha and len(body) >= NEAR_MIN_CHARS:
            sig = signature(body)     # bodies are not kept: one signature per text
            if sig is not None:
                sigs[sha] = sig
        by_sha[sha].append(chunk_id)
    union = _Union()
    for sha in by_sha:
        union.find(sha)
    buckets: dict[tuple[int, bytes], list[str]] = defaultdict(list)
    rows_per_band = PERMUTATIONS // BANDS
    for sha in sorted(sigs):
        sig = sigs[sha]
        for band in range(BANDS):
            part = sig[band * rows_per_band:(band + 1) * rows_per_band].tobytes()
            buckets[(band, part)].append(sha)
    for members in buckets.values():
        if len(members) < 2 or len(members) > 500:
            continue   # a huge bucket is boilerplate (a signature line); not a cluster
        for i, other in enumerate(members[1:], 1):
            for prev in members[max(0, i - 20):i]:
                if union.find(prev) == union.find(other):
                    break
                if float(np.mean(sigs[prev] == sigs[other])) >= NEAR_JACCARD:
                    union.union(prev, other)
                    break
    clusters: dict[str, list[str]] = defaultdict(list)
    for sha in by_sha:
        clusters[union.find(sha)].append(sha)
    out: dict[str, str] = {}
    for root, members in clusters.items():
        ids = [cid for sha in members for cid in by_sha[sha]]
        if len(ids) < 2:
            continue
        group = "d:" + min(members)[:16]
        for cid in ids:
            out[cid] = group
    return out


def near(a: str, b: str) -> float:
    """Estimated Jaccard similarity of two texts' shingle sets (tests, diagnostics)."""
    sa, sb = signature(a), signature(b)
    if sa is None or sb is None:
        return 0.0
    return float(np.mean(sa == sb))


__all__ = ["NEAR_JACCARD", "body_sha", "groups", "near", "representative", "signature"]


def representative(members: Sequence[tuple[str, int, str]]) -> str:
    """The chunk shown for a cluster among those a reader may read: highest authority,
    then earliest date, then id ((chunk_id, authority, occurred_at) rows)."""
    return sorted(members, key=lambda m: (-m[1], m[2] or "9999", m[0]))[0][0]
