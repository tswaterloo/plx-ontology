"""Equivalence of a page's old and new reads (speed specs 1.6, piece SV0), per reader, on records
drawn as the cold probe draws them (never opened by an earlier probe). Generalises
``operator_api.plan_equivalence`` from one plan section to whole pages.

    python3 tools/page_equiv.py --kind record:<Type> --n 30
        --readers owner,lead,l3,l2,partner-case --old SOURCE --new SOURCE
        [--base URL] [--session NAME=FILE] [--partner-ids FILE] [--dsn ...] [--json OUT]

A SOURCE is a URL path template (``/api/biz/page?kind=purchase_order&record={id}``; ``{type}``
and ``{id}`` filled, read through ``--base`` with the reader's session; a body with a
``sections`` object gives those sections, any other body is the one section ``body``) or a
``module:function`` called as ``function(session, record_type, record_id)`` that returns
``(http_status, {section: [items]})``: the adapter a piece writes for an old page whose shape is
not sections. ``partner-case`` is a staff reader's session over the records in
``--partner-ids`` (a private list kept outside the repository).

Each (reader, record) is ``same``, ``different`` or ``old_failed`` (the old read errored: not a
pass and not a difference). A difference is classified per item, an item keyed by its
``handle`` (the item's own, or its payload's ``v.handle``; an item without one is keyed by its
whole JSON, so a changed value shows as missing + extra):

* ``stored_missing``: the old page has it, the new one does not;
* ``stored_extra``: the new page has it, the old one does not;
* ``withheld``: both have it and they differ in what is withheld (a ``withheld`` field, or a
  value present on one side and null on the other);
* ``value``: both have it with another value;
* ``order``: the same items in another order.

Output: ``EQUIV <kind> same=<a> different=<b> old_failed=<c>`` (exit 0 when different=0 and
old_failed <= 2, else 1). A ``stored_extra`` or ``withheld`` difference for any reader but the
owner is a permission finding: ``EQUIV PERMISSION FINDING ...`` and exit 2, which stops the piece.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping, Optional, Sequence
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import cold_probe as cp  # noqa: E402

OWNER = "owner"
PARTNER = "partner-case"
PERMISSION_KINDS = frozenset({"stored_extra", "withheld"})
Page = Mapping[str, Sequence[Any]]


def key_of(item: Any) -> str:
    if isinstance(item, Mapping):
        if isinstance(item.get("handle"), str):
            return item["handle"]
        v = item.get("v")
        if isinstance(v, Mapping) and isinstance(v.get("handle"), str):
            return v["handle"]
    return json.dumps(item, sort_keys=True, default=str)


def _withheld_differs(a: Any, b: Any) -> bool:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        if a.get("withheld") != b.get("withheld"):
            return True
        return any(_withheld_differs(a.get(k), b.get(k)) for k in set(a) & set(b))
    return (a is None) != (b is None)


def classify(old: Page, new: Page) -> list[dict[str, Any]]:
    """Every difference between two pages, per section and item, classified."""
    out: list[dict[str, Any]] = []
    for section in sorted(set(old) | set(new)):
        o, n = list(old.get(section) or ()), list(new.get(section) or ())
        ok, nk = [key_of(i) for i in o], [key_of(i) for i in n]
        om, nm = dict(zip(ok, o)), dict(zip(nk, n))
        for k in ok:
            if k not in nm:
                out.append({"section": section, "kind": "stored_missing", "key": k})
        for k in nk:
            if k not in om:
                out.append({"section": section, "kind": "stored_extra", "key": k})
        changed = False
        for k in ok:
            if k in nm and cp.canonical(om[k]) != cp.canonical(nm[k]):
                changed = True
                out.append({"section": section, "key": k,
                            "kind": "withheld" if _withheld_differs(om[k], nm[k]) else "value",
                            "diff": cp.differences(cp.canonical(om[k]), cp.canonical(nm[k]))})
        common_o = [k for k in ok if k in nm]
        common_n = [k for k in nk if k in om]
        if not changed and common_o != common_n:
            out.append({"section": section, "kind": "order", "key": "*"})
    return out


def sections_of(body: bytes) -> dict[str, list]:
    got = json.loads(body or b"null")
    if isinstance(got, Mapping) and isinstance(got.get("sections"), Mapping):
        return {k: v if isinstance(v, list) else [v] for k, v in got["sections"].items()}
    return {"body": [got]}


def source(spec: str) -> Callable[[cp.Session, str, str], tuple[int, Page]]:
    """A URL template read with the reader's session, or a ``module:function``."""
    if spec.startswith("/") or spec.startswith("http"):
        def read(sess: cp.Session, rtype: str, rid: str) -> tuple[int, Page]:
            path = spec.replace("{type}", urllib.parse.quote(rtype, safe="")).replace(
                "{id}", urllib.parse.quote(rid, safe=""))
            if path.startswith("http"):
                split = urllib.parse.urlsplit(path)
                sess = cp.Session(sess.name, f"{split.scheme}://{split.netloc}", sess.cookie,
                                  sess.view_as, sess.missing)
                path = path[len(sess.base):]
            a = cp.get(sess, path, keep=True)
            if a.status == 404:
                return 404, {}
            if a.status != 200:
                return a.status, {}
            return 200, sections_of(a.body)
        return read
    mod, sep, name = spec.partition(":")
    if not sep:
        raise cp.Usage(f"source {spec!r}: a URL template or module:function")
    return getattr(importlib.import_module(mod), name)


def compare(readers: Sequence[cp.Session], ids: Mapping[str, Sequence[str]], rtype: str,
            old: Callable, new: Callable) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Counts {same, different, old_failed} over (reader, record) and the differences."""
    counts = {"same": 0, "different": 0, "old_failed": 0}
    found: list[dict[str, Any]] = []
    for sess in readers:
        if sess.missing:
            raise cp.Usage(sess.missing)
        for rid in ids.get(sess.name, ()):
            try:
                os_, op = old(sess, rtype, rid)
            except Exception as err:  # noqa: BLE001 - counted as old_failed and listed below
                os_, op = f"error {type(err).__name__}", {}
            if os_ not in (200, 404):
                counts["old_failed"] += 1
                found.append({"reader": sess.name, "record": rid, "old_status": os_})
                continue
            ns, np_ = new(sess, rtype, rid)
            if ns not in (200, 404):
                diffs = [{"section": "*", "kind": "stored_missing", "key": f"new status {ns}"}]
            else:
                diffs = classify(op, np_)
            if diffs:
                counts["different"] += 1
                found.append({"reader": sess.name, "record": rid, "diffs": diffs})
            else:
                counts["same"] += 1
    return counts, found


def permission_findings(found: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [f for f in found if f.get("reader") != OWNER
            and any(d["kind"] in PERMISSION_KINDS for d in f.get("diffs") or ())]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 tools/page_equiv.py",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--kind", required=True, help="record:<Type>")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--readers", default="owner,lead,l3,l2,partner-case")
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--base", default="")
    ap.add_argument("--session", action="append", default=[], help="NAME=ACCESS_LINK_FILE")
    ap.add_argument("--partner-ids", type=Path)
    ap.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN", "postgresql:///plx"))
    ap.add_argument("--seen", type=Path, default=cp.SEEN_FILE)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    try:
        family, sep, rtype = args.kind.partition(":")
        if family != "record" or not sep or not rtype:
            raise cp.Usage(f"--kind {args.kind!r}: record:<Type> (other families come with "
                           "their piece)")
        names = [r.strip() for r in args.readers.split(",") if r.strip()]
        sessions = cp.sessions_for(args.base.rstrip("/"), names,
                                   cp._pairs(args.session, "--session"), {})
        seen, said = cp.read_seen(args.seen)
        import psycopg
        with psycopg.connect(args.dsn, autocommit=True) as pg:
            drawn = cp.draw_ids(pg, [rtype], args.n, str(time.time_ns()), seen)[rtype]
        ids: dict[str, list[str]] = {}
        for name in names:
            if name == PARTNER:
                if args.partner_ids is None:
                    raise cp.Usage("partner-case needs --partner-ids FILE")
                ids[name] = [x.strip() for x in args.partner_ids.read_text().splitlines()
                             if x.strip()]
            else:
                ids[name] = drawn
        counts, found = compare(sessions, ids, rtype, source(args.old), source(args.new))
    except cp.Usage as err:
        print(f"EQUIV USAGE {err}", file=sys.stderr)
        return 2
    if said:
        print(said)
    if len(drawn) < args.n:
        print(f"record:{rtype}: {len(drawn)} of {args.n} records drawn (all there are)")
    print(f"EQUIV {args.kind} same={counts['same']} different={counts['different']} "
          f"old_failed={counts['old_failed']}", flush=True)
    if args.json:
        args.json.write_text(json.dumps({"counts": counts, "found": found}, indent=1,
                                        default=str), encoding="utf-8")
    leaks = permission_findings(found)
    if leaks:
        print(f"EQUIV PERMISSION FINDING {len(leaks)} (reader, record) pairs show a non-owner "
              "reader an extra or unwithheld item: stop the piece", flush=True)
        return 2
    return 0 if counts["different"] == 0 and counts["old_failed"] <= 2 else 1


if __name__ == "__main__":
    sys.exit(main())
