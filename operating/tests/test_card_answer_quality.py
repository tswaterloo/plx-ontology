"""What the case-card scoreboard found in the employee's MCP session, at its boundary.

An MCP ``tools/call`` on ``MCPServer`` over ``CaptureCaseHostTools`` and a versioned
SharePoint capture (the capture tests' in-process FakeDrive, as
``test_case_host_sharepoint``):

* the final-answer gate (``plx.case_get`` answer mode) checks a passage quoted from a
  captured file's text against the text ``plx.read`` served that reader, so a verbatim
  passage passes and anything else stays refused; a reader the file is withheld from
  gets no claim through;
* a repair outcome says the fixed answer may still be submitted (``repairs_left`` 1), and a
  final decision that refused claims says to withhold them;
* ``plx.documents`` finds a file by words of its captured text, says when no file holds
  every word, and reports ``audience_not_granted`` only for a reader who truly lacks
  captured units, with how many.

Evidence label: ``isolated-fixture``. No network, no credentials.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import unittest

from integrations.tests.test_capture_sharepoint import (EMPLOYEE, OUTSIDER, QUALITY,
                                                        TEXT_SECRET)
from ontology import mentions
from ontology import schema as schema_loader
from ontology.project import SharePointCaptures, build_store
from ontology.tests.test_mail_documents import StaticPortal
from ontology.text_index import store_text_index
from operating.answer_gate import (ANSWER_BACKING, ANSWER_FINAL, ANSWER_NOT_BLOCKED,
                                   STATED_GAP, WITHHOLD_REFUSED)
from operating.tests.test_case_host_sharepoint import NOW, SharePointHostCase
from operating.tests.test_case_tools_host import _client

RELEASE = "01FILEB"   # release.txt: TEXT_SECRET, item permissions Ricardo and sales


def _answer(handle: str, value: str, text: str = "The release note says so.") -> dict:
    return {"answer": {"claims": [{"text": text, "citations": [handle], "value_claims": [
        {"value": value, "citation": handle, "field": "row.content_text"}]}], "text": text}}


class CapturedTextClaimTests(SharePointHostCase):
    scanned = False

    def submit(self, server, arguments: dict) -> dict:
        result = _client(server, "plx.case_get", arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_a_passage_of_a_served_files_text_passes_the_gate_and_nothing_else_does(self):
        handle = self.handle(RELEASE)
        server = self.server(EMPLOYEE)
        read = self.ok(server, "plx.read", {"citation": handle})
        self.assertEqual(read["content"]["untrusted_text"], TEXT_SECRET)
        passed = self.submit(server, _answer(handle, "Lot release for PO100234"))
        self.assertEqual(passed["outcome"], "answered", json.dumps(passed)[:2000])
        self.assertEqual(len(passed["claims"]), 1)
        # Final: the answer is what was checked; nothing was refused, so nothing to withhold;
        # a passed claim and no unknown: answerable, never blocked.
        self.assertEqual(passed["answerability"]["state"], "answerable")
        self.assertEqual(passed["instruction"]["untrusted_text"],
                         f"{ANSWER_FINAL} {ANSWER_NOT_BLOCKED}")
        # Each passed claim comes ready to copy as the final reply keeps it: the passage
        # cited on the read field, nothing the gate did not check.
        (ready,) = passed["answer_values"]
        self.assertEqual(ready["answer_value"]["value"]["untrusted_text"],
                         "Lot release for PO100234")
        self.assertEqual(ready["answer_value"]["citations"],
                         [{"l1_handle": handle, "field_path": "row.content_text"}])
        self.assertEqual(set(ready["answer_value"]), {"value", "citations"})
        # A paraphrase is not in the text: refused, and the repair says it may be sent again.
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        refused = self.submit(server, _answer(handle, "Lot released for PO100234"))
        self.assertEqual((refused["outcome"], refused["repairs_left"]), ("repair", 1))
        self.assertNotIn("instruction", refused)
        self.assertIn("submit the fixed answer once more",
                      refused["repair"]["instruction"]["untrusted_text"])
        (failure,) = [f for c in refused["unsupported_claims"] for f in c["failures"]]
        self.assertEqual(failure["code"], "claim_value_mismatch")
        # Refused again: final, no repair left, and the refused claim is to be withheld.
        final = self.submit(server, _answer(handle, "Lot released for PO100234"))
        self.assertEqual((final["outcome"], final["repairs_left"]), ("abstained", 0))
        self.assertEqual(final["answerability"]["state"], "blocked")
        self.assertEqual(final["instruction"]["untrusted_text"],
                         f"{ANSWER_FINAL} {WITHHOLD_REFUSED}")
        self.assertNotIn("answerability", refused)   # a repair is not a final decision

    def test_a_document_stating_the_gap_is_a_partial_answer_never_blocked(self):
        # Case-card run (SP-05 and eight more cards): the agent read a document that states
        # the gap asked about, paraphrased it with no value claim, was refused, then
        # abstained, or had the passage passed and still answered blocked. The repair
        # says a stated gap is cited as a text value and the answer is then partial; the
        # final decision of a passed passage beside an unknown says partial, not blocked.
        handle = self.handle(RELEASE)
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        refused = self.submit(server, {"answer": {
            "text": "The release note does not record the screener.",
            "unknowns": ["not in the record: the screener"],
            "claims": [{"text": "The release note records no screener.",
                        "citations": [handle]}]}})
        self.assertEqual(refused["outcome"], "repair")
        said = refused["repair"]["instruction"]["untrusted_text"]
        self.assertIn(STATED_GAP, said)
        self.assertIn(ANSWER_BACKING, said)   # the answer quoted from the text, not metadata
        self.assertIn("Abstain only if nothing the tools returned bears on the question",
                      said)
        self.assertNotIn("abstain if nothing the tools returned answers any part", said)
        fixed = _answer(handle, "Lot release for PO100234")
        fixed["answer"]["unknowns"] = ["not in the record: the screener"]
        final = self.submit(server, fixed)
        self.assertEqual(final["outcome"], "answered", json.dumps(final)[:2000])
        self.assertEqual(final["answerability"]["state"], "partial")
        self.assertIn(ANSWER_NOT_BLOCKED, final["instruction"]["untrusted_text"])
        # One passage passed beside a refused one: still partial, the refused withheld.
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        mixed = _answer(handle, "Lot release for PO100234")
        mixed["answer"]["claims"].append({"text": "It names the screener.", "citations": [
            handle], "value_claims": [{"value": "Screener Acme", "citation": handle,
                                        "field": "row.content_text"}]})
        final = self.submit(server, mixed)
        self.assertEqual((final["outcome"], final["answerability"]["state"]),
                         ("answered", "partial"))
        self.assertEqual(final["instruction"]["untrusted_text"],
                         f"{ANSWER_FINAL} {ANSWER_NOT_BLOCKED} {WITHHOLD_REFUSED}")

    def test_a_field_plx_read_served_is_checked_as_served(self):
        # The file's name as plx.read served it (row.name) is a claim the gate checks
        # exactly: the captured view a case digests carries no row.
        handle = self.handle(RELEASE)
        server = self.server(EMPLOYEE)
        read = self.ok(server, "plx.read", {"citation": handle})
        self.assertIn("row.name", read["fields"])
        answer = _answer(handle, "release.txt", "The file is release.txt.")
        answer["answer"]["claims"][0]["value_claims"][0]["field"] = "row.name"
        passed = self.submit(server, answer)
        self.assertEqual(passed["outcome"], "answered", json.dumps(passed)[:2000])
        answer["answer"]["claims"][0]["value_claims"][0]["value"] = "release.pdf"
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        self.assertEqual(self.submit(server, answer)["claims"], [])

    def test_a_repair_says_how_to_fix_each_reason_it_refused(self):
        # Case-card run: claims citing a read record with no value claim, or a handle
        # seen only in a search hit, were refused and the agent abstained instead of
        # fixing them. The repair names the fix for each reason it lists, and only those.
        from operating.answer_gate import REPAIR_HINTS
        handle = self.handle(RELEASE)
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        unseen = handle.replace("01FILEB", "01FILEZ")
        refused = self.submit(server, {"answer": {"text": "Two things.", "claims": [
            {"text": "The release note covers the lot.", "citations": [handle]},
            {"text": "Another file says so.", "citations": [unseen], "value_claims": [
                {"value": "x", "citation": unseen, "field": "row.content_text"}]}]}})
        self.assertEqual(refused["outcome"], "repair")
        said = refused["repair"]["instruction"]["untrusted_text"]
        self.assertEqual({c["reason"] for c in refused["unsupported_claims"]},
                         {"claim_states_no_value", "cites_what_this_run_did_not_return"})
        refused_for = {"claim_states_no_value", "cites_what_this_run_did_not_return"}
        for reason, hint in REPAIR_HINTS.items():
            # A hint for each reason it refused, none for a reason it did not.
            self.assertEqual(hint in said, reason in refused_for, reason)

    def test_a_reader_the_file_is_withheld_from_gets_no_claim_through(self):
        handle = self.handle(RELEASE)
        server = self.server(OUTSIDER)
        read = _client(server, "plx.read", {"citation": handle})["structuredContent"]
        self.assertNotEqual(read.get("status"), "resolved")
        refused = self.submit(server, _answer(handle, "Lot release for PO100234"))
        self.assertEqual(refused["claims"], [])
        self.assertNotIn(TEXT_SECRET, json.dumps(refused))
        self.assertNotIn("PO100234", json.dumps(refused["unsupported_claims"]).replace(
            "Lot release for PO100234", ""))
        # Its final decision: a withheld file's passage never makes the answer partial.
        final = self.submit(server, {"answer": {**_answer(handle, "Lot release for PO100234")[
            "answer"], "unknowns": ["not in the record: the screener"]}})
        self.assertEqual((final["outcome"], final["answerability"]["state"]),
                         ("abstained", "blocked"))
        self.assertNotIn(ANSWER_NOT_BLOCKED, final["instruction"]["untrusted_text"])
        self.assertNotIn(TEXT_SECRET, json.dumps(final))


MAP = "01FILEM"   # map.csv: a process map longer than one 200-character read
MAP_HEAD = ("stage,table,quantities,state\n"
            "Screening,\u2014 (no table),\"Screener job status, qty good / spoiled\",,gap\n")
MAP_TEXT = MAP_HEAD + "".join(f"Step {n},app_step,step qty,mapped\n" for n in range(12))
MAP_VALUE = "Screener job status, qty good / spoiled"
# Where the value ends in the map's text: a read from here on serves none of it.
AFTER_VALUE = MAP_TEXT.index(MAP_VALUE) + len(MAP_VALUE)


class QuotedValueAndTruncatedReadTests(SharePointHostCase):
    """v6 use-case scoreboard: a value claim with the ``quote`` the eval prompt invites was
    refused whole (``invalid_value_claim:0``) though its value was copied from the text
    plx.read served; and an answer citing a plx.read the session got truncated was still
    ``answerable``. Now a quote is checked as well as its value (a forged one refuses
    the claim), and a claim citing a text the session was served only part of makes the
    answer partial with an unknown naming the unread rest, per session."""
    scanned = False

    def seed(self):
        from integrations.tests.test_capture_sharepoint import RICARDO, SALES, seeded_drive
        drive = seeded_drive()
        drive.file(MAP, "map.csv", "01LOTS", MAP_TEXT.encode(), mime="text/plain",
                   acl=(RICARDO, SALES))
        return drive

    def submit(self, server, arguments: dict) -> dict:
        result = _client(server, "plx.case_get", arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def quoted(self, handle, quote, value=MAP_VALUE):
        answer = _answer(handle, value, "The process map records no table for it.")
        answer["answer"]["claims"][0]["value_claims"][0]["quote"] = quote
        return answer

    def test_a_quote_is_checked_with_its_value_and_a_forged_one_refuses_the_claim(self):
        handle = self.handle(MAP)
        server = self.server(EMPLOYEE)
        read = self.ok(server, "plx.read", {"citation": handle})
        self.assertEqual(read["content"]["untrusted_text"], MAP_TEXT)
        passed = self.submit(server, self.quoted(
            handle, '\u2014 (no table),"Screener job status, qty good / spoiled",,gap'))
        self.assertEqual(passed["outcome"], "answered", json.dumps(passed)[:2000])
        self.assertEqual(passed["answerability"]["state"], "answerable")
        for quote in ('Screening, (no table),"Screener job status, qty good / spoiled"',
                      "Screener job status, qty good",          # misses the value
                      "Screener job status, qty good / spoiled".upper()):
            with self.subTest(quote=quote):
                server = self.server(EMPLOYEE)
                self.ok(server, "plx.read", {"citation": handle})
                final = self.submit(server, {"answer": {
                    **self.quoted(handle, quote)["answer"], "unknowns": []}})
                self.assertEqual(final["claims"], [], json.dumps(final)[:2000])
                (failure,) = [f for c in final["unsupported_claims"]
                              for f in c["failures"]]
                self.assertEqual(failure["code"], "claim_value_mismatch")
        # A reader the file is withheld from: the same correct quote passes nothing.
        server = self.server(OUTSIDER)
        _client(server, "plx.read", {"citation": handle})
        refused = self.submit(server, self.quoted(
            handle, '\u2014 (no table),"Screener job status, qty good / spoiled",,gap'))
        self.assertEqual(refused["claims"], [])
        self.assertNotIn("app_step", json.dumps(refused))

    def test_a_claim_on_a_truncated_read_is_partial_until_its_passage_is_read(self):
        from operating.answer_gate import UNREAD_PART
        handle = self.handle(MAP)
        unread = UNREAD_PART.format(field="row.content_text")
        # F22-0006: a truncated read that served the whole passage the claim cites keeps
        # the claim answerable; the unread rest of the text is not this claim's.
        server = self.server(EMPLOYEE)
        read = self.ok(server, "plx.read", {"citation": handle, "max_chars": 200})
        self.assertTrue(read["content"]["truncated"])
        self.assertIn(MAP_VALUE, read["content"]["untrusted_text"])
        self.assertTrue(server.unread(handle, self.FIELD))
        held = self.submit(server, _answer(handle, MAP_VALUE, "The map says so."))
        self.assertEqual((held["outcome"], held["answerability"]["state"]),
                         ("answered", "answerable"), json.dumps(held)[:2000])
        self.assertEqual(held["unknowns"], [])
        # A read that served the text past the value only: the claim cites what was not
        # served, so the answer is partial with the unknown naming the unread rest.
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle, "text_offset": AFTER_VALUE})
        partial = self.submit(server, _answer(handle, MAP_VALUE, "The map says so."))
        self.assertEqual((partial["outcome"], partial["answerability"]["state"]),
                         ("answered", "partial"), json.dumps(partial)[:2000])
        self.assertEqual([u["untrusted_text"] for u in partial["unknowns"]], [unread])
        # The final reply keeps the unknown that names the unread part.
        self.assertEqual([u["untrusted_text"] for u in partial["reply"]["unknowns"]],
                         [unread])
        # A read that starts inside the value served part of it only: still partial.
        start = MAP_TEXT.index(MAP_VALUE)
        cut = self.server(EMPLOYEE)
        self.ok(cut, "plx.read", {"citation": handle, "text_offset": start + 5})
        self.assertEqual(self.submit(cut, _answer(handle, MAP_VALUE, "The map says so."))[
            "answerability"]["state"], "partial")
        # Another session that read the whole text: answerable (per session).
        other = self.server(EMPLOYEE)
        self.ok(other, "plx.read", {"citation": handle})
        whole = self.submit(other, _answer(handle, MAP_VALUE, "The map says so."))
        self.assertEqual(whole["answerability"]["state"], "answerable")
        self.assertEqual(whole["unknowns"], [])
        # The partial session reads the part holding the passage: answerable.
        self.ok(server, "plx.read", {"citation": handle, "max_chars": 200})
        done = self.submit(server, _answer(handle, MAP_VALUE, "The map says so."))
        self.assertEqual(done["answerability"]["state"], "answerable",
                         json.dumps(done)[:2000])

    def test_a_served_part_the_text_no_longer_holds_reads_nothing_in_full(self):
        from unittest import mock
        handle = self.handle(MAP)
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle, "max_chars": 200})
        parts = server.served_parts(handle, self.FIELD)
        self.assertEqual([(start, end) for start, end, _sha in parts], [(0, 200)])
        # The characters served there differ from the text the gate re-reads now (a
        # forged or stale hash, another place): not read in full, the answer is partial.
        for forged in ([(0, 200, "0" * 64)], [(1, 201, parts[0][2])], [(0, 200)], None):
            with self.subTest(parts=forged), \
                    mock.patch.object(type(server), "served_parts", return_value=forged):
                final = self.submit(server, _answer(handle, MAP_VALUE, "The map says so."))
                self.assertEqual(final["answerability"]["state"], "partial")
        # Two parts that meet inside the passage hold it together: read in full.
        split = MAP_TEXT.index(MAP_VALUE) + 5

        def sha(start, end):
            return hashlib.sha256(MAP_TEXT[start:end].encode()).hexdigest()
        joined = [(split, 200, sha(split, 200)), (0, split, sha(0, split))]
        # Many served parts (1, 10, 60) that do not meet: none holds the passage alone;
        # the passage's own part among them reads it in full.
        apart = [(at, at + 4, sha(at, at + 4)) for at in range(0, 300, 5)]
        for given, state in ((joined, "answerable"), (joined[:1], "partial"),
                             (apart[:1], "partial"), (apart[:10], "partial"),
                             (apart[:60], "partial"),
                             (apart[:59] + [(0, 200, sha(0, 200))], "answerable")):
            with self.subTest(parts=len(given)), \
                    mock.patch.object(type(server), "served_parts", return_value=given):
                final = self.submit(server, _answer(handle, MAP_VALUE, "The map says so."))
                self.assertEqual(final["answerability"]["state"], state)

    FIELD = "row.content_text"

    def alias(self, server, handle):
        """The byte-pinned citation of the same file version (``#sha256=``): another
        handle of the one record the claim gate's re-read resolves to the same file."""
        view = server.case_runtime.claim_row(handle, server.actor)
        return f"{handle}#sha256={view['content_sha256']}"

    def test_aliases_of_one_text_share_what_the_session_was_served_of_it(self):
        from operating.answer_gate import UNREAD_PART
        handle = self.handle(MAP)
        server = self.server(EMPLOYEE)
        alias = self.alias(server, handle)
        read = self.ok(server, "plx.read", {"citation": alias, "text_offset": AFTER_VALUE})
        self.assertIsNone(read["text_window"]["next_offset"])
        # The record's other handle names the same partly served text.
        self.assertTrue(server.unread(handle, self.FIELD))
        self.assertFalse(server.unread(handle, "row.name"))
        partial = self.submit(server, _answer(alias, MAP_VALUE, "The map says so."))
        self.assertEqual(partial["answerability"]["state"], "partial",
                         json.dumps(partial)[:2000])
        self.assertEqual([u["untrusted_text"] for u in partial["unknowns"]],
                         [UNREAD_PART.format(field=self.FIELD)])
        # The rest read under the other handle covers the text whole for both.
        self.ok(server, "plx.read", {"citation": handle, "max_chars": 200})
        self.assertFalse(server.unread(alias, self.FIELD))
        done = self.submit(server, _answer(alias, MAP_VALUE, "The map says so."))
        self.assertEqual(done["answerability"]["state"], "answerable",
                         json.dumps(done)[:2000])

    def test_a_permission_change_on_the_file_drops_what_was_served_of_its_text(self):
        from integrations.tests.test_capture_sharepoint import DRIVE, RICARDO, SALES, scope
        handle = self.handle(MAP)
        server = self.server(EMPLOYEE)
        read = self.ok(server, "plx.read", {"citation": handle, "max_chars": 200})
        served = len(read["content"]["untrusted_text"])
        self.assertTrue(server.unread(handle, self.FIELD))
        # The file's item permissions change (the reader keeps it): what the session was
        # served of it under the earlier permissions no longer counts.
        self.drive.items[f"{DRIVE}:{MAP}"]["acl"] = [RICARDO, SALES, "qa@example-plx.com"]
        self.assertTrue(self.capture.run(scope())["complete"])
        rest = self.ok(server, "plx.read", {"citation": handle, "text_offset": served})
        self.assertIsNone(rest["text_window"]["next_offset"])
        self.assertTrue(server.unread(handle, self.FIELD))
        final = self.submit(server, _answer(handle, MAP_VALUE, "The map says so."))
        self.assertEqual(final["answerability"]["state"], "partial",
                         json.dumps(final)[:2000])
        # Read whole under the permissions in force now: answerable.
        self.ok(server, "plx.read", {"citation": handle})
        self.assertFalse(server.unread(handle, self.FIELD))

    def test_many_partly_read_texts_are_one_unknown_within_the_answer_bounds(self):
        from unittest import mock
        from operating import answer_gate
        handle = self.handle(MAP)
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle, "max_chars": 200})
        fields = [f"row.fields.{word}.value" for word in (
            "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
            "november oscar papa quebec romeo sierra tango uniform victor whiskey").split()]
        answer = _answer(handle, MAP_VALUE, "The map says so.")
        answer["answer"]["unknowns"] = [f"not in the record: the {word} stage" for word in (
            "cutting folding gluing packing sealing labelling stacking wrapping loading "
            "sorting printing coating drying curing pressing rolling trimming boxing "
            "checking shipping").split()]
        for partly in ([self.FIELD], fields):
            with self.subTest(fields=len(partly)), \
                    mock.patch.object(answer_gate, "_partly_read", return_value=partly):
                final = self.submit(server, answer)   # _client checks the output contract
                unknowns = [u["untrusted_text"] for u in final["unknowns"]]
                self.assertEqual(final["answerability"]["state"], "partial")
                self.assertEqual(len(unknowns), answer_gate.MAX_UNKNOWNS)
                self.assertTrue(all(len(u) <= answer_gate.MAX_UNKNOWN_CHARS
                                    for u in unknowns))
                said = [u for u in unknowns if u.startswith("not read:")]
                self.assertEqual(said, [answer_gate.unread_unknown(partly)])
                # the reply screen keeps it: no digit no claim states
                self.assertIn(said[0], [u["untrusted_text"]
                                        for u in final["reply"]["unknowns"]])
        self.assertTrue(answer_gate.unread_unknown(fields).startswith(
            "not read: the rest of more than nine cited texts"))
        self.assertTrue(answer_gate.unread_unknown(fields[:3]).startswith(
            "not read: the rest of three cited texts"))
        self.assertIn("and others", answer_gate.unread_unknown(fields))

    def test_the_console_ask_loop_marks_a_claim_on_a_truncated_read_partial(self):
        from operating import agent_loop
        from operating.answer_gate import UNREAD_PART
        handle = self.handle(MAP)

        def planner(read):
            def plan(request):
                if not request["transcript"]:
                    return {"action": "call", "calls": [
                        {"tool": "plx.read", "arguments": {"citation": handle, **read}}]}, None
                return {"action": "answer", "text": "The map records {c0}.", "claims": [
                    {"id": "c0", "value": MAP_VALUE, "citation": handle,
                     "field": self.FIELD}]}, None
            return plan

        cut = agent_loop.run("What does the process map record for screening?",
                             self.server(EMPLOYEE), planner({"text_offset": AFTER_VALUE}))
        self.assertEqual(cut["outcome"], "answered", cut["reason"])
        self.assertEqual(cut["unknowns"], [UNREAD_PART.format(field=self.FIELD)])
        for read in ({}, {"max_chars": 200}):   # whole, or a part holding the passage
            whole = agent_loop.run("What does the process map record for screening?",
                                   self.server(EMPLOYEE), planner(read))
            self.assertEqual(whole["outcome"], "answered", whole["reason"])
            self.assertEqual(whole["unknowns"], [])


PROBE = "01FILEP"   # probe.txt: a figure cut by a window's edge, its twin served nowhere
PROBE_HEAD = ("Lot record. " * 40)[:186] + "Qty shipped 150 units on the lot.\n"
PROBE_TEXT = (PROBE_HEAD + "".join(f"Line {n}: routine note, nothing to report here.\n"
                                   for n in range(10))
              + "Refund 15 dollars.\nShipment released to quarantine hold pending review.\n")


class CutWindowTests(SharePointHostCase):
    """Review of the partly-read scope: a served window's edge is no token boundary. Its
    first 200 characters end ``Qty shipped 15`` of ``150``, the unserved rest holds
    ``Refund 15``: a bare 15 matched only outside what was served stays partial; a value
    touching an edge inside the text (cut, or going on past it) was not read in full."""
    scanned = False

    def seed(self):
        from integrations.tests.test_capture_sharepoint import RICARDO, SALES, seeded_drive
        drive = seeded_drive()
        drive.file(PROBE, "probe.txt", "01LOTS", PROBE_TEXT.encode(), mime="text/plain",
                   acl=(RICARDO, SALES))
        return drive

    def decide(self, read: dict, value: str, quote: str | None = None) -> dict:
        handle = self.handle(PROBE)
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle, **read})
        answer = _answer(handle, value, "The record says so.")
        if quote is not None:
            answer["answer"]["claims"][0]["value_claims"][0]["quote"] = quote
        result = _client(server, "plx.case_get", answer)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_only_a_whole_occurrence_inside_what_was_served_reads_in_full(self):
        from operating.answer_gate import UNREAD_PART
        self.assertTrue(PROBE_TEXT[:200].endswith("Qty shipped 15"))
        shipment = PROBE_TEXT.index("Shipment") + len("Shipment released")
        refund = PROBE_TEXT.index("Refund")
        for read, value, quote, state in (
                ({"max_chars": 200}, "15", None, "partial"),
                ({"text_offset": shipment - 200, "max_chars": 200}, "Shipment released",
                 None, "partial"),
                ({"text_offset": refund - 100, "max_chars": 200}, "15",
                 "Refund 15 dollars", "answerable"),
                ({"text_offset": refund - 100, "max_chars": 200}, "15", None, "answerable"),
                ({}, "15", None, "answerable")):
            with self.subTest(read=read, value=value, quote=quote):
                decided = self.decide(read, value, quote)
                self.assertEqual((decided["outcome"], decided["answerability"]["state"]),
                                 ("answered", state), json.dumps(decided)[:2000])
                self.assertEqual([u["untrusted_text"] for u in decided["unknowns"]],
                                 [UNREAD_PART.format(field="row.content_text")]
                                 if state == "partial" else [])
        # The cut figure quoted as served is no figure of the full text: refused.
        for read in ({"max_chars": 200}, {}):
            with self.subTest(quoted_cut=read):
                refused = self.decide(read, "15", "Qty shipped 15")
                self.assertEqual(refused["outcome"], "repair")
                self.assertEqual([f["code"] for c in refused["unsupported_claims"]
                                  for f in c["failures"]], ["claim_value_mismatch"])


STATEMENT = "01FILES"   # statement.md: one figure written twice with two meanings
STATEMENT_ROW = "| Total open, all 1-30 days |  | 9,900.00 |"
STATEMENT_TEXT = ("| Until it runs out | $9,900.00 | $0.00 |\n"
                  "| Proportional at 15% | $1,485.00 | $8,415.00 |\n" + STATEMENT_ROW + "\n"
                  "| Less payment |  | 0.00 |\n| Deposit percent | 15 | 15 visible lines |\n"
                  "| Final | 68,160 |\nHandling 40.00 CAD.\n")
# Each figure the text writes twice with two meanings, and the passage that pins one.
AMBIGUOUS = (("9900.00", STATEMENT_ROW), ("0.00", "| Less payment |  | 0.00 |"),
             ("15", "| Deposit percent | 15 |"))


class GateScorerAgreementTests(SharePointHostCase):
    """v7 scoreboard (RRO pages 8 and 10, F21-0004/5/6/8, F22-0005, F22-0008): the gate and
    the scorer read one figure rule, so a figure the text writes twice with two meanings is
    refused bare by both and passes the gate quoted; an MCP claim with no citations of its
    own cites what its value claims cite; and the final reply keeps a value only as its
    passed claim states it, never with a quote, role or unit the gate did not check."""
    scanned = False

    def seed(self):
        from integrations.tests.test_capture_sharepoint import RICARDO, SALES, seeded_drive
        drive = seeded_drive()
        drive.file(STATEMENT, "statement.md", "01LOTS", STATEMENT_TEXT.encode(),
                   mime="text/plain", acl=(RICARDO, SALES))
        return drive

    def submit(self, server, arguments: dict) -> dict:
        result = _client(server, "plx.case_get", arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_a_figure_with_two_meanings_is_refused_bare_by_gate_and_scorer(self):
        from evals import score
        handle = self.handle(STATEMENT)
        evidence = {handle: {"ok": True, "relation": "sharepoint.driveItems",
                             "row": {"content_text": STATEMENT_TEXT}}}
        for value, passage in AMBIGUOUS:   # RRO page 10's 9900.00 and 0.00, page 8's 15
            with self.subTest(value=value):
                server = self.server(EMPLOYEE)
                read = self.ok(server, "plx.read", {"citation": handle})
                self.assertEqual(read["content"]["untrusted_text"], STATEMENT_TEXT)
                bare = self.submit(server, _answer(handle, value, "The statement says so."))
                self.assertEqual(bare["outcome"], "repair")
                (failure,) = [f for c in bare["unsupported_claims"] for f in c["failures"]]
                self.assertEqual(failure["code"], "claim_value_mismatch")
                detail = failure["detail"]["untrusted_text"]
                self.assertIn(f"{value} is ambiguous", detail)
                self.assertIn("2 times with different meanings", detail)
                self.assertIn("quote the passage", detail)
                # The scorer's re-read of the same text refuses the same bare value.
                final = {"format": score.ANSWER_FORMAT, "answerability": "answerable",
                         "text": "", "gaps": [], "values": [{
                             "name": "total", "value": value, "citations": [
                                 {"l1_handle": handle, "field_path": "row.content_text"}]}]}
                report = score.gate(final, evidence, None, require_citations=True)["report"]
                (scored,) = [f for f in report["failures"] if f["item"] == "V0"]
                self.assertEqual(scored["code"], "claim_value_mismatch")
                self.assertIn(f"{value} is ambiguous", scored["detail"])
                # The repair the detail asks for: the passage quoted, the gate passes it.
                quoted = _answer(handle, value, "The statement says so.")
                quoted["answer"]["claims"][0]["value_claims"][0]["quote"] = passage
                passed = self.submit(server, quoted)
                self.assertEqual((passed["outcome"], passed["answerability"]["state"]),
                                 ("answered", "answerable"), json.dumps(passed)[:2000])

    def test_a_claim_without_citations_cites_what_its_value_claims_cite(self):
        handle = self.handle(STATEMENT)
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        answer = _answer(handle, "1485.00", "The statement says so.")
        answer["answer"]["claims"][0]["citations"] = []
        passed = self.submit(server, answer)
        self.assertEqual(passed["outcome"], "answered", json.dumps(passed)[:2000])
        self.assertEqual([c["untrusted_text"] for c in passed["claims"][0]["citations"]],
                         [handle])
        # Never a citation no value claim carries: one the session was not returned, or
        # none at all, is refused as before.
        unseen = handle.replace(STATEMENT, "01FILEZ")
        for value_claims, reason in (
                ([{"value": "1485.00", "citation": unseen, "field": "row.content_text"}],
                 "cites_what_this_run_did_not_return"),
                ([{"value": "1485.00", "calculation": {"op": "expression", "inputs": [
                    {"l1_handle": unseen, "field_path": "row.content_text"}]}}],
                 "cites_what_this_run_did_not_return"),
                ([{"value": "1485.00", "calculation": {"op": "expression", "inputs": [
                    {"field_path": "row.content_text"}]}}],
                 "cites_what_this_run_did_not_return"),
                (None, "cites_no_evidence")):
            with self.subTest(reason=reason, value_claims=value_claims):
                server = self.server(EMPLOYEE)
                self.ok(server, "plx.read", {"citation": handle})
                refused = self.submit(server, {"answer": {"claims": [
                    {"text": "The statement says so.", "citations": [],
                     "value_claims": value_claims}]}})
                self.assertEqual([c["reason"] for c in refused["unsupported_claims"]],
                                 [reason])
        # F22-0005: an inline calculation over what the session read now reaches the
        # claim check, refused with a hint the agent can act on (never "uncited").
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        refused = self.submit(server, {"answer": {"claims": [
            {"text": "15% of the visible lines.", "citations": [], "value_claims": [
                {"value": "1485.00", "calculation": {"op": "expression",
                                                     "expression": "x0*x1", "inputs": [
                    {"l1_handle": handle, "field_path": "row.content_text",
                     "value": "9,900.00"}]}}]}]}})
        self.assertEqual([c["reason"] for c in refused["unsupported_claims"]],
                         ["value_claims_invalid"])
        self.assertIn("value_claims_invalid:",
                      refused["repair"]["instruction"]["untrusted_text"])

    def test_the_final_reply_keeps_a_value_only_as_its_passed_claim_states_it(self):
        from operating import answer_gate
        handle = self.handle(STATEMENT)
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        quoted = _answer(handle, "9900.00", "The statement says so.")
        quoted["answer"]["claims"][0]["value_claims"][0]["quote"] = STATEMENT_ROW
        decision = self.submit(server, quoted)   # as the MCP client received it
        passed = answer_gate.passed_cells(decision)
        self.assertEqual([(p["quote"], p["role"], p["unit"]) for p in passed],
                         [(STATEMENT_ROW, None, None)])
        cite = {"l1_handle": handle, "field_path": "row.content_text"}
        exact = {"name": "total", "value": "9900.00",
                 "citations": [{**cite, "quote": STATEMENT_ROW}]}
        self.assertTrue(answer_gate.backs(exact, passed))
        self.assertEqual(answer_gate.kept_value(exact, passed), exact)
        # F21-0004/5/6/8, F22-0008: what the reply added after the check is never backed;
        # the kept value is the passed claim's: its quote, no role or unit it lacks.
        for added in ({"citations": [{**cite, "quote": "Total open, all 1-30 days"}]},
                      {"citations": [{**cite, "quote": "| Until it runs out | $9,900.00"}]},
                      {"citations": [{**cite, "quote": "9,900.00"}]},
                      {"quantity_role": "invoiced"}, {"unit": "CAD"},
                      {"currency": "CAD"}, {"currency": "USD", "unit": "USD"},
                      {"quantity_role": "invoiced", "unit": "units"}):
            with self.subTest(added=added):
                value = {**exact, **added}
                self.assertFalse(answer_gate.backs(value, passed))
                self.assertEqual(answer_gate.kept_value(value, passed), exact)
        # No quote stated: backed, and kept with the quote the gate checked.
        bare = {"name": "total", "value": "9900.00", "citations": [cite]}
        self.assertTrue(answer_gate.backs(bare, passed))
        self.assertEqual(answer_gate.kept_value(bare, passed), exact)
        # Another value, another cell, or a reformatted text value: neither.
        for other in ({**exact, "value": "8415.00"},
                      {**exact, "citations": [{**cite, "field_path": "row.name"}]},
                      {**exact, "citations": []}, {**exact, "value": True}):
            with self.subTest(other=other):
                self.assertFalse(answer_gate.backs(other, passed))
                self.assertIsNone(answer_gate.kept_value(other, passed))
        # A role, unit or currency the gate checked stays; another one does not.
        checked = [{**passed[0], "role": "invoiced", "unit": "CAD", "currency": "CAD"}]
        both = {**exact, "quantity_role": "invoiced", "unit": "CAD", "currency": "CAD"}
        self.assertTrue(answer_gate.backs(both, checked))
        self.assertEqual(answer_gate.kept_value(both, checked), both)
        self.assertFalse(answer_gate.backs({**both, "currency": "USD"}, checked))
        self.assertEqual(answer_gate.kept_value({**both, "currency": "USD"}, checked),
                         {**exact, "quantity_role": "invoiced", "unit": "CAD"})
        # A unit the gate recorded unverified is no checked unit.
        unit = _answer(handle, "1485.00", "The statement says so.")
        unit["answer"]["claims"][0]["value_claims"][0]["unit"] = "percent"
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        cells = answer_gate.passed_cells(self.submit(server, unit))
        self.assertEqual([p["unit"] for p in cells], [None])


    def test_an_unverified_unit_is_dropped_and_the_value_kept(self):
        """A02's shape: 68160 claimed in "units" on a text that prints no unit after it.
        The claim passes with the unit unverified; nothing states the unit (the gate's
        phrase, the kept reply value), the value is never withheld for it, and a reply
        that adds the unit (or any unchecked unit, currency, role or quote) is refused."""
        from operating import answer_gate
        handle = self.handle(STATEMENT)
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        answer = _answer(handle, "68160", "The statement says so.")
        answer["answer"]["claims"][0]["value_claims"][0]["unit"] = "units"
        decision = self.submit(server, answer)
        self.assertEqual(decision["outcome"], "answered", json.dumps(decision)[:2000])
        (claim,) = decision["claims"]
        self.assertEqual(claim["check"]["unit_basis"], "unverified")
        self.assertEqual(claim["phrase"]["untrusted_text"], "68160")
        self.assertNotIn("units", decision["reply"]["text"]["untrusted_text"])
        passed = answer_gate.passed_cells(decision)
        self.assertEqual([(p["value"], p["unit"]) for p in passed], [("68160", None)])
        cite = {"l1_handle": handle, "field_path": "row.content_text"}
        bare = {"name": "final_units", "value": "68160", "citations": [cite]}
        self.assertTrue(answer_gate.backs(bare, passed))
        for added in ({"unit": "units"}, {"unit": "EA"}, {"currency": "CAD"},
                      {"quantity_role": "actual"},
                      {"citations": [{**cite, "quote": "| Final | 68,160 |"}]}):
            with self.subTest(added=added):
                value = {**bare, **added}
                self.assertFalse(answer_gate.backs(value, passed))
                self.assertEqual(answer_gate.kept_value(value, passed), bare)

    def test_a_currency_sent_through_mcp_is_checked(self):
        from operating import answer_gate
        from operating.mcp_server import _VALUE_CLAIMS
        self.assertIn("currency", _VALUE_CLAIMS["items"]["properties"])
        handle = self.handle(STATEMENT)
        cite = {"l1_handle": handle, "field_path": "row.content_text"}
        for currency, outcome in (("CAD", "answered"), ("USD", "repair")):
            with self.subTest(currency=currency):
                server = self.server(EMPLOYEE)
                self.ok(server, "plx.read", {"citation": handle})
                answer = _answer(handle, "40.00", "The statement says so.")
                answer["answer"]["claims"][0]["value_claims"][0]["currency"] = currency
                decision = self.submit(server, answer)
                self.assertEqual(decision["outcome"], outcome, json.dumps(decision)[:2000])
                if outcome == "repair":
                    self.assertEqual([f["code"] for c in decision["unsupported_claims"]
                                      for f in c["failures"]], ["claim_currency_mismatch"])
                    continue
                self.assertEqual(decision["claims"][0]["phrase"]["untrusted_text"],
                                 "40.00 CAD")
                passed = answer_gate.passed_cells(decision)
                stated = {"name": "handling", "value": "40.00", "currency": "CAD",
                          "citations": [cite]}
                self.assertTrue(answer_gate.backs(stated, passed))
                self.assertEqual(answer_gate.kept_value(stated, passed), stated)
                self.assertFalse(answer_gate.backs({**stated, "currency": "USD"}, passed))

    def expression(self, handle, value, expression, inputs):
        return {"answer": {"claims": [{"text": "The statement's figures give it.",
                                       "citations": [handle], "value_claims": [
            {"value": value, "op": "expression", "expression": expression, "of": [
                {"citation": handle, "field": "row.content_text", **given}
                for given in inputs]}]}], "text": "The statement's figures give it."}}

    def test_a_quoted_figure_inside_a_served_window_reads_in_full(self):
        """A03 (144), A04, A11, A07: the page was read through line= and text_offset
        windows; the quoted row lay inside one, yet the figure, written there and elsewhere
        with another meaning, was checked bare on the window and the answer marked
        partial. A claim's own quote inside a served window reads it in full; a quote
        outside every served part keeps the answer partial."""
        handle = self.handle(STATEMENT)
        row = STATEMENT_TEXT.index(STATEMENT_ROW)
        quoted = _answer(handle, "9900.00", "The statement says so.")
        quoted["answer"]["claims"][0]["value_claims"][0]["quote"] = STATEMENT_ROW
        for read, state in (({"text_offset": row - 4, "max_chars": 200}, "answerable"),
                            ({"max_chars": 200}, "answerable"),
                            ({"text_offset": row + 20}, "partial"),   # cuts the quote
                            ({"text_offset": row + len(STATEMENT_ROW)}, "partial"),
                            ({}, "answerable")):
            with self.subTest(read=read):
                server = self.server(EMPLOYEE)
                self.ok(server, "plx.read", {"citation": handle, **read})
                decided = self.submit(server, quoted)
                self.assertEqual((decided["outcome"], decided["answerability"]["state"]),
                                 ("answered", state), json.dumps(decided)[:2000])

    def test_a_respaced_table_quote_is_kept_as_the_text_writes_it(self):
        """Review of 1cbae4da8d: a quote matching a row across an empty cell passes, and
        the reply keeps the row as the text writes it, never the agent's respacing."""
        from operating import answer_gate
        handle = self.handle(STATEMENT)
        respaced = "| Total open, all 1-30 days |  |  | 9,900.00 |"
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        quoted = _answer(handle, "9900.00", "The statement says so.")
        quoted["answer"]["claims"][0]["value_claims"][0]["quote"] = respaced
        decision = self.submit(server, quoted)
        self.assertEqual(decision["outcome"], "answered", json.dumps(decision)[:2000])
        (passed,) = answer_gate.passed_cells(decision)
        self.assertEqual(passed["quote"], STATEMENT_ROW)
        cite = {"l1_handle": handle, "field_path": "row.content_text", "quote": respaced}
        kept = answer_gate.kept_value({"name": "total", "value": "9900.00",
                                       "citations": [cite]}, [passed])
        self.assertEqual(kept["citations"][0]["quote"], STATEMENT_ROW)

    def test_an_expression_over_figures_the_gate_verified_is_one_checked_value(self):
        """A01, A05, A07: the expected value is an expression over figures the agent's own
        passed claims read, and the gate checked only counts and sums, so the reply
        withheld it. An expression value claim recomputes from figures the gate verifies
        on the cited text; the final reply keeps the value as the gate checked it."""
        from evals import runner, score
        from operating import answer_gate
        handle = self.handle(STATEMENT)
        total = {"value": "9,900.00", "quote": STATEMENT_ROW}
        share = {"value": "1,485.00", "quote": "| Proportional at 15% | $1,485.00 |"}
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle})
        submitted = self.expression(handle, "8415.00", "x0 - x1", [total, share])
        decision = self.submit(server, submitted)
        self.assertEqual((decision["outcome"], decision["answerability"]["state"]),
                         ("answered", "answerable"), json.dumps(decision)[:2000])
        inputs = [{"l1_handle": handle, "field_path": "row.content_text", "value": "9,900.00"},
                  {"l1_handle": handle, "field_path": "row.content_text", "value": "1,485.00"}]
        stated = {"name": "balance", "value": "8415.00",
                  "calculation": {"op": "expression", "expression": "x0 - x1",
                                  "inputs": inputs}}
        final = {"format": score.ANSWER_FORMAT, "answerability": "answerable",
                 "text": "The balance is 8415.00.", "values": [stated], "gaps": []}
        events = [{"type": "tool_call", "name": "plx.case_get", "arguments": submitted,
                   "is_error": False, "result": decision}]
        reply, report = runner.gate_final_reply(
            "Done.\n```json\n" + json.dumps(final) + "\n```", events)
        # The reply states each input with the quote the gate checked it with, the one
        # the scorer pins the figure by (a figure the text writes twice, 9,900.00 here).
        kept = score.answer_candidate(reply)
        checked = {**stated, "calculation": {**stated["calculation"], "inputs": [
            {**inputs[0], "quote": total["quote"]}, {**inputs[1], "quote": share["quote"]}]}}
        self.assertEqual((report["withheld"], kept["values"], kept["answerability"]),
                         ([], [checked], "answerable"), reply)
        passed = answer_gate.passed_cells(decision)
        self.assertTrue(answer_gate.backs(checked, passed))
        # A quote the gate did not check on an input is not backed; kept, it is the gate's.
        forged = {**checked, "calculation": {**checked["calculation"], "inputs": [
            {**inputs[0], "quote": "| Until it runs out | $9,900.00"}, checked[
                "calculation"]["inputs"][1]]}}
        self.assertFalse(answer_gate.backs(forged, passed))
        self.assertEqual(answer_gate.kept_value(forged, passed), checked)
        # What the gate did not check is withheld: another expression, another input.
        for other in ({"expression": "x0 - x1 + x1 - x1"},
                      {"inputs": [inputs[0], {**inputs[1], "value": "1,485.01"}]},
                      {"inputs": [inputs[1], inputs[0]]}):
            with self.subTest(other=other):
                value = {**stated, "calculation": {**stated["calculation"], **other}}
                self.assertIsNone(answer_gate.kept_value(value, passed))
        # A wrong result, or an input the text does not hold, is refused with why.
        for value, inputs_given, said in (
                ("8415.01", [total, share], "is 8415 over its inputs"),
                ("8414.00", [total, {**share, "value": "1,486.00"}], "input x1")):
            with self.subTest(refused=value):
                server = self.server(EMPLOYEE)
                self.ok(server, "plx.read", {"citation": handle})
                refused = self.submit(server, self.expression(handle, value, "x0 - x1",
                                                              inputs_given))
                self.assertEqual(refused["outcome"], "repair")
                (failure,) = [f for c in refused["unsupported_claims"] for f in c["failures"]]
                self.assertEqual(failure["code"], "claim_value_mismatch")
                self.assertIn(said, failure["detail"]["untrusted_text"])
        # An input whose quote lies past the part the session was served: partial.
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": handle,
                                     "text_offset": STATEMENT_TEXT.index(STATEMENT_ROW)})
        partial = self.submit(server, submitted)
        self.assertEqual((partial["outcome"], partial["answerability"]["state"]),
                         ("answered", "partial"), json.dumps(partial)[:2000])
        # A reader the file is withheld from: nothing passes, no figure leaks.
        server = self.server(OUTSIDER)
        _client(server, "plx.read", {"citation": handle})
        refused = self.submit(server, submitted)
        self.assertEqual(refused["claims"], [])
        self.assertNotIn("Until it runs out", json.dumps(refused))


class PageCitationClaimTests(SharePointHostCase):
    """A value read on one page of a captured document is checked on that page's text
    (field ``text``), the field the scorer's re-read of the page gives (``{"page", "text"}``):
    the gate passes it there and refuses the document's row field on a page citation, so
    an answer it passes is never one the scorer fails on its field (EMD-05 of the case-card
    run: ``row.content_text`` on a ``&page=1`` citation passed the gate, then scored
    ``claim_value_mismatch``)."""
    scanned = True

    def submit(self, server, arguments: dict) -> dict:
        result = _client(server, "plx.case_get", arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_a_page_citation_passes_on_the_page_text_and_never_on_a_row_field(self):
        from evals import score
        from operating.tests.test_case_host_sharepoint import SCAN, SCAN_PO
        page = self.handle(SCAN) + "#page=2"
        server = self.server(EMPLOYEE)
        read = self.ok(server, "plx.read", {"citation": page})
        self.assertIn(SCAN_PO, read["content"]["untrusted_text"])
        refused = self.submit(server, _answer(page, SCAN_PO, "The scan names the order."))
        self.assertEqual(refused["outcome"], "repair")
        (claim,) = refused["unsupported_claims"]
        self.assertEqual(claim["reason"], "value_claims_invalid")
        self.assertIn('field "text"', claim["detail"]["untrusted_text"])
        answer = _answer(page, SCAN_PO, "The scan names the order.")
        answer["answer"]["claims"][0]["value_claims"][0]["field"] = "text"
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": page})
        passed = self.submit(server, answer)
        self.assertEqual(passed["outcome"], "answered", json.dumps(passed)[:2000])
        # The scorer's re-read of the page checks the same value on the same field.
        evidence = {page: {"ok": True, "page": 2,
                           "text": read["content"]["untrusted_text"]}}
        final = {"format": score.ANSWER_FORMAT, "answerability": "partial", "text": "",
                 "gaps": [], "values": [{"name": "order", "value": SCAN_PO, "citations": [
                     {"l1_handle": page, "field_path": "text"}]}]}
        report = score.gate(final, evidence, None, require_citations=True)["report"]
        self.assertFalse([f for f in report["failures"]
                          if f["code"] in score.BLOCKING_GATE_CODES], report)
        # Another page of the same file does not hold the value: refused there.
        other = self.handle(SCAN) + "#page=1"
        answer["answer"]["claims"][0]["citations"] = [other]
        answer["answer"]["claims"][0]["value_claims"][0]["citation"] = other
        server = self.server(EMPLOYEE)
        self.ok(server, "plx.read", {"citation": other})
        self.assertEqual(self.submit(server, answer)["claims"], [])


class ScorerMemberReadTests(unittest.TestCase):
    def test_the_scorer_reads_a_container_member_and_still_not_a_pinned_whole_file(self):
        # The scorer re-reads a cited member's text from the capture store (it read
        # nothing, so a gate-checked quote of a member scored claim_uncited); a pinned
        # whole file stays with the document reader.
        from evals import capture_evidence as ce
        whole = "sharepoint://b%21d/01ZIP@%22%7B01ZIP%7D%2C1%22#sha256=" + "a" * 64
        member = whole + "&member=notes.md&member_sha256=" + "b" * 64
        rows = {member: {"relation": "sharepoint.containerMembers",
                         "row": {"content_text": "Steps covered: scope."}},
                whole: {"relation": "sharepoint.driveItems", "row": {"name": "x.zip"}}}
        read = ce.reader(sharepoint=rows.get)
        self.assertEqual(read(member)["row"]["content_text"], "Steps covered: scope.")
        self.assertIsNone(read(whole))
        self.assertIsNone(ce.reader(sharepoint=lambda handle: None)(member))


class DocumentTextSearchTests(SharePointHostCase):
    """The projection a host builds: mention links and the document-text word index."""
    scanned = False

    def setUp(self):
        super().setUp()
        self.projection = self.root / "ontology-text.sqlite"
        with SharePointCaptures(self.state) as files:
            build_store(self.projection, schema_loader.load_schema(),
                        {"portal_api": StaticPortal([]), "sharepoint": files},
                        clock=lambda: NOW,
                        post_project={"mentions": mentions.store_mention_links,
                                      "text_index": store_text_index})

    def test_documents_finds_a_file_by_words_of_its_captured_text(self):
        server = self.server(QUALITY)
        found = self.ok(server, "plx.documents", {"query": "which units were filled",
                                                  "sources": ["sharepoint"]})
        self.assertEqual([d["citation"] for d in found["documents"]],
                         [self.handle(RELEASE)])
        snippet = found["documents"][0]["snippet"]
        self.assertIn("units filled", snippet["untrusted_text"])
        self.assertEqual(snippet["origin"]["field_path"], "row.content_text")
        self.assertNotIn("partial_word_match", {g["reason"] for g in found["gaps"]})
        # A reader the file is withheld from finds nothing by its text.
        nothing = self.ok(self.server(OUTSIDER), "plx.documents", {
            "query": "which units were filled", "sources": ["sharepoint"]})
        self.assertEqual(nothing["documents"], [])
        self.assertNotIn(TEXT_SECRET, json.dumps(nothing))

    def test_no_file_holding_every_word_lists_the_near_matches_as_such(self):
        server = self.server(QUALITY)
        found = self.ok(server, "plx.documents", {"query": "lot release units zebra",
                                                  "sources": ["sharepoint"]})
        self.assertEqual([d["citation"] for d in found["documents"]],
                         [self.handle(RELEASE)])
        (gap,) = [g for g in found["gaps"] if g["reason"] == "partial_word_match"]
        self.assertIn("zebra", gap["detail"])
        none = self.ok(server, "plx.documents", {"query": "zebra giraffe okapi",
                                                 "sources": ["sharepoint"]})
        self.assertEqual(none["documents"], [])
        self.assertIn("no_match", {g["reason"] for g in none["gaps"]})


class _DirectoryCache:
    """The mail reader's ``directory_cache`` block (``GraphMailReader``: nested blocks
    share the outermost one's lookups), counted; no lookup is made here."""

    def __init__(self):
        self.outermost = self.open = 0

    @contextlib.contextmanager
    def directory_cache(self):
        self.outermost += self.open == 0
        self.open += 1
        try:
            yield
        finally:
            self.open -= 1


class RequestDirectoryCacheTests(SharePointHostCase):
    scanned = False

    def test_each_tool_call_shares_one_directory_cache_and_closes_it(self):
        # An item gate that re-reads many messages of one mailbox asks the directory once
        # per call (2026-10-26: 214 lookups, 22 s, in one plx.documents call).
        server = self.server(QUALITY)
        mail = server.case_runtime.live.mail = _DirectoryCache()
        self.ok(server, "plx.documents", {"query": "release", "sources": ["sharepoint"]})
        self.ok(server, "plx.find", {"query": "release"})
        self.assertEqual((mail.outermost, mail.open), (2, 0))


class UnitCoverageGapTests(SharePointHostCase):
    scanned = False

    def gaps(self, actor) -> tuple[list[dict], list[str]]:
        """The reader's audience_not_granted gaps and the SharePoint units the store holds."""
        found = self.ok(self.server(actor), "plx.documents", {
            "query": "release", "sources": ["sharepoint"]})
        units = sorted(k for k in self.tools._reader_keys if k.startswith("sharepoint:"))
        return [g for g in found["gaps"] if g["reason"] == "audience_not_granted"], units

    def test_the_gap_names_how_many_captured_units_a_reader_lacks_and_only_then(self):
        # The capture holds one unit (the drive): quality and sales are each granted it
        # (the item permissions still decide per file), the outsider is not.
        for actor, lacking in ((QUALITY, 0), (EMPLOYEE, 0), (OUTSIDER, 1)):
            with self.subTest(actor=sorted(actor.principals)):
                gaps, units = self.gaps(actor)
                self.assertEqual(len(units), 1, units)
                if not lacking:
                    self.assertEqual(gaps, [])
                    continue
                (gap,) = gaps
                self.assertTrue(gap["detail"].startswith(f"{lacking} captured unit(s)"),
                                gap["detail"])
                self.assertNotIn("quality-Drive", json.dumps(gap))


if __name__ == "__main__":
    unittest.main()
