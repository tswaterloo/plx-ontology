"""One checker for the case-card proposal formats.

``plx-eval-case-card-proposals/v1`` (``evals/dev/<case>/proposed_items.json``) and
``plx-case-ontology-gaps/v1`` (``ontology_gaps.json``). Every case test calls
``problems`` directly; per-case fixture data (folder names, counts) stays in those tests.

Handled here, not patched around by callers:
  * ``source.related_advisories``: pinned like the main advisory and citable by cards;
  * per-map citations: when a file has ``maps``, a card ``<PREFIX>-...`` cites one of
    ``maps[PREFIX].advisories``;
  * the original-row cases a card may cite are derived from the ``proposed_items.json``
    files present under ``evals/dev`` (the file with the same ``case``), never a list.
Only ``evals/dev`` is read; nothing under held-out is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "evals" / "dev"
ORIGINALS = ROOT / "examples" / "case_originals.json"
CARDS_FORMAT = "plx-eval-case-card-proposals/v1"
GAPS_FORMAT = "plx-case-ontology-gaps/v1"
CARD_KEYS = {"id", "layer", "question", "criterion", "originals", "live_sources", "cites"}
# A card may carry one of these (card anchors, scripts/eval_case_cards.py ``_anchor``):
# ``anchor`` {identifier, object_type[, identifier_kind], source} names the record the
# card is about and the committed source that names it; ``anchor_gap`` says why no
# committed source names one. Never both.
ANCHOR_KEYS = {"anchor", "anchor_gap"}
# A card may carry its own audit fields (scripts/eval_case_cards.py ``card_scoring``, which
# refuses a malformed one): its own ``unscored`` mark, the own-document path's audit and
# its derived ``expected`` answer (scorer /26).
AUDIT_KEYS = {"unscored", "own_document_evidence", "own_document_note",
              "own_document_audited", "expected"}
ANCHOR_FIELDS = {"identifier", "object_type", "identifier_kind", "source"}
GAP_KINDS = {"object", "link", "reader"}
UNKNOWN = "source unknown"
# repo:path@commit, optionally :lines or a row id after it.
REF = re.compile(r"^[A-Za-z0-9_.-]+:[^@\s]+@[0-9a-f]{7,40}\b")


def load(case_dir: Path, name: str) -> dict:
    return json.loads((Path(case_dir) / name).read_text(encoding="utf-8"))


def proposal_files(dev_dir: Path = DEV) -> list[Path]:
    """The proposal files present under ``dev_dir`` (one level down)."""
    out = []
    for path in sorted(Path(dev_dir).glob("*/proposed_items.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("format") == CARDS_FORMAT:
            out.append(path)
    return out


def allowed_cases(case: str, originals: dict, dev_dir: Path = DEV) -> set[str]:
    """Original-row cases the cards of ``case`` may cite: those the committed
    ``proposed_items.json`` of that case under ``dev_dir`` cites. Empty when the case
    cites no row or is not present."""
    rows = {r["id"]: r for r in originals["rows"]}
    allowed: set[str] = set()
    for path in proposal_files(dev_dir):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("case") != case:
            continue
        for card in data.get("real_example", {}).get("cards", []):
            for co in card.get("originals", []):
                if co in rows and rows[co].get("case"):
                    allowed.add(rows[co]["case"])
    return allowed


def _pinned(a: dict) -> bool:
    return (str(a.get("id", "")).startswith("advisory:")
            and str(a.get("path", "")).startswith("context/") and "@" in str(a.get("ref", "")))


def anchor_problem(card: dict) -> str | None:
    """Why a card's anchor fields break the anchors rule (``ANCHOR_KEYS``), or None: an
    ``anchor`` has a non-empty identifier, object type and source, string fields only and
    no other key; an ``anchor_gap`` is non-empty text; a card never carries both."""
    if ANCHOR_KEYS <= set(card):
        return "anchor and anchor_gap"
    if "anchor_gap" in card and not (isinstance(card["anchor_gap"], str)
                                     and card["anchor_gap"].strip()):
        return "anchor_gap empty"
    if "anchor" in card:
        anchor = card["anchor"]
        if not isinstance(anchor, dict) or set(anchor) - ANCHOR_FIELDS \
                or not all(isinstance(v, str) for v in anchor.values()) \
                or not all(str(anchor.get(k, "")).strip()
                           for k in ("identifier", "object_type", "source")):
            return "anchor malformed"
    return None


def problems(cards: dict, gaps: dict, originals: dict, dev_dir: Path = DEV) -> list[str]:
    out: list[str] = []
    rows = {r["id"]: r for r in originals["rows"]}
    if cards.get("format") != CARDS_FORMAT:
        out.append("cards format")
    advisory = cards["source"]["advisory"]
    related = cards["source"].get("related_advisories", [])
    if not all(_pinned(a) for a in [advisory, *related]):
        out.append("advisory pin")
    allowed = allowed_cases(cards.get("case"), originals, dev_dir)
    present = any(json.loads(p.read_text(encoding="utf-8")).get("case") == cards.get("case")
                  for p in proposal_files(dev_dir))
    if not present:
        out.append(f"case {cards.get('case')!r} has no proposed_items.json under dev")
    live = {s["id"]: s for s in cards["live_sources"]}
    for sid, s in live.items():
        if not all(REF.match(r.strip()) for r in s["evidence"].split(";")):
            out.append(f"{sid}: evidence not path@commit")
    example = cards["real_example"]
    if (example.get("exposure"), example.get("split")) != ("dev-exposed", "dev"):
        out.append("exposure")
    ids = [c["id"] for c in example["cards"]]
    if len(set(ids)) != len(ids) or not ids:
        out.append("card ids")
    advisories = {advisory["id"], *(a["id"] for a in related)}
    maps = cards.get("maps", {})
    known = advisories | set(rows) | set(live)
    for card in example["cards"]:
        cid = card["id"]
        if set(card) - ANCHOR_KEYS - AUDIT_KEYS != CARD_KEYS:
            out.append(f"{cid}: keys "
                       f"{sorted((set(card) - ANCHOR_KEYS - AUDIT_KEYS) ^ CARD_KEYS)}")
            continue
        problem = anchor_problem(card)
        if problem:
            out.append(f"{cid}: {problem}")
        if card["layer"] not in cards["layers"] or not card["question"].endswith("?") \
                or not card["criterion"].strip():
            out.append(f"{cid}: layer/question/criterion")
        for co in card["originals"]:
            if co not in rows or rows[co].get("case") not in allowed:
                out.append(f"{cid}: unknown original {co}")
        for sid in card["live_sources"]:
            if sid not in live:
                out.append(f"{cid}: unknown live source {sid}")
        cites = card["cites"]
        if not cites or any(c not in known for c in cites):
            out.append(f"{cid}: uncited")
        if maps:
            own = set(maps.get(cid.split("-")[0], {}).get("advisories", []))
            if not own & set(cites) & advisories:
                out.append(f"{cid}: not cited to its map")
    if gaps.get("format") != GAPS_FORMAT or gaps["advisory"] != advisory["id"]:
        out.append("gaps format")
    for gap in gaps["gaps"]:
        gid = gap["id"]
        if gap["kind"] not in GAP_KINDS or not gap["ontology_today"].strip():
            out.append(f"{gid}: kind/ontology_today")
        if not gap["needed_by"] or any(c not in ids for c in gap["needed_by"]):
            out.append(f"{gid}: needed_by")
        refs = gap.get("sources", [])
        if any(not REF.match(s["ref"]) or not s["relation"] for s in refs):
            out.append(f"{gid}: source not path@commit")
        unknown = gap.get("source") == UNKNOWN
        if unknown and not str(gap.get("dot_question", "")).endswith("?"):
            out.append(f"{gid}: source unknown without a Dot question")
        if not refs and not unknown:
            out.append(f"{gid}: no source")
    return out
