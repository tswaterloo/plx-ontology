"""The vendor-mail pass: the slow vendor checks, apart from the capture cycle.

``vendor_no_reply`` (F28) and ``vendor_terms_unapplied`` (F27) investigate each purchase
order recent mail names; on the live store that is hundreds of orders at tens of seconds
each when cold, which no post-build budget holds without keeping the cycle lock for hours.
The build's own run (``host._proactive``) therefore only reuses their readings
(``vendor_bound=0``); this pass investigates the orders whose mail thread changed
(``proactive_events._cache_valid``), newest mail first, within its own time budget, over
the served projection, outside the cycle lock, and folds the result into the same inbox
(the readings are merged, never replaced: either may fold first).

A pass first runs the whole engine (``proactive_engine.refresh``) when the projection
served is newer than the inbox's last full run (the build's own run failed, did not run or
has not run yet), so the findings Home and the briefing list follow every published
generation; meanwhile ``read_inbox`` holds back what older data found.

Schedule: as its own system unit (``heavy.sh``) after the capture cycles, e.g. every 2
hours for up to 90 minutes (``scripts/vendor_watch_loop.sh`` also starts a pass as soon as
a new generation is published). The first passes over a store work off the backlog; after that
a pass reads only the threads that changed. Read only: no reader is built for any live
source, nothing is sent or written to a business system. Usage::

    python3 -m operating.vendor_watch --state-dir STATE --config HOST.json \\
        [--budget-seconds 5400] [--bound 400] [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

DEFAULT_SECONDS = 5400
DEFAULT_BOUND = 400


def run_pass(state_dir: Path, config: Any, *, seconds: float, bound: int,
             as_of: date | None = None, environ: Mapping[str, str] | None = None,
             refresh_new: bool = True, monotonic: Callable[[], float] = time.monotonic
             ) -> dict[str, Any]:
    """One vendor-mail pass over the served projection of ``state_dir``; counts only.
    ``refresh_new``: first the full engine run (``proactive_engine.refresh``, within
    ``REFRESH_BUDGET_SECONDS`` and half the pass) when the served generation is not the
    one the inbox's last full run read (``full_refresh``: its counts, else None)."""
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor

    from . import case_workspace_live as live
    from . import po_status
    from . import proactive_engine as pe
    from .host import engine_readers, projection_path

    store = projection_path(state_dir)
    day = po_status.business_today() if as_of is None else as_of
    actor = Actor(config.tenant, frozenset({config.principal}), "restricted")
    audience = AudienceMap.from_file(config.mirror_audience)
    started = monotonic()
    # no live reader: every investigation reads the projection and the captures only, as
    # the capture principal granted what it captured (``host.engine_readers``)
    runtime = live.offline_runtime(state_dir, config.mirror_audience, config.tenant,
                                   live=engine_readers(config), environ=environ)
    full: dict[str, Any] | None = None
    try:
        if refresh_new and pe.served_behind(store):
            # a generation was served since the last full run (the build's own run did not
            # run, failed or is still to come): the whole engine runs over it now, in this
            # fresh process, so Home and the briefing never list older data's findings
            # for long (``proactive_engine.read_inbox`` hides them meanwhile)
            share = min(float(pe.REFRESH_BUDGET_SECONDS), seconds / 2)
            try:
                ran = pe.refresh(runtime, actor, audience, store, day, captures=state_dir,
                                 scan_coverage="portal" in config.sources,
                                 budget=pe.Budget(share, reserve=pe.AMOUNTS_RESERVE_SECONDS),
                                 vendor_bound=0)
            except Exception as error:  # noqa: BLE001 - the vendor checks still run
                full = {"state": "failed", "error": type(error).__name__}
            else:
                full = {"state": "ran", **{key: ran.get(key) for key in (
                    "detectors", "detector_reasons", "gaps_found")}}
        left = seconds - (monotonic() - started)
        session = live._Session(runtime, actor, audience, store, captures=state_dir)
        try:
            found = pe.run(store, pe.inbox_path(store), day, call=session,
                           budget=pe.Budget(max(left, 1.0),
                                            reserve=pe.AMOUNTS_RESERVE_SECONDS),
                           only=pe.INCREMENTAL, vendor_bound=bound)
        finally:
            session.close()
    finally:
        runtime.close()
    return {**{key: found.get(key) for key in ("detectors", "detector_reasons", "reused",
                                               "gaps_by_kind", "not_evaluated")},
            "full_refresh": full}


def main(argv: Sequence[str] | None = None) -> int:
    from .host import HostConfig

    parser = argparse.ArgumentParser(prog="python3 -m operating.vendor_watch",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument("--bound", type=int, default=DEFAULT_BOUND)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--no-refresh-new", action="store_true",
                        help="never run the full engine first, even on a new generation")
    args = parser.parse_args(argv)
    if args.budget_seconds <= 0 or args.bound < 0:
        parser.error("budget and bound must be positive")
    config = HostConfig.from_file(args.config)
    print(json.dumps(run_pass(args.state_dir, config, seconds=args.budget_seconds,
                              bound=args.bound, as_of=args.as_of,
                              refresh_new=not args.no_refresh_new),
                     indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
