"""LiveDocuments' searches read the texts they find (gap-04f9b49ebae9,
gap-f713876847b6, gap-fdd76b96f8f6).

Live c89b2bbd (eval.oracle_dev): the SharePoint search found 14 documents, read the text
of none and counted 1 unread; 13 hits were never tried and never counted (a file kind the
reader skipped, one over the size cap, one past the read budget), and a saved .msg/.eml
message, a kind capture reads, was never asked for. Here a search finds documents whose
names say nothing and whose text names a closed root: each yields a reference, and every
hit is counted read or unread by a typed cause.

Evidence label: ``injected-transport`` (fake mail and drive readers behind the real
``LiveDocuments``); nothing here is live, ``eval.oracle_dev`` stays live-unverified.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest import mock

from verify import live_verify
from evals.oracle import cross_sample as cs
from evals.oracle.fixture_world import build_live_shaped_world
from evals.oracle.generator import candidate_split, portal_candidates
from integrations.live_source_guard import SourceUnavailable
from integrations.tests.test_bc_purchase import BUYER
from scripts import eval_oracle_dev as dev

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "live-2099-01-02T03"


MIME = {"txt": "text/plain", "eml": "message/rfc822", "jpg": "image/jpeg",
        "pdf": "application/pdf"}


def eml(text: str) -> bytes:
    return ("From: buyer@example.com\r\nTo: plant@example.com\r\nSubject: Forwarded\r\n"
            f"Content-Type: text/plain\r\n\r\n{text}\r\n").encode()


class Mailbox:
    """A mailbox whose search matches message bodies (as Graph ``$search`` does); the
    subjects and previews name nothing. ``fail``: the ids whose body read is gone."""

    def __init__(self, bodies: dict[str, str], fail: frozenset[str] = frozenset()):
        self.bodies, self.fail, self.got = bodies, fail, []

    def directory_cache(self):
        import contextlib
        return contextlib.nullcontext()

    def resolve_folder(self, actor, mailbox, name):
        return "fid-" + name

    def message(self, ident: str) -> dict:
        return {"id": ident, "citation": f"graph-mail://box/{ident}@c", "change_key": "ck",
                "subject": "Weekly update", "body_preview": "", "parent_folder_id": "fid-in"}

    def list_messages(self, actor, mailbox, *, folder=None, search=None, max_items=100,
                      **kwargs):
        found = [self.message(i) for i, body in sorted(self.bodies.items())
                 if search and search in body]
        return {"items": found[:max_items],
                "completeness": {"truncated": len(found) > max_items, "withheld_count": 0}}

    def get_message(self, actor, mailbox, ident, *, include_body, expected_folder_id=None):
        self.got.append(ident)
        if ident in self.fail:
            raise SourceUnavailable("gone")
        return {"change_key": "ck", "body_text": self.bodies[ident]}


class Drive:
    """A drive whose search matches file text (Graph indexes it); the names name nothing.
    ``files``: id -> (name, text, content bytes or None when the read fails)."""

    def __init__(self, files: dict[str, tuple[str, str, bytes | None]], tick=None):
        self.files, self.tick, self.read = files, tick, []

    def entry(self, ident: str) -> dict:
        return {"kind": "file", "id": ident, "name": self.files[ident][0], "size": 99,
                "mime_type": MIME.get(self.files[ident][0].rsplit(".", 1)[-1],
                                      "application/octet-stream"),
                "citation": f"sharepoint://d/{ident}@e"}

    def search(self, actor, query, *, max_items):
        if self.tick is not None:
            self.tick()
        found = [self.entry(i) for i, (_, text, _) in sorted(self.files.items())
                 if query in text]
        return {"items": found[:max_items], "completeness": {"truncated": False}}

    def get_content(self, actor, ident, *, max_bytes):
        self.read.append(ident)
        content = self.files[ident][2]
        if content is None:
            raise SourceUnavailable("changed")
        return {"content": content, "content_status": "read"}


def accounted(search: dict) -> int:
    return search.get("texts_read", 0) + search.get("texts_unread", 0)


def causes(search: dict) -> dict[str, int]:
    return {k[len("texts_unread_"):]: v for k, v in search.items()
            if k.startswith("texts_unread_")}


class DocumentTextReadTest(unittest.TestCase):
    def setUp(self):
        world = build_live_shaped_world(documents=False)
        self.sources, _, self.configured, _ = dev.build_fixture(world)
        closed = [c for c in portal_candidates(self.sources, "sales_order")
                  if c.family == "F1" and candidate_split(c) == "dev"][:2]
        self.assertEqual(len(closed), 2)
        self.one, self.two = (c.identifier for c in closed)

    def mailbox(self) -> Mailbox:
        return Mailbox({"m-a": f"Hello,\nplease ship order {self.one} today.\nThanks",
                        "m-b": f"Order {self.two} was delivered."},
                       fail=frozenset({"m-b"}))

    def drive(self, tick=None) -> Drive:
        return Drive({
            "slip": ("packing-slip.txt", f"Packing slip\nOrder {self.one}\nQty 12",
                     f"Packing slip\nOrder {self.one}\nQty 12".encode()),
            "fwd": ("forwarded.eml", f"Customer order {self.two}",
                    eml(f"Customer order {self.two} is ready to ship.")),
            "photo": ("pallet.jpg", f"Order {self.one}", b"\xff\xd8\xff"),
            "gone": ("notes.txt", f"Order {self.two}", None)}, tick)

    def docs(self, mail=None, drive=None) -> dev.LiveDocuments:
        kwargs = {}
        if mail is not None:
            kwargs.update(mail_reader=mail, mailbox="box")
        if drive is not None:
            kwargs.update(drive_reader=drive, drive_paths=["/Documents"])
        docs = dev.LiveDocuments(BUYER, **kwargs)
        docs.max_roots = 10_000   # every root searched: the two closed ones among them
        return docs

    def test_text_naming_a_root_yields_a_reference_and_every_hit_is_counted(self):
        mail, drive = self.mailbox(), self.drive()
        kinds = cs.sample(self.sources, run_id=RUN_ID, documents=self.docs(mail, drive),
                          now=dev.FIXTURE_NOW).kinds
        found = kinds["mail"]
        self.assertEqual(found["status"], "read", found)
        self.assertGreaterEqual(found["references"], 1, found)
        search = found["search"]
        self.assertGreaterEqual(search["documents_naming_one"], 1, search)
        self.assertEqual(search["texts_read"], 1, search)
        self.assertEqual(causes(search), {"unavailable": 1}, search)   # never a silent 0
        self.assertEqual(accounted(search), 2)
        self.assertEqual(sorted(mail.got), ["m-a", "m-b"])   # each body read once a run

        shared = kinds["sharepoint"]
        self.assertEqual(shared["status"], "read", shared)
        self.assertGreaterEqual(shared["references"], 1, shared)
        search = shared["search"]
        # the .txt and the saved .eml message: their text names the root, not their name
        self.assertEqual(search["documents_naming_one"], 2, search)
        self.assertEqual(search["texts_read"], 2, search)
        self.assertEqual(causes(search), {"not_text_kind": 1, "unavailable": 1}, search)
        self.assertEqual(accounted(search), len(drive.files))   # every hit, once
        self.assertEqual(sorted(drive.read), ["fwd", "gone", "slip"])   # never the .jpg
        self.assertNotIn(self.one, json.dumps(kinds))
        self.assertNotIn(self.two, json.dumps(kinds))

    def test_the_dev_set_strata_publish_the_reads_and_meet_the_live_check(self):
        docs = self.docs(self.mailbox(), self.drive())
        report, _ = dev.execute(self.sources, docs, run_id=RUN_ID, now=dev.FIXTURE_NOW,
                                count=30, audit=10, id_suffix="live", evidence_note=None,
                                evidence_label="injected-transport",
                                configured=self.configured)
        for kind, read in (("mail", 1), ("sharepoint", 2)):
            stratum = report["strata"][kind]
            self.assertGreaterEqual(stratum["candidates"], 1, stratum)
            self.assertEqual(stratum["funnel"]["texts_read"], read, stratum)
            self.assertGreaterEqual(stratum["funnel"]["texts_unread_unavailable"], 1)
        live = {**report, "evidence_label": "live-read"}
        spec = json.loads((ROOT / "verify" / "live-checks" / "eval.oracle_dev.json")
                          .read_text(encoding="utf-8"))
        problems = live_verify.check_expectations(spec, dev.exit_code(live), live)
        for kind in ("mail", "sharepoint"):
            self.assertNotIn(f"strata.{kind}.candidates < 1", problems)
        self.assertNotIn(self.one, json.dumps(report))

    def test_a_search_again_keeps_the_texts_its_first_read(self):
        """A document kind its slice cut is searched again; the port reads each hit's text
        once a run, so the second search reads none: the first one's counts stay."""
        bc = cs._sorted_roots(cs.bc_roots(self.sources, 50, {}))
        self.assertTrue(bc)
        number = cs._candidate(bc[0]).identifier
        now = [0.0]
        searched = []

        def tick():   # the first SharePoint search outlasts its slice (at most 1/8)
            if not searched:
                now[0] += 50_000.0
            searched.append(now[0])

        drive = Drive({"inv": ("statement.txt", f"Invoice {number}",
                               f"Statement\nInvoice {number}\nTotal".encode())}, tick)
        clock = cs.KindClock(lambda: 100_000.0 - now[0], lambda: now[0])
        kinds = cs.sample(self.sources, run_id=RUN_ID, documents=self.docs(drive=drive),
                          now=dev.FIXTURE_NOW, clock=clock).kinds
        shared = kinds["sharepoint"]
        # searched again (more searches than one pass sends), and the second search, its
        # slice not spent, verified the BC root the first left unchecked (kind_budget)
        one_pass = shared["search"]["searches"] + shared["search"]["fallback_searches"]
        self.assertEqual(len(searched), 2 * one_pass, shared)
        self.assertEqual((shared["status"], shared["references"]), ("read", 1), shared)
        self.assertEqual(drive.read, ["inv"])           # its text read once
        self.assertEqual(shared["search"]["texts_read"], 1, shared)
        self.assertEqual(accounted(shared["search"]), 1, shared)

    def test_every_hit_is_counted_at_any_cardinality(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                drive = Drive({f"f{i:02}": (f"doc{i}.txt", "ORD-1", b"ORD-1 shipped")
                               for i in range(n)})
                found = self.docs(drive=drive).search("sharepoint", ["ORD-1"], hits=100,
                                                      seed="s")
                tried = min(n, dev.MAX_SEARCH_TEXTS)
                self.assertEqual(len(found["records"]), n)
                self.assertEqual((found["texts_read"], found["texts_unread"]), (tried, n - tried))
                self.assertEqual(found["texts_unread_by"],
                                 {"text_budget": n - tried} if n > tried else {})
                self.assertEqual(sorted(drive.read), sorted(set(drive.read)))
                self.assertEqual(len(drive.read), tried)
                box = Mailbox({f"m{i:02}": "ORD-1 shipped" for i in range(n)})
                mail = self.docs(mail=box).search("mail", ["ORD-1"], hits=100, seed="s")
                self.assertEqual(len(mail["records"]), n)
                self.assertEqual((mail["texts_read"], mail["texts_unread"]), (tried, n - tried))
                self.assertEqual(len(box.got), tried)

    def test_an_unexpected_hit_is_counted_unread_never_read(self):
        drive = Drive({"big": ("archive.pdf", "ORD-1", b"%PDF"),
                       "noid": ("x.txt", "ORD-1", b"ORD-1"),
                       "bare": ("README", "ORD-1", b"ORD-1")})
        entry = drive.entry

        def odd(ident):
            got = entry(ident)
            if ident == "big":
                got["size"] = dev.MAX_TEXT_BYTES + 1
            if ident == "noid":
                got["id"] = 7
            return got
        drive.entry = odd
        found = self.docs(drive=drive).search("sharepoint", ["ORD-1"], hits=10, seed="s")
        self.assertEqual(found["texts_read"], 0)
        self.assertEqual(found["texts_unread_by"], {"exceeds_cap": 1, "not_text_kind": 2})
        self.assertEqual(drive.read, [])
        box = Mailbox({"m1": "ORD-1"})
        listed = box.message

        def no_id(ident):
            got = listed(ident)
            del got["id"]
            return got
        box.message = no_id
        mail = self.docs(mail=box).search("mail", ["ORD-1"], hits=10, seed="s")
        self.assertEqual((mail["texts_read"], mail["texts_unread_by"]), (0, {"failed": 1}))
        self.assertEqual(box.got, [])
        with mock.patch.object(dev, "MAX_SEARCH_TEXTS", 0):
            none = self.docs(mail=Mailbox({"m1": "ORD-1"})).search("mail", ["ORD-1"],
                                                                   hits=10, seed="s")
        self.assertEqual(none["texts_unread_by"], {"text_budget": 1})


if __name__ == "__main__":
    unittest.main()
