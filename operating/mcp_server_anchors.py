"""The case host's question anchors and the links walked between records."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import call_limits


def _letters_digits(text: str) -> str:
    """``text`` as letters and digits alone, upper case (the relevance check's form)."""
    return re.sub(r"[^0-9A-Za-z]", "", text).upper()


def _tokens(text: str) -> tuple[str, ...]:
    """``text`` as its words and codes: each run of letters and digits, upper case.
    "PO-1002/1" is (PO, 1002, 1); "PO10020" one token (GATE-002)."""
    return tuple(word.upper() for word in re.findall(r"[0-9A-Za-z]+", text))


# A cell of at least this many words is prose: a name in it is a mention, read the way
# page text is read (``_names``).
PROSE_WORDS = 6


def _names(anchor: str, tokens: Sequence[str], *, cell: bool) -> bool:
    """Whether ``tokens`` (one cell of a row, or page text: ``cell`` False) name
    ``anchor`` (``_anchor_keys``), on whole words only, never inside a longer code or
    name (GATE-002: PO1002 never names PO-10020; ACME never names ACMEFOODS):
    - a number alone (the number of a code the question writes) only as the whole cell:
      a code's number inside another code, or prose, may be another kind's (SO-1002);
    - a code with digits as a run of whole words anywhere (PO-1002, "PO 1002", PO1002);
    - a name (letters only) as the whole cell, or as a run of whole words in prose (page
      text, or a cell of ``PROSE_WORDS`` words or more), so a short party name never
      names a longer one's cell ("Acme Foods" never "Acme Foods International")."""
    if not anchor or not tokens:
        return False
    whole = "".join(tokens)
    if anchor not in whole:
        return False
    if cell and whole == anchor:
        return True
    if anchor.isdigit():
        return False
    if not any(ch.isdigit() for ch in anchor) and cell and len(tokens) < PROSE_WORDS:
        return False
    for start in range(len(tokens)):
        if not anchor.startswith(tokens[start]):
            continue
        joined = ""
        for token in tokens[start:]:
            joined += token
            if joined == anchor:
                return True
            if len(joined) >= len(anchor) or not anchor.startswith(joined):
                break
    return False


def _said_values(view: Any) -> list[tuple[tuple[str, ...], bool]] | None:
    """What a cited record says, as the relevance check reads it: each value of its row
    (never a column name) and its page text, as words (``_tokens``), each with whether
    it is a cell of the row (True) or page text (False). A record that moved on since it
    was read (``changed_since``) is read as the revision cited (``revision``), the row
    its claim was read from. None: nothing to read (unreadable, or no row and no
    text)."""
    if isinstance(view, Mapping) and view.get("changed_since") \
            and isinstance(view.get("revision"), Mapping):
        view = view["revision"]
    if not isinstance(view, Mapping):
        return None
    out: list[tuple[tuple[str, ...], bool]] = []

    def walk(value: Any, cell: bool) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                walk(item, cell)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, cell)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            said = _tokens(str(value))
            if said:
                out.append((said, cell))
    row, text = view.get("row"), view.get("text")
    if not isinstance(row, Mapping) and not isinstance(text, str):
        return None
    walk(row if isinstance(row, Mapping) else {}, True)
    walk(text if isinstance(text, str) else "", False)
    return out


# A name or code shorter than this names too much to tie a record to the question.
MIN_ANCHOR_CHARS = 4


# The question's things are resolved once, at the session's start (``_told_question``),
# within START_ANCHOR_SECONDS; the answer gate never waits on a search: a session told no
# question, or whose start-up resolution did not finish, resolves at its submission within
# SUBMIT_ANCHOR_SECONDS (a warm second try), and what is still unresolved then is not on
# the question (fail closed). Measured 2026-11-06: one plx.find of a question's names took
# 75 s of a 78 s submission under load 40, and sessions were killed waiting on it.
START_ANCHOR_SECONDS = 45.0


SUBMIT_ANCHOR_SECONDS = 10.0


# The link walk of a submission's relevance check (``_linked_to``) is bounded too: one not
# finished in time connects nothing.
LINK_SECONDS = 10.0


# The session's own finds kept for the relevance check (``_kept_find``).
MAX_KEPT_FINDS = 50


def _named_entities(entities: Any) -> list[Mapping[str, Any]]:
    """Of ``entities`` (``objective_runner.resolve_entities``' shape), those the resolver
    matched or derived as one thing."""
    return [entity for entity in entities or () if isinstance(entity, Mapping)
            and entity.get("status") in ("match", "derived")]


def _entity_records(entities: Iterable[Mapping[str, Any]]) -> set[str]:
    """The object ids of the records ``entities`` holds as the same thing."""
    out: set[str] = set()
    for entity in entities:
        for record in entity.get("records") or ():
            if not isinstance(record, Mapping) or record.get("status") not in (
                    "match", "derived"):
                continue
            # a find result's record holds its object; a resolved one its id
            held = record.get("object")
            oid = held.get("object_id") if isinstance(held, Mapping) \
                else record.get("object_id")
            if isinstance(oid, str):
                out.add(oid)
    return out


def _question_anchors(server: "MCPServer", asked: str,
                      records: set[str] | None = None, *,
                      entities: Sequence[Mapping[str, Any]] | None = None,
                      limit: float | None = None,
                      whole: list[bool] | None = None) -> frozenset[str]:
    """The names and codes, as letters and digits, the question ``asked`` names records
    by: each identifier it writes (the one grammar, ``find_identifiers``) and each entity
    its reader's resolver matches or reads out of a longer reference; a code's number
    alone too. ``records`` gets the object ids of those entities' records (``_linked_to``).
    ``entities``: the question's things as already resolved for this reader (the app's
    front door, ``objective_runner.resolve_entities``), so no search runs; None: resolved
    here by one ``plx.find`` (``resolve_entities``) within ``limit`` seconds (None: no
    limit). ``whole`` gets False when that resolution did not finish (timed out or
    failed): the anchors are then only what is known, never more."""
    from ontology.identifiers import find_identifiers
    from .objective_runner import resolve_entities
    if server.ontology is None:
        return frozenset()
    mentions = [mention.text for mention in find_identifiers(asked)]
    if entities is None:
        try:
            gated = server.case_runtime.employee_gate(server.actor)
            host = server._ontology_host()
            with call_limits.bounded(limit), host.request():
                entities = resolve_entities(asked, lambda arguments: host.call(
                    "plx.find", arguments, gated))
            if entities is None:   # the lookup failed: only what is known, never more
                entities = []
                if whole is not None:
                    whole.append(False)
        except call_limits.ToolTimedOut:
            entities = []   # not resolved in time: the question's identifiers still stand
            if whole is not None:
                whole.append(False)
        except Exception:  # noqa: BLE001 - the question's own identifiers still stand
            entities = []
            if whole is not None:
                whole.append(False)
    named = _named_entities(entities)
    mentions += [entity.get("mention") for entity in named]
    if records is not None:
        records.update(_entity_records(named))
    return _anchor_keys(mentions)


def _anchor_keys(mentions: Iterable[Any]) -> frozenset[str]:
    """``mentions`` as relevance anchors: each as letters and digits when long enough,
    and a code's number alone."""
    anchors = set()
    for mention in mentions:
        key = _letters_digits(str(mention or ""))
        if len(key) >= MIN_ANCHOR_CHARS:
            anchors.add(key)
        core = re.fullmatch(r"[A-Z]{1,4}(\d{4,})", key)
        if core:
            anchors.add(core.group(1))
    return frozenset(anchors)


# A name an agent's own find connects (``_found_on_question``): long enough to be a name
# the question writes, never a word of it ("R&D + Marketing" is 11). A shorter name
# (``MIN_ANCHOR_CHARS`` up) connects only from a find of one object type whose noun the
# question writes beside it: "teams channel Gleamin" (dev 11-10: G-0018, the entity index
# read "Gleamin" as the customer and the supplier, and the gate refused the channel's own
# date as off the question).
MIN_NAME_ANCHOR_CHARS = 8


def _type_noun(object_type: str) -> str | None:
    """The noun of an object type's name as a question writes it: its last word
    ("TeamsChannel" -> "channel", "MCTask" -> "task"), or None for a name with none."""
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", object_type or "")
    return words[-1].casefold() if words and words[-1].isalpha() and len(words[-1]) > 2 \
        else None


def _names_by_type(asked: str, name: str, object_type: Any) -> bool:
    """Whether the question writes ``name`` whole (as its own words, never inside a
    longer word) and, within a few words before it, the noun of ``object_type``
    ("teams channel Gleamin", "the Gleamin channel" too)."""
    noun = _type_noun(object_type) if isinstance(object_type, str) else None
    words = re.findall(r"[0-9A-Za-z]+", name)
    if noun is None or not words:
        return False
    named = r"\b" + r"\W+".join(re.escape(word) for word in words) + r"\b"
    near = r"(?:\W+\w+){0,2}?\W+"
    pattern = rf"\b{re.escape(noun)}s?{near}{named}|{named}{near}{re.escape(noun)}s?\b"
    return re.search(pattern, asked, re.IGNORECASE) is not None


def _display_text(candidate: Mapping[str, Any]) -> str:
    """A find candidate's display text (its record's name or title as recorded)."""
    obj = candidate.get("object")
    display = obj.get("display") if isinstance(obj, Mapping) else None
    text = display.get("untrusted_text") if isinstance(display, Mapping) else None
    return text if isinstance(text, str) else ""


def _found_on_question(asked: str, finds: Iterable[tuple[Mapping[str, Any], Any]]
                       ) -> tuple[list[Any], set[str]]:
    """What this session's own finds (``finds``: arguments, result) resolved of the
    question ``asked``: (mentions, record ids). Only a find the question itself decides:
    a ``mode: entities`` find of text the question writes, whose things the resolver
    matched or derived; or a find of an identifier the question writes
    (``find_identifiers``), found as exactly one record; or a find of a name the question
    writes whole (at least ``MIN_NAME_ANCHOR_CHARS`` letters and digits, or a shorter one a
    find of one object type reads where the question names that type, ``_names_by_type``)
    that exactly one candidate is named (its display text, as letters and digits, is that name),
    or, of several so named, exactly one of a type the question writes beside the name: the
    things the entity index holds no names of (a Teams chat's topic, a task's title).
    A find of any other words adds nothing, so an agent's own search never widens what
    the question names."""
    from ontology.identifiers import find_identifiers
    words = _tokens(asked)
    said = lambda text: _names(text, words, cell=False)  # noqa: E731 - on whole words
    codes = {_letters_digits(mention.text) for mention in find_identifiers(asked)}
    mentions: list[Any] = []
    ids: set[str] = set()
    for arguments, result in finds:
        if not isinstance(arguments, Mapping) or not isinstance(result, Mapping):
            continue
        if arguments.get("mode") == "entities":
            text = _letters_digits(str(arguments.get("text") or arguments.get("query") or ""))
            if len(text) < MIN_ANCHOR_CHARS or not said(text):
                continue
            named = [entity for entity in _named_entities(result.get("entities"))
                     if said(_letters_digits(str(entity.get("mention") or "")))]
            mentions += [entity.get("mention") for entity in named]
            ids |= _entity_records(named)
            continue
        query = _letters_digits(str(arguments.get("query") or ""))
        candidates = [c for c in result.get("candidates") or () if isinstance(c, Mapping)]
        if query in codes:
            if result.get("status") != "found" or len(candidates) != 1:
                continue
            picked = candidates[0]
        else:
            # A name the question writes whole (a chat's topic, a task's title: things the
            # entity index does not hold), found as exactly one record of that very name.
            if len(query) < MIN_ANCHOR_CHARS or not said(query) \
                    or result.get("status") not in ("found", "ambiguous"):
                continue
            if len(query) < MIN_NAME_ANCHOR_CHARS and not _names_by_type(
                    asked, str(arguments.get("query") or ""), arguments.get("object_type")):
                continue
            named = [c for c in candidates if _letters_digits(_display_text(c)) == query]
            if len(named) > 1:
                # Records of several types share the name (a meeting, its invitations and
                # its chat): the one whose type the question writes beside the name ("the
                # Teams chat Gleamin meeting prep") is the one it names.
                named = [c for c in named if isinstance(c.get("object"), Mapping)
                         and _names_by_type(asked, str(arguments.get("query") or ""),
                                            c["object"].get("object_type"))]
            if len(named) != 1:
                continue
            picked = named[0]
        oid = (picked.get("object") or {}).get("object_id") \
            if isinstance(picked.get("object"), Mapping) else None
        if isinstance(oid, str):
            mentions.append(arguments.get("query"))
            ids.add(oid)
    return mentions, ids


# A record a question's record links to is on the question too: a line, an entry, a
# component, the order that made it (2026-11-06: the answer gate refused the assembly
# orders that filled an item asked about, whose rows name the item by its id alone).
# Connected: within LINK_HOPS links of a question's record, over links records hold
# (never a mention read in text, a lead or a contradicted link), a record linked to more
# than HUB_LINKS others (a supplier, a customer) not walked through.
LINK_HOPS = 4


HUB_LINKS = 150


NEAR_CAP = 20000


_FIRM = ("superseded_at IS NULL AND binding_kind <> 'mention' "
         "AND tier NOT IN ('lead', 'contradicted')")


def _neighbours(conn: Any, ids: Iterable[str]) -> dict[str, set[str]]:
    """Each of ``ids`` -> the records its current firm links reach, either direction."""
    ids = sorted(set(ids))
    out: dict[str, set[str]] = {oid: set() for oid in ids}
    for start in range(0, len(ids), 500):
        part = json.dumps(ids[start:start + 500])
        for near, far in conn.execute(
                "SELECT from_object, to_object FROM link_rows WHERE from_object IN "
                f"(SELECT value FROM json_each(:ids)) AND {_FIRM} "
                "UNION ALL SELECT to_object, from_object FROM link_rows WHERE to_object IN "
                f"(SELECT value FROM json_each(:ids)) AND {_FIRM}", {"ids": part}):
            out[near].add(far)
    return out


def _hubs(conn: Any, ids: Iterable[str]) -> set[str]:
    """Those of ``ids`` holding more than ``HUB_LINKS`` current firm links."""
    out: set[str] = set()
    ids = sorted(set(ids))
    for start in range(0, len(ids), 500):
        part = json.dumps(ids[start:start + 500])
        out |= {oid for oid, count in conn.execute(
            "SELECT oid, count(*) FROM (SELECT from_object AS oid FROM link_rows WHERE "
            f"from_object IN (SELECT value FROM json_each(:ids)) AND {_FIRM} UNION ALL "
            "SELECT to_object FROM link_rows WHERE to_object IN "
            f"(SELECT value FROM json_each(:ids)) AND {_FIRM}) GROUP BY oid", {"ids": part})
            if count > HUB_LINKS}
    return out


def _link_connection(server: "MCPServer") -> Any:
    """A read-only connection of its own to the file the session's ontology store reads
    (one per file and process), so the relevance walk never holds the store's lock; None
    without a store file (an in-memory store is read under its lock instead)."""
    tools = getattr(getattr(server, "ontology", None), "tools", None)
    store = getattr(tools, "store", None)
    if store is None:
        return None
    try:
        with store.lock:
            path = next((row[2] for row in store.conn.execute("PRAGMA database_list")
                         if row[1] == "main"), "")
    except Exception:  # noqa: BLE001 - no file to open
        return None
    if not path:
        return _Locked(store)
    try:
        file = _file_key(path)
    except OSError:
        return None   # the file is gone: undecided, never a stale file's links
    with _LINK_CONNS_LOCK:
        held = _LINK_CONNS.get(path)
        if held is not None and held[0] == file:
            return held[1]
        # A new generation swapped in at this path (another file), or a path not opened
        # yet: every kept connection whose path no longer names its file is closed (a
        # walk still reading one fails and is undecided), then the file is opened.
        for kept, (key, conn) in list(_LINK_CONNS.items()):
            try:
                current = _file_key(kept)
            except OSError:
                current = None
            if current != key or kept == path:
                _LINK_CONNS.pop(kept)
                conn.close()
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                               check_same_thread=False)
        _LINK_CONNS[path] = (file, conn)
        return conn


def _file_key(path: str) -> tuple[int, int]:
    """The file a path names now: (device, inode)."""
    found = os.stat(path)
    return found.st_dev, found.st_ino


# The relevance walk's read-only connection per store file path, with the file (device,
# inode) it was opened on (``_link_connection``): a generation swapped in at the same path
# is another file, read on a new connection, and the old file's is closed.
_LINK_CONNS: dict[str, tuple[tuple[int, int], Any]] = {}


_LINK_CONNS_LOCK = threading.Lock()


class _Locked:
    """An in-memory store's connection, used under its lock (no file to open again)."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def execute(self, sql: str, params: Any = ()) -> list[Any]:
        with self.store.lock:
            return list(self.store.conn.execute(sql, params))


def _reader_visible(server: "MCPServer") -> Callable[[Iterable[str]], set[str]] | None:
    """Of some object ids, those this session's reader may read now (the store's own
    gate: audience, markings, time and item ACL), under the policy decided for them."""
    host = getattr(server, "ontology", None)
    if host is None or not hasattr(host, "tools"):
        return None
    try:
        gated = server.case_runtime.employee_gate(server.actor)
    except Exception:  # noqa: BLE001 - an undecided reader reads nothing
        return None

    def visible(ids: Iterable[str]) -> set[str]:
        ids = sorted(set(ids))
        if not ids:
            return set()
        with host.request(), host.per_call():
            tools = host.tools
            tools.policy = host.policy_now(gated)
            with tools.store.lock:
                scope = tools._scope(gated, tools._as_known_at({}))
                return set(tools._visible_versions(scope, ids))
    return visible
