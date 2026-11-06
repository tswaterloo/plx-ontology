"""Read and keep the store facts of a projection store file, each fact on its own core.

``python3 -m operating.store_facts [STORE] [--workers N]``: the facts every reader's
``plx.around`` / ``plx.timeline`` needs (``ontology_tools_traverse.prime``: the audience
keys, each type's audiences, the event types, the object, link and event slices, the
document families' cell spans), read from ``STORE`` (default the served projection of
``/data/plx-state``) read-only, one process per fact, and kept beside the file
(``ontology_tools_traverse.facts_path``) for every later process that opens it. A build
of the host keeps them beside each generation it serves (``operating.host``); this is for
a store served before that, or one whose facts code changed since.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from operating import ontology_tools_evidence, ontology_tools_traverse

DEFAULT_STORE = "/data/plx-state/projection/ontology.sqlite"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store", nargs="?", default=DEFAULT_STORE)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    started = time.monotonic()
    done = ontology_tools_traverse.prime_store_facts(
        args.store, ontology_tools_evidence.STORE_DOCUMENT_FAMILIES,
        workers=max(1, args.workers))
    if not done:
        print(f"no facts kept for {args.store}", file=sys.stderr)
        return 1
    print(f"primed in {time.monotonic() - started:.0f}s; facts at",
          ontology_tools_traverse.facts_path(args.store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
