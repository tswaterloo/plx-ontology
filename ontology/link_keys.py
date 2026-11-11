"""The keys a stored link was decided on, read back from its own row (a precision audit).

A link keeps what its decision compared in its ``reason`` (a JSON list of features), so a
precision audit reads the served store alone, repeatably, without re-deriving the join
from the source mirror:

* projector bindings (``fk``, ``json_path``, ``same_record``, ``text_match``): each
  ``pair`` feature names both ends' version (``from_handle``, ``to_handle``) and field
  (``from_field``, ``to_field``) and the compared value (``value``; ``from_value`` and
  ``to_value`` where they differ) or why there was none (``problem``);
* build steps (mentions, key joins, postings, sender and twin corroboration ...): each
  feature cites (``citations``) the version and field it read, with the compared value on
  the feature (``normalized``; ``matched_normalized`` for the matched record's side) and,
  where the step keeps it, the source's own text (``raw``);
* a confirmed lead (``ontology.key_links`` ``confirm``) names the stored lead it raised
  (``stored_lead.link_id``): that link's keys are its keys.

A feature that compared a value but cites nothing (written before its step kept keys)
leaves the link **keys not recorded**: the audit counts it so, never as precise. The keys
live in the link's own row, so they are trimmed with it: a reader that may not see a link
sees none of its keys, and they are deleted and rewritten with it.

``python3 -m ontology.link_keys audit --link-type T [--sample N] [--after LINK_ID]
(--store STORE.sqlite | --pg)`` reads N links of type T in link id order and reports
counts (never values): links with keys recorded, keys whose cited version the store
holds, keys whose value the store holds at the cited version and field (an identifier
row, a value of that field, or the record's native key for ``native_key.<part>``), and
keys not recorded. Stdlib (``--pg``: psycopg).
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sqlite3
import sys
from typing import Any, Mapping, Sequence

# Features that compare nothing themselves (an outcome, a reference to another link, a
# path or a count): they carry no key.
UNKEYED = frozenset({"unique_record", "unique", "same_party", "same_system", "uncorroborated",
                     "corroborates", "confirming_path", "stored_lead"})
VALUE_FIELDS = ("matched_normalized", "normalized", "value")
NATIVE_KEY = "native_key."


def keys(reason: Any) -> list[dict[str, Any]] | None:
    """The keys of one link's ``reason`` (a JSON text or list): per cited end, its
    ``feature``, ``l1_handle``, ``field_path``, compared ``value`` (None when the feature
    compared none, e.g. a name span) and source text ``raw`` when kept; a confirmed lead
    gives ``{"feature": "stored_lead", "link_id": ...}``. None when a feature that compared
    a value cites nothing (keys not recorded) or the reason holds no key at all."""
    features = json.loads(reason) if isinstance(reason, str) else reason
    out: list[dict[str, Any]] = []
    missing = False
    for f in features or ():
        if not isinstance(f, Mapping):
            continue
        name = f.get("feature")
        if name == "stored_lead" and f.get("link_id"):
            out.append({"feature": name, "link_id": f["link_id"]})
            continue
        if name == "pair":
            if f.get("from_handle") and f.get("to_handle"):
                for side in ("from", "to"):
                    out.append({"feature": name, "l1_handle": f[f"{side}_handle"],
                                "field_path": f.get(f"{side}_field"),
                                "value": f.get("value", f.get(f"{side}_value"))})
            else:
                missing = True
            continue
        if name in UNKEYED:
            continue
        cites = [c for c in f.get("citations") or () if isinstance(c, Mapping)]
        value = next((f[v] for v in VALUE_FIELDS if f.get(v) is not None), None)
        if not cites:
            missing = missing or value is not None
            continue
        for c in cites:
            out.append({"feature": name, "l1_handle": c.get("l1_handle"),
                        "field_path": c.get("field_path"), "value": value,
                        **({"raw": c["raw"]} if c.get("raw") is not None else {})})
    return None if missing or not out else out


def _one(conn: Any, sql: str, params: Sequence[Any]) -> Any:
    return conn.execute(sql, tuple(params)).fetchone()


def _held(conn: Any, key: Mapping[str, Any]) -> str:
    """``value_held``: the store holds the key's value at its version and field;
    ``version_held``: only the cited version (a text span, a value it does not index);
    ``version_missing``: not even the version."""
    handle, field, value = key.get("l1_handle"), key.get("field_path"), key.get("value")
    version = _one(conn, "SELECT object_id FROM object_versions WHERE l1_handle = ?", [handle])
    if version is None:
        return "version_missing"
    if value is None or field is None:
        return "version_held"
    text = str(value)
    if field.startswith(NATIVE_KEY):   # a record's own key (a BC system id, a FileMaker id)
        native = _one(conn, "SELECT native_key FROM objects WHERE object_id = ?", [version[0]])
        held = json.loads(native[0]).get(field[len(NATIVE_KEY):]) if native else None
        return ("value_held" if held is not None and str(held).strip().lower()
                == text.strip().lower() else "version_held")
    if _one(conn, "SELECT 1 FROM identifiers WHERE normalized = ? AND l1_handle = ? AND "
                  "field_path = ?", [text, handle, field]) is not None:
        return "value_held"
    raw = key.get("raw")
    for (held,) in conn.execute("SELECT value FROM object_values WHERE object_id = ? AND "
                                "l1_handle = ? AND field_path = ?", (version[0], handle, field)):
        if held is not None and str(held).strip().lower() in {
                text.strip().lower(), str(raw).strip().lower() if raw is not None else None}:
            return "value_held"
    return "version_held"


def audit(conn: Any, link_type: str, sample: int = 15, after: str = "") -> dict[str, Any]:
    """Counts of the keys of ``sample`` links of ``link_type`` after ``after`` in link id
    order (module docstring); ``not_recorded`` lists up to five such link ids."""
    counts: Counter[str] = Counter()
    not_recorded: list[str] = []
    rows = conn.execute("SELECT link_id, tier, reason FROM links WHERE link_type = ? AND "
                        "link_id > ? ORDER BY link_id LIMIT ?",
                        (link_type, after, int(sample))).fetchall()
    for link_id, tier, reason in rows:
        counts["links"] += 1
        counts[f"tier:{tier}"] += 1
        found = keys(reason)
        leads = [k["link_id"] for k in found or () if "link_id" in k]
        for lead in leads:   # a confirmed lead's keys are its stored lead's
            row = _one(conn, "SELECT reason FROM links WHERE link_id = ?", [lead])
            more = keys(row[0]) if row is not None else None
            found = None if more is None else [k for k in found if "link_id" not in k] + more
        if found is None:
            counts["keys_not_recorded"] += 1
            if len(not_recorded) < 5:
                not_recorded.append(link_id)
            continue
        counts["keys_recorded"] += 1
        for key in found:
            counts[f"key:{_held(conn, key)}"] += 1
    return {"link_type": link_type, "after": after, "counts": dict(sorted(counts.items())),
            "not_recorded": not_recorded}


def _connect(args: argparse.Namespace) -> Any:
    if args.pg:
        from operating.pg.config import dsn
        from operating.pg.sqlite_compat import PgCompatConnection
        return PgCompatConnection(dsn())
    conn = sqlite3.connect(f"file:{args.store}?mode=ro", uri=True)
    return conn


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.link_keys")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("audit", help="count the recorded keys of a sample of one link type")
    run.add_argument("--link-type", required=True)
    run.add_argument("--sample", type=int, default=15)
    run.add_argument("--after", default="")
    where = run.add_mutually_exclusive_group(required=True)
    where.add_argument("--store")
    where.add_argument("--pg", action="store_true")
    args = parser.parse_args(argv)
    conn = _connect(args)
    try:
        print(json.dumps(audit(conn, args.link_type, args.sample, args.after), indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["audit", "keys", "main"]
