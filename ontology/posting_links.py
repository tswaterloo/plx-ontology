"""Ledger entries linked to the records whose posting their document number names
(``<From>.posted_by_<to type>``, e.g. ``ItemLedgerEntry.posted_by_batch``).

One ``post_project`` step (``operating/build_steps.py`` ``trace_postings``, link id
prefix ``PREFIX``), self-contained: it reads the current store only, removes the rows it
wrote before and writes them again, so it runs in a full build or alone on a store in
place (the step migration).

* Inputs: the current identifiers of every kind that declares structured document forms
  (``ontology/schema/identifiers.json`` ``forms`` with a ``pattern`` and ``reads``, read by
  ``ontology.identifiers.document_form``: ``PLX-AO-AO-26197B`` reads the AO name
  ``AO-26197B``, ``PLX-BAT-11479`` the FileMaker batch id 11479); the identifiers of the
  records they name; for a FileMaker record id (``fm_id``), the native key of the named
  type's FileMaker bindings (lineage ``fm_bronze``, key ``id``); the confirming
  ``same_record`` links of the named records; a person's rejections (``contradicted``
  links of a pair). The schema declares which pairs are linked: a link type
  ``<From>.posted_by_<snake(To)>`` with a ``mention`` binding.
* A link is **corroborated**: the document number is written in exactly one declared form,
  the part it reads is exact, and it names one record of the declared type: one object, or
  objects each two of which a confirming ``same_record`` link joins (a FileMaker batch and
  its Portal copy). A part naming several unrelated records, or none, links nothing
  (counted). A form whose read names no record type (a day) never links.
* Every link keeps its keys (``ontology.link_keys``): the ``document_form`` feature cites
  the document number's identifier row, the ``exact_identifier`` feature the named
  record's (its identifier row, or for a FileMaker id its native key on its current
  version), each with the source's own text (``raw``); the link carries the identifier
  rows' markings with its ends'.

Stdlib only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping, Sequence

from ontology import identifiers as ids
from ontology import mentions

PREFIX = "postdoc:"
FIRM = "corroborated"
BINDING_KIND = "mention"
CONFIRMING = ("verified", "reviewed", "corroborated")
FM_LINEAGE = "fm_bronze"
FM_ID = "fm_id"
BATCH = 2000
CODE_PATH = "ontology/posting_links.py"


def _end(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()[:32]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _q(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    return mentions._query(conn, sql, list(params)).fetchall()


def link_type_of(from_type: str, to_type: str) -> str:
    from ontology.schema import _snake_case
    return f"{from_type}.posted_by_{_snake_case(to_type)}"


def declared(links: Mapping[str, Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    """(from type, to type) -> link type, for the link types this step writes."""
    out = {}
    for lid, link in links.items():
        if lid == link_type_of(link.get("from", ""), link.get("to", "")) and any(
                b.get("kind") == BINDING_KIND for b in link.get("bindings", ())):
            out[(link["from"], link["to"])] = lid
    return out


def form_kinds() -> list[str]:
    """Identifier kinds with a declared form that names a record type."""
    forms = ids._compiled().document_forms
    return sorted(k for k, entries in forms.items()
                  if any(m.get("object_type") for f, _p in entries
                         for m in f.get("reads", {}).values()))


def fm_bindings(bindings: Mapping[str, Any]) -> dict[str, list[str]]:
    """object type -> its FileMaker bindings keyed by the FM record id."""
    out: dict[str, list[str]] = defaultdict(list)
    for bid, (tid, b) in sorted(bindings.items()):
        lineage = b.get("lineage") or {}
        default = lineage.get("default") if isinstance(lineage, Mapping) else lineage
        if default == FM_LINEAGE and list(b.get("key") or ()) == ["id"]:
            out[tid].append(bid)
    return out


class Resolver:
    """The records of a type an identifier names, each with the key it holds the value in
    (citation, markings): its first identifier row by (handle, field), else its native
    key on its current version."""

    def __init__(self, conn: Any, fm: Mapping[str, Sequence[str]]) -> None:
        self.conn, self.fm, self.cache = conn, fm, {}

    def __call__(self, kind: str, object_type: str, value: str) -> list[str]:
        return sorted(self.keys(kind, object_type, value))

    def keys(self, kind: str, object_type: str, value: str
             ) -> dict[str, tuple[dict[str, Any], str]]:
        key = (kind, object_type, value)
        if key not in self.cache:
            found: dict[str, tuple[dict[str, Any], str]] = {}
            for r in sorted(_q(self.conn, "SELECT object_id, l1_handle, field_path, raw, "
                                          "markings FROM identifiers WHERE identifier_kind = ? "
                                          "AND normalized = ? AND superseded_at IS NULL AND "
                                          "object_id >= ? AND object_id < ?",
                               [kind, value, object_type + ":", object_type + ";"]),
                            key=lambda r: (r[0], r[1], r[2])):
                found.setdefault(r[0], ({"l1_handle": r[1], "field_path": r[2], "raw": r[3]},
                                        r[4]))
            if kind == FM_ID and value.lstrip("-").isdigit():
                native = _canon({"id": int(value)})
                for binding in self.fm.get(object_type, ()):
                    for r in _q(self.conn, "SELECT o.object_id, v.l1_handle FROM objects o JOIN "
                                           "object_versions v ON v.object_id = o.object_id AND "
                                           "v.superseded_at IS NULL WHERE o.object_type = ? AND "
                                           "o.binding_id = ? AND o.native_key = ? "
                                           "ORDER BY v.l1_handle", [object_type, binding, native]):
                        found.setdefault(r[0], ({"l1_handle": r[1], "field_path": "native_key.id",
                                                 "raw": value}, "[]"))
            self.cache[key] = found
        return self.cache[key]


def one_record(conn: Any, oids: Sequence[str]) -> bool:
    """Whether every two of ``oids`` are joined by a confirming same_record link."""
    oids = sorted(set(oids))
    if len(oids) < 2:
        return bool(oids)
    link = oids[0].split(":", 1)[0] + ".same_record_as"
    for i, a in enumerate(oids):
        joined = {r[0] for r in _q(
            conn, "SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? AND "
                  "tier IN ('verified', 'reviewed', 'corroborated') AND superseded_at IS NULL "
                  "UNION SELECT from_object FROM link_rows WHERE to_object = ? AND link_type = ? "
                  "AND tier IN ('verified', 'reviewed', 'corroborated') AND superseded_at IS NULL",
            [a, link, a, link])}
        if not set(oids[i + 1:]) <= joined:
            return False
    return True


def _versions(conn: Any, oids: Iterable[str]) -> dict[str, Any]:
    oids = sorted(set(oids))
    out: dict[str, Any] = {}
    for i in range(0, len(oids), 500):
        part = oids[i:i + 500]
        for r in _q(conn, "SELECT object_id, l1_handle, markings, recorded_at FROM "
                          "object_versions WHERE superseded_at IS NULL AND object_id IN "
                          f"({','.join('?' * len(part))})", part):
            out[r["object_id"]] = r
    return out


def _rejected(conn: Any, oids: Iterable[str]) -> set[tuple[str, str, str]]:
    out = set()
    for oid in sorted(set(oids)):
        out |= {(oid, r[0], r[1]) for r in _q(
            conn, "SELECT to_object, link_type FROM link_rows WHERE from_object = ? AND "
                  "tier = 'contradicted'", [oid])}
    return out


def posting_links(conn: Any, links: Mapping[str, Mapping[str, Any]],
                  bindings: Mapping[str, Any]) -> tuple[list[dict[str, Any]], Counter]:
    """The corroborated rows (module docstring) and the outcome counts."""
    counts: Counter[str] = Counter()
    pairs = declared(links)
    if not pairs:
        return [], counts
    from_types = sorted({f for f, _t in pairs})
    resolve = Resolver(conn, fm_bindings(bindings))
    found: list[tuple[Any, str, str, Any]] = []
    for kind in form_kinds():
        for from_type in from_types:
            for row in _q(conn, "SELECT object_id, normalized, raw, l1_handle, field_path, "
                                "markings, recorded_at FROM identifiers WHERE identifier_kind = ? AND "
                                "superseded_at IS NULL AND object_id >= ? AND object_id < ?",
                          [kind, from_type + ":", from_type + ";"]):
                form = ids.document_form(kind, row["normalized"])
                if form is None:
                    continue
                for part, named_kind, to_type, value in form.reads:
                    if not named_kind or not to_type:
                        counts["names_no_record"] += 1
                        continue
                    link_type = pairs.get((from_type, to_type))
                    if link_type is None:
                        continue
                    targets = resolve(named_kind, to_type, value)
                    if not targets:
                        counts["no_record"] += 1
                    elif not one_record(conn, targets):
                        counts["several_records"] += 1
                    else:
                        counts["firm"] += 1
                        named = resolve.keys(named_kind, to_type, value)
                        found += [(row, link_type, t, (form, part, named_kind, value, named[t]))
                                  for t in targets]
    versions = _versions(conn, [t for _r, _l, t, _f in found])
    rejected = _rejected(conn, {r["object_id"] for r, *_x in found})
    out: dict[str, dict[str, Any]] = {}
    for row, link_type, target, (form, part, named_kind, value, (cited, marks)) in found:
        version = versions.get(target)
        if version is None:
            continue
        if (row["object_id"], target, link_type) in rejected:
            counts["rejected"] += 1
            continue
        link_id = PREFIX + _digest(row["object_id"], link_type, target)
        if link_id in out:
            continue
        cite = {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
                "raw": row["raw"]}
        reason = [{"feature": "document_form", "agrees": True, "identifier_kind": form.kind,
                   "form": form.form, "normalized": row["normalized"], "citations": [cite]},
                  {"feature": "exact_identifier", "agrees": True, "part": part,
                   "identifier_kind": named_kind, "normalized": value, "citations": [cited]},
                  {"feature": "unique_record", "agrees": True}]
        out[link_id] = {
            "link_id": link_id, "link_type": link_type, "from_object": row["object_id"],
            "to_object": target, "tier": FIRM, "binding_kind": BINDING_KIND,
            "link_contract": None, "reason": _canon(reason), "why_not": "[]",
            "evidence_handles": _canon(list(dict.fromkeys([row["l1_handle"],
                                                           version["l1_handle"],
                                                           cited["l1_handle"]]))),
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": mentions._marks(row["markings"], version["markings"], marks),
            "recorded_at": max(row["recorded_at"], version["recorded_at"]),
            "superseded_at": None}
    counts["links"] = len(out)
    return [out[k] for k in sorted(out)], counts


def trace_postings_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``trace_postings`` step (module docstring)."""
    rows, counts = posting_links(builder.connection, getattr(projector, "links", {}) or {},
                                 getattr(projector, "bindings", {}) or {})
    builder.connection.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                               (PREFIX, _end(PREFIX)))
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    return {"counts": dict(counts), "link_tiers": {FIRM: len(rows)} if rows else {}}


__all__ = ["PREFIX", "declared", "form_kinds", "link_type_of", "one_record", "posting_links",
           "trace_postings_step"]
