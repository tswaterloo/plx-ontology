"""HS code proposals for items that have none: a deterministic keyword table, lead tier.

A proposal is a lead built from the item description and ``hs_proposals.json``. It is never a
classification and never feeds a duty citation (``hs_tariff.cite_duty`` reads recorded codes
only). A person turns one into ``reviewed`` through the existing review store
(``ontology.reviews.ReviewStore``, decision ``confirm_value`` for the proposed code or
``correct_value`` for another, property ``hs_code``): :func:`review_kwargs` builds that call,
:func:`hs_view` reads the answer. Evidence label: isolated-fixture in tests.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from . import hs_tariff

PROPOSALS_PATH = Path(__file__).with_name("hs_proposals.json")
LEAD_TIER = "lead"
STATUS = "proposed, needs human confirmation"
PROPERTY = "hs_code"


def load_table(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or PROPOSALS_PATH).read_text(encoding="utf-8"))


TABLE = load_table()


def _matches(text: str, table: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every keyword hit, with its span; a hit inside a longer hit of another family is
    dropped ('liquid soap' is a liquid wash, not also a soap bar)."""
    low = " ".join(text.lower().split())
    hits = []
    for fam in table["families"]:
        for word in fam["keywords"]:
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", low):
                hits.append({"family": fam["family"], "kind": fam["kind"],
                             "strength": fam["strength"], "hs_code": fam["hs_code"],
                             "keyword": word, "span": m.span()})
    keep = []
    for h in hits:
        inside = any(o["family"] != h["family"] and o["span"][0] <= h["span"][0]
                     and h["span"][1] <= o["span"][1]
                     and o["span"][1] - o["span"][0] > h["span"][1] - h["span"][0]
                     for o in hits)
        if not inside:
            keep.append(h)
    return keep


def propose_hs(item: str, description: Any, table: Mapping[str, Any] = TABLE
               ) -> dict[str, Any]:
    """One item's proposal, or why there is none: ``NO_DESCRIPTION``, ``NO_KEYWORD_MATCH`` or
    ``AMBIGUOUS_FAMILIES`` (several families with different codes, all named)."""
    base = {"item": item, "tier": LEAD_TIER, "status": STATUS}
    text = " ".join(str(description or "").split())
    if not text:
        return {**base, "hs_code": None, "gap": "NO_DESCRIPTION"}
    hits = _matches(text, table)
    for kind in ("packaging", "product"):  # packaging words win: a shampoo bottle is a bottle
        pool = [h for h in hits if h["kind"] == kind]
        strong = [h for h in pool if h["strength"] == "strong"]
        pool = strong or pool
        if not pool:
            continue
        families = {(h["family"], h["hs_code"]) for h in pool}
        if len({code for _, code in families}) > 1:
            return {**base, "hs_code": None, "gap": "AMBIGUOUS_FAMILIES",
                    "families": sorted(f for f, _ in families)}
        first = min(pool, key=lambda h: h["span"])
        return {**base, "hs_code": hs_tariff.norm_hs(first["hs_code"]),
                "hs_display": first["hs_code"], "family": first["family"], "kind": kind,
                "matched": first["keyword"], "destination": table["destination"],
                "source": "operating/hs_proposals.json keyword table"}
    return {**base, "hs_code": None, "gap": "NO_KEYWORD_MATCH"}


def propose_for_items(items: Sequence[Mapping[str, Any]], table: Mapping[str, Any] = TABLE
                      ) -> dict[str, Any]:
    """``items``: ``{item, description, hs_code}`` (``hs_code`` the recorded one, usually
    blank). An item with a recorded code gets no proposal. Returns ``{proposals, coverage}``:
    every proposed item once, and counts by family and by gap."""
    proposals, by_family, gaps = [], Counter(), Counter()
    recorded = 0
    for row in items:
        if hs_tariff.norm_hs(row.get("hs_code")):
            recorded += 1
            continue
        p = propose_hs(str(row.get("item")), row.get("description"), table)
        if p["hs_code"]:
            proposals.append(p)
            by_family[p["family"]] += 1
        else:
            gaps[p["gap"]] += 1
    return {"proposals": proposals, "coverage": {
        "items": len(items), "with_recorded_code": recorded,
        "without_code": len(items) - recorded, "proposed": len(proposals),
        "by_family": dict(sorted(by_family.items())), "no_proposal": dict(sorted(gaps.items()))}}


def review_kwargs(proposal: Mapping[str, Any], *, tenant: str, author: Mapping[str, Any],
                  subject: str, evidence: Sequence[Mapping[str, Any]],
                  corrected_code: str | None = None) -> dict[str, Any]:
    """The ``ReviewStore.record`` arguments by which a person confirms a proposal (or corrects
    it with another code). ``subject`` is the item's ontology object id and ``evidence`` the
    cited L1 field the person read (the review store checks neither: its caller does)."""
    if not proposal.get("hs_code") and not corrected_code:
        raise ValueError("no_proposal_to_confirm")
    code = hs_tariff.norm_hs(corrected_code) if corrected_code else proposal["hs_code"]
    if not code:
        raise ValueError("corrected_code_invalid")
    return {"tenant": tenant, "author": author,
            "decision": "correct_value" if corrected_code else "confirm_value",
            "subject": subject, "evidence": list(evidence),
            "value": {"property": PROPERTY, "value": code}}


def hs_view(recorded: Any, proposal: Mapping[str, Any] | None,
            review: Mapping[str, Any] | None) -> dict[str, Any]:
    """The HS code an item shows and its tier: a recorded code first (``recorded``), then a
    person's decision in force (``reviewed``), then a proposal (``lead``, stated as a proposal).
    ``review`` is the decision in force from ``ontology.reviews.effective`` or None."""
    code = hs_tariff.norm_hs(recorded)
    if code:
        return {"hs_code": code, "tier": "recorded"}
    if review and (review.get("value") or {}).get("property") == PROPERTY:
        got = hs_tariff.norm_hs(review["value"].get("value"))
        if got:
            return {"hs_code": got, "tier": "reviewed", "decision": review["decision"],
                    "review_id": review.get("review_id")}
    if proposal and proposal.get("hs_code"):
        return {"hs_code": proposal["hs_code"], "tier": LEAD_TIER, "status": STATUS,
                "family": proposal.get("family")}
    return {"hs_code": None, "tier": None}
