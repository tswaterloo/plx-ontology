"""Quote-to-order case: PROPOSED dev-exposed cards and the ontology gaps they need.

``evals/dev/quote-to-order/proposed_items.json`` holds the employee questions of Vince's
Quote-to-Order Map (QO-*, advisory 72bf7b7ae3f6, gap-ba79665b1e75; its invitation
b663c5f3b388 is pinned as related only), of the quote workbook and email originals of
the three AGENTS ``/Quote Examples/{G2-Beauty,SAHAJAN,Soapbox}/_Quote Workbooks``
folders (QW-*, manifests bb494ac6ba9d and 0dc11f49acee) and of the cost-to-assembly user
journey (CJ-*, bc0e2a7d0f24). Each worked example A-E names its missing outcome (the BC
order or the lost reason) in an outcome card and records the quote inputs missing at
quote time, the slot for the announced incomplete-information case (c70b2464d73c).
``ontology_gaps.json`` maps each of the map's 16 ranked gaps to an ontology gap.

The contract is ``evals.case_cards`` (related advisories and per-map citations are native
to it). The workbook
pins are names, item ids and SHA-256 only; ``examples/case_originals.json`` watches the
three folders, so a ``case.originals_read`` run reads them as discovered files whose
receipts compare against these pins.
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
DIR = ROOT / "evals" / "dev" / "quote-to-order"
MAPS = {"QO": {"advisory:72bf7b7ae3f6", "advisory:b663c5f3b388"},
        "QW": {"advisory:bb494ac6ba9d", "advisory:0dc11f49acee"},
        "CJ": {"advisory:bc0e2a7d0f24"}}
DRIVE = "b!W4ssx17NiUipzjgFW2Rt1fW8RXEL_4xBgKvtLTtDIYNO-gr9fxAMQ7PHzLrczCuH"
FOLDER = "/Quote Examples/{}/_Quote Workbooks"
OUTCOME_WORDS = {"bc_order": "Business Central", "lost_reason": "lost reason"}
INPUT_STATUSES = {"missing", "assumed", "unclear"}
SHA = re.compile(r"^[0-9a-f]{64}$")
# A money amount: a currency sign or code next to a number. Cards copy no prices.
PRICE = re.compile(r"[$€£]\s?\d|\b(?:CAD|USD|EUR)\s?\d|\d\s?(?:CAD|USD|EUR)\b")


def problems(cards: dict, gaps: dict, originals: dict) -> list[str]:
    out: list[str] = []
    source = cards["source"]
    # Stricter than the shared pin: the path names the record and a sha256 is held.
    pins = [source["advisory"], *source.get("related_advisories", [])]
    for a in pins:
        short = a["id"].removeprefix("advisory:")
        if not (a["id"].startswith("advisory:") and "@" in a["ref"]
                and a["path"].startswith(f"context/{short}")
                and SHA.match(str(a.get("record_sha256", "")))):
            out.append(f"advisory pin {a['id']}")
    out += contract.problems(cards, gaps, originals)
    by_id = {c["id"]: c for c in cards["real_example"]["cards"]}
    # Examples A-E: an outcome card naming the missing outcome, and the missing inputs.
    examples = cards.get("examples", {})
    if sorted(examples) != list("ABCDE"):
        out.append("examples A-E")
    for name, ex in examples.items():
        card = by_id.get(ex.get("outcome_card"))
        wanted = ex.get("missing_outcome") or []
        if not card or not wanted or any(w not in OUTCOME_WORDS
                                         or OUTCOME_WORDS[w] not in card["question"]
                                         for w in wanted):
            out.append(f"example {name}: outcome card")
        if ex.get("missing_inputs_card") not in by_id:
            out.append(f"example {name}: missing inputs card")
        inputs = ex.get("missing_at_quote") or []
        if not inputs or any(not str(i.get("input", "")).strip()
                             or i.get("status") not in INPUT_STATUSES for i in inputs):
            out.append(f"example {name}: missing at quote")
    # The map's 16 ranked gaps, each once.
    ranks = sorted(g.get("map_rank", 0) for g in gaps["gaps"])
    if ranks != list(range(1, 17)) or not all(g.get("map_gap") for g in gaps["gaps"]):
        out.append(f"ranked gaps {ranks}")
    # Watched originals: three folders on the AGENTS drive, every pin under one of them.
    watched = source.get("watched_originals", {})
    registry = {w["id"]: w for w in originals.get("watch_folders", [])}
    for case, wf in watched.get("folders", {}).items():
        w = registry.get(wf)
        if not w or (w["drive_id"], w["path"]) != (DRIVE, FOLDER.format(case)):
            out.append(f"watched folder {case}")
    files = watched.get("files", [])
    if len({f.get("item_id") for f in files}) != len(files) or not files:
        out.append("watched files")
    for f in files:
        if (f.get("case") not in watched.get("folders", {}) or not SHA.match(str(f.get("sha256")))
                or f.get("advisory") not in MAPS["QW"]):
            out.append(f"watched file {f.get('item_id')}")
    # No prices and no bodies in either file.
    text = json.dumps([cards, gaps], ensure_ascii=False)
    if PRICE.search(text):
        out.append(f"price {PRICE.search(text).group(0)!r}")
    if re.search(r'"(body|text|content|values|result)"\s*:', text):
        out.append("body")
    return out


def _git_show(ref: str, path: str) -> bytes | None:
    commit = ref.rsplit("@", 1)[1]
    run = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
                         capture_output=True, check=False)
    return run.stdout if run.returncode == 0 else None


class QuoteToOrderCardsTest(unittest.TestCase):
    def setUp(self):
        self.cards, self.gaps = (json.loads((DIR / n).read_text(encoding="utf-8"))
                                 for n in ("proposed_items.json", "ontology_gaps.json"))
        self.originals = json.loads(contract.ORIGINALS.read_text(encoding="utf-8"))

    def test_committed_cards_and_gaps_satisfy_the_contract(self):
        self.assertEqual(problems(self.cards, self.gaps, self.originals), [])
        cards = self.cards["real_example"]["cards"]
        for prefix, advisories in MAPS.items():
            self.assertEqual(set(self.cards["maps"][prefix]["advisories"]), advisories)
            mine = [c for c in cards if c["id"].startswith(prefix + "-")]
            self.assertTrue(mine, prefix)
        self.assertEqual({c["id"].split("-")[0] for c in cards}, set(MAPS))
        self.assertEqual(self.cards["maps"]["QO"]["gaps"][0], "gap-ba79665b1e75")
        self.assertEqual({g["kind"] for g in self.gaps["gaps"]}, contract.GAP_KINDS)
        # Every card that needs a live source is named by a gap.
        needed = {c for g in self.gaps["gaps"] for c in g["needed_by"]}
        self.assertEqual([c["id"] for c in cards if c["live_sources"] and c["id"] not in needed],
                         [])
        # The incomplete-information case slots into the examples' missing inputs.
        planned = self.cards["planned_case"]
        self.assertIn(planned["advisory"],
                      {a["id"] for a in self.cards["source"]["related_advisories"]})
        self.assertEqual(planned["slot"], "examples.*.missing_at_quote")
        # The three _Quote Workbooks folders are watched; 11 workbooks and 17 other entries.
        files = self.cards["source"]["watched_originals"]["files"]
        self.assertEqual(sorted(self.cards["source"]["watched_originals"]["folders"]),
                         ["G2-Beauty", "SAHAJAN", "Soapbox"])
        self.assertEqual(sum(f["advisory"] == "advisory:bb494ac6ba9d" for f in files), 11)
        self.assertEqual(sum(f["advisory"] == "advisory:0dc11f49acee" for f in files), 17)

    def test_pins_match_the_advisory_records(self):
        """Each record_sha256 is the SHA-256 of the record at its commit, and each watched
        file pin equals the manifest's own entry. Needs the codex/advisory commits."""
        source = self.cards["source"]
        pins = [source["advisory"], *source["related_advisories"]]
        records = {}
        for pin in pins:
            raw = _git_show(pin["ref"], pin["path"])
            if raw is None:
                self.skipTest(f"advisory commit of {pin['id']} is not in this clone")
            self.assertEqual(hashlib.sha256(raw).hexdigest(), pin["record_sha256"], pin["id"])
            records[pin["id"]] = json.loads(raw)
        for f in source["watched_originals"]["files"]:
            manifest = {o["item_id"]: o for o in records[f["advisory"]]["original_files"]}
            o = manifest.get(f["item_id"])
            self.assertIsNotNone(o, f["item_id"])
            self.assertEqual((o["case"], o["name"], o["sha256"], o["bytes"]),
                             (f["case"], f["name"], f["sha256"], f["bytes"]))
        self.assertEqual(len(source["watched_originals"]["files"]),
                         sum(len(records[a]["original_files"]) for a in MAPS["QW"]))

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        card = lambda c, cid: next(x for x in c["real_example"]["cards"]  # noqa: E731
                                   if x["id"] == cid)
        cases = {
            "uncited": lambda c, g: card(c, "QO-01").update(cites=["made-up"]),
            # Removing the map's own citation from an outcome card.
            "not cited to its map": lambda c, g: card(c, "QO-A1")["cites"]
            .remove("advisory:72bf7b7ae3f6"),
            "advisory pin": lambda c, g: c["source"]["related_advisories"][1]
            .update(record_sha256="0" * 63),
            "exposure": lambda c, g: c["real_example"].update(exposure="held-out"),
            "unknown original": lambda c, g: card(c, "QW-01")["originals"]
            .append("co-8fabde0ade07"),
            "unknown live source": lambda c, g: card(c, "QO-A1")["live_sources"]
            .append("bc.valueEntries"),
            "keys": lambda c, g: card(c, "QO-A2").update(missing_inputs=["formula cost"]),
            "example C: outcome card": lambda c, g: card(c, "QO-C1")
            .update(question="What happened to the G2 Beauty display quote?"),
            "example D: missing at quote": lambda c, g: c["examples"]["D"]
            .update(missing_at_quote=[]),
            "example E: missing at quote": lambda c, g: c["examples"]["E"]["missing_at_quote"][0]
            .update(status="known"),
            "examples A-E": lambda c, g: c["examples"].pop("B"),
            "ranked gaps": lambda c, g: g["gaps"].pop(11),
            "Dot question": lambda c, g: next(x for x in g["gaps"]
                                              if x.get("source") == contract.UNKNOWN)
            .update(dot_question="ask the Dot"),
            "no source": lambda c, g: next(x for x in g["gaps"] if "source" not in x)
            .update(sources=[]),
            "path@commit": lambda c, g: g["gaps"][0]["sources"][0].update(ref="the map"),
            "needed_by": lambda c, g: g["gaps"][0]["needed_by"].append("QO-99"),
            "watched folder SAHAJAN": lambda c, g: c["source"]["watched_originals"]["folders"]
            .update(SAHAJAN="wf-a672d852ff20"),
            "watched file": lambda c, g: c["source"]["watched_originals"]["files"][0]
            .update(sha256="pending"),
            "price": lambda c, g: card(c, "QO-D2")
            .update(criterion="The refill bottle sold at $0.50."),
            "body": lambda c, g: c["source"]["watched_originals"]["files"][0]
            .update(body="Hi Vince"),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))

    def test_watch_folders_reach_the_registry_offline(self):
        """The registry lists the three folders through --watch-drive, as generated_by
        records, so the next read lists them; no quote row was added by hand."""
        folders = self.cards["source"]["watched_originals"]["folders"]
        for case in folders:
            self.assertIn(f"--watch-drive '{DRIVE}' '{FOLDER.format(case)}'",
                          self.originals["generated_by"])
        self.assertFalse([r["id"] for r in self.originals["rows"]
                          if "Quote Examples" in json.dumps(r).replace("%20", " ")])


if __name__ == "__main__":
    unittest.main()
