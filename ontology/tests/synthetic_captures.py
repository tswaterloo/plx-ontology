"""Synthetic L1 captures for any compiled schema (test and benchmark support only).

Not business data and never a live result: the rows are generated from the schema's own
declarations by ``ontology/generator/fixture.py`` (re-exported here), so the projector's
scale behaviour can be measured without a source.

    python3 -m ontology.tests.synthetic_captures --rows 1000 [--old PATH] [--tracemalloc]
        [--source FAMILY ...]

prints one JSON line: counts, build seconds, peak memory and the content digest.

    python3 -m ontology.tests.synthetic_captures --rows 1000 --state DIR [--change REL ...]

times ``ontology.project_incremental.build_generation`` instead: a cold generation into
DIR (unless it holds one), then one generation per ``--change`` in which record 3 of one
more relation holds other text (``changed``), then an unchanged rebuild; one JSON line
per step; ``--eval-product`` times the eval.unfamiliar_sample product build instead
(the Portal limited to the relations that eval mirrors, mention links included).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ontology.generator.fixture import (  # noqa: F401 - re-exported for the tests
    FAMILY_SCHEMES, SYNTHETIC_PATH, T0, T1, SyntheticCaptures, is_synthetic, min_rows, sources)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ontology.tests.synthetic_captures")
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument("--old", help="path of another project.py to build with instead")
    parser.add_argument("--tracemalloc", action="store_true")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--state", help="time build_generation into this state dir")
    parser.add_argument("--change", action="append", default=[])
    parser.add_argument("--source", action="append", default=[],
                        help="only this capture family (portal_api, bc, graph_mail, ...)")
    parser.add_argument("--eval-product", action="store_true",
                        help="with --state: the eval.unfamiliar_sample product build (its "
                             "Portal relations only, mention links)")
    args = parser.parse_args(argv)
    import resource

    from ontology.schema import load_schema

    if args.state:
        return generations(load_schema(), args.rows, Path(args.state), args.change,
                           args.source, args.eval_product)

    if args.old:
        import importlib.util
        spec = importlib.util.spec_from_file_location("project_under_test", args.old)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)   # type: ignore[union-attr]
        build_store = module.build_store
    else:
        from ontology.project import build_store
    schema = load_schema()
    if args.tracemalloc:
        import tracemalloc
        tracemalloc.start()
    options = {} if args.batch_size is None else {"batch_size": args.batch_size}
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        report = build_store(Path(tmp) / "store.sqlite", schema, sources(schema, args.rows, only=args.source),
                             clock=lambda: __import__("datetime").datetime(
                                 2026, 1, 3, tzinfo=__import__("datetime").timezone.utc),
                             **options)
    seconds = time.monotonic() - started
    peak = None
    if args.tracemalloc:
        import tracemalloc
        peak = round(tracemalloc.get_traced_memory()[1] / 2 ** 20, 1)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"rows_per_relation": args.rows, "sources": sorted(args.source) or "all",
                      "counts": {k: report["counts"].get(k) for k in (
                          "objects", "versions", "values", "links", "identifiers", "events")},
                      "seconds": round(seconds, 1), "tracemalloc_peak_mb": peak,
                      "maxrss_mb": round(rss / (1024 * 1024 if sys.platform == "darwin"
                                                else 1024), 1),
                      "content_digest": report["content_digest"],
                      "report_digest": __import__("hashlib").sha256(json.dumps(
                          {k: report[k] for k in ("counts", "gaps")},
                          sort_keys=True).encode()).hexdigest()}, sort_keys=True))
    return 0


class _EvalMirror:
    """The unfamiliar-sample eval's Portal mirror: only the relations it syncs
    (``required_relations`` | ``process_relations``) are captured."""

    def __init__(self, inner: SyntheticCaptures, schema: Any) -> None:
        from scripts import eval_unfamiliar_sample as ev
        self.inner, self.source = inner, inner.source
        self.held = set(ev.required_relations(schema, ev.root_relations())) \
            | set(ev.process_relations(schema))

    def records(self, relations: Sequence[str]) -> Mapping[str, Any]:
        held = self.inner.records([r for r in relations if r in self.held])
        return {r: held[r] if r in self.held else iter(()) for r in relations}

    def capture_gap(self, relation: str) -> str | None:
        return None if relation in self.held else "source_not_captured"

    def describe(self) -> dict[str, Any]:
        return self.inner.describe()


def generations(schema: Any, rows: int, state: Path, changes: Sequence[str],
                only: Sequence[str] = (), eval_product: bool = False) -> int:
    import resource
    from datetime import datetime, timezone

    from ontology import project_incremental as inc
    from ontology.mentions import store_mention_links

    steps = [] if inc.current_generation(state) else [("cold", 0)]
    steps += [(f"changed:{relation}", n + 1) for n, relation in enumerate(changes)]
    steps.append(("unchanged", len(changes)))
    for name, changed in steps:
        built = sources(schema, rows, changes[:changed], only)
        if eval_product and "portal_api" in built:
            built["portal_api"] = _EvalMirror(built["portal_api"], schema)
        started = time.monotonic()
        generation = inc.build_generation(
            state, schema, built, clock=lambda: datetime(2026, 1, 3, tzinfo=timezone.utc),
            post_project={"mentions": store_mention_links} if eval_product else None)
        print(json.dumps({
            "step": name, "rows_per_relation": rows, "sources": sorted(only) or "all",
            "eval_product": eval_product,
            "seconds": round(time.monotonic() - started, 1), "phases": generation.seconds,
            "mode": generation.mode, "units_rebuilt": generation.units_rebuilt,
            "units": generation.units, "bindings_read": generation.bindings_read,
            "objects": generation.report["counts"]["objects"],
            "maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            "content_digest": generation.content_digest}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
