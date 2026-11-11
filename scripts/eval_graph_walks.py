"""Build the graph-walk eval family (``evals.graph_walks``) from the published projection.

    python3 -m scripts.eval_graph_walks census --out FILE [--state DIR] [--workers 12]
    python3 -m scripts.eval_graph_walks build --dev-dir DIR [--state DIR] [--census FILE]
        [--items 1000] [--seed 11] [--dev-share 0.1] [--show-dev]
    python3 -m scripts.eval_graph_walks sample --dev-dir DIR [--count 60] [--seed 1]

**census** reads the published generation's ``link_rows`` once, read-only (sqlite
``mode=ro``, ``query_only``), split by rowid range over ``--workers`` processes: per link
type and per pair of endpoint bindings, the count of current firm links (verified,
reviewed, corroborated), and per link type a fixed-size sample of its links (the ones with
the smallest SHA-256 of their link id: deterministic, and mergeable across the workers).
The census is written 0600 (it holds record ids) outside any git work tree.

**build** draws ``--items`` walks with a fixed ``--seed`` (``evals.graph_walks.Composer``):
each link type of the served schema gets walks in proportion to the square root of its
link count, at least ``FLOOR`` each; each walk starts on a sampled link, takes 1 to 4 hops
along the schema's link types, filters on a property along the way, and asks a count, a
sum, a minimum, a maximum, the latest date or a list (sometimes two branches compared).
The answer is the walk executed over every current firm link and current value of the
projection (``Store``), with the evidence ids; a walk the records cannot settle is
dropped, and about one in ten kept items has the true answer "none".

People: any walk touching a record of a skipped mailbox (``scripts.eval_live_ops.
SKIPPED_MAILBOXES``) or naming a person the people exclusion excludes, or an address of
ours it cannot decide, is dropped; with no usable exclusion list the build refuses (fail
closed). Items sharing a record are one group (``evals.partition``); about ``--dev-share``
of the items are dev (``--dev-dir``), the rest held out
(``scripts.eval_live_ops.held_out_dir``: reported as totals only). Output: counts only
(coverage per link type, system pair, hop count and answer kind); ``--show-dev`` adds the
dev questions.

**sample** prints ``N`` item ids drawn from both splits, stratified by hop count and
system pair (``evals.graph_walks.stratified``), for ``scripts.eval_live_ops run --items``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
import hashlib
import heapq
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Sequence

from evals import graph_walks as gw

FIRM = ("verified", "reviewed", "corroborated")
RESERVOIR = 240
CENSUS_FORMAT = "plx-graph-census/v1"


class Refused(ValueError):
    """The build cannot run safely (a code, never a value)."""


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = 1")
    return conn


def current_store(state: Path) -> tuple[Path, str, str]:
    """(the published generation's store, its generation, its day)."""
    doc = json.loads((state / "projection" / "CURRENT").read_text("utf-8"))
    generation = str(doc["generation"])
    store = state / "projection" / "generations" / generation / "store.sqlite"
    if not store.is_file():
        raise Refused("projection_store_missing")
    return store, generation, str(doc["committed_at"])[:10]


def _binding(object_id: str) -> str:
    parts = object_id.split(":", 2)
    return parts[1] if len(parts) > 2 else ""


def _census_part(args: tuple[str, int, int]) -> dict[str, Any]:
    store, low, high = args
    conn = _ro(Path(store))
    counts: Counter = Counter()
    heaps: dict[str, list[tuple[int, str, str]]] = {}
    try:
        for link_id, link_type, a, b in conn.execute(
                "SELECT link_id, link_type, from_object, to_object FROM link_rows WHERE rowid "
                "BETWEEN ? AND ? AND superseded_at IS NULL AND tier IN (?, ?, ?)",
                (low, high, *FIRM)):
            if gw.mirror_type(a.split(":", 1)[0]) or gw.mirror_type(b.split(":", 1)[0]):
                continue
            counts[(link_type, _binding(a), _binding(b))] += 1
            rank = -int.from_bytes(hashlib.sha256(link_id.encode()).digest()[:8], "big")
            heap = heaps.setdefault(link_type, [])
            if len(heap) < RESERVOIR:
                heapq.heappush(heap, (rank, a, b))
            elif rank > heap[0][0]:
                heapq.heapreplace(heap, (rank, a, b))
    finally:
        conn.close()
    return {"counts": [[*k, n] for k, n in counts.items()],
            "samples": {t: [list(x) for x in h] for t, h in heaps.items()}}


def census(store: Path, workers: int) -> dict[str, Any]:
    conn = _ro(store)
    try:
        low, high = conn.execute("SELECT min(rowid), max(rowid) FROM link_rows").fetchone()
    finally:
        conn.close()
    step = max(1, (high - low + 1) // (workers * 4) + 1)
    parts = [(str(store), start, min(high, start + step - 1))
             for start in range(low, high + 1, step)]
    counts: Counter = Counter()
    heaps: dict[str, list[tuple[int, str, str]]] = {}
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        for part in pool.map(_census_part, parts):
            for t, fb, tb, n in part["counts"]:
                counts[(t, fb, tb)] += n
            for t, rows in part["samples"].items():
                heap = heaps.setdefault(t, [])
                for row in rows:
                    item = (row[0], row[1], row[2])
                    if len(heap) < RESERVOIR:
                        heapq.heappush(heap, item)
                    elif item[0] > heap[0][0]:
                        heapq.heapreplace(heap, item)
    return {"format": CENSUS_FORMAT,
            "counts": sorted([[*k, n] for k, n in counts.items()]),
            "samples": {t: sorted([[a, b] for _r, a, b in h]) for t, h in sorted(heaps.items())}}


def _write_private(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
    tmp.replace(path)


class Store:
    """``evals.graph_walks.Records`` over the projection store (read-only) and the census."""

    PEOPLE_SYSTEMS = frozenset({"mail", "Teams", "meetings", "MC", "SharePoint"})
    EMAIL = __import__("re").compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    GUID = __import__("re").compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
    MAX_NAMED = 400_000
    # name indexes kept at once (each up to MAX_NAMED records): a recipes pass runs one
    # Store per worker, and an unbounded cache held every type's index (8 GB OOM, 11-10)
    MAX_INDEXES = 6

    def __init__(self, store: Path, samples: Mapping[str, Sequence[Sequence[str]]],
                 guard: Any, own: frozenset[str], skipped_boxes: Iterable[str]) -> None:
        self.conn = _ro(store)
        self.samples = {t: [tuple(x) for x in rows] for t, rows in samples.items()}
        self.guard, self.own = guard, own
        self.skipped_boxes = {b.casefold() for b in skipped_boxes if b}
        self._names: dict[str, dict[str, list[str]] | None] = {}
        self._cleared: dict[str, bool] = {}

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _chunks(ids: Sequence[str], size: int = 400) -> Iterable[list[str]]:
        ids = list(ids)
        for start in range(0, len(ids), size):
            yield ids[start:start + size]

    def sample(self, link_type: str) -> Sequence[tuple[str, str]]:
        return self.samples.get(link_type, [])

    def step(self, ids: Sequence[str], link_type: str, direction: str
             ) -> list[tuple[str, str]]:
        near, far = ("from_object", "to_object") if direction == "out" else \
            ("to_object", "from_object")
        out = []
        for chunk in self._chunks(ids):
            out += self.conn.execute(
                f"SELECT {near}, {far} FROM link_rows WHERE {near} IN "
                f"({','.join('?' * len(chunk))}) AND link_type = ? AND superseded_at IS NULL "
                f"AND tier IN (?, ?, ?)", (*chunk, link_type, *FIRM)).fetchall()
        return [(a, b) for a, b in out if not gw.mirror_type(gw.object_type(b))]

    def rows(self, ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for chunk in self._chunks(ids, 200):
            for oid, prop, vtype, value, unit, currency, marks in self.conn.execute(
                    "SELECT object_id, property, value_type, value, unit, currency, markings "
                    f"FROM object_values WHERE object_id IN ({','.join('?' * len(chunk))}) "
                    "AND superseded_at IS NULL", chunk):
                out.setdefault(oid, []).append(
                    {"property": prop, "value_type": vtype, "value": value, "unit": unit,
                     "currency": currency, "markings": marks})
        return out

    def versions(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        out = {}
        for chunk in self._chunks(ids):
            for oid, handle, marks in self.conn.execute(
                    "SELECT object_id, l1_handle, markings FROM object_versions WHERE object_id "
                    f"IN ({','.join('?' * len(chunk))}) AND superseded_at IS NULL", chunk):
                out[oid] = (handle, marks)
        return out

    def lookup(self, object_type: str, label: str) -> list[str] | None:
        """Every record of the type that the label names (any ``NAME_PROPS`` value, case
        folded); None for a type too large to index (it is never a start)."""
        if object_type not in self._names:
            count = self.conn.execute(
                "SELECT count(*) FROM objects WHERE object_id >= ? AND object_id < ?",
                (object_type + ":", object_type + ";")).fetchone()[0]
            if count > self.MAX_NAMED:
                self._names[object_type] = None
            else:
                index: dict[str, list[str]] = {}
                props = gw.NAME_PROPS
                for oid, value in self.conn.execute(
                        "SELECT object_id, value FROM object_values WHERE object_id >= ? AND "
                        f"object_id < ? AND property IN ({','.join('?' * len(props))}) AND "
                        "superseded_at IS NULL", (object_type + ":", object_type + ";", *props)):
                    text = " ".join(str(value or "").split()).casefold()
                    if text:
                        index.setdefault(text, []).append(oid)
                self._names[object_type] = index
                while len(self._names) > self.MAX_INDEXES:
                    self._names.pop(next(iter(self._names)))
        index = self._names[object_type]
        if index is None:
            return None
        return sorted(set(index.get(" ".join(label.split()).casefold(), [])))

    def _address_ok(self, address: str) -> bool:
        from integrations.live_source_guard import NotVisible
        from scripts import eval_live_ops as lo
        address = address.strip().casefold()
        if lo.skipped(address):
            return False
        try:
            if self.guard.decides(address):
                return not self.guard.is_excluded(address)
        except NotVisible:
            return False
        return address.rsplit("@", 1)[-1] not in self.own

    def _oid_ok(self, value: str) -> bool:
        from integrations.live_source_guard import NotVisible
        try:
            return not self.guard.is_excluded(value)
        except NotVisible:
            return False

    def cleared(self, ids: Sequence[str]) -> bool:
        """No record of a skipped mailbox, no person the exclusion excludes, no address of
        ours it cannot decide (fail closed)."""
        if self.guard.unavailable:
            return False
        want = [i for i in ids if i not in self._cleared]
        if want:
            versions = self.versions(want)
            rows = self.rows(want)
            for oid in want:
                handle = versions.get(oid, ("", ""))[0].casefold()
                texts = [handle] + [str(r.get("value") or "") for r in rows.get(oid, [])]
                ok = not any(box in handle for box in self.skipped_boxes)
                for text in texts:
                    if not ok:
                        break
                    ok = all(self._address_ok(a) for a in self.EMAIL.findall(text))
                    if ok and gw.system_of(gw.binding_of(oid)) in self.PEOPLE_SYSTEMS:
                        ok = all(self._oid_ok(g) for g in self.GUID.findall(text))
                self._cleared[oid] = ok
        return all(self._cleared.get(i, False) for i in ids)


def schema_links(counts: Sequence[Sequence[Any]]) -> list[gw.Link]:
    """The served schema's link types (their names from the schema), each with its count
    of current firm links in the census; relation mirrors left out."""
    from ontology.schema import load_schema
    per: Counter = Counter()
    for link_type, _a, _b, n in counts:
        per[link_type] += int(n)
    out = []
    for link_id, link in sorted(load_schema().link_types.items()):
        if gw.mirror_type(link["from"]) or gw.mirror_type(link["to"]):
            continue
        out.append(gw.Link(link_id, link["name"], link.get("reverse_name") or link["name"],
                           link["from"], link["to"], per.get(link_id, 0)))
    return out


def people_guard(state: Path, environ: Mapping[str, str]) -> tuple[Any, frozenset[str], list[str]]:
    """(the people exclusion with the captured directory, our mail domains, the skipped
    mailboxes' ids); refuses with no usable exclusion list or mailbox list (fail closed)."""
    from integrations.people_exclusion import PeopleExclusion, with_directory
    from scripts import eval_live_ops as lo
    guard = with_directory(PeopleExclusion.from_env(environ), state)
    if guard.unavailable:
        raise Refused(f"people_exclusion_unavailable:{guard.unavailable}")
    mail_db = state / lo.MAIL_CAPTURE
    if not mail_db.is_file():
        raise Refused("mail_capture_missing")
    conn = lo._ro(mail_db)
    try:
        own = lo.own_domains(conn)
        boxes = lo.skipped_mailboxes(conn)
    finally:
        conn.close()
    if not own:
        raise Refused("mail_own_domains_unknown")
    return guard, own, boxes


def build(links: Sequence[gw.Link], store: Any, *, items: int, seed: int, as_of: date,
          generation: str, now: str, dev_share: float
          ) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """``items`` walks (``quotas`` per first link type, topped up by weight), their items
    and keys dealt dev/held-out by start record; and the counts."""
    import random
    composer = gw.Composer(links, store, as_of)
    quota = gw.quotas(links, items)
    share = gw.weights(links)
    rng = random.Random(seed)
    walks: list[gw.Walk] = []
    dropped: Counter = Counter()
    made: Counter = Counter()
    starts: set[tuple[str, str]] = set()
    nones = 0
    order = sorted(quota, key=lambda k: (-quota[k], k))
    attempts = {k: 0 for k in quota}
    max_attempts = 12

    def attempt(link_id: str) -> None:
        nonlocal nones
        attempts[link_id] += 1
        try:
            walk = composer.compose(link_id, rng)
        except gw.Dropped as why:
            dropped[str(why)] += 1
            return
        if (walk.start, walk.question) in starts:
            dropped["duplicate"] += 1
            return
        if walk.kind == "none" and nones + 1 > gw.NONE_SHARE * items:
            dropped["none_over_share"] += 1
            return
        nones += walk.kind == "none"
        starts.add((walk.start, walk.question))
        walks.append(walk)
        made[link_id] += 1

    for link_id in order:   # every link type its floor first, then its share
        while made[link_id] < min(quota[link_id], gw.FLOOR) and \
                attempts[link_id] < max_attempts * gw.FLOOR:
            attempt(link_id)
    for link_id in order:
        while made[link_id] < quota[link_id] and len(walks) < items and \
                attempts[link_id] < max_attempts * quota[link_id]:
            attempt(link_id)
    ids, w = list(share), list(share.values())
    spins = 0
    while len(walks) < items and spins < items * 20:
        spins += 1
        link_id = rng.choices(ids, weights=w)[0]
        if attempts[link_id] < max_attempts * max(quota[link_id], 1) * 3:
            attempt(link_id)
    walks = walks[:items]
    digest = hashlib.sha256("\x1f".join(sorted(x.question for x in walks)).encode()).hexdigest()
    out_items: list[dict] = []
    out_keys: list[dict] = []
    for n, walk in enumerate(walks):
        item, key = gw.item_and_key(f"graph#{n}", walk, now=now, as_of=as_of,
                                    generations={"projection": generation}, digest=digest)
        out_items.append(item)
        out_keys.append(key)
    by_start: dict[str, list[int]] = {}
    for n, walk in enumerate(walks):
        by_start.setdefault(walk.start, []).append(n)
    from evals import blind_mined as bm
    dealt = bm.deal(list(by_start.values()), dev_share, digest)
    counters = {"dev": 0, "held_out": 0}
    for index, (item, key) in enumerate(zip(out_items, out_keys)):
        split = dealt[index]
        counters[split] += 1
        item["split"] = split
        item["id"] = f"{'dev' if split == 'dev' else 'ho'}-{gw.FAMILY}-{counters[split]:04d}"
        key["item_id"] = item["id"]
    linked = [l for l in links if l.count > 0]
    covered = gw.coverage(out_keys)
    stats = {"items": len(out_items), "dev": counters["dev"], "held_out": counters["held_out"],
             "dropped": dict(sorted(dropped.items())), "coverage": covered,
             "link_types_with_links": len(linked),
             "link_types_covered": sum(1 for l in linked if covered["link_types"].get(l.id)),
             "link_types_uncovered": sorted(l.id for l in linked
                                            if not covered["link_types"].get(l.id)),
             "schema_link_types_without_firm_links": sum(1 for l in links if l.count == 0)}
    return out_items, out_keys, stats


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("census")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--state", type=Path)
    c.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 4))
    b = sub.add_parser("build")
    b.add_argument("--dev-dir", type=Path, required=True)
    b.add_argument("--state", type=Path)
    b.add_argument("--census", type=Path)
    b.add_argument("--items", type=int, default=1000)
    b.add_argument("--seed", type=int, default=11)
    b.add_argument("--dev-share", type=float, default=0.1)
    b.add_argument("--show-dev", action="store_true")
    m = sub.add_parser("sample")
    m.add_argument("--dev-dir", type=Path, required=True)
    m.add_argument("--state", type=Path)
    m.add_argument("--count", type=int, default=60)
    m.add_argument("--seed", default="1")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    from scripts import eval_live_ops as lo
    try:
        named = args.state or environ.get(lo.real_state_env())
        if not named or not Path(named).is_dir():
            raise Refused("state_missing")
        store, generation, _day = current_store(Path(named))
        if args.command == "census":
            if lo._inside_git(args.out.parent):
                raise Refused("census_inside_a_git_work_tree")
            doc = census(store, args.workers)
            doc["generation"] = generation
            _write_private(args.out, doc)
            print(json.dumps({"link_types": len(doc["samples"]),
                              "links": sum(r[3] for r in doc["counts"])}))
            return 0
        if lo._inside_git(args.dev_dir):
            raise Refused("dev_dir_inside_a_git_work_tree")
        held = lo.held_out_dir(args.dev_dir)   # refuses outside the credentialed env
        if args.command == "sample":
            pairs = lo.load_split(args.dev_dir) + lo.load_split(held)
            pairs = [p for p in pairs if p[1].get("suite") == gw.SUITE]
            print(",".join(gw.stratified(pairs, args.count, args.seed)))
            return 0
        guard, own, boxes = people_guard(Path(named), environ)
        census_path = args.census or args.dev_dir.parent / "census.json"
        if not census_path.is_file():
            raise Refused("census_missing")
        doc = json.loads(census_path.read_text("utf-8"))
        if doc.get("format") != CENSUS_FORMAT or doc.get("generation") != generation:
            raise Refused("census_of_another_generation")
        links = schema_links(doc["counts"])
        records = Store(store, doc["samples"], guard, own, boxes)
        try:
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            items, keys, stats = build(links, records, items=args.items, seed=args.seed,
                                       as_of=date.fromisoformat(_day), generation=generation,
                                       now=now, dev_share=args.dev_share)
        finally:
            records.close()
        from evals import runner
        for item in items:
            errors = runner.validate_item(item)
            if errors:
                raise Refused(f"item_invalid:{errors[0]}")
        from evals import live_ops
        for key in keys:
            problems = live_ops.key_problems(key)
            if problems:
                raise Refused(f"key_invalid:{problems[0]}")
        for split, directory in (("dev", args.dev_dir), ("held_out", held)):
            pairs = [(i, k) for i, k in zip(items, keys) if i["split"] == split]
            lo.write_split(directory, [i for i, _ in pairs], [k for _, k in pairs])
        stats.update(generation=generation, as_of=_day, seed=args.seed)
        if args.show_dev:
            stats["dev_items"] = [{"id": i["id"], "question": i["question"]["text"],
                                   "card": k["card"], "systems": k["strata"]["systems"]}
                                  for i, k in zip(items, keys) if i["split"] == "dev"]
        print(json.dumps(stats, indent=1, sort_keys=True))
        return 0
    except (Refused, lo.MailRefused, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": type(exc).__name__, "code": str(exc)[:200]}),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
