"""The vendor-mail pass: the slow vendor checks, apart from the capture cycle.

``vendor_terms_unapplied`` (F27) investigates each purchase order recent mail names (on the
host's process pool, ``operating.proactive_pool``; ``vendor_no_reply`` is a set-based rule
of the build's own run); on the live store that is hundreds of orders at tens of seconds
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

``--refresh calling`` (the scheduled pass since the incremental store-only passes,
``operating.proactive_incremental``): the whole engine when the served generation or code
is new to the inbox, else only the tool-call rules and the customer PO scan
(``CALLING_PASS``); ``--refresh always`` (the nightly pass) runs the whole engine first on
every pass, whatever the inbox's last
full run read (the day moves on, a run cut short or failed is made whole): the scheduled
pass (``deploy/systemd/plx-proactive-nightly.timer``, nightly; the 2-hourly
``plx-proactive.timer`` runs ``--refresh calling``)
gives the engine ``--refresh-seconds`` and the vendor checks what is left.

Schedule: ``deploy/systemd/plx-proactive.{service,timer}`` (every 2 hours after the last
pass ended, ``--refresh calling``) and ``plx-proactive-nightly.{service,timer}`` (03:30 ET,
``--refresh always``: the safety net that re-reads every record); the store-only rules
follow every delta cycle in ``plx-proactive-inc`` (``operating.proactive_incremental``).
``scripts/vendor_watch_loop.sh`` is the older loop (every 2 hours, and as soon as
a new generation is published, ``--refresh new``). The first passes over a store work off
the backlog; after that a pass reads only the threads that changed. Read only: no reader is
built for any live source, nothing is sent or written to a business system. Usage::

    python3 -m operating.vendor_watch --state-dir STATE --config HOST.json \\
        [--budget-seconds 5400] [--bound 400] [--as-of YYYY-MM-DD] \\
        [--refresh new|always|never] [--refresh-seconds S]
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

DEFAULT_SECONDS = 5400
# the rules ``--refresh calling`` runs whole when the served data and code are current: the
# tool-call rules (but the vendor-terms check, run incrementally below) and the customer PO
# scan of the Portal mirror
CALLING_PASS = frozenset({"orders_at_risk", "card_rules", "negative_margin",
                          "margin_erosion", "order_without_customer_po"})
DEFAULT_BOUND = 400
# the rules ``--refresh whole`` runs (``plx-proactive-whole.timer``, every 4 hours): the
# store-only rules the incremental pass leaves to a full pass (they read every record of
# their types), so their alerts are at most 4 hours old, not a day
WHOLE_PASS = frozenset({"stock_out_risk", "overdue_receivable", "unit_cost_jump",
                        "customer_demand_drop", "promise_date_slip", "supplier_reliability",
                        "shipped_not_invoiced", "duplicate_customer_order",
                        "recurring_component_damage", "customer_chase", "quality_holds",
                        "expedite_requests", "supplier_shortage", "payment_chase",
                        "customer_payment_behaviour", "early_pay_discounts",
                        "duplicate_payables", "returns_and_credits",
                        "price_consistency",
                        "mc_task_overdue", "mc_task_stuck",
                        "mc_approval_pending", "planner_task_overdue",
                        "aged_balance_worsening", "production_late",
                        "assembly_damage_spike", "teams_unanswered_ask",
                        "commitments", "needs_reply", "slow_moving_stock", "expiring_lot_stock",
                        "approvals_waiting", "chase_escalation", "party_chases", "internal_chases",
                        "assembly_component_shortfall", "certificate_expiry",
                        "freight_cost_anomaly", "stale_quote", "priority_fill_unmet",
                        "duplicate_parties",
                        "key_fields_missing", "twin_disagreement", "stale_captures", "sync_gaps",
                        "reorder_soon",
                        "inventory_adjustment", "blocked_account_activity",
                        "gl_posting_errors", "recurring_task_missed",
                        "cash_application", "statement_reconciliation"})
REFRESH_MODES = ("new", "always", "never", "calling", "whole")


def run_pass(state_dir: Path, config: Any, *, seconds: float, bound: int,
             as_of: date | None = None, environ: Mapping[str, str] | None = None,
             refresh_new: bool = True, monotonic: Callable[[], float] = time.monotonic,
             refresh: str | None = None, refresh_seconds: float | None = None
             ) -> dict[str, Any]:
    """One vendor-mail pass over the served projection of ``state_dir``; counts only.
    ``refresh``: ``new`` (the default; ``refresh_new=False``: ``never``) runs the full
    engine first (``proactive_engine.refresh``) when the served generation is not the one
    the inbox's last full run read, ``always`` on every pass; within ``refresh_seconds``
    (default half the pass). ``full_refresh``: its counts and each detector's seconds,
    else None."""
    mode = refresh or ("new" if refresh_new else "never")
    if mode not in REFRESH_MODES:
        raise ValueError("refresh_mode_unknown")
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor

    from . import case_workspace_live as live
    from . import po_status
    from . import proactive_engine as pe
    from . import proactive_pool
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
    # every core the host can spare reads as the same principal, offline (``proactive_pool``)
    pool = proactive_pool.from_host_config(config, state_dir, store, environ)
    try:
        behind = mode in ("new", "calling", "whole") and pe.served_behind(store)
        if mode == "always" or behind or mode in ("calling", "whole"):
            # a generation was served since the last full run (the build's own run did not
            # run, failed or is still to come): the whole engine runs over it now, in this
            # fresh process, so Home and the briefing never list older data's findings
            # for long (``proactive_engine.read_inbox`` hides them meanwhile)
            # outside the cycle lock: half the pass, not the build's bound
            # (``REFRESH_BUDGET_SECONDS``), so every rule finishes on a cold store
            share = min(seconds, refresh_seconds) if refresh_seconds else seconds / 2
            # ``calling``: when nothing is behind, only the rules the incremental pass
            # leaves to this one (the tool-call rules and the Portal mirror's customer PO
            # scan); the store-only rules follow every delta cycle there
            # (``operating.proactive_incremental``)
            only = (sorted(CALLING_PASS) if mode == "calling" and not behind
                    else sorted(WHOLE_PASS) if mode == "whole" and not behind else None)
            try:
                ran = pe.refresh(runtime, actor, audience, store, day, captures=state_dir,
                                 scan_coverage="portal" in config.sources,
                                 budget=pe.Budget(share, reserve=pe.AMOUNTS_RESERVE_SECONDS),
                                 vendor_bound=0, pool=pool, only=only)
            except Exception as error:  # noqa: BLE001 - the vendor checks still run
                full = {"state": "failed", "error": type(error).__name__}
            else:
                full = {"state": "ran", **{key: ran.get(key) for key in (
                    "detectors", "detector_reasons", "gaps_found", "seconds")}}
        left = seconds - (monotonic() - started)
        session = live._Session(runtime, actor, audience, store, captures=state_dir,
                                marking_grants=pe.engine_marking_grants(actor))
        try:
            found = pe.run(store, pe.inbox_path(store), day, call=session,
                           budget=pe.Budget(max(left, 1.0),
                                            reserve=pe.AMOUNTS_RESERVE_SECONDS),
                           only=pe.INCREMENTAL, vendor_bound=bound, pool=pool)
            # recorded meetings follow themselves up here, as the same reader (a no-op
            # without a captured transcript or an export: operating.meeting_followup.cycle)
            try:
                from . import meeting_followup
                found = {**found, "meetings": meeting_followup.cycle(
                    state_dir, session, environ=environ, today=day)}
            except Exception as error:  # noqa: BLE001 - the alerts stand without it
                found = {**found, "meetings": {"failed": type(error).__name__}}
        finally:
            session.close()
    finally:
        if pool is not None:
            pool.close()
        runtime.close()
    return {**{key: found.get(key) for key in ("detectors", "detector_reasons", "reused",
                                               "gaps_by_kind", "not_evaluated", "meetings")},
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
    parser.add_argument("--refresh", choices=REFRESH_MODES, default=None,
                        help="run the full engine first: on a new generation (new, the "
                             "default), on every pass (always) or never")
    parser.add_argument("--refresh-seconds", type=float, default=None,
                        help="the full engine run's budget (default half the pass)")
    args = parser.parse_args(argv)
    if args.budget_seconds <= 0 or args.bound < 0 or (
            args.refresh_seconds is not None and args.refresh_seconds <= 0):
        parser.error("budget and bound must be positive")
    config = HostConfig.from_file(args.config)
    print(json.dumps(run_pass(args.state_dir, config, seconds=args.budget_seconds,
                              bound=args.bound, as_of=args.as_of,
                              refresh_new=not args.no_refresh_new, refresh=args.refresh,
                              refresh_seconds=args.refresh_seconds),
                     indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
