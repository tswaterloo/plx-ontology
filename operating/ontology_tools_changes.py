"""``plx.changes``: what changed since a time, as the asking reader may see it.

"What changed since yesterday" cannot be read off a source's own modified stamp alone: the
Portal's ETL bumps every ``app_*.updated_at`` on each run whether or not a field changed
(the mirror recorded 21,200 manufacturing
orders restamped a day with no other column changed, 2026-10-29..31). The change signal
here is the projection's own version history: each captured version of a record is an
``object_versions`` row (a new one only when the captured content changed), and two
versions are compared on the values the ontology reads from them.

For each record of the asked object types (default ``DEFAULT_TYPES``, the order types) the
reader may read now, the values the reader may read of the version in force at ``since`` are
compared with those of the version in force at ``as_known_at`` (now when absent):

* ``changed``: a property's values differ (its before and after values, each cited). The
  systems' own write stamps (``STAMPS``) never count as a change by themselves.
* ``first_captured``: new: no version was in force at ``since`` and the record says it was
  created at or after it (``CREATED``: the Portal's ``created_at``, naive UTC as Prisma
  writes it, or FileMaker's creation time; else a BC document date on or after that day).
  A record first read since then that states an earlier creation, or none, is counted in
  ``counts.first_captured_earlier``, never listed (the ETL bumps every Portal updated_at).
* ``removed``: the source stopped holding it (missing from the source, or its own soft
  delete) since then.

A record with newer versions but no property changed is ``restamped`` (counted, never
listed): only its write stamp, or a column the ontology does not read, moved. A record that
changed only in values whose markings the reader lacks is neither listed nor counted, not
even as restamped (``withheld`` says the answer may hide such changes, never how many).
A record its item ACL denies the reader (mail, SharePoint, Teams) is no entry and no count:
the answer equals one from a store that never held it. The page ends where the result
budget does (``completeness.next_cursor``, bound to the store generation).

Permissions: records are read under the caller's read policy before anything is compared,
ranked or counted (``StoreReader.objects_of_type``: audience, version markings, as known at
the answer's time); values are filtered by marking per version; the item gate re-checks
each listed record's cited handle. The answer never holds a record or value the reader
cannot read through ``plx.object``.

Times: ``captured_at`` is when the capture first held the new version (the change happened
after the previous capture of the old one and at or before it); a write stamp of the record
is quoted beside it as the source states it. A source whose last capture is older than
``since`` cannot show what changed since: the answer names it (``not_captured_since``).
"""

from __future__ import annotations

import datetime as dt
import atexit
import hashlib
import json
import threading
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from ontology.json_contract import validate

from .ontology_tools import (_AUDIENCE_SOURCE, _ITEM_ACL_AUDIENCES, Actor, OntologyTools,
                             ToolError, _envelope_bytes, _error,
                             _format_time, _gap, _parse_time, _value_out)

TOOL_NAME = "plx.changes"
TOOL_NAMES = (TOOL_NAME,)
DEFAULT_TYPES = ("PurchaseOrder", "SalesOrder", "AssemblyOrder", "SalesInvoice",
                 "PurchaseInvoice")
# The systems' own write stamps (ontology/schema/domains: one property per system, so
# twins never conflict on them) and how far each proves a write of the record.
STAMPS = {"bc_last_modified_at": "source_write_time",
          "fm_modified_at": "source_write_time",
          "portal_updated_at": "bumped_by_etl"}
BUDGET_RESERVE = 2000   # bytes kept free of max_result_bytes for the host's labels
DEFAULT_LIMIT = 10   # a page of real entries fits the result budget (2 KB each)
BATCH = 2000          # records or versions read per query
PRIME_DAYS = 7        # the value signatures read ahead: versions captured this recently
MAX_FIELDS = 12       # changed properties listed per record (the rest counted)
_CURSOR = "chg1"


def _ms(text: Any) -> dt.datetime | None:
    return _parse_time(text) if isinstance(text, str) else None


class _Version:
    __slots__ = ("handle", "start", "end", "markings", "recorded")

    def __init__(self, handle: str, recorded: str, superseded: str | None,
                 markings: str) -> None:
        self.handle, self.recorded, self.markings = handle, recorded, markings
        self.start = _ms(recorded) or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        self.end = None if superseded is None else (
            _ms(superseded) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))


def _current(chain: Sequence[_Version], at: dt.datetime) -> _Version | None:
    """The version current at ``at`` (recorded at or before, superseded after); a time
    that does not parse is never current."""
    for version in chain:
        if version.start <= at and (version.end is None or version.end > at):
            return version
    return None


class _Index:
    """Reader-independent facts of one store generation: per object type its records
    (id, audience), each record's versions in time order, the records with a removal, and
    per (record, version handle) the signature of its values: property -> the sorted
    (value, unit, currency, markings) it holds. A version's values never change within a
    generation, so a signature is read once and kept."""

    def __init__(self, stamp: Any) -> None:
        self.stamp = stamp
        self.types: dict[str, list[tuple[str, str]]] = {}
        self.versions: dict[str, list[_Version]] = {}
        self.removed: set[str] = set()
        self.sig: dict[tuple[str, str], dict[str, tuple]] = {}
        # the background prime and a call fill it alike: one at a time
        self.lock = threading.RLock()

    def of_type(self, conn: Any, object_type: str) -> list[tuple[str, str]]:
        with self.lock:
            return self._of_type(conn, object_type)

    def _of_type(self, conn: Any, object_type: str) -> list[tuple[str, str]]:
        if object_type not in self.types:
            held = [(r[0], r[1]) for r in conn.execute(
                "SELECT object_id, audience_key FROM objects WHERE object_type = ? "
                "ORDER BY object_id", (object_type,))]
            ids = [oid for oid, _a in held]
            for start in range(0, len(ids), BATCH):
                chunk = json.dumps(ids[start:start + BATCH])
                for oid, handle, recorded, superseded, markings in conn.execute(
                        "SELECT object_id, l1_handle, recorded_at, superseded_at, markings "
                        "FROM object_versions WHERE object_id IN (SELECT value FROM "
                        "json_each(?)) ORDER BY object_id, recorded_at", (chunk,)):
                    self.versions.setdefault(oid, []).append(
                        _Version(handle, recorded, superseded, markings))
                self.removed.update(r[0] for r in conn.execute(
                    "SELECT DISTINCT object_id FROM object_removals WHERE object_id IN "
                    "(SELECT value FROM json_each(?))", (chunk,)))
            self.types[object_type] = [(oid, a) for oid, a in held if oid in self.versions]
        return self.types[object_type]

    def signatures(self, conn: Any, wanted: Iterable[tuple[str, str]]
                   ) -> dict[tuple[str, str], dict[str, tuple]]:
        with self.lock:
            return self._signatures(conn, wanted)

    def _signatures(self, conn: Any, wanted: Iterable[tuple[str, str]]
                    ) -> dict[tuple[str, str], dict[str, tuple]]:
        missing = sorted({key for key in wanted if key not in self.sig})
        for start in range(0, len(missing), BATCH):
            chunk = missing[start:start + BATCH]
            got: dict[tuple[str, str], dict[str, list]] = {key: {} for key in chunk}
            for oid, handle, prop, value, unit, currency, markings in conn.execute(
                    "SELECT object_id, l1_handle, property, value, unit, currency, markings "
                    "FROM object_values WHERE object_id IN (SELECT value FROM json_each(?)) "
                    "AND l1_handle IN (SELECT value FROM json_each(?)) AND value IS NOT NULL",
                    (json.dumps(sorted({o for o, _h in chunk})),
                     json.dumps(sorted({h for _o, h in chunk})))):
                if (oid, handle) in got:
                    got[(oid, handle)].setdefault(prop, []).append(
                        (value, unit, currency, frozenset(_marks(markings))))
            for key, props in got.items():
                self.sig[key] = {p: tuple(sorted(v, key=repr)) for p, v in props.items()}
        return self.sig


def _marks(text: Any) -> tuple[str, ...]:
    try:
        return tuple(json.loads(text)) if isinstance(text, str) else ()
    except ValueError:
        return ("unreadable",)   # never held: the value is never read


CREATED = ("portal_created_at", "fm_created_at")   # the systems' own creation times
CREATED_DATES = ("order_date", "invoice_date")     # a BC document's own date (no time)


def _created_since(values: Mapping[str, tuple], since: dt.datetime) -> bool:
    """Whether the record says it was created at or after ``since``: a creation time
    (Portal ``created_at``: naive UTC as Prisma writes it), else a document date on or
    after the day of ``since``. A record that states none is not called new."""
    times = [t for name in CREATED for v in values.get(name, ()) if (t := _ms(v[0]))]
    if times:
        return max(times) >= since
    days = [v[0][:10] for name in CREATED_DATES for v in values.get(name, ())
            if isinstance(v[0], str)]
    return bool(days) and max(days) >= since.date().isoformat()


def _readable(signature: Mapping[str, tuple], held: frozenset[str]) -> dict[str, tuple]:
    """A version's signature as this reader may read it: the values whose markings it
    holds (a property it holds none of is absent, never compared)."""
    out = {}
    for prop, values in signature.items():
        kept = tuple(v[:3] for v in values if v[3] <= held)
        if kept:
            out[prop] = kept
    return out


def _cursor(offset: int, key: str) -> str:
    return f"{_CURSOR}:{offset}:{key}"


class ChangeTools:
    """``plx.changes`` over one host's ``OntologyTools`` (store, policy, item gate, clock,
    schema, contracts): the same read rules every generic tool applies."""

    def __init__(self, base: OntologyTools) -> None:
        self.base = base
        self.contracts = base.contracts
        self._lock = threading.Lock()
        self._index: _Index | None = None

    # ------------------------------------------------------------------ entry

    def call(self, arguments: Any, actor: Actor) -> dict[str, Any]:
        contract = self.contracts["tools"][TOOL_NAME]
        if not isinstance(arguments, dict):
            return self._checked(_error("invalid_input", "arguments must be an object"))
        problems = validate(arguments, contract["input"], root=self.contracts, limit=3)
        if problems:
            return self._checked(_error("invalid_input", "; ".join(problems)))
        try:
            with self.base.store.lock:
                result = self._changes(arguments, actor)
        except ToolError as exc:
            result = _error(exc.code, exc.message, exc.hint)
        except Exception as exc:  # noqa: BLE001 - sqlite3.Error and the like
            if type(exc).__module__ != "sqlite3":
                raise
            result = _error("store_unavailable", f"the projection store could not be read "
                            f"({type(exc).__name__})", "rebuild or reopen the projection store")
        return self._checked(result)

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        problems = validate(result, self.contracts["tools"][TOOL_NAME]["output"],
                            root=self.contracts, limit=5)
        if problems:
            return _error("contract_violation",
                          ("output failed the contract: " + "; ".join(problems))[:600])
        if _envelope_bytes(result) > self.contracts["limits"]["max_result_bytes"]:
            return _error("result_too_large", "the answer exceeds the result budget",
                          "ask for fewer object types or a smaller limit")
        return result

    # ------------------------------------------------------------------ the answer

    def _changes(self, arguments: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        base = self.base
        at = base._as_known_at(arguments)
        since = _parse_time(arguments["since"])
        if since is None:
            raise ToolError("invalid_input", "since is not a valid date or timestamp")
        if since >= at:
            raise ToolError("invalid_input", "since must be before as_known_at (now)")
        types = list(dict.fromkeys(arguments.get("object_types") or DEFAULT_TYPES))
        declared = set(base.schema.object_types) if base.schema is not None else None
        unknown = [t for t in types if declared is not None and t not in declared]
        if unknown:
            raise ToolError("unknown_object_type", f"not declared: {', '.join(unknown)}",
                            "plx.schema lists the object types")
        limit = int(arguments.get("limit", DEFAULT_LIMIT))
        generation = base._store_meta().get("content_digest", "")
        key = hashlib.sha256(json.dumps([_format_time(since), _format_time(at), types,
                                         generation]).encode()).hexdigest()[:12]
        offset = 0
        if arguments.get("cursor") is not None:
            parts = str(arguments["cursor"]).split(":")
            if len(parts) != 3 or parts[0] != _CURSOR or parts[2] != key \
                    or not parts[1].isdigit():
                raise ToolError("invalid_cursor", "the cursor is not one this question gave",
                                "ask again without a cursor")
            offset = int(parts[1])
        scope = base._scope(actor, at)
        store_meta = base._store_meta()
        found: list[dict[str, Any]] = []
        counts = {"changed": 0, "first_captured": 0, "removed": 0, "restamped": 0,
                  "first_captured_earlier": 0}
        by_type: dict[str, dict[str, int]] = {}
        sources: set[str] = set()
        index = self.index()
        for object_type in types:
            tally = by_type.setdefault(object_type, {"changed": 0, "first_captured": 0,
                                                     "removed": 0, "restamped": 0})
            for entry in self._classify(index, scope, actor, object_type, since, at,
                                        sources):
                kind = entry["kind"]
                counts[kind] += 1
                if kind in tally:
                    tally[kind] += 1
                if kind in ("changed", "first_captured", "removed"):
                    entry["object_type"] = object_type
                    found.append(entry)
        found.sort(key=lambda e: (e["when"], e["object_id"]), reverse=True)
        page = found[offset:offset + limit]
        self._page_rows(scope, page)
        displays = base._displays(scope, [e["object_id"] for e in page])
        lineages = self._lineages(page)
        changes = [self._entry(e, displays, lineages) for e in page]
        gaps = self._capture_gaps(store_meta, since, sources)
        meta = base._meta(store_meta, at, sorted(_SOURCES.get(s, s) for s in sources))
        budget = self.contracts["limits"]["max_result_bytes"] - BUDGET_RESERVE

        def answer(shown: int) -> dict[str, Any]:
            end = offset + shown
            more = end < len(found)
            return {
                "since": _format_time(since),
                "object_types": types,
                "changes": changes[:shown],
                "counts": counts,
                "by_type": by_type,
                "basis": ("captured versions compared on the values this reader may read; "
                          "the systems' write stamps (" + ", ".join(sorted(STAMPS)) + ") never "
                          "count as a change by themselves; portal_updated_at is bumped by the "
                          "Portal ETL; new means created at or after since"),
                "gaps": gaps,
                # What the reader may not read is never compared or counted: whether such
                # a record or value changed is not said (a count would tell).
                "withheld": {"properties": None, "objects": None,
                             "reason": "marking_and_audience"},
                "meta": meta,
                "completeness": {"complete": not more, "truncated": more,
                                 **({"reason": "page_cap"} if more else {}),
                                 "returned": shown, "total": len(found),
                                 "next_cursor": _cursor(end, key) if more else None},
            }

        shown = len(changes)
        result = answer(shown)
        while shown > 1 and _envelope_bytes(result) > budget:
            shown -= 1        # the page ends where the result budget does; the rest pages on
            result = answer(shown)
        return result

    # ------------------------------------------------------------------ per record

    def index(self) -> "_Index":
        """The reader-independent facts of this store generation (records, versions,
        removals; per-version value signatures filled as they are first needed), read
        once while the store is unchanged (``PRAGMA data_version``, as
        ``StoreReader.audience_keys``)."""
        conn = self.base.store.conn
        stamp = (conn.execute("PRAGMA data_version").fetchone()[0], conn.total_changes)
        with self._lock:
            if self._index is None or self._index.stamp != stamp:
                self._index = _Index(stamp)
            return self._index

    def prime_later(self) -> threading.Thread | None:
        """Start ``prime`` in the background on a connection opened here, on the caller's
        thread, to the file the shared connection reads now (so a generation renamed over
        the path later is never read into this index); the shared connection is never used
        off its callers' threads."""
        import sqlite3
        conn = self.base.store.conn
        try:
            path = next(row[2] for row in conn.execute("PRAGMA database_list")
                        if row[1] == "main")
            index = self.index()
            if not path:
                return None
            own = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        except (sqlite3.Error, StopIteration):
            return None
        thread = threading.Thread(target=self.prime, args=(own, index),
                                  name="plx-changes-prime", daemon=True)
        thread.start()
        return thread

    def prime(self, conn: Any, index: "_Index", types: Sequence[str] = DEFAULT_TYPES,
              days: float = PRIME_DAYS) -> None:
        """Read the facts of ``types`` into ``index`` and the value signatures of every
        version captured in the last ``days`` (and the one each replaced) ahead of the first
        question, on ``conn`` (its own; closed here); an exit stops it between batches."""
        import sqlite3
        try:
            for object_type in types:
                if _STOP.is_set():
                    return
                wanted = []
                for oid, _audience in index.of_type(conn, object_type):
                    chain = index.versions[oid]
                    cut = chain[-1].start - dt.timedelta(days=days)
                    for i, version in enumerate(chain):
                        if i and version.start >= cut:
                            wanted += [(oid, version.handle), (oid, chain[i - 1].handle)]
                for start in range(0, len(wanted), BATCH):
                    if _STOP.is_set():
                        return
                    index.signatures(conn, wanted[start:start + BATCH])
        except sqlite3.Error:
            return
        finally:
            conn.close()

    def _classify(self, index: "_Index", scope: Any, actor: Actor, object_type: str,
                  since: dt.datetime, at: dt.datetime, sources: set[str]
                  ) -> Iterable[dict[str, Any]]:
        """The entry of each record of ``object_type`` this reader may read that a version
        or removal since ``since`` touches (``kind`` changed, first_captured, removed,
        restamped or first_captured_earlier). A record the reader may not read (audience,
        markings, or its item ACL through the L1 handle of each version compared) is never
        an entry: every count is over readable records only, as if the store did not hold
        the others."""
        conn = self.base.store.conn
        audiences = set(scope.audience_keys)
        marks = set(scope.marking_sets)
        pairs: dict[str, tuple[Any, Any]] = {}
        visible: list[str] = []
        for oid, audience in index.of_type(conn, object_type):
            if audience not in audiences:
                continue
            chain = index.versions[oid]
            after = _current(chain, at)
            # StoreReader._VISIBLE_OBJECT: the version in force and the latest one carry
            # only markings this reader holds
            if after is None or after.markings not in marks or chain[-1].markings not in marks:
                continue
            before = _current(chain, since)
            if audience.split(":", 1)[0] in _ITEM_ACL_AUDIENCES and not all(
                    self.base._item_allowed(actor, v.handle) for v in (after, before) if v):
                continue
            visible.append(oid)
            sources.add(audience.split(":", 1)[0])
            if before is not None and before.handle == after.handle:
                continue   # no version captured since: nothing observed to change
            pairs[oid] = (before, after)
        removed = [oid for oid in visible if oid in index.removed]
        if removed:
            from ontology.store import removals_at
            now = removals_at(conn, removed, _format_time(at))
            then = removals_at(conn, removed, _format_time(since))
            for oid in removed:
                if oid in now and oid not in then:
                    basis, when = now[oid]
                    pairs.pop(oid, None)
                    yield {"kind": "removed", "object_id": oid,
                           "handle": _current(index.versions[oid], at).handle,
                           "when": when or _format_time(at), "basis": basis}
        signatures = index.signatures(conn, [(oid, v.handle) for oid, pair in pairs.items()
                                             for v in pair if v is not None])
        held = scope.held
        for oid, (before, after) in pairs.items():
            full_new = signatures.get((oid, after.handle), {})
            new = _readable(full_new, held)
            if before is None:
                if not _created_since(new, since):
                    yield {"kind": "first_captured_earlier", "object_id": oid}
                    continue
                first = index.versions[oid][0]
                yield {"kind": "first_captured", "object_id": oid, "handle": after.handle,
                       "when": first.recorded, "captured_at": first.recorded,
                       "previous": None, "fields": [], "pair": (None, after.handle)}
                continue
            full_old = signatures.get((oid, before.handle), {})
            old = _readable(full_old, held)
            fields = [name for name in sorted((set(old) | set(new)) - set(STAMPS))
                      if old.get(name, ()) != new.get(name, ())]
            if not fields:
                if any(full_old.get(n, ()) != full_new.get(n, ())
                       for n in (set(full_old) | set(full_new)) - set(STAMPS)):
                    continue   # changed only where this reader may not read: not said at all
                yield {"kind": "restamped", "object_id": oid}
                continue
            first = next(v for v in index.versions[oid] if v.start > since)
            yield {"kind": "changed", "object_id": oid, "handle": after.handle,
                   "when": first.recorded, "captured_at": first.recorded,
                   "previous": before.recorded, "fields": fields,
                   "pair": (before.handle, after.handle)}

    def _page_rows(self, scope: Any, entries: Sequence[dict[str, Any]]) -> None:
        """The cited values a listed entry shows: its changed properties before and after
        and its write stamps, read for the page only, under the reader's markings."""
        wanted = {(e["object_id"], h) for e in entries if "pair" in e for h in e["pair"] if h}
        rows: dict[tuple[str, str], dict[str, list[Any]]] = {}
        if wanted:
            for row in self.base.store.conn.execute(
                    "SELECT * FROM object_values v WHERE v.object_id IN "
                    "(SELECT value FROM json_each(:ids)) AND v.l1_handle IN "
                    "(SELECT value FROM json_each(:handles)) AND v.markings IN "
                    "(SELECT value FROM json_each(:marks))",
                    scope.params(ids=json.dumps(sorted({o for o, _h in wanted})),
                                 handles=json.dumps(sorted({h for _o, h in wanted})))):
                rows.setdefault((row["object_id"], row["l1_handle"]), {}).setdefault(
                    row["property"], []).append(row)
        for e in entries:
            if "pair" not in e:
                continue
            old = rows.get((e["object_id"], e["pair"][0]), {}) if e["pair"][0] else {}
            new = rows.get((e["object_id"], e["pair"][1]), {})
            e["stamps"] = {name: new[name] for name in STAMPS if name in new}
            e["fields"] = [(name, old.get(name, []), new.get(name, [])) for name in e["fields"]]

    def _lineages(self, entries: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        out: dict[str, str] = {}
        for entry in entries:
            row = self.base.store.conn.execute(
                "SELECT lineage FROM object_versions WHERE object_id = ? AND l1_handle = ?",
                (entry["object_id"], entry["handle"])).fetchone()
            if row is not None:
                out[entry["object_id"]] = row["lineage"]
        return out

    def _entry(self, e: Mapping[str, Any], displays: Mapping[str, Any],
               lineages: Mapping[str, str]) -> dict[str, Any]:
        limit = self.contracts["limits"]["max_untrusted_text_chars"]
        out: dict[str, Any] = {
            "object": {"object_id": e["object_id"], "object_type": e["object_type"],
                       "display": displays.get(e["object_id"]),
                       "lineage": lineages.get(e["object_id"], "gold_native_staging")},
            "kind": e["kind"]}
        if e["kind"] == "removed":
            out["removal"] = {"basis": e["basis"], "since": e["when"]}
            return out
        out["captured_at"] = e["captured_at"]
        out["previous_captured_at"] = e["previous"]
        stamps = []
        for name in sorted(e["stamps"]):
            for row in e["stamps"][name]:
                value, _gap_ = _value_out(row, e["object_type"], limit)
                if value is not None:
                    stamps.append({"stamp": STAMPS[name], "value": value})
        out["source_stamps"] = stamps
        fields = []
        for name, old, new in e["fields"][:MAX_FIELDS]:
            fields.append({"property": name,
                           "before": [v for r in old
                                      if (v := _value_out(r, e["object_type"], limit)[0])],
                           "after": [v for r in new
                                     if (v := _value_out(r, e["object_type"], limit)[0])]})
        out["fields"] = fields
        if len(e["fields"]) > MAX_FIELDS:
            out["more_fields"] = len(e["fields"]) - MAX_FIELDS
        return out

    def _capture_gaps(self, store_meta: Mapping[str, str], since: dt.datetime,
                      sources: Iterable[str]) -> list[dict[str, Any]]:
        """A source family of the read records whose last capture is older than
        ``since`` (or unknown) cannot show what changed since: named, never 'none'."""
        try:
            freshness = json.loads(store_meta.get("freshness") or "[]")
        except ValueError:
            freshness = []
        captured = {f.get("source"): f.get("captured_at") for f in freshness
                    if isinstance(f, Mapping)}
        gaps = []
        for audience in sorted(set(sources)):
            source = _SOURCES.get(audience, audience)
            moment = _ms(captured.get(source))
            if moment is None or moment < since:
                gaps.append(_gap("unknown", "not_captured_since", source,
                                 f"the projection's last capture of {source} is "
                                 f"{captured.get(source) or 'unknown'}, before the asked time: "
                                 "changes after it are not seen"))
        return gaps


# audience key prefix -> the source family the store's freshness names
_SOURCES = _AUDIENCE_SOURCE


def _contract() -> Mapping[str, Any]:
    from .ontology_tools import load_contracts
    return load_contracts()["tools"][TOOL_NAME]


_CONTRACT = _contract()
TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": _CONTRACT["description"],
    "inputSchema": _CONTRACT["input"],
    "annotations": {"readOnlyHint": True},
}


_STOP = threading.Event()   # set at interpreter exit: a prime stops between batches
_PRIMES: list[threading.Thread] = []


@atexit.register
def _stop_primes() -> None:
    _STOP.set()
    for thread in _PRIMES:
        thread.join(timeout=10)


def register(table: MutableMapping[str, dict[str, Any]], base: OntologyTools, *,
             prime: bool = False) -> ChangeTools:
    """Serve ``plx.changes`` in a host tool table (``ontology_host.tool_table``) over the
    host's ``OntologyTools``: it reads under the policy the host decided for the call.
    ``prime``: read the store's facts and recent value signatures in the background now
    (``ChangeTools.prime``), so the first question does not pay for them."""
    tool = ChangeTools(base)
    if prime and not _STOP.is_set():
        thread = tool.prime_later()
        if thread is not None:
            _PRIMES[:] = [t for t in _PRIMES if t.is_alive()] + [thread]
    if TOOL_NAME in table:
        raise ValueError(f"tool already registered: {TOOL_NAME}")
    table[TOOL_NAME] = dict(TOOL, handler=tool.call)
    return tool


__all__ = ["ChangeTools", "DEFAULT_TYPES", "STAMPS", "TOOL", "TOOL_NAME", "TOOL_NAMES",
           "register"]
