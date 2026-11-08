"""Read parity of the Postgres ontology with the served SQLite generation and its delta
overlay, through the host's own tools: counts, timings and differing ids only.

    python3 -m scripts.pg_read_parity --config ~/.config/plx/workbench-host-vince/host.json \\
        [--state-dir /data/plx-state] [--sample 10000] [--workers 8] [--tools ...]

For ``--sample`` objects drawn at random from the Postgres ontology (seeded), each tool is
called with the same arguments by the same readers on both stores (``Host.served_tools``:
the case host with its grants, markings, item gates and people exclusion; no live source
reads on either side), once with ``PLX_ONTOLOGY_PG`` off (the served generation and the
delta overlay) and once on:

* ``plx.object`` {object_id}; ``plx.find`` {query: an identifier of it (primary first), kind};
* ``plx.around`` {from, path: one link type its type declares}; ``plx.timeline`` {from};
* ``plx.read`` {citation: its current version's handle};
* ``plx.query`` {from: {object_id}, traverse: that hop, measures: count}.

Readers: the host's capture principal, a reader granted some sources and no marking
(``--restricted``), and an outsider with no grant. Answers are compared as JSON with the
fields that differ by nature removed (``meta`` timings, cursors and set handles); the
report gives, per tool and reader, equal / differing / errored counts, the first differing
object ids and the parts that differ (keys, never values), and per store p50 / p95 / max
latency. Nothing is written.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time
from typing import Any, Sequence

VOLATILE = {"meta", "cursor", "next_cursor", "set", "set_handle", "handle", "timings",
            "elapsed_ms", "served_at", "as_of", "freshness", "warm"}
TOOLS = ("plx.object", "plx.find", "plx.around", "plx.timeline", "plx.read", "plx.query")


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in sorted(value.items()) if k not in VOLATILE}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _where(a: Any, b: Any, path: str = "") -> str:
    """The first key path where two answers differ (keys only)."""
    if type(a) is not type(b):
        return path or "/"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}/{key}"
            if a[key] != b[key]:
                return _where(a[key], b[key], f"{path}/{key}")
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}[len]"
        for n, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return _where(x, y, f"{path}[{n}]")
    return path or "/"


def _sample(dsn: str, size: int, seed: int) -> list[dict[str, Any]]:
    import psycopg
    rng = random.Random(seed)
    with psycopg.connect(dsn) as pg:
        top = pg.execute("SELECT count(*) FROM ontology.objects").fetchone()[0]
        picks: list[dict[str, Any]] = []
        offsets = sorted(rng.sample(range(top), min(size, top)))
        # one ordered pass: the n-th object by id for each sampled n
        cur = pg.cursor(name="sample")
        cur.itersize = 50_000
        cur.execute("SELECT object_id, object_type FROM ontology.objects ORDER BY object_id")
        want = iter(offsets)
        nxt = next(want, None)
        for n, (oid, otype) in enumerate(cur):
            if nxt is None:
                break
            if n == nxt:
                picks.append({"object_id": oid, "object_type": otype})
                nxt = next(want, None)
        cur.close()
        ids = [p["object_id"] for p in picks]
        primary = dict(pg.execute(
            "SELECT DISTINCT ON (object_id) object_id, json_build_array(identifier_kind, "
            "normalized)::text FROM ontology.identifiers WHERE object_id = ANY(%s) AND "
            "superseded_at IS NULL ORDER BY object_id, role <> 'primary', identifier_kind",
            (ids,)).fetchall())
        handles = dict(pg.execute(
            "SELECT object_id, l1_handle FROM ontology.object_versions WHERE object_id = ANY(%s) "
            "AND superseded_at IS NULL", (ids,)).fetchall())
    for p in picks:
        ident = primary.get(p["object_id"])
        p["identifier"] = json.loads(ident) if ident else None
        p["handle"] = handles.get(p["object_id"])
    return picks


def _calls(pick: dict[str, Any], hops: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    oid = pick["object_id"]
    hop = (hops.get(pick["object_type"]) or [None])[0]
    out: dict[str, Any] = {"plx.object": {"object_id": oid}, "plx.timeline": {"from": oid}}
    if pick["identifier"]:
        kind, value = pick["identifier"]
        out["plx.find"] = {"query": value, "identifier_kind": kind}
    if hop:
        out["plx.around"] = {"from": oid, "path": [hop]}
        out["plx.query"] = {"from": {"object_id": oid}, "traverse": [hop],
                            "measures": [{"op": "count"}]}
    if pick["handle"]:
        out["plx.read"] = {"citation": pick["handle"], "max_chars": 2000}
    return out


def _worker(args: tuple) -> dict[str, Any]:
    config_path, state_dir, picks, tools, restricted, pg_mode = args
    from ontology.types import Actor
    from operating.host import Host, HostConfig

    class QuietHost(Host):
        def _live_readers(self) -> Any:   # no live source on either side
            return None

    config = HostConfig.from_file(config_path)
    readers = {"capture": Actor(config.tenant, frozenset({config.principal}), "restricted"),
               "outsider": Actor(config.tenant, frozenset({"probe:no-grant"}), "restricted")}
    if restricted:
        readers["restricted"] = Actor(config.tenant, frozenset({restricted}), "restricted")
    from operating.ontology_tools import default_schema
    schema = default_schema()
    hops: dict[str, list[dict[str, str]]] = {}
    for name, link in sorted((schema.link_types if schema else {}).items()):
        hops.setdefault(link["from"], []).append({"link_type": name, "direction": "forward"})
        hops.setdefault(link["to"], []).append({"link_type": name, "direction": "reverse"})
    results: dict[str, Any] = {}
    host = QuietHost(Path(state_dir), config)
    stacks = {}
    for store in ("sqlite", "pg"):
        if store == "pg":
            os.environ["PLX_ONTOLOGY_PG"] = pg_mode
        else:
            os.environ.pop("PLX_ONTOLOGY_PG", None)
        for who, actor in readers.items():
            context = host.served_tools(actor)
            tools_ = context.__enter__()
            tools_.call("plx.object", {"object_id": picks[0]["object_id"]}, actor)  # build
            reader = type(getattr(getattr(tools_, "tools", None), "store", None)).__name__
            if (store == "pg") != (reader == "PgStoreReader"):
                raise RuntimeError(f"{store} side reads {reader}: Postgres not ready?")
            stacks[(store, who)] = (context, tools_, actor)
    os.environ.pop("PLX_ONTOLOGY_PG", None)
    for pick in picks:
        for name, arguments in _calls(pick, hops).items():
            if name not in tools:
                continue
            for who in readers:
                answers, seconds = {}, {}
                for store in ("sqlite", "pg"):
                    _, caller, actor = stacks[(store, who)]
                    if store == "pg":   # the mode is read per call (``pg_only``)
                        os.environ["PLX_ONTOLOGY_PG"] = pg_mode
                    else:
                        os.environ.pop("PLX_ONTOLOGY_PG", None)
                    started = time.monotonic()
                    try:
                        answers[store] = _clean(caller.call(name, dict(arguments), actor))
                    except Exception as exc:  # noqa: BLE001 - counted
                        answers[store] = {"exception": type(exc).__name__}
                    seconds[store] = time.monotonic() - started
                entry = results.setdefault(f"{name}|{who}", {
                    "equal": 0, "differ": 0, "errored_pg": 0, "differing": [], "where": {},
                    "seconds": {"sqlite": [], "pg": []}})
                for store in seconds:
                    entry["seconds"][store].append(seconds[store])
                if "exception" in answers["pg"]:
                    entry["errored_pg"] += 1
                if answers["sqlite"] == answers["pg"]:
                    entry["equal"] += 1
                else:
                    entry["differ"] += 1
                    spot = _where(answers["sqlite"], answers["pg"])
                    entry["where"][spot] = entry["where"].get(spot, 0) + 1
                    if len(entry["differing"]) < 5:
                        entry["differing"].append(pick["object_id"])
    for context, _, _ in stacks.values():
        context.__exit__(None, None, None)
    return results


def _merge(parts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part in parts:
        for key, entry in part.items():
            held = out.setdefault(key, {"equal": 0, "differ": 0, "errored_pg": 0,
                                        "differing": [], "where": {},
                                        "seconds": {"sqlite": [], "pg": []}})
            for field in ("equal", "differ", "errored_pg"):
                held[field] += entry[field]
            held["differing"] = (held["differing"] + entry["differing"])[:10]
            for spot, n in entry["where"].items():
                held["where"][spot] = held["where"].get(spot, 0) + n
            for store in ("sqlite", "pg"):
                held["seconds"][store] += entry["seconds"][store]
    for entry in out.values():
        for store, values in list(entry["seconds"].items()):
            values = sorted(values)
            entry["seconds"][store] = {
                "n": len(values),
                "p50_ms": round(1000 * statistics.median(values), 1) if values else None,
                "p95_ms": round(1000 * values[int(0.95 * (len(values) - 1))], 1)
                if values else None,
                "max_ms": round(1000 * values[-1], 1) if values else None}
        entry["where"] = dict(sorted(entry["where"].items(), key=lambda kv: -kv[1])[:8])
    return out


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config as pg_config

    parser = argparse.ArgumentParser(prog="python3 -m scripts.pg_read_parity")
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", default="/data/plx-state")
    parser.add_argument("--sample", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--tools", nargs="*", default=list(TOOLS))
    parser.add_argument("--restricted", default=None,
                        help="a principal granted some sources and no marking")
    parser.add_argument("--out", default=None)
    parser.add_argument("--pg-mode", choices=("1", "only"), default="1",
                        help="PLX_ONTOLOGY_PG on the Postgres side: 1 (word index from the "
                             "SQLite generation) or only (Postgres the only store)")
    parser.add_argument("--batch", type=int, default=100,
                        help="objects per worker task (the report is written after each)")
    args = parser.parse_args(argv)
    started = time.monotonic()
    picks = _sample(pg_config.dsn(), args.sample, args.seed)
    # batches of objects, the report rewritten after each one: a run stopped part way
    # (an outage, the memory guard) keeps what it measured
    size = max(1, min(args.batch, -(-len(picks) // args.workers)))
    chunks = [picks[n:n + size] for n in range(0, len(picks), size)]
    parts: list[dict[str, Any]] = []
    done = 0

    def write() -> str:
        report = {"sample": len(picks), "objects_done": done,
                  "seconds": round(time.monotonic() - started, 1), "tools": _merge(parts)}
        text = json.dumps(report, sort_keys=True, indent=1)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        return text

    with futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(_worker, (args.config, args.state_dir, chunk, set(args.tools),
                                      args.restricted, args.pg_mode)): len(chunk) for chunk in chunks}
        for job in futures.as_completed(jobs):
            parts.append(job.result())
            done += jobs[job]
            write()
    print(write())
    return 0


if __name__ == "__main__":
    sys.exit(main())
