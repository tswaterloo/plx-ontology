"""Cited reads through the generic primitives, as one reader, for answers built of many records.

An answer that assembles many records (a meeting agenda, the sample requests of a team, the
ingredient lists of a formula family) reads them only through the host's tool dispatch
``call(name, arguments, actor)`` (``ontology_host.CaseHostTools.call`` or
``OntologyTools.call``): ``plx.find`` with an object type and filters, paged to a cap,
``plx.object`` per record and ``plx.around`` per hop. Every permission rule is the
primitives': what the reader may not read never reaches the answer, and the counts the
primitives report withheld are kept as ``withheld`` (a number, never a record).

Each value kept is the primitive's own value (an ``untrusted_text`` envelope stays one)
with its citation. What the primitives could not give becomes a typed gap, never an empty
answer:

* ``source_not_served`` (``not_ingested``): the schema declares the type but no source
  serves it (``plx.find`` error ``not_served``);
* ``binding_expected`` (``not_ingested``): the schema does not declare a property the
  answer expects (a later projection may add it; the code path stays ready);
* ``withheld_from_reader`` (``inaccessible``): the primitives withheld objects or
  properties; how many, never which;
* ``read_truncated`` (``unknown``): more records matched than the cap read;
* ``read_failed`` (``unknown``): any other tool error, with its code;
* ``budget_exhausted`` (``unknown``): the answer's time or record budget ran out first;
  what was not read is named (how many records), never silently dropped.

A budget (``budget_seconds`` on ``clock``, ``budget_records`` ``plx.object`` reads) is
checked before each primitive call: a call already running finishes, none starts after.
A record already read is not read again in one answer (the same properties).

``plx.find``'s own ``no_visible_match`` is not absence (the reader may not see a record
that exists), so an empty read is reported as the gap ``nothing_visible`` (``unknown``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

Call = Callable[[str, Mapping[str, Any], Any], Mapping[str, Any]]
PAGE = 50   # the contracts' max_page


def gap(code: str, state: str, subject: str, detail: str) -> dict[str, str]:
    return {"code": code, "state": state, "subject": subject[:200], "detail": detail[:600]}


def text_of(value: Any) -> str | None:
    """The plain text of a primitive value (an envelope's ``untrusted_text``), for
    comparison only: an answer keeps the envelope itself."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        value = value["untrusted_text"]
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def json_of(value: Any) -> Any:
    """A ``json`` value read back from its envelope; None when it does not parse."""
    raw = text_of(value)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


@dataclass
class Record:
    """One object as ``plx.object`` gave it to this reader."""

    object_id: str
    object_type: str
    values: dict[str, dict[str, Any]]
    withheld_properties: int = 0

    def get(self, *names: str) -> dict[str, Any] | None:
        """The first of ``names`` that carries a value: ``{"property", "value",
        "value_type", "citation"}``."""
        for name in names:
            cell = self.values.get(name)
            if cell is not None and cell.get("value") is not None:
                return cell
        return None

    def text(self, *names: str) -> str | None:
        cell = self.get(*names)
        return text_of(cell["value"]) if cell else None

    def handles(self) -> list[str]:
        return sorted({c["citation"]["l1_handle"] for c in self.values.values()
                       if (c.get("citation") or {}).get("l1_handle")})


@dataclass
class CitedReads:
    """Reads for one ``actor`` through ``call``; gaps and withheld counts accumulate."""

    call: Call
    actor: Any
    max_records: int = 200
    gaps: list[dict[str, str]] = field(default_factory=list)
    withheld: dict[str, int] = field(default_factory=lambda: {"objects": 0, "properties": 0})
    calls: int = 0
    budget_seconds: float | None = None
    budget_records: int | None = None
    clock: Callable[[], float] = time.monotonic
    records_read: int = 0
    stopped: str | None = None   # "time" or "records" once a budget ran out
    _started: float = field(default=0.0, init=False, repr=False)
    _cache: dict[tuple[str, tuple[str, ...]], Record | None] = field(
        default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._started = self.clock()

    def elapsed(self) -> float:
        return self.clock() - self._started

    def over(self) -> str | None:
        """Which budget has run out (``time`` or ``records``), else None. Once out, it
        stays out for the rest of the answer."""
        if self.stopped is None:
            if self.budget_seconds is not None and self.elapsed() >= self.budget_seconds:
                self.stopped = "time"
            elif self.budget_records is not None and self.records_read >= self.budget_records:
                self.stopped = "records"
        return self.stopped

    def budget_gap(self, subject: str, detail: str) -> None:
        what = (f"the time budget ({self.budget_seconds:g} s)" if self.stopped == "time"
                else f"the record budget ({self.budget_records} reads)")
        self.add(gap("budget_exhausted", "unknown", subject, f"{what} ran out: {detail}"))

    def budget(self) -> dict[str, Any]:
        """The budget and what was spent of it, for the answer."""
        return {"seconds": self.budget_seconds, "records": self.budget_records,
                "elapsed_seconds": round(self.elapsed(), 2), "records_read": self.records_read,
                "stopped": self.stopped}

    def _call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        out = self.call(name, dict(arguments), self.actor)
        return out if isinstance(out, Mapping) else {"error": {"code": "malformed_result"}}

    def add(self, item: Mapping[str, str]) -> None:
        if dict(item) not in self.gaps:
            self.gaps.append(dict(item))

    def _withheld(self, out: Mapping[str, Any]) -> None:
        held = out.get("withheld") or {}
        for key in ("objects", "properties"):
            count = held.get(key)
            if isinstance(count, int) and count > 0:
                self.withheld[key] += count

    def _error(self, subject: str, error: Mapping[str, Any]) -> None:
        code = str(error.get("code") or "error")
        message = str(error.get("message") or "")
        if code == "not_served":
            self.add(gap("source_not_served", "not_ingested", subject, message))
        elif code == "unknown_property":
            self.add(gap("binding_expected", "not_ingested", subject, message))
        else:
            self.add(gap("read_failed", "unknown", subject, f"{code}: {message}"))

    def find(self, object_type: str, filters: Sequence[Mapping[str, Any]] = (), *,
             subject: str | None = None, empty_gap: bool = True) -> list[str] | None:
        """Object ids of ``object_type`` matching ``filters`` that this reader may read, in
        the primitive's order, at most ``max_records``; None when the read failed (its gap
        is recorded). A budget that runs out between pages ends the read with the ids
        found so far and a ``budget_exhausted`` gap. ``empty_gap=False``: no
        ``nothing_visible`` gap for an empty read (a caller that widens and asks again)."""
        subject = subject or object_type
        args: dict[str, Any] = {"object_type": object_type, "limit": PAGE}
        if filters:
            args["filters"] = [dict(f) for f in filters]
        ids: list[str] = []
        while True:
            if self.over():
                self.budget_gap(subject, f"{len(ids)} matching record(s) found before it; "
                                "the rest of the search was not run")
                return ids
            out = self._call("plx.find", args)
            if "error" in out:
                self._error(subject, out["error"])
                return None
            self._withheld(out)
            for candidate in out.get("candidates") or ():
                oid = (candidate.get("object") or {}).get("object_id")
                if isinstance(oid, str) and oid not in ids:
                    ids.append(oid)
            cursor = (out.get("completeness") or {}).get("next_cursor")
            if len(ids) >= self.max_records:
                if cursor or len(ids) > self.max_records:
                    self.add(gap("read_truncated", "unknown", subject,
                                 f"more than {self.max_records} records matched; the rest "
                                 "were not read"))
                return ids[:self.max_records]
            if not cursor:
                break
            args = {**args, "cursor": cursor}
        if not ids and empty_gap:
            self.nothing_visible(subject)
        return ids

    def nothing_visible(self, subject: str) -> None:
        self.add(gap("nothing_visible", "unknown", subject,
                     "no record this reader can read matched; absence from the source "
                     "is not established"))

    def read(self, object_id: str, properties: Sequence[str] | None = None) -> Record | None:
        """``plx.object`` of one record (only ``properties`` when given), read once per
        answer for the same properties."""
        key = (object_id, tuple(sorted(set(properties))) if properties else ())
        if key in self._cache:
            return self._cache[key]
        args: dict[str, Any] = {"object_id": object_id}
        if properties:
            args["properties"] = list(key[1])
        self.records_read += 1
        out = self._call("plx.object", args)
        if "error" in out:
            self._error(object_id.split(":", 1)[0], out["error"])
            self._cache[key] = None
            return None
        self._withheld(out)
        held = (out.get("withheld") or {}).get("properties")
        values = {v["property"]: dict(v) for v in out.get("values") or ()
                  if isinstance(v, Mapping) and "property" in v}
        obj = out.get("object") or {}
        record = Record(obj.get("object_id", object_id), obj.get("object_type", ""), values,
                        held if isinstance(held, int) else 0)
        self._cache[key] = record
        return record

    def around(self, object_id: str, link_type: str, direction: str, *,
               filters: Sequence[Mapping[str, Any]] = (), subject: str | None = None,
               not_visible: str = "read_failed", empty_gap: bool = False
               ) -> list[str] | None:
        """Object ids one hop along ``link_type`` from ``object_id`` (those passing
        ``filters``), paged to ``max_records``; None on a failed read. A start object
        this reader cannot see (``object_not_found``) is ``not_visible``: a
        ``read_failed`` gap, or with ``"nothing_visible"`` that gap (one answer for not
        captured and not readable). ``empty_gap``: an empty read is ``nothing_visible``."""
        subject = subject or link_type
        args: dict[str, Any] = {"from": object_id, "limit": PAGE, "path": [
            {"link_type": link_type, "direction": direction}]}
        if filters:
            args["filters"] = [dict(f) for f in filters]
        ids: list[str] = []
        while True:
            if self.over():
                self.budget_gap(subject, f"{len(ids)} linked record(s) found before it; the "
                                "rest were not looked up")
                return ids
            out = self._call("plx.around", args)
            if "error" in out:
                if not_visible == "nothing_visible" and not ids and (
                        out["error"] or {}).get("code") == "object_not_found":
                    self.nothing_visible(subject)
                    return []
                self._error(subject, out["error"])
                return None
            self._withheld(out)
            for item in out.get("objects") or ():
                oid = item.get("object_id") if isinstance(item, Mapping) else None
                if isinstance(oid, str) and oid != object_id and oid not in ids:
                    ids.append(oid)
            cursor = (out.get("completeness") or {}).get("next_cursor")
            if len(ids) >= self.max_records:
                if cursor or len(ids) > self.max_records:
                    self.add(gap("read_truncated", "unknown", subject,
                                 f"more than {self.max_records} records are linked; the rest "
                                 "were not read"))
                return ids[:self.max_records]
            if not cursor:
                break
            args = {**args, "cursor": cursor}
        if not ids and empty_gap:
            self.nothing_visible(subject)
        return ids

    def read_all(self, ids: Iterable[str], properties: Sequence[str] | None = None, *,
                 subject: str | None = None) -> list[Record]:
        """Each record of ``ids`` this reader may read. A budget that runs out stops the
        reads; how many of the ids were not read is a ``budget_exhausted`` gap."""
        ids = list(ids or ())
        out: list[Record] = []
        for n, oid in enumerate(ids):
            cached = (oid, tuple(sorted(set(properties))) if properties else ()) in self._cache
            if not cached and self.over():
                self.budget_gap(subject or oid.split(":", 1)[0],
                                f"{len(ids) - n} of {len(ids)} records were not read")
                break
            record = self.read(oid, properties)
            if record is not None:
                out.append(record)
        return out

    def withheld_gap(self, subject: str) -> None:
        """One ``withheld_from_reader`` gap for what the primitives withheld so far."""
        if self.withheld["objects"] or self.withheld["properties"]:
            self.add(gap("withheld_from_reader", "inaccessible", subject,
                         f"{self.withheld['objects']} objects and "
                         f"{self.withheld['properties']} properties were withheld from this "
                         "reader; they are not absent"))


__all__ = ["Call", "CitedReads", "Record", "gap", "json_of", "text_of"]
