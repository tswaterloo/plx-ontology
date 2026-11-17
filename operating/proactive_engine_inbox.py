"""The proactive inbox: where it lives, how findings are folded and stored per reader, what a
reader may see, the cached unjudged tables, shown wording, visits and pass records."""

from __future__ import annotations

import dataclasses
from collections import Counter, OrderedDict, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import store as ostore

from . import po_status
from . import proactive_gaps as pg
from operating import declared_schema as _schemas
from operating.declared_schema import proactive_inbox as _PROACTIVE_INBOX
from operating.declared_schema.proactive_inbox import _VISITS_DDL

from .proactive_engine_config import (
    DETECTOR_SECONDS, DETECTOR_SECONDS_DEFAULT, DETECTORS, INBOX_SCHEMA, INCREMENTAL,
    KEEP_RUNS, ON_RUN_THREAD, PRIORITY, REQUIRES_GRANT, SET_BASED, UNREADABLE, WHOLE_ONLY,
)
from .proactive_engine_purchasing import _ORDER
from .proactive_engine_drafts import PROMISE_CAVEAT, PROMISE_CAVEAT_TEXT
from .proactive_engine_findings import _checked


def _read_cache(inbox: Path) -> dict[str, dict[str, Any]]:
    """The incremental detectors' last readings (``event_cache``): detector -> root ->
    reading. None stored (or no inbox yet): every root is read."""
    if not inbox.is_file():
        return {}
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    with closing(_open_inbox(inbox)) as conn:
        for row in conn.execute("SELECT detector, root, reading FROM event_cache"):
            try:
                out[row["detector"]][row["root"]] = json.loads(row["reading"])
            except ValueError:
                continue   # an unreadable reading is read again
    return dict(out)


def inbox_path(store_path: Any) -> Path:
    """The inbox file of a projection store: beside it, named after it."""
    store = Path(store_path)
    return store.with_name(store.name + ".gap-inbox.sqlite")


def reader_key(actor: Any) -> str:
    """The reader an acknowledgement belongs to: tenant and sorted principals."""
    return f"{actor.tenant}:" + ",".join(sorted(actor.principals))


def _open_inbox(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    _schemas.apply(conn, _PROACTIVE_INBOX)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)",
                     (INBOX_SCHEMA,))
    elif row[0] != INBOX_SCHEMA:
        conn.close()
        raise ValueError("gap_inbox_schema_mismatch")
    return conn


def _people(store_path: Path) -> Any:
    """The people exclusion (environment, else the passes' env file:
    ``integrations.people_exclusion.environ_with_exclusion``) with the tenant's mail
    directory and domains
    from the mail capture of the state directory ``store_path`` sits in (the projection
    file, or a generation's store under it); unreadable: an unavailable guard."""
    from integrations.people_exclusion import environ_with_exclusion
    from .chunking.permissions import load_exclusion

    parent = store_path.parent
    state = parent.parent.parent.parent if parent.parent.name == "generations" \
        else parent.parent
    try:
        # the passes' env file when this process was started without the exclusion
        return load_exclusion(state, environ_with_exclusion())
    except Exception:  # noqa: BLE001 - no directory: the rule is blocked, never guessed
        return None


COUNTED_KEYS = ("late", "at_risk", "unconfirmed", "undated")


def _uncount(result: dict[str, Any], kept: Sequence[Mapping[str, Any]],
             gone: Mapping[str, str]) -> None:
    """Take each order whose every gap the filter dropped (``gone``: order -> closed or
    historic) out of the rule's per-order counts (``counted``) into ``historic`` or
    ``closed``: the counts then say what is listed."""
    counted, counts = result.get("counted"), result.get("counts")
    if not isinstance(counted, Mapping) or not isinstance(counts, dict):
        return
    left = {o for g in kept for o in [((g.get("follow_up") or {}).get(_ORDER) or {})
                                      .get("key")] if o}
    for order, why in gone.items():
        if order in left or order not in counted:
            continue
        for key in counted[order]:
            if key in COUNTED_KEYS and counts.get(key):
                counts[key] -= 1
        counts[why] = counts.get(why, 0) + 1


def _summary(results: Mapping[str, Mapping[str, Any]], gaps: Mapping[str, Mapping[str, Any]],
             scope: Sequence[tuple[str, str, int]]) -> dict[str, Any]:
    cited = [g for g in gaps.values() if all(g["groups"])]
    return {"scanned": sum(n for _, _, n in scope),
            "scope_truncated": any(results[d]["status"] in ("truncated", "failed")
                                   for d in SET_BASED if d in results),
            "truncated": any(r["status"] in ("truncated", "failed") for r in results.values()),
            "gaps_found": len(gaps), "gaps_cited": len(cited),
            "gaps_uncited": len(gaps) - len(cited),
            "with_follow_up": sum(1 for g in gaps.values() if g["follow_up"]["who_can_answer"]),
            "gaps_by_kind": dict(sorted(Counter(g["kind"] for g in gaps.values()).items())),
            "detectors": {name: results[name]["status"] for name in DETECTORS
                          if name in results},
            "detector_reasons": {name: results[name]["reasons"] for name in DETECTORS
                                 if name in results and results[name]["reasons"]},
            "not_evaluated": {name: results[name]["not_evaluated"] for name in DETECTORS
                              if name in results and results[name]["not_evaluated"]},
            "reused": {name: results[name]["reused"] for name in INCREMENTAL
                       if "reused" in results.get(name, {})},
            "po_lateness": results.get("late_or_unconfirmed_po", {}).get("counts"),
            # per detector: of the records it should judge, how many it judged and why
            # the others were not (``_coverage``)
            "coverage": {name: results[name]["coverage"] for name in DETECTORS
                         if name in results and results[name].get("coverage")},
            "top_finding": results.get("orders_at_risk", {}).get("finding")}


_POPULATIONS_KEY = "populations"   # inbox meta: per rule, the populations of its last fold


def _populations(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_POPULATIONS_KEY,)).fetchone()
    try:
        held = json.loads(row[0]) if row else {}
    except ValueError:
        held = {}
    return held if isinstance(held, dict) else {}


@dataclasses.dataclass(frozen=True)
class Scope:
    """An incremental pass (``operating.proactive_incremental``): ``subjects``, per rule,
    the records it judges again (every subject whose declared inputs hold a changed
    record, or whose truth moved with the day); ``read``, every record those judgements
    read; ``audience_keys``: the store's keys (the generation's and its changed records')."""
    subjects: Mapping[str, frozenset[str]]
    read: frozenset[str]
    audience_keys: frozenset[str] | None = None


def _fold_scoped(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
                 gaps: Mapping[str, Mapping[str, Any]], run_at: str, scope: Scope) -> None:
    """Fold an incremental pass: the gaps it found are written as a full run writes them
    (``_upsert_gaps``), and of each rule that judged its subjects whole (``complete``) an
    open gap about one of them that it no longer finds is resolved. The run records,
    counts, coverage and generation stay the last full run's."""
    _upsert_gaps(conn, results, gaps, run_at)
    _note_unchecked(conn, results, scope.subjects)
    for name, result in results.items():
        if result["status"] != "complete":
            continue
        unchecked = {o for o in result.get("unchecked") or () if isinstance(o, str)}
        conn.execute(
            "UPDATE gaps SET state = 'resolved', resolved_at = ? WHERE state = 'open' "
            "AND detector = ? AND object_id IN (SELECT value FROM json_each(?)) "
            "AND gap_id NOT IN (SELECT value FROM json_each(?))",
            (run_at, name, json.dumps(sorted(set(scope.subjects.get(name, ())) - unchecked)),
             json.dumps(sorted(gaps))))


def _fold(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
          gaps: Mapping[str, Mapping[str, Any]], scope: Sequence[tuple[str, str, int]],
          run_at: str, as_of: date, digest: str | None, summary: Mapping[str, Any], *,
          record_run: bool = True, whole_scope: bool = False) -> None:
    if record_run:
        # a run of some detectors only (the vendor-mail pass) is no build's run: it never
        # moves the run the inbox is read as of ("new this generation", stale, as of)
        conn.execute("INSERT INTO runs (run_at, as_of, store_digest, summary) VALUES "
                     "(?, ?, ?, ?)", (run_at, as_of.isoformat(), digest,
                                      json.dumps(summary, sort_keys=True)))
        conn.execute("DELETE FROM runs WHERE run_id NOT IN (SELECT run_id FROM runs "
                     "ORDER BY run_id DESC LIMIT ?)", (KEEP_RUNS,))
    if whole_scope or set(SET_BASED) <= set(results):   # some detectors only: scope kept
        conn.execute("DELETE FROM scope")
        conn.executemany("INSERT INTO scope VALUES (?, ?, ?)", scope)
    for name, result in results.items():
        if result["status"] == "not_configured" and conn.execute(
                "SELECT 1 FROM detector_runs WHERE detector = ?", (name,)).fetchone():
            continue   # a run without this detector's input says nothing about its gaps
        conn.execute(
            "INSERT INTO detector_runs VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (detector) DO "
            "UPDATE SET status = excluded.status, reasons = excluded.reasons, found = "
            "excluded.found, not_evaluated = excluded.not_evaluated, run_at = excluded.run_at",
            (name, result["status"], json.dumps(result["reasons"]), len(result["gaps"]),
             json.dumps(result["not_evaluated"], sort_keys=True), run_at))
        # how many records the rule had to judge this run and how many it judged:
        # the briefing's judged share; NULL when the rule does not count them, never 0
        conn.execute(
            "INSERT INTO detector_checked VALUES (?, ?, ?, ?) ON CONFLICT (detector) DO "
            "UPDATE SET checked = excluded.checked, judged = excluded.judged, "
            "run_at = excluded.run_at", (name, *_checked(result), run_at))
        # The records this run's rule left unjudged with each skip's cause, replacing the
        # last run's: a reader is told the count of those it may read whose whole cause it
        # may read (``read_inbox``), never of the others.
        conn.execute("DELETE FROM unjudged_causes WHERE detector = ?", (name,))
        conn.execute("DELETE FROM unjudged_reasons WHERE detector = ?", (name,))
        conn.executemany("INSERT OR IGNORE INTO unjudged_reasons VALUES (?, ?, ?)",
                         [(name, n, str(skip.get("reason") or "not_judged"))
                          for n, skip in enumerate(result.get("unjudged") or ())])
        conn.executemany(
            "INSERT OR IGNORE INTO unjudged_causes VALUES (?, ?, ?, ?, ?)",
            [(name, n, object_id, markings, int(object_id in skip["subjects"]))
             for n, skip in enumerate(result.get("unjudged") or ())
             for object_id, markings in skip["cause"]])
        if result["status"] != "not_configured":
            conn.execute("INSERT OR REPLACE INTO detector_history VALUES (?, ?, ?, ?)",
                         (name, run_at, result["status"], len(result["gaps"])))
            # what was found per gap kind, and per kind whether late by the status rule
            # (``kind:late``/``kind:not_late``): "seen live" is said per event type
            found = Counter()
            for gap in result["gaps"]:
                late = gap["follow_up"].get("days_late") is not None
                found.update([gap["kind"], f"{gap['kind']}:{'late' if late else 'not_late'}"])
            conn.executemany("INSERT OR REPLACE INTO kind_history VALUES (?, ?, ?)",
                             [(kind, run_at, n) for kind, n in sorted(found.items())])
        if isinstance(result.get("cache"), Mapping):
            # merged, never replaced wholesale (a build's run and the vendor pass may fold
            # one after the other): a root no longer named by recent mail drops, every
            # reading this run holds is stored, the others stay as they were
            conn.execute("DELETE FROM event_cache WHERE detector = ? AND root NOT IN (SELECT "
                         "value FROM json_each(?))", (name, json.dumps(result["roots"])))
            conn.executemany("INSERT OR REPLACE INTO event_cache VALUES (?, ?, ?)", [
                (name, root, json.dumps(reading, sort_keys=True))
                for root, reading in result["cache"].items()])
        conn.execute(
            "INSERT INTO rule_coverage VALUES (?, ?, ?) ON CONFLICT (detector) DO UPDATE SET "
            "unattributed = excluded.unattributed, run_at = excluded.run_at",
            (name, int(result.get("unattributed") or 0), run_at))
        if result["status"] in READ_WHOLE_DATA:
            _note_generation(conn, name, digest, run_at)
    _upsert_gaps(conn, results, gaps, run_at)
    complete = [name for name, result in results.items() if result["status"] == "complete"]
    _note_unchecked(conn, results)
    conn.execute(
        "UPDATE gaps SET state = 'resolved', resolved_at = ? WHERE state = 'open' "
        "AND detector IN (SELECT value FROM json_each(?)) "
        "AND gap_id NOT IN (SELECT value FROM json_each(?)) "
        "AND (detector || '|' || object_id) NOT IN (SELECT value FROM json_each(?))",
        (run_at, json.dumps(complete), json.dumps(sorted(gaps)),
         json.dumps(_unchecked_keys(results))))


# Inbox meta key per detector: the subjects its last run read but could not judge (a failed
# read, an undecided figure; the rule's result ``unchecked``). Their open gaps are neither
# found again nor resolved by that run: they stay listed, marked as not rechecked
# (``read_inbox``), until a run judges them.
_UNCHECKED_KEY = "unchecked:"


def _unchecked_keys(results: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(f"{name}|{oid}" for name, result in results.items()
                  for oid in result.get("unchecked") or () if isinstance(oid, str))


def _note_unchecked(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
                    scoped: Mapping[str, Iterable[str]] | None = None) -> None:
    """Record each rule's unchecked subjects (a whole run replaces its list; an incremental
    pass replaces only its own subjects' entries, ``scoped``)."""
    for name, result in results.items():
        key = _UNCHECKED_KEY + name
        now = {o for o in result.get("unchecked") or () if isinstance(o, str)}
        if scoped is not None:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            try:
                held = set(json.loads(row[0])) if row is not None else set()
            except ValueError:
                held = set()
            now |= held - set(scoped.get(name, ()))
        if now:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (key, json.dumps(sorted(now))))
        else:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def _held_unchecked(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(rule, subject) of every subject a rule's last run could not judge; an unreadable
    record holds none (its gaps then follow the rule's status alone)."""
    out: set[tuple[str, str]] = set()
    for key, value in conn.execute("SELECT key, value FROM main.meta WHERE key LIKE ?",
                                   (_UNCHECKED_KEY + "%",)):
        try:
            ids = json.loads(value)
        except ValueError:
            continue
        out |= {(key[len(_UNCHECKED_KEY):], o) for o in ids if isinstance(o, str)}
    return out


def _upsert_gaps(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
                 gaps: Mapping[str, Mapping[str, Any]], run_at: str) -> None:
    """Write each found gap (a resolved one reopened, its acknowledgements dropped)."""
    for gap in gaps.values():
        before = conn.execute("SELECT state FROM gaps WHERE gap_id = ?", (gap["id"],)).fetchone()
        if before is not None and before["state"] == "resolved":
            conn.execute("DELETE FROM acks WHERE gap_id = ?", (gap["id"],))   # reopened
        elif before is not None and gap["kind"] in WHOLE_ONLY \
                and results[gap["detector"]]["status"] != "complete":
            # a money claim summed over a run that did not finish may be a part: the open
            # gap keeps the wording, citations and time of the last run that did (never a
            # fresh last-seen: over newer data it is then listed marked as not rechecked,
            # ``_carried``, never as current)
            continue
        conn.execute(
            "INSERT INTO gaps VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL) "
            "ON CONFLICT (gap_id) DO UPDATE SET summary = excluded.summary, follow_up = "
            "excluded.follow_up, priority = excluded.priority, state = 'open', last_seen = "
            "excluded.last_seen, resolved_at = NULL",
            (gap["id"], gap["kind"], gap["detector"], gap["object_id"], gap["object_type"],
             gap["summary"], json.dumps(gap["follow_up"], sort_keys=True),
             PRIORITY.index(gap["follow_up"]["priority"]), run_at, run_at))
        conn.execute("DELETE FROM gap_cites WHERE gap_id = ?", (gap["id"],))
        rows = {(grp, c[0]): (gap["id"], grp, c[0], c[1], c[2])
                for grp, group in enumerate(gap["groups"]) for c in group}
        for c in gap["extra"]:
            rows.setdefault((-1, c[0]), (gap["id"], -1, c[0], c[1], c[2]))
        conn.executemany("INSERT INTO gap_cites VALUES (?, ?, ?, ?, ?)", rows.values())
        conn.execute("DELETE FROM gap_requires WHERE gap_id = ?", (gap["id"],))
        conn.executemany("INSERT INTO gap_requires VALUES (?, ?)",
                         [(gap["id"], key) for key in sorted(set(gap.get("requires", ())))])


# A detector whose run ended in one of these read the store it ran over: what it found is
# that store's (a truncated run's findings stand, its unfound older gaps do not).
READ_WHOLE_DATA = ("complete", "truncated")
_GENERATION_KEY = "generation:"   # inbox meta key per detector: the data its gaps are of


def _note_generation(conn: sqlite3.Connection, detector: str, digest: str | None,
                     run_at: str) -> None:
    """Record that ``detector`` ran over the store of ``digest`` at ``run_at``: the first
    run over that data is kept (``since``), so a gap last seen before it is of older data."""
    key = _GENERATION_KEY + detector
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        held = json.loads(row[0]) if row is not None else None
    except ValueError:
        held = None
    # and the code it ran: a gap found by older code than the deployed engine is of
    # older code (``_current_since``), never shown as current (``operating.derived_status``)
    from .proactive_engine import engine_code as _engine_code  # read there: tests patch it
    code = _engine_code()
    if isinstance(held, dict) and held.get("digest") == digest \
            and isinstance(held.get("since"), str) and held.get("code") == code:
        return
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (key, json.dumps({"digest": digest, "since": run_at, "code": code},
                                  sort_keys=True)))


def engine_code() -> str | None:
    """The deployed engine's code version (``operating.derived_status``): what each rule's
    record names, so findings of older code are told apart from this code's."""
    from . import derived_status
    return derived_status.version("proactive-inbox")


def _moment(text: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _generation_of(stamp: Any) -> str | None:
    """The generation's content digest within a run's data stamp: a SQLite store's digest
    as it is; Postgres's ``<digest>@<data version>`` (``ontology_reader.change_stamp``) the
    part before the ``@`` (``proactive_incremental.generation_digest``)."""
    if not isinstance(stamp, str):
        return None
    return stamp.partition("@")[0] or None


def _current_since(conn: sqlite3.Connection, served: str | None,
                   older: set[str] | None = None, *, by_generation: bool = False
                   ) -> tuple[dict[str, datetime | None], dict[str, str | None]]:
    """Per detector, from when its gaps are of the data served now (``served``: the store's
    content digest): ``{detector: since}`` for those that ran over it (None: every gap, an
    inbox written before per-detector records whose last full run was over this data), and
    ``{detector: last run}`` for those that did not (their gaps are of older data: never
    shown as current, ``read_inbox``'s ``refreshing``). A rule whose last run was by
    other code than the deployed engine's is behind too (``older``: its name added).
    ``by_generation``: data is compared by generation only (``_generation_of``), never by
    Postgres's data version: what the scheduler asks (``rules_behind``, ``served_behind``),
    the delta layers between being the incremental pass's to fold."""
    older = set() if older is None else older

    def same(held: Any, now: Any) -> bool:
        if not by_generation:
            return held == now
        return _generation_of(held) is not None and _generation_of(held) == _generation_of(now)
    held: dict[str, Any] = {}
    for key, value in conn.execute("SELECT key, value FROM main.meta WHERE key LIKE ?",
                                   (_GENERATION_KEY + "%",)):
        try:
            held[key[len(_GENERATION_KEY):]] = json.loads(value)
        except ValueError:
            held[key[len(_GENERATION_KEY):]] = None   # unreadable: of no known data
    last = conn.execute("SELECT store_digest FROM main.runs ORDER BY run_id DESC "
                        "LIMIT 1").fetchone()
    ran = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, run_at FROM main.detector_runs")}
    statuses = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, status FROM main.detector_runs")}
    current: dict[str, datetime | None] = {}
    behind: dict[str, str | None] = {}
    from .proactive_engine import engine_code as _engine_code  # read there: tests patch it
    code = _engine_code()
    for name in sorted(set(DETECTORS) | set(ran) | set(held)):
        record = held.get(name, ...)
        if (isinstance(record, dict) and (code is None or record.get("code") != code)) or (
                record is ... and statuses.get(name) in READ_WHOLE_DATA):
            # found by older code than the engine deployed (or one not known): its gaps
            # wait for a run of this code (the deploy's refresh), as older data's do
            behind[name] = ran.get(name)
            older.add(name)
            continue
        if record is ...:
            # no per-detector record (an older engine's inbox): its last full run says
            if last is not None and same(last[0], served):
                current[name] = None
            else:
                behind[name] = ran.get(name)
            continue
        since = _moment(record.get("since")) if isinstance(record, dict) else None
        if isinstance(record, dict) and same(record.get("digest"), served) \
                and since is not None:
            current[name] = since
        else:
            behind[name] = ran.get(name)
    return current, behind


def _of_served(row: Mapping[str, Any], current: Mapping[str, datetime | None]) -> bool:
    """Whether a gap was found over the data served now (else it may say what the record
    pages no longer do: never shown as current)."""
    if row["detector"] not in current:
        return False
    since = current[row["detector"]]
    if since is None:
        return True
    # an open gap was found again over it; a resolved one was resolved by a run over it
    keys = row.keys() if hasattr(row, "keys") else ()
    resolved = "state" in keys and row["state"] == "resolved"
    seen = _moment(row["resolved_at"] if resolved else row["last_seen"])
    return seen is not None and seen >= since


def _reader_view(conn: sqlite3.Connection, may_read: Callable[[str], bool],
                 held: Iterable[str]) -> tuple[str, str]:
    """(visible audience keys, allowed markings values) as JSON, from the store's keys."""
    row = conn.execute("SELECT value FROM st.store_meta WHERE key = 'audience_keys'").fetchone()
    keys = json.loads(row[0]) if row is not None else None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        keys = [r[0] for r in conn.execute("SELECT DISTINCT audience_key FROM st.objects")]
    if _has_requires(conn):   # a gap's required audiences, held by no record maybe
        keys = list(dict.fromkeys([*keys, *(r[0] for r in conn.execute(
            "SELECT DISTINCT audience_key FROM gap_requires ORDER BY audience_key"))]))
    visible = []
    for key in keys:
        try:
            if may_read(key) is True:
                visible.append(key)
        except Exception:  # noqa: BLE001 - a failing grant check denies
            continue
    return json.dumps(visible), json.dumps(ostore.allowed_marking_sets(held))


def _owned(follow_up: Any, kind: str, reader: Any) -> bool:
    """A gap of a person's own mail (``proactive_commitments.OWNER_KINDS``) only for that
    person's reader (its ``employee:<oid>`` principal); any other kind for every reader."""
    from .proactive_commitments import owned_by

    return owned_by(follow_up if isinstance(follow_up, Mapping) else {}, kind, reader)


_OWNER_KINDS = frozenset({"needs_my_reply", "reply_owed"})   # ``proactive_commitments.OWNER_KINDS``


def _has_requires(conn: sqlite3.Connection) -> bool:
    """An inbox written before ``gap_requires`` has none: its ``REQUIRES_GRANT`` gaps
    then show to nobody until the next run records their requirements."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                        "'gap_requires'").fetchone() is not None


_OBJECT_VISIBLE = (
    "EXISTS (SELECT 1 FROM st.objects so JOIN st.object_versions sv ON sv.object_id = "
    "so.object_id AND sv.superseded_at IS NULL WHERE so.object_id = {o} "
    "AND so.audience_key IN (SELECT value FROM json_each(:aud)) "
    "AND sv.markings IN (SELECT value FROM json_each(:marks)))")


def _visible(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], params: Mapping[str, str],
             decide: Callable[[Sequence[str]], Mapping[str, Any]]
             ) -> list[tuple[sqlite3.Row, list[str]]]:
    """Each gap the reader may see, with the citations it may open."""
    ids = json.dumps([row["gap_id"] for row in rows])
    cites: dict[str, list[tuple[int, str]]] = defaultdict(list)
    objects: dict[str, set[Any]] = defaultdict(set)
    groups: dict[str, set[int]] = defaultdict(set)
    for cite in conn.execute(
            "SELECT gc.gap_id, gc.grp, gc.handle, gc.object_id, "
            "gc.object_id IS NOT NULL AND gc.markings IN "
            "(SELECT value FROM json_each(:marks)) AND "
            + _OBJECT_VISIBLE.format(o="gc.object_id")
            + " AS shown FROM gap_cites gc WHERE gc.gap_id IN (SELECT value FROM "
            "json_each(:ids)) ORDER BY gc.gap_id, gc.grp, gc.handle", {**params, "ids": ids}):
        if cite["grp"] >= 0:
            groups[cite["gap_id"]].add(cite["grp"])
        if cite["shown"]:
            cites[cite["gap_id"]].append((cite["grp"], cite["handle"]))
            objects[cite["handle"]].add(cite["object_id"])
    requires: dict[str, set[str]] = defaultdict(set)
    if _has_requires(conn):
        for req in conn.execute(
                "SELECT gap_id, audience_key FROM gap_requires WHERE gap_id IN (SELECT value "
                "FROM json_each(:ids))", {"ids": ids}):
            requires[req["gap_id"]].add(req["audience_key"])
    granted = set(json.loads(params["aud"]))
    handles = sorted({h for found in cites.values() for _, h in found})
    refused = {h for h, why in decide(handles).items() if why is not None} if handles else set()
    # a document the reader's markings withhold (a pay stub) is never cited, whatever the
    # row's own markings say (``operating.search.document_marks``)
    from .search import document_marks
    held = set(json.loads(params["marks"]))
    refused |= {h for h in handles if document_marks.handle_withheld(
        h, held, [o for o in objects.get(h, ()) if o])}
    out = []
    for row in rows:
        open_ = [(grp, h) for grp, h in cites.get(row["gap_id"], []) if h not in refused]
        need = groups.get(row["gap_id"], set())
        required = requires.get(row["gap_id"], set())
        if row["detector"] in REQUIRES_GRANT and not (required and required <= granted):
            continue   # a verdict on what a source does not hold: the run's grant only
        if need and need <= {grp for grp, _ in open_}:
            out.append((row, list(dict.fromkeys(h for _, h in open_))))
    return out


def _served_digest(conn: sqlite3.Connection, store: Any) -> tuple[str] | None:
    """The data an inbox's runs are compared with, read as the engine stamps a run
    (``_Store.meta``): a host reading Postgres (``ontology_reader.serves``) compares with
    Postgres's change stamp, else with the served generation's digest (``(digest,)``, None
    when it has none). Without it a mode-1 host (``PLX_ONTOLOGY_PG=1``) compared a Postgres
    run with the SQLite generation's digest and held every gap back as of older data.
    Postgres unreadable: the generation's digest, so a Postgres run's gaps stay held back."""
    from operating.pg import ontology_reader
    if ontology_reader.serves(store):
        stamp = ontology_reader.change_stamp()
        if stamp is not None:
            return (stamp,)
    row = conn.execute(
        "SELECT value FROM st.store_meta WHERE key = 'content_digest'").fetchone()
    return (row[0],) if row else None


def _attach(path: Path, store: Path, *, write: bool = False) -> sqlite3.Connection:
    mode = "rw" if write else "ro"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, isolation_level=None,
                           timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if not write:
            conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != INBOX_SCHEMA:
            raise ValueError("gap_inbox_schema_mismatch")
        from operating.pg import ontology_reader
        if ontology_reader.pg_only():
            _attach_postgres(conn)
        elif not Path(store).is_file():
            raise ostore.StoreError("store_missing")
        else:
            conn.execute("ATTACH DATABASE ? AS st", (f"file:{store}?mode=ro",))
        row = conn.execute("SELECT value FROM st.store_meta WHERE key = 'format'").fetchone()
        if row is None or row[0] != ostore.FORMAT:
            raise ostore.StoreError("store_format_mismatch")
    except BaseException:
        conn.close()
        raise
    return conn


def _attach_postgres(conn: sqlite3.Connection) -> None:
    """``st`` for an inbox when Postgres is the only store (``ontology_reader.pg_only``):
    the rows of the store the inbox's reads join, copied from Postgres into memory, so its
    permission SQL (``_OBJECT_VISIBLE``, ``_reader_view``) runs as written: ``store_meta``
    (format, content digest; ``audience_keys``: every audience key the ontology's objects
    hold now), and the objects the inbox names (gaps, citations, unjudged records) with
    their current versions' markings. Postgres not whole: ``StoreError``, never an inbox
    read over a partial store."""
    from operating.pg import ontology_reader
    problem = ontology_reader.whole()
    if problem is not None:
        raise ostore.StoreError("store_not_whole", problem)
    named: set[str] = set()
    for table in ("gaps", "gap_cites", "unjudged_causes"):
        try:
            named.update(r[0] for r in conn.execute(
                f"SELECT DISTINCT object_id FROM {table} WHERE object_id IS NOT NULL"))
        except sqlite3.OperationalError:   # an inbox written before the table
            continue
    import psycopg
    with psycopg.connect(ontology_reader.dsn(), connect_timeout=5) as pg:
        meta = dict(pg.execute("SELECT key, value FROM ontology.store_meta WHERE key IN "
                               "('format', 'content_digest')").fetchall())
        # the data Postgres holds now, changed by every delta layer and migration (a
        # generation's digest no longer changes): what the inbox's runs are compared with
        stamp = ontology_reader.change_stamp()
        if stamp is not None:
            meta["content_digest"] = stamp
            # said: the digest is the data version, so a reader's counts may be kept by it
            meta["changes_stamp"] = "1"
        # the distinct keys by their index, one probe per key
        keys = [r[0] for r in pg.execute(
            "WITH RECURSIVE k (key) AS (SELECT min(audience_key) FROM ontology.objects "
            "UNION ALL SELECT (SELECT min(audience_key) FROM ontology.objects "
            "WHERE audience_key > k.key) FROM k WHERE k.key IS NOT NULL) "
            "SELECT key FROM k WHERE key IS NOT NULL ORDER BY key")]
        ids = sorted(named)
        objects = pg.execute("SELECT object_id, audience_key FROM ontology.objects "
                             "WHERE object_id = ANY(%s)", (ids,)).fetchall()
        versions = pg.execute("SELECT object_id, superseded_at, markings FROM "
                              "ontology.object_versions WHERE object_id = ANY(%s) "
                              "AND superseded_at IS NULL", (ids,)).fetchall()
    query_only = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only = OFF")   # the copy in memory only; restored below
    conn.execute("ATTACH DATABASE ':memory:' AS st")
    conn.execute("CREATE TABLE st.store_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE st.objects (object_id TEXT PRIMARY KEY, audience_key TEXT)")
    conn.execute("CREATE TABLE st.object_versions (object_id TEXT, superseded_at TEXT, "
                 "markings TEXT)")
    conn.execute("CREATE INDEX st.object_versions_by_object ON object_versions (object_id)")
    conn.executemany("INSERT INTO st.store_meta VALUES (?, ?)",
                     [*meta.items(), ("audience_keys", json.dumps(keys))])
    conn.executemany("INSERT INTO st.objects VALUES (?, ?)", objects)
    conn.executemany("INSERT INTO st.object_versions VALUES (?, ?, ?)",
                     [(o, None if at is None else str(at), m) for o, at, m in versions])
    if query_only:
        conn.execute("PRAGMA query_only = ON")


# the skips a reader may not read the whole cause of: any record of the skip outside the
# reader's markings or audiences. Computed once per query (an uncorrelated set SQLite builds
# once), never re-evaluated per counted record: a skip of 10,000 records was 10^8 checks.
_HIDDEN_SKIPS = (
    "SELECT c.detector, c.skip FROM unjudged_causes c WHERE NOT (c.markings IN (SELECT "
    "value FROM json_each(:marks)) AND " + _OBJECT_VISIBLE.format(o="c.object_id") + ")")

# per (inbox, store data, reader's grant): the counts ``_rule_coverage`` reads; the inbox
# and the store change only on a run or a delta, while every page read asks again
_COVERAGE_KEPT = 64
_coverage_kept: OrderedDict[tuple[Any, ...],
                            tuple[dict[str, int], dict[str, dict[str, int]]]] = OrderedDict()
_coverage_lock = threading.Lock()


def _file_stamp(path: str) -> tuple[Any, ...] | None:
    """A SQLite file as it stands: identity, time, size and its first 32 bytes (a
    database's file change counter, which every commit not through a WAL moves; a WAL's
    checkpoint sequence and salts, which every restart of it moves). None: no file."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            head = fh.read(32)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size, head)


def _wal_stamp(path: str) -> tuple[Any, ...] | None:
    """``_file_stamp`` of a WAL file, None for an empty one as for none: an empty WAL holds
    no frame, so the database is its main file (whose stamp says when it changed); each
    reader that opened and reset it moved its time, and every page read built the table
    again (40 s of CPU three times over at once, 2026-11-13)."""
    stamp = _file_stamp(path)
    return None if stamp is None or stamp[3] == 0 else stamp


def _coverage_key(conn: sqlite3.Connection, params: Mapping[str, str]
                  ) -> tuple[Any, ...] | None:
    """What the reader's counts depend on, or None (never kept) when any part cannot be
    said: the inbox file (and its WAL) and its last write, the store's data (a SQLite
    file and its digest, or Postgres's change stamp), and the reader's audiences and
    markings exactly as the query reads them."""
    files = {row[1]: row[2] for row in conn.execute("PRAGMA database_list")}
    inbox, store = files.get("main") or "", files.get("st")
    if not inbox or store is None:
        return None   # an in-memory inbox: nothing says when it changed
    stamps = (_file_stamp(inbox), _wal_stamp(inbox + "-wal"))
    if stamps[0] is None:
        return None
    meta = dict(conn.execute("SELECT key, value FROM st.store_meta WHERE key IN "
                             "('content_digest', 'changes_stamp')").fetchall())
    if store:
        held = (store, _file_stamp(store), _wal_stamp(store + "-wal"),
                meta.get("content_digest"))
        if held[1] is None:
            return None
    else:
        # Postgres copied into memory: its stamp changes with every delta layer; without
        # one the store's change is unknown, so nothing is kept
        if not meta.get("changes_stamp") or not meta.get("content_digest"):
            return None
        held = ("postgres", meta["content_digest"])
    inner = tuple(conn.execute(
        "SELECT (SELECT max(rowid) FROM unjudged_causes), (SELECT count(*) FROM "
        "unjudged_causes), (SELECT max(run_at) FROM detector_runs), (SELECT "
        "group_concat(detector || '=' || unattributed || '@' || run_at) FROM "
        "rule_coverage)").fetchone())
    return (inbox, stamps, inner, held, params["aud"], params["marks"])


# per (inbox, store data): ``_unjudged_table``, the same for every reader
_TABLES_KEPT = 4
_tables_kept: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()


_tables_building: dict[tuple[Any, ...], threading.Lock] = {}


def _built_table(conn: sqlite3.Connection, tables: set[str],
                 key: tuple[Any, ...] | None) -> dict[str, Any]:
    """``_unjudged_table`` read from the inbox (and kept, under ``key``)."""
    pairs_of: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for object_id, audience, markings in conn.execute(
            "SELECT so.object_id, so.audience_key, sv.markings FROM st.objects so JOIN "
            "st.object_versions sv ON sv.object_id = so.object_id AND sv.superseded_at IS "
            "NULL WHERE so.object_id IN (SELECT DISTINCT object_id FROM unjudged_causes)"):
        pairs_of[object_id].add((audience, markings))
    # each record's versions as one of the few kinds there are (not in the store: none)
    places: dict[frozenset, int] = {frozenset(): 0}
    place_of = {o: places.setdefault(frozenset(p), len(places)) for o, p in pairs_of.items()}
    del pairs_of
    causes: dict[tuple[Any, int], int] = {}
    skip_causes: dict[tuple[str, str], set[int]] = defaultdict(set)
    subjects: list[tuple[str, str, str]] = []
    for name, skip, object_id, markings, subject in conn.execute(
            "SELECT detector, skip, object_id, markings, subject FROM unjudged_causes"):
        kind = (markings, place_of.get(object_id, 0))
        skip_causes[(name, skip)].add(causes.setdefault(kind, len(causes)))
        if subject == 1:
            subjects.append((name, skip, object_id))
    skips: dict[frozenset, int] = {}
    skip_kind = {pair: skips.setdefault(frozenset(held), len(skips))
                 for pair, held in skip_causes.items()}
    reasons: dict[tuple[str, str], list[str]] = defaultdict(list)
    if "unjudged_reasons" in tables:
        for name, skip, reason in conn.execute(
                "SELECT detector, skip, reason FROM unjudged_reasons"):
            if reason is not None:
                reasons[(name, skip)].append(reason)
    under: dict[tuple[str, Any, str], set[int]] = defaultdict(set)
    for name, skip, object_id in subjects:
        kind = skip_kind[(name, skip)]
        under[(name, None, object_id)].add(kind)
        for reason in reasons.get((name, skip), ()):
            under[(name, reason, object_id)].add(kind)
    counts: dict[tuple[str, Any, frozenset], int] = defaultdict(int)
    for (name, reason, _), kinds in under.items():
        counts[(name, reason, frozenset(kinds))] += 1
    place_pairs = {n: tuple(sorted(p)) for p, n in places.items()}
    table = {"causes": [(m, place_pairs[p]) for (m, p), _ in
                        sorted(causes.items(), key=lambda item: item[1])],
             "skips": [tuple(sorted(k)) for k, _ in sorted(skips.items(),
                                                          key=lambda item: item[1])],
             "counts": dict(counts)}
    if key is not None:
        with _coverage_lock:
            _tables_kept[key] = table
            while len(_tables_kept) > _TABLES_KEPT:
                _tables_kept.popitem(last=False)
        _table_to_disk(key, table)
    return table


# ``_unjudged_table`` kept on disk too, per key, so every process (the API, its warm-up and
# section workers, each Ask host) and a restart read it instead of each building it again
# (15 s for the owner's inbox under load, 2026-11-12). It holds markings, audience keys and
# counts only, never a record. ``PLX_RULE_COVERAGE_DIR`` (default
# ``~/.cache/plx/rule-coverage``); the newest ``_TABLES_ON_DISK`` files are kept.
_TABLE_FORMAT = "unjudged-table/1"
_TABLES_ON_DISK = 8


def _table_file(key: tuple[Any, ...]) -> Path:
    folder = Path(os.environ.get("PLX_RULE_COVERAGE_DIR")
                  or Path.home() / ".cache" / "plx" / "rule-coverage")
    digest = hashlib.sha256(f"{_TABLE_FORMAT}\x00{key!r}".encode()).hexdigest()
    return folder / f"table-{digest[:32]}.json"


def _table_from_disk(key: tuple[Any, ...]) -> dict[str, Any] | None:
    """The table kept for ``key`` (its file names the key's digest and holds the key's
    text again: a digest collision or a foreign file reads as none); anything unreadable
    or of another shape: none (built as before)."""
    try:
        doc = json.loads(_table_file(key).read_text(encoding="utf-8"))
        if doc.get("format") != _TABLE_FORMAT or doc.get("key") != repr(key):
            return None
        causes = [(m if m is None else str(m),
                   tuple((str(a), str(b)) for a, b in pairs)) for m, pairs in doc["causes"]]
        skips = [tuple(int(k) for k in kinds) for kinds in doc["skips"]]
        counts = {(str(name), None if reason is None else str(reason),
                   frozenset(int(k) for k in kinds)): int(n)
                  for name, reason, kinds, n in doc["counts"]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    if any(k >= len(skips) for _, _, kinds in counts for k in kinds) or \
            any(c >= len(causes) for kinds in skips for c in kinds):
        return None   # an index past its list: not this table
    return {"causes": causes, "skips": skips, "counts": counts}


def _table_to_disk(key: tuple[Any, ...], table: Mapping[str, Any]) -> None:
    """Keep ``table`` for ``key``, written whole or not at all (owner-only, a temporary
    file renamed over it); a directory that cannot be written keeps nothing."""
    path = _table_file(key)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = json.dumps({"format": _TABLE_FORMAT, "key": repr(key),
                           "causes": [[m, [list(p) for p in pairs]]
                                      for m, pairs in table["causes"]],
                           "skips": [list(kinds) for kinds in table["skips"]],
                           "counts": [[name, reason, sorted(kinds), n] for
                                      (name, reason, kinds), n in table["counts"].items()]})
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        kept = sorted(path.parent.glob("table-*.json"), key=lambda p: p.stat().st_mtime)
        for old in kept[:-_TABLES_ON_DISK]:
            old.unlink(missing_ok=True)
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink(missing_ok=True)   # a write that failed leaves nothing behind
        except OSError:
            pass


def _today() -> date:
    """The business day a read counts relative wording to (``read_inbox``)."""
    return po_status.business_today()


def _carried(row: Mapping[str, Any], carried_rules: set[str], older: set[str],
             failed_at: Mapping[str, Any], cut_short: set[str] = frozenset(),
             current: Mapping[str, datetime | None] | None = None,
             unchecked: set[tuple[str, str]] = frozenset()) -> dict[str, Any] | None:
    """Why a shown finding is not current (None: it is): its rule's last run failed
    (``check_failed``, with the failure time), its rule ran older code (``older_code``) or
    over older data (``older_data``), or its rule's last run was cut short before finding
    it again (``check_incomplete``: ``cut_short``, a finding not of the data ``current``
    names; or its subject was ``unchecked`` by the rule's last run); with the time it was
    last found."""
    name = row["detector"]
    unfinished = (name in cut_short or (name, row["object_id"]) in unchecked) \
        and current is not None and not _of_served(row, current)
    if name not in carried_rules and name not in failed_at and not unfinished:
        return None
    reason = ("check_failed" if name in failed_at else "older_code" if name in older
              else "older_data" if name in carried_rules else "check_incomplete")
    return {"reason": reason, "last_checked": row["last_seen"],
            **({"check_failed_at": failed_at[name]} if name in failed_at else {})}


CARRIED_WORDS = {"check_failed": "check failed at {failed}; last checked {checked}",
                 "check_incomplete": "the latest check did not finish; last checked {checked}",
                 "older_code": "not rechecked since the rules changed; last checked {checked}",
                 "older_data": "not rechecked on the newest data; last checked {checked}"}
# A count of days in a finding's words, and which way it moves as days pass: "in 3 days"
# counts down, "12 days past/late/overdue/ago/old" counts up; any other count is left.
_RELATIVE = re.compile(r"\b(?P<in>in )?(?P<n>\d+) (?P<unit>days?)\b(?P<after> (?:past|late|"
                       r"overdue|ago|old|behind|since|without)\b)?")


def _relative(summary: str, event: Any, days_late: Any, found_day: date | None,
              today: date) -> tuple[str, Any, Any]:
    """``summary``'s day counts (and ``event['days']``, ``days_late``) moved from the day
    the run found them (``found_day``) to ``today``: only a count equal to the finding's
    own days, so a stated term ("30 days") is never touched."""
    if found_day is None or today <= found_day or not isinstance(summary, str):
        return summary, event, days_late
    shift = (today - found_day).days
    known = {v for v in ((event or {}).get("days") if isinstance(event, dict) else None,
                         days_late) if isinstance(v, int)}
    moved: dict[str, int] = {}

    def move(match: re.Match) -> str:
        n = int(match["n"])
        if n not in known or not (match["in"] or match["after"]):
            return match[0]
        new = n - shift if match["in"] else n + shift
        moved["down" if match["in"] else "up"] = new
        if match["in"] and new <= 0:
            return "today" if new == 0 else "by now"
        return f"{match['in'] or ''}{new} day{'' if new == 1 else 's'}{match['after'] or ''}"
    text = _RELATIVE.sub(move, summary)
    if "up" in moved and isinstance(days_late, int):
        days_late += shift
    if isinstance(event, dict) and isinstance(event.get("days"), int) and moved:
        event = {**event, "days": event["days"] + (shift if "up" in moved else -shift)}
    return text, event, days_late


def _learned_order(shown: list[dict[str, Any]], store: Any, reader: str) -> None:
    """An alert kind this reader keeps dismissing (``operating.learning``: a lesson from
    their own not useful / wrong marks) is weighed down (``learned``: its weight and the
    lesson) and listed after the rest, in place; nothing is hidden by it."""
    from . import feedback_signals, learning
    state = feedback_signals.state_of(store)
    lessons = learning.Lessons.load(learning.ledger_dir(state))
    if not lessons or not shown:
        return
    who = learning.person_id(reader)
    roles = [r for r, members in learning._roles_of(state).items()
             if who in {learning.person_id(m) for m in members}]
    from .briefing import EVENT_TYPES
    weights = []
    for entry in shown:
        names = [entry.get("kind")] + [n for n, spec in EVENT_TYPES.items()
                                       if entry.get("kind") in (spec.get("kinds") or ())]
        weight, lesson = min((lessons.alert_weight(reader, str(n), roles) for n in names
                              if n), key=lambda x: x[0], default=(1.0, None))
        if lesson is not None:
            entry["learned"] = {"weight": weight, "lesson_id": lesson}
        weights.append(weight)
    order = sorted(range(len(shown)), key=lambda i: -weights[i])
    shown[:] = [shown[i] for i in order]


def _shown(row: Mapping[str, Any], citations: Sequence[str], state: str,
           follow_up: dict[str, Any], allowed: Iterable[str] = (),
           audiences: Iterable[str] = (), *, today: date | None = None,
           carried: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One inbox row as its reader sees it (``follow_up`` without the order field): its
    day counts as of ``today`` (``_relative``) and, when it is not current (``carried``),
    marked so in its words."""
    days_late = follow_up.pop("days_late", None)
    event = follow_up.pop("event", None)
    summary = row["summary"]
    if today is not None:
        seen = _moment(row["last_seen"])
        summary, event, days_late = _relative(
            summary, event, days_late,
            po_status.business_today(seen) if seen is not None else None, today)
    if carried is not None:
        words = CARRIED_WORDS[carried["reason"]].format(
            checked=_at_words(carried.get("last_checked")) or "an earlier run",
            failed=_at_words(carried.get("check_failed_at")) or "the last run")
        summary = pg._clip(summary, pg.MAX_TEXT - len(words) - 3) + " (" + words + ")"
    caveat = follow_up.pop("promise_caveat", None)
    if caveat is not None:
        unless = caveat.get("unless") if isinstance(caveat, dict) else None
        # a reader who may not read every audience a promise sits in (or a caveat this
        # engine cannot read) is told a later promise may exist; the audiences are never
        # shown
        if not isinstance(unless, list) or caveat.get("always") is not False \
                or not set(unless) <= set(audiences):
            summary = pg._clip(summary, pg.MAX_TEXT - len(PROMISE_CAVEAT_TEXT)) \
                + PROMISE_CAVEAT_TEXT
            event = {**(event if isinstance(event, dict) else {}), "caveat": PROMISE_CAVEAT}
    if isinstance(event, dict) and isinstance(event.get("amount"), dict):
        amount = event["amount"]
        handle = amount.get("handle")
        needed = {h for h in (handle, amount.get("currency_handle")) if h}
        if handle is not None and (not needed <= set(citations) or amount.get(
                "markings", UNREADABLE) not in set(allowed)):
            event.pop("amount")   # a figure this reader may not open is never shown
        else:
            scope = amount.pop("suspect_scope", None)
            plain = amount.pop("suspect_plain", None)
            if scope is not None and (scope.get("audience") not in set(audiences)
                                      or scope.get("markings") not in set(allowed)):
                amount["suspect"] = plain   # its peers' scope is not this reader's: no band
            lines = amount.get("lines")
            if isinstance(lines, dict):
                hidden = lines.get("verdict") != "unpriced" and (
                    lines.get("handles") is None
                    or not set(lines["handles"]) <= set(citations)
                    or lines.get("markings", UNREADABLE) not in set(allowed))
                # a line this reader may not open: nothing its lines say is stated, only
                # that the total was not checked against them
                amount["lines"] = {"verdict": "not_open"} if hidden else {
                    k: v for k, v in lines.items() if k in ("verdict", "lines_sum")}
    if isinstance(event, dict) and isinstance(event.get("chases"), dict):
        # our payment chases of an overdue invoice (``proactive_mail_pains.chase_fold``):
        # counted of only the messages this reader may open, never a handle shown
        mine = [m for m in event.pop("chases").get("messages") or ()
                if isinstance(m, dict) and m.get("handle") in set(citations)]
        if mine:
            last = max(m.get("sent") or "" for m in mine)
            summary = pg._clip(f"{summary}; chased {len(mine)} time"
                               f"{'' if len(mine) == 1 else 's'}, last on {last}")
            event["chases"] = {"count": len(mine), "last": last,
                               "messages": [{k: m.get(k) for k in ("id", "sent", "subject")}
                                            for m in mine]}
    shown_by = follow_up.pop("confirmation_cites", None)
    if isinstance(shown_by, list) and not set(shown_by) & set(citations):
        follow_up.pop("confirmation", None)   # a confirmation this reader may not open
    draft = follow_up.get("draft")
    if isinstance(draft, dict) and not set(draft.get("citations") or ()) <= set(citations):
        follow_up.pop("draft")   # a fact it states is not this reader's to open
    return {"id": row["gap_id"], "kind": row["kind"],
            "object_id": row["object_id"], "object_type": row["object_type"],
            "summary": summary, "citations": citations,
            "detected_by": "rule", "state": state,
            **({"carried": dict(carried)} if carried is not None else {}),
            "first_seen": row["first_seen"], "last_seen": row["last_seen"],
            **({"days_late": days_late} if days_late is not None else {}),
            **({"event": event} if isinstance(event, dict) and event else {}),
            "follow_up": follow_up}


def _at_words(stamp: Any) -> str | None:
    from .briefing import at_words
    return at_words(stamp) if isinstance(stamp, str) else None


UNJUDGED_SHOWN = 200   # records of one check's unjudged list read per page at most


class UnknownCheck(ValueError):
    """A check or reason no rule has (``unjudged_records``): refused, nothing read."""


def last_visit(inbox: Any, reader: str) -> str | None:
    """When this reader last marked its briefing seen (``record_visit``); None: never."""
    path = Path(inbox)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                        "'visits'").fetchone() is None:
            return None
        row = conn.execute("SELECT at FROM visits WHERE reader = ?", (reader,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def record_visit(inbox: Any, reader: str, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    """Mark this reader's briefing seen now (the Workbench's own inbox; nothing else is
    written). An inbox the engine never wrote is refused."""
    path = Path(inbox)
    if not path.is_file():
        raise FileNotFoundError("gap_inbox_missing")
    at = clock().isoformat()
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != INBOX_SCHEMA:
            raise ValueError("gap_inbox_schema_mismatch")
        conn.execute(_VISITS_DDL)
        conn.execute("INSERT INTO visits (reader, at, previous) VALUES (?, ?, NULL) ON CONFLICT "
                     "(reader) DO UPDATE SET previous = visits.at, at = excluded.at",
                     (reader, at))
    finally:
        conn.close()
    return at


# The markings the engine's own reader holds in its tool calls: an invoiced order's margin
# rests on finance-marked money (BC and Sage revenue and cost), so without it every margin
# is withheld and ``negative_margin`` never decides one. Its findings cite each figure at
# its value's markings, so a reader who does not hold finance never sees the loss
# (``read_inbox`` trims per reader).
ENGINE_MARKINGS = ("finance",)


def engine_marking_grants(actor: Any) -> dict[str, list[str]]:
    """``marking_grants`` of the engine's case sessions: ``ENGINE_MARKINGS`` held by the
    run principal (``actor``'s principals) only."""
    return {marking: sorted(actor.principals) for marking in ENGINE_MARKINGS}


# --- a whole pass, rule group by rule group (perm-data-1114, Fable audit 11-14 item 10) ---
# One run of every rule folded only at its end: a pass killed at its unit's timeout (the
# 4-hourly whole pass, 80 minutes, 11-13 10:35 PM ET) or by the host lost every rule it had
# judged, and the next pass began again from the first. Here a pass is a plan of rules run
# in groups, each group folded with the pass record (inbox meta ``pass:<kind>``: the plan,
# the generation and code it is over, the rules done) in one transaction; a pass that stops
# (its share spent, killed) is resumed by the next of its kind over the same generation and
# code within ``PASS_RESUME_HOURS``, from the first group not folded. Every rule is judged
# once either way, so a resumed pass leaves what one uninterrupted pass would.
PASS_KEY = "pass:"
PASS_RESUME_HOURS = 12.0
GROUP_SECONDS = 1200.0   # a group's planned length (its rules' windows over the threads)


def group_seconds(group: Sequence[str], threads: int = 1) -> float:
    return sum(DETECTOR_SECONDS.get(d, DETECTOR_SECONDS_DEFAULT)
               / (1 if d in ON_RUN_THREAD else max(1, threads)) for d in group)


def _write_pass(conn: sqlite3.Connection, key: str, record: Mapping[str, Any],
                ran: Iterable[str]) -> None:
    done = sorted(set(record.get("done") or ()) | set(ran))
    if set(record.get("plan") or ()) <= set(done):
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        return
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (key, json.dumps({**record, "done": done}, sort_keys=True)))


def pass_record(inbox: Any, kind: str) -> dict[str, Any] | None:
    path = Path(inbox)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None,
                           timeout=30)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (PASS_KEY + kind,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    try:
        held = json.loads(row[0]) if row is not None else None
    except ValueError:
        return None
    return held if isinstance(held, dict) else None


def _resumable(held: Mapping[str, Any] | None, plan: Sequence[str], generation: str | None,
               code: str | None, moment: datetime) -> bool:
    if not held or generation is None or held.get("generation") != generation:
        return False
    if held.get("code") != code or list(held.get("plan") or ()) != list(plan):
        return False
    started = _moment(held.get("started"))
    return started is not None and timedelta(0) <= moment - started \
        <= timedelta(hours=PASS_RESUME_HOURS)
