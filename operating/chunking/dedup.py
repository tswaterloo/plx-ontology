"""Duplicate clusters (``search.chunks.dup_group``): the same text in many places.

The same mail delivered to many mailboxes, one file in many folders, a forwarded copy:
exact duplicates share the hash of their normalised, case-folded body (``body_sha``);
near duplicates (a forward with one line added, a re-saved file) share a MinHash
signature band over word 5-gram shingles; a band match is only a candidate and is
confirmed on the two texts themselves (``same_text``): the exact shingle Jaccard
``>= NEAR_JACCARD`` and no number on each side that the other lacks (two invoices, two
sheet rows or two order mails differing only in their amounts or ids are distinct
records). The MinHash estimate alone (64 permutations) let pairs at a true 0.76-0.85
through: 12 of the 14 wrong groups of the 11-15 measure. Each cluster of two or more
chunks gets one id (``d:`` + the smallest member body hash); a singleton has none.

A cluster never merges permissions: every copy keeps its own row, audience key,
markings and citation, so a reader who can read only one copy still finds it; search
shows one representative of a cluster among the copies the reader may read.
"""

from __future__ import annotations

import functools
import re
import zlib
from collections import defaultdict
from typing import Callable, Iterable, Mapping, Sequence

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
CANDIDATE_JACCARD = 0.7    # a band pair estimated this close is checked on the texts
_WORD = re.compile(r"[0-9a-z]+")


# a digit-group separator inside a number ("1,000", "1'000", "1 000" with a no-break or
# thin space): dropped so "1,000" and "1000" are one number. A dot is kept: "1.250" may be
# a decimal, and reading it as 1250 would merge two records.
_DIGIT_GROUP = re.compile(r"(?<=\d)[,'\u00a0\u202f](?=\d{3}(?!\d))")


def _words(text: str) -> list[str]:
    return _WORD.findall(_DIGIT_GROUP.sub("", text.casefold()))


def _grams(words: Sequence[str]) -> set[str]:
    if len(words) < SHINGLE:
        return {" ".join(words)}
    return {" ".join(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


@functools.lru_cache(maxsize=2048)
def _prepared(text: str) -> tuple[str, frozenset[str], frozenset[str]]:
    """(body hash, numbers, word 5-grams) of a text, once per text (a search compares one
    hit's text with many copies)."""
    words = _words(text)
    return (body_sha(text), frozenset(w for w in words if any(c.isdigit() for c in w)),
            frozenset(_grams(words)))


def same_text(a: str, b: str) -> bool:
    """True when two bodies are one text: the same normalised body, or word 5-gram
    Jaccard ``>= NEAR_JACCARD`` with no number on each side missing from the other (a
    forward may add a dated line; two records differing in an amount or id are two).
    Digit grouping is not a difference ("1,000" is "1000")."""
    sha_a, na, ga = _prepared(a)
    sha_b, nb, gb = _prepared(b)
    if sha_a == sha_b:
        return True
    if na - nb and nb - na:
        return False
    union = len(ga | gb)
    return bool(union) and len(ga & gb) / union >= NEAR_JACCARD


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


def groups(rows: Iterable[tuple[str, str, str]],
           body_of: Callable[[Sequence[str]], Mapping[str, str]] | None = None
           ) -> dict[str, str]:
    """chunk_id -> dup_group for every chunk in a cluster of two or more, from
    (chunk_id, body_sha, body) rows. A near pair is confirmed on its texts
    (``same_text``): ``body_of`` (chunk ids -> bodies) reads them back for the candidate
    pairs only; without it every body long enough for a signature is kept in memory."""
    by_sha: dict[str, list[str]] = defaultdict(list)
    sigs: dict[str, np.ndarray] = {}
    kept: dict[str, str] = {}
    for chunk_id, sha, body in rows:
        if sha not in by_sha and len(body) >= NEAR_MIN_CHARS:
            sig = signature(body)     # one signature per text
            if sig is not None:
                sigs[sha] = sig
                if body_of is None:
                    kept[sha] = body
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
    candidates: set[tuple[str, str]] = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > 500:
            continue   # a huge bucket is boilerplate (a signature line); not a cluster
        for i, other in enumerate(members[1:], 1):
            for prev in members[max(0, i - 20):i]:
                if float(np.mean(sigs[prev] == sigs[other])) >= CANDIDATE_JACCARD:
                    candidates.add((prev, other) if prev < other else (other, prev))
    if candidates:
        involved = sorted({sha for pair in candidates for sha in pair})
        if body_of is None:
            text = kept
        else:
            got = body_of([by_sha[sha][0] for sha in involved])
            text = {sha: got[by_sha[sha][0]] for sha in involved if by_sha[sha][0] in got}
        for a, b in sorted(candidates):
            if union.find(a) != union.find(b) and a in text and b in text \
                    and same_text(text[a], text[b]):
                union.union(a, b)
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


__all__ = ["NEAR_JACCARD", "body_sha", "groups", "near", "representative", "same_text", "signature"]


def representative(members: Sequence[tuple[str, int, str]]) -> str:
    """The chunk shown for a cluster among those a reader may read: highest authority,
    then earliest date, then id ((chunk_id, authority, occurred_at) rows)."""
    return sorted(members, key=lambda m: (-m[1], m[2] or "9999", m[0]))[0][0]
