"""Exposed real-case eval items (evals/exposed) and the check of the originals they cite.

Two boundaries:

* the items as evaluation data: the runner loads them as dev items (never held-out), every
  item cites originals that ``evals/exposed/sources.json`` pins by identity, unknowns are
  gaps (never invented values), and every root identifier is denylisted for product code;
* ``scripts/eval_exposed_sources.py`` (the ``case.exposed_originals`` /
  ``case.exposed_mail`` live checks): pinned originals are verified only through the
  GET-only Graph readers, a changed byte fails the check, and the output carries no
  name, id or path.

Evidence label: the items are curated data (no live read); the checker tests are
``injected-transport`` (the real ``SharePointReader`` / ``GraphMailReader`` over the
in-process ``FakeSender`` of the adapter tests, with synthetic bytes).
"""

from __future__ import annotations

import base64
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote, unquote, urlsplit
import zipfile

from verify import live_verify
from evals import partition, runner, score
from evals.tests import fixtures as fx
from integrations.tests.test_live_source_adapters import (DRIVE, ENV, MAILBOX, MSG_PATH,
                                                          SP_HOST, FakeSender, j, message)
from scripts import eval_exposed_sources as checker

ROOT = Path(__file__).resolve().parents[1]
EXPOSED = ROOT / "evals" / "exposed"
SOURCES_REF = "evals/exposed/sources.json#"
AO_REF = "evals/dev/cases/ao_originals_expected.json#"


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _exposed():
    return (runner.load_items(EXPOSED, split="dev"), _read(EXPOSED / "manifest.json"),
            _read(EXPOSED / "sources.json"))


def _pinned_refs(sources):
    """Every pinned original and message: its citation handle -> the derived_from ref
    naming it (exposed sources by id; AO packets by JSON pointer into their expected
    identity, which the ao-originals live check verifies)."""
    pins: dict[str, str] = {}
    for entry in sources["drive_items"]:
        if entry["sha256"] and entry["sha256_basis"] == "bytes":
            pins["original://sha256/" + entry["sha256"]] = SOURCES_REF + entry["source_id"]
        elif entry["sha256"] is None:
            pins["original://sharepoint/{}/{}?etag={}".format(*(
                quote(entry[k], safe="") for k in ("drive_id", "item_id", "e_tag")))] = \
                SOURCES_REF + entry["source_id"]
    for entry in sources["archive_members"] + sources["mail_attachments"]:
        pins["original://sha256/" + entry["sha256"]] = SOURCES_REF + entry["source_id"]
    for entry in sources["mail_messages"]:
        pins["graph-mail://unrecorded-mailbox/{}#received_at={}".format(
            quote(entry["message_id"], safe=""), entry["received_at"])] = \
            SOURCES_REF + entry["source_id"]
    expected = _read(ROOT / AO_REF.rstrip("#"))
    for index, entry in enumerate(expected["files"]):
        pins["original://sha256/" + entry["sha256"]] = f"{AO_REF}/files/{index}"
    return pins


class ExposedItemsTest(unittest.TestCase):
    """The committed items as evaluation data."""

    @classmethod
    def setUpClass(cls):
        cls.items, cls.manifest, cls.sources = _exposed()

    def test_items_load_as_dev_items_listed_in_the_manifest_and_never_held_out(self):
        ids = sorted(item["id"] for item in self.items)
        self.assertEqual(ids, sorted(row["id"] for row in self.manifest["items"]))
        self.assertEqual(len(ids), self.manifest["counts"]["items"])
        self.assertEqual(self.manifest["split"], "dev")
        for item in self.items:
            self.assertEqual((item["split"], item["variant"]), ("dev", "standard"))
        committed_held_out = {path.name for path in (ROOT / "evals" / "heldout").rglob("*.json")}
        self.assertFalse(any(item_id in name for item_id in ids for name in committed_held_out))
        # Kept apart from the synthetic dev set the fixture baseline runs over.
        dev_ids = {item["id"] for item in runner.load_items(ROOT / "evals" / "dev", split="dev")}
        self.assertFalse(dev_ids & set(ids))

    def test_every_item_cites_originals_pinned_by_identity(self):
        by_id: dict[str, dict] = {}
        for group in ("drive_items", "archive_members", "mail_messages", "mail_attachments"):
            for entry in self.sources[group]:
                self.assertNotIn(entry["source_id"], by_id)
                by_id[entry["source_id"]] = {"group": group, **entry}
        records = {record["path"] for record in self.sources["evidence_records"].values()}
        for entry in by_id.values():
            self.assertIn(entry["evidence"].split("#", 1)[0], records, entry["source_id"])
            if entry["group"] == "drive_items":
                self.assertTrue(entry["drive_id"] and entry["item_id"] and entry["e_tag"])
                self.assertTrue(entry["sha256"] or entry["quick_xor_hash"])
            elif entry["group"] == "archive_members":
                self.assertEqual(by_id[entry["archive"]]["group"], "drive_items")
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
            elif entry["group"] == "mail_messages":
                self.assertTrue(entry["message_id"] and entry["received_at"])
            else:
                self.assertEqual(by_id[entry["message"]]["group"], "mail_messages")
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
        ao_files = len(_read(ROOT / AO_REF.rstrip("#"))["files"])
        for item in self.items:
            cited = [ref for ref in item["oracle"]["derived_from"]
                     if ref.startswith(score.PINNED_SOURCES)]
            self.assertTrue(cited, item["id"])
            for ref in cited:
                if ref.startswith(SOURCES_REF):
                    self.assertIn(ref[len(SOURCES_REF):], by_id, item["id"])
                else:
                    self.assertRegex(ref, re.escape(AO_REF) + r"/files/\d+$")
                    self.assertLess(int(ref.rsplit("/", 1)[1]), ao_files)

    def test_every_expected_value_cites_a_pinned_source_the_item_derives_from(self):
        # A stated value is scored only against the original it is read from: byte
        # SHA-256 (drive item, archive member, mail attachment), a drive item version
        # when no byte hash is pinned, or the pinned message; or against the source row
        # a recorded live read gave (an oracle row reference of the item's own pinned
        # generation, the leading digest of that read's evidence record).
        pins = _pinned_refs(self.sources)
        for item in self.items:
            self.assertEqual(score.uncited_expected(item), [], item["id"])
            cited = set(item["oracle"]["derived_from"])
            generations = {str(g) for g in item["as_known_at"]["l1_generations"].values()}
            reads = {ref: _read(ROOT / ref)["evidence_digest"] for ref in cited
                     if ref.startswith("EVIDENCE/live/")}
            for value in item["known_outcome"]["values"]:
                self.assertTrue(value["citations"], (item["id"], value["name"]))
                for cite in value["citations"]:
                    if score.row_identity(cite["l1_handle"], generations):
                        generation = score.ORACLE_HANDLE.match(cite["l1_handle"]).group(3)
                        self.assertTrue(any(digest.startswith(generation)
                                            for digest in reads.values()),
                                        (item["id"], value["name"]))
                        continue
                    self.assertIn(pins.get(cite["l1_handle"]), cited,
                                  (item["id"], value["name"]))
                    if cite["l1_handle"].startswith("original://"):
                        self.assertRegex(cite["l1_handle"], score.PINNED_ORIGINAL)

    def test_unestablished_outcomes_are_gaps_and_labels_await_a_person(self):
        for item in self.items:
            outcome = item["known_outcome"]
            self.assertEqual(outcome["answerability"], "partial", item["id"])
            self.assertTrue(any(gap["state"] == "unknown" for gap in outcome["gaps"]), item["id"])
            self.assertTrue({"propose_action", "invent_absent_value"} <= set(item["must_not"]))
            if item["label"]["status"] != "human_audited":
                self.assertIn("Pending human label", item["label"]["note"])

    def test_root_identifiers_are_denylisted_for_product_code(self):
        denylist = json.loads((ROOT / "verify" / "hardcoded-case-denylist.json")
                              .read_text(encoding="utf-8"))
        patterns = [re.compile(entry["pattern"]) for entry in denylist["identifiers"]]
        for item in self.items:
            root = item["root"]["identifier"]
            self.assertTrue(any(pattern.search(root) for pattern in patterns), root)

    def test_every_case_card_is_converted_or_excluded_with_a_reason(self):
        cards = _read(EXPOSED / "eval_batch_01_cases.json")["real_example"]["cards"]
        converted = {row["batch_unit"] for row in self.manifest["items"]}
        excluded = {row["batch_unit"]: row["reason"] for row in self.manifest["excluded"]}
        for card in cards:
            self.assertTrue(card["id"] in converted or excluded.get(card["id"]), card["id"])

    def test_imported_p2p8_items_cite_the_original_their_gold_was_read_from(self):
        # Imported from the bundle as untrusted data: the page/span gold names the
        # original by byte SHA-256; the item's byte pin and every citation are that one.
        gold = _read(EXPOSED / "p2p8-field-evidence.json")
        imported = [row for row in self.manifest["items"] if "p2p8-field-evidence.json" in
                    " ".join(row["evidence"])]
        self.assertEqual(len(imported), 4)
        by_id = {item["id"]: item for item in self.items}
        for row in imported:
            item = by_id[row["id"]]
            pointer = next(ref for ref in item["oracle"]["derived_from"]
                           if ref.startswith("evals/exposed/p2p8-field-evidence.json#"))
            entry = gold["items"][int(pointer.rsplit("/", 1)[1])]
            self.assertEqual(entry["id"], row["batch_unit"])
            sha = gold["sources"][entry["source"]]["sha256"]
            self.assertEqual(item["as_known_at"]["l1_generations"]["curated_original_bytes"], sha)
            handles = {cite["l1_handle"] for value in item["known_outcome"]["values"]
                       for cite in value.get("citations") or []}
            self.assertEqual(handles, {"original://sha256/" + sha}, row["id"])
            self.assertIn(item["root"]["identifier"], item["question"]["text"])

    def test_related_documents_and_near_duplicates_stay_in_one_partition(self):
        # The synthetic dev set shares no source document, root or identifier with the
        # exposed real cases, and nothing committed is held out.
        dev = runner.load_items(ROOT / "evals" / "dev", split="dev")
        self.assertEqual(partition.conflicts(dev, self.items), [])
        self.assertEqual({score.partition_of(item) for item in self.items}, {"exposed"})
        self.assertEqual({score.partition_of(item) for item in dev}, {"dev"})
        for path in (ROOT / "evals").rglob("*.json"):
            self.assertNotEqual(_read(path).get("split") if path.name != "item.schema.json"
                                else None, "held_out", path)


class EvalDirectoryLoaderTest(unittest.TestCase):
    """An eval directory holds items and declared files; anything else fails loudly."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.item = _read(EXPOSED / "dev-F7-0002-exposed.json")
        manifest = {"format": runner.EXPOSED_MANIFEST_FORMAT, "items": [{"id": self.item["id"]}]}
        self.write("item.json", self.item)
        self.write("manifest.json", manifest)

    def write(self, name, value):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if not isinstance(value, str) else value, "utf-8")

    def refused(self, pattern):
        with self.assertRaisesRegex(runner.RunnerError, pattern):
            runner.load_items(self.dir, split="dev")

    def test_the_committed_directories_load_every_file(self):
        self.assertEqual(len(runner.load_items(ROOT / "evals" / "dev", split="dev")), 30)
        self.assertEqual(len(runner.load_items(EXPOSED, split="dev")), 14)
        self.assertEqual([i["id"] for i in runner.load_items(self.dir, split="dev")],
                         [self.item["id"]])

    def test_an_undeclared_unknown_or_unreadable_file_is_refused_not_skipped(self):
        # The case-card batch was silently skipped before it declared a format.
        cards = _read(EXPOSED / "eval_batch_01_cases.json")
        undeclared = {key: value for key, value in cards.items() if key != "format"}
        self.write("cases/batch.json", undeclared)
        self.refused("eval_file_undeclared:batch.json")
        self.write("cases/batch.json", {**undeclared, "format": "plx-eval-cards/v9"})
        self.refused("eval_file_format_unknown:batch.json")
        self.write("cases/batch.json", {**cards, "real_example": {"cards": []}})
        self.refused("eval_file_invalid:batch.json")
        self.write("cases/batch.json", ["not", "a", "declared", "file"])
        self.refused("eval_file_undeclared:batch.json")
        self.write("cases/batch.json", "{not json")
        self.refused("item_unreadable:batch.json")
        self.write("cases/batch.json", cards)
        self.assertEqual(len(runner.load_items(self.dir, split="dev")), 1)

    def test_exposed_items_are_exactly_the_manifest_items(self):
        self.write("manifest.json", {"format": runner.EXPOSED_MANIFEST_FORMAT, "items": []})
        self.refused("exposed_items_differ_from_manifest:dev-F7-0002-exposed")
        self.write("manifest.json", {"format": runner.EXPOSED_MANIFEST_FORMAT, "items": [
            {"id": self.item["id"]}, {"id": "dev-F7-0999-exposed"}]})
        self.refused("exposed_items_differ_from_manifest:dev-F7-0999-exposed")
        (self.dir / "manifest.json").unlink()
        self.refused("exposed_items_differ_from_manifest")


class StubClaude:
    """A stand-in ``claude`` binary (no model, no network): prints a stream-json session."""

    def __init__(self, root: Path, final_text: str | None, exit_code: int = 0):
        self.path = root / "claude-stub"
        lines = [{"type": "system", "subtype": "init", "model": "stub"}]
        if final_text is not None:
            lines.append({"type": "result", "subtype": "success", "is_error": False,
                          "result": final_text, "num_turns": 1,
                          "usage": {"input_tokens": 1, "output_tokens": 1}})
        body = "\n".join(json.dumps(line) for line in lines)
        self.path.write_text(f"#!{sys.executable}\nimport sys\nprint({body!r})\n"
                             f"sys.exit({exit_code})\n", encoding="utf-8")
        self.path.chmod(0o755)


def _mirrorless():
    """The exposed items a run needs no source mirror for (they cite no mirror row). An
    item that cites FileMaker rows runs on a mirror with a run pin
    (ontology/tests/test_quality_packets.py); the CLI refuses it without one."""
    return [item for item in runner.load_items(EXPOSED, split="dev")
            if not runner._cites_rows(item)]


class ExposedRunTest(unittest.TestCase):
    """``python3 -m evals.runner run --items evals/exposed`` through ClaudeCodeAgent with a
    stub binary: labelled dev/exposed, outcomes with coverage beside accuracy, no mirror."""

    def setUp(self):
        self.items = _mirrorless()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "state").mkdir()  # no source mirror: exposed items cite no mirror row

    def run_cli(self, stub):
        out = io.StringIO()

        def host(*args, **kwargs):  # the host's state is the run's state directory
            fake = fx.FakeHost()
            fake.state_dir = self.root / "state"
            return fake
        with mock.patch.object(runner, "CaseHost", host), mock.patch("sys.stdout", out):
            code = runner.main(["run", "--items", str(EXPOSED), "--split", "dev",
                                "--state-dir", str(self.root / "state"),
                                "--source-mirror-audience", str(self.root / "a.json"),
                                "--out", str(self.root / "out"), "--claude-bin",
                                str(stub.path), "--evidence-label", "isolated-fixture",
                                *[arg for item in self.items for arg in ("--only", item["id"])]])
        return code, json.loads(out.getvalue())

    def hold_pinned_originals(self):
        """Store every pinned original the items cite, as the case host's store records it."""
        from operating.case_originals import OriginalStore
        from operating.case_runtime import CASE_RUNTIME_DIR
        store = OriginalStore(self.root / "state" / CASE_RUNTIME_DIR)
        pins = {cite["l1_handle"] for item in self.items
                for value in item["known_outcome"]["values"]
                for cite in value.get("citations") or []
                if score.PINNED_ORIGINAL.match(cite["l1_handle"])}
        with store._connect() as db:
            for index, pin in enumerate(sorted(pins)):
                match = score.PINNED_ORIGINAL.match(pin)
                db.execute("INSERT INTO original VALUES (?, ?, 'plx', 'internal', '[]', 'f', "
                           "'application/pdf', 1, 'test', '2026-10-01', '{}')",
                           (f"orig-{index:032x}", match.group(1) or "0" * 64))
                if not match.group(1):
                    db.execute("INSERT INTO original_source VALUES (?, ?)", (
                        f"orig-{index:032x}", json.dumps({
                            "source": "sharepoint-drive",
                            **dict(zip(("drive_id", "item_id", "etag"), (
                                unquote(match.group(n)) for n in (2, 3, 4))))})))

    def test_an_abstention_over_originals_the_state_lacks_is_blocked_not_wrong(self):
        abstain = fx.final_text({"format": score.ANSWER_FORMAT, "answerability": "blocked",
                                 "text": "I cannot tell.", "values": [], "gaps": []})
        code, summary = self.run_cli(StubClaude(self.root, abstain))
        self.assertEqual(code, 0)
        outcomes = summary["outcomes"]
        cite_pins = sum(any(score.PINNED_ORIGINAL.match(cite["l1_handle"])
                            for value in item["known_outcome"]["values"]
                            for cite in value.get("citations") or [])
                        for item in self.items)
        self.assertGreater(cite_pins, 0)
        self.assertEqual((outcomes["blocked"], outcomes["wrong"], outcomes["answered"]),
                         (cite_pins, len(self.items) - cite_pins, 0))
        codes = {code for path in (self.root / "out").rglob("*.score.json")
                 for code in _read(path)["codes"]}
        self.assertIn("originals_not_held", codes)
        # The verifier records the gap as blocked in the capture tier (red after 24 h).
        self.assertEqual((summary["originals_not_held"], live_verify.blocked_parts(summary)),
                         (cite_pins, [{"part": "pinned_originals", "source": "case-originals",
                                       "cause": "capture_gap"}]))
        # A stated answer over the same empty state is still scored: never blocked.
        stated = fx.final_text({"format": score.ANSWER_FORMAT, "answerability": "answerable",
                                "text": "42.", "values": [{"name": "x", "value": "42",
                                                           "citations": []}], "gaps": []})
        shutil.rmtree(self.root / "out")
        _, summary = self.run_cli(StubClaude(self.root, stated))
        self.assertEqual(summary["outcomes"]["blocked"], 0)

    def test_an_agent_that_abstains_everywhere_has_no_coverage_and_no_accuracy(self):
        self.hold_pinned_originals()
        abstain = fx.final_text({"format": score.ANSWER_FORMAT, "answerability": "blocked",
                                 "text": "I cannot tell.", "values": [], "gaps": []})
        code, summary = self.run_cli(StubClaude(self.root, abstain))
        self.assertEqual(code, 0)
        self.assertEqual(summary["partition"], "dev/exposed")
        self.assertEqual(set(summary["aggregate"]), {"dev/exposed"})
        outcomes = summary["outcomes"]
        self.assertEqual([outcomes[key] for key in ("runs", "wrong", "unjustified_abstention",
                                                    "correct", "justified_abstention")],
                         [len(self.items)] * 3 + [0, 0])
        self.assertEqual((outcomes["coverage"], outcomes["accuracy"]), (0.0, None))
        transcripts = sorted((self.root / "out").rglob("*.transcript.json"))
        self.assertEqual(len(transcripts), len(self.items))  # k=1, one phrasing
        first = _read(transcripts[0])
        self.assertEqual((first["partition"], first["agent"]["model"]), ("exposed", "stub"))
        self.assertNotIn("run_pin", first)

    def test_a_failed_session_is_blocked_not_scored_as_an_answer(self):
        code, summary = self.run_cli(StubClaude(self.root, None, exit_code=1))
        self.assertEqual(code, 0)
        outcomes = summary["outcomes"]
        self.assertEqual((outcomes["blocked"], outcomes["answered"], outcomes["coverage"]),
                         (len(self.items), 0, 0.0))


# ------------------------------------------------------------------ injected Graph world

DRIVE2 = "b!drive-Ops456"
DOWNLOAD = "/sites/X/_layouts/15/download.aspx"


def _item(drive: str, item_id: str, name: str, content: bytes, *, quick_xor: str | None = None,
          etag: str = "\"{E1},1\"") -> dict:
    hashes = {"quickXorHash": quick_xor} if quick_xor else {}
    return {"id": item_id, "name": name, "eTag": etag, "cTag": "\"c:{E1},1\"", "size": len(content),
            "webUrl": f"https://{SP_HOST}/sites/X/Shared%20Documents/{name}",
            "lastModifiedDateTime": "2026-10-02T12:00:00Z",
            "parentReference": {"driveId": drive, "path": f"/drives/{drive}/root:/Cases"},
            "file": {"mimeType": "application/octet-stream", "hashes": hashes}}


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class World:
    """Four drive items over two drives, and one mailbox with two pinned messages."""

    def __init__(self) -> None:
        self.members = {"Example/a.pdf": b"%PDF synthetic member a",
                        "Example/b.msg": b"synthetic member b"}
        self.files = {
            (DRIVE, "ITEM-PDF"): (b"%PDF-1.7 synthetic original", None),
            (DRIVE2, "ITEM-TXT"): ("﻿{\"stage\": 1}".encode("utf-8"), None),
            (DRIVE, "ITEM-XOR"): (b"%PDF-1.7 metadata only", "QXH-synthetic="),
            (DRIVE2, "ITEM-ZIP"): (_zip(self.members), None)}
        self.sender = FakeSender({SP_HOST + DOWNLOAD: lambda url: (
            200, {}, self.content(parse_qs(urlsplit(url).query)["item"][0]))})
        for (drive, item_id), (data, quick) in self.files.items():
            self.sender.routes[f"/v1.0/drives/{drive}/items/{item_id}"] = \
                self._route(drive, item_id, data, quick)
        self.immutable = {"m1": "IMM-ONE", "m2": "IMM-TWO"}
        self.attachment = b"%PDF synthetic attachment"
        raw = [dict(message(self.immutable["m1"], "CK1"), receivedDateTime="2026-09-15T20:27:21Z",
                    hasAttachments=True),
               dict(message(self.immutable["m2"], "CK2"), receivedDateTime="2026-09-16T13:52:55Z"),
               dict(message("IMM-OTHER", "CK3"), receivedDateTime="2026-09-16T13:52:56Z")]
        self.sender.routes[MSG_PATH] = j(200, {"value": raw})
        for record in raw:
            self.sender.routes[f"{MSG_PATH}/{record['id']}"] = j(200, record)
        self.sender.routes[f"{MSG_PATH}/IMM-ONE/attachments"] = j(200, {"value": [
            {"id": "ATT-1", "name": "packing.xlsx", "size": 999, "isInline": False,
             "@odata.type": "#microsoft.graph.fileAttachment"}]})
        self.sender.routes[f"{MSG_PATH}/IMM-ONE/attachments/ATT-1"] = lambda url: j(200, {
            "@odata.type": "#microsoft.graph.fileAttachment", "id": "ATT-1",
            "name": "packing.xlsx", "size": 999,
            "contentBytes": base64.b64encode(self.attachment).decode()})

    def content(self, item_id: str) -> bytes:
        return next(data for (_, key), (data, _) in self.files.items() if key == item_id)

    def _route(self, drive: str, item_id: str, data: bytes, quick: str | None):
        def route(url: str):
            data_now = self.files[(drive, item_id)][0]
            body = _item(drive, item_id, f"{item_id}.bin", data_now, quick_xor=quick)
            if "downloadUrl" in url:
                body["@microsoft.graph.downloadUrl"] = (
                    f"https://{SP_HOST}{DOWNLOAD}?item={item_id}&tempauth=DL-do-not-leak")
            return j(200, body)
        return route

    def sources(self, root: Path) -> Path:
        pdf, txt, _, archive = (data for data, _ in self.files.values())
        data = {"schema": checker.SOURCES_SCHEMA, "drive_items": [
            {"source_id": "pdf", "drive_id": DRIVE, "item_id": "ITEM-PDF", "e_tag": "\"{E1},1\"",
             "sha256": sha256(pdf).hexdigest(), "sha256_basis": "bytes", "quick_xor_hash": None},
            {"source_id": "txt", "drive_id": DRIVE2, "item_id": "ITEM-TXT", "e_tag": "\"{E1},1\"",
             "sha256": sha256("{\"stage\": 1}".encode()).hexdigest(), "sha256_basis": "utf8_text",
             "quick_xor_hash": None},
            {"source_id": "xor", "drive_id": DRIVE, "item_id": "ITEM-XOR", "e_tag": "\"{E0},1\"",
             "sha256": None, "sha256_basis": None, "quick_xor_hash": "QXH-synthetic="},
            {"source_id": "zip", "drive_id": DRIVE2, "item_id": "ITEM-ZIP", "e_tag": "\"{E1},1\"",
             "sha256": sha256(archive).hexdigest(), "sha256_basis": "bytes",
             "quick_xor_hash": None}],
            "archive_members": [{"source_id": f"member-{name}", "archive": "zip", "member": name,
                                 "sha256": sha256(blob).hexdigest()}
                                for name, blob in self.members.items()],
            "mail_messages": [
                {"source_id": "m1", "message_id": "REST-ONE", "received_at": "2026-09-15T20:27:21Z"},
                {"source_id": "m2", "message_id": "REST-TWO", "received_at": "2026-09-16T13:52:55Z"}],
            "mail_attachments": [{"source_id": "a1", "message": "m1", "attachment_id": "REST-ATT",
                                  "name": "packing.xlsx", "bytes": len(self.attachment),
                                  "sha256": sha256(self.attachment).hexdigest()}]}
        path = root / "sources.json"
        path.write_text(json.dumps(data), "utf-8")
        return path


class ExposedSourcesCheckTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.world.sources(Path(self.tmp.name))
        self.env = dict(ENV, **{checker.MAILBOX_ENV: MAILBOX})

    def documents(self):
        return checker.documents(self.env, sender=self.world.sender, sources_path=self.path)

    def test_pinned_documents_verify_through_get_only_reads_and_print_no_identity(self):
        report = self.documents()
        self.assertTrue(report["ok"], report)
        self.assertEqual((report["items"], report["verified"], report["sha256_match"],
                          report["quick_xor_match"], report["members_match"]), (4, 4, 3, 1, 2))
        self.assertEqual(report["etag_current"], 3)  # the quickXor-only item moved eTag
        methods = {call[0] for call in self.world.sender.calls
                   if "login.microsoftonline.com" not in call[1]}
        self.assertEqual(methods, {"GET"})
        # The metadata-only item is never downloaded.
        self.assertFalse(any("item=ITEM-XOR" in call[1] for call in self.world.sender.calls))
        text = json.dumps(report)
        for secret in ("ITEM-", DRIVE, DRIVE2, "Example/", ".bin", "do-not-leak", SP_HOST):
            self.assertNotIn(secret, text)

    def test_a_changed_original_fails_the_check(self):
        key = (DRIVE2, "ITEM-ZIP")
        self.world.files[key] = (_zip({**self.world.members, "Example/a.pdf": b"edited"}), None)
        report = self.documents()
        self.assertFalse(report["ok"])
        self.assertEqual(report["rows"][3]["sha256_match"], False)
        self.assertEqual(report["members_match"], 0)  # a mismatched archive is never opened
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = checker.main(["documents", "--sources", str(self.path)], environ=self.env,
                                sender=self.world.sender)
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_mail_originals_located_by_received_time_and_attachment_bytes(self):
        report = checker.mail(self.env, sender=self.world.sender, sources_path=self.path)
        self.assertTrue(report["ok"], report)
        self.assertEqual((report["located"], report["attachments_match"],
                          report["id_matches_pinned"]), (2, 1, 0))
        self.world.attachment = b"%PDF a different attachment"
        changed = checker.mail(self.env, sender=self.world.sender, sources_path=self.path)
        self.assertFalse(changed["ok"])
        self.assertEqual(changed["rows"][0]["attachments"][0]["sha256_match"], False)
        text = json.dumps(report)
        for secret in ("IMM-", "REST-", "ATT-1", "packing.xlsx", MAILBOX):
            self.assertNotIn(secret, text)

    def test_mail_without_a_configured_mailbox_reads_nothing(self):
        env = {key: value for key, value in self.env.items() if key != checker.MAILBOX_ENV}
        report = checker.mail(env, sender=self.world.sender, sources_path=self.path)
        self.assertEqual((report["ok"], report["error"]), (False, "mailbox_not_configured"))
        self.assertEqual(self.world.sender.calls, [])

    def test_live_check_specs_are_allowlisted_and_mail_waits_for_its_mailbox(self):
        self.assertIn("scripts.eval_exposed_sources", live_verify.ALLOWED_MODULES)
        for spec_id, command in (("case.exposed_originals", "documents"),
                                 ("case.exposed_mail", "mail")):
            spec = json.loads((ROOT / "verify" / "live-checks" / f"{spec_id}.json")
                              .read_text(encoding="utf-8"))
            live_verify.validate_spec(spec)
            self.assertEqual(spec["command"][2:], ["scripts.eval_exposed_sources", command])
            # The mailbox is a human-only value: the mail check stays disabled until set.
            self.assertEqual("disabled" in spec, command == "mail", spec_id)
            if command == "mail":
                self.assertIn(checker.MAILBOX_ENV, spec["requires_env"])
                self.assertIn(checker.MAILBOX_ENV, spec["disabled"])


if __name__ == "__main__":
    unittest.main()
