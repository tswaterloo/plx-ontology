"""Build, list and grade the link-coverage eval family (``evals.link_coverage``, F32).

    python3 -m scripts.eval_link_coverage build --dev-dir DIR --state DIR
        [--schema-code CHECKOUT] [--expect-schema PREFIX] [--seed 7] [--dev-share 0.3]
        [--workers 4] [--show-dev]
    python3 -m scripts.eval_link_coverage questions --dev-dir DIR --out FILE [--count N]
    python3 -m scripts.eval_link_coverage grade --dev-dir DIR --runs DIR [--times FILE]
        [--split dev|held_out|both] [--per-item]

**build** reads the Postgres ontology (``PLX_PG_DSN``, default ``postgresql:///plx``)
read-only (a read-only session: ``default_transaction_read_only``): the link types of the
schema compiled by ``--schema-code`` (a checkout whose ``ontology.schema`` compiled the
applied schema; default this one), refused unless that schema's digest is the one
Postgres holds (``store_meta.schema_digest``) and starts with ``--expect-schema``, so the
questions are phrased from the very schema the keys are read under. Every link kind gets
one question per template (``evals.link_coverage.TEMPLATES``); keys are executed over all
current firm links. The people exclusion and skipped mailboxes come from ``--state``
(read-only) exactly as the graph-walk family's (``scripts.eval_graph_walks.people_guard``:
refuses without a usable exclusion list). Items sharing a start record are one group;
about ``--dev-share`` (30%) of the items are dev (``--dev-dir``, outside any git work
tree), the rest held out (``scripts.eval_live_ops.held_out_dir``: only in the credentialed
environment, reported as totals only). Output: counts per link kind, template and
difficulty; ``--show-dev`` adds the dev questions.

**questions** writes ``[[id, question], ...]`` of both splits (0600, outside any git work
tree) for the askers (``ask.sh``); it is for the people running the askers, never builders.

**grade** grades each ``RUNS/<id>/answer.sse`` (the final ``direct_answer`` event) against
its key: correct / incomplete / wrong / abstain, and seconds (``--times`` lines
``<id> <start> <end> <seconds> ...`` else the answer's ``latency_ms``), reported per link
kind, per difficulty and per template. ``--per-item`` lists dev items only (a held-out
item is never shown beyond its id and grade).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

from evals import graph_walks as gw
from evals import link_coverage as lc
from scripts.eval_graph_walks import FIRM, Refused, Store, people_guard

SAMPLE = 40
SCAN = 4000
SMALL = 20000
DUMP = ("import json; from ontology.schema import load_schema; s = load_schema(); "
        "print(json.dumps({'digest': s.digest, 'link_types': {k: {'from': v['from'], "
        "'to': v['to'], 'name': v['name'], 'reverse_name': v.get('reverse_name')} "
        "for k, v in s.link_types.items()}}))")


def schema_of(checkout: Path) -> dict[str, Any]:
    """The link types and digest the checkout's ``ontology.schema`` compiles (in a child
    process, so a schema-prep worktree's compiler is used as is)."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
    done = subprocess.run([sys.executable, "-c", DUMP], cwd=checkout, env=env,
                          capture_output=True, text=True, timeout=600)
    if done.returncode != 0:
        raise Refused("schema_compile_failed")
    return json.loads(done.stdout)


class PgRecords(Store):
    """``evals.graph_walks.Records`` over the Postgres ontology, read-only."""

    NAME_BATCH = 5000

    def __init__(self, conn: Any, guard: Any, own: frozenset[str],
                 skipped_boxes: Sequence[str]) -> None:   # noqa: D107 - no SQLite store
        self.conn = conn
        self.samples: dict[str, list[tuple[str, str]]] = {}
        self.counts: dict[str, int] = {}
        self.guard, self.own = guard, own
        self.skipped_boxes = {b.casefold() for b in skipped_boxes if b}
        self._names: dict[str, dict[str, list[str]] | None] = {}
        self._cleared: dict[str, bool] = {}
        self._fields: list[int] | None = None

    def _all(self, sql: str, args: Sequence[Any]) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def sample(self, link_type: str) -> Sequence[tuple[str, str]]:
        """Up to ``SAMPLE`` current firm links, the smallest SHA-256 of their link id (of
        all of them for a type under ``SMALL`` links, else of the first ``SCAN`` read)."""
        if link_type not in self.samples:
            n = self._all("SELECT count(*) FROM (SELECT 1 FROM ontology.link_rows WHERE "
                          "link_type = %s AND tier = ANY(%s) AND superseded_at IS NULL "
                          "LIMIT %s) t", (link_type, list(FIRM), SMALL + 1))[0][0]
            self.counts[link_type] = int(n)
            rows = self._all("SELECT link_id, from_object, to_object FROM ontology.link_rows "
                             "WHERE link_type = %s AND tier = ANY(%s) AND superseded_at IS "
                             "NULL" + ("" if n <= SMALL else f" LIMIT {SCAN}"),
                             (link_type, list(FIRM)))
            rows = [r for r in rows if not gw.mirror_type(gw.object_type(r[1]))
                    and not gw.mirror_type(gw.object_type(r[2]))]
            rows.sort(key=lambda r: hashlib.sha256(r[0].encode()).digest())
            self.samples[link_type] = [(a, b) for _i, a, b in rows[:SAMPLE]]
        return self.samples[link_type]

    def step(self, ids: Sequence[str], link_type: str, direction: str
             ) -> list[tuple[str, str]]:
        near, far = ("from_object", "to_object") if direction == "out" else \
            ("to_object", "from_object")
        out: list[tuple] = []
        for chunk in self._chunks(ids):
            out += self._all(f"SELECT {near}, {far} FROM ontology.link_rows WHERE {near} = "
                             "ANY(%s) AND link_type = %s AND superseded_at IS NULL AND tier = "
                             "ANY(%s)", (chunk, link_type, list(FIRM)))
        return [(a, b) for a, b in out if not gw.mirror_type(gw.object_type(b))]

    def rows(self, ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """The records' current values (``object_values`` rows), read key by key: their
        versions, those versions' current value sources, those sources' cells. Through
        the view the planner misjudged a record's versions (385 for 11,200) and scanned
        all value sources (126 s for 200 records on the served store, 11-09)."""
        out: dict[str, list[dict[str, Any]]] = {}
        for chunk in self._chunks(ids, 200):
            owner = dict(self._all("SELECT ver, object_id FROM ontology.object_versions "
                                   "WHERE object_id = ANY(%s)", (chunk,)))
            if not owner:
                continue
            of_src = {src: owner[ver] for src, ver in self._all(
                "SELECT src, ver FROM ontology.value_sources WHERE ver = ANY(%s) AND "
                "superseded_at IS NULL", (sorted(owner),))}
            if not of_src:
                continue
            for src, prop, vtype, value, unit, currency, marks in self._all(
                    "SELECT c.src, f.property, k.value_type, c.value, k.unit, c.currency, "
                    "k.markings FROM ontology.value_cells c JOIN ontology.value_fields f ON "
                    "f.field = c.field JOIN ontology.value_kinds k ON k.kind = c.kind WHERE "
                    "c.src = ANY(%s) ORDER BY c.src, c.field", (sorted(of_src),)):
                out.setdefault(of_src[src], []).append(
                    {"property": prop, "value_type": vtype, "value": value, "unit": unit,
                     "currency": currency, "markings": marks})
        return out

    def versions(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        out = {}
        for chunk in self._chunks(ids):
            for oid, handle, marks in self._all(
                    "SELECT object_id, l1_handle, markings FROM ontology.object_versions "
                    "WHERE object_id = ANY(%s) AND superseded_at IS NULL", (chunk,)):
                out[oid] = (handle, marks)
        return out

    def _name_fields(self) -> list[int]:
        """The value fields of every ``NAME_PROPS`` property (read once)."""
        if self._fields is None:
            self._fields = [r[0] for r in self._all(
                "SELECT field FROM ontology.value_fields WHERE property = ANY(%s) "
                "ORDER BY field", (list(gw.NAME_PROPS),))]
        return self._fields

    def lookup(self, object_type: str, label: str) -> list[str] | None:
        """``Store.lookup`` on Postgres: the type's name index is read once, ``NAME_BATCH``
        records a statement in object id order (the ``objects`` key, then each record's
        current value cells of the name fields by the version and cell keys), so no
        statement walks a whole type through the ``object_values`` view: that walk ran
        past the statement timeout on the served store (11-08, 499 s)."""
        if object_type not in self._names:
            low, high = object_type + ":", object_type + ";"
            count = self._all("SELECT count(*) FROM (SELECT 1 FROM ontology.objects WHERE "
                              "object_id > %s AND object_id < %s LIMIT %s) t",
                              (low, high, self.MAX_NAMED + 1))[0][0]
            if count > self.MAX_NAMED:
                self._names[object_type] = None
            else:
                index: dict[str, list[str]] = {}
                fields, last = self._name_fields(), low
                while fields:
                    ids = [r[0] for r in self._all(
                        "SELECT object_id FROM ontology.objects WHERE object_id > %s AND "
                        "object_id < %s ORDER BY object_id LIMIT %s",
                        (last, high, self.NAME_BATCH))]
                    if not ids:
                        break
                    for oid, value in self._all(
                            "SELECT v.object_id, c.value FROM ontology.object_versions v "
                            "JOIN ontology.value_sources s ON s.ver = v.ver AND "
                            "s.superseded_at IS NULL JOIN ontology.value_cells c ON "
                            "c.src = s.src AND c.field = ANY(%s) WHERE v.object_id = ANY(%s)",
                            (fields, ids)):
                        text = " ".join(str(value or "").split()).casefold()
                        if text:
                            index.setdefault(text, []).append(oid)
                    last = ids[-1]
                    if len(ids) < self.NAME_BATCH:
                        break
                self._names[object_type] = index
        index = self._names[object_type]
        if index is None:
            return None
        return sorted(set(index.get(" ".join(label.split()).casefold(), [])))


def _compose_part(args: tuple) -> tuple[list, dict]:
    """One worker's link kinds, on its own read-only connection."""
    dsn, state, environ, schema, only, seed, as_of = args
    guard, own, boxes = people_guard(Path(state), environ)
    conn = connect(dsn)
    try:
        records = PgRecords(conn, guard, own, boxes)
        made = lc.compose(lc.link_types(schema["link_types"]), records, seed=seed,
                          as_of=as_of, only=only)
        return made[0], {"dropped": made[1], "counts": records.counts}
    finally:
        conn.close()


def deal(links: Sequence[Any], workers: int) -> list[list[str]]:
    """The link kind ids in ``workers`` parts, every kind of one from-type in one part
    (largest type first, to the part holding fewest kinds)."""
    by_type: dict[str, list[str]] = {}
    for link in links:
        by_type.setdefault(link.source, []).append(link.id)
    parts: list[list[str]] = [[] for _ in range(max(1, workers))]
    for _type, ids in sorted(by_type.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        min(parts, key=len).extend(sorted(ids))
    return [sorted(p) for p in parts]


def compose_parallel(dsn: str, state: Path, environ: Mapping[str, str], schema: Mapping,
                     seed: int, as_of: Any, workers: int) -> tuple[list, dict, dict]:
    """``evals.link_coverage.compose`` over ``workers`` processes; the same questions as
    one process (each link kind draws from its own stream). Link kinds are dealt by type
    (``deal``), so a type's name index is read by one worker, not by each."""
    from concurrent.futures import ProcessPoolExecutor
    parts = deal(lc.link_types(schema["link_types"]), workers)
    questions, dropped, counts = [], {}, {}
    env = dict(environ)
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        for made, extra in pool.map(_compose_part, [(dsn, str(state), env, schema, p, seed,
                                                     as_of) for p in parts if p]):
            questions += made
            dropped.update(extra["dropped"])
            counts.update(extra["counts"])
    return questions, dropped, counts


def connect(dsn: str) -> Any:
    import psycopg
    conn = psycopg.connect(dsn, autocommit=True,
                           options="-c default_transaction_read_only=on "
                                   "-c statement_timeout=300000")
    conn.read_only = True
    return conn


def store_meta(conn: Any) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM ontology.store_meta WHERE key IN "
                    "('schema_digest', 'content_digest', 'built_at')")
        return {k: v for k, v in cur.fetchall()}


def check_schema(held: Mapping[str, str], compiled: str, expect: str | None) -> None:
    """Refuse unless Postgres holds the compiled schema (and the expected one)."""
    digest = str(held.get("schema_digest") or "")
    if not digest or digest != compiled:
        raise Refused("schema_not_applied")
    if expect and not digest.removeprefix("sha256:").startswith(expect):
        raise Refused("schema_not_expected")


def times_of(path: Path | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if path and path.is_file():
        for line in path.read_text("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3].lstrip("-").isdigit():
                out[parts[0]] = float(parts[3])   # the last attempt's time wins
    return out


def grade_runs(pairs: Sequence[tuple[dict, dict]], runs: Path, times: Mapping[str, float]
               ) -> list[dict[str, Any]]:
    rows = []
    for item, key in pairs:
        path = runs / item["id"] / "answer.sse"
        if not path.is_file():
            continue
        answer = lc.final_answer(path.read_text("utf-8", errors="replace"))
        grade, detail = lc.grade(answer, key)
        seconds = times.get(item["id"])
        if seconds is None and answer and answer.get("latency_ms") is not None:
            seconds = float(answer["latency_ms"]) / 1000
        rows.append({"item_id": item["id"], "split": item["split"], "key": key,
                     "grade": grade, "detail": detail, "seconds": seconds,
                     "question": item["question"]["text"],
                     "answer": str((answer or {}).get("direct_answer") or "")[:300]})
    return rows


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--dev-dir", type=Path, required=True)
    b.add_argument("--state", type=Path, required=True)
    b.add_argument("--schema-code", type=Path, default=Path(__file__).resolve().parents[1])
    b.add_argument("--expect-schema")
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--dev-share", type=float, default=lc.DEV_SHARE)
    b.add_argument("--as-of")
    b.add_argument("--workers", type=int, default=4)
    b.add_argument("--show-dev", action="store_true")
    q = sub.add_parser("questions")
    q.add_argument("--dev-dir", type=Path, required=True)
    q.add_argument("--out", type=Path, required=True)
    q.add_argument("--count", type=int, help="a sample: every link kind before any twice")
    q.add_argument("--sample-seed", default="1")
    g = sub.add_parser("grade")
    g.add_argument("--dev-dir", type=Path, required=True)
    g.add_argument("--runs", type=Path, required=True)
    g.add_argument("--times", type=Path)
    g.add_argument("--split", choices=("dev", "held_out", "both"), default="both")
    g.add_argument("--per-item", action="store_true")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    from scripts import eval_live_ops as lo
    try:
        if lo._inside_git(args.dev_dir):
            raise Refused("dev_dir_inside_a_git_work_tree")
        held = lo.held_out_dir(args.dev_dir)   # refuses outside the credentialed env
        if args.command == "questions":
            if lo._inside_git(args.out.parent):
                raise Refused("questions_inside_a_git_work_tree")
            pairs = lo.load_split(args.dev_dir) + lo.load_split(held)
            pairs = [p for p in pairs if p[1].get("family") == lc.KEY_FAMILY]
            if args.count:
                chosen = lc.sample(pairs, args.count, args.sample_seed)
                by_id = {i["id"]: (i, k) for i, k in pairs}
                pairs = [by_id[i] for i in chosen]
            args.out.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump([[i["id"], i["question"]["text"]] for i, _k in pairs], fh)
            print(json.dumps({"questions": len(pairs)}))
            return 0
        if args.command == "grade":
            pairs = []
            if args.split in ("dev", "both"):
                pairs += lo.load_split(args.dev_dir)
            if args.split in ("held_out", "both"):
                pairs += lo.load_split(held)
            pairs = [p for p in pairs if p[1].get("family") == lc.KEY_FAMILY]
            rows = grade_runs(pairs, args.runs, times_of(args.times))
            report = lc.grade_report(rows)
            report["unanswered"] = len(pairs) - len(rows)
            if args.per_item:
                report["dev_items"] = [{k: r[k] for k in ("item_id", "grade", "detail",
                                                           "seconds", "question", "answer")}
                                       for r in rows if r["split"] == "dev"]
                report["held_out_items"] = [{"item_id": r["item_id"], "grade": r["grade"]}
                                            for r in rows if r["split"] != "dev"]
            print(json.dumps(report, indent=1, sort_keys=True))
            return 0
        if not args.state.is_dir():
            raise Refused("state_missing")
        schema = schema_of(args.schema_code)
        guard, own, boxes = people_guard(args.state, environ)
        from operating.pg import config
        conn = connect(config.dsn(environ))
        try:
            meta = store_meta(conn)
            check_schema(meta, schema["digest"], args.expect_schema)
        finally:
            conn.close()
        links = lc.link_types(schema["link_types"])
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        as_of = datetime.fromisoformat(args.as_of).date() if args.as_of else \
            datetime.now(timezone.utc).date()
        questions, dropped, counts = compose_parallel(
            config.dsn(environ), args.state, environ, schema, args.seed, as_of, args.workers)
        items, keys, stats = lc.build(
            links, None, seed=args.seed, as_of=as_of, now=now,
            generations={"postgres": f"{meta['schema_digest'][7:19]}/"
                                     f"{str(meta.get('content_digest'))[7:23]}"},
            dev_share=args.dev_share, composed=(questions, dropped))
        stats["firm_links_capped"] = dict(sorted(counts.items()))
        from evals import live_ops, runner
        for item, key in zip(items, keys):
            errors = runner.validate_item(item)
            if errors:
                raise Refused(f"item_invalid:{errors[0]}")
            problems = live_ops.key_problems(key)
            if problems:
                raise Refused(f"key_invalid:{problems[0]}")
        for split, directory in (("dev", args.dev_dir), ("held_out", held)):
            chosen = [(i, k) for i, k in zip(items, keys) if i["split"] == split]
            lo.write_split(directory, [i for i, _ in chosen], [k for _, k in chosen])
        stats.update(schema=meta["schema_digest"], as_of=as_of.isoformat(), seed=args.seed)
        if args.show_dev:
            stats["dev_items"] = [{"id": i["id"], "question": i["question"]["text"],
                                   **{s: k["strata"][s] for s in ("link_kind", "template",
                                                                 "difficulty")}}
                                  for i, k in zip(items, keys) if i["split"] == "dev"]
        print(json.dumps(stats, indent=1, sort_keys=True))
        return 0
    except (Refused, lo.MailRefused, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "code": str(exc)[:200]}),
              file=sys.stderr)
        return 1
    except Exception as exc:   # noqa: BLE001 - a database error is a refusal, never a pass
        print(json.dumps({"error": type(exc).__name__, "code": str(exc)[:200]}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
