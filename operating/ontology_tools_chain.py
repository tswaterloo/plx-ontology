"""``plx.sql {"chain": "<record handle or number>", "hops": N}``: follow one record through its
links in ONE read (Ask spec 16.10 mechanism 2, K1).

The host walks ``record_links`` both ways from the record (by id; the start is found by handle
or by number through the handle and label indexes, ``records`` and the links' own
``from_handle`` / ``to_handle``) and returns every record linked to it, hop by hop, as one
result of the ordinary kind: every row held and cited ``sql://<id>#row=<n>``, read on with
``{"page": ..., "offset": n}``, the profile's exact counts, and a ``chain`` summary of how many
records of each type were reached. A row is one link: the hop, which way it points, its type
and tier, the record at the far end (type, handle, label, date, system) and its ``facts``: the
fields the type's own catalog declares as money, quantity, number, date or enum, and the
category-like fields the catalog lists, each with its currency or unit where the record holds
one. Which field is "the amount" or "the status" is never decided here: every declared one is
shown.

Conservative identity: a number that fits several records (the same number in two systems, a
reused number) is never resolved by taking one; the candidates come back as the result, to pick
by handle, by ``type`` or by ``party`` (a name that a neighbouring record's label holds). A
link whose tier is a guess (``GUESS_TIER``) is listed, with its tier, and not walked from.

Permissions are the door's: every statement is read as ``plx.sql`` reads a statement (the same
check, ceiling, scoped transaction and withheld counts), so a record or link the reader may
not read is not in the result. No bound is placed on the walk: the ids of a frontier go to
Postgres in batches (``BATCH``: the size of one statement, not a limit), every batch is read,
and a hub (a supplier's thousands of purchase orders) is walked when the read asks for it."""
from __future__ import annotations

import json
import time
from typing import Any, Mapping, Sequence

from operating import result_profile
from operating.ontology_tools_identity import ToolError
from operating.pg import generic_views as gv
from operating.pg.sql_views import _literal

# the link tier the catalog calls a guess (record_links.tier; ``generic_views.LINK_TIERS``):
# listed beside the others, never walked from
GUESS_TIER = "lead"
assert GUESS_TIER in gv.LINK_TIERS
# the ids one statement carries (statement size only: every batch is read)
BATCH = 1000
COLUMNS = ["hop", "direction", "link_type", "tier", "via", "type", "handle", "label",
           "record_date", "system", "facts", "known"]
# the catalog's field kinds a record's ``facts`` show (record_types.fields[].kind), beside
# the fields the catalog lists as category-like (record_types.kinds)
FACT_KINDS = frozenset(gv.NUMERIC_KINDS) | frozenset(gv.DATE_KINDS) | {"enum", "boolean"}
VIEWS = ["records", "record_links"]


def _ids(ids: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(_literal(i) for i in ids) + "]::text[]"


def _like(text: str) -> str:
    """``text`` as an ILIKE pattern that matches only itself (no wildcard)."""
    return _literal(text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def _batches(ids: Sequence[str]) -> list[list[str]]:
    return [list(ids[i:i + BATCH]) for i in range(0, len(ids), BATCH)]


class _Walk:
    """One chain read: the reads go through ``tool`` (the plx.sql tool: its check, ceiling
    and door) as ``actor``; ``reads`` collects their results for the answer."""

    def __init__(self, tool: Any, conn: Any, scope: Any, actor: Any) -> None:
        self.tool, self.conn, self.scope, self.actor = tool, conn, scope, actor
        self.reads: list[dict[str, Any]] = []

    def read(self, sql: str) -> list[dict[str, Any]]:
        got = self.tool._read(self.conn, self.scope, self.tool._statement(self.conn, sql),
                              self.actor)
        self.reads.append(got)
        cols = got["cols"]
        return [dict(zip(cols, r)) for r in got["rows"]]

    # -- the start -----------------------------------------------------------------------

    def starts(self, key: str, type_: str | None) -> list[dict[str, Any]]:
        """The records the handle or number names (``records``), else the records the links
        name by that handle (a record the store holds no row of)."""
        kind = f" AND type = {_literal(type_)}" if type_ else ""
        found = self.read(
            "SELECT record_id, type, handle, label, record_date, system, fields FROM records "
            f"WHERE (handle = {_literal(key)} OR label ILIKE {_like(key)}){kind}")
        if found:
            return found
        out: dict[str, dict[str, Any]] = {}
        for side in ("from", "to"):
            for r in self.read(
                    f"SELECT {side}_id AS record_id, {side}_type AS type, {side}_handle AS "
                    f"handle FROM record_links WHERE {side}_handle = {_literal(key)}"):
                out.setdefault(r["record_id"], {**r, "label": None, "record_date": None,
                                                "system": None, "fields": None})
        return [r for r in out.values() if not type_ or r["type"] == type_]

    # -- one hop -------------------------------------------------------------------------

    def edges(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for chunk in _batches(ids):
            literal = _ids(chunk)
            out += self.read(
                "SELECT handle, link_type, tier, from_id, from_type, from_handle, to_id, "
                f"to_type, to_handle FROM record_links WHERE from_id = ANY({literal}) "
                "UNION ALL SELECT handle, link_type, tier, from_id, from_type, from_handle, "
                f"to_id, to_type, to_handle FROM record_links WHERE to_id = ANY({literal})")
        return out

    def details(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for chunk in _batches(ids):
            for r in self.read(
                    "SELECT record_id, type, handle, label, record_date, system, fields "
                    f"FROM records WHERE record_id = ANY({_ids(chunk)})"):
                out[r["record_id"]] = r
        return out

    def catalog(self, types: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not types:
            return {}
        rows = self.read("SELECT type, fields, kinds FROM record_types WHERE type = ANY("
                         f"{_ids(sorted(types))})")
        return {r["type"]: r for r in rows}


def _obj(value: Any) -> Any:
    """A jsonb value as ``plx.sql`` reads it comes as text (``_fetch`` casts it): its JSON."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def facts(record: Mapping[str, Any], entry: Mapping[str, Any] | None) -> dict[str, Any]:
    """The fields of ``record`` its type's catalog entry declares as facts (``FACT_KINDS``
    fields and the category-like ones), with their currency or unit where it holds one."""
    fields = _obj(record.get("fields"))
    if not isinstance(fields, Mapping) or entry is None:
        return {}
    declared = {f["name"] for f in _obj(entry.get("fields")) or ()
                if isinstance(f, Mapping) and f.get("kind") in FACT_KINDS}
    kinds = _obj(entry.get("kinds"))
    declared |= set(kinds) if isinstance(kinds, Mapping) else set()
    out: dict[str, Any] = {}
    for name in sorted(declared):
        value = fields.get(name)
        if value is None or value == "":
            continue
        out[name] = value
        for suffix in ("_currency", "_unit"):
            if fields.get(name + suffix) not in (None, ""):
                out[name + suffix] = fields[name + suffix]
    return out


def _row(hop: int, direction: str, edge: Mapping[str, Any] | None, via: str | None,
         record: Mapping[str, Any], known: bool, entry: Mapping[str, Any] | None
         ) -> list[Any]:
    return [hop, direction, edge["link_type"] if edge else None,
            edge["tier"] if edge else None, via, record.get("type"), record.get("handle"),
            record.get("label"), record.get("record_date"), record.get("system"),
            facts(record, entry) if known else {}, known]


def walk(tool: Any, conn: Any, scope: Any, actor: Any, args: Mapping[str, Any]
         ) -> dict[str, Any]:
    """The chain read ``args`` asks (``chain``, ``hops``, ``party``, ``type``) as ``actor``;
    the plx.sql answer shape (``tool._present``) with a ``chain`` summary."""
    key = args["chain"]
    if not isinstance(key, str) or not key.strip():
        raise ToolError("invalid_request", "chain is a record's handle or its number")
    key = key.strip()
    hops = args.get("hops", 1)
    if isinstance(hops, bool) or not isinstance(hops, int) or hops < 1:
        raise ToolError("invalid_request", "hops is a whole number, 1 or more")
    party, type_ = args.get("party"), args.get("type")
    for name, value in (("party", party), ("type", type_)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ToolError("invalid_request", f"{name} is a name as text")
    started = time.monotonic()
    w = _Walk(tool, conn, scope, actor)
    starts = w.starts(key, type_.strip() if type_ else None)
    note = None
    if not starts:
        raise ToolError("not_found", f"no record has the handle or number {key!r}"
                        + (f" of type {type_}" if type_ else ""),
                        "find it with plx.search (sources records), or send its handle")
    edges: list[dict[str, Any]] = []
    if len(starts) > 1:
        edges = w.edges([r["record_id"] for r in starts])
        if not _joined(starts, edges) and party:
            starts, note = _by_party(w, starts, party.strip(), edges)
        if len(starts) > 1 and _joined(starts, edges):
            note = ((note + "; ") if note else "") + (
                f"{len(starts)} records fit {key!r} and they are linked to one another "
                "(solid links), so all are walked as one chain")
    if len(starts) > 1 and not _joined(starts, edges) and _joined(starts, edges, True):
        note = ((note + "; ") if note else "") + (
            f"{len(starts)} records fit {key!r} and only a guess-tier link joins them: each "
            "is walked by its own solid links and the guess links are listed, not followed")
        guess_joined = True
    else:
        guess_joined = False
    ambiguous = len(starts) > 1 and not guess_joined and not _joined(starts, edges)
    rows: list[list[Any]] = []
    reached: dict[str, int] = {}
    per_hop: list[dict[str, Any]] = []
    guesses = 0
    if ambiguous:
        entry = w.catalog(sorted({r["type"] for r in starts}))
        for r in sorted(starts, key=lambda r: (r["type"], str(r.get("handle")))):
            rows.append(_row(0, "candidate", None, None, r, True, entry.get(r["type"])))
        note = ((note + "; ") if note else "") + (
            f"{len(starts)} records fit {key!r} and are not linked to one another: pick "
            "one by its handle, by type, or by party (a name a linked record's label holds); "
            "nothing was walked")
        for r in starts:
            reached[r["type"]] = reached.get(r["type"], 0) + 1
    else:
        visited = {r["record_id"] for r in starts}
        frontier = [r["record_id"] for r in starts]
        found: dict[str, dict[str, Any]] = {r["record_id"]: r for r in starts}
        seen_links: set[str] = set()
        pending: list[tuple[int, str, Mapping[str, Any], str, str]] = []
        for hop in range(1, hops + 1):
            if not frontier:
                break
            inside = set(frontier)
            fresh: dict[str, str] = {}
            count = 0
            for e in w.edges(frontier):
                if e["handle"] in seen_links:
                    continue
                seen_links.add(e["handle"])
                if e["from_id"] in inside:
                    direction, near, far = "out", e["from_handle"], e["to_id"]
                else:
                    direction, near, far = "in", e["to_handle"], e["from_id"]
                pending.append((hop, direction, e, near, far))
                count += 1
                if e["tier"] == GUESS_TIER:
                    guesses += 1
                elif far not in visited:
                    fresh[far] = far
            per_hop.append({"hop": hop, "links": count, "new_records": len(fresh)})
            visited |= set(fresh)
            frontier = list(fresh)
        found.update(w.details(sorted({p[4] for p in pending} - set(found))))
        entry = w.catalog(sorted({r["type"] for r in found.values()}
                                 | {p[2]["to_type" if p[1] == "out" else "from_type"]
                                    for p in pending}))
        for r in sorted(starts, key=lambda r: (r["type"], str(r.get("handle")))):
            rows.append(_row(0, "start", None, None, r, True, entry.get(r["type"])))
            reached[r["type"]] = reached.get(r["type"], 0) + 1
        for hop, direction, e, near, far in sorted(
                pending, key=lambda p: (p[0], p[1], p[2]["link_type"],
                                        p[2]["to_type" if p[1] == "out" else "from_type"],
                                        str(p[2]["to_handle" if p[1] == "out"
                                                 else "from_handle"]))):
            if far in found:
                record, known = found[far], True
            else:   # the link names a record this reader's ``records`` shows no row of
                side = "to" if direction == "out" else "from"
                record = {"type": e[f"{side}_type"], "handle": e[f"{side}_handle"]}
                known = False
            rows.append(_row(hop, direction, e, near, record, known, entry.get(record["type"])))
            reached[record["type"]] = reached.get(record["type"], 0) + 1
    got = _got(w, rows)
    out = tool._present(f"chain {key}", got, actor, started)
    out["chain"] = {
        "start": key, "hops": hops, "records": len(rows) - (0 if ambiguous else len(starts)),
        "reached": dict(sorted(reached.items())), "per_hop": per_hop,
        "tiers_walked": [t for t in gv.LINK_TIERS if t != GUESS_TIER],
        "guess_links_listed_not_walked": guesses,
        "ambiguous": ambiguous,
        "columns": "hop 0 is the record itself; direction out = the record at hop's near end "
                   "holds the link, in = the far record holds it; facts = the type's "
                   "declared amounts, quantities, dates and states; known false = a record "
                   "this reader's records view shows no row of (the link names it)"}
    if note:
        out["note"] = (out.get("note", "") + " " + note).strip()
    return out


def _joined(starts: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]],
            guesses: bool = False) -> bool:
    """Whether the records are one connected set by the solid links between them (a link of a
    guess tier does not join): the same thing held by two systems, a record and its
    reference: one chain, not several things sharing a number."""
    ids = {r["record_id"] for r in starts}
    reach = {next(iter(ids))} if ids else set()
    grew = True
    while grew:
        grew = False
        for e in edges:
            if e["tier"] == GUESS_TIER and not guesses:
                continue
            a, b = e["from_id"], e["to_id"]
            if a in ids and b in ids and (a in reach) != (b in reach):
                reach |= {a, b}
                grew = True
    return reach == ids


def _by_party(w: _Walk, starts: list[dict[str, Any]], party: str,
              edges: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """The candidates linked to a record whose label holds ``party`` (``edges``: their
    links, already read), and what was done."""
    word = party.lower()
    near: dict[str, set[str]] = {r["record_id"]: set() for r in starts}
    for e in edges:
        for mine, theirs in ((e["from_id"], e["to_id"]), (e["to_id"], e["from_id"])):
            if mine in near:
                near[mine].add(theirs)
    labels = w.details(sorted(set().union(*near.values())) if near else [])
    kept = [r for r in starts if any(word in str(labels.get(n, {}).get("label") or "").lower()
                                     for n in near[r["record_id"]])]
    return (kept if kept else starts,
            f"party {party!r} " + (f"kept {len(kept)} of {len(starts)} candidates"
                                   if kept else "matched no linked record's label: all "
                                   f"{len(starts)} candidates are shown"))


def _got(w: _Walk, rows: list[list[Any]]) -> dict[str, Any]:
    """The walk's rows as ``tool._read`` returns a read: one table, a profile over the whole
    of it, and what the reads' doors withheld (merged: per view and column the most any read
    had withheld)."""
    cats = [result_profile.OTHER] * len(COLUMNS)
    profile = result_profile.from_rows(COLUMNS, rows, cats, [(None, None)] * len(COLUMNS))
    withheld: dict[str, dict[str, int]] = {}
    for read in w.reads:
        for view, cols in read["withheld"].items():
            for col, n in cols.items():
                withheld.setdefault(view, {})
                withheld[view][col] = max(withheld[view].get(col, 0), n)
    sources: list[Any] = []
    for read in w.reads:
        sources += [s for s in read.get("sources") or () if s not in sources]
    built = next((r["built"] for r in w.reads if r.get("built") is not None), None)
    return {"cols": COLUMNS, "rows": rows, "stopped": False, "built": built,
            "profile": profile, "sources": sources, "views": VIEWS,
            "read_at": w.reads[-1]["read_at"] if w.reads else None, "withheld": withheld}
