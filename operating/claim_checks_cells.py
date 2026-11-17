"""Cited records and cells for the claim checks: stored values and their time zones, money
cells, moments and days, folding for comparison, and record and collection lookups."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
import json
import re
import unicodedata
from typing import Any, Mapping, Sequence

from .claim_checks_grammar import _COLUMN_FIELD, _ISO_MOMENT, _STATUS_COLUMN
from .claim_checks_parse import dig


@lru_cache(maxsize=1)
def _stamp_zones() -> Mapping[tuple[str, str, str], Mapping[str, str] | None]:
    """(source, mirror relation, column) -> the time-zone declaration (``naive_zone`` or
    ``wall_clock_zone``) of every ``iso_timestamp`` source the ontology schema declares
    one for (C-09); None for a column two sources declare differently. Empty when the
    schema does not load."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: cells are read as written
        return {}
    out: dict[tuple[str, str, str], Mapping[str, str] | None] = {}
    for spec in schema.object_types.values():
        for prop in spec.get("properties") or ():
            for source in prop.get("sources") or ():
                zone = {k: source[k] for k in ("naive_zone", "wall_clock_zone") if k in source}
                _type, binding = schema.bindings.get(source.get("binding"), (None, {}))
                path = source.get("path")
                if not zone or not binding.get("relation") or not isinstance(path, str):
                    continue
                key = (binding.get("source"), binding["relation"], path)
                if key not in out:
                    out[key] = zone
                elif out[key] != zone:
                    out[key] = None
    return out


def stamp_zone(handle: str, resolved: Mapping[str, Any], field: str
               ) -> Mapping[str, str] | None:
    """The zone the schema declares for a cited Portal mirror timestamp column, or None."""
    match = _COLUMN_FIELD.match(field)
    if match is None or not field.startswith("row.") or not handle.startswith("api://") \
            or not isinstance(resolved.get("relation"), str):
        return None
    return _stamp_zones().get(("portal_api", resolved["relation"], match.group(1)))


def _as_stored(text: str, stamp: Mapping[str, str] | None) -> str:
    """A cited timestamp cell as the projection stores it under its source's declared zone
    (``ontology.project._timestamp``: a Prisma zoneless UTC stamp, FileMaker's wall clock
    labelled UTC); the cell as written when nothing is declared or it is no timestamp."""
    if not stamp or ("naive_zone" in stamp and _moment(text) is not None
                     and (_moment(text)[1] is None or _moment(text)[1].tzinfo is not None)):
        return text   # a date, or a cell with its own offset: the same instant as written
    from ontology.project import _timestamp
    stored, problem = _timestamp(text, stamp)
    return stored if stored is not None and problem is None else text


def _bc_entity_set(resolved: Mapping[str, Any]) -> str | None:
    """The BC entity set of a re-read BC view: its relation (``bc.<entity set>``) or its
    own citation (a BC capture view)."""
    relation, cited = resolved.get("relation"), resolved.get("citation")
    if isinstance(relation, str):
        return relation.removeprefix("bc.") if relation.startswith("bc.") else None
    if isinstance(cited, str) and cited.startswith("bc://"):
        parts = cited[len("bc://"):].split("@", 1)[0].split("/")
        return parts[2] if len(parts) == 4 else None
    return None


@lru_cache(maxsize=1)
def _money_columns() -> frozenset[tuple[str, str, str]]:
    """(source, relation or BC entity set, column) of every Portal mirror and BC column the
    ontology schema types ``money``; none when the schema does not load (a tool's returned
    currency then marks money alone)."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: no money columns
        return frozenset()
    out = set()
    for spec in schema.object_types.values():
        for prop in spec.get("properties") or ():
            if prop.get("value_type") != "money":
                continue
            for source in prop.get("sources") or ():
                _type, binding = schema.bindings.get(source.get("binding"), (None, {}))
                path = source.get("path")
                if binding.get("source") in ("portal_api", "bc") and isinstance(path, str) \
                        and "." not in path:
                    out.add((binding["source"], binding["relation"], path))
    return frozenset(out)


def _money_cell(handle: str, resolved: Mapping[str, Any], field: str) -> bool:
    """Whether the schema types a cited row column money (``_money_columns``)."""
    match = _COLUMN_FIELD.match(field)
    if match is None or field.startswith("key."):
        return False
    if handle.startswith("api://"):
        where = ("portal_api", resolved.get("relation"))
    elif handle.startswith("bc://"):
        where = ("bc", _bc_entity_set(resolved))
    else:
        return False
    return (*where, match.group(1)) in _money_columns()


def _fail(out: dict[str, Any], code: str, detail: str) -> dict[str, Any]:
    return {**out, "status": "failed", "code": code, "detail": detail}


def _changed(resolved: Mapping[str, Any] | None) -> bool:
    """Whether a re-read is a changed-since view: the cited revision moved on
    (``changed_since`` true, as ``CaseStore`` refuses support; ``CaseRuntime._live_view``,
    ``_capture_view``, ``claim_row``). Any flag but an explicit false is changed: it fails closed."""
    return resolved is not None and resolved.get("changed_since") not in (None, False)


def _moved_on(resolved: Mapping[str, Any], fields: Sequence[str], evidence: "_Evidence"
              ) -> tuple[Mapping[str, Any] | None, str]:
    """A changed-since re-read (``_changed``) judged by what it cites: (the current
    revision's view, "") when the source names one (``current_citation``), this reader can
    read it as current, and every cited ``field`` holds the same value there, so a source
    that moved on elsewhere in the record (a live mirror ahead of the served projection)
    never refuses the claim; else (None, why): the value itself changed (what it is now,
    and the revision to cite), or no current revision can be read (refused)."""
    current = resolved.get("current_citation")
    view = evidence.row(current) if isinstance(current, str) and current \
        and current != resolved.get("citation") else None
    if view is None or _changed(view):
        return None, ("the cited revision is no longer the source's current one and no "
                      "current revision of it can be read now")
    cited = resolved.get("revision") if isinstance(resolved.get("revision"), Mapping) \
        else resolved    # a gate view keeps the superseded row only as ``revision``
    for field in fields:
        (had, before), (has, now) = dig(cited, field), dig(view, field)
        if not had or not has or before != now:
            shown = json.dumps(now, default=str)[:120] if has else "absent"
            return None, (f"the cited {field} changed since this revision: it is now "
                          f"{shown} in {current[:300]}; cite that revision")
    return view, ""


def _moment_of(match: re.Match[str]) -> tuple[str, datetime | None] | None:
    """(the calendar date as written, the moment when a time is written: aware when an
    offset is) of a ``_MOMENT`` match; None when it names no real date or time."""
    try:
        day = date(int(match["y"]), int(match["m"]), int(match["d"]))
        if match["H"] is None:
            return day.isoformat(), None
        zone = match["tz"]
        tzinfo = None if zone is None else timezone.utc if zone == "Z" else timezone(
            (1 if zone[0] == "+" else -1) * timedelta(
                hours=int(zone[1:3]), minutes=int(zone[3:].lstrip(":") or 0)))
        fraction = (match["f"] or ".")[1:7].ljust(6, "0")
        return day.isoformat(), datetime(day.year, day.month, day.day, int(match["H"]),
                                         int(match["M"]), int(match["S"] or 0),
                                         int(fraction), tzinfo=tzinfo)
    except ValueError:
        return None


def _moment(text: Any) -> tuple[str, datetime | None] | None:
    """``_moment_of`` a whole ISO date or timestamp, else None."""
    match = _ISO_MOMENT.fullmatch(text.strip()) if isinstance(text, str) else None
    return _moment_of(match) if match else None


def _same_instant(one: datetime, other: datetime) -> bool:
    """Two timestamps on one instant: both with an offset, or both without (a time without
    an offset is no instant, never equal to one with an offset)."""
    return (one.tzinfo is None) == (other.tzinfo is None) and one == other


def _day(moment: tuple[str, datetime | None], zone: tzinfo | None) -> str:
    """The calendar day of a ``_moment``: an instant's day in the time zone its source
    declares (``zone``), else as written (its own offset: a value ending ``Z`` is its UTC
    day; a time without an offset, or a bare date, is that day)."""
    when = moment[1]
    if when is None or when.tzinfo is None or zone is None:
        return moment[0]
    return when.astimezone(zone).date().isoformat()


# A date claim as the prose checks read it: (its ``_moment``, the instant of the cell a
# bare date was read from or None, the time zone its source declares or None).
_Dated = tuple[tuple[str, "datetime | None"], "datetime | None", "tzinfo | None"]


def _covers(reading: _Dated, written: tuple[str, datetime | None]) -> bool:
    """Whether a date claim covers a date or timestamp written in prose: a date written
    when it is the claimed value's day (``_day``: a claimed timestamp in its source's
    zone); a timestamp written only when it is an instant the claim establishes, the
    claimed timestamp's or that of the cell a claimed date was read from. A date claim
    establishes no other time of its day (``2026-01-07`` read from ``23:30-05:00`` does
    not cover ``2026-01-07 12:00+00:00``)."""
    claimed, cell, zone = reading
    if written[1] is None:
        return written[0] == _day(claimed, zone)
    instant = claimed[1] if claimed[1] is not None else cell
    return instant is not None and _same_instant(instant, written[1])


def _fold(text: str) -> str:
    """An enum value as compared: NFKC, case-folded, ``_`` and whitespace runs one space."""
    return " ".join(re.split(r"[\s_]+", unicodedata.normalize("NFKC", text).casefold())
                    ).strip()


@lru_cache(maxsize=1)
def _enum_columns() -> frozenset[tuple[str, str]]:
    """(relation, column path) of every Portal mirror column the ontology schema types
    ``enum``; none when the schema does not load (values then compare exactly)."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: no enum folding (stricter, never looser)
        return frozenset()
    out = set()
    for spec in schema.object_types.values():
        for prop in spec.get("properties") or ():
            if prop.get("value_type") != "enum":
                continue
            for source in prop.get("sources") or ():
                _type, binding = schema.bindings.get(source.get("binding"), (None, {}))
                if binding.get("source") == "portal_api" and source.get("path"):
                    out.add((binding["relation"], source["path"]))
    return frozenset(out)


def value_form(claim: Mapping[str, Any], handle: str = "",
               resolved: Mapping[str, Any] | None = None, field: str = "") -> str:
    """How a claim's value is compared and stated: ``number``, ``date`` (an ISO date or
    timestamp), ``enum`` (a status/state column, or a Portal column the schema types
    enum) or ``text`` (names and codes, exactly)."""
    if claim["kind"] == "number":
        return "number"
    if _moment(claim["value"]) is not None:
        return "date"
    column = field.removeprefix("row.")
    if _COLUMN_FIELD.match(field) and _STATUS_COLUMN.search(field.rsplit(".", 1)[-1]) or (
            handle.startswith("api://") and field.startswith("row.") and resolved
            and (str(resolved.get("relation")), column) in _enum_columns()):
        return "enum"
    return "text"


def _not_in_quote(value: str) -> str:
    """Why a claim's quote fails because it does not hold the claim's value, in words a
    repair can act on (EMD-10: a value longer than the quote meant to hold it)."""
    shown = value if len(value) <= 60 else value[:57] + "..."
    return (f"the value {shown} is not inside its own quote: a quote is the passage as "
            "written that holds the whole value; quote a passage that contains all of it, "
            "or claim as the value only what the quote holds")


def _record(handle: str, resolved: Mapping[str, Any]) -> tuple[str, ...]:
    """The record a re-read view is of, version-free: a mirror row by its relation and key,
    any other by the view's own citation without its version and fragment (a BC capture
    revision, a page of an original)."""
    if isinstance(resolved.get("relation"), str) and isinstance(resolved.get("key"), Mapping):
        return ("row", resolved["relation"], json.dumps(resolved["key"], sort_keys=True,
                                                        default=str))
    cited = resolved.get("citation") if isinstance(resolved.get("citation"), str) else handle
    body = cited.split("#", 1)[0]
    record = ("record", body.rpartition("@")[0] or body)
    # A member of a container (a part of a .msg, a file in a zip) is its own record, cited
    # by its container's handle and its path (``captured_text.member_handle``): two members
    # of one file are two records, the same member under two pins or on any page one. A
    # member is its own record only when the view shows the container holds it (the row
    # the capture reads from the pinned bytes, ``captured_text.member_view``); a path the
    # view does not show is the container's record, so naming paths adds no record.
    member = _member(cited)
    if member is None and isinstance(handle, str) and handle.split("#", 1)[0] == body:
        member = _member(handle)
    if member is None or not _member_shown(resolved, *member):
        return record
    return (*record, member[0])


def _member(handle: str) -> tuple[str, str | None] | None:
    """(member path, member SHA-256 or None) a container member's citation names, with or
    without a page (a page of a part is that part), else None: a citation that is not
    exactly a member's (``captured_text.split_member``) or names the container itself
    (``.``) names none."""
    try:
        from integrations.captured_text import split_member
        from ontology.mentions import cited_page
        split = split_member(cited_page(handle)[0])
    except Exception:  # noqa: BLE001 - not decidable: the container's record (fail closed)
        return None
    if split is None or not split[1] or split[1] == ".":
        return None
    return split[1], split[2]


def _member_shown(resolved: Mapping[str, Any], path: str, digest: str | None) -> bool:
    """Whether ``resolved`` shows the member at ``path`` (bytes ``digest``) exists: its row
    is the one the capture reads from the container's pinned bytes for that member
    (``captured_text.member_view``: its path, bytes and MIME type, which a gate-only
    resolve that never opens the container does not state)."""
    row = resolved.get("row")
    return (isinstance(row, Mapping) and row.get("path") == path
            and row.get("content_sha256") == digest and "mime_type" in row)


def _part_of(part: tuple[str, ...], whole: tuple[str, ...]) -> bool:
    """Whether the record ``part`` is the record ``whole`` or within it: a member of a
    file is within the file, a member of a nested container (``inner.zip!/a.pdf``)
    within that container's member."""
    if part[0] != "record" or part[:2] != whole[:2] or len(part) < len(whole):
        return False
    return len(whole) == 2 or part[2] == whole[2] or part[2].startswith(whole[2] + "!/")


def _collection(record: tuple[str, ...]) -> tuple[str, ...]:
    """The source collection a ``_record`` is in: a mirror row's relation, else its
    citation up to the record's id (a BC company's entity set, a mailbox, a drive)."""
    return record[:2] if record[0] == "row" else ("record", record[1].rpartition("/")[0])
