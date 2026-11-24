"""Live equivalence of a purchase order's page read two ways, per reader class: today's reads
(``resolve`` then the header and lines from the business views) against the record page door
(``PLX_RECORD_DOOR``: one read of ``biz.page_items``). For N purchase orders it builds
both ``BizPage`` models through the same reader door (``operating.page_reads.PageReads``,
``scoped_read``: the grant decides the rows and cells), compares them exactly (the timing field
aside), and times each first read.

Read-only: it connects to the standby (``STANDBY_DSN``, default the local standby socket as
``plx_api``) and writes nothing. Reader classes are modelled as door grants over the real
audience keys and markings of the served tables:

* owner: every audience key and every marking;
* no-finance: every audience key and every marking set that does not name finance (finance cells come back withheld);
* no-marks: every audience key, no markings;
* narrow: every second audience key, every marking set but finance's (rows of the others are not served);
* nobody: no key.

  python3 -m tools.record_door_equivalence [--n 60] [--seed 1]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DSN = os.environ.get("STANDBY_DSN", "host=/run/plx-standby-pg dbname=plx user=plx_api")


def facts() -> tuple[list[str], list[str]]:
    """Every audience key and every marking the served tables name."""
    import psycopg
    with psycopg.connect(DSN) as c:
        keys, marks = set(), set()
        for audience, rec, doc in c.execute(
                "SELECT audience_keys, rec_marks, doc_marks FROM biz.access_classes"):
            keys.update(audience or ())
            marks.update(rec or ())
            marks.update(doc or ())
        for column in ("marks", "needs"):
            for (r,) in c.execute(f"SELECT {column} FROM biz.col_classes"):
                for v in r.values():
                    marks.update(json.loads(v) if isinstance(v, str) else v)
    return sorted(keys), sorted(m for m in marks if isinstance(m, str) and m.startswith("["))


def sample(n: int, seed: int) -> list[str]:
    import psycopg
    with psycopg.connect(DSN) as c:
        rows = [r[0] for r in c.execute(
            "SELECT page_key FROM biz.page_items WHERE page_kind = 'record' AND section = 'header' "
            "ORDER BY page_key")]
        counts = dict(c.execute("SELECT page_key, count(*) FROM biz.page_items WHERE page_kind = 'record' "
                                "AND section = 'lines' GROUP BY 1"))
    rnd = random.Random(seed)
    big = sorted(rows, key=lambda r: -counts.get(r, 0))[:max(3, n // 12)]   # the biggest pages too
    pick = list(dict.fromkeys(big + rnd.sample(rows, min(n, len(rows)))))[:n]
    return pick


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    from operating import page_reads as pr
    from operator_api import page_sql as ps
    keys, marks = facts()
    plain = [m for m in marks if "finance" not in m]
    classes = {"owner": (keys, marks), "no-finance": (keys, plain), "no-marks": (keys, []),
               "narrow": (keys[::2], plain), "nobody": ([], [])}
    ids = sample(args.n, args.seed)
    print(f"{len(ids)} purchase orders, {len(keys)} audience keys, markings {marks}")
    base = SimpleNamespace(store=SimpleNamespace(conn=SimpleNamespace(dsn=DSN, postgres=True)),
                           clock=lambda: None, item_gate=None)
    bad = 0
    for name, (k, mk) in classes.items():
        scope = SimpleNamespace(audience_keys=tuple(k), marking_sets=tuple(mk), held=frozenset(mk))
        base._scope = lambda actor, now, scope=scope: scope

        class View:
            def _call(self, tool, a):
                return pr.PageReads(base).call(a, object())

        class Worker:
            def run(self, reader, fn, timeout=None, priority=None):
                return fn(View())
        backend = SimpleNamespace(access_worker=Worker())
        old_t, new_t, same, none = [], [], 0, 0
        seen = 0
        for rid in ids:
            t0 = time.perf_counter()
            try:
                at = ps.resolve(backend, object(), "purchase_order", rid)
                old = ps.order(backend, object(), "purchase_order", at)
            except ps.NotFound:
                old = None
            old_t.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            new = ps.order_from_record(backend, object(), rid)
            new_t.append(time.perf_counter() - t0)
            if old is None and new is None:
                none += 1
                continue
            seen += 1
            if new is None or old is None:
                bad += 1
                print(f"  {name}: {rid[:60]} old={'page' if old else 'none'} new={'page' if new else 'none'}")
                continue
            a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
            a.pop("took_ms"), b.pop("took_ms")
            # the door page says the items' own build time as its as-of
            a["sources"], b["sources"] = (x.split("; as of")[0] for x in (a["sources"], b["sources"]))
            if a == b:
                same += 1
            else:
                bad += 1
                diff = [k2 for k2 in a if a[k2] != b[k2]]
                print(f"  {name}: {rid[:60]} DIFFERS in {diff}")
        med = lambda v: round(statistics.median(v) * 1000, 1)  # noqa: E731
        p90 = lambda v: round(sorted(v)[int(len(v) * 0.9) - 1] * 1000, 1)  # noqa: E731
        print(f"{name:10s} pages {seen:3d} identical {same:3d} neither {none:3d} | old ms p50 {med(old_t)} "
              f"p90 {p90(old_t)} | new ms p50 {med(new_t)} p90 {p90(new_t)}")
    print("EQUIVALENT" if not bad else f"{bad} DIFFERENCES")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
