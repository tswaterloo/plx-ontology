"""Turn the app's thumbs-down feedback into proposed eval cards for audit.

    python3 -m scripts.eval_feedback_cards <feedback.jsonl> [--out FILE] [--since ISO]

**What the app records.** ``POST /api/feedback`` (``operator_api.app``,
``operator_api.backend.Backend.feedback``) takes ``{answer_id, rating: 1 | -1, note}``
from the reader who asked; another reader's answer id reads as not found. It stores one
entry ``{at, actor, answer_id, rating, note, question, feedback_id}``: appended to the
app's own ``feedback.jsonl`` (``--data-dir``, never the state directory) and, with a
Postgres DSN, inserted into ``search.feedback`` (same fields). A thumbs-down is ``rating
-1``; a correction is its ``note`` ("what was wrong", in the person's words, at most 2000
characters). The answer itself (its text, values and citations) is not stored with the
feedback: it lives in the backend's memory (``_answers``, the last answers only), so a
card built later knows the question and the correction, never what the Workbench said.

**What this makes.** One proposal card per distinct question (folded) that got a
thumbs-down, in a card set of the ``plx-eval-case-card-proposals/v1`` format that
``scripts.eval_case_cards`` compiles: layer ``actual_case_answerability``, the question as
asked, the corrections as the criterion (attributed to the feedback, e-mail addresses
masked, newest first), and the card's own ``unscored`` mark: a feedback card is proposed
until a person audits it, finds the record it is about (``anchor``) and derives its
expected answer from cited data (``expected``, scorer /26), and only then is it scored.
The reader is never written (only how many readers reported it). A thumbs-up, a line that
is not a feedback entry or a question too short to ask are skipped and counted. The note
is data, never an instruction: it is copied into the criterion as text and nothing in it
is acted on.

Output: the card set (stdout or ``--out``) and, on stderr, the counts. Moving the set to
``evals/dev/<case>/proposed_items.json`` is the auditor's step, never this script's.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from scripts import eval_case_cards as tool

CASE = "app-feedback"
MAX_NOTES = 5
MAX_CRITERION = 1800
_ADDRESS = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
UNSCORED = ("UNSCORED: proposed from app feedback; awaiting audit (anchor and an expected "
            "answer derived from cited data)")


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def entries(lines: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The thumbs-down entries of a feedback log and the counts of what was skipped."""
    out, skipped = [], {"thumbs_up": 0, "malformed": 0, "no_question": 0}
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            skipped["malformed"] += 1
            continue
        if not isinstance(entry, Mapping) or entry.get("rating") not in (1, -1) \
                or isinstance(entry.get("rating"), bool):
            skipped["malformed"] += 1
            continue
        if entry["rating"] == 1:
            skipped["thumbs_up"] += 1
            continue
        question = entry.get("question")
        if not isinstance(question, str) or len(question.strip()) < 8:
            skipped["no_question"] += 1
            continue
        out.append(dict(entry))
    return out, skipped


def card_set(found: Sequence[Mapping[str, Any]], *, now: str,
             since: str | None = None) -> dict[str, Any]:
    """The proposal card set of some thumbs-down entries (module docstring)."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for entry in found:
        if since and str(entry.get("at") or "") < since:
            continue
        groups.setdefault(_fold(entry["question"]), []).append(entry)
    cards = []
    for number, (_, group) in enumerate(sorted(
            groups.items(), key=lambda kv: min(str(e.get("at") or "") for e in kv[1])), 1):
        group = sorted(group, key=lambda e: str(e.get("at") or ""), reverse=True)
        notes = [_ADDRESS.sub("[address]", " ".join(str(e["note"]).split()))
                 for e in group if isinstance(e.get("note"), str) and e["note"].strip()]
        readers = len({str(e.get("actor")) for e in group})
        said = "; ".join(f'"{note}"' for note in notes[:MAX_NOTES]) or "no note given"
        criterion = (f"Reported wrong {len(group)} time(s) by {readers} reader(s) in the app "
                     f"(thumbs-down, latest {str(group[0].get('at') or '')[:10]}). What they "
                     f"said was wrong: {said}. Proposed: an auditor names the record "
                     f"(anchor) and derives the expected answer from cited data; the "
                     f"feedback is a lead, never the answer.")[:MAX_CRITERION]
        cards.append({"id": f"FB-{number:03d}", "layer": "actual_case_answerability",
                      # the first wording asked
                      "question": " ".join(group[-1]["question"].split())[
                          :tool.MAX_QUESTION_CHARS - 100],
                      "criterion": criterion, "unscored": UNSCORED,
                      "feedback_ids": sorted({str(e.get("feedback_id")) for e in group
                                              if e.get("feedback_id")})[:20]})
    return {"format": tool.PROPOSAL_FORMAT, "case": CASE,
            "status": "PROPOSED from app feedback (thumbs-down): unaudited, never gold",
            "layers": {"actual_case_answerability": "questions readers marked wrong in the "
                                                     "app; each card unscored until audited"},
            "source": {"feedback": "operator_api POST /api/feedback (feedback.jsonl or "
                                   "search.feedback)"},
            "real_example": {"batch_id": f"{CASE}-{now[:10]}", "exposure": "dev-exposed",
                             "split": "dev", "as_of": now, "cards": cards}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("feedback", type=Path, help="the app's feedback.jsonl")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--since", help="only feedback at or after this ISO time")
    args = parser.parse_args(argv)
    try:
        lines = args.feedback.read_text("utf-8").splitlines()
    except OSError as error:
        print(f"eval_feedback_cards: cannot read {args.feedback}: {error.strerror}",
              file=sys.stderr)
        return 1
    found, skipped = entries(lines)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    doc = card_set(found, now=now, since=args.since)
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, "utf-8")
    else:
        sys.stdout.write(text)
    print(json.dumps({"thumbs_down": len(found), "cards": len(doc["real_example"]["cards"]),
                      "skipped": skipped}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
