"""Expired-material disposition case: PROPOSED dev-exposed cards and the gaps they need.

``evals/dev/expired-disposition/proposed_items.json`` holds the questions of QC's
Expired-Material Disposition Map (EMD-*): one disposition case per
lot, the retest, extension, recertification, waiver, relabel and disposal routes,
batch-demand priority, customer policy, extension history, approval evidence and the
waiver that sets no new expiry, plus the R-0663 extension and the R-0030 waiver. The
archive is pinned by item id and SHA-256 (the record's source_id and source_revision) and
watched in ``/Ontology UseCases``; its ten members are pinned by path, bytes and SHA-256 in
the proposal's ``original_archive`` descriptor, and both reach ``examples/case_originals.json``
as crosswalk rows. ``ontology_gaps.json`` maps the map's ten gaps in order plus the
disposal, replacement and waiver-verification links.

The contract is ``evals.case_cards``; this check adds the stricter pins, the map's gap
order, a system of record or a question for QC on every gap, Portal/BC implementation
claims labelled unverified, the waiver's identifiers kept unverified, and no prices,
bodies or e-mail addresses.
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
DIR = ROOT / "evals" / "dev" / "expired-disposition"
PROPOSAL = "evals/dev/expired-disposition/proposed_items.json"
ADVISORY = "advisory:28a8482d8ba0"
DRIVE = "b!W4ssx17NiUipzjgFW2Rt1fW8RXEL_4xBgKvtLTtDIYNO-gr9fxAMQ7PHzLrczCuH"
ARCHIVE_ROOT = "Expired Material Disposition Map/"
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
    # The archive: watched in the registry's /Ontology UseCases folder, pinned, and its
    # members pinned under the same item and hash.
    watched = source.get("watched_originals", {})
    folders = {w["id"]: w for w in originals.get("watch_folders", [])}
    for name, wf in watched.get("folders", {}).items():
        w = folders.get(wf)
        if not w or (w["drive_id"], w["path"]) != (DRIVE, f"/{name}"):
            out.append(f"watched folder {name}")
    files = watched.get("files", [])
    archive = source.get("original_archive", {})
    if len(files) != 1:
        out.append("watched file")
    for f in files:
        if (f.get("case") not in watched.get("folders", {}) or not SHA.match(str(f.get("sha256")))
                or not f.get("item_id") or f.get("advisory") != ADVISORY
                or (f.get("item_id"), f.get("sha256"), f.get("bytes"))
                != (archive.get("item_id"), archive.get("sha256"), archive.get("bytes"))):
            out.append(f"watched file {f.get('item_id')}")
    members = archive.get("members", [])
    paths = [m.get("path", "") for m in members]
    if len(members) != 10 or len(set(paths)) != 10 or sum(p.endswith(".pdf") for p in paths) != 2:
        out.append("archive members")
    for m in members:
        if (not m["path"].startswith(ARCHIVE_ROOT) or not SHA.match(str(m.get("sha256")))
                or not isinstance(m.get("bytes"), int) or m["bytes"] <= 0):
            out.append(f"unpinned member {m.get('path')}")
    # The waiver's lot and batch stay unverified, wherever they are named.
    waiver = cards.get("waiver_identifiers", {})
    claimed = []
    for key in ("lot", "batch"):
        ident = waiver.get(key, {})
        claimed.append(str(ident.get("claimed", "")))
        if ident.get("verified") is not False or not claimed[-1]:
            out.append(f"waiver {key} verified")
    if waiver.get("member") not in paths:
        out.append("waiver member")
    for rec in cards["real_example"]["cards"] + gaps["gaps"]:
        text = json.dumps(rec, ensure_ascii=False)
        if any(c and c in text for c in claimed) and "unverified" not in text:
            out.append(f"waiver identifiers unmarked in {rec['id']}")
    # The map's ten gaps in its order; every gap attributed to the map and either naming
    # its system of record or 'source unknown' with a question for QC; an implementation
    # claim about the Portal or BC always labelled unverified.
    order = [g["map_gap_index"] for g in gaps["gaps"] if "map_gap_index" in g]
    if order != list(range(1, 11)):
        out.append(f"map gaps {order}")
    for g in gaps["gaps"]:
        if not str(g.get("map_says", "")).strip():
            out.append(f"map_says {g['id']}")
        if not (str(g.get("system_of_record", "")).strip()
                or (g.get("source") == contract.UNKNOWN
                    and str(g.get("open_question", "")).startswith("QC"))):
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


class ExpiredDispositionCardsTest(unittest.TestCase):
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
        """record_sha256 is the SHA-256 of the record at its commit; the archive pin is the
        record's own source item, revision hash and size, and the members it hashes carry
        the same hashes in the descriptor."""
        pin = self.cards["source"]["advisory"]
        raw = _git_show(pin["ref"], pin["path"])
        if raw is None:
            self.skipTest("commit c8fcb26d is not in this clone")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), pin["record_sha256"])
        record = json.loads(raw)
        self.assertEqual((record["source_revision"], record["created_at"]),
                         (pin["source_revision"], pin["created_at"]))
        (f,) = self.cards["source"]["watched_originals"]["files"]
        self.assertEqual(record["source_id"], f"sharepoint:drive:{DRIVE}:item:{f['item_id']}")
        self.assertEqual(record["source_revision"].rsplit("sha256:", 1)[1], f["sha256"])
        self.assertEqual(record["source_bytes"], f["bytes"])
        members = {m["path"].rsplit("/", 1)[1]: m["sha256"]
                   for m in self.cards["source"]["original_archive"]["members"]}
        hashes = record["source_member_hashes"]
        self.assertTrue(hashes)
        for h in hashes:
            self.assertEqual(members[h["name"]], h["sha256"], h["name"])

    def test_registry_crosswalk_carries_the_committed_archive_pin(self):
        """examples/case_originals.json was regenerated with this proposal as a crosswalk
        input: its recorded hash is the committed file's (which pins the members) and the
        archive is both a watched source and a descriptor."""
        self.assertIn(f"--crosswalk {PROPOSAL}", self.originals["generated_by"])
        walk = self.originals["crosswalk"]
        (entry,) = [i for i in walk["inputs"] if i["path"] == PROPOSAL]
        self.assertEqual(entry["sha256"],
                         hashlib.sha256((ROOT / PROPOSAL).read_bytes()).hexdigest())
        (f,) = self.cards["source"]["watched_originals"]["files"]
        rows = [r for r in walk["rows"] if r["case"] == "expired-disposition"]
        self.assertEqual(sorted((r["kind"], r["item_id"], r["sha256"], r["record"]) for r in rows),
                         [("descriptor", f["item_id"], f["sha256"], ADVISORY),
                          ("watched_source", f["item_id"], f["sha256"], ADVISORY)])
        self.assertFalse([r["id"] for r in self.originals["rows"]
                          if "Expired" in json.dumps(r)])  # no row added by hand

    def test_check_fails_closed(self):
        def mutated(edit):
            cards, gaps = copy.deepcopy(self.cards), copy.deepcopy(self.gaps)
            edit(cards, gaps)
            return problems(cards, gaps, self.originals)

        card = lambda c, cid: next(x for x in c["real_example"]["cards"]  # noqa: E731
                                   if x["id"] == cid)
        gap = lambda g, gid: next(x for x in g["gaps"] if x["id"] == gid)  # noqa: E731
        source = lambda c: c["source"]  # noqa: E731
        cases = {
            # A removed citation, a missing gap source, an unpinned original.
            "uncited": lambda c, g: card(c, "EMD-05").update(cites=[]),
            "no source": lambda c, g: gap(g, "EMD-GAP-07").update(sources=[]),
            "system of record EMD-GAP-04": lambda c, g: gap(g, "EMD-GAP-04")
            .pop("system_of_record"),
            "source unknown without an open question": lambda c, g: gap(g, "EMD-GAP-02")
            .pop("open_question"),
            "watched file": lambda c, g: source(c)["watched_originals"]["files"][0]
            .update(sha256="pending"),
            "unpinned member": lambda c, g: source(c)["original_archive"]["members"][0]
            .pop("sha256"),
            "archive members": lambda c, g: source(c)["original_archive"]["members"].pop(1),
            # The waiver's identifiers stay unverified.
            "waiver lot verified": lambda c, g: c["waiver_identifiers"]["lot"]
            .update(verified=True),
            "waiver identifiers unmarked in EMD-A4": lambda c, g: card(c, "EMD-A4")
            .update(criterion="The waiver covers lot 52174440 for batch BC-26267D."),
            # Map claims apart from implementation claims; the map's ten gaps.
            "implementation claim EMD-GAP-11": lambda c, g: gap(g, "EMD-GAP-11")
            .update(implementation="write-offs post to BC through codeunit 50116"),
            "map gaps": lambda c, g: g["gaps"].pop(2),
            "price": lambda c, g: card(c, "EMD-11")
            .update(criterion="The replacement cost USD 900."),
            "body": lambda c, g: source(c)["original_archive"].update(text="Dear QC"),
            "e-mail address": lambda c, g: card(c, "EMD-04")
            .update(criterion="Ask qa@example.com."),
        }
        for label, edit in cases.items():
            found = mutated(edit)
            self.assertTrue(any(label in p for p in found), (label, found))


if __name__ == "__main__":
    unittest.main()
