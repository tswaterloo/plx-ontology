"""Evaluation items (``plx-eval-item/v1``) from the delivered use cases, in live state only.

Live check ``ingest.use_cases``: runs the use-case scan (``operating.use_case_intake``,
the same arguments) and adds ``evaluation_items`` (counts only) to its report. Eval
tooling calls the product, never the reverse: the item schema is ``evals/``'s.

    python3 -m scripts.eval_use_case_items scan --state-dir ... [--sources ... --live-audience ...]

The use-case intake (``operating.use_case_intake``) records one stub per
delivered artifact. A delivery is often a zip of many cases (one mail thread,
one document each), so the item unit is a **case unit**: a top-level member of
a delivered zip (with everything inside it: a ``.msg``'s body, recipients and
attachments), else the whole artifact. A unit becomes one item only when its
extracted text both

* states an outcome on a labelled line (``use_case_intake.outcome_statements``:
  ``Outcome:``, ``Known outcome:``, ``Actual outcome:``, ``Resolution:``; a
  placeholder is not an outcome), and
* asks at least one question (a line ending in ``?``).

The item's question is the unit's questions as written, its expected value
``stated_outcome`` is the first stated outcome verbatim, cited to the pinned
original (``sharepoint://...#sha256=``) and to the span of the stored
extraction text (``oracle.derived_from`` names the byte pin
``original://sha256/``, so the admission rule holds the value to its citation), with the
``extraction_method`` of the part it was read from
(``tesseract-ocr``: machine-read by OCR). Nothing is invented: a unit without a stated outcome or
without a question is counted, never itemised, and no question is composed. A
case with no extracted text is counted ``no_text``; ``text_unavailable`` counts
only a stored extraction that cannot be read back or no longer matches its digest.
Every item is ``label.status: oracle_only`` (a person has not confirmed the
outcome; ``status: stub_unconfirmed`` in the stub says the same). The report's
``denominators`` say how much of that is evidence: ``total`` units that state an
outcome and ask a question = ``scorable`` (label ``human_audited``: a person confirmed
the outcome) + ``oracle_only`` (pending a human label, not a correctness result) +
``failed`` (the item failed its schema and was not written).

Every item is a **dev exposed** item (id ``dev-F20-NNNN-exposed``, listed in
the directory's exposed manifest), never held out: the delivered cases were
read by people building this product, so they are calibration material
(the stub's own ``split`` is not used here). The
directory ``<state>/use_cases/eval_items/dev`` is what the eval runner executes
(``python3 -m evals.runner run --items <state>/use_cases/eval_items/dev --split
dev ...``); it lives in the state directory, never in the repository. It is
rebuilt from the newest record of every case on each call, so a re-upload
replaces its items. Ids are stable per (case, unit). The report is counts only.

**Proposed items over a pinned original** (``proposal run``). A committed case-card
file (``plx-eval-case-cards/v1``, e.g. ``evals/dev/rro/proposed_items.json``) carries
someone's *proposed* questions about one delivered document, each citing 1-based
inclusive line anchors in its exact bytes (``source.original.sha256``), in two layers:
``comprehension`` (family F21: what the document says, with qualifiers; the card's
criterion is a rubric, never gold) and ``arithmetic`` (family F22: a calculation over
figures the document states, under the card's stated assumption, and its proposed
result). An expected value cites the byte pin (``original://sha256/<sha>``, field
``row.text``, the item schema's field form): the scorer matches the answer value whose page
citation re-reads as that original by its (recomputed) value when it is the card's only
expected value, never by this compiler's name ``result`` alone; a recorded
provenance conflict on a card's lines is an expected ``unknown`` gap with reason
``evals.measure.CONFLICT_REASON`` (no ``concerns``: a conflict names no expected value), matched on state,
a conflict reason and a distinctive subject word in common, not its whole wording
(``evals.score``).
``proposal run`` needs a recorded capture of that SHA-256
(``operating.use_case_eval_items.captured_original``: the body read counts as proven
by nothing else; ``--capture`` takes it first through the guarded shared-link reader);
without one it writes nothing. It writes one dev exposed ``plx-eval-item/v1`` item per
card under ``<state>/use_cases/eval_items/proposals/<batch>`` with its exposed manifest,
loads them back through ``evals.runner.load_items`` (the runner executes them like the
F20 items; ``items.loaded`` counts every item that passed its checks, ``items.admitted``
those the admission rule ``evals.admission.admissible`` lets a run score, ``items.refused``
the rest by id with reason codes: a comprehension card with no expected value or gap is
``empty_outcome``) and reports per item on the captured original, keeping two things apart:

* ``tool_output``: what the run read: each anchor in range and the SHA-256 of its lines
  (never text); for arithmetic the calculation re-evaluated by the case calculation
  evaluator (``operating.cases.evaluate``, Decimal; ``round_to_cents`` rounds half-up
  to 0.01; ``;`` separates results) and how many of its operands appear as figures in
  the anchored lines;
* ``scored``: comprehension items are ``unscored`` (a proposed rubric is not truth);
  arithmetic items are ``consistent`` / ``inconsistent`` / ``unevaluable`` (the proposed
  result against the recomputation: arithmetic consistency, not product truth).

Actual-case answerability is scored (``actual_case_answerability``, ``answerability``):
each item is ``answerable`` from the captured original (every anchor in range and a
verified arithmetic result, or a human-audited comprehension outcome without gaps),
``partial`` (its expected outcome carries a gap, e.g. a recorded provenance conflict on
its lines), ``blocked`` (an anchor out of range: the evidence is not in these bytes) or
``undetermined`` (no expected outcome to judge: an unaudited comprehension card or an
unverified result). The case is ``undetermined`` when every item is (nothing was judged,
so nothing is blocked), ``answerable`` only when every item is and the card file declares
no missing original and no conflict, ``blocked`` when no item is answerable or partial
(some anchor out of range), else ``partial``. Recorded provenance conflicts stay ``unresolved`` and policy
statuses are echoed as recorded. A card with an unknown layer, a duplicate id or a malformed anchor
refuses the whole file.

A card may carry a person's **audit** (``"audit": {"audited_by", "audited_at":
"YYYY-MM-DD", "values": [{"name", "value"}], "gaps": [{"state", "subject", "reason"}]}``):
its item is then ``label.status: human_audited``. An audited arithmetic card confirms its
proposed result (no values or gaps of its own); an audited comprehension card states the
expected quoted values (cited to the byte pin) and/or gaps. Anything else in an audit
refuses the file.

**Answers** (``--answer``). The
admitted items run through ``evals.runner`` with the agent (``ClaudeCodeAgent``: one
headless Claude Code session per item behind the runner's tool gate, read-only tools only)
as the employee principal ``--reader``. Each run's host is a fresh disposable fixture state
(an empty mirror) seeded from the RECORDED capture: its bytes are stored through
``OriginalStore.store_source`` with the capture's provenance (route, drive item, eTag,
source ACL, ``captured_at``; ``RECORDED_CAPTURE_SOURCE``) and the reader as its audience,
so no source is read again. The agent must reach the original itself through ``plx.*``
tools; before any session the harness checks, as the reader, that ``plx.documents`` lists
it by name and ``plx.read`` reads its first page. If not (a capture too old for its
recorded permissions, a reader they deny), every admitted item is ``blocked`` with that
reason and no session starts: never a pass. Every result carries ``discovery:
seeded_from_capture``. Results fall in three buckets, by label provenance:

* ``scored``: items labelled ``human_audited``; accuracy (``evals.score.outcome_summary``,
  coverage beside it) is computed over these only;
* ``agreement_with_proposed``: ``oracle_only`` items whose expected value was verified
  independently (the calculation recomputes to the proposed result and every operand is a
  figure on the anchored lines): agreement with a proposed value, never accuracy;
* ``unscored``: everything else (comprehension without a human-audited quote or gap,
  refused items included).

Per layer and in total the report counts each bucket, ``gap_only`` (admitted items that
expect gaps and no value), ``blocked``, ``refused`` and ``coverage`` (answered runs over
runs). When no card carries a human audit the answers still run and are reported, but
``blocked_reason`` is ``no_human_audited_items`` and ``blocked_upstream`` names the part
``use_case_answers_scored`` with cause ``later_ask``: there is no accuracy until a person
audits the cards, so the check is blocked on that ask, never a pass. Transcripts and scores
go to ``--out`` (default ``<state>/use_cases/eval_runs/
<batch>``). The report is written to ``<state>/use_cases/eval_reports/<batch>.json`` and
printed::

    python3 -m scripts.eval_use_case_items proposal run --cards evals/dev/rro/proposed_items.json \\
        --state-dir D --evidence-label live-captures [--capture] \\
        [--answer --reader <employee principal> [--out DIR] [--max-turns N] [--timeout S]]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Callable, Mapping

from operating import use_case_intake
from operating.use_case_intake import IntakeState, outcome_statements, questions_in
from evals.measure import CONFLICT_REASON
from evals.score import evaluate_expression


FAMILY = "F20"  # a stakeholder's delivered case: its question and its stated outcome
ITEMS_DIR = "eval_items"
ORACLE = "use-case-stated-outcome"
MAX_QUESTION_CHARS = 2000
_SCHEMA = Path(__file__).resolve().parents[1] / "evals" / "item.schema.json"
_MAIL = (".msg", ".eml")


def _method(record: Mapping[str, Any], span: list[int]) -> str | None:
    """The extraction method of the part of the stored text a span lies in."""
    for part in (record.get("accounting") or {}).get("parts") or []:
        if "start" in part and int(part["start"]) <= span[0] and span[1] <= int(part["end"]):
            return part.get("method")
    return None


def _units(record: Mapping[str, Any]) -> dict[str, list[tuple[int, int]]]:
    """``unit key -> text spans`` of a record's extracted parts (key ``""``: whole)."""
    name = (record.get("original") or {}).get("name") or "original"
    prefix = name + "!/" if record.get("detected_type") == "zip" else None
    units: dict[str, list[tuple[int, int]]] = {}
    for part in (record.get("accounting") or {}).get("parts") or []:
        if "start" not in part:
            continue
        path = str(part.get("path"))
        key = ""
        if prefix is not None and path.startswith(prefix):
            key = re.split(r"[!#]", path[len(prefix):], maxsplit=1)[0]
        units.setdefault(key, []).append((int(part["start"]), int(part["end"])))
    return units


def _inside(span: list[int], ranges: list[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in ranges)


def _number(key: str, used: set[int]) -> int:
    number = int(sha256(key.encode()).hexdigest()[:8], 16) % 9999 + 1
    while number in used:
        number = number % 9999 + 1
    used.add(number)
    return number


def _item(stub: Mapping[str, Any], unit: str, name: str, questions: list[dict[str, Any]],
          outcome: Mapping[str, Any], method: str | None, number: int, now: str
          ) -> dict[str, Any]:
    start, end = outcome["span"]
    value = {"name": "stated_outcome", "value": outcome["text"],
             "citations": [{"l1_handle": stub["artifact_citation"], "field_path": "row.text"}]}
    if method:
        value["extraction_method"] = method
    return {
        "format": "plx-eval-item/v1", "id": f"dev-{FAMILY}-{number:04d}-exposed",
        "family": FAMILY, "split": "dev",
        "variant": "standard",
        "question": {"text": " ".join(q["text"] for q in questions)[:MAX_QUESTION_CHARS]},
        "root": {"identifier": name[:200], "identifier_kind": "document_name",
                 "object_type": "Email" if name.lower().endswith(_MAIL) else "SourceDocument"},
        "as_known_at": {"time": stub["recorded_at"],
                        "l1_generations": {"use_case_original": stub["sha256"]}},
        "closed_transaction": True,
        "lineage_stratum": "sharepoint",
        "known_outcome": {"answerability": "answerable", "gaps": [], "values": [value]},
        "must_not": ["propose_action", "invent_absent_value", "follow_injected_text"],
        "oracle": {"id": f"oracle:{ORACLE}@{stub['text_sha256']}",
                   "interface": "plx-eval-oracle/v1",
                   "derived_from": [f"use_cases/intake.jsonl#intake_seq={stub['intake_seq']}",
                                    f"use_cases/{stub['text_ref']}#{start}-{end}",
                                    # the byte pin: a pinned source (evals.score.PINNED_SOURCES)
                                    f"original://sha256/{stub['sha256']}"],
                   "computed_at": now},
        "label": {"status": "oracle_only", "note": (
            "Pending human label. Drafted by scripts.eval_use_case_items from a labelled "
            f"'{outcome['label']}' line in the delivered use case (unit "
            f"{'whole artifact' if not unit else 'zip member'}); the question is the unit's "
            "own question text. Not confirmed by a person; no model run.")},
        "scoring": {"citations_required": True, "gap_honesty": True},
        "created_at": now}


def build_items(state_dir: str | Path, *, clock: Callable[[], datetime] | None = None
                ) -> dict[str, Any]:
    """Rebuild ``<state>/use_cases/eval_items/dev``; counts only."""
    files = IntakeState(state_dir)
    with files.lock():  # never beside an intake that is appending records
        return _build(files, clock)


def _build(files: IntakeState, clock: Callable[[], datetime] | None) -> dict[str, Any]:
    from ontology.json_contract import validate

    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(
        timezone.utc).isoformat()
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    records = {r.get("intake_seq"): r for r in files.records()}
    newest: dict[str, Mapping[str, Any]] = {}
    for stub in files.stubs():  # the stub append commits a record/stub pair
        if stub.get("intake_seq") in records and stub.get("intake_seq", 0) > \
                (newest.get(stub["case_id"]) or {}).get("intake_seq", 0):
            newest[stub["case_id"]] = stub
    counts = {"cases": len(newest), "units": 0, "units_with_outcome": 0,
              "units_with_questions": 0, "no_text": 0, "text_unavailable": 0, "invalid": 0}
    items: list[dict[str, Any]] = []
    used: set[int] = set()
    for case_id in sorted(newest):
        stub = newest[case_id]
        record = records[stub["intake_seq"]]
        if not stub.get("text_ref"):  # nothing was extracted (all unread, capped or empty)
            counts["no_text"] += 1
            continue
        try:
            text = (files.root / stub["text_ref"]).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            text = None
        if text is None or sha256(text.encode("utf-8")).hexdigest() != stub.get("text_sha256"):
            counts["text_unavailable"] += 1
            continue
        stated, _placeholders = outcome_statements(text)
        questions, _ = questions_in(text, limit=None)
        original = (record.get("original") or {}).get("name") or "original"
        for unit, ranges in sorted(_units(record).items()):
            counts["units"] += 1
            unit_outcomes = [o for o in stated if _inside(o["span"], ranges)]
            unit_questions = [q for q in questions if _inside(q["span"], ranges)]
            counts["units_with_outcome"] += bool(unit_outcomes)
            counts["units_with_questions"] += bool(unit_questions)
            if not unit_outcomes or not unit_questions:
                continue
            name = PurePosixPath(unit).name if unit else original
            item = _item(stub, unit, name, unit_questions, unit_outcomes[0],
                         _method(record, unit_outcomes[0]["span"]),
                         _number(f"{case_id}/{unit}", used), now)
            if validate(item, schema):
                counts["invalid"] += 1
                continue
            items.append(item)
    directory = files.root / ITEMS_DIR / "dev"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for stale in directory.glob("*.json"):
        stale.unlink()
    for item in items:
        _write(directory / f"{item['id']}.json", item)
    _write(directory / "manifest.json", {
        "format": "plx-eval-exposed-manifest/v1", "split": "dev", "exposure": "exposed",
        "rule": "Delivered use cases (scripts.eval_use_case_items): dev exposed items of "
                "the live state only, never held out, never committed.",
        "items": [{"id": item["id"]} for item in items]})
    status = [item["label"]["status"] for item in items]
    return {**counts, "items": len(items), "denominators": {
        "total": len(items) + counts["invalid"],
        "scorable": status.count("human_audited"), "oracle_only": status.count("oracle_only"),
        "failed": counts["invalid"]}}


def _write(path: Path, value: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


# ------------------------------------------------------------------ proposed items

PROPOSAL_FAMILIES = {"comprehension": "F21", "arithmetic": "F22"}
PROPOSALS_DIR = "proposals"
REPORTS_DIR = "eval_reports"
REPORT_FORMAT = "plx-eval-proposal-report/v1"
CARDS_FORMAT = "plx-eval-case-cards/v1"
CAPTURE_PRINCIPAL = "svc:use-case-capture"
CAPTURE_TENANT = "plx"
_BATCH = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_OPERAND = re.compile(r"\d+(?:\.\d+)?")
_FIGURE = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")


class ProposalError(ValueError):
    """A refused card file or run; the message is a code (and at most a card id)."""


_AUDIT_KEYS = {"audited_by", "audited_at", "values", "gaps"}
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _audit_valid(card: Mapping[str, Any]) -> bool:
    """A card's audit (module docstring): who, when, and for comprehension what."""
    audit = card["audit"]
    if not isinstance(audit, Mapping) or set(audit) - _AUDIT_KEYS or \
            not isinstance(audit.get("audited_by"), str) or \
            not 1 <= len(audit["audited_by"]) <= 128 or \
            not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(audit.get("audited_at"))):
        return False
    values, gaps = audit.get("values", []), audit.get("gaps", [])
    if not isinstance(values, list) or not isinstance(gaps, list):
        return False
    if (card["layer"] == "arithmetic") == bool(values or gaps):
        return False  # arithmetic confirms its result; comprehension states what it expects
    states = json.loads(_SCHEMA.read_text(encoding="utf-8"))["$defs"]["gap_state"]["enum"]
    return all(isinstance(v, Mapping) and set(v) == {"name", "value"} and
               _NAME_PATTERN.match(str(v["name"])) and isinstance(v["value"], str) and
               0 < len(v["value"]) <= 2000 for v in values) and \
        all(isinstance(g, Mapping) and set(g) == {"state", "subject", "reason"} and
            g["state"] in states and isinstance(g["subject"], str) and
            0 < len(g["subject"]) <= 700 and _NAME_PATTERN.match(str(g["reason"]))
            for g in gaps)


def _cards(doc: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The pinned original and the validated cards; anything unexpected refuses."""
    if not isinstance(doc, Mapping) or doc.get("format") != CARDS_FORMAT:
        raise ProposalError("cards_format")
    original = (doc.get("source") or {}).get("original") or {}
    example = doc.get("real_example") or {}
    if not re.fullmatch(r"[0-9a-f]{64}", str(original.get("sha256"))) or \
            not isinstance(original.get("name"), str) or \
            not _BATCH.match(str(example.get("batch_id"))):
        raise ProposalError("cards_original_or_batch_invalid")
    cards, seen = [], set()
    for card in example.get("cards") or []:
        cid = card.get("id") if isinstance(card, Mapping) else None
        if not isinstance(cid, str) or cid in seen:
            raise ProposalError(f"card_id_invalid_or_duplicate:{cid}")
        seen.add(cid)
        if card.get("layer") not in PROPOSAL_FAMILIES:
            raise ProposalError(f"card_layer_unknown:{cid}")
        spans = card.get("source_lines")
        if not isinstance(card.get("question"), str) or not isinstance(spans, list) or \
                not spans or not all(isinstance(s, list) and len(s) == 2 and all(
                    type(n) is int for n in s) and 1 <= s[0] <= s[1] for s in spans):
            raise ProposalError(f"card_question_or_anchor_invalid:{cid}")
        if card["layer"] == "arithmetic":
            result = card.get("result")
            results = result if isinstance(result, list) else [result]
            if not isinstance(card.get("calculation"), str) or not results or \
                    not all(isinstance(r, str) for r in results):
                raise ProposalError(f"card_calculation_invalid:{cid}")
        if "audit" in card and not _audit_valid(card):
            raise ProposalError(f"card_audit_invalid:{cid}")
        cards.append(dict(card))
    if not cards:
        raise ProposalError("cards_empty")
    return dict(original), cards


def _results(card: Mapping[str, Any]) -> list[str]:
    return list(card["result"]) if isinstance(card["result"], list) else [card["result"]]


def recompute(calculation: str) -> list[Decimal]:
    """Each ``;``-separated expression through the case calculation evaluator
    (``evals.score.evaluate_expression``: ``round_to_cents(...)`` rounds half-up)."""
    return [evaluate_expression(part.strip(), {})[0] for part in calculation.split(";")]


def _figures(lines: list[bytes], spans: list[list[int]]) -> set[Decimal]:
    """Every figure printed on the anchored lines (``10,032`` is 10032, ``15%`` 0.15)."""
    figures: set[Decimal] = set()
    for first, last in spans:
        for line in lines[first - 1:last]:
            for token in _FIGURE.findall(line.decode("utf-8", "replace")):
                number = Decimal(token.rstrip("%").replace(",", ""))
                figures.add(number / 100 if token.endswith("%") else number)
    return figures


def proposal_items(doc: Mapping[str, Any], *, now: str) -> list[dict[str, Any]]:
    """One dev exposed ``plx-eval-item/v1`` item per card (see the module docstring)."""
    original, cards = _cards(doc)
    example = doc["real_example"]
    sha = original["sha256"]
    pin = f"original://sha256/{sha}"
    revision = str(((doc.get("source") or {}).get("advisory") or {}).get("contract_sha256")
                   or sha)
    label = original.get("label") or "pinned"
    conflicts = doc.get("provenance_conflicts") or []
    numbers = {layer: 0 for layer in PROPOSAL_FAMILIES}
    items = []
    for card in cards:
        layer = card["layer"]
        numbers[layer] += 1
        spans = card["source_lines"]
        cited = {n for a, b in spans for n in range(a, b + 1)}
        audit = card.get("audit")
        if layer == "arithmetic":
            results = _results(card)
            outcome = {"answerability": "answerable", "gaps": [], "values": [
                {"name": "result" if len(results) == 1 else f"result_{i}", "value": r,
                 "citations": [{"l1_handle": pin, "field_path": "row.text"}]}
                for i, r in enumerate(results, 1)]}
            note = (f"PROPOSED arithmetic check {card['id']}: the proposed result of "
                    f"{card['calculation']} under the stated assumption "
                    f"({card.get('interpretation')}). Arithmetic consistency over figures the "
                    f"{label} document states, never a verified business value.")
            scoring = {"exact_values": True, "citations_required": True}
        else:
            gaps = [{"state": "unknown", "subject": str(c.get("subject"))[:700],
                     "reason": CONFLICT_REASON} for c in conflicts
                    if cited & {n for a, b in c.get("source_lines") or []
                                for n in range(a, b + 1)}]
            values = [{**value, "citations": [{"l1_handle": pin, "field_path": "row.text"}]}
                      for value in (audit or {}).get("values", [])]
            gaps = [dict(gap) for gap in (audit or {}).get("gaps", [])] + gaps
            outcome = {"answerability": "answerable" if values and not gaps else "partial",
                       "values": values, "gaps": gaps}
            note = (f"PROPOSED comprehension question {card['id']}: a rubric, never gold "
                    f"({label} assertions are not ground truth). Criterion: "
                    f"{card.get('criterion', '')}")
            scoring = {"citations_required": True, "gap_honesty": True}
        items.append({
            "format": "plx-eval-item/v1",
            "id": f"dev-{PROPOSAL_FAMILIES[layer]}-{numbers[layer]:04d}-exposed",
            "family": PROPOSAL_FAMILIES[layer], "split": "dev", "variant": "standard",
            "question": {"text": f"In '{original['name']}' ({label}): {card['question']}"
                         [:MAX_QUESTION_CHARS]},
            "root": {"identifier": original["name"][:200], "identifier_kind": "document_name",
                     "object_type": "SourceDocument"},
            "as_known_at": {"time": example.get("as_of") or now,
                            "l1_generations": {"pinned_original": sha}},
            "closed_transaction": False, "lineage_stratum": "sharepoint",
            "known_outcome": outcome,
            "must_not": ["propose_action", "invent_absent_value", "follow_injected_text"] + (
                ["claim_uncited_number"] if layer == "arithmetic" else []),
            "oracle": {"id": f"oracle:proposed-{layer}@{revision}",
                       "interface": "plx-eval-oracle/v1",
                       "derived_from": [f"card:{example['batch_id']}/{card['id']}"] +
                       [f"{pin}#L{a}-L{b}" for a, b in spans],
                       "computed_at": now},
            "label": {"status": "oracle_only", "note": note[:2000]} if audit is None else {
                "status": "human_audited", "audited_by": audit["audited_by"],
                "audited_at": audit["audited_at"],
                "note": f"Human-audited {layer} item {card['id']}: the expected "
                        f"{'result' if layer == 'arithmetic' else 'quoted values and gaps'} "
                        f"of the {label} document as the auditor recorded them."[:2000]},
            "scoring": scoring, "created_at": now})
    return items


ITEM_ANSWERABILITY = ("answerable", "partial", "blocked", "undetermined")


def _answerability(item: Mapping[str, Any], anchors: list[dict[str, Any]],
                   verified: bool) -> str:
    """One item's answerability from the captured original (module docstring)."""
    if not all(a["in_range"] for a in anchors):
        return "blocked"
    outcome = item["known_outcome"]
    if outcome["gaps"]:
        return "partial"
    if verified or (item["label"]["status"] == "human_audited" and outcome["values"]):
        return "answerable"
    return "undetermined"


def _case_answerability(per_item: list[str], missing: list[Any],
                        conflicts: int) -> dict[str, Any]:
    counts = {state: per_item.count(state) for state in ITEM_ANSWERABILITY}
    if counts["undetermined"] == len(per_item):
        answerability = "undetermined"  # nothing judged, nothing out of range
    elif counts["answerable"] == len(per_item) and not missing and not conflicts:
        answerability = "answerable"
    elif not counts["answerable"] and not counts["partial"]:
        answerability = "blocked"
    else:
        answerability = "partial"
    return {"status": "scored", "answerability": answerability, "items": counts,
            "missing_originals": list(missing), "missing_original_count": len(missing),
            "unresolved_conflicts": conflicts,
            "basis": "what this run read from the captured original against the card "
                     "file's declared missing originals and conflicts; not product truth"}


def _now(clock: Callable[[], datetime] | None) -> str:
    return (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()


def _arithmetic(card: Mapping[str, Any], lines: list[bytes],
                anchors: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(tool output, scored) of one arithmetic card."""
    from operating.cases import ExpressionError

    proposed = _results(card)
    try:
        values: list[Decimal] | None = recompute(card["calculation"])
    except ExpressionError:
        values = None
    status = "unevaluable" if values is None else (
        "consistent" if len(values) == len(proposed) and all(
            value == Decimal(p) for value, p in zip(values, proposed)) else "inconsistent")
    figures = _figures(lines, [a["lines"] for a in anchors if a["in_range"]])
    operands = [Decimal(o) for o in _OPERAND.findall(card["calculation"])]
    return ({"recomputed": None if values is None else [str(v) for v in values],
             "operands": {"total": len(operands),
                          "found_in_anchored_lines": sum(o in figures for o in operands)}},
            {"status": status, "basis": "arithmetic consistency of the proposed result "
                                        "under the stated assumption; not product truth"})


ANSWER_TENANT = "plx"
DISCOVERY = "seeded_from_capture"
BUCKETS = ("scored", "agreement_with_proposed", "unscored")
# No card carries a human audit: the answers ran but there is no accuracy to report.
NO_AUDIT = "no_human_audited_items"
RUNS_DIR = "eval_runs"


def _seed(state_dir: Path, reader: Any, content: bytes, capture: Mapping[str, Any]
          ) -> dict[str, Any]:
    """Store the recorded capture's bytes in one run's disposable state (no source read):
    the capture's provenance, the reader as audience."""
    from operating.case_originals import RECORDED_CAPTURE_SOURCE, OriginalStore
    from operating.case_runtime import CASE_RUNTIME_DIR

    keys = ("route", "captured_at", "captured_by", "drive_id", "item_id", "etag", "name",
            "path")
    provenance = {key: capture.get(key) for key in keys}
    provenance.update(
        source=RECORDED_CAPTURE_SOURCE, discovery=DISCOVERY,
        identity=[capture.get("drive_id"), capture.get("item_id"), capture.get("etag"),
                  capture["sha256"]],
        source_citation=capture.get("drive_citation"), content_sha256=capture["sha256"],
        source_acl=capture.get("source_acl") or {})
    return OriginalStore(Path(state_dir) / CASE_RUNTIME_DIR).store_source(
        reader, str(capture.get("name") or ""), content, provenance)


def _unreachable(host: Any, seeded: Mapping[str, Any]) -> str | None:
    """Why the reader cannot reach the seeded original through plx.* tools (None: it can):
    ``plx.documents`` lists it by its name, ``plx.read`` reads its first page."""
    listed = host.read("plx.documents", {"query": seeded["filename"]})
    if seeded["citation"] not in [d.get("citation") for d in
                                  (listed or {}).get("documents") or [] if isinstance(d, dict)]:
        return "original_not_listed_for_reader"
    pages = seeded.get("page_citations") or []
    read = host.read("plx.read", {"citation": pages[0]}) if pages else None
    if not isinstance(read, Mapping) or read.get("status") != "resolved":
        return "original_unreadable_for_reader"
    return None


def _counts(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Bucket, gap-only, blocked, refused counts and coverage of some per-item results."""
    from evals.score import outcome_summary

    runs = [{"outcome": r["outcome"], "abstained": r.get("abstained", False)}
            for r in rows if r["outcome"] != "refused"]
    return {"items": len(rows), **{b: sum(r["bucket"] == b for r in rows) for b in BUCKETS},
            "gap_only": sum(bool(r.get("gap_only")) for r in rows),
            "blocked": sum(r["outcome"] == "blocked" for r in rows),
            "refused": sum(r["outcome"] == "refused" for r in rows),
            "coverage": outcome_summary(runs)["coverage"]}


def _answers(files: IntakeState, batch: str, cards: list[dict[str, Any]],
             items: list[dict[str, Any]], loaded: Any, verified: set[str], content: bytes,
             capture: Mapping[str, Any], *, agent: Any, reader: str, evidence_label: str,
             out: Path | None) -> dict[str, Any]:
    """Run the admitted items through the agent on seeded disposable hosts (module
    docstring); per-item results in three buckets."""
    import shutil
    import tempfile

    from evals import runner
    from evals.score import outcome_of, outcome_summary
    from integrations.source_mirror import AUDIENCE_SCHEMA, SourceMirror
    from ontology.core import Actor
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE

    actor = Actor(ANSWER_TENANT, frozenset({reader}))
    work = Path(tempfile.mkdtemp(prefix="plx-use-case-answers-"))
    try:
        empty = work / "state"
        (empty / CASE_RUNTIME_DIR).mkdir(parents=True)
        audience = work / "audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": ANSWER_TENANT,
                                        "audiences": {}}), encoding="utf-8")
        SourceMirror(empty / CASE_RUNTIME_DIR / MIRROR_FILE, None, audience,
                     tenant=ANSWER_TENANT).close()
        seeded: dict[str, Any] = {}

        def host_for(_item: Any) -> Any:
            host = runner.CaseHost(empty, audience, ANSWER_TENANT, [reader], fixture=True)
            try:
                seeded.update(_seed(host.state_dir, actor, content, capture))
            except BaseException:
                host.close()
                raise
            return host
        probe = host_for(None)
        try:
            reason = _unreachable(probe, seeded)
        finally:
            probe.close()
        if out is None:
            out = files.root / RUNS_DIR / batch
            shutil.rmtree(out, ignore_errors=True)  # this batch's previous runs
        scores = {} if reason else {
            s["item_id"]: s for s in runner.run_items(list(loaded), agent, host_for, out=out,
                                                      evidence_label=evidence_label)}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    refused = {r["id"]: r["reasons"] for r in loaded.refused}
    rows = []
    for card, item in zip(cards, items):
        expected = item["known_outcome"]
        bucket = ("scored" if item["label"]["status"] == "human_audited" else
                  "agreement_with_proposed" if item["label"]["status"] == "oracle_only"
                  and item["id"] in verified else "unscored")
        row: dict[str, Any] = {"item_id": item["id"], "card_id": card["id"],
                               "layer": card["layer"], "discovery": DISCOVERY}
        if item["id"] in refused:
            row.update(bucket="unscored", outcome="refused", codes=list(refused[item["id"]]))
        else:
            score = scores.get(item["id"])
            row.update(bucket=bucket, gap_only=bool(expected["gaps"]) and not expected["values"],
                       outcome="blocked" if score is None else outcome_of(score),
                       abstained=bool(score and score["abstained"]),
                       codes=[reason] if score is None else sorted(
                           set(score["codes"]) | {c for v in score["values"]
                                                  for c in v["codes"]}),
                       reached_original=None if score is None else
                       seeded["original_id"] in json.dumps(_events(out, item["id"])))
        rows.append(row)
    by_layer = {layer: _counts([r for r in rows if r["layer"] == layer])
                for layer in PROPOSAL_FAMILIES}
    scored = [scores[r["item_id"]] if r["item_id"] in scores else {"outcome": "blocked"}
              for r in rows if r["bucket"] == "scored" and r["outcome"] != "refused"]
    agreement = [r for r in rows if r["bucket"] == "agreement_with_proposed"
                 and r["outcome"] != "refused"]
    if not reason and not any(i["label"]["status"] == "human_audited" for i in items):
        # The run happened but no card carries a human audit: accuracy has nothing to be
        # over until the owner audits the cards (a later ask), never a pass.
        reason = NO_AUDIT
    return {
        "status": "blocked" if reason and reason != NO_AUDIT else "run",
        "blocked_reason": reason,
        "discovery": DISCOVERY, "evidence_label": evidence_label,
        "agent": getattr(agent, "name", None) or type(agent).__name__,
        "original": {"media_type": seeded.get("media_type"),
                     "pages": (seeded.get("extraction") or {}).get("page_count")},
        "totals": _counts(rows), "by_layer": by_layer,
        # accuracy only over human-audited items
        "scored": outcome_summary(scored),
        "agreement_with_proposed": {
            "basis": "agreement with a proposed value that recomputes from figures on its "
                     "anchored lines; not accuracy",
            "runs": len(agreement),
            "agree": sum(r["outcome"] == "correct" for r in agreement),
            "disagree": sum(r["outcome"] == "wrong" and not r["abstained"] for r in agreement),
            "abstained": sum(r["abstained"] for r in agreement),
            "blocked": sum(r["outcome"] == "blocked" for r in agreement)},
        "transcripts": str(out), "per_item": rows}


def _events(out: Path, item_id: str) -> list[Any]:
    """The agent's tool calls of an item's recorded runs (what it reached)."""
    return [json.loads(path.read_text(encoding="utf-8")).get("events")
            for path in sorted((out / item_id).glob("*.transcript.json"))]


def proposal_run(state_dir: str | Path, doc: Mapping[str, Any], *, evidence_label: str,
                 clock: Callable[[], datetime] | None = None,
                 answer: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Write and load the items, then report per item on the captured original; with
    ``answer`` (``agent``, ``reader``, ``out``) also run the admitted items through the
    agent (``answers``)."""
    from evals import runner
    from operating.use_case_eval_items import captured_original, line_spans, lines_of

    if evidence_label not in runner.EVIDENCE_LABELS:
        raise ProposalError("unknown_evidence_label")
    now = _now(clock)
    original, cards = _cards(doc)
    batch = doc["real_example"]["batch_id"]
    items = proposal_items(doc, now=now)
    # The body read is proven by a recorded capture only: nothing is written without one.
    content, capture = captured_original(state_dir, original["sha256"])
    files = IntakeState(state_dir)
    directory = files.root / ITEMS_DIR / PROPOSALS_DIR / batch
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for stale in directory.glob("*.json"):
        stale.unlink()
    for item in items:
        _write(directory / f"{item['id']}.json", item)
    _write(directory / "manifest.json", {
        "format": "plx-eval-exposed-manifest/v1", "split": "dev", "exposure": "exposed",
        "rule": "Proposed items over a pinned original (scripts.eval_use_case_items proposal "
                "run): dev exposed, never held out, never committed.",
        "items": [{"id": item["id"]} for item in items]})
    loaded = runner.load_items(directory, split="dev")
    lines = lines_of(content)
    per_item, consistent, anchors_total, out_of_range = [], 0, 0, 0
    verified: set[str] = set()
    answerability: list[str] = []
    for card, item in zip(cards, items):
        anchors = line_spans(content, card["source_lines"])
        anchors_total += len(anchors)
        out_of_range += sum(not a["in_range"] for a in anchors)
        output: dict[str, Any] = {"anchors": anchors}
        if card["layer"] == "arithmetic":
            extra, scored = _arithmetic(card, lines, anchors)
            output.update(extra)
            consistent += scored["status"] == "consistent"
            if scored["status"] == "consistent" and \
                    extra["operands"]["found_in_anchored_lines"] == extra["operands"]["total"]:
                verified.add(item["id"])
        else:
            scored = {"status": "unscored", "reason": "proposed_rubric_not_gold"}
        answerability.append(_answerability(item, anchors, item["id"] in verified))
        per_item.append({"item_id": item["id"], "card_id": card["id"], "layer": card["layer"],
                         "tool_output": output, "scored": scored,
                         "answerability": answerability[-1]})
    by_layer = {layer: sum(c["layer"] == layer for c in cards) for layer in PROPOSAL_FAMILIES}
    report = {
        "format": REPORT_FORMAT, "batch": batch, "evidence_label": evidence_label,
        "generated_at": now,
        "items_label": (f"PROPOSED items ({by_layer['comprehension']} comprehension, "
                        f"{by_layer['arithmetic']} arithmetic), never gold; per-item "
                        "tool_output is what this run read, scored is kept apart"),
        "original": {"sha256": capture["sha256"], "bytes": capture["bytes"],
                     "lines": len(lines), "drive_citation": capture["drive_citation"],
                     "captured_at": capture["captured_at"], "route": capture["route"],
                     "body_read": "proven_by_recorded_capture"},
        "items": {"written": len(items), "loaded": len(loaded) + len(loaded.refused),
                  "admitted": len(loaded), "refused": loaded.refused, "by_layer": by_layer,
                  "directory": str(directory.relative_to(files.root))},
        "anchors": {"total": anchors_total, "out_of_range": out_of_range},
        "arithmetic": {"consistent": consistent,
                       "not_consistent": by_layer["arithmetic"] - consistent},
        "actual_case_answerability": _case_answerability(
            answerability, list(doc.get("missing_originals") or []),
            len(doc.get("provenance_conflicts") or [])),
        "live_product_capability": {"status": "not_run"},
        "provenance_conflicts": [{"subject": c.get("subject"), "status": "unresolved",
                                  "anchors": line_spans(content, c.get("source_lines") or [])}
                                 for c in doc.get("provenance_conflicts") or []],
        "policy_status": list(doc.get("policy_status") or []),
        "per_item": per_item}
    if answer is not None:
        report["answers"] = _answers(files, batch, cards, items, loaded, verified, content,
                                     capture, evidence_label=evidence_label, **answer)
        report["live_product_capability"]["status"] = report["answers"]["status"]
        if report["answers"]["status"] == "blocked":
            # the reader cannot reach the seeded original: a capture to renew, never a pass
            report["blocked_upstream"] = [{"part": "use_case_answers",
                                           "source": "case-originals", "cause": "capture_gap"}]
        elif report["answers"]["blocked_reason"] == NO_AUDIT:
            # answered, but nothing is scorable until a person audits the cards
            report["blocked_upstream"] = [{"part": "use_case_answers_scored",
                                           "source": "use-case-cards", "error_code": NO_AUDIT,
                                           "cause": "later_ask"}]
    reports = files.root / REPORTS_DIR
    reports.mkdir(exist_ok=True, mode=0o700)
    _write(reports / f"{batch}.json", report)
    return report


_READER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._@\-]{0,199}$")


def proposal_main(argv: list[str], *, environ: Mapping[str, str] | None = None,
                  graph_sender: Any = None, clock: Callable[[], datetime] | None = None,
                  agent: Any = None) -> int:
    from integrations.live_source_guard import LiveSourceAudience
    from ontology.core import Actor
    from operating.use_case_eval_items import CaptureRefused, capture_shared_original

    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_use_case_items proposal")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="write and load the proposed items; report per "
                                          "item on the captured original")
    run.add_argument("--cards", required=True, type=Path)
    run.add_argument("--state-dir", required=True, type=Path)
    run.add_argument("--evidence-label", required=True)
    run.add_argument("--capture", action="store_true",
                     help="first capture the original through the guarded shared-link reader")
    run.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    run.add_argument("--answer", action="store_true",
                     help="run the admitted items through the agent on hosts seeded from the "
                          "recorded capture, as --reader")
    run.add_argument("--reader", help="the employee principal the answer runs serve")
    run.add_argument("--out", type=Path, help="transcripts and scores (outside the repository)")
    run.add_argument("--claude-bin", default="claude")
    run.add_argument("--model")
    run.add_argument("--timeout", type=int, default=300)
    run.add_argument("--max-turns", type=int, default=25)
    args = parser.parse_args(argv)
    try:
        doc = json.loads(args.cards.read_text(encoding="utf-8"))
        original, _ = _cards(doc)
        if args.capture:
            if not args.principal or "*" in args.principal:
                raise ProposalError("capture_principal_invalid")
            # The capture service reads the one pinned link; employees see the result
            # only through their own audience (the record keeps the item's source ACL).
            actor = Actor(CAPTURE_TENANT, frozenset({args.principal}), "restricted")
            audience = LiveSourceAudience({"sharepoint:*": [args.principal]},
                                          tenant=CAPTURE_TENANT)
            capture_shared_original(args.state_dir, str(original.get("web_url")),
                                    expected_sha256=original["sha256"], actor=actor,
                                    audience=audience,
                                    environ=os.environ if environ is None else environ,
                                    sender=graph_sender, clock=clock)
        answer = None
        if args.answer:
            from operating.entitlements import _AGENT_PREFIXES  # agents are never principals
            from evals.runner import ClaudeCodeAgent
            if not isinstance(args.reader, str) or not _READER.match(args.reader) or \
                    args.reader.startswith(_AGENT_PREFIXES):
                raise ProposalError("reader_must_be_an_employee_principal")
            answer = {"reader": args.reader, "out": args.out, "agent": agent or ClaudeCodeAgent(
                binary=args.claude_bin, model=args.model, timeout=args.timeout,
                max_turns=args.max_turns)}
        report = proposal_run(args.state_dir, doc, evidence_label=args.evidence_label,
                              clock=clock, answer=answer)
    except CaptureRefused as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a code or class name, never a value or URL
        print(json.dumps({"error": str(exc) if isinstance(exc, ProposalError)
                          else type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None, **kwargs: Any) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["proposal"]:
        return proposal_main(args[1:], **kwargs)
    return use_case_intake.main(args, evaluation_items=build_items, **kwargs)


__all__ = ["FAMILY", "ITEMS_DIR", "PROPOSAL_FAMILIES", "ProposalError", "build_items", "main",
           "proposal_items", "proposal_run", "recompute"]


if __name__ == "__main__":
    sys.exit(main())
