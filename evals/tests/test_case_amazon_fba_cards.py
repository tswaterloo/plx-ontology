"""Amazon FBA case: PROPOSED dev-exposed cards and the ontology gaps they need.

``evals/dev/amazon-fba/proposed_items.json`` holds the questions of Greg's Amazon FBA
Replenishment Map (FBA-*): shipment identity across Amazon, the
Sage OR, Freightcom and BC, case-to-unit reconciliation, item to Amazon SKU mapping,
freight on final dims, the inventory move at BOL and the 45/90-day supply rule, and the
real shipment OR1000154027. The map's original is pinned by item id and SHA-256 (from
the record's source_id and source_revision) and reaches ``examples/case_originals.json``
as a crosswalk row of its watched ``/Ontology UseCases`` folder; the example's unread
originals are typed ``missing_originals``. ``ontology_gaps.json`` maps the map's seven
ranked gaps, two weak links, its dependency and the unread source files.

The contract is ``evals.case_cards``; this check adds the stricter pin, the map's ranks,
the typed missing originals, the questions for Greg or Vince, and no prices, bodies or
e-mail addresses.
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
DIR = ROOT / "evals" / "dev" / "amazon-fba"
PROPOSAL = "evals/dev/amazon-fba/proposed_items.json"
ADVISORY = "advisory:45cd3b70238a"
DRIVE = "b!W4ssx17NiUipzjgFW2Rt1fW8RXEL_4xBgKvtLTtDIYNO-gr9fxAMQ7PHzLrczCuH"
SHIPMENT = "OR1000154027"
# Every original kind the map names for the example; each unread one is typed.
ORIGINAL_KINDS = {"email", "order", "customs", "bol", "label", "freight", "tracking"}
STATES = {"link_pending", "to_come", "not_provided"}
# The three systems the map asks Greg or Vince about.
ASKED = ("Seller Central", "Freightcom", "sales")
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
    ids = {c["id"] for c in cards["real_example"]["cards"]}
    # The map's seven ranked gaps, each once, and every gap attributed to the map.
    ranks = sorted(g["map_rank"] for g in gaps["gaps"] if isinstance(g.get("map_rank"), int))
    if ranks != list(range(1, 8)) or not all(g.get("map_gap") and g.get("map_says")
                                             for g in gaps["gaps"]):
        out.append(f"ranked gaps {ranks}")
    asked = " ".join(g.get("open_question", "") for g in gaps["gaps"])
    out += [f"question for {s}" for s in ASKED if s not in asked]
    # The map's own original: its folder is the registry's /Ontology UseCases watch folder.
    watched = source.get("watched_originals", {})
    folders = {w["id"]: w for w in originals.get("watch_folders", [])}
    for name, wf in watched.get("folders", {}).items():
        w = folders.get(wf)
        if not w or (w["drive_id"], w["path"]) != (DRIVE, f"/{name}"):
            out.append(f"watched folder {name}")
    files = watched.get("files", [])
    if not files:
        out.append("watched file")
    for f in files:
        if (f.get("case") not in watched.get("folders", {}) or not SHA.match(str(f.get("sha256")))
                or not f.get("item_id") or f.get("advisory") != ADVISORY):
            out.append(f"watched file {f.get('item_id')}")
    # The example's unread originals: every kind typed, each with a state and its gap.
    missing = cards.get("missing_originals", [])
    gap_ids = {g["id"] for g in gaps["gaps"]}
    if not ORIGINAL_KINDS <= {m.get("kind") for m in missing}:
        out.append("missing original kinds")
    for m in missing:
        if (m.get("shipment") != SHIPMENT or m.get("state") not in STATES
                or m.get("gap") not in gap_ids or not str(m.get("item", "")).strip()):
            out.append(f"missing original {m.get('id')}")
    if not {"FBA-A6"} <= ids:
        out.append("originals card")
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


class AmazonFbaCardsTest(unittest.TestCase):
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
        """record_sha256 is the SHA-256 of the record at its commit, and the map's pin is
        the record's own source item, revision hash and size."""
        pin = self.cards["source"]["advisory"]
        raw = _git_show(pin["ref"], pin["path"])
        if raw is None:
            self.skipTest("commit e58e8a98 is not in this clone")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), pin["record_sha256"])
        record = json.loads(raw)
        self.assertEqual((record["source_revision"], record["created_at"]),
                         (pin["source_revision"], pin["created_at"]))
        (f,) = self.cards["source"]["watched_originals"]["files"]
        self.assertEqual(record["source_id"], f"sharepoint:drive:{DRIVE}:item:{f['item_id']}")
        self.assertEqual(record["source_revision"].rsplit("sha256:", 1)[1], f["sha256"])
        self.assertEqual(record["source_bytes"], f["bytes"])

    def test_registry_crosswalk_carries_the_committed_map_pin(self):
        """examples/case_originals.json was regenerated with this proposal as a crosswalk
        input: its recorded hash is the committed file's and the map is a watched source."""
        self.assertIn(f"--crosswalk {PROPOSAL}", self.originals["generated_by"])
        walk = self.originals["crosswalk"]
        (entry,) = [i for i in walk["inputs"] if i["path"] == PROPOSAL]
        self.assertEqual(entry["sha256"],
                         hashlib.sha256((ROOT / PROPOSAL).read_bytes()).hexdigest())
        (f,) = self.cards["source"]["watched_originals"]["files"]
        rows = [r for r in walk["rows"] if r["case"] == "amazon-fba"]
        self.assertEqual([(r["kind"], r["item_id"], r["sha256"], r["record"]) for r in rows],
                         [("watched_source", f["item_id"], f["sha256"], ADVISORY)])
        self.assertFalse([r["id"] for r in self.originals["rows"]
                          if "Amazon FBA" in json.dumps(r)])  # no row added by hand

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        card = lambda c, cid: next(x for x in c["real_example"]["cards"]  # noqa: E731
                                   if x["id"] == cid)
        gap = lambda g, gid: next(x for x in g["gaps"] if x["id"] == gid)  # noqa: E731
        files = lambda c: c["source"]["watched_originals"]["files"]  # noqa: E731
        cases = {
            # A removed citation, a missing gap source, an unpinned original.
            "uncited": lambda c, g: card(c, "FBA-03").update(cites=[]),
            "no source": lambda c, g: gap(g, "FBA-GAP-01").update(sources=[]),
            "source unknown without an open question": lambda c, g: gap(g, "FBA-GAP-03")
            .pop("open_question"),
            "watched file": lambda c, g: files(c)[0].update(sha256="pending"),
            "advisory pin": lambda c, g: c["source"]["advisory"].update(record_sha256=""),
            "missing original kinds": lambda c, g: c.update(missing_originals=[
                m for m in c["missing_originals"] if m["kind"] != "customs"]),
            "missing original MO-04": lambda c, g: c["missing_originals"][3].pop("gap"),
            "ranked gaps": lambda c, g: g["gaps"].pop(5),
            "question for Freightcom": lambda c, g: gap(g, "FBA-GAP-06")
            .update(open_question="Who books the truck?"),
            "price": lambda c, g: card(c, "FBA-06")
            .update(criterion="Freight was quoted at USD 900."),
            "body": lambda c, g: files(c)[0].update(body="Hi Greg"),
            "e-mail address": lambda c, g: card(c, "FBA-A3")
            .update(criterion="Sold to orders@example.com."),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
