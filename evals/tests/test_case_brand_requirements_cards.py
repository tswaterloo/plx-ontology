"""Brand requirements case: PROPOSED dev-exposed cards and the ontology gaps they need.

``evals/dev/brand-requirements/proposed_items.json`` holds the questions of Laura Abate's
Customer Brand Requirements map (Draft 6): which requirements apply
to a customer, brand and product, the compliance documents on supplier-specific R numbers,
the supplier switch review, the batching gate, approved versions and claim-affecting
changes. The map's 12 stages and 15 rules are stakeholder proposals; the Three Ships
example is an attributed account; the map's failing checks are source assertions. The four
originals of the ZIP are pinned by SHA-256 from the source record.

The contract is ``evals.case_cards``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unittest

from evals import case_cards as contract

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "evals" / "dev" / "brand-requirements"
ADVISORY = "advisory:51cf697a454b"
PINS = {
    "customer-brand-requirements-map.md":
        "9f92c805c0a7e30cd1c1b6ac68fb201ff11f8e01a87b676c4d19e65ad44c0ab1",
    "customer-brand-requirements-map-ontology.json":
        "a09b5863de64fe4b207b49af5a7fed5228a8a1b9b9a8957ffc2c1d8b73e9eac0",
    "customer-brand-requirements-map-ontology.csv":
        "30358efd6bba715c42fd3e7f5b1e40d602dbb24f406e060a6835bf2ec991440a",
    "customer-brand-requirements-map-relations.csv":
        "ab1998076eef1cbbc23f6a77a090a8052483bd8b0ca428978ccfff1ebcb745b5",
}
STATES = {"link_pending", "to_come", "not_provided"}
SHA = re.compile(r"^[0-9a-f]{64}$")
PRICE = re.compile(r"[$€£]\s?\d|\b(?:CAD|USD|EUR)\s?\d|\d\s?(?:CAD|USD|EUR)\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")


def problems(cards: dict, gaps: dict, originals: dict) -> list[str]:
    out: list[str] = []
    a = cards["source"]["advisory"]
    short = a["id"].removeprefix("advisory:")
    if not (a["id"] == ADVISORY and a["path"].startswith(f"context/{short}")
            and SHA.match(str(a.get("record_sha256", "")))):
        out.append(f"advisory pin {a['id']}")
    out += contract.problems(cards, gaps, originals)
    # The four members pinned exactly; no other hash accepted for them.
    members = {m.get("name"): m.get("sha256")
               for m in cards["source"].get("original_archive", {}).get("members", [])}
    if members != PINS:
        out.append("original pins")
    # The draft stays a proposal: status says so, and counts are source assertions.
    status = cards["status"]
    for phrase in ("stakeholder proposals", "not approved decisions", "attributed",
                   "not a verified incident", "source assertions", "not runtime results"):
        if phrase not in status:
            out.append(f"status honesty: {phrase}")
    counts = cards.get("map_counts", {})
    if (counts.get("stages"), counts.get("rules"), counts.get("checks_marked_fail")) != (12, 15, 4):
        out.append("map counts")
    # Every gap that needs a person asks Laura or Regulatory; each missing original has a gap.
    gap_ids = {g["id"] for g in gaps["gaps"]}
    for g in gaps["gaps"]:
        q = g.get("dot_question")
        if q is not None and not re.search(r"\b(Laura|Regulatory)\b", q):
            out.append(f"{g['id']}: question for Laura or Regulatory")
    for m in cards.get("missing_originals", []):
        if m.get("state") not in STATES or m.get("gap") not in gap_ids:
            out.append(f"missing original {m.get('id')}")
    if not any(g.get("dot_question") for g in gaps["gaps"]):
        out.append("question for Laura or Regulatory")
    text = json.dumps([cards, gaps], ensure_ascii=False)
    if PRICE.search(text):
        out.append(f"price {PRICE.search(text).group(0)!r}")
    if re.search(r'"(body|text|content|values|result)"\s*:', text):
        out.append("body")
    if EMAIL.search(text):
        out.append("e-mail address")
    return out


class BrandRequirementsCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards, self.gaps = (json.loads((DIR / n).read_text(encoding="utf-8"))
                                 for n in ("proposed_items.json", "ontology_gaps.json"))
        self.originals = json.loads(contract.ORIGINALS.read_text(encoding="utf-8"))

    def test_committed_cards_and_gaps_satisfy_the_contract(self):
        self.assertEqual(problems(self.cards, self.gaps, self.originals), [])
        cards = self.cards["real_example"]["cards"]
        self.assertEqual({c["layer"] for c in cards}, set(self.cards["layers"]))
        self.assertTrue(all(ADVISORY in c["cites"] for c in cards))
        self.assertEqual({g["kind"] for g in self.gaps["gaps"]}, contract.GAP_KINDS)
        # Every card that needs a not-served or partial source is named by a gap.
        needed = {c for g in self.gaps["gaps"] for c in g["needed_by"]}
        served = {s["id"] for s in self.cards["live_sources"] if s["reader"] != "served"}
        self.assertEqual([c["id"] for c in cards
                          if served & set(c["live_sources"]) and c["id"] not in needed], [])

    def test_pin_matches_the_advisory_record(self):
        """record_sha256 is the SHA-256 of the record at its commit; the four members and
        the ZIP are the record's own. Needs the record commit (git fetch it)."""
        pin = self.cards["source"]["advisory"]
        commit = pin["ref"].rsplit("@", 1)[1]
        run = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{pin['path']}"],
                             capture_output=True, check=False)
        if run.returncode != 0:
            self.skipTest("advisory commit 3d9269e8 is not in this clone")
        self.assertEqual(hashlib.sha256(run.stdout).hexdigest(), pin["record_sha256"])
        record = json.loads(run.stdout)
        archive = self.cards["source"]["original_archive"]
        self.assertEqual(record["source_revision"], "sha256:" + archive["sha256"])
        self.assertEqual({m["name"]: m["sha256"] for m in record["original_members"]}, PINS)
        self.assertEqual({m["name"]: m["bytes"] for m in record["original_members"]},
                         {m["name"]: m["bytes"] for m in archive["members"]})

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        card = lambda c, cid: next(x for x in c["real_example"]["cards"]  # noqa: E731
                                   if x["id"] == cid)
        gap = lambda g, gid: next(x for x in g["gaps"] if x["id"] == gid)  # noqa: E731
        cases = {
            "uncited": lambda c, g: card(c, "BR-03").update(cites=[]),
            "no source": lambda c, g: gap(g, "BR-GAP-02").update(sources=[]),
            "original pins": lambda c, g: c["source"]["original_archive"]["members"][0]
            .update(sha256="0" * 64),
            "advisory pin": lambda c, g: c["source"]["advisory"].update(record_sha256=""),
            "status honesty: stakeholder proposals": lambda c, g: c.update(
                status=c["status"].replace("stakeholder proposals", "decisions")),
            "map counts": lambda c, g: c["map_counts"].update(rules=14),
            "question for Laura or Regulatory": lambda c, g: gap(g, "BR-GAP-01")
            .update(dot_question="Who owns it?"),
            "source unknown without a Dot question": lambda c, g: gap(g, "BR-GAP-11")
            .pop("dot_question"),
            "missing original MO-01": lambda c, g: c["missing_originals"][0].pop("gap"),
            "price": lambda c, g: card(c, "BR-12").update(criterion="Costs USD 900."),
            "body": lambda c, g: c["source"]["original_archive"].update(body="x"),
            "e-mail address": lambda c, g: card(c, "BR-02").update(criterion="Ask a@b.com."),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
