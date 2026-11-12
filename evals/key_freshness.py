"""Detect answer keys whose source records changed since the key was made (stale keys).

A key stores ``computed_at`` and the handles of the records it was read from. Two checks,
both read-only on the published projection store:

* ``touched``: a key record has no current version, a newer version, a newer link
  (``recorded_at`` after the key's ``computed_at``) or a source absence opened or closed
  after it (``object_removals``).
* ``replay`` (graph-walk keys): the walk named by the key (links, directions, the word
  filter) is run again on the current links, and the answer is compared with the key's
  (an empty "none" set that now holds records, a count that moved).

A finding names the item id and a reason code only: never a question, never a value from
a held-out key, so the report is safe to read next to the held-out store.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from evals import graph_walks as gw

FIRM = ("verified", "reviewed", "corroborated")
MAX_FOUND = 60
_JOIN_THE = re.compile(r"\b(?:of|for|linked to|on) the\b")
_WORD = re.compile(r'mentions "([^"]+)"')
_HOP = re.compile(r"^(\S+) \((in|out)\)$")
COUNT_ASK = re.compile(r"^(?:how many|count the|what's the number of)", re.I)


def open_store(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = 1")
    return conn


def _day(stamp: str) -> str:
    return str(stamp or "")[:19]


def touched(conn: sqlite3.Connection, key: Mapping[str, Any]) -> list[str]:
    """Reason codes for key records changed after the key was computed."""
    made = _day(key.get("computed_at", ""))
    reasons: list[str] = []
    handles = [h for f in key.get("facts", ()) for h in f.get("records") or ()]
    for handle in sorted(set(handles)):
        rows = conn.execute("SELECT object_id, recorded_at, superseded_at IS NULL "
                            "FROM object_versions WHERE l1_handle = ?", (handle,)).fetchall()
        if not any(current for _o, _t, current in rows):
            reasons.append("record_not_current")
            continue
        for oid, at, current in rows:
            if current and _day(at) > made:
                reasons.append("record_version_newer")
            # a source absence opened or closed after the key was made: the record's
            # version is unchanged, but the source no longer (or again) holds it
            for since, until in conn.execute(
                    "SELECT since, until FROM object_removals WHERE object_id = ?", (oid,)):
                if _day(since) > made:
                    reasons.append("record_removed_since")
                if until is not None and _day(until) > made:
                    reasons.append("record_returned_since")
            if conn.execute("SELECT 1 FROM link_rows WHERE (from_object = ? OR to_object = ?) "
                            "AND recorded_at > ? AND tier IN (?, ?, ?) LIMIT 1",
                            (oid, oid, key["computed_at"], *FIRM)).fetchone():
                reasons.append("link_newer")
    return sorted(set(reasons))


def hops_of(key: Mapping[str, Any]) -> list[tuple[str, str]]:
    out = []
    for read in key.get("query", {}).get("reads", ()):
        found = _HOP.match(read)
        if found:
            out.append((found.group(1), found.group(2)))
    return out


def filter_position(question: str, hops: int) -> int:
    """Which set (1..hops) the word filter was put on, read from where the phrase sits in
    the question: the outermost noun comes first, each deeper noun follows an 'of the'."""
    found = _WORD.search(question)
    if not found:
        return hops
    before = question[:found.start()]
    return max(1, min(hops, hops - len(_JOIN_THE.findall(before))))


def _step(conn, ids: Iterable[str], link_type: str, direction: str) -> set[str]:
    near, far = ("from_object", "to_object") if direction == "out" else ("to_object", "from_object")
    out: set[str] = set()
    for oid in ids:
        out |= {r[0] for r in conn.execute(
            f"SELECT {far} FROM link_rows WHERE {near} = ? AND link_type = ? "
            "AND superseded_at IS NULL AND tier IN (?, ?, ?)", (oid, link_type, *FIRM))}
    return {o for o in out if not gw.mirror_type(gw.object_type(o))}


def _rows(conn, oid: str) -> list[dict[str, Any]]:
    return [{"property": p, "value": v, "value_type": t} for p, v, t in conn.execute(
        "SELECT property, value, value_type FROM object_values WHERE object_id = ? "
        "AND superseded_at IS NULL", (oid,))]


def replay(conn: sqlite3.Connection, item: Mapping[str, Any], key: Mapping[str, Any]
           ) -> dict[str, Any] | None:
    """The key's walk run again: ``{"final": [object ids], "position": p}``; None when the
    walk cannot be replayed (not a word-filtered graph walk, or the start is not unique)."""
    if key.get("family") != "graph_walk":
        return None
    hops = hops_of(key)
    word = _WORD.search(str(item.get("question", {}).get("text", "")))
    if not hops or "filter word on" not in " ".join(key["query"]["reads"]) or not word:
        return None
    # The walk's own root, kept in the query (a count key's facts hold the answer records,
    # not the root); an older absent key names its root as its one fact record.
    start_id = key["query"].get("start")
    if isinstance(start_id, str) and start_id:
        start = [r[0] for r in conn.execute("SELECT object_id FROM object_versions WHERE "
                                            "object_id = ? AND superseded_at IS NULL "
                                            "LIMIT 1", (start_id,))]
    else:
        facts = key.get("facts") or [{}]
        handle = (facts[0].get("records") or [None])[0] if facts[0].get("kind") == "absent" \
            else None
        if handle is None:
            return None
        start = [r[0] for r in conn.execute("SELECT object_id FROM object_versions WHERE "
                                            "l1_handle = ? AND superseded_at IS NULL",
                                            (handle,))]
    if len(start) != 1:
        return None
    position = filter_position(item["question"]["text"], len(hops))
    # "mentions W" is read as the question reads: W anywhere in the name, case folded. The
    # generator's own filter demands a whole token, so a word cut from inside "image001.png"
    # matched nothing there (see DECISIONS/open-eval-keys-word-filter.md).
    needle = word.group(1).casefold()
    sets = [[start[0]]]
    for index, (link_type, direction) in enumerate(hops, start=1):
        nxt = sorted(_step(conn, sets[-1], link_type, direction))
        if index == position:
            kept = []
            for oid in nxt:
                label = gw.label_of(_rows(conn, oid))
                if label and needle in label[0].casefold():
                    kept.append(oid)
            nxt = kept
        sets.append(nxt)
    return {"final": sets[-1], "position": position, "sizes": [len(s) for s in sets],
            "start": start[0]}


def verdict(conn: sqlite3.Connection, item: Mapping[str, Any] | None,
            key: Mapping[str, Any]) -> list[str]:
    """Reason codes why ``key`` no longer matches the records (empty: still fresh)."""
    reasons = touched(conn, key)
    if item is not None:
        done = replay(conn, item, key)
        if done is not None:
            fact = key["facts"][0]
            if fact["kind"] == "absent" and done["final"]:
                reasons.append("none_now_has_records")
            elif fact["kind"] == "number" and fact.get("id") == "count" \
                    and str(len(done["final"])) != str(fact.get("value")):
                reasons.append("count_changed")
    return sorted(set(reasons))


def revised_key(conn: sqlite3.Connection, item: Mapping[str, Any], key: Mapping[str, Any],
                reason: str, now: datetime | None = None) -> dict[str, Any]:
    """The key recomputed from the current links, with a ``revisions`` entry (the old
    answer kind and the reason). Raises ValueError when the walk cannot be replayed."""
    done = replay(conn, item, key)
    if done is None:
        raise ValueError("walk_not_replayable")
    final = done["final"]
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = key["facts"][0]
    new = {k: v for k, v in key.items() if k != "revisions"}
    new["revisions"] = list(key.get("revisions", ())) + [{
        "at": when, "reason": reason, "was": {"id": old["id"], "kind": old["kind"]},
        "now_count": len(final)}]
    new["computed_at"] = when
    new["query"] = {**key["query"], "start": done["start"]}
    if not final:
        if old["kind"] == "absent":
            return new
        # a count that fell to zero is the walk's "none" answer, rooted at the start record
        root = conn.execute("SELECT l1_handle FROM object_versions WHERE object_id = ? AND "
                            "superseded_at IS NULL", (done["start"],)).fetchone()
        system = gw.fact_system(root[0]) if root else None
        if not system:
            raise ValueError("walk_not_replayable")
        noun = (old.get("label") or ["records"])[0]
        new["facts"] = [{"id": "none", "kind": "absent", "value": f"no {noun}",
                         "accept": [f"no {noun}", *gw.NONE_FORMS], "records": [root[0]],
                         "allowed": [system]}]
        new["card"] = new["card"].removesuffix("count") + "none"
        new["strata"] = {**new["strata"], "answer": "none"}
        new["query"] = {**new["query"], "rule": "the walk's last set is empty"}
        return new
    handles, marks = [], set()
    for oid in final[:MAX_FOUND]:
        for handle, markings in conn.execute("SELECT l1_handle, markings FROM object_versions "
                                             "WHERE object_id = ? AND superseded_at IS NULL", (oid,)):
            handles.append(handle)
            marks |= set(gw.markings([{"markings": markings}]))
    allowed = sorted({s for h in handles if (s := gw.fact_system(h))})
    nouns = [w for w in old.get("accept", [])[:1]]
    label = [nouns[0].removeprefix("no ")] if nouns else ["records"]
    fact = {"id": "count", "kind": "number", "value": str(len(final)), "label": label,
            "records": handles, "allowed": allowed}
    if marks:
        fact["markings"] = sorted(marks)
    new["facts"] = [fact]
    new["card"] = new["card"].removesuffix("none") + "count"
    new["strata"] = {**new["strata"], "answer": "count"}
    new["query"] = {**new["query"], "rule": "count of distinct records of the last set"}
    return new
