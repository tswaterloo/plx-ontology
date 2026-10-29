"""Messy questions for the graded use-case cards: data, integrity check, items, latency.

    python3 -m scripts.eval_messy check            # variant integrity (exit 3 on a problem)
    python3 -m scripts.eval_messy latency RUN_DIR [RUN_DIR ...]   # tool-call p50/p90

**Variants** (``evals/messy/variants.json``, ``plx-eval-messy-variants/v1``): for every
compiled card (``scripts.eval_case_cards.compile_cards``) three questions a staff member
might really type, each tied to its card by ``(case, card_id)`` and the SHA-256 of the
card's question (``question_sha256``; a card whose question changed makes its variants
``messy_question_changed``, never silently reused):

- ``m1`` typos and lowercase: the question, lowercased, with a few misspellings;
- ``m2`` vague words and synonyms: the same question with none of its rare words
  (``rare_terms``: its distinctive words of at least five letters and its acronyms, minus
  the business's generic words ``eval_case_cards.generic_terms``, people's names and the
  everyday words ``EVERYDAY``); a party or product name the question cannot be asked
  without (``Amazon``, ``Sahajan``) is listed in the card's ``keep``;
- ``m3`` short conversational ("whats up with the g2 beauty quote").

The variants are written by hand (an LLM builder), never generated at run time, and the
case context ("In the quote-to-order use case") that the compiled question carries is
said the way a person would say it, or not at all.

**Integrity** (``check``): every compiled card has exactly ``m1``-``m3``; none leaks the
answer (no word of the card's criterion that its question does not hold, ``leak_term``,
and no number the question does not hold, ``leak_number``); each keeps the question's
meaning as far as a machine can tell (every number and identifier of the question is in
every variant, ``number_missing``; every sub-question of the card, ``asked_parts``, is
asked by a declared span of each variant, ``part_dropped``, so a short m3 or a vague m2
never answers half the card); ``m1`` is lowercase and differs from the question;
``m2`` shares no rare word (``source_term``); ``m3`` is short. Everything else about
meaning is the writer's job and the reviewer's.

**Items** (``messy_items``): each variant becomes one item derived from its card's
compiled item: id ``<dev-Fnn-nnnn>-m<k>-exposed``, ``variant`` ``paraphrase`` with
``parent_id`` the card's item, and ``question`` ``{text: <the card's question>,
paraphrases: [<variant>]}``. The agent is asked the variant only (the runner's phrasing
1, ``evals.runner.record``), while everything the scorer reads (``scoring``, ``label``,
``oracle``, ``root``, ``known_outcome`` and the question text its card words come from)
is the card's own, so the existing scorer applies unchanged. ``eval_case_cards run
--messy`` runs them beside their cards; without the flag nothing changes.

**Aggregate** (``messy_report``): a card counts only if its original and all three
variants are ``correct``; reported per case, per variant and in total, beside the clean
count.

**Latency** (``latency``): p50/p90 of each tool's call time (``events[].elapsed_ms``),
of the whole answer (``timing.answer_ms``), of the time outside tool calls (the model's
own turns: the answer minus its calls) and of the first search call, from recorded
transcripts (``<run>/work/runs/<item>/p*-r*.transcript.json`` or any directory holding
them).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "plx-eval-messy-variants/v1"
VARIANTS_PATH = ROOT / "evals" / "messy" / "variants.json"
KINDS = {"m1": "typos_lowercase", "m2": "vague_synonyms", "m3": "short_conversational"}
PHRASING = 1  # the runner's phrasing index of the variant (``question.paraphrases[0]``)
MIN_CHARS, MAX_CHARS = 8, 400
M1_MIN_SIMILARITY = 0.5
M3_MAX_WORDS = 12
STEM = 6  # two words sharing their first six letters are one word ("approval"/"approved")
_ID = re.compile(r"^(dev-F[1-9][0-9]?-[0-9]{4})-exposed$")
_DIGITS = re.compile(r"\d+")
# An acronym of the question (``FBA``, ``BOL``), never the letters of an identifier
# (``PO-00269``, ``AI-CAF``, ``MIDAS2``); matched case-sensitively in a variant ("or").
_ACRONYM = re.compile(r"(?<![\w-])[A-Z][A-Z&]{1,4}(?![\w-])")
_IDENTIFIER = re.compile(r"\S*\d\S*")
_WORD = re.compile(r"[a-z]+")
# Everyday English a vague question may use whatever the card says: never a source term.
EVERYDAY = frozenset("""
about above after again agree against along already among another answer anyone anything
asked based became because become before being below between bring built called cannot
change changed check could count cover covers different during either enough every first
found given going happen happens kind known later least maybe might money needed needs
never often other others people person place point really right roles same should shown
since single small start started still their there these thing things those three times
today under until using whose where which while would write wrote years yet
january february march april june july august september october november december
seven eight nine eleven twelve
""".split())


class MessyError(ValueError):
    """The variants file cannot be read as ``FORMAT`` (fail closed: no messy run)."""


def question_sha(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def load(path: Path | None = None) -> dict[str, Any]:
    """The variants document; ``MessyError`` when it is not ``FORMAT`` with a ``cards``
    list of well-formed entries."""
    path = Path(path or VARIANTS_PATH)
    try:
        doc = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise MessyError(f"messy_variants_unreadable:{type(error).__name__}") from None
    cards = doc.get("cards") if isinstance(doc, Mapping) else None
    if not isinstance(doc, Mapping) or doc.get("format") != FORMAT or not isinstance(cards, list):
        raise MessyError("messy_variants_format")
    for entry in cards:
        variants = entry.get("variants") if isinstance(entry, Mapping) else None
        if not isinstance(entry, Mapping) or not all(
                isinstance(entry.get(key), str) and entry[key]
                for key in ("case", "card_id", "question_sha256")) \
                or not isinstance(variants, Mapping) \
                or not all(isinstance(k, str) and isinstance(t, str) for k, t in variants.items()) \
                or not isinstance(entry.get("keep", []), list):
            raise MessyError("messy_variants_entry")
    return dict(doc)


def by_card(doc: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    for entry in doc["cards"]:
        key = (entry["case"], entry["card_id"])
        if key in out:
            raise MessyError(f"messy_variants_duplicate:{key[0]}/{key[1]}")
        out[key] = entry
    return out


# --------------------------------------------------------------------------- words

def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _same(word: str, term: str) -> bool:
    return word == term or (len(term) >= STEM and word[:STEM] == term[:STEM]) \
        or (word.endswith("s") and word[:-1] == term)


def _has(text: str, terms: Iterable[str]) -> list[str]:
    words = set(_words(text))
    return sorted({t for t in terms if any(_same(w, t) for w in words)})


def _subject(text: str) -> set[str]:
    from evals import score
    return score._subject_terms(text)


def _ignored(keep: Iterable[str], names: Iterable[str]) -> set[str]:
    from evals import score
    from scripts import eval_case_cards as cards
    return (set(cards.generic_terms()) | set(score.TOPIC_COMMON_TERMS) | EVERYDAY
            | set(names) | {k.casefold() for k in keep})


def rare_terms(question: str, case: str, keep: Sequence[str] = (),
               names: Iterable[str] = ()) -> list[str]:
    """The question's rare words a vague variant (``m2``) may not use: its distinctive
    words (``evals.score._subject_terms``) and its case name's words, minus generic,
    everyday and kept words and people's names (folded; acronyms: ``acronyms``)."""
    ignored = _ignored(keep, names)
    words = _subject(_IDENTIFIER.sub(" ", question)) | _subject(case.replace("-", " "))
    return sorted(w for w in words
                  if w not in ignored and not any(_same(i, w) for i in ignored if len(i) >= STEM))


def acronyms(question: str, keep: Sequence[str] = ()) -> list[str]:
    """The question's acronyms (``FBA``, ``BOL``, ``QC``) a vague variant may not use, as
    written (matched case-sensitively, so the word "or" is never the acronym ``OR``)."""
    kept = {k.casefold() for k in keep}
    return sorted({a for a in _ACRONYM.findall(question) if a.casefold() not in kept})


def leak_terms(question: str, topic: Sequence[str], keep: Sequence[str] = (),
               names: Iterable[str] = ()) -> list[str]:
    """The card-specific words of its answer (``scoring.topic_terms``: the words the
    scorer ties a claim to the card by, mostly from its criterion) that its question does
    not hold: no variant may say them."""
    ignored = _ignored(keep, names)
    asked = _subject(question)
    return sorted(w for w in set(topic) - ignored
                  if not any(_same(a, w) or _same(w, a) for a in asked))


# A card's sub-questions (``asked_parts``): its clauses joined by "and"/"or" before a
# question word ("When did the stock move ..., and was that at the BOL"), or by ";", and
# each parenthetical of two or more words ("(seven quoted statement invoices)",
# "(sold-to, ship-from, ...)"); "(example A)" is a qualifier, never a part.
_PART_SPLIT = re.compile(
    r"(?:,\s*|\s+)(?:and|or)\s+(?=(?:what|which|when|who|whom|how|is|was|are|were|did|"
    r"does|do|can|could|has|have|had|where|whether|why|by whom)\b)|;\s*(?=[A-Za-z])", re.I)
_PAREN = re.compile(r"\(([^()]*)\)")
MIN_PART_SIMILARITY = 0.5  # an m1 span against the part it asks (typos, never a rewrite)


def asked_parts(question: str) -> list[str]:
    """The sub-questions of a card's question, in order (clauses first, then the
    parentheticals that are parts); a variant must ask every one of them."""
    text = question.strip().rstrip("?").strip()
    paren = [p for p in _PAREN.findall(text) if len(
        [w for w in re.findall(r"[A-Za-z]{3,}", p) if w.casefold() != "example"]) >= 2]
    body = re.sub(r"\s+", " ", _PAREN.sub(
        lambda m: "" if m.group(1) in paren else m.group(0), text)).replace(" ,", ",")
    # a ";" or "and" inside a kept parenthetical never splits ("(... ; not ...)")
    masked = _PAREN.sub(lambda m: "(" + "_" * len(m.group(1)) + ")", body)
    out, start = [], 0
    for match in _PART_SPLIT.finditer(masked):
        out.append(body[start:match.start()])
        start = match.end()
    out.append(body[start:])
    return [s.strip(" ,") for s in out if s.strip(" ,")] + [f"({p})" for p in paren]


def part_problems(entry: Mapping[str, Any], question: str) -> list[tuple[str, str]]:
    """``(variant, code)`` for every sub-question a variant does not ask: the entry's
    ``parts`` must list the question's ``asked_parts`` exactly (``messy_parts_changed``),
    and each variant must hold, for every part, at least one declared span
    (``spans.<m_k>``) that is its own text (``part_dropped:<n>``, 1-based), the spans of
    different parts never overlapping (``part_spans_overlap``) and never a single word the
    part does not hold (``part_too_thin:<n>``: "not" never asks "what remains
    unverified"); an m1 span reads as its part with typos (``part_rewritten:<n>``)."""
    parts = entry.get("parts")
    asked = asked_parts(question)
    if not isinstance(parts, list) or [p.get("asked") if isinstance(p, Mapping) else None
                                       for p in parts] != asked:
        return [("", "messy_parts_changed")]
    out: list[tuple[str, str]] = []
    for kind in KINDS:
        text = str(entry["variants"].get(kind) or "").casefold()
        owner: dict[int, int] = {}
        for n, part in enumerate(parts, 1):
            spans = (part.get("spans") or {}).get(kind) if isinstance(
                part.get("spans"), Mapping) else None
            spans = [str(x) for x in spans or [] if isinstance(x, str) and _words(x)]
            found = [(text.find(x.casefold()), len(x)) for x in spans]
            if not spans or any(at < 0 for at, _ in found):
                out.append((kind, f"part_dropped:{n}"))
                continue
            for at, size in found:
                for i in range(at, at + size):
                    if owner.setdefault(i, n) != n:
                        out.append((kind, "part_spans_overlap"))
                        break
            said = [w for x in spans for w in re.findall(r"[a-z0-9]{2,}", x.casefold())]
            if len(said) < 2 and not {w[:4] for w in said} & {
                    w[:4] for w in re.findall(r"[a-z0-9]{2,}", part["asked"].casefold())}:
                out.append((kind, f"part_too_thin:{n}"))  # one word, none of the part's
            if kind == "m1" and difflib.SequenceMatcher(
                    None, " ".join(spans).casefold(), part["asked"].casefold()
            ).ratio() < MIN_PART_SIMILARITY:
                out.append((kind, f"part_rewritten:{n}"))
    return sorted(set(out))


def numbers(text: str) -> list[str]:
    return sorted(set(_DIGITS.findall(text)))


def asked_text(card: Mapping[str, Any], item: Mapping[str, Any]) -> str:
    """What a variant must keep: the card's question and the record its compiled item
    names (``root``: an anchor such as ``PO5447``), never the compiler's case prefix."""
    root = item.get("root") or {}
    anchor = root.get("identifier") if root.get("identifier_kind") not in (
        "case_card_batch", "document_name") else None
    return " ".join(str(part) for part in (anchor, card.get("question")) if part)


# --------------------------------------------------------------------------- check

def _compiled(sets: Mapping[str, Any] | None = None) -> tuple[Any, list[dict[str, Any]]]:
    from scripts import eval_case_cards as cards
    sets = cards.card_sets() if sets is None else sets
    compiled, _ = cards.compile_cards(sets, now="2026-10-29T00:00:00Z")
    return sets, compiled


def card_of(sets: Mapping[str, Any], case: str, card_id: str) -> Mapping[str, Any] | None:
    from scripts import eval_case_cards as cards
    doc = sets.get(case)
    for card in cards._cards(doc[0]) if doc else ():
        if isinstance(card, Mapping) and card.get("id") == card_id:
            return card
    return None


def check(doc: Mapping[str, Any], sets: Mapping[str, Any],
          compiled: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Every integrity problem of the variants against the compiled cards (module
    docstring, **Integrity**): ``{case, card_id, variant, code}`` rows, none when clean."""
    from scripts import eval_case_cards as cards
    names = cards.person_names()
    index = by_card(doc)
    problems: list[dict[str, str]] = []

    def bad(case: str, card_id: str, variant: str, code: str) -> None:
        problems.append({"case": case, "card_id": card_id, "variant": variant, "code": code})

    wanted = {(r["case"], str(r["card_id"])): r for r in compiled}
    for key in sorted(set(index) - set(wanted)):
        bad(key[0], key[1], "", "messy_card_not_compiled")
    for key in sorted(wanted):
        case, card_id = key
        entry = index.get(key)
        if entry is None:
            bad(case, card_id, "", "messy_variants_missing")
            continue
        card = card_of(sets, case, card_id) or {}
        question = str(card.get("question") or "")
        if entry["question_sha256"] != question_sha(question):
            bad(case, card_id, "", "messy_question_changed")
            continue
        variants = entry["variants"]
        if set(variants) != set(KINDS):
            bad(case, card_id, "", "messy_variant_set")
        keep = [str(k) for k in entry.get("keep") or []]
        item = wanted[key]["item"]
        asked = asked_text(card, item)
        topic = list((item.get("scoring") or {}).get("topic_terms") or [])
        leaks = leak_terms(asked, topic, keep, names)
        # the record's own identifier ("PO 5503") is the question's subject, never a
        # source word a vague variant must avoid
        rare = rare_terms(question, case, keep, names)
        letters = acronyms(question, keep)
        # every number of the question and its record is kept; one the compiled
        # question's prefix holds ("Draft 8") may be said, any other is a leak
        required = set(numbers(asked))
        allowed = required | set(numbers(str((item.get("question") or {}).get("text") or "")))
        texts = [variants.get(k, "") for k in KINDS]
        if len({t.casefold().strip() for t in texts}) != len(texts):
            bad(case, card_id, "", "messy_duplicate_text")
        for kind, code in part_problems(entry, question):
            bad(case, card_id, kind, code)
        for kind in KINDS:
            text = variants.get(kind)
            if not isinstance(text, str):
                continue
            if not MIN_CHARS <= len(text) <= MAX_CHARS:
                bad(case, card_id, kind, "length")
            for term in _has(text, leaks):
                bad(case, card_id, kind, f"leak_term:{term}")
            for number in sorted(set(numbers(text)) - allowed):
                bad(case, card_id, kind, f"leak_number:{number}")
            for number in sorted(required - set(numbers(text))):
                bad(case, card_id, kind, f"number_missing:{number}")
            if kind == "m1":
                lowered = asked.casefold()
                words = _IDENTIFIER.sub(" ", text)  # an identifier keeps its case
                if words != words.casefold() or text.casefold().strip() == lowered.strip():
                    bad(case, card_id, kind, "m1_not_lowercase_typo")
                elif not set(_words(text)) - set(_words(lowered)):
                    bad(case, card_id, kind, "m1_no_typo")
                elif difflib.SequenceMatcher(None, text, lowered).ratio() < M1_MIN_SIMILARITY:
                    bad(case, card_id, kind, "m1_too_far")
            if kind == "m2":
                for term in _has(_IDENTIFIER.sub(" ", text), rare):
                    bad(case, card_id, kind, f"source_term:{term}")
                said = set(re.findall(r"[\w&]+", _IDENTIFIER.sub(" ", text)))
                for term in sorted(set(letters) & said):
                    bad(case, card_id, kind, f"source_term:{term}")
            if kind == "m3" and len(text.split()) > M3_MAX_WORDS:
                bad(case, card_id, kind, "m3_not_short")
    return problems


# --------------------------------------------------------------------------- items

def messy_id(item_id: str, kind: str) -> str:
    match = _ID.match(item_id)
    if not match or kind not in KINDS:
        raise MessyError(f"messy_id:{item_id}")
    return f"{match.group(1)}-{kind}-exposed"


def messy_items(compiled: Sequence[Mapping[str, Any]], doc: Mapping[str, Any],
                sets: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(entries, skipped): one run entry per variant of each compiled card (module
    docstring, **Items**), each ``{case, card_id, layer, item, messy, parent_id}`` (plus
    the card's ``unscored``); ``skipped`` rows ``{case, card_id, reason}`` for a card with
    no variants or whose question changed (its variants never run on the new question)."""
    from evals import runner
    index = by_card(doc)
    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in compiled:
        key = (row["case"], str(row["card_id"]))
        entry = index.get(key)
        card = card_of(sets, *key) or {}
        if entry is None:
            skipped.append({"case": key[0], "card_id": key[1], "reason": "messy_variants_missing"})
            continue
        if entry["question_sha256"] != question_sha(str(card.get("question") or "")):
            skipped.append({"case": key[0], "card_id": key[1], "reason": "messy_question_changed"})
            continue
        parent = row["item"]
        for kind in KINDS:
            text = entry["variants"].get(kind)
            if not isinstance(text, str):
                skipped.append({"case": key[0], "card_id": key[1],
                                "reason": f"messy_variant_missing:{kind}"})
                continue
            item = {**json.loads(json.dumps(parent)), "id": messy_id(parent["id"], kind),
                    "variant": "paraphrase", "parent_id": parent["id"],
                    "question": {"text": parent["question"]["text"], "paraphrases": [text]}}
            if runner.validate_item(item):
                skipped.append({"case": key[0], "card_id": key[1],
                                "reason": f"messy_schema_invalid:{kind}"})
                continue
            entries.append({**{k: row[k] for k in ("case", "card_id", "layer")},
                            **({"unscored": row["unscored"]} if row.get("unscored") else {}),
                            "item": item, "messy": kind, "parent_id": parent["id"]})
    return entries, skipped


def messy_report(rows: Sequence[Mapping[str, Any]], messy_rows: Sequence[Mapping[str, Any]],
                 skipped: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """A card counts only if its original and all its variants are ``correct`` (module
    docstring, **Aggregate**). ``rows``: the cards' own rows, ``messy_rows``: the
    variants' (``messy``, ``parent_id``); a card whose variants did not all run is
    ``incomplete``, never counted as passing."""
    by_parent: dict[str, dict[str, str]] = {}
    for row in messy_rows:
        by_parent.setdefault(row["parent_id"], {})[row["messy"]] = row["outcome"]

    def tally(own: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        out = {"cards": len(own), "original_correct": 0, "all_correct": 0, "incomplete": 0,
               **{f"{kind}_correct": 0 for kind in KINDS}}
        for row in own:
            variants = by_parent.get(row["item_id"], {})
            original = row["outcome"] == "correct"
            out["original_correct"] += original
            for kind in KINDS:
                out[f"{kind}_correct"] += variants.get(kind) == "correct"
            if set(variants) != set(KINDS) or row["outcome"] == "not_run" \
                    or "not_run" in variants.values():
                out["incomplete"] += 1
            elif original and all(v == "correct" for v in variants.values()):
                out["all_correct"] += 1
        return out

    ran = [r for r in rows if r["item_id"] in by_parent]
    cases = sorted({r["case"] for r in ran})
    return {"rule": "a card counts only if the original and all its messy variants "
                    "(m1 typos+lowercase, m2 vague/synonyms, m3 short conversational) are "
                    "correct",
            "kinds": dict(KINDS), "totals": tally(ran),
            "cases": {case: tally([r for r in ran if r["case"] == case]) for case in cases},
            "outcomes": {kind: _outcomes([r for r in messy_rows if r["messy"] == kind])
                         for kind in KINDS},
            "original_outcomes": _outcomes(ran),
            "skipped": [dict(s) for s in skipped]}


def _outcomes(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row["outcome"]] = out.get(row["outcome"], 0) + 1
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------- latency

SEARCH_TOOLS = ("plx.documents", "plx.find")


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..100) of ``values``; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def _stats(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": percentile(values, 50), "p90": percentile(values, 90),
            "max": max(values) if values else None}


def transcripts(paths: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        path = Path(path)
        found += [path] if path.is_file() else sorted(path.rglob("p*-r*.transcript.json"))
    return found


def latency(paths: Sequence[Path]) -> dict[str, Any]:
    """Per step p50/p90 in milliseconds (module docstring, **Latency**)."""
    per_tool: dict[str, list[float]] = {}
    answer, first_search, calls, model, sessions = [], [], [], [], 0
    for path in transcripts(paths):
        try:
            doc = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        sessions += 1
        events = [e for e in doc.get("events") or [] if isinstance(e, Mapping)
                  and e.get("type") == "tool_call" and isinstance(e.get("elapsed_ms"), (int, float))]
        calls.append(len(events))
        for event in events:
            per_tool.setdefault(str(event.get("name")), []).append(event["elapsed_ms"])
        search = next((e["elapsed_ms"] for e in events if e.get("name") in SEARCH_TOOLS), None)
        if search is not None:
            first_search.append(search)
        answer_ms = (doc.get("timing") or {}).get("answer_ms")
        if isinstance(answer_ms, (int, float)):
            answer.append(answer_ms)
            # what the session spent outside its tool calls: the model's own turns
            model.append(max(0, answer_ms - sum(e["elapsed_ms"] for e in events)))
    every = [v for values in per_tool.values() for v in values]
    return {"unit": "ms", "transcripts": sessions,
            "answer": _stats(answer), "outside_tools": _stats(model),
            "first_search": _stats(first_search),
            "tool_call": _stats(every), "calls_per_session": _stats(calls),
            "by_tool": {name: _stats(values) for name, values in sorted(per_tool.items())}}


# --------------------------------------------------------------------------- main

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_messy",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check_cmd = commands.add_parser("check", help="variant integrity against the compiled cards")
    check_cmd.add_argument("--variants", type=Path, default=VARIANTS_PATH)
    lat = commands.add_parser("latency", help="tool-call p50/p90 from recorded transcripts")
    lat.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if args.command == "latency":
        print(json.dumps(latency(args.paths), indent=1, sort_keys=True))
        return 0
    try:
        doc = load(args.variants)
        sets, compiled = _compiled()
        problems = check(doc, sets, compiled)
    except MessyError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({"cards": len(compiled), "variants": 3 * len(compiled),
                      "problems": len(problems), "detail": problems[:200]}, indent=1))
    return 3 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
