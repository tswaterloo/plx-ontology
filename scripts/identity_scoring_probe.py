"""Live check of weighted identity resolution (ontology.identity_scoring): counts only.

    python3 -m scripts.identity_scoring_probe --db DIR/mirror.sqlite \\
        --store DIR/ontology.sqlite [--budget-seconds N] [--score-budget-seconds N] \\
        [--workers N] [--bc-capture] [--live-audience FILE] \\
        [--previous-report DAILY.json] [--session-projection SESSION_DIR]

GET only. The projection probe (``scripts.ontology_projection_probe --build-only``, as
``scripts.process_dependencies_probe`` runs it: one build, a failed build's
``build_failure``, a refused one's ``need_bytes`` and ``free_bytes``) builds or reuses the
throwaway mirror and projection store and lays them out as the served host state. Then,
offline over that state and without any client, ``ontology.identity_scoring.score`` runs
every kind (``KINDS``) on a read-only connection to the served projection as the served
host's capture principal, under the scope the served tools give it (``Scope.of_tools``:
the policy each call is served with, its markings and item gate). Recorded per kind:

* ``records``, ``systems``, ``pairs_compared``, ``bands`` (``identity``, ``rejected``,
  ``review``, ``below_review``), ``identity_links`` by tier, ``review_total`` and
  ``review_offered`` (candidates carrying a ``plx.review`` payload), ``not_reviewable``
  by reason, ``blocks_capped`` and ``pairs_capped``;
* ``evaluation``: the two-fold evaluation on the reader's identity pairs (m never from the
  pair's own fold): ``precision_reviewed`` (a person's rejections as negatives),
  ``precision_with_closure``, ``coverage`` and ``blocking_recall``, with their counts;
* ``portal_bc``: the same for the party kinds' Portal-BC pairs (``by_sources``
  ``bc+portal_api``), and ``portal_bc_pairs`` / ``portal_bc_review`` /
  ``portal_bc_identity_pairs`` their totals.

Must be 0: ``kinds_unscored``, ``train_eval_overlap``, ``payload_failures`` (a review
payload that is not a valid ``plx.review`` record call for the candidate it is on, or a
candidate with neither payload nor reason), ``below_threshold_offered`` (a candidate under ``REVIEW_PROBABILITY``),
``links_written`` (changes made through the scoring connection) and ``scoring_errors``
(a kind that raised, by class in ``error_codes``). With nothing projected the probe prints
``sources_blocked`` and measures nothing. Output: counts, shares, booleans and kind, band,
tier and source names only; never a value, key, identifier or citation.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence

PROBE = "identity-scoring/v1"
PORTAL_BC = "bc+portal_api"
_METRICS = ("identity_pairs", "rejected_pairs", "true_positives",
            "false_positives_rejected", "false_positives_closure", "precision_reviewed",
            "precision_with_closure", "coverage", "blocking_recall")


def _payload_failures(kind: Mapping[str, Any], contract: Mapping[str, Any],
                      contracts: Mapping[str, Any]) -> int:
    from ontology.json_contract import validate

    failures = 0
    for candidate in kind["candidates"]:
        review = candidate.get("review")
        if review is None:
            failures += not candidate.get("not_reviewable")
            continue
        failures += bool(validate(dict(review, mode="record"), contract, root=contracts,
                                  limit=1)) or any(
            review[k] != candidate[k] for k in ("subject", "target", "link_type"))
    return failures


def check_identity(host: Any, *, budget_seconds: float | None) -> dict[str, Any]:
    """Score every kind through the served ``host`` as its capture principal; counts only
    (module doc)."""
    from ontology import identity_scoring as ids
    from operating.host import projection_path
    from scripts.ontology_projection_probe import reader_actor

    started = time.monotonic()
    report: dict[str, Any] = {
        "kinds_scored": [], "records": {}, "systems": {}, "pairs_compared": {}, "bands": {},
        "identity_links": {}, "review_total": {}, "review_offered": {}, "not_reviewable": {},
        "blocks_capped": {}, "pairs_capped": {}, "evaluation": {}, "portal_bc": {},
        "portal_bc_pairs": 0, "portal_bc_review": 0, "portal_bc_identity_pairs": 0,
        "train_eval_overlap": 0, "payload_failures": 0, "below_threshold_offered": 0,
        "scoring_errors": 0, "error_codes": {}, "links_written": 0,
        "budget_exhausted": False}
    store = projection_path(host.state_dir)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, check_same_thread=False)
    reader = reader_actor(host)
    try:
        with host.served_tools(reader) as served:
            contracts = served.tools.contracts
            contract = contracts["tools"]["plx.review"]["input"]
            for name in ids.KINDS:
                left = None if budget_seconds is None else \
                    budget_seconds - (time.monotonic() - started)
                if left is not None and left <= 0:
                    report["budget_exhausted"] = True
                    break
                served.tools.policy = served.policy_now(reader)   # as a served call
                before = conn.total_changes
                try:
                    out = ids.score(conn, served.tools.schema,
                                    ids.Scope.of_tools(served.tools, reader), kinds=[name],
                                    budget_seconds=left)
                except Exception as exc:  # noqa: BLE001 - counted by class, others go on
                    report["scoring_errors"] += 1
                    codes = report["error_codes"]
                    codes[type(exc).__name__] = codes.get(type(exc).__name__, 0) + 1
                    continue
                finally:
                    report["links_written"] += conn.total_changes - before
                report["budget_exhausted"] |= out["budget_exhausted"]
                if name not in out["kinds"]:
                    continue
                kind = out["kinds"][name]
                report["kinds_scored"].append(name)
                for key in ("records", "systems", "pairs_compared", "review_total",
                            "blocks_capped", "pairs_capped"):
                    if key in kind:
                        report[key][name] = kind[key]
                report["bands"][name] = kind["bands"]
                report["identity_links"][name] = kind["identity_links"]
                offered = [c for c in kind["candidates"] if c.get("review")]
                report["review_offered"][name] = len(offered)
                reasons: dict[str, int] = {}
                for c in kind["candidates"]:
                    if c.get("not_reviewable"):
                        reasons[c["not_reviewable"]] = reasons.get(c["not_reviewable"], 0) + 1
                report["not_reviewable"][name] = reasons
                report["payload_failures"] += _payload_failures(kind, contract, contracts)
                report["below_threshold_offered"] += sum(
                    c["probability"] < ids.REVIEW_PROBABILITY for c in kind["candidates"])
                evaluation = kind["evaluation"]
                report["train_eval_overlap"] += evaluation["train_eval_overlap"]
                report["evaluation"][name] = {k: evaluation[k] for k in _METRICS}
                split = evaluation.get("by_sources", {}).get(PORTAL_BC)
                if name in ids.PARTY_KINDS and split is not None:
                    report["portal_bc"][name] = {
                        **{k: split[k] for k in _METRICS},
                        "pairs_compared": split["pairs_compared"], "bands": split["bands"]}
                    report["portal_bc_pairs"] += split["pairs_compared"]
                    report["portal_bc_review"] += split["bands"].get("review", 0)
                    report["portal_bc_identity_pairs"] += split["identity_pairs"]
    finally:
        conn.close()
    report["kinds_unscored"] = len(ids.KINDS) - len(report["kinds_scored"])
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         bc_sender: Any = None, bc_books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.identity_scoring_probe")
    parser.add_argument("--db", required=True, help="throwaway mirror file")
    parser.add_argument("--store", required=True, help="throwaway ontology store file")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="the projection build's budget")
    parser.add_argument("--score-budget-seconds", type=float, default=None)
    parser.add_argument("--previous-report", default=None,
                        help="last daily verifier report, passed to the projection build")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--bc-capture", action="store_true")
    parser.add_argument("--live-audience", default=None)
    parser.add_argument("--session-projection", default=None,
                        help="the verifier session's projection (the projection probe's)")
    args = parser.parse_args(argv)
    phase = None   # the consumer's own phase, once the build succeeded
    try:
        from scripts import ontology_projection_probe as projection

        build_args = ["--db", args.db, "--store", args.store, "--build-only"]
        for flag, value in (("--budget-seconds", args.budget_seconds),
                            ("--previous-report", args.previous_report),
                            ("--workers", args.workers),
                            ("--live-audience", args.live_audience),
                            ("--session-projection", args.session_projection)):
            if value is not None:
                build_args += [flag, str(value)]
        if args.bc_capture:
            build_args.append("--bc-capture")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            built = projection.main(build_args, environ=environ, bc_sender=bc_sender,
                                    bc_books=bc_books)
        if built == projection.EXIT_BUDGET:
            partial = json.loads(out.getvalue())
            print(json.dumps({"probe": PROBE, "status": "budget_exhausted",
                              "phase": partial.get("phase"), "stage": "projection_build"}))
            return projection.EXIT_BUDGET
        if built != 0:
            print(json.dumps({**projection.build_failure(out.getvalue()), "probe": PROBE,
                              "error": "projection_build_failed",
                              "stage": "projection_build"}, sort_keys=True))
            return 1
        build = json.loads(out.getvalue())
        parts = {key: build[key] for key in ("blocked_upstream", "bc_capture", "sources",
                                            "session_projection", "disk") if key in build}
        if build.get("sources_blocked"):   # nothing was projected: measured nothing
            print(json.dumps({"probe": PROBE, "sources_blocked": build["sources_blocked"],
                              **parts}, sort_keys=True))
            return 0
        host = projection.served_host(args.store, environ)
        phase = "identity_scoring"   # an error opening the served host is not its own
        report = check_identity(host, budget_seconds=args.score_budget_seconds)
    except Exception as exc:  # noqa: BLE001 - a class name, or a SQLite error's error line
        from scripts.ontology_projection_probe import consumer_error
        print(json.dumps(consumer_error(exc, PROBE, phase, args.store), sort_keys=True))
        return 1
    report.update(parts, probe=PROBE, mirror_complete=build.get("mirror_complete") is True)
    print(json.dumps(report, sort_keys=True))
    ok = (report["kinds_unscored"] == 0 and report["scoring_errors"] == 0
          and report["payload_failures"] == 0 and report["below_threshold_offered"] == 0 and report["links_written"] == 0
          and report["train_eval_overlap"] == 0 and not report["budget_exhausted"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
