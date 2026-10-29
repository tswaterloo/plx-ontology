"""Meeting agendas assembled from cited records since the previous meeting (``plx.agenda``).

A recurring meeting's agenda is a spec (``ontology/agendas/<id>.json``, format
``plx-meeting-agenda/v1``, ``_agenda.schema.json``): the lanes that feed it (object type,
the property that says when a record changed, the ones a line cites as title, owner and
status), the agenda sections, where the previous agenda was posted, and the sources the map
names that the ontology does not hold. :func:`assemble` reads the lanes only through the
host's generic primitives as the asking reader (``operating.cited_reads``), so every
permission rule is theirs, and returns:

* ``open_projects``: every open record of a ``project`` lane, with its status;
* ``changes``: records of every lane changed since the previous meeting;
* ``blockers``: open projects with no change this cycle (``no_change_since_last_meeting``)
  or waiting on an open record of a ``decision`` lane (``waiting_on_decision``);
* ``decisions_needed``: the open records of ``decision`` lanes;
* ``owners``: the cited owner of each line, grouped.

Every line names the object and cites the values it shows (``untrusted_text`` envelopes kept
as the primitives gave them). A value a line lacks is named on it (``owner_withheld`` when
the schema marks the owner property and the reader was withheld a property,
``owner_not_recorded`` otherwise, ``text_not_projected`` while the type declares no text
property the lane expects); a lane without the scope values it needs, a source not served or
not in the ontology, a withheld or truncated read are gaps of the answer, never an empty
section that reads as nothing to report.

A lane is read with the scope values the caller gives for it or, when it gives none, with
the lane's ``default_scope`` (values plus the evidence naming them, e.g. the one chat whose
topic is the meeting's); ``scope_basis`` says which, per lane. A default is a filter only:
every record is still read as the asking reader.

The cycle starts at ``since`` (an ISO-8601 instant with its offset) or, when the caller
gives none, at the latest record of the ``previous_agenda`` lane whose attachments name a
previous agenda (cited); with neither, nothing is assembled (``previous_agenda_not_found``).

``register`` serves ``plx.agenda`` in the host tool table (``ontology_host.tool_table``);
the MCP hosts list it to the agents that name it (``operating/agent_definitions.json``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from ontology.json_contract import validate

from .cited_reads import CitedReads, Record, gap, json_of, text_of

ROOT = Path(__file__).resolve().parents[1]
AGENDA_DIR = ROOT / "ontology" / "agendas"
FORMAT_PATH = AGENDA_DIR / "_agenda.schema.json"
TOOL_NAME = "plx.agenda"
TOOL_NAMES = (TOOL_NAME,)
ANSWER_FORMAT = "plx-meeting-agenda-answer/v1"
DEFAULT_LIMIT = 40   # lines per page of plx.agenda, fewer when MAX_BYTES ends the page
# The contracts' limits.max_result_bytes (48000) less room for the host's labels.
MAX_BYTES = 44000
# A title longer than this is cited only (a SharePoint list item's whole fields JSON).
MAX_TITLE_CHARS = 600
KINDS = ("open_projects", "changes", "blockers", "decisions_needed")


class AgendaSpecError(ValueError):
    """An agenda spec that does not hold (format or schema cross-check)."""


@dataclass(frozen=True)
class Agenda:
    """A checked spec plus what the schema says about it."""

    doc: Mapping[str, Any]
    # lane id -> text_expected properties the schema does not declare (binding_expected)
    undeclared: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # lane id -> owner properties the schema marks (a withheld one is owner_withheld)
    marked_owner: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # lane id -> value_type of its ``changed`` property
    changed_type: Mapping[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.doc["id"]

    def lane(self, lane_id: str) -> Mapping[str, Any]:
        return next(lane for lane in self.doc["lanes"] if lane["id"] == lane_id)


def _props(spec: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {p["name"]: p for p in spec.get("properties") or ()}


def check_agenda(doc: Mapping[str, Any], schema: Any) -> list[str]:
    """Problems of ``doc``: its format, then (with ``schema``, an
    ``ontology_tools.SchemaView``) every type, property and link type declared."""
    fmt = json.loads(FORMAT_PATH.read_text(encoding="utf-8"))
    out = list(validate(doc, fmt))
    if out:
        return out
    sections = {s["id"] for s in doc["sections"]}
    lanes = {lane["id"]: lane for lane in doc["lanes"]}
    if len(lanes) != len(doc["lanes"]):
        out.append("lanes: duplicate id")
    ids = [e["id"] for e in doc["not_in_ontology"]]
    if len(set(ids)) != len(ids):
        out.append("not_in_ontology: duplicate id")
    previous = doc.get("previous_agenda")
    if previous is not None:
        if previous["lane"] not in lanes:
            out.append(f"previous_agenda: unknown lane {previous['lane']}")
        try:
            re.compile(previous["name_pattern"])
        except re.error:
            out.append("previous_agenda: name_pattern does not compile")
    for lane in doc["lanes"]:
        lid = lane["id"]
        if lane["section"] not in sections:
            out.append(f"{lid}: unknown section {lane['section']}")
        via = lane.get("via")
        if via is not None and (via["lane"] not in lanes or via["lane"] == lid
                                or "via" in lanes[via["lane"]]):
            out.append(f"{lid}: via must name another lane read by scope")
        if lane["role"] == "decision" and via is None:
            out.append(f"{lid}: a decision lane is reached via a project lane")
        default = lane.get("default_scope")
        if default is not None and set(default["values"]) != set(lane.get("scope", ())):
            out.append(f"{lid}: default_scope must give every scope property, and only those")
        if lane["role"] in ("project", "decision") and "open" not in lane:
            out.append(f"{lid}: a {lane['role']} lane says when a record is open")
        if schema is None:
            continue
        spec = schema.object_types.get(lane["object_type"])
        if spec is None:
            out.append(f"{lid}: undeclared object type {lane['object_type']}")
            continue
        if (spec.get("availability") or {}).get("state") != "served":
            out.append(f"{lid}: {lane['object_type']} is not served (list it in not_in_ontology)")
        props = _props(spec)
        named = [lane["changed"], *lane.get("title", ()), *lane.get("owner", ()),
                 *lane.get("scope", ()), *([lane["status"]] if "status" in lane else ())]
        named += [lane[k]["property"] for k in ("open", "skip") if k in lane]
        for name in named:
            if name not in props:
                out.append(f"{lid}: {lane['object_type']} declares no property {name}")
        if lane["changed"] in props and props[lane["changed"]]["value_type"] not in (
                "timestamp", "date"):
            out.append(f"{lid}: changed must be a timestamp or date property")
        if via is not None:
            link = schema.link_types.get(via["link_type"])
            far = via["lane"] in lanes and lanes[via["lane"]]["object_type"]
            ends = (link["to"], link["from"]) if link and via["direction"] == "reverse" else (
                (link["from"], link["to"]) if link else (None, None))
            if link is None or ends != (far, lane["object_type"]):
                out.append(f"{lid}: {via['link_type']} {via['direction']} does not lead from "
                           f"{far} to {lane['object_type']}")
    if schema is not None:
        for entry in doc["not_in_ontology"]:
            if "object_type" in entry and entry["object_type"] not in schema.object_types:
                out.append(f"{entry['id']}: undeclared object type {entry['object_type']}")
    return out


def load_agenda(path: Path, schema: Any) -> Agenda:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = check_agenda(doc, schema)
    if problems:
        raise AgendaSpecError(f"{Path(path).name}: " + "; ".join(problems[:8]))
    undeclared, marked, changed = {}, {}, {}
    for lane in doc["lanes"]:
        props = _props(schema.object_types[lane["object_type"]]) if schema is not None else {}
        undeclared[lane["id"]] = tuple(p for p in lane.get("text_expected", ()) if p not in props)
        marked[lane["id"]] = frozenset(p for p in lane.get("owner", ())
                                       if (props.get(p) or {}).get("markings"))
        changed[lane["id"]] = (props.get(lane["changed"]) or {}).get("value_type", "timestamp")
    return Agenda(doc, undeclared, marked, changed)


def load_agendas(directory: Path = AGENDA_DIR, schema: Any = None) -> dict[str, Agenda]:
    """Every agenda spec under ``directory`` (files not starting with ``_``)."""
    out: dict[str, Agenda] = {}
    for path in sorted(Path(directory).glob("*.json")):
        if path.name.startswith("_"):
            continue
        agenda = load_agenda(path, schema)
        if agenda.id in out:
            raise AgendaSpecError(f"{path.name}: duplicate agenda id {agenda.id}")
        out[agenda.id] = agenda
    return out


def instant(value: Any) -> dt.datetime | None:
    """An ISO-8601 instant with its offset (``Z`` or ``+hh:mm``), or None. A bare date
    or a local time without an offset is no instant (its day depends on a time zone)."""
    text = text_of(value)
    if not text or not re.match(r"^\d{4}-\d{2}-\d{2}T", text):
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(dt.timezone.utc) if moment.tzinfo is not None else None


def _day(value: Any) -> dt.date | None:
    text = text_of(value)
    if not text or not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _changed_since(cell: Mapping[str, Any] | None, value_type: str,
                   since: dt.datetime) -> bool | None:
    """Whether a cited change time is at or after ``since``; None when unreadable. A date
    is on or after the day of ``since`` (inclusive: a record of the meeting day counts)."""
    if cell is None:
        return None
    if value_type == "date":
        day = _day(cell["value"])
        return None if day is None else day >= since.date()
    moment = instant(cell["value"])
    return None if moment is None else moment >= since


def _cell(record: Record, names: Sequence[str]) -> dict[str, Any] | None:
    cell = record.get(*names)
    if cell is None:
        return None
    citation = {k: cell["citation"][k] for k in ("l1_handle", "field_path", "digest")
                if k in (cell.get("citation") or {})}
    return {"property": cell["property"], "value": cell["value"], "citation": citation}


def _matches(record: Record, condition: Mapping[str, Any] | None) -> bool | None:
    if condition is None:
        return None
    value = record.text(condition["property"])
    if value is None:
        return None
    if "in" in condition:
        return value in condition["in"]
    return value not in condition["not_in"]


class _Assembly:
    def __init__(self, agenda: Agenda, reads: CitedReads) -> None:
        self.agenda, self.reads = agenda, reads
        self.lines: list[dict[str, Any]] = []
        self.lane_gaps: dict[tuple[str, str], list[str]] = {}

    def line(self, kind: str, lane: Mapping[str, Any], record: Record,
             **extra: Any) -> dict[str, Any] | None:
        handles = record.handles()
        if not handles:
            self.reads.add(gap("uncited_record", "unknown", record.object_id,
                               "the record carries no cited value; no line is made of it"))
            return None
        missing: list[str] = []
        owner = _cell(record, lane["owner"]) if "owner" in lane else None
        if "owner" in lane and owner is None:
            withheld = bool(record.withheld_properties and self.agenda.marked_owner[lane["id"]])
            missing.append("owner_withheld" if withheld else "owner_not_recorded")
        title = _cell(record, lane.get("title", ()))
        if title is not None and len(text_of(title["value"]) or "") > MAX_TITLE_CHARS:
            title = None
            missing.append("title_cited_only")
        elif title is None:
            missing.append("text_not_projected" if self.agenda.undeclared[lane["id"]]
                           else "title_not_recorded")
        out = {"kind": kind, "lane": lane["id"], "section": lane["section"],
               "object_id": record.object_id, "object_type": record.object_type,
               "at": _cell(record, (lane["changed"],)), "title": title,
               "status": _cell(record, (lane["status"],)) if "status" in lane else None,
               "owner": owner, "citations": handles, "missing": missing, **extra}
        for code in missing:
            self.lane_gaps.setdefault((lane["id"], code), []).append(record.object_id)
        self.lines.append(out)
        return out

    def finish(self) -> list[dict[str, Any]]:
        order = {s["id"]: n for n, s in enumerate(self.agenda.doc["sections"])}
        self.lines.sort(key=lambda line: (KINDS.index(line["kind"]), order[line["section"]],
                                          text_of((line["at"] or {}).get("value")) or "",
                                          line["object_id"]))
        for n, line in enumerate(self.lines, 1):
            line["line"] = f"L{n}"
        ids = {(line["kind"], line["object_id"]): line["line"] for line in self.lines}
        for line in self.lines:
            if "decision_objects" in line:
                line["decisions"] = [ids[("decisions_needed", o)]
                                     for o in line.pop("decision_objects")]
        states = {"owner_withheld": "inaccessible", "owner_not_recorded": "absent_in_source",
                  "text_not_projected": "not_ingested", "title_not_recorded": "absent_in_source",
                  "title_cited_only": "unknown"}
        for (lane, code), objects in sorted(self.lane_gaps.items()):
            self.reads.add(gap(code, states[code], lane,
                               f"{len(objects)} line(s) of lane {lane} lack it"))
        return self.lines


def _previous_agenda(agenda: Agenda, reads: CitedReads,
                     scope: Mapping[str, Mapping[str, str]]
                     ) -> tuple[dt.datetime, dict[str, Any], str] | None:
    """The latest posting of a previous agenda: (its time, the cited cell, object id)."""
    spec = agenda.doc.get("previous_agenda")
    if spec is None:
        return None
    lane = agenda.lane(spec["lane"])
    given = scope.get(lane["id"]) or {}
    if any(p not in given for p in lane["scope"]):
        return None
    pattern = re.compile(spec["name_pattern"])
    subject = f"{lane['id']} (previous agenda)"
    ids = reads.find(lane["object_type"], [*_filters(lane, given), {
        "property": spec["property"], "op": "present"}], subject=subject) or []
    if any(g["code"] == "read_truncated" and g["subject"] == subject for g in reads.gaps):
        return None   # the latest posting may be among the records not read
    best: tuple[dt.datetime, dict[str, Any], str] | None = None
    for record in reads.read_all(ids):
        items = json_of((record.get(spec["property"]) or {}).get("value"))
        names = [text_of(i.get("name")) for i in items if isinstance(i, Mapping)] \
            if isinstance(items, list) else []
        if not any(n and pattern.search(n) for n in names):
            continue
        cell = _cell(record, (lane["changed"],))
        moment = instant(cell["value"]) if cell else None
        if moment is not None and (best is None or moment > best[0]):
            best = (moment, cell, record.object_id)
    return best


def _with_defaults(agenda: Agenda, scope: Mapping[str, Mapping[str, str]]
                   ) -> tuple[dict[str, Mapping[str, str]], dict[str, str]]:
    """The caller's scope, with each lane it gives nothing for read by the spec's
    ``default_scope`` (a caller's scope for a lane replaces the default whole); and per
    lane read with values, where they came from (``argument`` or ``default``)."""
    merged: dict[str, Mapping[str, str]] = {}
    basis: dict[str, str] = {}
    for lane in agenda.doc["lanes"]:
        lid, default = lane["id"], lane.get("default_scope")
        if scope.get(lid):
            merged[lid], basis[lid] = dict(scope[lid]), "argument"
        elif default is not None:
            merged[lid], basis[lid] = dict(default["values"]), "default"
    return merged, basis


def _filters(lane: Mapping[str, Any], given: Mapping[str, str]) -> list[dict[str, Any]]:
    return [{"property": p, "op": "eq", "value": given[p]} for p in lane["scope"]]


def assemble(call: Callable[..., Mapping[str, Any]], actor: Any, agenda: Agenda, *,
             since: str | None = None, scope: Mapping[str, Mapping[str, str]] | None = None,
             max_records: int = 200, offset: int = 0, limit: int | None = None
             ) -> dict[str, Any]:
    """The agenda for ``actor`` from cited records since the previous meeting. Lines are
    numbered in one order (kind, section, time, object) and paged by ``offset`` and
    ``limit`` over that order; gaps and owners always cover every line."""
    scope, basis = _with_defaults(agenda, scope or {})
    reads = CitedReads(call, actor, max_records=max_records)
    doc = agenda.doc
    out: dict[str, Any] = {"format": ANSWER_FORMAT, "agenda": agenda.id, "title": doc["title"],
                           "source": dict(doc["source"]),
                           "sections": [dict(s) for s in doc["sections"]],
                           "scope_basis": dict(sorted(basis.items()))}
    skip_object = None
    if since is not None:
        start = instant(since)
        if start is None:
            return {"error": {"code": "invalid_since", "message":
                              "since must be an ISO-8601 instant with its offset"}}
        out["since"] = {"value": start.isoformat().replace("+00:00", "Z"), "basis": "argument"}
    else:
        found = _previous_agenda(agenda, reads, scope)
        if found is None:
            reads.add(gap("previous_agenda_not_found", "unknown", "since",
                          "no since was given and no previous agenda posting is visible to "
                          "this reader in the previous-agenda lane; nothing is assembled"))
            reads.withheld_gap(agenda.id)
            return {**out, "since": None, **{k: [] for k in KINDS}, "owners": [],
                    "gaps": reads.gaps, "withheld": reads.withheld, "complete": False,
                    "coverage": {"lanes": len(doc["lanes"]), "lanes_read": 0},
                    "page": {"offset": 0, "returned": 0, "total": 0, "next_offset": None}}
        start, cell, skip_object = found
        out["since"] = {"value": start.isoformat().replace("+00:00", "Z"),
                        "basis": "previous_agenda", "object_id": skip_object, "cited": cell}
    work = _Assembly(agenda, reads)
    read_lanes = 0
    reached: dict[str, list[Record]] = {}
    unscoped: set[str] = set()
    for lane in doc["lanes"]:
        lid = lane["id"]
        if agenda.undeclared[lid]:
            reads.add(gap("binding_expected", "not_ingested", lid,
                          f"{lane['object_type']} declares no {', '.join(agenda.undeclared[lid])}"
                          "; lines cite the record and show no text until a projection "
                          "declares it"))
        if "via" in lane:
            continue
        given = scope.get(lid) or {}
        if any(p not in given for p in lane["scope"]):
            reads.add(gap("lane_scope_not_given", "unknown", lid,
                          f"{lane['label']}: give {', '.join(lane['scope'])} in scope.{lid}; "
                          "the lane was not read"))
            unscoped.add(lid)
            continue
        filters = _filters(lane, given)
        if lane["role"] == "activity":
            value = out["since"]["value"]
            filters.append({"property": lane["changed"], "op": "gte", "value": (
                value[:10] if agenda.changed_type[lid] == "date" else value)})
        ids = reads.find(lane["object_type"], filters, subject=lid)
        if ids is None:
            continue
        read_lanes += 1
        records = [r for r in reads.read_all(ids) if r.object_id != skip_object
                   and _matches(r, lane.get("skip")) is not True]
        reached[lid] = records
        for record in records:
            changed = _changed_since(record.get(lane["changed"]), agenda.changed_type[lid], start)
            if lane["role"] == "activity":
                work.line("changes", lane, record)
                continue
            is_open = _matches(record, lane["open"])
            if is_open is None:
                reads.add(gap("status_unknown", "unknown", record.object_id,
                              f"{lane['open']['property']} is not readable; open or closed "
                              "is not decided"))
            if changed:
                work.line("changes", lane, record)
            if is_open:
                work.line("open_projects", lane, record)
    decided: dict[str, list[str]] = {}
    for lane in doc["lanes"]:
        via = lane.get("via")
        if via is None:
            continue
        if via["lane"] not in reached:
            if via["lane"] in unscoped:
                reads.add(gap("lane_scope_not_given", "unknown", lane["id"],
                              f"{lane['label']}: reached via {via['lane']}, whose scope was "
                              "not given; the lane was not read"))
            else:
                reads.add(gap("lane_not_read", "unknown", lane["id"],
                              f"reached via {via['lane']}, whose read failed"))
            continue
        read_lanes += 1
        far, seen = agenda.lane(via["lane"]), set()
        for source in reached[via["lane"]]:
            if _matches(source, far["open"]) is not True:
                continue
            for record in reads.read_all(reads.around(source.object_id, via["link_type"],
                                                      via["direction"]) or ()):
                if record.object_id in seen or _matches(record, lane["open"]) is not True:
                    continue
                seen.add(record.object_id)
                if work.line("decisions_needed", lane, record) is not None:
                    decided.setdefault(source.object_id, []).append(record.object_id)
    for lane in doc["lanes"]:
        if lane["role"] != "project":
            continue
        for record in reached.get(lane["id"], ()):
            if _matches(record, lane["open"]) is not True:
                continue
            if record.object_id in decided:
                work.line("blockers", lane, record, basis="waiting_on_decision",
                          decision_objects=decided[record.object_id])
            elif _changed_since(record.get(lane["changed"]), agenda.changed_type[lane["id"]],
                                start) is False:
                work.line("blockers", lane, record, basis="no_change_since_last_meeting")
    for entry in doc["not_in_ontology"]:
        if "object_type" not in entry:
            reads.add(gap("source_not_in_ontology", "not_ingested", entry["label"],
                          entry["needs"]))
            continue
        probe = reads.call("plx.find", {"object_type": entry["object_type"], "limit": 1}, actor)
        if (probe.get("error") or {}).get("code") == "not_served":
            reads.add(gap("source_not_served", "not_ingested", entry["label"], entry["needs"]))
        else:
            reads.add(gap("lane_not_declared", "unknown", entry["label"],
                          f"{entry['object_type']} is readable now; the agenda spec reads no "
                          "lane of it"))
    lines = work.finish()
    reads.withheld_gap(agenda.id)
    owners: dict[str, dict[str, Any]] = {}
    for line in lines:
        if line["owner"] is not None:
            key = text_of(line["owner"]["value"]) or ""
            owners.setdefault(key, {"owner": line["owner"]["value"], "lines": []})["lines"].append(
                line["line"])
    failed = {"read_failed", "read_truncated", "lane_not_read"}
    complete = not any(g["code"] in failed for g in reads.gaps)

    def paged(end: int) -> dict[str, Any]:
        page = lines[offset:end]
        return {**out, **{kind: [l for l in page if l["kind"] == kind] for kind in KINDS},
                "page": {"offset": offset, "returned": len(page), "total": len(lines),
                         "next_offset": end if end < len(lines) else None},
                "owners": [owners[k] for k in sorted(owners)], "gaps": reads.gaps,
                "withheld": reads.withheld, "complete": complete,
                "coverage": {"lanes": len(doc["lanes"]), "lanes_read": read_lanes,
                             "not_in_ontology": len(doc["not_in_ontology"])}}
    end = len(lines) if limit is None else min(len(lines), offset + limit)
    result = paged(end)
    # A page ends before the result budget: the next page starts where this one ends.
    while limit is not None and end > offset + 1 and len(json.dumps(result)) > MAX_BYTES:
        end -= 1
        result = paged(end)
    return result


def _contract() -> Mapping[str, Any]:
    """``plx.agenda``'s entry in ``operating/ontology_tool_contracts.json``: its listing
    (description, input) and its output contract, whose typed handles (every line's
    ``citations`` and each cited value's ``l1_handle``) the final-answer gate takes as
    evidence (``answer_gate.evidence_handles``)."""
    from .ontology_tools import load_contracts
    return load_contracts()["tools"][TOOL_NAME]


_CONTRACT = _contract()
TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": _CONTRACT["description"],
    "inputSchema": _CONTRACT["input"],
    "annotations": {"readOnlyHint": True},
}


class AgendaTool:
    """``plx.agenda`` over a dispatch ``call(name, arguments, actor)``."""

    def __init__(self, call: Callable[..., Mapping[str, Any]], schema: Any,
                 directory: Path = AGENDA_DIR) -> None:
        self.dispatch, self.schema, self.directory = call, schema, directory
        self._agendas: dict[str, Agenda] | None = None

    def agendas(self) -> dict[str, Agenda]:
        if self._agendas is None:
            self._agendas = load_agendas(self.directory, self.schema)
        return self._agendas

    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        problems = validate(arguments, TOOL["inputSchema"]) if isinstance(arguments, dict) \
            else ["arguments must be an object"]
        if problems:
            return {"error": {"code": "invalid_input", "message": problems[0][:300]}}
        try:
            agendas = self.agendas()
        except (AgendaSpecError, OSError, ValueError) as exc:
            return {"error": {"code": "agenda_spec_invalid", "message": str(exc)[:300]}}
        agenda = agendas.get(arguments["agenda"])
        if agenda is None:
            return {"error": {"code": "unknown_agenda", "message": "known: " + ", ".join(
                sorted(agendas))}}
        scope = arguments.get("scope") or {}
        lanes = {lane["id"]: lane for lane in agenda.doc["lanes"]}
        for lid, given in scope.items():
            if lid not in lanes or not set(given) <= set(lanes[lid].get("scope", ())):
                return {"error": {"code": "invalid_scope", "message":
                                  f"scope.{lid}: lane or property not declared by "
                                  f"{agenda.id}"[:300]}}
        return assemble(self.dispatch, actor, agenda, since=arguments.get("since"),
                        scope=scope, offset=arguments.get("offset", 0),
                        limit=arguments.get("limit", DEFAULT_LIMIT))


def register(table: dict[str, dict[str, Any]], schema: Any) -> AgendaTool:
    """Serve ``plx.agenda`` in a host tool table (``ontology_host.tool_table``): its reads
    are that table's ``plx.find``/``plx.object``/``plx.around`` handlers, so it runs for the
    actor the host passes (employee-gated) under the policy the host decided for the call,
    as ``plx.investigate`` does."""
    tool = AgendaTool(lambda name, arguments, actor: table[name]["handler"](arguments, actor),
                      schema)
    if TOOL_NAME in table:
        raise ValueError(f"tool already registered: {TOOL_NAME}")
    table[TOOL_NAME] = dict(TOOL, handler=tool.call)
    return tool


__all__ = ["AGENDA_DIR", "ANSWER_FORMAT", "Agenda", "AgendaSpecError", "AgendaTool", "KINDS",
           "TOOL", "TOOL_NAME", "TOOL_NAMES", "assemble", "check_agenda", "instant", "load_agenda",
           "load_agendas", "register"]
