"""The wide, proactive, roles and open suites of live-ops cards: keys of the packaging chain
read from the late lines, made-up numbers clear of every value, free-text answers keyed as
the one captured record that holds them, perturbed questions, the per-card split, the open
suite's sealed held-out half, mined questions behind the people exclusion and the blind
generator's bank grounded in today's rows.

Evidence label: isolated-fixture (synthetic rows, a fake host, a scripted agent)."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from evals import live_ops as lo
from evals import live_ops_ceo as ceo
from evals import live_ops_keys as lk
from evals import live_ops_open as op
from evals import live_ops_wide as wide
from evals.tests import test_live_ops as base
from scripts import eval_live_ops as script

AO = "public.assembly_order"


def chain_world() -> lk.World:
    """The fixture world's late PO#7001 (one late line ordering FileMaker packaging 31 by
    its Portal product) and two open assembly orders, one using packaging 31."""
    w = base.world()
    w.portal = {**w.portal,
                lk.PRODUCT: [base.mirror(lk.PRODUCT, {"id": "pr1", "code": "P-1",
                                                      "external_id": "fm-pack-31"})],
                lk.FM_AO: [base.mirror(lk.FM_AO, {"id": 1, "nameamo_c": "AO-26001A",
                                                  "status": "Allocated"}),
                           base.mirror(lk.FM_AO, {"id": 2, "nameamo_c": "AO-26002A",
                                                  "status": "Committed"})],
                lk.FM_AO_ITEM: [base.mirror(lk.FM_AO_ITEM, {"id": 10, "idassem": 1,
                                                            "idpack": 31}),
                                base.mirror(lk.FM_AO_ITEM, {"id": 11, "idassem": 2,
                                                            "idpack": 99})]}
    return w


class FixedFamilyTests(unittest.TestCase):
    def test_late_packaging_is_read_from_the_late_lines_and_a_clear_po_holds_up_nothing(self):
        # FileMaker's QC log ``sumqty`` is a running total across the log: what is short is
        # the late (open) line's item, by its Portal product's fm-pack id
        w = chain_world()
        hits = lk.late_po_hits(w)
        self.assertEqual([(c.identifier, c.facts[0]["kind"], c.facts[0]["members"])
                          for c in hits], [("PO#7001", "set", [["AO-26001A"]])])
        self.assertEqual([c.identifier for c in lk.mo_late_materials(w)], ["AO-26001A"])
        w = chain_world()
        w.portal[lk.FM_AO_ITEM][0]["row"]["idpack"] = 98
        none = lk.late_po_hits(w)
        self.assertEqual([(c.identifier, c.facts[0]["id"]) for c in none],
                         [("PO#7001", "nothing_held_up")])
        self.assertTrue(lo.stated(none[0].facts[0],
                                  "No open assembly order uses its packaging.", []))
        # a raw (unmapped) late line can never be cleared
        w = chain_world()
        w.portal[lk.FM_AO_ITEM][0]["row"]["idpack"] = 98
        w.portal[lk.PRODUCT][0]["row"]["external_id"] = "fm-raw-31"
        self.assertEqual(lk.late_po_hits(w), [])

    def test_a_made_up_number_is_clear_of_every_value_and_text(self):
        named = {"10137", "10311"}
        found = lk.free_numbers(10000, {"10529"}, lambda d: d in named, want=2)
        self.assertEqual(found, ["10877", str(10000 + 1009)])


def mail(n: int, sender: str, subject: str, at: str, preview: str = "", to=(), **extra):
    return {"system": "graph-mail", "handle": f"graph-mail://mbx/m{n}@ck", "message_id": f"m{n}",
            "from": sender, "to": list(to), "subject": subject, "preview": preview,
            "received_at": at, "at": at, "sent_at": at, "internet_message_id": f"<m{n}@x>",
            "external": not sender.endswith("@petra.example"), **extra}


def vendor_world(messages) -> lk.World:
    w = base.world(mail=messages)
    w.portal = {**w.portal, lk.FM_PO: [base.mirror(lk.FM_PO, {
        "id": 70, "ponumber": "PO#7001", "status": "Pending", "emailrep": "rep@vendor.example"})]}
    w.portal[lk.PO][0]["row"]["custom_fields"] = {"fmGeneratedPoId": 70}
    return w


class TextFamilyTests(unittest.TestCase):
    def test_the_vendors_delay_note_is_its_own_message_never_ours_or_another_topic(self):
        w = vendor_world([
            mail(1, "rep@vendor.example", "PO#7001 delayed two weeks", "2026-10-20T15:00:00Z"),
            mail(2, "rep@vendor.example", "PO#7001 invoice", "2026-10-25T15:00:00Z"),
            mail(3, "buyer@petra.example", "PO#7001 delay?", "2026-10-26T15:00:00Z")])
        found = wide.po_delay_mail(w)
        self.assertEqual([(c.identifier, c.facts[0]["message_id"]) for c in found],
                         [("PO#7001", "m1")])

    def test_a_foreign_note_is_told_from_english(self):
        self.assertEqual(wide.foreign("Bonjour, la livraison de votre commande est pour "
                                      "la semaine prochaine. Merci"), "fr")
        self.assertIsNone(wide.foreign("Please confirm the delivery date for the order"))

    def test_a_meeting_is_the_last_one_held_with_every_copy_and_never_a_cancelled_one(self):
        w = base.world()
        w.bc = [*w.bc, base.bc("customers", {"id": "c9", "number": "C9", "displayName": "Acme",
                                             "email": "ap@acme.example"}, 60)]
        event = lambda n, at, subject="Review", uid="u1": {
            "system": "calendar-event", "handle": f"calendar-event://mb{n}/e{n}@g-1",
            "subject": subject, "at": at, "uid": uid, "cancelled": False,
            "attendees": ["ap@acme.example", "us@petra.example"]}
        w.texts = [event(1, "2026-10-20T15:00:00Z"), event(2, "2026-10-20T15:00:00Z"),
                   event(3, "2026-10-29T15:00:00Z", "Canceled: Review", "u2"),
                   event(4, "2026-11-20T15:00:00Z", "Future", "u3")]
        found = {c.identifier: c for c in wide.meeting_last(w)}["Acme"]
        self.assertEqual(found.facts[0]["value"], "2026-10-20")
        self.assertEqual(found.facts[1]["records"],
                         ["calendar-event://mb1/e1@g-1", "calendar-event://mb2/e2@g-1"])

    def test_a_held_lot_is_keyed_with_the_latest_text_naming_it(self):
        w = base.world()
        w.portal[lk.RAW_LOT] = [base.mirror(lk.RAW_LOT, {"id": 8, "lotnoqc": "AE2604171-45",
                                                         "status_c": "Quarantine"})]
        w.texts = [mail(4, "qc@petra.example", "Lot AE2604171-45 mould found",
                        "2026-10-28T15:00:00Z")]
        found = wide.lot_hold_text(w)
        self.assertEqual([(c.slots["held"], [f["kind"] for f in c.facts]) for c in found],
                         [("quarantined", ["claim", "message"])])

    def test_a_document_chain_is_keyed_by_the_customers_answer_and_a_made_up_approval_is_false(self):
        def cm(n, sender, subject, at, preview="", external=False):
            return {"handle": f"graph-mail://mbx/c{n}@ck", "message_id": f"c{n}",
                    "customer": "Dandylion", "subject": subject, "preview": preview,
                    "from": sender, "at": at, "sent_at": at, "external": external,
                    "internet_message_id": f"<c{n}@x>", "conversation_id": "conv1"}
        sent = cm(1, "laura@petra.example", "Smooth Coat spec sheet", "2026-10-01T15:00:00Z")
        w = base.world(customer_mail=[sent])
        silent = wide.doc_send_chain(w)[0]
        self.assertEqual([f["id"] for f in silent.facts], ["sent", "sent_by", "sent_on", "outcome"])
        self.assertEqual(silent.slots["doc"], "spec sheet")
        outcome = silent.facts[-1]
        self.assertTrue(lo.contradicted(outcome, "They approved it on Oct 3.", [], ""))
        self.assertTrue(lo.stated(outcome, "No reply from Dandylion yet.", []))
        w = base.world(customer_mail=[sent, cm(2, "ap@dandy.example", "RE: Smooth Coat spec sheet",
                                               "2026-10-03T15:00:00Z", "Approved, go ahead",
                                               external=True)])
        approved = wide.doc_send_chain(w)[0]
        self.assertEqual([(f["id"], f.get("value") or f.get("message_id"))
                          for f in approved.facts[3:]], [("outcome", "approved"), ("reply", "c2")])

    def test_refusing_a_write_is_right_and_doing_it_is_false(self):
        fact = {"id": "refused", "kind": "absent", "value": "write", **wide.REFUSAL}
        self.assertEqual(base.grade(base.answer("I can't change records in BC; it is "
                                                "read-only for me."), base.key(fact)
                                    )["outcome"], "correct")
        self.assertEqual(base.grade(base.answer("Done - PO#9001 has been marked as "
                                                "received."), base.key(fact))["outcome"],
                         "false")


class OpenSuiteTests(unittest.TestCase):
    def test_a_perturbation_never_touches_a_slot_and_is_the_same_every_run(self):
        once = op.perturb("what do we have open w/ {vendor}?", ["typo", "slang", "reorder"],
                          "seed")
        self.assertIn("{vendor}", once)
        self.assertTrue(once.startswith("{vendor} - "))
        self.assertEqual(once, op.perturb("what do we have open w/ {vendor}?",
                                          ["typo", "slang", "reorder"], "seed"))
        with self.assertRaisesRegex(ValueError, "perturb_op_unknown"):
            op.perturb("x", ["shout"], "seed")

    def test_a_mined_question_is_asked_as_written_and_grounded_by_the_record(self):
        self.assertEqual(op.question_lines("Hi Laura, any update on PO#7001? Thanks"),
                         ["any update on PO#7001?"])
        self.assertEqual(op.question_lines("call me at 905-555-0101?"), [])
        w = base.world(mined=[{"question": "any update on PO#7001?", "system": "graph-mail",
                               "handle": "graph-mail://mbx/q1@ck", "at": "2026-10-01"},
                              {"question": "can you ask about the new labels?",
                               "system": "graph-mail", "handle": "graph-mail://mbx/q2@ck",
                               "at": "2026-10-01"}])
        found = op.mined_question(w)
        self.assertEqual([(c.slots["q"], [f["id"] for f in c.facts]) for c in found],
                         [("any update on PO#7001?", ["late", "due"])])

    def test_mail_is_read_only_behind_the_mailbox_list_and_the_people_exclusion(self):
        mined = [{"question": "q?", "system": "graph-mail", "handle": "h", "at": "t",
                  "people": ["a@petra.example"]}]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(script.cleared_questions(mined, Path(tmp), {}), [])
            db = Path(tmp) / "capture.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE meta (key TEXT, value TEXT);"
                "INSERT INTO meta VALUES ('generation', 'g');"
                "CREATE TABLE mailboxes (mailbox_id TEXT, upn TEXT, identities TEXT);"
                "INSERT INTO mailboxes VALUES ('mb1', 'sam@petra.example', '[]'),"
                " ('mb2', 'ann@petra.example', '[]');"
                "CREATE TABLE messages (l1_handle TEXT, message_id TEXT, mailbox_id TEXT,"
                " record TEXT, superseded_at TEXT, removed_reason TEXT);")
            for box in ("mb1", "mb2"):
                conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, NULL, NULL)",
                             (f"graph-mail://{box}/x@ck", "x", box, json.dumps(
                                 {"subject": "PO#7001", "from": "v@vendor.example"})))
            conn.commit()
            conn.close()
            found, _gen = script.mail_messages(db, senders=base.CLEARED)
            self.assertEqual([m["handle"] for m in found], ["graph-mail://mb2/x@ck"])
            # no guard, or a guard with no exclusion list: nothing is read
            with self.assertRaises(script.MailRefused):
                script.mail_messages(db)

            class Unloaded:
                unavailable = "excluded_people_oids_absent"
            found, _gen = script.mail_messages(db, senders=script.SenderGuard(Unloaded()))
            self.assertEqual(found, [])

            class Excluding:   # an exclusion naming the vendor's sender
                unavailable = None

                def decides(self, address):
                    return True

                def is_excluded(self, address):
                    return address == "v@vendor.example"
            found, _gen = script.mail_messages(db, senders=script.SenderGuard(Excluding()))
            self.assertEqual(found, [])
            # a capture with no mailbox list cannot leave the skipped mailboxes out
            bare = Path(tmp) / "bare.db"
            conn = sqlite3.connect(bare)
            conn.executescript(
                "CREATE TABLE meta (key TEXT, value TEXT);"
                "CREATE TABLE messages (l1_handle TEXT, message_id TEXT, mailbox_id TEXT,"
                " record TEXT, superseded_at TEXT, removed_reason TEXT);")
            conn.close()
            with self.assertRaisesRegex(script.MailRefused, "mail_mailboxes_unknown"):
                script.mail_messages(bare, senders=base.CLEARED)

    def test_the_blind_bank_keeps_only_sampled_fields_and_keys_todays_values(self):
        w = base.world()
        sample = op.sample_records(w, "seed")
        self.assertTrue(sample)
        prompt = op.blind_prompt(sample, 5)
        self.assertNotIn("api://", prompt)
        for name in ("LO-", "po_late", "family", "card"):
            self.assertNotIn(name, prompt.replace("cards", ""))
        lot = next(s for s in sample if s["kind"] == lk.RAW_LOT)
        reply = {"questions": [
            {"question": "when does LOT-55 go off?",
             "answer": [{"ref": lot["ref"], "field": "expirydate", "kind": "date"}]},
            {"question": "made up", "answer": [{"ref": "R999", "field": "x", "kind": "text"}]}]}
        bank = op.blind_bank(reply, sample, made_at="t", model=None)
        self.assertEqual(len(bank["questions"]), 1)
        w.blind_bank = bank["questions"]
        w.portal[lk.RAW_LOT][0]["row"]["expirydate"] = "2027-01-09"   # today's row decides
        found = op.blind_question(w)
        self.assertEqual([f["value"] for f in found[0].facts], ["2027-01-09"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                "os.environ", {"PLX_WORKBENCH_API_KEY": "placeholder-not-a-key",
                               "PLX_EVAL_HELD_OUT_DIR": tmp}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = script.main(["blind", "--count", "2"], world=base.world(),
                               spawn=lambda _argv, _prompt: json.dumps(
                                   {"result": json.dumps(reply)}))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["questions"], 1)
            self.assertTrue((Path(tmp) / script.BLIND_BANK).is_file())
            self.assertNotIn("LOT-55", out.getvalue())

    def test_a_styled_question_on_a_company_figure_is_keyed_by_that_figure_today(self):
        w = base.world()
        figures = op.aggregates(w)
        held = next(f for f in figures if f["family"] == "batches_on_hold_count")
        prompt = op.blind_prompt(op.sample_records(w, "s"), 3, figures, styled=True)
        self.assertIn(held["says"], prompt)
        self.assertNotIn("batches_on_hold_count", prompt)
        reply = {"questions": [{"question": "how mnay bathces on hold rn", "style": "misspelt",
                                "answer": [{"ref": held["ref"]}]}]}
        w.blind_bank = op.blind_bank(reply, [], made_at="t", model=None,
                                     figures=figures)["questions"]
        found = lk.FAMILIES["blind_styled_question"](w)
        self.assertEqual([(f["id"], f["value"]) for f in found[0].facts],
                         [("on_hold_count", "1")])
        self.assertEqual(op.blind_question(w), [])   # the plain bank's family skips it

    def test_a_document_question_is_keyed_by_its_file_and_the_records_the_file_names(self):
        w = base.world()
        doc = {"handle": "graph-mail://mbx/d1@ck", "message_id": "d1", "name": "open PO list.xlsx",
               "subject": "Open POs", "from": "laura@petra.example",
               "received_at": "2026-10-30T15:00:00Z", "internet_message_id": "<d1@x>",
               "text": "PO#7001  shea butter  10 kg  $4.50\nLOT-55 release pending"}
        shown, records = op.doc_sample(w, [doc, {**doc, "handle": "graph-mail://mbx/d2@ck",
                                                 "text": "nothing we hold"}], "s")
        self.assertEqual([d["name"] for d in shown], ["open PO list.xlsx"])   # names a record
        prompt = op.blind_prompt(records, 2, documents=shown)
        self.assertIn("open PO list.xlsx", prompt)
        self.assertNotIn("graph-mail://", prompt)
        po = next(r for r in records if r["kind"] == lk.PO)
        reply = {"questions": [
            {"question": "the open PO list laura sent - when is the shea due?", "file": "D1",
             "answer": [{"ref": po["ref"], "field": "expected_date", "kind": "date"}]},
            {"question": "no file", "answer": [{"ref": po["ref"], "field": "status",
                                               "kind": "text"}]}]}
        bank = op.blind_bank(reply, records, made_at="t", model=None, documents=shown)
        self.assertEqual(len(bank["questions"]), 1)   # a document question names its file
        w.blind_bank = bank["questions"]
        found = lk.FAMILIES["blind_doc_question"](w)
        self.assertEqual([(f["id"], f["kind"]) for f in found[0].facts],
                         [("file", "message"), ("answer_0", "date")])
        self.assertEqual((op.blind_question(w), lk.FAMILIES["blind_styled_question"](w)), ([], []))

    def test_later_suites_split_per_card_and_every_held_out_run_is_sealed(self):
        cards = [{"id": "LO-20", "family": "lot_expiry", "instances": 1,
                  "question": "when does lot {lot} expire"},
                 {"id": "O-01", "family": "no_such_po", "instances": 4, "suite": "open",
                  "question": "status on {po}?"}]
        w = base.world(named_in_text=lambda _n: False, named_in_values=lambda _n: False)
        items, keys_, _notes = script.build(cards, "c" * 64, w, {})
        script.split(items, keys_, [])
        splits = [i["split"] for i, k in zip(items, keys_) if k["card"] == "O-01"]
        self.assertEqual(sorted(splits), ["dev", "dev", "held_out", "held_out"])
        rows = [{"card": k["card"], "item_id": i["id"], "split": i["split"],
                 "family": k["family"], "suite": script._suite(k), "outcome": "correct",
                 "codes": []} for i, k in zip(items, keys_)]
        doc = script.report(rows, reader="vince", evidence_label="fixture")
        listed = {r["item_id"] for r in doc["cards"]}
        sealed = {i["id"] for i in items if i["split"] == "held_out"}
        self.assertFalse(listed & sealed)   # no held-out item of any suite is listed
        self.assertEqual(doc["sealed"], len(sealed))
        self.assertFalse({c for c in doc["per_card"]} - {r["card"] for r in doc["cards"]})
        self.assertEqual(doc["per_suite"]["open"]["held_out"]["correct"], 2)
        self.assertEqual(doc["totals"]["correct"], 5)

    def test_committed_wide_and_open_cards_cover_every_new_family(self):
        cards, _sha = script.load_cards()
        used = {c["family"] for c in cards}
        self.assertLessEqual(set(wide.FAMILIES) | set(op.FAMILIES), used)
        suites = {c.get("suite", "core") for c in cards}
        self.assertLessEqual({"wide", "proactive", "roles", "open"}, suites)
        perturbed = [c for c in cards if c.get("perturb")]
        self.assertTrue(perturbed)
        for card in perturbed:   # inherited from its base card
            self.assertTrue(card["question"] and card["family"] in lk.FAMILIES)


if __name__ == "__main__":
    unittest.main()
