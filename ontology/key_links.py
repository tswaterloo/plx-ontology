"""Records of two bindings joined on an exact identifier both hold (key joins).

One ``post_project`` step (``operating/build_steps.py`` ``key_joins``, link id prefix
``PREFIX``), self-contained: it reads the current store only, removes the rows it wrote
before and writes them again, so it runs in a full build or alone on a store in place (the
step migration).

The schema declares every join: a link type with a ``mention`` binding carrying a ``key``
(``from_binding`` -> ``to_binding``)::

    "key": {"from": "bc_document_no",       # identifier kind the from record holds
            "to": "bc_document_no",         # the same kind on the to record, or
                                            # "system_id": the BC record's system id key
            "to_field": "row.number",       # optional: only the value of this field (a
                                            # document's own number, not one it applies to)
            "company": "same_bc_company",   # optional: both ends in one BC company
            "party": {"from": [...], "to": [...], "twin": "Customer.same_record_as"},
            "targets": "one"}               # or "same_party"

* Inputs: the current identifiers of the declared kinds of both bindings (the general
  resolver's normalized values); for ``system_id``, the to binding's native keys; for a
  ``party`` key, the links each path follows (``~`` reads a link backwards) and the
  confirming twin links of the parties they reach, plus the BC code twins the
  ``twins_parties`` rules corroborate (``ontology.identity_scoring``); the confirming
  ``same_record`` links of the to records; a person's rejections (``contradicted`` rows).
* A value joins only when it holds at least ``min_digits`` digits (default 3): a word
  such as ``RESTOCK`` is no identifier. Values held by more than ``MAX_HOLDERS`` records of
  the to binding are skipped (a placeholder).
* ``targets: one``: the link is **corroborated** when the value names one record of the to
  binding (in the from record's BC company for ``company``; twins joined by a confirming
  ``same_record`` link count as one) and, with a ``party`` key, the parties agree; two or
  three candidates are **leads** (``why_not`` ``ambiguous_target``).
* A ``confirm`` binding (no ``key``) raises a stored lead of its link type: a lead from a
  record of ``from_binding`` to one of ``to_binding`` is **corroborated** when the records
  the from record reaches along ``confirm.path`` by confirming links only (``~`` reads a
  link backwards), closed under the confirming ``confirm.twin`` links, hold the lead's
  target. A path reaching only other records leaves the lead as it is (``path_disagrees``);
  so does no path (``no_path``). An ETL's guessed key confirmed by the source's own record
  is the use: a Portal MO line's product, picked by a number shared across product
  families, against the FileMaker component line of the same assembly order.
* ``targets: same_party``: a value repeats across a party's records of one thing (a
  customer PO split over several orders), so each candidate whose party agrees is
  **corroborated**, and a candidate whose party is unknown is a **lead**; a candidate of
  another party is never linked (PO numbers repeat across customers).

Stdlib only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from ontology import mentions
from ontology.posting_links import _canon, _digest, _end, _q, _rejected, _versions, one_record

PREFIX = "keyjoin:"
FIRM = "corroborated"
LEAD = "lead"
BINDING_KIND = "mention"
CONFIRMING = ("verified", "reviewed", "corroborated", "stored_by_text_join")
MAX_HOLDERS = 25
MAX_LEADS = 3
MIN_DIGITS = 3
BATCH = 2000
SYSTEM_ID = "system_id"
CODE_PATH = "ontology/key_links.py"
_DIGIT = re.compile(r"\d")


def declared(links: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(link type, binding) of every key join the schema declares."""
    return [(lid, dict(b)) for lid in sorted(links)
            for b in links[lid].get("bindings", ())
            if b.get("kind") == BINDING_KIND and isinstance(b.get("key"), Mapping)]


def confirmations(links: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(link type, binding) of every confirming path the schema declares."""
    return [(lid, dict(b)) for lid in sorted(links)
            for b in links[lid].get("bindings", ())
            if b.get("kind") == BINDING_KIND and isinstance(b.get("confirm"), Mapping)]


def written_types(links: Mapping[str, Mapping[str, Any]]) -> set[str]:
    return {lid for lid, _b in declared(links) + confirmations(links)}


def _type_of(oid: str) -> str:
    return oid.split(":", 1)[0]


def _range(object_type: str, binding: str) -> tuple[str, str]:
    start = f"{object_type}:{binding}:"
    return start, _end(start)


def _held(conn: Any, kind: str, object_type: str, binding: str, field: str | None = None
          ) -> list[tuple[str, str]]:
    lo, hi = _range(object_type, binding)
    return [(r[0], r[1]) for r in _q(
        conn, "SELECT object_id, normalized, field_path FROM identifiers WHERE "
              "identifier_kind = ? AND superseded_at IS NULL AND object_id >= ? AND "
              "object_id < ?", [kind, lo, hi]) if field is None or r[2] == field]


def _system_ids(conn: Any, object_type: str, binding: str) -> list[tuple[str, str]]:
    out = []
    for oid, native in _q(conn, "SELECT object_id, native_key FROM objects WHERE "
                                "object_type = ? AND binding_id = ?", [object_type, binding]):
        key = json.loads(native)
        if isinstance(key, Mapping) and isinstance(key.get("id"), str):
            out.append((oid, key["id"].strip().lower()))
    return out


def _companies(conn: Any, object_type: str, binding: str) -> dict[str, str]:
    """object id -> BC company (the capture audience ``bc:<company>/<entity>``)."""
    return {r[0]: r[1].split("/", 1)[0] for r in _q(
        conn, "SELECT object_id, audience_key FROM objects WHERE object_type = ? AND "
              "binding_id = ?", [object_type, binding]) if str(r[1]).startswith("bc:")}


class Parties:
    """The party (a set of twin-joined records) a record reaches along a declared path."""

    def __init__(self, conn: Any, schema: Any) -> None:
        self.conn, self.schema = conn, schema
        self._cache: dict[tuple, frozenset[str]] = {}
        self._twins: dict[str, dict[str, set[str]]] = {}

    def _hop(self, oid: str, step: str) -> set[str]:
        back = step.startswith("~")
        link = step.lstrip("~")
        if back:
            sql = ("SELECT from_object FROM link_rows WHERE to_object = ? AND link_type = ? "
                   "AND superseded_at IS NULL AND tier <> 'contradicted'")
        else:
            sql = ("SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? "
                   "AND superseded_at IS NULL AND tier <> 'contradicted'")
        return {r[0] for r in _q(self.conn, sql, [oid, link])}

    def _twin_graph(self, twin: str) -> dict[str, set[str]]:
        if twin not in self._twins:
            graph: dict[str, set[str]] = defaultdict(set)
            for a, b in self._confirming(twin):
                graph[a].add(b)
                graph[b].add(a)
            self._twins[twin] = graph
        return self._twins[twin]

    def _confirming(self, twin: str) -> Iterable[tuple[str, str]]:
        object_type = twin.split(".", 1)[0]
        for (oid,) in _q(self.conn, "SELECT object_id FROM objects WHERE object_type = ?",
                         [object_type]):
            for r in _q(self.conn, "SELECT to_object FROM link_rows WHERE from_object = ? AND "
                                   "link_type = ? AND superseded_at IS NULL AND tier IN "
                                   f"({','.join('?' * len(CONFIRMING))})",
                        [oid, twin, *CONFIRMING]):
                yield oid, r[0]
        # the BC code twins the twins_parties rules corroborate (their step runs later);
        # judged for this link type only, so nothing outside the step's inputs is read
        from ontology.identity_scoring import PARTY_TWIN_LINKS, _PartyTwin
        from ontology.resolve import NEVER_AUTHORITY
        if twin in PARTY_TWIN_LINKS:
            previous = self.conn.row_factory
            self.conn.row_factory = sqlite3.Row
            try:
                judge = _PartyTwin(self.conn, twin, NEVER_AUTHORITY)
                pairs = []
                for lead in judge.leads():
                    _code, row = judge.judge(lead)
                    if row is not None:
                        pairs.append((row["from_object"], row["to_object"]))
            finally:
                self.conn.row_factory = previous
            yield from pairs

    def of(self, oid: str, path: Sequence[str], twin: str) -> frozenset[str]:
        key = (oid, tuple(path), twin)
        if key not in self._cache:
            ends = {oid}
            for step in path:
                ends = {n for e in ends for n in self._hop(e, step)}
            graph = self._twin_graph(twin)
            seen, todo = set(ends), list(ends)
            while todo:
                for n in graph.get(todo.pop(), ()):
                    if n not in seen:
                        seen.add(n)
                        todo.append(n)
            self._cache[key] = frozenset(seen)
        return self._cache[key]


class FirmParties(Parties):
    """Parties along confirming links only (a confirming path never follows a lead)."""

    def _hop(self, oid: str, step: str) -> set[str]:
        back = step.startswith("~")
        near, far = ("to_object", "from_object") if back else ("from_object", "to_object")
        return {r[0] for r in _q(
            self.conn, f"SELECT {far} FROM link_rows WHERE {near} = ? AND link_type = ? AND "
                       f"superseded_at IS NULL AND tier IN ({','.join('?' * len(CONFIRMING))})",
            [oid, step.lstrip("~"), *CONFIRMING])}


def _confirms(conn: Any, lid: str, link: Mapping[str, Any], b: Mapping[str, Any],
              parties: Parties, counts: Counter) -> list[tuple[str, str, str, list, list]]:
    """(from, to, tier, reason, why_not) of one declared confirming path."""
    confirm = b["confirm"]
    lo, hi = _range(link["from"], b["from_binding"])
    to_lo, to_hi = _range(link["to"], b["to_binding"])
    out = []
    for frm, to in _q(conn, "SELECT from_object, to_object FROM link_rows WHERE link_type = ? "
                            "AND tier = 'lead' AND superseded_at IS NULL AND from_object >= ? "
                            "AND from_object < ?", [lid, lo, hi]):
        if not to_lo <= to < to_hi:
            continue
        reached = parties.of(frm, confirm["path"], confirm["twin"])
        if not reached:
            counts["no_path"] += 1
        elif to in reached:
            out.append((frm, to, FIRM, [{"feature": "stored_lead", "agrees": True},
                                        {"feature": "confirming_path", "agrees": True,
                                         "via": list(confirm["path"]),
                                         "twin": confirm["twin"]}], []))
            counts["firm"] += 1
        else:
            counts["path_disagrees"] += 1
    return out


def _joins(conn: Any, lid: str, link: Mapping[str, Any], b: Mapping[str, Any],
           parties: Parties, counts: Counter) -> list[tuple[str, str, str, list, list]]:
    """(from, to, tier, reason, why_not) of one declared key join."""
    key = b["key"]
    from_type, to_type = link["from"], link["to"]
    fb, tb = b["from_binding"], b["to_binding"]
    min_digits = int(key.get("min_digits", MIN_DIGITS))
    system = key.get("to") == SYSTEM_ID
    if system:
        held_to = _system_ids(conn, to_type, tb)
        held_from = [(o, v.strip().lower()) for o, v in _held(conn, key["from"], from_type, fb,
                                                              key.get("from_field"))]
    else:
        held_to = _held(conn, key.get("to") or key["from"], to_type, tb, key.get("to_field"))
        held_from = _held(conn, key["from"], from_type, fb, key.get("from_field"))
    by_value: dict[str, list[str]] = defaultdict(list)
    for oid, value in held_to:
        by_value[value].append(oid)
    scoped = key.get("company") == "same_bc_company"
    company_from = _companies(conn, from_type, fb) if scoped else {}
    company_to = _companies(conn, to_type, tb) if scoped else {}
    party = key.get("party")
    same_party = key.get("targets") == "same_party"
    out = []
    for oid, value in held_from:
        if not system and len(_DIGIT.findall(value)) < min_digits:
            counts["not_an_identifier"] += 1
            continue
        targets = sorted({t for t in by_value.get(value, ()) if t != oid})
        if scoped:
            targets = [t for t in targets if company_to.get(t) == company_from.get(oid)
                       and company_from.get(oid)]
        if not targets:
            counts["no_record"] += 1
            continue
        if len(targets) > MAX_HOLDERS:
            counts["placeholder"] += 1
            continue
        reason = [{"feature": "exact_identifier", "agrees": True,
                   "identifier_kind": key["from"], "normalized": value,
                   "to": SYSTEM_ID if system else (key.get("to") or key["from"])}]
        if party:
            mine = parties.of(oid, party["from"], party["twin"])
            agree, unknown = [], []
            for t in targets:
                theirs = parties.of(t, party["to"], party["twin"])
                if mine and theirs and mine & theirs:
                    agree.append(t)
                elif not mine or not theirs:
                    unknown.append(t)
            counts["other_party"] += len(targets) - len(agree) - len(unknown)
            reason = reason + [{"feature": "same_party", "agrees": True,
                                "via": party["twin"]}]
            if same_party:
                out += [(oid, t, FIRM, reason, []) for t in agree]
                if len(unknown) <= MAX_LEADS:
                    out += [(oid, t, LEAD, reason[:1], [{"feature": "party_unknown",
                                                         "agrees": None}]) for t in unknown]
                counts["firm" if agree else ("lead" if unknown else "other_party_only")] += 1
                continue
            targets = agree or (unknown if len(unknown) <= MAX_LEADS else [])
            if not agree:
                out += [(oid, t, LEAD, reason[:1], [{"feature": "party_unknown",
                                                     "agrees": None}]) for t in targets]
                counts["lead" if targets else "other_party_only"] += 1
                continue
        if one_record(conn, targets):
            out += [(oid, t, FIRM, reason + [{"feature": "unique_record", "agrees": True}], [])
                    for t in targets]
            counts["firm"] += 1
        elif len(targets) <= MAX_LEADS:
            out += [(oid, t, LEAD, reason, [{"feature": "ambiguous_target", "agrees": None,
                                             "candidates": len(targets)}]) for t in targets]
            counts["lead"] += 1
        else:
            counts["several_records"] += 1
    return out


def key_links(conn: Any, links: Mapping[str, Mapping[str, Any]], schema: Any = None
              ) -> tuple[list[dict[str, Any]], Counter]:
    """The rows of every declared key join (module docstring) and the outcome counts."""
    counts: Counter[str] = Counter()
    parties = Parties(conn, schema)
    found: list[tuple[str, str, str, str, list, list]] = []
    for lid, b in declared(links):
        tally: Counter[str] = Counter()
        for frm, to, tier, reason, why_not in _joins(conn, lid, links[lid], b, parties, tally):
            found.append((lid, frm, to, tier, reason, why_not))
        counts.update({f"{lid}:{k}": v for k, v in tally.items()})
    firm_parties = FirmParties(conn, schema)
    for lid, b in confirmations(links):
        tally = Counter()
        for frm, to, tier, reason, why_not in _confirms(conn, lid, links[lid], b, firm_parties,
                                                        tally):
            found.append((lid, frm, to, tier, reason, why_not))
        counts.update({f"{lid}:confirm:{k}": v for k, v in tally.items()})
    oids = {f for _l, f, *_x in found} | {t for _l, _f, t, *_x in found}
    versions = _versions(conn, oids)
    rejected = _rejected(conn, {f for _l, f, *_x in found})
    out: dict[str, dict[str, Any]] = {}
    for lid, frm, to, tier, reason, why_not in found:
        a, z = versions.get(frm), versions.get(to)
        if a is None or z is None:
            continue
        if (frm, to, lid) in rejected:
            counts["rejected"] += 1
            continue
        link_id = PREFIX + _digest(frm, lid, to)
        if link_id in out and (out[link_id]["tier"] == FIRM or tier != FIRM):
            continue
        out[link_id] = {
            "link_id": link_id, "link_type": lid, "from_object": frm, "to_object": to,
            "tier": tier, "binding_kind": BINDING_KIND, "link_contract": None,
            "reason": _canon(reason), "why_not": _canon(why_not),
            "evidence_handles": _canon(list(dict.fromkeys([a["l1_handle"], z["l1_handle"]]))),
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": mentions._marks(a["markings"], z["markings"]),
            "recorded_at": max(a["recorded_at"], z["recorded_at"]), "superseded_at": None}
    counts["links"] = len(out)
    return [out[k] for k in sorted(out)], counts


def key_joins_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``key_joins`` step (module docstring)."""
    rows, counts = key_links(builder.connection, getattr(projector, "links", {}) or {},
                             getattr(projector, "schema", None))
    builder.connection.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                               (PREFIX, _end(PREFIX)))
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    tiers = Counter(r["tier"] for r in rows)
    return {"counts": dict(counts), "link_tiers": dict(tiers)}


__all__ = ["PREFIX", "FirmParties", "Parties", "confirmations", "declared", "key_joins_step", "key_links", "written_types"]
