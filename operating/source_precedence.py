"""Measured system-of-record precedence: which live copy of a record to trust when the
copies disagree, chosen by how often each source agreed with what really happened.

FileMaker, the Portal, Sage and BC hold copies of the same orders, purchase orders and
production records, and the copies disagree (quantities, dates, amounts). A fixed order
("the Portal first") is wrong wherever that source is the stale one, so the order is
measured instead, per record type x field:

* **Evidence** (``ontology/schema/precedence_evidence.json``, data, never code): what each
  type x field is checked against: shipments for sales quantities, posted invoices for
  amounts, receipts for purchase order quantities and dates, completion records for
  production. A path of link hops from the record to the evidence records and the
  evidence field, combined per evidence source (sum, min, max).
* **Measure** (``measure``, read-only, on history): the same-record copies of each record
  form one cluster; each copy's value (a line field summed per record with ``rollup``) is
  compared with the evidence the cluster reaches, per evidence source (a BC invoice and a
  Sage invoice of one order are two readings, never added). A value read from an evidence
  record itself is not compared. Counts per source, on every record with evidence and on
  the *disputed* ones (copies holding different values), go to the measured table, a data
  file re-measured on a schedule (``deploy/systemd/plx-precedence-measure.timer``) and
  read through ``PLX_PRECEDENCE_TABLE`` (while that is missing, the committed
  ``ontology/schema/measured_precedence.json``).
* **Decide** (``decide``, at read time, over whatever the store holds): for a disagreement
  on a measured type x field, the source with the highest agreement wins (the disputed
  share when every source in the disagreement has ``MIN_COMPARED`` disputed comparisons,
  else the share over all records with evidence, else no decision). Shares within
  ``TIE_POINTS`` are a tie, which the most recently updated copy wins. Never a silent
  pick: ``operating.answer_conflicts.resolve`` shows every value with its source and
  states the shares and the evidence that chose the winner. A source the table does not
  hold, a reading without a lineage, an unreadable or foreign table: no decision here
  (the declared precedence and the other rules apply), never a guess.

A source is a lineage family (``SOURCES``): FileMaker, the Portal's own tables, the
Portal's copies from FileMaker or Sage, Sage, BC (every company). The table holds counts
per source only, never a value, a record or an id; a reader sees shares rounded to whole
percent, never counts.

Run (read-only on the store; writes only ``--out``)::

    python3 -m operating.source_precedence --store STORE.sqlite --out TABLE.json
    python3 -m operating.source_precedence --pg-dsn "dbname=plx" --out TABLE.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import datetime as dt
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Sequence

SPEC_FILE = Path(__file__).resolve().parents[1] / "ontology" / "schema" / \
    "precedence_evidence.json"
TABLE_FILE = SPEC_FILE.with_name("measured_precedence.json")
TABLE_ENV = "PLX_PRECEDENCE_TABLE"
SPEC_SCHEMA = "plx-precedence-evidence/v1"
TABLE_SCHEMA = "plx-measured-precedence/v1"
MIN_COMPARED = 20
TIE_POINTS = Decimal("0.02")
CHUNK = 500
AGGREGATES = ("sum", "min", "max")
# lineage family -> (source key, words)
SOURCES = {"fm_bronze": ("filemaker", "FileMaker"),
           "gold_etl_from_fm": ("portal_from_filemaker", "the Portal's copy from FileMaker"),
           "gold_native_staging": ("portal", "the Portal"),
           "silver_view": ("portal_silver", "the Portal's silver view"),
           "sage_bronze": ("sage", "Sage"),
           "gold_etl_from_sage": ("portal_from_sage", "the Portal's copy from Sage")}
SOURCE_WORDS = {key: words for key, words in SOURCES.values()} | {"bc": "BC"}
_RULE_KEYS = {"subject", "type", "field", "rollup", "evidence_label", "path",
              "evidence_field", "aggregate", "tolerance_days"}


class SpecInvalid(ValueError):
    pass


def source_of(lineage: Any) -> str | None:
    """The source key of a value's lineage; None for anything not a system-of-record copy
    (mail, documents, Mission Control)."""
    if not isinstance(lineage, str):
        return None
    if lineage.startswith("bc_"):
        return "bc"
    return SOURCES.get(lineage, (None,))[0]


# ----------------------------------------------------------------------------- spec

def _hop(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {"links", "dir"} or \
            raw["dir"] not in ("in", "out") or not raw["links"] or \
            not all(isinstance(x, str) and "." in x for x in raw["links"]):
        raise SpecInvalid(f"hop {raw!r}")
    return {"links": list(raw["links"]), "dir": raw["dir"]}


def load_spec(path: Path | str | None = None) -> list[dict[str, Any]]:
    """The evidence rules; anything unknown refuses the whole file."""
    raw = json.loads(Path(path or SPEC_FILE).read_text(encoding="utf-8"))
    if raw.get("schema") != SPEC_SCHEMA:
        raise SpecInvalid("schema")
    out, seen = [], set()
    for rule in raw.get("rules") or ():
        if not isinstance(rule, Mapping) or set(rule) - _RULE_KEYS or \
                not {"subject", "type", "field", "path", "evidence_field",
                     "aggregate"} <= set(rule):
            raise SpecInvalid(f"rule {rule!r}"[:200])
        if rule["aggregate"] not in AGGREGATES or rule["subject"] in seen or \
                rule["subject"].count(".") != 1:
            raise SpecInvalid(f"rule {rule['subject']}")
        tolerance = rule.get("tolerance_days", 0)
        if not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0:
            raise SpecInvalid(f"tolerance {rule['subject']}")
        seen.add(rule["subject"])
        out.append({**rule, "path": [_hop(h) for h in rule["path"]],
                    "rollup": _hop(rule["rollup"]) if "rollup" in rule else None,
                    "tolerance_days": tolerance,
                    "evidence_label": str(rule.get("evidence_label") or "the evidence")})
    if not out:
        raise SpecInvalid("no rules")
    return out


# --------------------------------------------------------------------------- values

def _number(text: Any) -> Decimal | None:
    try:
        n = Decimal(str(text).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return n if n.is_finite() else None


def _day(text: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def agrees(value: Any, evidence: Any, *, tolerance_days: int = 0,
           quantity: bool = False) -> bool:
    """Whether a value agrees with an evidence value: dates within the tolerance, numbers
    as Decimal (equal, or one figure by ``answer_conflicts.same_quantity``), else text."""
    from operating.answer_conflicts import same_quantity, value_key
    a, b = _day(value), _day(evidence)
    if a is not None and b is not None and len(str(value)) >= 10 and len(str(evidence)) >= 10:
        return abs((a - b).days) <= tolerance_days
    x, y = _number(value), _number(evidence)
    if x is not None and y is not None:
        return x == y or same_quantity([str(value), str(evidence)], quantity=quantity) \
            is not None
    return value_key(value) == value_key(evidence)


def _combine(values: Sequence[str], how: str) -> str | None:
    days = [_day(v) for v in values]
    if values and all(d is not None and len(v) >= 10 for d, v in zip(days, values)):
        if how == "sum":
            return None                                     # dates do not add
        pick = min(days) if how == "min" else max(days)     # type: ignore[type-var]
        return pick.isoformat()                             # type: ignore[union-attr]
    numbers = [_number(v) for v in values]
    if not numbers or any(n is None for n in numbers):
        return None
    if how == "sum":
        return str(sum(numbers, Decimal(0)))                # type: ignore[arg-type]
    return str(min(numbers) if how == "min" else max(numbers))  # type: ignore[type-var]


# -------------------------------------------------------------------------- measure

def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), CHUNK):
        yield items[start:start + CHUNK]


def _hop_rows(conn: Any, ids: Sequence[str], hop: Mapping[str, Any]
              ) -> list[tuple[str, str]]:
    """(from this end, the other end) of the current links of the hop at ``ids``."""
    here, there = ("to_object", "from_object") if hop["dir"] == "in" else \
        ("from_object", "to_object")
    out = []
    for part in _chunks(sorted(set(ids))):
        out += [(a, b) for a, b in conn.execute(
            f"SELECT {here}, {there} FROM link_rows WHERE {here} IN "
            "(SELECT value FROM json_each(?)) AND link_type IN (SELECT value FROM "
            "json_each(?)) AND superseded_at IS NULL AND tier <> 'contradicted'",
            (json.dumps(list(part)), json.dumps(hop["links"])))]
    return out


def _values(conn: Any, ids: Sequence[str], prop: str
            ) -> dict[str, list[tuple[str, str, str, str]]]:
    """object -> [(value, lineage, l1 handle, value type)] of its current values."""
    out: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for part in _chunks(sorted(set(ids))):
        for oid, value, lineage, handle, vtype in conn.execute(
                "SELECT object_id, value, lineage, l1_handle, value_type FROM object_values "
                "WHERE object_id IN (SELECT value FROM json_each(?)) AND property = ? "
                "AND superseded_at IS NULL AND value IS NOT NULL",
                (json.dumps(list(part)), prop)):
            out[oid].append((str(value), str(lineage), str(handle), str(vtype)))
    return out


def _clusters(conn: Any, ids: Sequence[str], type_id: str) -> dict[str, str]:
    """object -> its cluster's root: same-record copies of one type (never a lead or a
    contradicted link)."""
    parent = {i: i for i in ids}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    prefix = type_id + ":"
    for part in _chunks(list(ids)):
        for a, b in conn.execute(
                "SELECT from_object, to_object FROM link_rows WHERE from_object IN "
                "(SELECT value FROM json_each(?)) AND binding_kind IN ('same_record', "
                "'reviewed') AND tier NOT IN ('lead', 'contradicted') "
                "AND superseded_at IS NULL", (json.dumps(list(part)),)):
            if b.startswith(prefix) and b in parent:
                ra, rb = root(a), root(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    return {i: root(i) for i in ids}


def _type_ids(conn: Any, type_id: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT object_id FROM objects WHERE object_type = ? ORDER BY object_id", (type_id,))]


def _kept(root: str, sample: int | None) -> bool:
    """Whether a cluster is in a 1-in-``sample`` sample (whole clusters, by a hash of the
    cluster's root, so every copy of a kept record is kept)."""
    if not sample or sample <= 1:
        return True
    return int(hashlib.sha256(root.encode()).hexdigest()[:8], 16) % sample == 0


def measure_rule(conn: Any, rule: Mapping[str, Any], *, sample: int | None = None
                 ) -> dict[str, Any]:
    """One rule's counts per source (module doc)."""
    every = _type_ids(conn, rule["type"])
    cluster = {k: v for k, v in _clusters(conn, every, rule["type"]).items()
               if _kept(v, sample)}
    ids = sorted(cluster)
    # each copy's readings: (source, value, handles)
    copies: dict[str, list[tuple[str, str, frozenset[str], bool]]] = defaultdict(list)
    if rule["rollup"]:
        children: dict[str, list[str]] = defaultdict(list)
        for parent_id, child in _hop_rows(conn, ids, rule["rollup"]):
            children[parent_id].append(child)
        vals = _values(conn, [c for cs in children.values() for c in cs], rule["field"])
        for oid, kids in children.items():
            rows = [r for k in kids for r in vals.get(k, ())]
            sources = {source_of(r[1]) for r in rows}
            if not rows or len(sources) != 1 or None in sources or \
                    any(k not in vals for k in kids):
                continue          # a line without the value, or lines of two sources
            total = _combine([r[0] for r in rows], "sum")
            if total is not None:
                copies[cluster[oid]].append((sources.pop(), total,
                                             frozenset(r[2] for r in rows),
                                             rows[0][3] == "quantity"))
    else:
        for oid, rows in _values(conn, ids, rule["field"]).items():
            for value, lineage, handle, vtype in rows:
                source = source_of(lineage)
                if source is not None:
                    copies[cluster[oid]].append((source, value, frozenset([handle]),
                                                 vtype == "quantity"))
    # evidence: every member of a cluster reaches it, hop by hop
    reach: dict[str, set[str]] = defaultdict(set)      # frontier object -> clusters
    for oid in ids:
        if cluster[oid] in copies:
            reach[oid].add(cluster[oid])
    for hop in rule["path"]:
        nxt: dict[str, set[str]] = defaultdict(set)
        for here, there in _hop_rows(conn, list(reach), hop):
            nxt[there] |= reach[here]
        reach = nxt
    evidence_rows = _values(conn, list(reach), rule["evidence_field"])
    per: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    handles: dict[str, set[str]] = defaultdict(set)
    for oid, roots in reach.items():
        for value, lineage, handle, _vt in evidence_rows.get(oid, ()):
            source = source_of(lineage) or str(lineage)
            for r in roots:
                per[r][source].append(value)
                handles[r].add(handle)
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"compared": 0, "agreed": 0, "disputed_compared": 0, "disputed_agreed": 0})
    with_evidence = disputed = 0
    from operating.answer_conflicts import value_key
    for root, readings in copies.items():
        if root not in per:
            continue
        evidence = [v for v in (_combine(vs, rule["aggregate"])
                                for vs in per[root].values()) if v is not None]
        readings = [r for r in readings if not (r[2] & handles[root])]   # not self-evidence
        if not evidence or not readings:
            continue
        with_evidence += 1
        is_disputed = len({r[0] for r in readings}) > 1 and \
            len({value_key(r[1]) for r in readings}) > 1
        disputed += is_disputed
        for source in {r[0] for r in readings}:
            mine = [r for r in readings if r[0] == source]
            ok = any(agrees(r[1], e, tolerance_days=rule["tolerance_days"], quantity=r[3])
                     for r in mine for e in evidence)
            c = counts[source]
            c["compared"] += 1
            c["agreed"] += ok
            if is_disputed:
                c["disputed_compared"] += 1
                c["disputed_agreed"] += ok
    return {"type": rule["type"], "evidence_label": rule["evidence_label"],
            "records": len(ids), "clusters": len(set(cluster.values())),
            "with_evidence": with_evidence, "disputed": disputed,
            "sources": {k: dict(v) for k, v in sorted(counts.items())}}


def measure(conn: Any, spec: Sequence[Mapping[str, Any]], *, store: str,
            now: dt.datetime | None = None, sample: int | None = None,
            only: Sequence[str] | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    rules = {}
    for rule in spec:
        if only and rule["subject"] not in only:
            continue
        rules[rule["subject"]] = measure_rule(conn, rule, sample=sample)
    return {"schema": TABLE_SCHEMA, "measured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "store": store, "sample": sample or 1, "min_compared": MIN_COMPARED,
            "tie_points": str(TIE_POINTS), "rules": rules}


def write_table(path: Path, table: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- decide

def table_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get(TABLE_ENV) or TABLE_FILE)


@lru_cache(maxsize=4)
def _read_table(path: str, stamp: float) -> dict[str, Any] | None:
    del stamp                                  # part of the cache key: a new file re-reads
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, Mapping) or raw.get("schema") != TABLE_SCHEMA or \
            not isinstance(raw.get("rules"), Mapping):
        return None
    return dict(raw)


def load_table(path: Path | None = None) -> dict[str, Any] | None:
    """The measured table: ``path``, else the scheduled measurement
    (``PLX_PRECEDENCE_TABLE``) and, while that is missing or unreadable, the committed one
    (its date is stated with every decision). None when none reads (missing, unreadable,
    another schema): no decision."""
    for candidate in ([path] if path else [table_path(), TABLE_FILE]):
        try:
            stamp = candidate.stat().st_mtime
        except OSError:
            continue
        found = _read_table(str(candidate), stamp)
        if found is not None:
            return found
    return None


def _share(counts: Mapping[str, Any], disputed: bool) -> tuple[Decimal, int] | None:
    try:
        n = int(counts["disputed_compared" if disputed else "compared"])
        k = int(counts["disputed_agreed" if disputed else "agreed"])
    except (KeyError, TypeError, ValueError):
        return None
    if n < MIN_COMPARED or not 0 <= k <= n:
        return None
    return Decimal(k) / Decimal(n), n


def _pct(share: Decimal) -> str:
    return f"{int((share * 100).to_integral_value())}%"


def decide(subject: Any, readings: Sequence[Any], *, table: Mapping[str, Any] | None = None
           ) -> tuple[list[Any], str] | None:
    """(the trusted readings, why) by the measured table, or None when it does not decide
    (module doc). ``readings``: ``answer_conflicts.Reading`` (``lineage``, ``value``,
    ``as_of``), already only those the reader may open."""
    from operating.answer_conflicts import NEWER_BY, value_key
    table = load_table() if table is None else table
    entry = ((table or {}).get("rules") or {}).get(str(subject or ""))
    if not isinstance(entry, Mapping) or len(readings) < 2:
        return None
    by_source: dict[str, list[Any]] = defaultdict(list)
    for r in readings:
        source = source_of(getattr(r, "lineage", None))
        if source is None:
            return None                          # a copy the table cannot weigh
        by_source[source].append(r)
    if len(by_source) < 2:
        return None
    measured = entry.get("sources") or {}
    shares: dict[str, tuple[Decimal, int]] = {}
    basis = None
    for disputed in (True, False):
        found = {s: _share(measured.get(s) or {}, disputed) for s in by_source}
        if all(v is not None for v in found.values()):
            shares, basis = found, disputed      # type: ignore[assignment]
            break
    if not shares:
        return None
    best = max(v[0] for v in shares.values())
    tied = sorted(s for s, v in shares.items() if best - v[0] < TIE_POINTS)
    held = [r for s in tied for r in by_source[s]]
    tie_words = ""
    if len(tied) > 1:
        if any(r.as_of is None for r in held):
            return None
        held.sort(key=lambda r: r.as_of, reverse=True)
        newest = held[0]
        if any(value_key(r.value) != value_key(newest.value) and
               newest.as_of - r.as_of < NEWER_BY for r in held[1:]):
            return None                          # equally recent copies disagree
        held = [r for r in held if value_key(r.value) == value_key(newest.value)]
        tie_words = (f"; {' and '.join(SOURCE_WORDS[s] for s in tied)} agree equally "
                     "often, so the most recently updated copy is trusted "
                     f"(updated {newest.as_of.date().isoformat()})")
    elif len({value_key(r.value) for r in held}) != 1:
        return None                              # the winning source disagrees with itself
    if len(held) == len(readings):
        return None
    when = str(table.get("measured_at") or "")[:10] if table else ""  # type: ignore[union-attr]
    order = sorted(shares, key=lambda s: shares[s][0], reverse=True)
    rates = ", ".join(f"{SOURCE_WORDS[s]} {_pct(shares[s][0])}" for s in order)
    which = "where the copies disagreed" if basis else "on records with evidence"
    why = (f"{SOURCE_WORDS[order[0]] if len(tied) == 1 else 'the newest copy'} is trusted "
           f"for this field: measured against {entry.get('evidence_label') or 'the evidence'}"
           f" {which}, {rates} agreed (measured {when}){tie_words}")
    return held, why


# ---------------------------------------------------------------------------- CLI

def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.source_precedence")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--store", help="an ontology store (SQLite), opened read-only")
    src.add_argument("--pg-dsn", help="the Postgres ontology (read-only queries)")
    p.add_argument("--out", required=True)
    p.add_argument("--spec", default=None)
    p.add_argument("--sample", type=int, default=None,
                   help="measure 1 in N records (whole same-record clusters), stated in the "
                        "table")
    p.add_argument("--only", action="append", default=None, help="a subject (repeatable)")
    a = p.parse_args(argv)
    spec = load_spec(a.spec)
    if a.store:
        conn: Any = sqlite3.connect(f"file:{a.store}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = 1")
        label = "sqlite:" + Path(a.store).parent.name
    else:
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(a.pg_dsn)
        label = "postgres"
    try:
        table = measure(conn, spec, store=label, sample=a.sample, only=a.only)
    finally:
        conn.close()
    write_table(Path(a.out), table)
    print(json.dumps({k: {"with_evidence": v["with_evidence"], "disputed": v["disputed"]}
                      for k, v in table["rules"].items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
