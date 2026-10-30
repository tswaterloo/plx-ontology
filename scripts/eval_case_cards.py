"""The product's answers to every committed use-case card set (live check
``eval.case_cards_answers``).

    python3 -m scripts.eval_case_cards run --work-dir {tmp} [--previous-report=...]
        [--eval-principal] [--measure-only]

Thirteen case card sets live under ``evals/dev/<case>/proposed_items.json``: the RRO
Draft 8 set (``plx-eval-case-cards/v1``, compiled by ``scripts.eval_use_case_items``'s
``proposal_items``: comprehension F21, arithmetic F22) and twelve proposal sets
(``plx-eval-case-card-proposals/v1``: amazon-fba, artwork-lifecycle, asset-tracker,
brand-requirements, expired-disposition, grants-program, packaging-sourcing, procure-to-gl, quote-to-order,
ricardo-maps, rnd-maps, screening). Every card becomes one dev
exposed ``plx-eval-item/v1`` item (``-exposed``: developer-visible calibration material,
never held out). A proposal card carries a question, a rubric criterion and the
advisories, case originals and live sources it cites, never an expected value: its item
is family ``F23`` (comprehension: what the case's documents say), ``F24`` (tracing: a
record chain across the live sources) or ``F25`` (actual_case_answerability: can the
product answer the real case question today), ``known_outcome`` ``partial`` with no
values and no gaps, label ``oracle_only`` whose note holds the criterion. Numbers run
per family across the sets in case-name order, so ids are stable while the files are.

A card that cannot be compiled (an unknown layer, a missing question) or whose item
fails the item schema (``evals.runner.validate_item``) is **refused** with its reason and
never runs. So is a card (RRO or proposal) that points at one record without naming it ("for this
AO", "product P": ``needs_anchor``) when neither the card nor its set carries an
``anchor`` (``{identifier, object_type[, identifier_kind]}``):
``card_anchor_missing``. Its abstention would say nothing about the agent. An anchor is
the item's root and is named in the question. A card naming a case original the registry
(``examples/case_originals.json``) does not list is ``card_original_unregistered``. A
refused card keeps its number, so the ids after it do not move.

**Originals.** A proposal item's ``oracle.derived_from`` carries the byte pin
(``original://sha256/<hex>``) of each case original the card names that the registry
pinned; the runner counts them (``evals.runner.originals_held``), so an abstention while
the host did not hold one or more of those bytes scores ``blocked`` (``originals_not_held``), never
``wrong``. A card naming a registry ``gap`` row with no known SHA-256 (a map known only from
an artifact invitation) carries ``original://unpinned/<row id>`` instead: no one holds
those bytes, so the scorer blocks its abstention the same way
(``evals.score.UNHELD_ORIGINAL``). A stated answer is still scored.

**Evidence** (scorer ``plx-eval-scorer/17``):
a proposal item is ``correct`` only with an evidenced claim (``evals.score.evidenced_claims``);
its ``partial`` label alone is ``label_only``, an abstention. Each proposal item carries
its card-specific words (``scoring.topic_terms``, ``_with_topic_terms``, scorer /18): a
claim is on the card's topic through one of them, never through a case-title word, a
word of over a quarter of its set's cards or a word of over a third of the committed
sets (``generic_terms``), and only beside a second word of the card (scorer /19). People's
names (``topic_names``, from the registry's owners) and e-mail addresses never count.
The report counts
``correct_with_evidence`` and ``label_only`` per case, layer and in total.

**Time pin**: a set's ``as_of`` is
when its cards were written, never a time the question asks about, so a proposal item is
pinned by the set's digest (``case_cards``) and the runner states no time. A card that
carries its own ``as_of`` is time-pinned (``case_cards_as_of``) and the runner states it.

**Unscored.** A set whose ``layers.<layer>`` description starts with ``UNSCORED``
(``UNSCORED_MARK``) marks that layer not scorable yet. Its cards run; the mark is written
into each item's ``scoring.unscored``, so the scorer itself scores the run ``unscored``
(scorer /20, ``evals.score.unscored_mark``; before, only this report's row said so and
the written score still said ``wrong``); each row is ``unscored`` (a blocked run stays
``blocked``), never correct or wrong, and ``unscored`` is counted beside ``blocked``.
The admission rule (``evals.admission.admissible``) is not applied: it keeps
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
case. ``--per-case N`` (a manual sample, never the verifier's command) runs at most the
first N cards of each selected case, ``--items A,B`` (also a manual sample) only the named
items of the selected cases (an id not selected is an error); the report then carries
``sample`` (``per_case`` or ``items``, the cards it left out) so a sample's counts are
never read as a full run.

**Messy.** ``--messy`` (a manual measure, never the verifier's command) also runs each
selected card's three messy variants (``scripts.eval_messy``: typos, vague words, short
conversational; ``evals/messy/variants.json``), each asked as its own item's phrasing 1
and scored by the card's own scoring. Their rows stay out of every count above; the
report's ``messy`` block says how many cards had the original and every variant
``correct``. Without the flag the run and its report are unchanged.

**Reader.** Every item runs through ``evals.runner`` with one headless Claude Code
session (``evals.runner.ClaudeCodeAgent``: read-only ``plx.*`` tools behind the tool gate,
k=1) running the investigate agent (``CARD_AGENT``: ``plx.agent.investigate``, whose
reads include ``plx.agenda``) on the persistent host as one reader
(``scripts.eval_real_agent.LiveHost``, the
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
``blocked_upstream`` and no session starts; so is a run whose served projection was
built from another schema than this checkout's (``projection_schema_drift``: every tool
would state ``schema_drift``; the host's next build on this checkout clears it). ``--workers`` sessions run at once; a card not
started before ``--budget-seconds`` is ``not_run`` (budget).

**Report.** Per case, per layer and in total: cards, refused, run, not_run, answered,
abstained, no_answer, blocked, gap_only (an answer with gaps and no value), coverage
(answered over run plus refused cards: a refused card is a failed card, never skipped)
and confident_unsupported (a wrong, non-abstained answer with an
unsupported assertion or a failed claim check), and abstain_reasons: the most frequent
gap reason codes of the abstained answers (cards per code; a non-code reason is
``other``), so a run that abstains says why. **Answered** is this check's own rule: a
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
REASON_CODE = __import__("re").compile(r"[a-z][a-z0-9_]{0,63}")  # an answer gap's reason code
MAX_ABSTAIN_REASONS = 8  # the most frequent reason codes reported per case, layer and total
# The agent definition a card's session runs (operating/agent_definitions.json): a card is
# a question to investigate, never an action, and only this definition lists every read the
# cards need (plx.agenda for the meeting-agenda cards; the default agent is at its tool
# budget without it). The runner's gate still admits read-only tools only.
CARD_AGENT = "plx.agent.investigate"
# The runner phrasing a messy variant is asked as (``scripts.eval_messy.PHRASING``).
MESSY_PHRASING = 1
# A card set's ``layers.<layer>`` description that starts with this word marks the layer
# not scorable yet ("UNSCORED until the originals ... are read"): its cards still run, and
# their rows are ``unscored``, never correct or wrong (module docstring, **Unscored**).
UNSCORED_MARK = scoring.UNSCORED_MARK
# The committed registry of case originals (``scripts.case_originals``): a card's
# ``originals`` name its rows (``co-``/``nr-``/``rv-`` ids), each with the bytes it pinned.
REGISTRY_PATH = ROOT / "examples" / "case_originals.json"
_RE = __import__("re")
# A question about one unnamed record: lowercase "this" before a record noun ("this AO",
# "this asset"; never the case's own map, brief, proposal, agenda or source, which the use
# case names, and never a plural "these documents"), or a placeholder letter ("product P",
# "customer X"; never a range "A to E", an article, a pronoun or a series "R numbers").
# Case-sensitive: "This" opening a sentence is not a pointer to a record.
_NOT_RECORDS = (r"maps?|briefs?|proposals?|agendas?|use\s+cases?|cases?|walkthroughs?|"
                r"documents?|examples?|sources?|questions?|sets?|layers?|cards?|files?")
_DEICTIC = _RE.compile(r"\bthis\s+(?!(?:" + _NOT_RECORDS + r")\b)[A-Za-z]")
_PLACEHOLDER = _RE.compile(r"(?<![\w'’])(?!(?:examples?|to|and|or|from|through|of|in|on|at|"
                           r"by)\b)[a-z][a-z-]*\s+(?![AI]\b)[A-Z](?=[\s,?;:)]|$)"
                           r"(?!\s+(?:numbers|codes|series|items|lines)\b)")


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


def unscored_mark(doc: Any, layer: Any) -> str | None:
    """The set's ``layers.<layer>`` description when it marks the layer unscored
    (``UNSCORED_MARK``), else None (a layer without that mark is scored)."""
    layers = doc.get("layers") if isinstance(doc, Mapping) else None
    text = layers.get(layer) if isinstance(layers, Mapping) and isinstance(layer, str) \
        else None
    return text[:scoring.MAX_UNSCORED_CHARS] if isinstance(text, str) \
        and text.startswith(UNSCORED_MARK) else None


def needs_anchor(question: str) -> bool:
    """Whether a card question names one record only by pointing at it ("this AO",
    "product P"): it cannot be answered, or scored, without the record it means."""
    return bool(_DEICTIC.search(question) or _PLACEHOLDER.search(question))


def _anchor(doc: Mapping[str, Any], card: Mapping[str, Any]) -> dict[str, str] | None:
    """The record a card is about (``{identifier, object_type[, identifier_kind]}``): the
    card's own ``anchor``, else, for a card that points at one record (``needs_anchor``),
    its set's ``real_example.anchor``; or None. A malformed anchor is no anchor, so a
    card that needs one is refused ``card_anchor_missing``."""
    shared = (doc.get("real_example") or {}).get("anchor") \
        if needs_anchor(card["question"]) else None
    for anchor in (card.get("anchor"), shared):
        if isinstance(anchor, Mapping) and isinstance(anchor.get("identifier"), str) \
                and anchor["identifier"].strip() and isinstance(anchor.get("object_type"), str):
            out = {"identifier": anchor["identifier"].strip()[:200],
                   "object_type": anchor["object_type"]}
            if isinstance(anchor.get("identifier_kind"), str):
                out["identifier_kind"] = anchor["identifier_kind"]
            return out
    return None


def registry_pins(path: Path = REGISTRY_PATH) -> dict[str, str | None]:
    """Case original id -> the byte pin (``original://sha256/<hex>``) the registry holds
    for it, or None for a file row it lists without pinned bytes (unfetched, unpinned,
    oversize: the host may still hold them, unchecked). A ``gap`` row (an original no
    source-pin record delivered) pins its expected SHA-256 when the row knows it, else
    is ``original://unpinned/<id>`` (``evals.score.UNHELD_ORIGINAL``): bytes no one holds,
    so an abstention on a card naming it is ``originals_not_held``."""
    rows = json.loads(Path(path).read_text("utf-8")).get("rows") or []
    hex64 = _RE.compile(r"^[0-9a-f]{64}$")
    out: dict[str, str | None] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        sha = (row.get("expected") or {}).get("sha256") \
            if isinstance(row.get("expected"), Mapping) else None
        known = isinstance(sha, str) and bool(hex64.match(sha))
        if row.get("status") == "gap":
            out[row["id"]] = (f"original://sha256/{sha}" if known
                              else f"original://unpinned/{row['id']}")
        else:
            out[row["id"]] = (f"original://sha256/{sha}" if row.get("status") == "pinned"
                              and known else None)
    return out


# The registry's record kind of a case's own submitted map or brief (its case original:
# ``case_originals``), as against a file linked from it.
CASE_ORIGINAL_KIND = "original submitted process map"
_HEX64 = _RE.compile(r"^[0-9a-f]{64}$")
# Keys of a set's ``source`` that pin one original by its bytes: a map's archive or the
# Markdown original (``{..., "sha256": <hex>}``).
_SOURCE_ORIGINAL_KEYS = ("original_zip", "original_archive", "original")


def case_originals(path: Path = REGISTRY_PATH) -> dict[str, list[str]]:
    """Registry case -> the SHA-256 of each pinned row of ``CASE_ORIGINAL_KIND`` (the
    case's own submitted map or brief), sorted."""
    rows = json.loads(Path(path).read_text("utf-8")).get("rows") or []
    out: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("status") != "pinned" \
                or row.get("record_kind") != CASE_ORIGINAL_KIND:
            continue
        sha = (row.get("expected") or {}).get("sha256") \
            if isinstance(row.get("expected"), Mapping) else None
        if isinstance(row.get("case"), str) and isinstance(sha, str) and _HEX64.match(sha):
            out.setdefault(row["case"], set()).add(sha)
    return {case: sorted(shas) for case, shas in out.items()}


def use_case_documents(path: Path | None = None,
                       registry: Path = REGISTRY_PATH) -> dict[str, list[str]]:
    """Use case id -> the SHA-256 of each document the use-case catalog
    (``operating.use_case_catalog``, ``examples/use_case_catalog.json``) names as the use
    case's own that the registry pins by its bytes: an ``original`` entry whose registry
    row is ``pinned`` with a SHA-256, whatever its record kind (the Ricardo maps are
    ``... process map package`` rows, the procure-to-gl map a rendered text). A
    SharePoint item names no bytes (its captured version may change), and a row not
    pinned names none either: neither is a source document. A catalog that cannot be read
    is ``CatalogError`` (fail closed: no compile on a guess)."""
    from operating import use_case_catalog

    catalog = use_case_catalog.load(path)
    rows = json.loads(Path(registry).read_text("utf-8")).get("rows") or []
    pinned: dict[str, str] = {}
    for row in rows:
        sha = (row.get("expected") or {}).get("sha256") \
            if isinstance(row, Mapping) and isinstance(row.get("expected"), Mapping) else None
        if isinstance(row, Mapping) and row.get("status") == "pinned" \
                and isinstance(row.get("id"), str) and isinstance(sha, str) \
                and _HEX64.match(sha):
            pinned[row["id"]] = sha
    return {case.id: sorted({pinned[doc["original"]] for doc in case.documents
                             if doc.get("original") in pinned})
            for case in catalog.use_cases}


def set_documents(doc: Any, originals: Mapping[str, Sequence[str]]) -> set[str]:
    """A card set's own source documents, by byte SHA-256 (scorer /21,
    ``evals.score.evidenced_claims``): its ``source.watched_originals`` files, its
    ``source`` archive or original (``_SOURCE_ORIGINAL_KEYS``), its case's own original
    in the registry (``originals``: ``case_originals``, by the set's ``case``), its
    maps' own files (``maps.<prefix>.source_files``) and each related advisory's file the
    set pins as its source by byte SHA-256 (``source.related_advisories[].source_revision``
    of ``sha256:<hex>``, alone or as a ``;``-separated part: ``_revision_sha``)."""
    if not isinstance(doc, Mapping):
        return set()
    source = doc.get("source") if isinstance(doc.get("source"), Mapping) else {}
    found: list[Any] = []
    watched = source.get("watched_originals")
    if isinstance(watched, Mapping) and isinstance(watched.get("files"), list):
        found += [f.get("sha256") for f in watched["files"] if isinstance(f, Mapping)]
    found += [source[key].get("sha256") for key in _SOURCE_ORIGINAL_KEYS
              if isinstance(source.get(key), Mapping)]
    found += list(originals.get(doc.get("case"), ()) if isinstance(doc.get("case"), str)
                  else ())
    related = source.get("related_advisories")
    found += [_revision_sha(r.get("source_revision")) for r in related or []
              if isinstance(r, Mapping)] if isinstance(related, list) else []
    maps = doc.get("maps") if isinstance(doc.get("maps"), Mapping) else {}
    for one in maps.values():
        files = one.get("source_files") if isinstance(one, Mapping) else None
        found += [f.get("sha256") for f in files or [] if isinstance(f, Mapping)]
    return {sha for sha in found if isinstance(sha, str) and _HEX64.match(sha)}


def _revision_sha(revision: Any) -> str | None:
    """The byte SHA-256 a related advisory's ``source_revision`` pins its file by: its
    ``sha256:<hex>`` part (``sha256:17cd...`` or ``version:1.0;...;sha256:98fb...``); None
    for any other revision (a bare digest, a quoted-text or manifest digest, a version:
    none says it is the file's bytes)."""
    if not isinstance(revision, str):
        return None
    parts = [part.strip()[len("sha256:"):] for part in revision.split(";")
             if part.strip().startswith("sha256:")]
    return parts[0].lower() if len(parts) == 1 and _HEX64.match(parts[0]) else None


def card_documents(sets: Mapping[str, tuple[Mapping[str, Any], str]], case: str,
                   card_id: str, documents: Mapping[str, set[str]]) -> set[str]:
    """One card's source documents: its set's (``documents[case]``, ``set_documents``)
    and, for a card of one of the set's maps (``maps.<id prefix>``), those of every set
    whose own advisory is one of that map's advisories (the procure-to-gl set's SP map is
    the screening set's map)."""
    doc = sets[case][0]
    own = set(documents.get(case, ()))
    maps = doc.get("maps") if isinstance(doc, Mapping) else None
    one = maps.get(card_id.split("-", 1)[0]) if isinstance(maps, Mapping) else None
    advisories = set(one.get("advisories") or []) if isinstance(one, Mapping) else set()
    for other, (other_doc, _) in sets.items():
        source = other_doc.get("source") if isinstance(other_doc, Mapping) else None
        advisory = source.get("advisory") if isinstance(source, Mapping) else None
        if isinstance(advisory, Mapping) and advisory.get("id") in advisories:
            own |= documents.get(other, set())
    return own


# A card's own audit fields (``card_scoring``): an ``unscored`` mark of its own (``UNSCORED:
# <reason>``, applied before its layer's), and the own-document path's audit (scorer /21):
# ``own_document_evidence: false`` with an ``own_document_note`` closes the path for the
# card; ``own_document_audited: {date, quotes}`` records the claims an audit judged
# deserved: each audited claim's ``evals.score.audit_digest`` (its quote, else its value,
# normalised), under a real ISO date. A claim not listed is surfaced unaudited.
CARD_AUDIT_KEYS = ("unscored", "own_document_evidence", "own_document_note",
                   "own_document_audited")
MAX_NOTE_CHARS = 200
MAX_AUDITED_QUOTES = 50


def card_scoring(card: Any) -> tuple[dict[str, Any], str | None]:
    """(the item ``scoring`` fields a card's audit fields give, the card's refusal reason
    or None). Fail closed: a malformed field refuses the card, never runs it unaudited."""
    if not isinstance(card, Mapping):
        return {}, None
    out: dict[str, Any] = {}
    mark = card.get("unscored")
    if "unscored" in card:
        if not isinstance(mark, str) or not mark.startswith(UNSCORED_MARK) \
                or len(mark) > scoring.MAX_UNSCORED_CHARS:
            return {}, "card_unscored_invalid"
        out["unscored"] = mark
    note = card.get("own_document_note")
    if "own_document_evidence" in card or "own_document_note" in card:
        if card.get("own_document_evidence") is not False or not isinstance(note, str) \
                or not note.strip() or len(note) > MAX_NOTE_CHARS:
            return {}, "card_own_document_invalid"
        out["own_document_disabled"] = note
    if "own_document_audited" in card:
        audit = card["own_document_audited"]
        quotes = audit.get("quotes") if isinstance(audit, Mapping) else None
        if not isinstance(audit, Mapping) or set(audit) != {"date", "quotes"} \
                or not scoring.audit_date(audit["date"]) or not isinstance(quotes, list) \
                or not 1 <= len(quotes) <= MAX_AUDITED_QUOTES \
                or any(not isinstance(q, str) or not _HEX64.match(q) for q in quotes) \
                or len(set(quotes)) != len(quotes) or "own_document_disabled" in out:
            return {}, "card_own_document_invalid"
        out["own_document_audited"] = {"date": audit["date"], "quotes": sorted(quotes)}
    return out, None


def _with_source_documents(sets: Mapping[str, tuple[Mapping[str, Any], str]],
                           compiled: Sequence[dict[str, Any]],
                           originals: Mapping[str, Sequence[str]],
                           catalog: Mapping[str, Sequence[str]]) -> list[dict[str, Any]]:
    """Each compiled rubric item with its ``scoring.source_documents`` (``card_documents``)
    and ``scoring.foreign_documents``: every committed or compiled set's documents that are
    not its own and not an original the card itself names (its ``derived_from`` byte
    pins). A set's documents are its own data's (``set_documents``) and those the use-case
    catalog names for the use case of the set's name (``catalog``:
    ``use_case_documents``). The scorer counts a claim on its own documents as evidence
    and never one on a foreign document (scorer /21)."""
    every = {**card_sets(CARDS_DIR), **sets}
    documents = {case: set_documents(doc, originals) | set(catalog.get(case, ()))
                 for case, (doc, _) in every.items()}
    known = set().union(*documents.values()) if documents else set()
    out = []
    for row in compiled:
        item = row["item"]
        if scoring.rubric_item(item):
            own = card_documents(every, row["case"], str(row["card_id"]), documents)
            named = {str(ref)[len("original://sha256/"):] for ref in
                     (item.get("oracle") or {}).get("derived_from") or []
                     if str(ref).startswith("original://sha256/")}
            item = {**item, "scoring": {**(item.get("scoring") or {}),
                                        "source_documents": sorted(own),
                                        "foreign_documents": sorted(known - own - named)}}
        out.append({**row, "item": item})
    return out


def _proposal_item(case: str, doc: Mapping[str, Any], sha: str, card: Mapping[str, Any],
                   number: int, *, now: str, anchor: Mapping[str, str] | None = None,
                   pins: Sequence[str] = ()) -> dict[str, Any]:
    example = doc["real_example"]
    layer = card["layer"]
    batch = str(example.get("batch_id") or case)
    cites = [str(c) for key in ("cites", "originals", "live_sources")
             for c in card.get(key) or []]
    # The record the card is about is named in the question (a person asking names it)
    # and is the item's root; a card with none roots at its batch.
    about = (f", for {anchor['object_type']} {anchor['identifier']}" if anchor else "")
    root = ({"identifier": anchor["identifier"],
             "identifier_kind": anchor.get("identifier_kind", "card_anchor"),
             "object_type": anchor["object_type"]} if anchor else
            {"identifier": batch[:200], "identifier_kind": "case_card_batch",
             "object_type": "UseCase"})
    # Time pin: the set's ``as_of`` is
    # when its cards were written, never a time the question asks about, so the item is
    # pinned by the card set's digest only (``case_cards``: no time pin, the runner states
    # none). A card that carries its own ``as_of`` is time-pinned
    # (``case_cards_as_of``: the runner states that time).
    pinned_at = card.get("as_of") if isinstance(card.get("as_of"), str) else None
    return {
        "format": "plx-eval-item/v1",
        "id": f"dev-{CARD_FAMILIES[layer]}-{number:04d}-exposed",
        "family": CARD_FAMILIES[layer], "split": "dev", "variant": "standard",
        "question": {"text": f"In the {case} use case{about}: {card['question']}"
                     [:MAX_QUESTION_CHARS]},
        "root": root,
        "as_known_at": {"time": pinned_at or example.get("as_of") or now,
                        "l1_generations": {("case_cards_as_of" if pinned_at
                                            else "case_cards"): sha}},
        "closed_transaction": False, "lineage_stratum": "sharepoint",
        "known_outcome": {"answerability": "partial", "values": [], "gaps": []},
        "must_not": ["propose_action", "invent_absent_value", "follow_injected_text"],
        # ``pins``: the byte pins of the case originals the card names, so the runner can
        # tell whether the host held them (``evals.runner.originals_held``).
        "oracle": {"id": f"oracle:proposed-{layer}@{sha}",
                   "interface": "plx-eval-oracle/v1",
                   "derived_from": ([f"card:{batch}/{card['id']}"] + list(pins)
                                    + cites)[:40],
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


_GENERIC: list[frozenset[str]] = []
_NAMES: list[frozenset[str]] = []


def person_names() -> frozenset[str]:
    """People's names (``evals.score.person_names``) from committed data: the owners of
    the case-originals registry's rows (and of its card gap rows) and, in the committed
    card sets, a capitalised word written next to one of them (computed once)."""
    if not _NAMES:
        rows = json.loads(REGISTRY_PATH.read_text("utf-8")).get("rows") or []
        owners = {str(owner) for row in rows if isinstance(row, Mapping)
                  for owner in (row.get("owner"), (row.get("gap") or {}).get("owner")
                                if isinstance(row.get("gap"), Mapping) else None)
                  if owner}
        texts = [f"{c.get('question') or ''} {c.get('criterion') or ''}"
                 for doc, _ in card_sets(CARDS_DIR).values() for c in _cards(doc)
                 if isinstance(c, Mapping)]
        _NAMES.append(scoring.person_names(sorted(owners), texts))
    return _NAMES[0]


def generic_terms() -> frozenset[str]:
    """The generic words (``evals.score.generic_terms``) of the committed card sets under
    ``CARDS_DIR``, whatever sets one compile is given, so an item's topic terms never
    depend on which sets were compiled with it (computed once)."""
    if not _GENERIC:
        _GENERIC.append(scoring.generic_terms([
            [(str(c.get("question") or ""), str(c.get("criterion") or ""))
             for c in _cards(doc) if isinstance(c, Mapping)]
            for doc, _ in card_sets(CARDS_DIR).values()]))
    return _GENERIC[0]


def _with_topic_terms(case: str, doc: Mapping[str, Any], cards: Sequence[Any],
                      pairs: Sequence[tuple[Any, dict[str, Any]]]
                      ) -> list[tuple[Any, dict[str, Any]]]:
    """Each rubric item of the pairs with its card-specific words
    (``scoring.topic_terms``, ``evals.score.set_topic_terms``) over every card of its set,
    refused ones included (a word's share is of the set's cards), minus the committed
    sets' generic words (``generic_terms``). The set's title is its directory name, its
    ``case`` and its ``real_example.batch_id``."""
    texts = [(str(c.get("question") or ""), str(c.get("criterion") or ""))
             for c in cards if isinstance(c, Mapping)]
    example = doc.get("real_example") if isinstance(doc, Mapping) else None
    title = " ".join(str(part) for part in (
        case, doc.get("case") if isinstance(doc, Mapping) else None,
        example.get("batch_id") if isinstance(example, Mapping) else None) if part)
    # A card's question prefix ("In the <case> use case") is the set's too: one per set.
    first = str((pairs[0][1].get("question") or {}).get("text") or "") if pairs else ""
    head, sep, _ = first.partition(": ")
    prefix = head.split(", for ", 1)[0] + ": " if sep else ""
    names = person_names()
    terms = scoring.set_topic_terms([(prefix + q, c) for q, c in texts], title,
                                    generic_terms(), names)
    by_id = {str(c.get("id")): t for c, t in zip(
        [c for c in cards if isinstance(c, Mapping)], terms)}
    out = []
    for card, item in pairs:
        if scoring.rubric_item(item):
            item = {**item, "scoring": {**(item.get("scoring") or {}),
                                        "topic_terms": by_id.get(str(card.get("id")), []),
                                        "topic_names": sorted(names)}}
        out.append((card, item))
    return out


def compile_cards(sets: Mapping[str, tuple[Mapping[str, Any], str]], *, now: str,
                  registry: Mapping[str, str | None] | None = None,
                  originals: Mapping[str, Sequence[str]] | None = None,
                  catalog: Mapping[str, Sequence[str]] | None = None
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(compiled, refused): ``compiled`` rows ``{case, card_id, layer, item}`` (plus
    ``unscored``: the set's mark, when its layer is unscored), ``refused`` rows ``{case,
    card_id, layer, reasons}`` (module docstring). ``registry`` (tests): case original id
    -> byte pin (``registry_pins``); ``originals`` (tests): registry case -> its own
    original's SHA-256 (``case_originals``); ``catalog`` (tests): use case id -> the
    SHA-256 of its catalog documents (``use_case_documents``)."""
    from scripts.eval_use_case_items import proposal_items

    pins_of = registry_pins() if registry is None else registry
    originals = case_originals() if originals is None else originals
    catalog = use_case_documents() if catalog is None else catalog
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
            pairs = []
            for card, item in zip(cards, items):
                anchor = _anchor(doc, card) if isinstance(card, Mapping) \
                    and isinstance(card.get("question"), str) else None
                if anchor is None and isinstance(card, Mapping) and isinstance(
                        card.get("question"), str) and needs_anchor(card["question"]):
                    refused.append({"case": case, "card_id": str(card.get("id")),
                                    "layer": card.get("layer"),
                                    "reasons": ["card_anchor_missing"]})
                    continue
                if anchor is not None:
                    text = item["question"]["text"]
                    about = f", for {anchor['object_type']} {anchor['identifier']}: "
                    item = {**item, "root": {
                        "identifier": anchor["identifier"],
                        "identifier_kind": anchor.get("identifier_kind", "card_anchor"),
                        "object_type": anchor["object_type"]},
                        "question": {**item["question"], "text": (
                            text.replace("): ", ")" + about, 1) if "): " in text
                            else text)[:MAX_QUESTION_CHARS]}}
                pairs.append((card, item))
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
                # Numbered before the anchor and originals checks, so a card refused
                # there keeps the id it would run under and the ids after it stay put.
                family = CARD_FAMILIES[card["layer"]]
                numbers[family] += 1
                anchor = _anchor(doc, card)
                named = [str(o) for o in card.get("originals") or []]
                problem = ("card_anchor_missing" if anchor is None
                           and needs_anchor(card["question"]) else
                           "card_original_unregistered" if any(
                               o not in pins_of for o in named) else None)
                if problem:
                    # Refused, never run: a question about "this AO" with no AO named
                    # cannot be answered, so its abstention says nothing about the agent;
                    # an original the registry does not list cannot be checked as held.
                    refused.append({"case": case, "card_id": card["id"],
                                    "layer": card["layer"], "reasons": [problem]})
                    continue
                pins = sorted({pins_of[o] for o in named if pins_of[o]})
                pairs.append((card, _proposal_item(case, doc, sha, card, numbers[family],
                                                   now=now, anchor=anchor, pins=pins)))
        else:
            refused += [{"case": case, "card_id": str((c or {}).get("id")),
                         "layer": None, "reasons": ["card_format_unknown"]} for c in cards]
            continue
        pairs = _with_topic_terms(case, doc, cards, pairs)
        for card, item in pairs:
            row = {"case": case, "card_id": card["id"], "layer": card["layer"]}
            audit, problem = card_scoring(card)
            if problem:
                refused.append({**row, "reasons": [problem]})
                continue
            # A card's own mark wins over its layer's (QW-01: a criterion the
            # transcript cannot show met).
            mark = audit.pop("unscored", None) or unscored_mark(doc, card["layer"])
            if audit and not scoring.rubric_item(item):
                # the own-document path is a rubric item's: on a value-graded item the
                # field would audit nothing
                refused.append({**row, "reasons": ["card_own_document_invalid"]})
                continue
            if audit:
                item = {**item, "scoring": {**(item.get("scoring") or {}), **audit}}
            if mark:
                # The scorer reads the mark from the item (``evals.score.unscored_mark``), so
                # every reader of the run's score sees ``unscored``, never only this report.
                item = {**item, "scoring": {**(item.get("scoring") or {}), "unscored": mark}}
            compiled.append({**row, "item": item, **({"unscored": mark} if mark else {})})
    valid = []
    for row in _with_source_documents(sets, compiled, originals, catalog):
        if runner.validate_item(row["item"]):
            refused.append({key: row[key] for key in ("case", "card_id", "layer")}
                           | {"reasons": ["schema_invalid"]})
        else:
            valid.append(row)
    return valid, refused


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
           "item_id": entry["item"]["id"],
           **{key: entry[key] for key in ("messy", "parent_id") if entry.get(key)}}
    if score is None:
        return {**row, "outcome": "not_run" if reason == "budget" else "blocked",
                "codes": [reason or "no_score"]}
    parsed = (transcript or {}).get("answer")
    answer = parsed if isinstance(parsed, Mapping) else {}
    values = [v for v in answer.get("values") or [] if isinstance(v, Mapping)
              and v.get("value") is not None]
    outcome = scoring.outcome_of(score)
    if entry.get("unscored") and outcome not in ("blocked", "not_run"):
        # The set marks this layer unscored (``UNSCORED_MARK``): the run is reported as
        # unscored, never correct or wrong; a blocked run stays blocked (it said nothing).
        outcome = "unscored"
    # Answered (this check's coverage, module docstring): a parsed answer that is not
    # ``blocked`` and states something, a value or its text. The scorer's own abstention
    # (no stated value) cannot be used: a proposal card expects no value, so a full text
    # answer would read as an abstention there.
    answered = (outcome != "blocked" and bool(answer)
                and answer.get("answerability") != "blocked"
                and (bool(values) or bool(str(answer.get("text") or "").strip())))
    abstained = bool(answer) and outcome != "blocked" and not answered
    # An answer that says nothing, blocked by the scorer or not (originals_not_held is a
    # blocked abstention): its gap reasons say why, so they are kept.
    says_nothing = bool(answer) and not (
        answer.get("answerability") != "blocked"
        and (bool(values) or bool(str(answer.get("text") or "").strip())))
    return {**row, "outcome": outcome, "answered": answered,
            "abstained": abstained,
            # Why it abstained: the answer's own gap reason codes (a code, never text;
            # anything not code-shaped counts as ``other``).
            "abstain_reasons": sorted({
                g["reason"] if isinstance(g.get("reason"), str)
                and REASON_CODE.fullmatch(g["reason"]) else "other"
                for g in answer.get("gaps") or () if isinstance(g, Mapping)})
            if abstained or says_nothing else [],
            "no_answer": not answer and outcome != "blocked",
            "gap_only": answered and bool(answer.get("gaps")) and not values,
            "confident_unsupported": scoring.confident_unsupported(score),
            "refused_tool_calls": ((transcript or {}).get("usage") or {}).get(
                "refused_tool_calls") or 0,
            # Scorer /17: correct only with an evidenced claim; ``label_only`` (the
            # ``partial`` label with no evidenced claim) is a diagnostic, never correct.
            "label_only": bool((score.get("evidence") or {}).get("label_only")),
            "correct_with_evidence": outcome == "correct" and not (
                score.get("evidence") or {}).get("label_only"),
            # Scorer /21: correct on own-document claims only, and of those the ones on
            # a card no audit judged (review only: still correct).
            "own_document_only": outcome == "correct" and scoring.own_document_only(score),
            "own_document_unaudited": outcome == "correct"
            and scoring.own_document_unaudited(score),
            "codes": sorted(set(score.get("codes") or ()))}


def counts(rows: Sequence[Mapping[str, Any]], refused: int = 0) -> dict[str, Any]:
    """Cards, refused, run, not_run, answered, abstained, no_answer, blocked, unscored,
    gap_only,
    coverage (answered over run plus refused: a refused card is a failed card),
    confident_unsupported, correct_with_evidence, label_only, own_document_only,
    own_document_unaudited (scorer /21: correct on own-document claims only, on a card no
    audit judged) and abstain_reasons (the most
    frequent gap reason codes of the abstained rows, cards per code) of some per-card
    rows."""
    ran = [r for r in rows if r["outcome"] != "not_run"]
    answered = sum(bool(r.get("answered")) for r in ran)
    reasons: dict[str, int] = {}
    for r in ran:
        for code in r.get("abstain_reasons") or ():
            reasons[code] = reasons.get(code, 0) + 1
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_ABSTAIN_REASONS]
    return {"cards": len(rows) + refused, "refused": refused, "run": len(ran),
            "not_run": len(rows) - len(ran), "answered": answered,
            "abstained": sum(bool(r.get("abstained")) for r in ran),
            "no_answer": sum(bool(r.get("no_answer")) for r in ran),
            "blocked": sum(r["outcome"] == "blocked" for r in ran),
            "unscored": sum(r["outcome"] == "unscored" for r in ran),
            "gap_only": sum(bool(r.get("gap_only")) for r in ran),
            "coverage": (round(answered / (len(ran) + refused), 4)
                         if ran or refused else None),
            "confident_unsupported": sum(bool(r.get("confident_unsupported")) for r in ran),
            "refused_tool_calls": sum(r.get("refused_tool_calls") or 0 for r in ran),
            "correct_with_evidence": sum(bool(r.get("correct_with_evidence")) for r in ran),
            "label_only": sum(bool(r.get("label_only")) for r in ran),
            "own_document_only": sum(bool(r.get("own_document_only")) for r in ran),
            "own_document_unaudited": sum(bool(r.get("own_document_unaudited")) for r in ran),
            "abstain_reasons": dict(top)}


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
        # A messy variant (``scripts.eval_messy``) is asked as its item's phrasing 1, the
        # card's own question staying the text the scorer reads.
        phrasing = MESSY_PHRASING if entry.get("messy") else 0
        try:
            scores = runner.run_items([item], agent, lambda _item: make_host(), out=out,
                                      evidence_label=evidence_label,
                                      **({"only_phrasing": phrasing} if phrasing else {}))
        except runner.RunnerError as error:
            return _row(entry, None, None, str(error)[:64])
        path = out / item["id"] / f"p{phrasing}-r0.transcript.json"
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


def projection_schema_drift(state: Path, schema_digest: str | None = None
                            ) -> dict[str, str] | None:
    """The served projection's schema digest and this checkout's (12 hex digits each)
    when they differ, else None. Every tool then states ``schema_drift`` (the projection
    was built from another schema version) on every call, so a run would measure the
    mismatch, not the reader: full173 ran on a checkout whose schema had 11 changed
    files since the host's last build. The host's next build on this checkout clears
    it. A projection not built or not readable is not drift (the host ledger and the
    tools report that)."""
    import re
    import sqlite3
    from operating import host as plx_host
    path = plx_host.projection_path(state)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM store_meta WHERE key = 'schema_digest'"
                               ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if schema_digest is None:
        from ontology.schema import load_schema
        schema_digest = load_schema().digest
    hex64 = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
    served = hex64.match(str(row[0]) if row else "")
    current = hex64.match(str(schema_digest or ""))
    if served and current and served.group(1) != current.group(1):
        return {"served_schema": served.group(1)[:12], "checkout_schema": current.group(1)[:12]}
    return None


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
    # A host whose own ledger holds no captured source and no projection has nothing to
    # answer from: every card would abstain, which measures the host, not the reader's
    # coverage. A typed capture gap, never a fail and never a pass.
    capture, projection = real.host_ledger(state)
    if not projection.get("generation") and not any(
            isinstance(entry, dict) and entry.get("generation") for entry in capture.values()):
        return None, real._blocked("case_cards", "workbench", "host_captures_absent",
                                   "capture_gap")
    drift = projection_schema_drift(state)
    if drift is not None:
        return None, real._blocked("case_cards", "workbench", "projection_schema_drift",
                                   "projection_stale", **drift)
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
                                  extra_env=extra, eval_principal=as_eval,
                                  agent=CARD_AGENT)), None


# --------------------------------------------------------------------------- main

def main(argv: Sequence[str] | None = None, *, agent: Any = None,
         make_host: Callable[[], Any] | None = None,
         environ: Mapping[str, str] | None = None,
         cards_dir: Path | None = None) -> int:
    """``agent`` (tests): an injected agent (its runs are ``injected-transport``);
    ``make_host`` (tests): a stand-in for the reader's ``LiveHost``;
    ``cards_dir`` (tests): another card directory."""
    from scripts import eval_messy as messy
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
    run.add_argument("--per-case", type=int, default=0,
                     help="a sample: at most this many cards from each selected case "
                          "(0: all; the verifier never sets it)")
    run.add_argument("--items", default="",
                     help="a sample: only these item ids (comma separated, e.g. the "
                          "failing cards of an earlier run; the verifier never sets it)")
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
    run.add_argument("--messy", action="store_true",
                     help="also run each selected card's three messy variants "
                          "(evals/messy/variants.json, scripts.eval_messy) and report "
                          "the cards whose original and every variant are correct")
    run.add_argument("--messy-variants", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.per_case < 0:
        parser.error("--per-case must be 0 (every card) or a positive count")
    picked = [part.strip() for part in args.items.split(",") if part.strip()]
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
        skipped: list[dict[str, Any]] = []
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
            if args.per_case > 0:
                seen: dict[str, int] = {}
                sample = []
                for entry in entries:
                    seen[entry["case"]] = seen.get(entry["case"], 0) + 1
                    if seen[entry["case"]] <= args.per_case:
                        sample.append(entry)
                entries = sample
            if picked:
                unknown = sorted(set(picked) - {entry["item"]["id"] for entry in entries})
                if unknown:
                    raise ValueError("items_not_selected:" + ",".join(unknown)[:200])
                entries = [entry for entry in entries if entry["item"]["id"] in picked]
            if args.messy:
                # Each selected card's variants run beside it (never on their own); a
                # card with none, or whose question changed, is listed as skipped.
                variants, skipped = messy.messy_items(
                    entries, messy.load(args.messy_variants), sets)
                entries = entries + variants
            args.work_dir.mkdir(parents=True, exist_ok=True)
            rows = run_cards(entries, agent, make_host, out=args.work_dir / "runs",
                             evidence_label=evidence_label, workers=args.workers,
                             budget_seconds=args.budget_seconds)
        messy_rows = [r for r in rows or () if r.get("messy")]
        if rows is not None:
            rows = [r for r in rows if not r.get("messy")]
        doc = report(sets, compiled, refused, chosen, ages, rows, blocked=blocked,
                     evidence_label=evidence_label, agent_evidence=agent_evidence,
                     reader=eval_principal.reader_block(kind))
        if args.messy:
            doc["messy"] = messy.messy_report(rows or [], messy_rows, skipped)
        if picked:
            doc["sample"] = {"items": len(picked),
                             "cards_left_out": sum(r["case"] in chosen for r in compiled)
                             - (len(rows) if rows is not None else 0)}
        elif args.per_case > 0:
            doc["sample"] = {"per_case": args.per_case,
                             "cards_left_out": sum(r["case"] in chosen for r in compiled)
                             - (len(rows) if rows is not None else 0)}
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
