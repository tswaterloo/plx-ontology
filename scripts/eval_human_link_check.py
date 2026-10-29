"""Live check ``devloop/live-checks/case.human_link.json``: the landed-cost case reads
the links people confirmed in the console (``operating.human_links``).

``python3 -m scripts.eval_human_link_check check`` copies the console's review store
(``--reviews-from``, default ``PLX_WORKBENCH_STATE_DIR``; read only, through a SQLite
backup of a ``mode=ro`` open) into the check's own state directory, runs the landed-cost
case (``scripts.eval_packaging_oracle_check landed-cost``: capture, projection with those
reviews, investigation as the reader, the independent oracle) and reports each missing
link with the exact confirm step a person must take, the human links the build
projected and the supplier-alias join. ``blocked`` names the confirm step
(``confirm_link:<LinkType>``, ``confirm_supplier_alias``) or another typed gap; the case
answered with nothing left to confirm is null. Counts and enums only; nothing is written
to the console's store or to any business system.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from operating.human_links import ALIAS_STATES, GRANT, MISSING_LINKS, WHERE

SCHEMA = "plx-human-link-check/v1"
STATE_ENV = "PLX_WORKBENCH_STATE_DIR"


def copy_reviews(source_dir: Path | None, state_dir: Path) -> tuple[str, dict[str, Any]]:
    """The console's review store copied into the check's state directory, read only
    (``mode=ro``): ("copied", counts of the links (per link type) and aliases in force)
    or ("none", {}) when there is none. A state directory that already holds one is refused."""
    from ontology import reviews

    target = reviews.store_path(state_dir)
    if target.exists():
        raise ValueError("state_dir_holds_reviews")
    source = reviews.store_path(source_dir) if source_dir is not None else None
    if source is None or not source.is_file():
        return "none", {}
    state_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro",
                                            uri=True)) as read, \
            contextlib.closing(sqlite3.connect(target)) as write:
        read.backup(write)
    from operating.build_steps import ReviewsIfPresent
    held = ReviewsIfPresent(target, strict=True)
    in_force: dict[str, Any] = {"links": 0, "aliases": 0, "link_types": {}}
    for tenant in _tenants(target):
        for record in reviews.effective(held.link_history(tenant)).values():
            if record["decision"] == "accept_link":
                in_force["links"] += 1
                types = in_force["link_types"]
                types[record["link_type"]] = types.get(record["link_type"], 0) + 1
        aliases = reviews.effective(held.alias_history(tenant, None)).values()
        in_force["aliases"] += sum(1 for r in aliases if r["decision"] == "alias")
    return "copied", in_force


def _tenants(path: Path) -> list[str]:
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                            uri=True)) as db:
        return [row[0] for row in db.execute("SELECT DISTINCT tenant FROM reviews")]


def _gaps(check: Mapping[str, Any], copied: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each missing link the oracle reports, with the confirm step a person must take; a
    missing-link code no longer reported while a person's link is in force, answered."""
    out = []
    for missing in check.get("missing") or []:
        link_type = MISSING_LINKS.get(missing.get("code"))
        if link_type is not None:
            out.append({"code": missing["code"], "pin": missing.get("pin"),
                        "state": "confirm_needed",
                        "confirm_step": {"decision": "accept_link", "link_type": link_type,
                                         "grant": GRANT, "where": WHERE}})
    reported = set(check.get("missing_codes") or [])
    if check.get("status") in ("agree", "missing"):
        for code, link_type in sorted(MISSING_LINKS.items()):
            if code not in reported and (copied.get("link_types") or {}).get(link_type):
                out.append({"code": code, "pin": None, "state": "answered",
                            "link_type": link_type, "confirm_step": None})
    return out


def check_main(argv: Sequence[str], *, environ: Mapping[str, str],
               host_options: Mapping[str, Any] | None = None) -> int:
    from scripts import eval_packaging_oracle_check as harness

    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_human_link_check check",
                                     add_help=False)
    parser.add_argument("--reviews-from", type=Path, default=None)
    parser.add_argument("--state-dir", required=True, type=Path)
    known, rest = parser.parse_known_args(argv)
    source = known.reviews_from or (Path(environ[STATE_ENV]) if environ.get(STATE_ENV)
                                    else None)
    try:
        reviews_source, copied = copy_reviews(source, known.state_dir)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, ValueError)
                          else type(exc).__name__}), file=sys.stderr)
        return 1
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = harness.main(["landed-cost", "--state-dir", str(known.state_dir), *rest],
                            environ=environ, host_options=host_options)
    try:
        landed = json.loads(out.getvalue())
    except ValueError:
        print(json.dumps({"error": "landed_cost_no_report"}), file=sys.stderr)
        return code or 1
    check = landed.get("oracle_check") or {}
    projection = (landed.get("capture") or {}).get("projection") or {}
    outcome = (projection.get("reference_alias") or {}).get("outcome")
    alias_state = ALIAS_STATES.get(outcome, "not_applicable")
    gaps = _gaps(check, copied)
    blocked = None
    if landed.get("case", {}).get("ok") and check.get("status") == "missing":
        needed = [g for g in gaps if g["state"] == "confirm_needed"]
        blocked = (f"confirm_link:{needed[0]['confirm_step']['link_type']}" if needed
                   else landed["case"].get("missing"))
    elif check.get("status") == "agree" and alias_state == "confirm_needed":
        blocked = "confirm_supplier_alias"
    print(json.dumps({
        "schema": SCHEMA, "evidence_label": landed.get("evidence_label"),
        "adapter_status": "live-unverified", "reviews_source": reviews_source,
        "reviews_copied": copied,
        "landed_cost": {"case": landed.get("case"), "oracle_check": check,
                        "capture": landed.get("capture")},
        "human_link": {"gaps": gaps, "projected": projection.get("reviews") or {},
                       "supplier_alias": {
                           "outcome": outcome, "state": alias_state,
                           "confirm_step": {"decision": "alias", "grant": GRANT,
                                            "where": WHERE}
                           if alias_state == "confirm_needed" else None}},
        "blocked": blocked, "blocked_upstream": landed.get("blocked_upstream") or []},
        indent=1, sort_keys=True))
    return code


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] != ["check"]:
        print("usage: python3 -m scripts.eval_human_link_check check --state-dir DIR "
              "[--reviews-from DIR] <landed-cost arguments>", file=sys.stderr)
        return 2
    return check_main(argv[1:], environ=os.environ if environ is None else environ,
                      host_options=host_options)


if __name__ == "__main__":
    raise SystemExit(main())
