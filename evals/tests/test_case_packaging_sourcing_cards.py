"""Packaging sourcing case: PROPOSED dev-exposed cards and the gaps they need.

``evals/dev/packaging-sourcing/proposed_items.json`` holds the questions of Ross's New
Packaging Sourcing proposal (PS-*, advisory 62802e1f72f6): approval sign-offs, part
creation at approval, drawing and HS code before order, component-level prices, PO before
PI, PI checks before payment, deposits as BC PO prepayments and the P-02486 allocation. The
proposal file is pinned by item id and SHA-256 (the record's source_id and source_revision)
and watched in ``/Ontology UseCases``, reaching ``examples/case_originals.json`` as a
crosswalk row; the originals it names and nobody supplied are typed as missing, unhashed.
``ontology_gaps.json`` lists the objects, links and readers the cards need.

The contract is ``evals.case_cards``; this check adds the stricter pins, the proposal's
rules P1-P6 kept as proposals pending Vince, a system of record or a question on every gap,
Portal/BC implementation claims labelled unverified, and no prices, bodies or e-mail
addresses.
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
DIR = ROOT / "evals" / "dev" / "packaging-sourcing"
PROPOSAL = "evals/dev/packaging-sourcing/proposed_items.json"
ADVISORY = "advisory:62802e1f72f6"
DRIVE = "b!W4ssx17NiUipzjgFW2Rt1fW8RXEL_4xBgKvtLTtDIYNO-gr9fxAMQ7PHzLrczCuH"
PENDING = "proposed, pending Vince"
RULES = [f"P{i}" for i in range(1, 7)]
RULE = re.compile(r"\bP[1-6]\b")
DECIDED = re.compile(r"\b(?:adopted|in force|agreed|decided by Vince)\b", re.I)
SHA = re.compile(r"^[0-9a-f]{64}$")
PRICE = re.compile(r"[$€£]\s?\d|\b(?:CAD|USD|EUR)\s?\d|\d\s?(?:CAD|USD|EUR)\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")


def problems(cards: dict, gaps: dict, originals: dict) -> list[str]:
    out: list[str] = []
    source = cards["source"]
    a = source["advisory"]
    short = a["id"].removeprefix("advisory:")
    if not (a["id"] == ADVISORY and "@" in a["ref"] and a["path"].startswith(f"context/{short}")
            and SHA.match(str(a.get("record_sha256", "")))):
        out.append(f"advisory pin {a['id']}")
    out += contract.problems(cards, gaps, originals)
    # The proposal file: watched in the registry's /Ontology UseCases folder and pinned; the
    # originals it names but nobody supplied are typed as missing and carry no hash.
    watched = source.get("watched_originals", {})
    folders = {w["id"]: w for w in originals.get("watch_folders", [])}
    for name, wf in watched.get("folders", {}).items():
        w = folders.get(wf)
        if not w or (w["drive_id"], w["path"]) != (DRIVE, f"/{name}"):
            out.append(f"watched folder {name}")
    files = watched.get("files", [])
    if len(files) != 1:
        out.append("watched file")
    for f in files:
        if (f.get("case") not in watched.get("folders", {}) or not SHA.match(str(f.get("sha256")))
                or not f.get("item_id") or f.get("advisory") != ADVISORY
                or not isinstance(f.get("bytes"), int) or f["bytes"] <= 0):
            out.append(f"watched file {f.get('item_id')}")
    gap_ids = {g["id"] for g in gaps["gaps"]}
    for m in cards.get("missing_originals", []):
        if m.get("gap") not in gap_ids or "sha256" in m or not m.get("state"):
            out.append(f"missing original {m.get('id')}")
    # P1-P6 stay proposals pending Vince: in the rule list and wherever a card or gap
    # names one.
    rules = cards.get("proposed_rules", {}).get("rules", [])
    if [r.get("id") for r in rules] != RULES or any(
            r.get("status") != PENDING or r.get("adopted") is not False for r in rules):
        out.append("P-rule adopted in proposed_rules")
    for rec in cards["real_example"]["cards"] + gaps["gaps"]:
        text = json.dumps(rec, ensure_ascii=False)
        if RULE.search(text) and (DECIDED.search(text) or "pending Vince" not in text):
            out.append(f"P-rule adopted {rec['id']}")
    # Every gap either names its system of record or says 'source unknown' with a question;
    # an implementation claim about the Portal or BC always labelled unverified.
    for g in gaps["gaps"]:
        if not str(g.get("proposal_says", "")).strip():
            out.append(f"proposal_says {g['id']}")
        if not (str(g.get("system_of_record", "")).strip()
                or (g.get("source") == contract.UNKNOWN
                    and str(g.get("dot_question", "")).endswith("?"))):
            out.append(f"system of record {g['id']}")
        if "implementation" in g and "unverified" not in g["implementation"]:
            out.append(f"implementation claim {g['id']}")
    # No prices, no bodies and no e-mail addresses in either file.
    text = json.dumps([cards, gaps], ensure_ascii=False)
    if PRICE.search(text):
        out.append(f"price {PRICE.search(text).group(0)!r}")
    if re.search(r'"(body|text|content|values|result)"\s*:', text):
        out.append("body")
    if EMAIL.search(text):
        out.append("e-mail address")
    return out


def _git_show(ref: str, path: str) -> bytes | None:
    commit = ref.rsplit("@", 1)[1]
    run = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
                         capture_output=True, check=False)
    return run.stdout if run.returncode == 0 else None


class PackagingSourcingCardsTest(unittest.TestCase):
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
        # Every card that needs a live source is named by a gap.
        needed = {c for g in self.gaps["gaps"] for c in g["needed_by"]}
        self.assertEqual([c["id"] for c in cards if c["live_sources"] and c["id"] not in needed],
                         [])

    def test_pin_matches_the_advisory_record(self):
        """record_sha256 is the SHA-256 of the record at its commit, and the watched file is
        the record's own source item, revision hash and byte count. Needs codex/advisory."""
        pins = [self.cards["source"]["advisory"], *self.cards["source"]["related_advisories"]]
        for pin in pins:
            raw = _git_show(pin["ref"], pin["path"])
            if raw is None:
                self.skipTest(f"advisory commit of {pin['id']} is not in this clone")
            self.assertEqual(hashlib.sha256(raw).hexdigest(), pin["record_sha256"], pin["id"])
            record = json.loads(raw)
            self.assertEqual((record["source_revision"], record["created_at"]),
                             (pin["source_revision"], pin["created_at"]), pin["id"])
        record = json.loads(_git_show(pins[0]["ref"], pins[0]["path"]))
        (f,) = self.cards["source"]["watched_originals"]["files"]
        self.assertEqual(record["source_id"], f"sharepoint:{DRIVE}:{f['item_id']}")
        self.assertEqual(record["source_revision"].rsplit("sha256:", 1)[1], f["sha256"])
        self.assertIn(f"{f['bytes']}bytes", record["evidence"][0]["detail"])

    def test_registry_crosswalk_carries_the_committed_pin(self):
        """examples/case_originals.json was regenerated with this proposal as a crosswalk
        input: its recorded hash is the committed file's and the proposal file is a watched
        source; no registry row was added by hand."""
        self.assertIn(f"--crosswalk {PROPOSAL}", self.originals["generated_by"])
        walk = self.originals["crosswalk"]
        (entry,) = [i for i in walk["inputs"] if i["path"] == PROPOSAL]
        self.assertEqual(entry["sha256"],
                         hashlib.sha256((ROOT / PROPOSAL).read_bytes()).hexdigest())
        (f,) = self.cards["source"]["watched_originals"]["files"]
        rows = [r for r in walk["rows"] if r["case"] == "packaging-sourcing"]
        self.assertEqual([(r["kind"], r["item_id"], r["sha256"], r["record"]) for r in rows],
                         [("watched_source", f["item_id"], f["sha256"], ADVISORY)])
        self.assertFalse([r["id"] for r in self.originals["rows"]
                          if "Packaging Sourcing" in json.dumps(r, ensure_ascii=False)])

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        card = lambda c, cid: next(x for x in c["real_example"]["cards"]  # noqa: E731
                                   if x["id"] == cid)
        gap = lambda g, gid: next(x for x in g["gaps"] if x["id"] == gid)  # noqa: E731
        cases = {
            "uncited": lambda c, g: card(c, "PS-09").update(cites=[]),
            "no source": lambda c, g: gap(g, "PS-GAP-08").update(sources=[]),
            "watched file": lambda c, g: c["source"]["watched_originals"]["files"][0]
            .update(sha256="pending"),
            "P-rule adopted PS-09": lambda c, g: card(c, "PS-09").update(
                criterion="P5 is adopted: purchasing records every deposit as a BC PO "
                          "prepayment."),
            "P-rule adopted in proposed_rules": lambda c, g: c["proposed_rules"]["rules"][0]
            .update(adopted=True),
            "price": lambda c, g: card(c, "PS-07")
            .update(criterion="The cap costs USD 900."),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
