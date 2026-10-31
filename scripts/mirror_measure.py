"""Read-only measurements of the Portal reporting API behind the source mirror.

Two measurements, both GET-only through ``integrations.workbench_read_api`` (key
from ``PLX_WORKBENCH_API_KEY``, origin from ``PLX_WORKBENCH_API_URL``)::

    python3 -m scripts.mirror_measure relation-counts --relation public.x [...] \
        [--budget-seconds N] [--daily-budget-seconds N] [--max-keys N] [--sample-keys N] \
        [--previous-report FILE]

``relation-counts`` is a no-store keyset counter. For each named relation it pages
through the key columns only, counts rows and distinct keys (an 8-byte digest per key
held in memory, nothing written), then reads the first keys a second time and reports
how many of the first read's keys the second read saw. A relation whose ids change
between two reads in one run (an ETL that re-inserts rows under fresh ids) is a
data-truth defect upstream, not a paging bug; the output says which
(``classification``). A key missing from the second read alone is a normal delete
(``deletes_observed``); a re-insert is claimed only when keys are missing *and* new
keys appear inside the first read's key window (by position in the service's order),
or when at least half the sample is gone (``ids_replaced``: fresh, later-sorting ids or
a bulk purge; confirm across two daily runs via ``first_keys_hash``). An HTTP 5xx or a statement timeout halves the page and retries
the same cursor down to ``--min-page-size``; a read that still fails is recorded as
``upstream_unavailable`` with its HTTP status and error code. Output: counts, booleans,
16-hex key-list hashes, relation names and enums; never a row value or a key. Its
``row_data_check`` proves it: every key read (>= 8 characters) is searched for in the
serialized output and ``leaked`` must be 0. The check covers the keys the
counter holds as text (the stability sample of both reads, up to 3 x ``--sample-keys``);
every other key exists only as an 8-byte digest and is never formatted.

The relations rotate across runs (:func:`relation_counts`): least recently completed
first, each with the whole remaining budget, so a relation too large for an equal share
completes in some run; ``complete_count`` counts the relations completed within
``ROTATION_WINDOW_HOURS`` and ``relations[*].completed_at`` names each one's completing
run. A traversal is never resumed in a later run: the service's cursor carries the last
key's values, which this output never prints. A relation one run's budget cannot hold (the
previous report shows it cut with the first turn, or a need over the budget) gets up to
``--daily-budget-seconds`` (the daily limit); cut even with all of that, it stays
``count_lower_bound`` and is named in ``exceeds_daily_bound``.

Known limitation: the re-insert test looks for new keys only up to the last
first-read key that the second read still has. When the first read's tail keys were
deleted, the window ends earlier, so a new key sorting among those deleted tail keys is
not seen and the verdict can read ``deletes_observed``. The output field
``window_shortened: true`` marks that case; treat a deletes-only verdict with it as weaker.

Replaced rows: for a relation whose natural key ``integrations.source_mirror``
derives (``replacement_key``: an owned child row), both stability reads also carry
the natural-key columns, and a first-read key gone by the second read whose natural
key one new key carries, one to one in both reads, counts in ``replaced_rows``; any
other pairing counts in ``ambiguous_replacements``. A key is new only when the whole
first read never saw it and it lies inside the second read's window; without a
complete first read the counts are withheld (None). Counts over the samples only (a
lower bound); the natural-key values join the leak self-check.

``ordered_full_coverage`` is the ``mirror-full-coverage`` probe of
``scripts.live_read_probes``: the full mirror sync, but the relations bound in
``ontology/schema/domains`` (the evaluation denominator) are captured first, and the
report adds ``complete_bound_relation_count`` / ``bound_total`` next to the catalog
totals.

Completeness (decision 2026-10-02 capture-persistence) is one number, computed here by
:func:`completeness` for the full-coverage probe, the ontology projection probe and the
unfamiliar-transaction eval alike: ``complete_bound_relation_count / bound_total``, where
``bound_total`` is the bound relations minus the *unstable* ones; the 93-relation catalog
figures stay secondary. A relation is unstable once ``relation-counts`` classified it
``data_truth_defect`` on two consecutive daily runs (:func:`stability_streaks`; the
verifier passes the previous daily report as ``--previous-report``, so the streak is
carried from one daily report to the next). From then on it leaves the denominator, the
mirror never stages it (the coverage probe reads it key-only through the counter, nothing
stored), and every binding of it carries the gap ``capture_unstable_ids``. It stays
unstable until two consecutive conclusive runs see stable ids. ``upstream-findings``
renders the Portal ETL finding from daily reports: counts and classifications only.
"""

from __future__ import annotations

import argparse
from array import array
from collections import Counter
from datetime import datetime, timezone
from hashlib import blake2b, sha256
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
DOMAINS_DIR = ROOT / "ontology" / "schema" / "domains"
SCHEMA = "mirror-relation-counts/v1"
PAGE_SIZE, MIN_PAGE_SIZE = 5000, 250
MAX_KEYS = 5_000_000
SAMPLE_KEYS = 20_000
THROTTLE_ATTEMPTS, MAX_RETRY_AFTER = 6, 60.0
# A relation's last complete count stays valid this long; complete_count is judged over it.
ROTATION_WINDOW_HOURS = 96.0
MIN_LEAK_CHARS = 8  # operating.ingestion.row_data_check's threshold
# Share of a relation's budget for the full traversal; the rest is the stability re-read's.
TRAVERSAL_SHARE = 0.9
# A first-read sample this far gone by the second read is a bulk replacement, not deletes:
# fresh cuids sort after every old id, so re-inserted rows never land inside the window.
BULK_MISSING_RATIO = 0.5
_RELATION = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
PROVENANCE_KEYS = {"evidence", "note", "notes", "description"}
COUNTS_CHECK = "mirror.relation_counts"   # the daily counter's live-check id
DEFECT = "data_truth_defect"
STABLE_CLASSES = frozenset({"ok", "mirror_bound"})
UNSTABLE_RUNS = 2        # consecutive daily runs that make (or clear) an unstable relation
CLOCK_SKEW_HOURS = 0.1  # a previous report stamped up to 6 min ahead (verifier host skew) still counts
CONSECUTIVE_HOURS = 36.0  # a previous daily report older than this does not continue a streak
REREAISE_DAYS = 14       # a defect on a relation unstable this long counts as new again once
UNSTABLE_GAP = "capture_unstable_ids"
COMPLETENESS_METRIC = "complete_bound_relation_count/bound_total"
KEY_ONLY_SAMPLE = 1000   # stability sample of a key-only read in the coverage probe


# ------------------------------------------------------------------ bound relations

def bound_relations(domains_dir: str | Path = DOMAINS_DIR) -> list[str]:
    """Portal relations bound by a curated domain file (``source: portal_api``), sorted.

    These are the relations the ontology projects, so the evaluation reads only them;
    the generated skeleton is not a binding of record and is not counted."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            if node.get("source") == "portal_api" and isinstance(node.get("relation"), str):
                found.add(node["relation"])
            for key, value in node.items():
                if key not in PROVENANCE_KEYS:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for path in sorted(Path(domains_dir).glob("*.json")):
        if not path.name.startswith("_"):
            walk(json.loads(path.read_text(encoding="utf-8")))
    return sorted(found)


# ------------------------------------------------------------------ keyset counter

class ReadFailed(Exception):
    """A page that could not be read after the retry policy (no message text kept)."""

    def __init__(self, reason: str, *, status: int | None = None,
                 error_code: str | None = None, error_class: str | None = None) -> None:
        super().__init__(reason)
        self.reason, self.status, self.error_code = reason, status, error_code
        self.error_class = error_class


class _Pager:
    """Reads keyset pages of one relation's key columns (and ``extra`` ones) under the
    retry policy."""

    def __init__(self, client: Any, name: str, entry: Mapping[str, Any], *,
                 page_size: int, min_page_size: int, sleep: Callable[[float], None],
                 throttle_attempts: int, max_retry_after: float,
                 extra: Sequence[str] = ()) -> None:
        self.client, self.name, self.entry = client, name, entry
        self.columns = list(dict.fromkeys([*entry["key"], *extra]))
        self.size, self.min_size = page_size, min_page_size
        self.sleep, self.throttle_attempts = sleep, throttle_attempts
        self.max_retry_after = max_retry_after
        self.halvings = 0
        self.throttle_waits = 0
        self.retried_statuses: dict[str, int] = {}

    def page(self, cursor: str | None) -> dict[str, Any]:
        from integrations.http import SourceUnavailable
        throttled = 0
        while True:
            try:
                return self.client.read_page(self.name, self.entry, columns=self.columns,
                                             cursor=cursor, page_size=self.size)
            except SourceUnavailable as exc:
                status = getattr(exc, "status", None)
                raw_code = getattr(exc, "error_code", None)
                code = raw_code if isinstance(raw_code, str) and \
                    _ERROR_CODE.fullmatch(raw_code) else None
                if status in (429, 503):
                    throttled += 1
                    delay = getattr(exc, "retry_after_seconds", None)
                    delay = float(2 ** (throttled - 1)) if delay is None else float(delay)
                    if throttled >= self.throttle_attempts or delay > self.max_retry_after:
                        raise ReadFailed("throttled", status=status, error_code=code) from None
                    self.throttle_waits += 1
                    self.sleep(delay)
                    continue
                if status is not None and 400 <= status < 500:
                    raise ReadFailed("cursor_rejected" if code == "invalid-cursor"
                                     else "http_client_error", status=status,
                                     error_code=code) from None
                # HTTP 5xx, a statement timeout or a transport failure: a smaller page
                # is the remedy the service documents; retry the same cursor.
                label = str(status) if status is not None else (code or "transport")
                self.retried_statuses[label] = self.retried_statuses.get(label, 0) + 1
                if self.size <= self.min_size:
                    raise ReadFailed("upstream_unavailable", status=status, error_code=code,
                                     error_class=type(exc).__name__) from None
                self.size = max(self.min_size, self.size // 2)
                self.halvings += 1
            except ValueError:
                raise ReadFailed("source_contract") from None


class _DigestSet:
    """8-byte key digests (non-zero ints) in one flat array, open addressing with linear
    probing: about 18 MB per million keys where a set of ``bytes`` takes about 75 MB, so
    a run can hold the tens of millions of keys of an ETL-grown relation."""

    def __init__(self, bits: int = 10) -> None:
        self._bits, self._count = bits, 0
        self._table = array("Q", bytes(8 << bits))

    def __len__(self) -> int:
        return self._count

    def _slot(self, value: int) -> int:
        """The slot holding ``value``, else the empty slot where it goes."""
        table, mask = self._table, (1 << self._bits) - 1
        index = value & mask
        while table[index] not in (0, value):
            index = (index + 1) & mask
        return index

    def __contains__(self, value: int) -> bool:
        return self._table[self._slot(value)] == value

    def add(self, value: int) -> None:
        index = self._slot(value)
        if self._table[index] == value:
            return
        self._table[index] = value
        self._count += 1
        if self._count * 10 > 7 << self._bits:   # keep the load under 0.7
            old, self._bits, self._count = self._table, self._bits + 1, 0
            self._table = array("Q", bytes(8 << self._bits))
            for kept in old:
                if kept:
                    self.add(kept)

    def update(self, values: Iterable[int]) -> None:
        for value in values:
            self.add(value)


def _digest(text: str) -> int:
    """The non-zero 8-byte digest of a key text (0 marks an empty slot)."""
    return int.from_bytes(blake2b(text.encode("utf-8"), digest_size=8).digest(), "big") or 1


def _served(entry: Mapping[str, Any] | None) -> bool:
    return bool(entry) and not entry.get("unavailable") and bool(entry.get("key"))


def _key_text(row: Mapping[str, Any], columns: Sequence[str]) -> str:
    return json.dumps([row.get(col) for col in columns], sort_keys=True, default=str,
                      separators=(",", ":"), ensure_ascii=False)


def _list_hash(keys: Sequence[str]) -> str | None:
    if not keys:
        return None
    return sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def count_relation(client: Any, name: str, entry: Mapping[str, Any] | None, *,
                   mirror_bound: int | None, deadline: float | None,
                   page_size: int = PAGE_SIZE, min_page_size: int = MIN_PAGE_SIZE,
                   max_keys: int = MAX_KEYS, sample_keys: int = SAMPLE_KEYS,
                   sleep: Callable[[float], None] = time.sleep,
                   monotonic: Callable[[], float] = time.monotonic,
                   throttle_attempts: int = THROTTLE_ATTEMPTS,
                   max_retry_after: float = MAX_RETRY_AFTER,
                   seen_values: set[str] | None = None,
                   replacements: bool = True) -> dict[str, Any]:
    """Count one relation's rows and distinct keys and test id stability (counts only).

    ``replacements``: also read the natural-key columns and count replaced rows
    (module docstring); without it the reads are key-only. ``seen_values`` collects
    the raw key texts of the stability sample for the caller's leak self-check; they
    are never part of the result."""
    started = monotonic()
    traversal_deadline = (None if deadline is None
                          else started + max(0.0, deadline - started) * TRAVERSAL_SHARE)
    result: dict[str, Any] = {"in_catalog": entry is not None, "served": _served(entry)}
    if not result["served"]:
        result.update(verdict="not_served", classification="upstream_unavailable",
                      complete=False)
        return result
    from integrations.source_mirror import (generated_replacement_models, natural_text,
                                            pair_replacements, replacement_key)
    # The natural key a replaced row's successor carries (the mirror's own rule).
    natural = (replacement_key(generated_replacement_models(), name, entry.get("columns") or ())
               if replacements else None)
    pager = _Pager(client, name, entry, page_size=page_size, min_page_size=min_page_size,
                   sleep=sleep, throttle_attempts=throttle_attempts,
                   max_retry_after=max_retry_after, extra=natural or ())
    columns = list(entry["key"])
    naturals: list[dict[str, str | None]] = [{}, {}]   # key text -> natural text, per read
    digests = _DigestSet()
    first: list[str] = []
    rows_seen = pages = cross_page_repeats = same_page_repeats = 0
    cursor: str | None = None
    stop, failure = "end", None
    while True:
        if traversal_deadline is not None and monotonic() >= traversal_deadline:
            stop = "budget"
            break
        try:
            page = pager.page(cursor)
        except ReadFailed as exc:
            stop, failure = "read_failed", exc
            break
        pages += 1
        page_digests: set[int] = set()
        for row in page["rows"]:
            text = _key_text(row, columns)
            digest = _digest(text)
            rows_seen += 1
            if digest in digests:
                cross_page_repeats += 1
            elif digest in page_digests:
                same_page_repeats += 1
            page_digests.add(digest)
            if len(first) < sample_keys:
                first.append(text)
                if natural:
                    naturals[0][text] = natural_text(row, natural)
        digests.update(page_digests)
        cursor = page["next_cursor"]
        if cursor is None:
            break
        if len(digests) >= max_keys:
            stop = "key_bound"
            break
    result.update({
        "complete": stop == "end", "stop": stop, "rows_seen": rows_seen,
        "distinct_keys": len(digests), "pages": pages,
        "page_size_final": pager.size, "page_halvings": pager.halvings,
        "throttle_waits": pager.throttle_waits, "retried": dict(pager.retried_statuses),
        "cross_page_repeats": cross_page_repeats, "same_page_repeats": same_page_repeats,
        "exceeds_mirror_bound": (mirror_bound is not None and len(digests) > mirror_bound)})
    if failure is not None:
        result.update({"failure_reason": failure.reason,
                       "failure_http_status": failure.status,
                       "failure_error_code": failure.error_code,
                       "failure_error_class": failure.error_class})
    # Stability: read the first keys again (up to twice as many, so rows inserted in
    # between cannot push a surviving key out of the window) and count survivors.
    second: list[str] = []
    second_failure = None
    second_cut = False
    if first:
        cursor = None
        while len(second) < 2 * len(first):
            if deadline is not None and monotonic() >= deadline:
                second_cut = True
                break
            try:
                page = pager.page(cursor)
            except ReadFailed as exc:
                second_failure = exc
                break
            for row in page["rows"]:
                second.append(_key_text(row, columns))
                if natural:
                    naturals[1][second[-1]] = natural_text(row, natural)
            cursor = page["next_cursor"]
            if cursor is None:
                break
    first_set, second_set = set(first), set(second)
    both = len(first_set & second_set)
    missing = len(first_set - second_set)
    # The window is the second read up to its last surviving first-read key, in the
    # order the service returned (it sorts by column type and collation, and sends int8
    # and numeric keys as strings, so the counter never re-sorts): a key the first read
    # never saw before that position was inserted between the two reads.
    boundary = max((index for index, text in enumerate(second) if text in first_set),
                   default=-1)
    new_in_window = (len({text for text in second[:boundary] if text not in first_set})
                     if boundary > 0 else 0)
    stability: dict[str, Any] = {
        "first_read_keys": len(first_set), "second_read_keys": len(second_set),
        "seen_in_both": both, "missing_in_second": missing, "new_in_window": new_in_window,
        "ratio_seen_in_both": round(both / len(first_set), 6) if first_set else None,
        "first_keys_hash": _list_hash(first),
        "second_keys_hash": _list_hash(second[:len(first)]),
        # The window ends at the last surviving first-read key. When the first read's
        # last key itself is gone, the window is shorter than the sample (see the
        # module docstring: a new key sorting among deleted tail keys is not seen).
        "window_shortened": bool(first) and first[-1] not in second_set,
        "second_read_failed": second_failure is not None, "second_read_cut": second_cut}
    stability["hash_equal"] = (stability["first_keys_hash"] is not None and
                               stability["first_keys_hash"] == stability["second_keys_hash"])
    # Replaced rows (module docstring): counted over the samples only.
    stability["replacement_key"] = natural is not None
    stability["replaced_rows"] = stability["ambiguous_replacements"] = None
    if natural and first and stop == "end" and second_failure is None and not second_cut:
        # New: a key the whole first read never saw, inside the second read's window. A
        # key the first read saw past its sample is held in both reads, never new.
        def seen_first(text: str) -> bool:
            return _digest(text) in digests
        window = set(second[:boundary]) if boundary > 0 else set()
        appeared = {k for k in window - first_set if not seen_first(k)}
        held = [(0, k) for k in first_set & second_set] + [
            (1, k) for k in second_set - first_set if seen_first(k)]
        pairs = pair_replacements(
            {k: naturals[0][k] for k in first_set - second_set if naturals[0].get(k)},
            {k: naturals[1][k] for k in appeared if naturals[1].get(k)},
            Counter(naturals[i][k] for i, k in held if naturals[i].get(k)))
        bases = Counter(basis for basis, _ in pairs.values())
        stability["replaced_rows"] = bases.get("source_replacement", 0)
        stability["ambiguous_replacements"] = bases.get("ambiguous_replacement", 0)
    result.update(stability)
    if seen_values is not None:
        for text in first_set | second_set:
            seen_values.add(text)
            for value in json.loads(text):
                seen_values.add(str(value))
        for text in {*naturals[0].values(), *naturals[1].values()} - {None}:
            for value in json.loads(text):
                seen_values.add(str(value))
    result["verdict"], result["classification"] = _verdict(
        result, failure, second_failure is not None or second_cut)
    if result["verdict"] == "ids_replaced":
        result["verdict_note"] = ("ids replaced or bulk purge; confirm across two daily "
                                  "runs via first_keys_hash")
    result["seconds"] = round(monotonic() - started, 3)
    return result


def _verdict(result: Mapping[str, Any], failure: ReadFailed | None,
             second_incomplete: bool) -> tuple[str, str]:
    """(verdict, classification). Classifications: ``data_truth_defect`` (rows
    re-inserted under new ids between reads: report upstream), ``paging_defect`` (a
    key recurs on a later page), ``mirror_bound`` (stable ids, the true count exceeds
    the mirror's row bound), ``upstream_unavailable``, ``inconclusive`` and ``ok``.
    Keys missing from the second read with no new key in the window are deletes
    (verdict ``deletes_observed``), never a defect."""
    first, missing = result["first_read_keys"], result["missing_in_second"]
    # ids_replaced: most of the sample is gone. Fresh ids replacing it, or a bulk purge.
    if not second_incomplete and missing:
        if result["new_in_window"]:
            return "ids_reinserted", "data_truth_defect"
        if first and missing / first >= BULK_MISSING_RATIO:
            return "ids_replaced", "data_truth_defect"
    if result["cross_page_repeats"]:
        return "keyset_repeats", "paging_defect"
    if failure is not None:
        return failure.reason, "upstream_unavailable"
    if not result["complete"] or second_incomplete:
        return "count_lower_bound", "inconclusive"
    deletes = bool(missing) and not second_incomplete
    if result["exceeds_mirror_bound"]:
        return ("deletes_observed" if deletes else "stable_exceeds_mirror_bound"), "mirror_bound"
    return ("deletes_observed" if deletes else "stable_within_bound"), "ok"


def _previous_fields(path: str | Path | None, check_id: str) -> dict[str, Any]:
    """The recorded ``detail.fields`` of ``check_id`` in an earlier verifier report."""
    if path is None:
        return {}
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    checks = report.get("checks") if isinstance(report, Mapping) else None
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, Mapping) and check.get("id") == check_id:
            detail = check.get("detail") if isinstance(check.get("detail"), Mapping) else {}
            fields = detail.get("fields")
            return dict(fields) if isinstance(fields, Mapping) else {}
    return {}


def _per_relation(fields: Mapping[str, Any], path: str) -> dict[str, Any]:
    """``relations[*].<path>`` keyed by ``relations[*].relation`` (aligned lists only)."""
    names, values = fields.get("relations[*].relation"), fields.get(f"relations[*].{path}")
    if not isinstance(names, list) or not isinstance(values, list) or \
            len(names) != len(values):
        return {}
    return {name: value for name, value in zip(names, values)
            if isinstance(name, str) and _RELATION.fullmatch(name)}


def _previous_hashes(path: str | Path | None, check_id: str) -> dict[str, str]:
    """``first_keys_hash`` per relation recorded by an earlier verifier report (the
    spec records ``relations[*].relation`` and ``relations[*].first_keys_hash``)."""
    hashes = _per_relation(_previous_fields(path, check_id), "first_keys_hash")
    return {name: value for name, value in hashes.items()
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value)}


def previous_unstable(path: str | Path | None) -> list[str]:
    """The relations an earlier daily report's ``mirror.relation_counts`` check named
    unstable (``unstable_relations``); empty without a readable report."""
    names = _previous_fields(path, COUNTS_CHECK).get("unstable_relations")
    return sorted({name for name in names if isinstance(name, str)
                   and _RELATION.fullmatch(name)}) if isinstance(names, list) else []


COVERAGE_CHECK = "mirror.full_coverage"   # the daily full-coverage live-check id
DEFER_CLASSES = frozenset({"row_bound", "paging_defect"})


def previous_deferred(path: str | Path | None) -> list[str]:
    """Relations the next full-coverage run captures last: the previous daily report's
    ``mirror.full_coverage`` ``defer_next_run`` (a row-bound or paging-defect run, or
    a source defect whose page-size window closed, kept until a run captures it) and
    every relation its ``mirror.relation_counts`` counted above the mirror bound (``relations[*].exceeds_mirror_bound``). Names only; empty
    without a readable report."""
    names = _previous_fields(path, COVERAGE_CHECK).get("defer_next_run")
    deferred = {name for name in names if isinstance(name, str)
                and _RELATION.fullmatch(name)} if isinstance(names, list) else set()
    over = _per_relation(_previous_fields(path, COUNTS_CHECK), "exceeds_mirror_bound")
    deferred |= {name for name, value in over.items() if value is True}
    return sorted(deferred)


def _previous_stamp(path: str | Path | None, check_id: str | None) -> datetime | None:
    """When an earlier verifier report's ``check_id`` check ran: its own ``checked_at`` (a
    daily set rewritten as it runs carries older checks under a fresh ``generated_at``),
    else, for a check that carries no origin, the report's ``generated_at``; None without a
    readable stamp or for a check of ``origin`` unknown."""
    if path is None:
        return None
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        check = next((c for c in report.get("checks") or [] if isinstance(c, Mapping)
                      and check_id is not None and c.get("id") == check_id), {})
        if check.get("origin") == "unknown":
            return None
        return _parse_stamp(str(check.get("checked_at") or report.get("generated_at")))
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _hours(since: datetime, now: datetime) -> float:
    return round((now - since).total_seconds() / 3600, 2)


def previous_report_age_hours(path: str | Path | None, now: datetime,
                              check_id: str | None = None) -> float | None:
    """Hours between ``now`` and when an earlier verifier report's ``check_id`` check ran
    (:func:`_previous_stamp`); None without a readable stamp."""
    stamp = _previous_stamp(path, check_id)
    return None if stamp is None else _hours(stamp, now)


def _streak(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and \
        0 <= value <= 10_000 else None


def stability_streaks(classification: str, *, previous_classification: Any = None,
                      previous_defect_streak: Any = None,
                      previous_stable_streak: Any = None,
                      previously_unstable: bool = False) -> dict[str, Any]:
    """The two-run rule for one relation, carried from the previous daily report.

    ``defect_streak`` counts consecutive daily runs classified ``data_truth_defect``;
    ``stable_streak`` consecutive conclusive runs that were not (``ok`` or
    ``mirror_bound``). A run that proves neither (``inconclusive``,
    ``upstream_unavailable``, ``paging_defect``) carries both unchanged. A report from
    before the streaks existed counts its own classification as a streak of one.
    Unstable: two defect runs in a row, or already unstable and not yet two stable
    runs in a row."""
    defect, stable = _streak(previous_defect_streak), _streak(previous_stable_streak)
    if defect is None:
        defect = 1 if previous_classification == DEFECT else 0
    if stable is None:
        stable = 1 if previous_classification in STABLE_CLASSES else 0
    if classification == DEFECT:
        defect, stable = defect + 1, 0
    elif classification in STABLE_CLASSES:
        defect, stable = 0, stable + 1
    unstable = defect >= UNSTABLE_RUNS or (previously_unstable and stable < UNSTABLE_RUNS)
    return {"defect_streak": defect, "stable_streak": stable, "unstable": unstable}


def reraise_defect(classification: str, *, previously_unstable: bool,
                   previous_defect_streak: Any) -> bool:
    """Whether a defect run on a long-unstable relation counts as new again.

    A relation unstable for ``REREAISE_DAYS`` daily defect runs past the two that made
    it unstable raises once more (the gate fires again); the caller then restarts the
    streak at ``UNSTABLE_RUNS``. Without a consecutive previous report the streak is
    unknown, so nothing re-raises."""
    streak = _streak(previous_defect_streak)
    return (classification == DEFECT and previously_unstable and streak is not None
            and streak >= UNSTABLE_RUNS + REREAISE_DAYS)


CUT_STOPS = ("budget", "key_bound")
# Kept on every relation entry so the recorded relations[*] lists stay aligned.
ENTRY_KEYS = ("stop", "rows_seen", "distinct_keys", "pages", "exceeds_mirror_bound", "seconds")


def _rotation_state(path: str | Path | None, names: Sequence[str],
                    now: datetime) -> dict[str, dict[str, Any]]:
    """Per relation, what the rotation carries from the previous counter report: the run
    that last completed it (``completed_at``) and its count then, the seconds a traversal
    needs (``need``, recorded as ``need_seconds``: a lower bound while no run completed
    it), whether it yields the first turn (it had it and was cut), whether one run's budget
    could not hold it (``over_run``: it had the first turn and the budget cut it, or it ran
    to the daily deadline and no run has ended it since: a cut need is only a lower bound,
    so it alone may not exceed the run budget) and whether it was known above the mirror
    bound or the daily bound. A report from before these fields counts its own complete
    relations as completed in its run; a completing run stamped after ``now`` is unknown."""
    fields = _previous_fields(path, COUNTS_CHECK)
    stamp = _previous_stamp(path, COUNTS_CHECK) if fields else None
    per = {key: _per_relation(fields, key) for key in (
        "completed_at", "completed_distinct_keys", "complete", "distinct_keys", "stop",
        "seconds", "need_seconds", "rotation_rank", "exceeds_mirror_bound", "budget_extended")}
    # Names only: the recorded detail is capped (the live verifier MAX_DETAIL_CHARS).
    over_daily = fields.get("exceeds_daily_bound")
    over_daily = set(over_daily) if isinstance(over_daily, list) else set()
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        at, keys = per["completed_at"].get(name), per["completed_distinct_keys"].get(name)
        if _stamp_or_none(at) is None and per["complete"].get(name) is True and stamp:
            at, keys = stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), per["distinct_keys"].get(name)
        when = _stamp_or_none(at)
        if when is None or _hours(when, now) < -CLOCK_SKEW_HOURS:
            at = None
        stop, seconds = per["stop"].get(name), per["seconds"].get(name)
        need = per["need_seconds"].get(name)
        if not _is_seconds(need):   # a report from before need_seconds
            need = seconds if stop in ("end", *CUT_STOPS) and _is_seconds(seconds) else 0.0
        exceeds = per["exceeds_mirror_bound"].get(name)
        first = _is_count(rank := per["rotation_rank"].get(name)) and rank == 0
        out[name] = {
            "completed_at": at,
            "completed_distinct_keys": keys if at is not None and _is_count(keys) else None,
            "need": float(need),
            "yields": first and stop in CUT_STOPS,
            "over_run": first and stop == "budget" or (
                per["budget_extended"].get(name) is True and stop in ("rotation", *CUT_STOPS)),
            "exceeds_mirror_bound": exceeds if isinstance(exceeds, bool) else None,
            "exceeds_daily_bound": name in over_daily}
    return out


def _stamp_or_none(value: Any) -> datetime | None:
    try:
        return _parse_stamp(value) if isinstance(value, str) and _STAMP.fullmatch(value) \
            else None
    except ValueError:
        return None


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_seconds(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and \
        0 <= value < float("inf")


def relation_counts(client: Any, relations: Sequence[str], *,
                    budget_seconds: float | None = None,
                    daily_budget_seconds: float | None = None, max_keys: int = MAX_KEYS,
                    sample_keys: int = SAMPLE_KEYS, page_size: int = PAGE_SIZE,
                    min_page_size: int = MIN_PAGE_SIZE, mirror_bound: int | None = None,
                    previous_report: str | Path | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    monotonic: Callable[[], float] = time.monotonic,
                    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
                    ) -> dict[str, Any]:
    """Count each relation in rotation order, each with the whole remaining budget.

    Rotation (``_rotation_state``, from ``previous_report``): the relation least recently
    completed goes first (never completed before any), then the others in the order
    given; one that had the first turn last run and was cut yields it once, so one
    relation that never fits cannot starve the rest (two or more that never fit take the
    first turn in turn and can leave the others unreached: complete_count stays short).
    After the first, a served relation whose last traversal needed more seconds than are
    left is not reached (``stop`` ``rotation``, verdict ``not_reached``, nothing read; its
    streaks carry, so the two-run rule counts measured runs). A relation is
    ``complete_in_window`` when this run completed it, or its last completing run
    (``completed_at``) is at most ``ROTATION_WINDOW_HOURS`` old and this run found it
    neither unserved nor unreadable (``upstream_unavailable``); ``complete_count`` counts those and
    ``complete_this_run_count`` the relations this run completed. A run cut by the budget
    stays ``count_lower_bound``: a traversal is never stitched across runs.

    ``daily_budget_seconds`` (the daily limit, above ``budget_seconds``): a relation the
    previous report shows one run's budget cannot hold (``over_run``, or a ``need_seconds``
    over ``budget_seconds``) runs to that deadline instead (``budget_extended``); the others
    keep ``budget_seconds``. One the budget cuts with the first turn and that deadline is
    ``exceeds_daily_bound`` (carried until a run completes it): still
    ``count_lower_bound``, listed in the report's ``exceeds_daily_bound``.

    The two-run rule continues from ``previous_report`` only when its counter check ran at
    most ``CONSECUTIVE_HOURS`` ago (``previous_report_consecutive``, aged by
    ``previous_report_age_hours``); an older or unstamped one starts every streak afresh,
    though relations it named unstable stay so."""
    if not relations or any(_RELATION.fullmatch(name) is None for name in relations):
        raise ValueError("relations must be schema.relation names")
    if not 1 <= max_keys <= 50_000_000 or not 1 <= sample_keys <= 200_000 or \
            not 1 <= min_page_size <= page_size <= 5000:
        raise ValueError("counter bounds out of range")
    if daily_budget_seconds is not None and not _is_seconds(daily_budget_seconds):
        raise ValueError("daily budget out of range")
    if mirror_bound is None:
        from integrations.source_mirror import load_ingest_config, mirror_options
        mirror_bound = mirror_options(load_ingest_config()).get("max_rows_per_relation")
    started = monotonic()
    catalog = client.relations()
    previous = _previous_hashes(previous_report, COUNTS_CHECK)
    fields = _previous_fields(previous_report, COUNTS_CHECK)
    at = now()
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    age = previous_report_age_hours(previous_report, at, COUNTS_CHECK) if fields else None
    consecutive = age is not None and -CLOCK_SKEW_HOURS <= age <= CONSECUTIVE_HOURS
    streak_fields = fields if consecutive else {}
    before_class = _per_relation(streak_fields, "classification")
    before_defect = _per_relation(streak_fields, "defect_streak")
    before_stable = _per_relation(streak_fields, "stable_streak")
    before_unstable = set(previous_unstable(previous_report))
    seen: set[str] = set()
    out: dict[str, Any] = {}
    names = list(dict.fromkeys(relations))
    rotation = _rotation_state(previous_report, names, at)
    order = sorted(range(len(names)), key=lambda i: (
        rotation[names[i]]["yields"], rotation[names[i]]["completed_at"] or "", i))
    end = None if budget_seconds is None else started + budget_seconds
    daily_end = (None if end is None or daily_budget_seconds is None
                 or daily_budget_seconds <= budget_seconds else started + daily_budget_seconds)
    for rank, index in enumerate(order):
        name, state = names[index], rotation[names[index]]
        extended = daily_end is not None and (state["over_run"]
                                              or state["need"] > budget_seconds)
        deadline = daily_end if extended else end
        remaining = None if deadline is None else deadline - monotonic()
        entry = catalog.get(name)
        # Only a served relation is rotated out: an unserved one is classified (no HTTP).
        if rank and remaining is not None and remaining <= state["need"] and _served(entry):
            item = {"in_catalog": True, "served": True, "complete": False, "stop": "rotation", "pages": 0,
                    "exceeds_mirror_bound": state["exceeds_mirror_bound"],
                    "verdict": "not_reached", "classification": "inconclusive",
                    "seconds": 0.0}
        else:
            item = count_relation(client, name, entry, mirror_bound=mirror_bound,
                                  deadline=deadline, page_size=page_size,
                                  min_page_size=min_page_size, max_keys=max_keys,
                                  sample_keys=sample_keys, sleep=sleep, monotonic=monotonic,
                                  seen_values=seen)
        for key in ENTRY_KEYS:
            item.setdefault(key, None)
        item["rotation_rank"] = rank
        item["budget_extended"] = extended
        # Cut with the first turn and the whole daily deadline, no daily run completes it;
        # it stays named so until a run completes it.
        item["exceeds_daily_bound"] = not item["complete"] and (
            extended and rank == 0 and item["stop"] == "budget"
            or state["exceeds_daily_bound"])
        # Seconds a complete traversal needs: this run's when it completed, at least this
        # run's when it was cut, else (not reached, failed) what the last run knew.
        seconds = item["seconds"] or 0.0
        item["need_seconds"] = (seconds if item["stop"] == "end" else
                                max(seconds, state["need"]) if item["stop"] in CUT_STOPS
                                else state["need"])
        if item["complete"]:
            item["completed_at"], item["completed_distinct_keys"] = stamp, item["distinct_keys"]
        else:
            item["completed_at"] = state["completed_at"]
            item["completed_distinct_keys"] = state["completed_distinct_keys"]
        # An earlier completion never stands for a relation unserved or unreadable now.
        item["complete_in_window"] = bool(item["complete"]) or (
            item["classification"] != "upstream_unavailable"
            and item["completed_at"] is not None and -CLOCK_SKEW_HOURS <= _hours(
                _parse_stamp(item["completed_at"]), at) <= ROTATION_WINDOW_HOURS)
        before = previous.get(name)
        item["previous_run_hash_equal"] = (
            None if before is None or item.get("first_keys_hash") is None
            else before == item["first_keys_hash"])
        item.update(stability_streaks(
            item["classification"], previous_classification=before_class.get(name),
            previous_defect_streak=before_defect.get(name),
            previous_stable_streak=before_stable.get(name),
            previously_unstable=name in before_unstable))
        item["reraised"] = reraise_defect(
            item["classification"], previously_unstable=name in before_unstable,
            previous_defect_streak=before_defect.get(name))
        if item["reraised"]:
            item["defect_streak"] = UNSTABLE_RUNS   # the streak restarts; still unstable
        out[name] = item
    out = {name: out[name] for name in names}
    verdicts: dict[str, int] = {}
    classes: dict[str, list[str]] = {}
    for name, item in out.items():
        verdicts[item["verdict"]] = verdicts.get(item["verdict"], 0) + 1
        classes.setdefault(item["classification"], []).append(name)
    new_defects = sorted((set(classes.get(DEFECT, [])) - before_unstable)
                         | {name for name, item in out.items() if item["reraised"]})
    report = {
        "probe": SCHEMA, "relation_count": len(names),
        "measured_count": sum(1 for item in out.values() if item.get("pages")),
        # Over the rotation window: completed by this run or by one at most
        # ROTATION_WINDOW_HOURS ago (relations[*].completed_at names that run).
        "complete_count": sum(1 for item in out.values() if item["complete_in_window"]),
        "complete_this_run_count": sum(1 for item in out.values() if item.get("complete")),
        "rotation_window_hours": ROTATION_WINDOW_HOURS,
        "mirror_max_rows_per_relation": mirror_bound,
        "budget_seconds": budget_seconds, "daily_budget_seconds": daily_budget_seconds,
        "exceeds_daily_bound": [name for name, item in out.items()
                                if item["exceeds_daily_bound"]],
        "exceeds_daily_bound_count": sum(1 for item in out.values()
                                         if item["exceeds_daily_bound"]),
        "verdicts": verdicts, "by_classification": classes,
        "data_truth_defect_count": len(classes.get("data_truth_defect", [])),
        # Defects not already known unstable from the previous daily report: the gate.
        # A defect on a relation unstable over REREAISE_DAYS counts as new again once.
        "new_data_truth_defects": new_defects,
        "new_data_truth_defect_count": len(new_defects),
        "upstream_unavailable_count": len(classes.get("upstream_unavailable", [])),
        # Counts only, over the stability samples (relations with a natural key).
        "replaced_rows_total": sum(item.get("replaced_rows") or 0 for item in out.values()),
        "ambiguous_replacement_total": sum(item.get("ambiguous_replacements") or 0
                                           for item in out.values()),
        # Sticky: a relation unstable in the previous report and not measured now stays.
        "unstable_relations": sorted({name for name, item in out.items() if item["unstable"]}
                                     | (before_unstable - set(out))),
        "unstable_rule": f"{DEFECT} on {UNSTABLE_RUNS} consecutive daily runs; cleared by "
                         f"{UNSTABLE_RUNS} consecutive conclusive stable runs",
        "previous_report_read": bool(fields),
        "previous_report_age_hours": age,
        "previous_report_consecutive": consecutive,
        "consecutive_max_hours": CONSECUTIVE_HOURS,
        "relations": [{"relation": name, **item} for name, item in out.items()],
        "stores_rows": False,
        "seconds": round(monotonic() - started, 3)}
    report["row_data_check"] = leak_check(report, seen)
    return report


def leak_check(report: Mapping[str, Any], values: Iterable[str]) -> dict[str, int]:
    """``operating.ingestion.row_data_check`` for values held in memory: how many key
    texts (>= 8 characters) were checked and how many occur in the serialized report."""
    text = json.dumps(report, sort_keys=True)
    checked = leaked = 0
    for value in values:
        if len(value) < MIN_LEAK_CHARS:
            continue
        checked += 1
        leaked += value in text
    return {"values_checked": checked, "leaked": leaked}


# ------------------------------------------------------------------ completeness

def completeness(complete: Iterable[str], *, bound: Iterable[str],
                 unstable: Iterable[str] = (), scope: str = "bound") -> dict[str, Any]:
    """The one completeness figure: ``complete_bound_relation_count / bound_total``.

    ``bound`` is the denominator's relations (``bound_relations()``, or a subset a
    caller is required to hold: ``scope`` names which); ``unstable`` relations leave it
    (they are listed in ``unstable_excluded``); ``complete`` names the relations whose
    capture completed. Names and counts only."""
    unstable_set = set(unstable)
    declared = sorted(set(bound))
    denominator = [name for name in declared if name not in unstable_set]
    done = set(complete)
    complete_bound = [name for name in denominator if name in done]
    total = len(denominator)
    return {"metric": COMPLETENESS_METRIC, "scope": scope,
            "bound_declared_total": len(declared), "bound_total": total,
            "complete_bound_relation_count": len(complete_bound),
            "completeness_rate": round(len(complete_bound) / total, 4) if total else None,
            "all_bound_complete": bool(total) and len(complete_bound) == total,
            "bound_incomplete": sorted(set(denominator) - done),
            "unstable_excluded": sorted(unstable_set & set(declared)),
            "unstable_relations": sorted(unstable_set)}


def completeness_ratio(block: Mapping[str, Any] | None) -> str | None:
    """A :func:`completeness` block as ``complete_bound_relation_count/bound_total``
    text (the eval's ``x/y`` rate style), for report blocks with a tight size budget."""
    if not isinstance(block, Mapping):
        return None
    return f"{block['complete_bound_relation_count']}/{block['bound_total']}"


def _schema_bindings(schema: Any = None,
                     domains_dir: str | Path = DOMAINS_DIR) -> list[tuple[str, str]]:
    """(binding id, relation) of every ``portal_api`` object binding: from a loaded
    schema's ``object_types`` when given, else from the curated domain files."""
    if schema is not None:
        types = schema.object_types.values()
    else:
        types = [t for path in sorted(Path(domains_dir).glob("*.json"))
                 if not path.name.startswith("_")
                 for t in json.loads(path.read_text(encoding="utf-8")).get("object_types", [])]
    return sorted({(b["id"], b["relation"]) for t in types for b in t.get("bindings", [])
                   if b.get("source") == "portal_api" and isinstance(b.get("id"), str)
                   and isinstance(b.get("relation"), str)})


def unstable_gaps(unstable: Iterable[str], *, schema: Any = None,
                  domains_dir: str | Path = DOMAINS_DIR) -> list[dict[str, Any]]:
    """Gap ``capture_unstable_ids`` on every binding of an unstable relation, in the
    projector's gap shape (``code``, ``where`` = binding id, ``count``, ``samples``)."""
    names = set(unstable)
    return [{"code": UNSTABLE_GAP, "where": binding, "count": 1, "samples": []}
            for binding, relation in _schema_bindings(schema, domains_dir)
            if relation in names]


# ------------------------------------------------------------------ bound-first coverage

def ordered_full_coverage(client: Any, db_path: str | Path, *,
                          budget_seconds: float | None = None, resolve_sample: int = 5000,
                          bound: Sequence[str] | None = None,
                          unstable: Sequence[str] = (), key_only_unstable: bool = True,
                          deferred: Sequence[str] = (), workers: int = 1,
                          monotonic: Callable[[], float] = time.monotonic,
                          sleep: Callable[[float], None] = time.sleep,
                          **options: Any) -> dict[str, Any]:
    """``integrations.source_mirror.full_coverage_probe`` with bound relations first.

    Same steps and report as that probe (the audience grants one service principal
    every served schema; counts, booleans, timings and relation names only), with
    these changes: ``SourceMirror.sync(order=bound, workers=workers)`` captures the
    relations bound in ``ontology/schema/domains`` first and the rest after, inside one
    budget; ``unstable`` relations (:func:`previous_unstable`) are left out of the sync,
    so the mirror never stages them, and with ``key_only_unstable`` read key-only by the
    counter in the remaining budget (nothing stored); ``deferred`` relations
    (:func:`previous_deferred`: the last run hit the row bound or a paging defect, or
    the counter measured them above the bound) stay in the denominator but are synced
    last, after the key-only reads, so a relation that cannot finish never starves the
    rest; the report adds the :func:`completeness` block, the binding gaps of the
    unstable relations and one ``relation_outcomes`` record per relation not captured
    (outcome, failure class, HTTP status, the service's error code, pages, rows, final
    page size, seconds) so the next failure is evidence-led.
    """
    from integrations.source_mirror import (MAX_BATCH_HANDLES, PROBE_PRINCIPAL, AudienceMap,
                                            SourceMirror, load_ingest_config,
                                            make_citation, mirror_options,
                                            soft_delete_counts)
    from ontology.core import Actor
    if (isinstance(resolve_sample, bool) or not isinstance(resolve_sample, int) or
            not 0 <= resolve_sample <= MAX_BATCH_HANDLES):
        raise ValueError("resolve_sample must be 0..%d" % MAX_BATCH_HANDLES)
    if isinstance(unstable, (str, bytes)) or \
            any(not isinstance(n, str) or _RELATION.fullmatch(n) is None for n in unstable):
        raise ValueError("unstable must be schema.relation names")
    bound_names = sorted(set(bound_relations() if bound is None else bound))
    unstable_set = set(unstable)
    actor = Actor("plx", frozenset({PROBE_PRINCIPAL}))
    started = monotonic()
    options_all = mirror_options(load_ingest_config())
    with SourceMirror(db_path, client, AudienceMap({}, tenant="plx"), tenant="plx",
                      **{**options_all, "monotonic": monotonic, "sleep": sleep,
                         **options}) as mirror:
        catalog, _, error = mirror._catalog(force=True)
        if error:
            raise mirror._catalog_failed(error)   # with the upstream status and code
        schemas = sorted({name.split(".", 1)[0] for name in catalog})
        mirror.set_audience_map(AudienceMap(
            {f"{schema}.*": [PROBE_PRINCIPAL] for schema in schemas}, tenant="plx"))
        bound_set = set(bound_names)
        deferred_set = set(deferred) - unstable_set
        first = [name for name in sorted(catalog) if name in bound_set]
        staged = [name for name in sorted(catalog)
                  if name not in unstable_set and name not in deferred_set]
        report = mirror.sync(actor, relations=staged, order=first,
                             budget_seconds=budget_seconds, workers=workers)
        sync_seconds = monotonic() - started
        key_only_started = monotonic()
        key_only = (_key_only_reads(client, catalog, sorted(unstable_set & set(catalog)),
                                    started=started, budget_seconds=budget_seconds,
                                    mirror_bound=options_all.get("max_rows_per_relation"),
                                    monotonic=monotonic, sleep=sleep)
                    if key_only_unstable else
                    {"relations": {}, "row_data_check": {"values_checked": 0, "leaked": 0}})
        key_only_seconds = monotonic() - key_only_started
        runs = list(report["runs"])
        waits = dict(report["throttle_waits"])
        late = sorted(deferred_set & set(catalog))
        deferred_started = monotonic()
        if late:
            remaining = (None if budget_seconds is None else
                         max(0.0, budget_seconds - (monotonic() - started)))
            later = mirror.sync(actor, relations=late, order=first,
                                budget_seconds=remaining, workers=workers)
            runs += later["runs"]
            waits = {"count": waits["count"] + later["throttle_waits"]["count"],
                     "seconds": round(waits["seconds"] + later["throttle_waits"]["seconds"],
                                      3)}
            report = {**later, "runs": runs, "throttle_waits": waits}
        deferred_sync_seconds = monotonic() - deferred_started
        sample = mirror._db.execute(
            # Only relations with a completed capture and no merge in progress: a batched
            # merge's rows are not served (integrations.source_mirror._merge_batches).
            """SELECT relation, key_json, MIN(capture_id) AS capture_id FROM row_current
               WHERE absent_since_capture IS NULL
               AND relation IN (SELECT relation FROM relation_state)
               AND relation NOT IN (SELECT relation FROM sync_progress
                                    WHERE merge_json IS NOT NULL)
               GROUP BY relation, key_json
               ORDER BY relation, key_json LIMIT ?""", (resolve_sample,)).fetchall()
        handles = [make_citation(row["relation"], json.loads(row["key_json"]),
                                 row["capture_id"], mirror.generation) for row in sample]
        resolve_started = monotonic()
        batch = mirror.resolve_citations(handles, actor)
        resolve_seconds = monotonic() - resolve_started
    coverage = report["coverage"]  # every visible relation, staged or not
    lineage_totals: dict[str, int] = {}
    for item in coverage["relations"]:
        for lineage, count in item["lineage_counts"].items():
            lineage_totals[lineage] = lineage_totals.get(lineage, 0) + count
    by_name = {item["relation"]: item for item in coverage["relations"]}
    complete = [name for name, item in by_name.items() if item.get("complete") is True]
    block = completeness(complete, bound=bound_names, unstable=unstable_set)
    stable_served = [item for item in coverage["relations"]
                     if item["served"] and item["relation"] not in unstable_set]
    key_only_done = [name for name, item in key_only["relations"].items() if item["complete"]]
    accounted = len(complete) + len(key_only_done)
    run_of = {run["relation"]: run for run in runs}
    relations = {}
    for item in coverage["relations"]:
        name = item["relation"]
        run = run_of.get(name, {})
        entry = {"bound": name in bound_set, "complete": item["complete"],
                 "rows_current": item["rows_current"],
                 "rows_missing": item["rows_missing_from_source"],
                 "rows_deleted": item["rows_deleted_in_source"],
                 "pages": (item["latest_capture"] or {}).get("pages"),
                 "outcome": run.get("outcome"), "failure_class": run.get("failure_class"),
                 "seconds": run.get("seconds"), "deferred": name in deferred_set}
        if name in unstable_set:
            entry.update(unstable=True, capture_mode="key_only",
                         key_only=key_only["relations"].get(name))
        relations[name] = entry
    # A bound relation the catalog lists but does not serve (outcome ``unavailable``) is a
    # typed upstream gap, named as a ``relation_unavailable`` part; it never counts complete
    # (it stays in ``bound_incomplete``, ``all_bound_complete`` false). A bound relation
    # incomplete any other way (budget, row bound, paging defect, a failure) is
    # ``bound_incomplete_untyped``: never blocked, always a failure.
    unavailable_bound = sorted({run["relation"] for run in runs
                                if run["outcome"] == "unavailable"
                                and run["relation"] in bound_set
                                and run["relation"] not in unstable_set}
                               - set(complete))
    untyped = sorted(set(block["bound_incomplete"]) - set(unavailable_bound))
    return {
        "probe": "mirror-full-coverage/v3",
        "capture_order": "bound_first",
        "relation_count": len(catalog),
        "catalog_relation_count": coverage["catalog_relation_count"],
        "served_relation_count": coverage["served_relation_count"],
        "unavailable_relation_count": coverage["unavailable_relation_count"],
        "complete_relation_count": coverage["complete_relation_count"],
        "all_served_complete": coverage["all_served_complete"],
        "complete_equals_catalog": (coverage["complete_relation_count"] ==
                                    coverage["catalog_relation_count"] == len(catalog)),
        # Secondary 93-relation figures that count an unstable relation as accounted
        # for when its key-only read completed (it is never staged by the mirror).
        "all_stable_served_complete": bool(stable_served) and
        all(item["complete"] for item in stable_served),
        "key_only_relation_count": len(key_only["relations"]),
        "key_only_complete_count": len(key_only_done),
        "accounted_relation_count": accounted,
        "accounted_equals_catalog": accounted == coverage["catalog_relation_count"]
        == len(catalog),
        "key_only_row_data_check": key_only["row_data_check"],
        "completeness": block,
        "bound_declared_total": block["bound_declared_total"],
        "bound_total": block["bound_total"],
        "bound_in_catalog_count": len(first),
        "complete_bound_relation_count": block["complete_bound_relation_count"],
        "completeness_rate": block["completeness_rate"],
        "all_bound_complete": block["all_bound_complete"],
        "bound_incomplete": block["bound_incomplete"],
        "bound_incomplete_untyped": untyped,
        "blocked_upstream": [{"part": name, "source": "portal-workbench-api",
                              "cause": "relation_unavailable"}
                             for name in unavailable_bound],
        "unstable_relations": block["unstable_relations"],
        "unstable_bindings": [gap["where"] for gap in unstable_gaps(unstable_set)],
        "in_progress_count": coverage["in_progress_count"],
        "failed_count": coverage["failed_count"],
        "run_outcomes": _outcomes(runs),
        "bound_run_outcomes": _outcomes(run for run in runs if run["relation"] in bound_set),
        "workers": workers,
        "failure_classes": _failure_classes(runs),
        "relation_outcomes": [_outcome_record(run, bound_set, deferred_set)
                              for run in runs if run["outcome"] != "captured"],
        "slowest_relations": [
            {"relation": run["relation"], "seconds": run.get("seconds"),
             "rows_received": run.get("rows_received"), "pages": run.get("pages")}
            for run in sorted((r for r in runs if r["outcome"] == "captured"),
                              key=lambda r: -(r.get("seconds") or 0))[:SLOWEST]],
        "deferred_relations": sorted(deferred_set),
        "defer_next_run": sorted(
            {run["relation"] for run in runs if run.get("failure_class") in DEFER_CLASSES
             or run.get("defer_next_run") is True} |
            {run["relation"] for run in runs
             if run["relation"] in deferred_set and run["outcome"] != "captured"}),
        "rows_current_total": coverage["rows_current_total"],
        "rows_missing_total": coverage["rows_missing_total"],
        **soft_delete_counts(coverage),
        "lineage_totals": lineage_totals,
        "relations": relations,
        # The main sync, the key-only reads of unstable relations and the late sync
        # of deferred relations, each timed on its own.
        "sync_seconds": round(sync_seconds, 3),
        "key_only_seconds": round(key_only_seconds, 3),
        "deferred_sync_seconds": round(deferred_sync_seconds, 3),
        "throttle_waits": report["throttle_waits"],
        "handles_resolved": len(handles),
        "handles_ok": batch["ok_count"],
        "resolve_all_ok": batch["ok_count"] == len(handles),
        "batch_resolve_seconds": round(resolve_seconds, 3),
        "batch_resolve_under_3s": resolve_seconds < 3.0,
        "atomic_upstream_snapshot": False, "deletions_proven": False}


def _key_only_reads(client: Any, catalog: Mapping[str, Any], names: Sequence[str], *,
                    started: float, budget_seconds: float | None,
                    mirror_bound: int | None, monotonic: Callable[[], float],
                    sleep: Callable[[float], None]) -> dict[str, Any]:
    """Key-only counts of the unstable relations in what is left of the budget (each an
    equal share), through the no-store counter: nothing is staged or written."""
    seen: set[str] = set()
    out: dict[str, Any] = {}
    for index, name in enumerate(names):
        deadline = None
        if budget_seconds is not None:
            remaining = max(0.0, budget_seconds - (monotonic() - started))
            deadline = monotonic() + remaining / (len(names) - index)
        item = count_relation(client, name, catalog.get(name), mirror_bound=mirror_bound,
                              deadline=deadline, sample_keys=KEY_ONLY_SAMPLE, sleep=sleep,
                              monotonic=monotonic, seen_values=seen, replacements=False)
        out[name] = {key: item.get(key) for key in (
            "complete", "stop", "rows_seen", "distinct_keys", "pages", "verdict",
            "classification", "exceeds_mirror_bound")}
    result = {"relations": out}
    result["row_data_check"] = leak_check(result, seen)
    return result


SLOWEST = 10   # captured relations listed by read time in the coverage report
OUTCOME_FIELDS = ("outcome", "failure_class", "http_status", "upstream_error_code",
                  "exception_type", "pages", "rows_received", "page_size_final", "seconds")


def _outcome_record(run: Mapping[str, Any], bound: set[str],
                    deferred: set[str]) -> dict[str, Any]:
    """One relation's run as counts and codes only (never the error message)."""
    return {"relation": run["relation"], "bound": run["relation"] in bound,
            "deferred": run["relation"] in deferred,
            **{field: run.get(field) for field in OUTCOME_FIELDS}}


def _failure_classes(runs: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in runs:
        if run.get("failure_class"):
            counts[run["failure_class"]] = counts.get(run["failure_class"], 0) + 1
    return counts


def _outcomes(runs: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in runs:
        counts[run["outcome"]] = counts.get(run["outcome"], 0) + 1
    return counts


# ------------------------------------------------------------------ upstream finding

FINDING_FIELDS = ("classification", "verdict", "rows_seen", "distinct_keys",
                  "first_read_keys", "seen_in_both", "missing_in_second", "new_in_window",
                  "ratio_seen_in_both", "exceeds_mirror_bound", "defect_streak")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_CLASSIFICATION = re.compile(r"[a-z_]{1,40}")


def _cell(value: Any) -> str:
    """A table cell from a recorded field: numbers, booleans and short enums only."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and _CLASSIFICATION.fullmatch(value):
        return value
    return "-"


def upstream_findings(paths: Sequence[str | Path]) -> str:
    """The Portal ETL finding (markdown) from daily verifier reports, oldest first.

    Reads only the recorded fields of the ``mirror.relation_counts`` and
    ``mirror.full_coverage`` checks (counts, booleans, relation names and enums, as the
    verifier already scrubbed them); never a row value, key, hash or citation."""
    runs = []
    for path in paths:
        try:
            report = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stamp = report.get("generated_at") if isinstance(report, Mapping) else None
        fields = _previous_fields(path, COUNTS_CHECK)
        if fields and isinstance(stamp, str) and _STAMP.fullmatch(stamp):
            runs.append((stamp, report.get("main_sha"), fields,
                         _previous_fields(path, "mirror.full_coverage")))
    # latest.json repeats one <sha>.json: one run per generated_at.
    runs = sorted({run[0]: run for run in runs}.values(), key=lambda run: run[0])
    lines = ["# Upstream findings: Portal ETL id stability",
             "",
             "Generated by `python3 -m scripts.mirror_measure upstream-findings` from the "
             "daily live-verifier reports. Counts "
             "and classifications only: no row value, key or hash. Audience: Petra "
             "(Portal ETL owners).",
             "",
             f"Rule: a relation classified `{DEFECT}` on {UNSTABLE_RUNS} consecutive daily "
             "runs is unstable: it leaves the completeness denominator, the mirror reads it "
             f"key-only, and its bindings carry the gap `{UNSTABLE_GAP}`.",
             ""]
    if not runs:
        lines += ["## Status", "",
                  "No daily `mirror.relation_counts` result yet. Nothing is claimed.", ""]
        return "\n".join(lines)
    stamp, sha, latest, coverage = runs[-1]
    unstable = sorted({n for n in latest.get("unstable_relations") or []
                       if isinstance(n, str) and _RELATION.fullmatch(n)})
    sha_text = sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{7,40}", sha) else "-"
    lines += ["## Latest daily run", "",
              f"- generated_at: {stamp} (main {sha_text[:12]}); daily runs read: {len(runs)}",
              f"- mirror max_rows_per_relation: "
              f"{_cell(latest.get('mirror_max_rows_per_relation'))}",
              f"- unstable relations ({len(unstable)}): "
              + (", ".join(f"`{name}`" for name in unstable) or "none")]
    if coverage:
        lines.append(f"- bound completeness: {_cell(coverage.get('complete_bound_relation_count'))}"
                     f"/{_cell(coverage.get('bound_total'))} (catalog: "
                     f"{_cell(coverage.get('complete_relation_count'))}/"
                     f"{_cell(coverage.get('catalog_relation_count'))})")
    history: dict[str, list[str]] = {}
    for _, _, fields, _ in runs:
        for name, value in _per_relation(fields, "classification").items():
            history.setdefault(name, []).append(_cell(value))
    columns = {field: _per_relation(latest, field) for field in FINDING_FIELDS}
    names = sorted(columns["classification"])
    lines += ["", "## Per relation (latest run)", "",
              "| relation | " + " | ".join(FINDING_FIELDS) + " | unstable | history |",
              "|" + "---|" * (len(FINDING_FIELDS) + 3)]
    for name in names:
        cells = [_cell(columns[field].get(name)) for field in FINDING_FIELDS]
        lines.append(f"| `{name}` | " + " | ".join(cells) +
                     f" | {_cell(name in unstable)} | {' > '.join(history.get(name, []))} |")
    defects = [name for name in names if columns["classification"].get(name) == DEFECT]
    lines += ["", "## Finding for the Portal ETL", ""]
    if not defects:
        lines.append("No relation is classified as a data-truth defect in the latest run.")
    else:
        lines += [f"{len(defects)} relation(s) changed ids between two key reads of one run "
                  "(`ids_reinserted`: new keys inside the first read's key window; "
                  "`ids_replaced`: at least half of the sampled keys gone). This fits a child "
                  "ETL that deletes and re-inserts rows under fresh ids each run, not real "
                  "volume. Ask: keep ids stable across ETL runs (upsert on the natural key), "
                  "or expose the natural key in the Workbench catalog.", ""]
        lines += [f"- `{name}`: {_cell(columns['verdict'].get(name))}, "
                  f"{_cell(columns['distinct_keys'].get(name))} distinct keys, "
                  f"{_cell(columns['ratio_seen_in_both'].get(name))} of the sample seen "
                  f"in both reads, defect streak {_cell(columns['defect_streak'].get(name))}"
                  for name in defects]
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI

def _client(environ: Mapping[str, str]) -> Any:
    base_url, key = environ.get("PLX_WORKBENCH_API_URL"), environ.get("PLX_WORKBENCH_API_KEY")
    if not base_url or not key:
        raise LookupError("missing environment variables: PLX_WORKBENCH_API_URL, "
                          "PLX_WORKBENCH_API_KEY")
    from integrations.workbench_read_api import WorkbenchReadApiClient
    return WorkbenchReadApiClient(base_url, key)


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         client: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.mirror_measure",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    counts = commands.add_parser("relation-counts", help="no-store keyset counter")
    counts.add_argument("--relation", action="append", required=True)
    counts.add_argument("--budget-seconds", type=float)
    counts.add_argument("--daily-budget-seconds", type=float,
                        help="deadline for a relation one run's budget cannot hold")
    counts.add_argument("--max-keys", type=int, default=MAX_KEYS)
    counts.add_argument("--sample-keys", type=int, default=SAMPLE_KEYS)
    counts.add_argument("--page-size", type=int, default=PAGE_SIZE)
    counts.add_argument("--min-page-size", type=int, default=MIN_PAGE_SIZE)
    counts.add_argument("--previous-report", type=Path,
                        help="an earlier plx-live-verify report: compare first_keys_hash")
    commands.add_parser("bound-relations", help="relations bound in ontology/schema/domains")
    findings = commands.add_parser("upstream-findings",
                                   help="Portal ETL finding (markdown) from daily reports")
    findings.add_argument("reports", nargs="*", type=Path,
                          help="daily verifier reports (results/daily/*.json)")
    findings.add_argument("--output", type=Path, help="write here (devloop/UPSTREAM-FINDINGS.md)")
    args = parser.parse_args(argv)
    if args.command == "upstream-findings":
        text = upstream_findings(args.reports)
        if args.output is not None:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    env = os.environ if environ is None else environ
    try:
        if args.command == "bound-relations":
            names = bound_relations()
            report: dict[str, Any] = {"probe": "bound-relations/v1", "bound_total": len(names),
                                      "relations": names}
        else:
            report = relation_counts(client or _client(env), args.relation,
                                     budget_seconds=args.budget_seconds,
                                     daily_budget_seconds=args.daily_budget_seconds,
                                     max_keys=args.max_keys, sample_keys=args.sample_keys,
                                     page_size=args.page_size,
                                     min_page_size=args.min_page_size,
                                     previous_report=args.previous_report)
    except Exception as exc:  # the class only; messages may carry URLs
        from integrations.live_source_guard import ReadOnlyGuardError
        code = "read_only_guard" if isinstance(exc, ReadOnlyGuardError) else type(exc).__name__
        print(json.dumps({"error": code, "probe": args.command}), file=sys.stderr)
        return 2 if code == "read_only_guard" else 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
