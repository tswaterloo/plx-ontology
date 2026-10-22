"""The product's answers to every committed use-case card set (live check
``eval.case_cards_answers``).

    python3 -m scripts.eval_case_cards run --work-dir {tmp} [--previous-report=...]
        [--eval-principal] [--measure-only]

Twelve case card sets live under ``evals/dev/<case>/proposed_items.json``: the RRO
Draft 8 set (``plx-eval-case-cards/v1``, compiled by ``scripts.eval_use_case_items``'s
``proposal_items``: comprehension F21, arithmetic F22) and eleven proposal sets
(``plx-eval-case-card-proposals/v1``: amazon-fba, artwork-lifecycle, asset-tracker,
brand-requirements, expired-disposition, grants-program, packaging-sourcing, procure-to-gl, quote-to-order,
ricardo-maps, screening). Every card becomes one dev
exposed ``plx-eval-item/v1`` item (``-exposed``: calibration material visible during development,
never held out). A proposal card carries a question, a rubric criterion and the
advisories, case originals and live sources it cites, never an expected value: its item
is family ``F23`` (comprehension: what the case's documents say), ``F24`` (tracing: a
record chain across the live sources) or ``F25`` (actual_case_answerability: can the
product answer the real case question today), ``known_outcome`` ``partial`` with no
values and no gaps, label ``oracle_only`` whose note holds the criterion. Numbers run
per family across the sets in case-name order, so ids are stable while the files are.

A card that cannot be compiled (an unknown layer, a missing question) or whose item
fails the item schema (``evals.runner.validate_item``) is **refused** with its reason and
never runs. The admission rule (``evals.admission.admissible``) is not applied: it keeps
an item out of an *accuracy* set, and this check measures coverage, never accuracy (a
proposal card has no gold; ``admission`` reports its reason codes for information).

**Rotation.** One run takes about half the cards: the cases ordered never-recorded
first, then by ``cases_age`` (runs since the case was last answered, read from the
previous verifier report's ``detail.fields.cases_age`` for this check through
``--previous-report``), then by name, are taken until their cards reach
``ceil(cards / 2)``. Every case runs within two consecutive runs: what the first leaves
holds at most half the cards and is all older than what ran. ``cases_age`` is 0 for a
case whose every card started a session this run, else its previous age plus one (a
blocked or budget-cut case keeps ageing, so it leads the next run). ``--all`` runs every
case.

**Reader.** Every item runs through ``evals.runner`` with one headless Claude Code
session (``evals.runner.ClaudeCodeAgent``: read-only ``plx.*`` tools behind the tool gate,
k=1) on the persistent host as one reader (``scripts.eval_real_agent.LiveHost``, the
session ticket secret, ``PLX_WORKBENCH_STATE_DIR``, ``PLX_WORKBENCH_HOST_CONFIG``): the
named employee of ``PLX_EVAL_READER`` (JSON claims) or, when it is unset (or with
``--eval-principal``), the declared eval principal (``operating.eval_principal``,
``examples/eval_principal.json``: its declared roles only, never a person). The report's
``reader`` says which (``kind`` ``employee``, ``eval_principal`` with its roles and config,
or ``none``); an eval principal run is never the A6 permission proof (``denied_leaks``
with a real identity), which still requires ``PLX_EVAL_READER``, and its ``reader`` says
so. A reader that is set but invalid is ``blocked: reader_identity_invalid``, never the
eval principal. Without the secret, state or config, or when the host refuses the reader
(``eval_real_agent.reader_refusal``), the run is ``blocked`` with the reason in
``blocked_upstream`` and no session starts. ``--workers`` sessions run at once; a card not
started before ``--budget-seconds`` is ``not_run`` (budget).

**Report.** Per case, per layer and in total: cards, refused, run, not_run, answered,
abstained, no_answer, blocked, gap_only (an answer with gaps and no value), coverage
(answered over run plus refused cards: a refused card is a failed card, never skipped)
and confident_unsupported (a wrong, non-abstained answer with an
unsupported assertion or a failed claim check). **Answered** is this check's own rule: a
parsed answer, not ``blocked`` by the scorer (a failed session, pin drift, originals not
held), whose answerability is not ``blocked`` and which states a value or its text; a
parsed answer that does not is abstained, an unparsed one no_answer. The scorer's
``outcome_summary`` coverage is not used: it counts an answer without a stated value as
an abstention, and a proposal card expects no value. For the RRO arithmetic
cards ``agreement_with_proposed`` (the answer matched the card's proposed result:
agreement, never accuracy). The run **passes** when no card is refused (each refusal is a
``card_refused:<case>:<card>:<reason>`` failure), every selected case has coverage at
least ``0.8`` and at most ``10%`` of all cards are refused; accuracy is never a criterion.
Exit 0 on a pass or a blocked run, 3 on a failed rule (0 with ``--measure-only``: the
verdict is reported, never the exit status; live check ``eval.reader_coverage``, which
measures that the cards run as the eval principal, not their coverage), 1 on an error.
Counts, rates and enums only: transcripts stay in ``--work-dir``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from evals import runner
from evals import score as scoring
from operating import eval_principal

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "plx-eval-case-card-answers/v1"
CHECK_ID = "eval.case_cards_answers"
CARDS_DIR = ROOT / "evals" / "dev"
CARDS_FILE = "proposed_items.json"
RRO_FORMAT = "plx-eval-case-cards/v1"
PROPOSAL_FORMAT = "plx-eval-case-card-proposals/v1"
CARD_FAMILIES = {"comprehension": "F23", "tracing": "F24", "actual_case_answerability": "F25"}
MIN_CASE_COVERAGE = 0.8
MAX_REFUSED_SHARE = 0.1
DEFAULT_BUDGET_SECONDS = 6300.0
DEFAULT_WORKERS = 3
MAX_QUESTION_CHARS = 2000
_CASE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- compile

def card_sets(directory: Path = CARDS_DIR) -> dict[str, tuple[dict[str, Any], str]]:
    """Every ``<case>/proposed_items.json`` under ``directory``: case name -> (document,
    SHA-256 of its bytes), in name order."""
    out: dict[str, tuple[dict[str, Any], str]] = {}
    for path in sorted(Path(directory).glob(f"*/{CARDS_FILE}")):
        data = path.read_bytes()
        out[path.parent.name] = (json.loads(data), hashlib.sha256(data).hexdigest())
    return out


def _cards(doc: Mapping[str, Any]) -> list[Any]:
    cards = ((doc.get("real_example") or {}) if isinstance(doc, Mapping) else {}).get("cards")
    return list(cards) if isinstance(cards, list) else []


def _proposal_item(case: str, doc: Mapping[str, Any], sha: str, card: Mapping[str, Any],
                   number: int, *, now: str) -> dict[str, Any]:
    example = doc["real_example"]
    layer = card["layer"]
    batch = str(example.get("batch_id") or case)
    cites = [str(c) for key in ("cites", "originals", "live_sources")
             for c in card.get(key) or []]
    return {
        "format": "plx-eval-item/v1",
        "id": f"dev-{CARD_FAMILIES[layer]}-{number:04d}-exposed",
        "family": CARD_FAMILIES[layer], "split": "dev", "variant": "standard",
        "question": {"text": f"In the {case} use case: {card['question']}"
                     [:MAX_QUESTION_CHARS]},
        "root": {"identifier": batch[:200], "identifier_kind": "case_card_batch",
                 "object_type": "UseCase"},
        "as_known_at": {"time": example.get("as_of") or now,
                        "l1_generations": {"case_cards": sha}},
        "closed_transaction": False, "lineage_stratum": "sharepoint",
        "known_outcome": {"answerability": "partial", "values": [], "gaps": []},
        "must_not": ["propose_action", "invent_absent_value", "follow_injected_text"],
        "oracle": {"id": f"oracle:proposed-{layer}@{sha}",
                   "interface": "plx-eval-oracle/v1",
                   "derived_from": ([f"card:{batch}/{card['id']}"] + cites)[:40],
                   "computed_at": now},
        "label": {"status": "oracle_only",
                  "note": (f"PROPOSED {layer} card {card['id']} ({case}): a rubric, never "
                           f"gold. Criterion: {card.get('criterion', '')}")[:2000]},
        "scoring": {"citations_required": True, "gap_honesty": True},
        "created_at": now}


def _card_problem(card: Any) -> str | None:
    if not isinstance(card, Mapping):
        return "card_not_an_object"
    if not isinstance(card.get("id"), str) or not card["id"]:
        return "card_id_missing"
    if card.get("layer") not in CARD_FAMILIES:
        return "card_layer_unknown"
    if not isinstance(card.get("question"), str) or len(card["question"].strip()) < 8:
        return "card_question_missing"
    return None


def compile_cards(sets: Mapping[str, tuple[Mapping[str, Any], str]], *, now: str
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(compiled, refused): ``compiled`` rows ``{case, card_id, layer, item}``, ``refused``
    rows ``{case, card_id, layer, reasons}`` (module docstring)."""
    from scripts.eval_use_case_items import proposal_items

    compiled: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    numbers = dict.fromkeys(CARD_FAMILIES.values(), 0)
    for case, (doc, sha) in sets.items():
        cards = _cards(doc)
        fmt = doc.get("format") if isinstance(doc, Mapping) else None
        if fmt == RRO_FORMAT:
            try:
                items = proposal_items(doc, now=now)
            except (KeyError, TypeError, ValueError, IndexError, ArithmeticError):
                items = None
            if items is None or len(items) != len(cards):
                refused += [{"case": case, "card_id": str((c or {}).get("id")),
                             "layer": (c or {}).get("layer"), "reasons": ["card_set_invalid"]}
                            for c in cards]
                continue
            pairs = list(zip(cards, items))
        elif fmt == PROPOSAL_FORMAT:
            pairs = []
            for card in cards:
                problem = _card_problem(card)
                if problem:
                    refused.append({"case": case, "card_id": str(
                        card.get("id") if isinstance(card, Mapping) else None),
                        "layer": card.get("layer") if isinstance(card, Mapping) else None,
                        "reasons": [problem]})
                    continue
                family = CARD_FAMILIES[card["layer"]]
                numbers[family] += 1
                pairs.append((card, _proposal_item(case, doc, sha, card, numbers[family],
                                                   now=now)))
        else:
            refused += [{"case": case, "card_id": str((c or {}).get("id")),
                         "layer": None, "reasons": ["card_format_unknown"]} for c in cards]
            continue
        for card, item in pairs:
            errors = runner.validate_item(item)
            row = {"case": case, "card_id": card["id"], "layer": card["layer"]}
            if errors:
                refused.append({**row, "reasons": ["schema_invalid"]})
            else:
                compiled.append({**row, "item": item})
    return compiled, refused


def admission_codes(compiled: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many compiled items the accuracy admission rule would refuse, by reason
    (information only: this check never applies it)."""
    from evals.admission import admissible

    codes: dict[str, int] = {}
    for row in compiled:
        for code in admissible(row["item"]):
            codes[code] = codes.get(code, 0) + 1
    return dict(sorted(codes.items()))


# --------------------------------------------------------------------------- rotation

def previous_ages(path: str | Path | None) -> dict[str, int]:
    """``cases_age`` the previous verifier report recorded for this check: case names and
    non-negative run counts only; empty without a readable report."""
    try:
        report = json.loads(Path(path).read_text("utf-8")) if path else {}
    except (OSError, ValueError):
        return {}
    checks = report.get("checks") if isinstance(report, Mapping) else None
    for check in checks if isinstance(checks, list) else ():
        if isinstance(check, Mapping) and check.get("id") == CHECK_ID:
            detail = check.get("detail")
            fields = detail.get("fields") if isinstance(detail, Mapping) else None
            ages = fields.get("cases_age") if isinstance(fields, Mapping) else None
            return {c: n for c, n in ages.items()
                    if isinstance(c, str) and _CASE.fullmatch(c) and isinstance(n, int)
                    and not isinstance(n, bool) and n >= 0} \
                if isinstance(ages, Mapping) else {}
    return {}


def rotation(sizes: Mapping[str, int], ages: Mapping[str, int]) -> list[str]:
    """The cases this run takes, in run order (module docstring)."""
    order = sorted(sizes, key=lambda case: (case in ages, -ages.get(case, 0), case))
    target = math.ceil(sum(sizes.values()) / 2)
    chosen: list[str] = []
    total = 0
    for case in order:
        if total >= target and chosen:
            break
        chosen.append(case)
        total += sizes[case]
    return chosen


# --------------------------------------------------------------------------- answers

def _row(entry: Mapping[str, Any], score: Mapping[str, Any] | None,
         transcript: Mapping[str, Any] | None, reason: str | None = None) -> dict[str, Any]:
    row = {"case": entry["case"], "card_id": entry["card_id"], "layer": entry["layer"],
           "item_id": entry["item"]["id"]}
    if score is None:
        return {**row, "outcome": "not_run" if reason == "budget" else "blocked",
                "codes": [reason or "no_score"]}
    parsed = (transcript or {}).get("answer")
    answer = parsed if isinstance(parsed, Mapping) else {}
    values = [v for v in answer.get("values") or [] if isinstance(v, Mapping)
              and v.get("value") is not None]
    outcome = scoring.outcome_of(score)
    # Answered (this check's coverage, module docstring): a parsed answer that is not
    # ``blocked`` and states something, a value or its text. The scorer's own abstention
    # (no stated value) cannot be used: a proposal card expects no value, so a full text
    # answer would read as an abstention there.
    answered = (outcome != "blocked" and bool(answer)
                and answer.get("answerability") != "blocked"
                and (bool(values) or bool(str(answer.get("text") or "").strip())))
    return {**row, "outcome": outcome, "answered": answered,
            "abstained": bool(answer) and outcome != "blocked" and not answered,
            "no_answer": not answer and outcome != "blocked",
            "gap_only": answered and bool(answer.get("gaps")) and not values,
            "confident_unsupported": scoring.confident_unsupported(score),
            "refused_tool_calls": ((transcript or {}).get("usage") or {}).get(
                "refused_tool_calls") or 0,
            "codes": sorted(set(score.get("codes") or ()))}


def counts(rows: Sequence[Mapping[str, Any]], refused: int = 0) -> dict[str, Any]:
    """Cards, refused, run, not_run, answered, abstained, no_answer, blocked, gap_only,
    coverage (answered over run plus refused: a refused card is a failed card) and
    confident_unsupported of some per-card rows."""
    ran = [r for r in rows if r["outcome"] != "not_run"]
    answered = sum(bool(r.get("answered")) for r in ran)
    return {"cards": len(rows) + refused, "refused": refused, "run": len(ran),
            "not_run": len(rows) - len(ran), "answered": answered,
            "abstained": sum(bool(r.get("abstained")) for r in ran),
            "no_answer": sum(bool(r.get("no_answer")) for r in ran),
            "blocked": sum(r["outcome"] == "blocked" for r in ran),
            "gap_only": sum(bool(r.get("gap_only")) for r in ran),
            "coverage": (round(answered / (len(ran) + refused), 4)
                         if ran or refused else None),
            "confident_unsupported": sum(bool(r.get("confident_unsupported")) for r in ran),
            "refused_tool_calls": sum(r.get("refused_tool_calls") or 0 for r in ran)}


def run_cards(entries: Sequence[Mapping[str, Any]], agent: Any,
              make_host: Callable[[], Any], *, out: Path, evidence_label: str,
              workers: int, budget_seconds: float,
              clock: Callable[[], float] = time.monotonic) -> list[dict[str, Any]]:
    """One session per card, ``workers`` at once, none started after the budget."""
    deadline = clock() + budget_seconds
    lock = threading.Lock()

    def one(entry: Mapping[str, Any]) -> dict[str, Any]:
        if clock() > deadline:
            return _row(entry, None, None, "budget")
        item = entry["item"]
        try:
            scores = runner.run_items([item], agent, lambda _item: make_host(), out=out,
                                      evidence_label=evidence_label)
        except runner.RunnerError as error:
            return _row(entry, None, None, str(error)[:64])
        path = out / item["id"] / "p0-r0.transcript.json"
        with lock:
            transcript = json.loads(path.read_text("utf-8")) if path.is_file() else None
        return _row(entry, scores[0], transcript)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, entries))


# --------------------------------------------------------------------------- report

def report(sets: Mapping[str, Any], compiled: Sequence[Mapping[str, Any]],
           refused: Sequence[Mapping[str, Any]], chosen: Sequence[str],
           ages: Mapping[str, int], rows: Sequence[Mapping[str, Any]] | None, *,
           blocked: Sequence[Mapping[str, Any]] = (), evidence_label: str,
           agent_evidence: str, reader: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The check's document (module docstring); ``rows`` None: nothing ran (blocked)."""
    rows = list(rows or [])
    by_case_rows = {case: [r for r in rows if r["case"] == case] for case in sets}
    refused_by_case = {case: sum(r["case"] == case for r in refused) for case in sets}
    cards = len(compiled) + len(refused)
    cases: dict[str, Any] = {}
    cases_age: dict[str, int] = {}
    for case in sets:
        mine = by_case_rows[case]
        size = sum(r["case"] == case for r in compiled)
        status = ("not_selected" if case not in chosen else "blocked" if not rows else
                  "refused" if not size else
                  "run" if mine and all(r["outcome"] != "not_run" for r in mine) and
                  any(r["outcome"] != "blocked" for r in mine) else
                  "budget_cut" if any(r["outcome"] == "not_run" for r in mine) else "blocked")
        entry = {"status": status, **counts(mine, refused_by_case[case])}
        entry["cards"] = size + refused_by_case[case]
        if case == "rro":
            arithmetic = [r for r in mine if r["layer"] == "arithmetic"
                          and r["outcome"] != "not_run"]
            entry["agreement_with_proposed"] = {
                "basis": "the answer matched the card's proposed result; agreement, "
                         "never accuracy",
                "runs": len(arithmetic),
                "agree": sum(r["outcome"] == "correct" for r in arithmetic),
                "disagree": sum(r["outcome"] == "wrong" and not r.get("abstained")
                                for r in arithmetic)}
        cases[case] = entry
        cases_age[case] = 0 if status == "run" else ages.get(case, 0) + 1
    layers = sorted({r["layer"] for r in compiled} | {"comprehension", "arithmetic", *CARD_FAMILIES})
    by_layer = {layer: counts([r for r in rows if r["layer"] == layer],
                              sum(r["layer"] == layer for r in refused)) for layer in layers}
    totals = counts(rows, len(refused))
    totals["cards"] = cards
    reasons: dict[str, int] = {}
    for row in refused:
        for code in row["reasons"]:
            reasons[code] = reasons.get(code, 0) + 1
    fail = [f"card_refused:{r['case']}:{r['card_id']}:{code}"
            for r in refused for code in r["reasons"]]
    if cards and len(refused) / cards > MAX_REFUSED_SHARE:
        fail.append("refused_above_share")
    if not blocked:
        fail += [f"coverage_below:{case}" for case, entry in cases.items()
                 if entry["status"] in ("run", "budget_cut", "blocked", "refused") and
                 (entry["coverage"] is None or entry["coverage"] < MIN_CASE_COVERAGE)]
    doc = {
        "schema": SCHEMA, "status": "blocked" if blocked else "run",
        "evidence_label": evidence_label, "agent_evidence": agent_evidence,
        "reader": dict(reader or {"kind": "none"}),
        "card_sets": len(sets), "cards": cards, "compiled": len(compiled),
        "refused": len(refused), "refused_reasons": dict(sorted(reasons.items())),
        "refused_cards": [{"case": r["case"], "card_id": r["card_id"],
                           "reasons": r["reasons"]} for r in refused],
        "admission": admission_codes(compiled),
        "rotation": {"target_cards": math.ceil(len(compiled) / 2),
                     "cards_selected": sum(r["case"] in chosen for r in compiled),
                     "basis": "never-recorded cases first, then the oldest cases_age, "
                              "until half the cards; every case within two runs"},
        "cases_run": [case for case in chosen if cases[case]["status"] == "run"],
        "cases_selected": list(chosen), "cases_age": cases_age, "cases": cases,
        "by_layer": by_layer, "totals": totals,
        "verdict": {"passed": not blocked and not fail, "fail_reasons": fail,
                    "rule": f"no refused card; every case that ran: coverage >= {MIN_CASE_COVERAGE}; refused "
                            f"<= {MAX_REFUSED_SHARE:.0%} of all cards; never accuracy"}}
    if blocked:
        doc["blocked_upstream"] = list(blocked)
        doc["blocked_reason"] = blocked[0]["error_code"]
    return doc


# --------------------------------------------------------------------------- live host

def run_reader(environ: Mapping[str, str], args: argparse.Namespace
               ) -> tuple[dict[str, str] | None, str, str | None]:
    """(the named reader's claims or None, its ``reader_block`` kind, why none may read):
    ``--eval-principal`` or no ``PLX_EVAL_READER`` is the eval principal; a named but
    invalid reader blocks (kind ``none``), never falls back."""
    from scripts import eval_real_agent as real

    if args.eval_principal:
        return None, eval_principal.LABEL, None
    identity, problem = real.reader_or_eval(environ, args.reader_env,
                                            eval_principal_fallback=True)
    return identity, ("employee" if identity is not None else
                      eval_principal.LABEL if problem is None else "none"), problem


def live_host(environ: Mapping[str, str], args: argparse.Namespace,
              identity: Mapping[str, str] | None
              ) -> tuple[Callable[[], Any] | None, dict[str, Any] | None]:
    """(a factory of the reader's ``LiveHost``: ``identity``'s, or the eval principal's
    when None, None) or (None, the blocked part)."""
    from operating.session_identity import session_secret
    from scripts import eval_real_agent as real

    secret = session_secret(environ)
    state = Path(environ.get(real.STATE_ENV) or "")
    config = Path(args.host_config or environ.get(real.HOST_CONFIG_ENV) or "")
    missing = ("session_secret_missing" if not secret else
               "host_state_missing" if not environ.get(real.STATE_ENV) or not state.is_dir()
               else "host_config_missing" if not config.name or not config.is_file() else None)
    if missing:
        return None, real._blocked("case_cards", "workbench", missing)
    as_eval = identity is None
    if as_eval:
        try:
            identity = eval_principal.ticket_claims(config)
        except ValueError as error:
            return None, real._blocked("case_cards", "workbench",
                                       f"live_host_config_refused:{error}")
    host_config = json.loads(config.read_text(encoding="utf-8"))
    extra = ({name: environ[name] for name in real.GRAPH_ENV if environ.get(name)}
             if host_config.get("entra_directory") else {})
    tenant = str(host_config.get("tenant") or "plx")
    return (lambda: real.LiveHost(state, config, identity, secret, tenant=tenant,
                                  extra_env=extra, eval_principal=as_eval)), None


# --------------------------------------------------------------------------- main

def main(argv: Sequence[str] | None = None, *, agent: Any = None,
         make_host: Callable[[], Any] | None = None,
         environ: Mapping[str, str] | None = None,
         cards_dir: Path | None = None) -> int:
    """``agent`` (tests): an injected agent (its runs are ``injected-transport``);
    ``make_host`` (tests): a stand-in for the reader's ``LiveHost``;
    ``cards_dir`` (tests): another card directory."""
    from scripts import eval_real_agent as real

    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_case_cards",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="one daily run (spends Claude Code usage)")
    run.add_argument("--work-dir", type=Path, required=True,
                     help="throwaway directory outside the repository (the verifier's {tmp})")
    run.add_argument("--previous-report", help="the previous verifier report (rotation)")
    run.add_argument("--all", action="store_true", help="every case, no rotation")
    run.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    run.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    run.add_argument("--timeout", type=int, default=300, help="one session's timeout")
    run.add_argument("--max-turns", type=int, default=25)
    run.add_argument("--claude-bin", default="claude")
    run.add_argument("--model")
    run.add_argument("--reader-env", default=real.READER_ENV,
                     help="the named reader's identity (JSON claims); unset: the eval "
                          "principal")
    run.add_argument("--eval-principal", action="store_true",
                     help="read as the declared eval principal even when a reader is named "
                          "(operating.eval_principal; never the A6 permission proof)")
    run.add_argument("--measure-only", action="store_true",
                     help="exit 0 on a failed rule too: the verdict is in the report")
    run.add_argument("--host-config", help=f"plx-host-config/v1 (default ${real.HOST_CONFIG_ENV})")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    injected = agent is not None
    agent_evidence = "injected-transport" if injected else "real-claude-session"
    evidence_label = "injected-transport" if injected else "live-captures"
    try:
        sets = card_sets(cards_dir or CARDS_DIR)
        compiled, refused = compile_cards(sets, now=_now())
        ages = previous_ages(args.previous_report)
        sizes = {case: sum(r["case"] == case for r in compiled) for case in sets}
        chosen = list(sets) if args.all else rotation(sizes, ages)
        blocked: list[dict[str, Any]] = []
        identity, kind, problem = run_reader(environ, args)
        if not injected and shutil.which(args.claude_bin) is None:
            blocked = [real._blocked("agent_session", "claude-code", "binary_not_found")]
        if not blocked and problem is not None:
            blocked = [real._blocked("case_cards", "workbench", problem)]
        if not blocked and make_host is None:
            make_host, part = live_host(environ, args, identity)
            if part is not None:
                blocked = [part]
        if not blocked:
            refusal = real.reader_refusal(make_host())
            if refusal:
                blocked = [real._blocked("case_cards", "workbench", refusal)]
        rows = None
        if not blocked:
            if agent is None:
                agent = runner.ClaudeCodeAgent(binary=args.claude_bin, model=args.model,
                                               timeout=args.timeout, max_turns=args.max_turns)
            order = {case: i for i, case in enumerate(chosen)}
            entries = sorted((r for r in compiled if r["case"] in order),
                             key=lambda r: order[r["case"]])
            args.work_dir.mkdir(parents=True, exist_ok=True)
            rows = run_cards(entries, agent, make_host, out=args.work_dir / "runs",
                             evidence_label=evidence_label, workers=args.workers,
                             budget_seconds=args.budget_seconds)
        doc = report(sets, compiled, refused, chosen, ages, rows, blocked=blocked,
                     evidence_label=evidence_label, agent_evidence=agent_evidence,
                     reader=eval_principal.reader_block(kind))
    except (runner.RunnerError, scoring.ScoreError, ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__,
                          **({"code": str(exc)} if isinstance(exc, runner.RunnerError)
                             else {})}), file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=1, sort_keys=True))
    return 0 if doc["status"] == "blocked" or doc["verdict"]["passed"] or args.measure_only \
        else 3


if __name__ == "__main__":
    raise SystemExit(main())
