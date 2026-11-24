"""Guards on a person's own autopilot switch (``operating.autopilot_write``): opt-in and
pause are per person and per workflow, a blank or malformed person is refused, the people
exclusion fails closed, concurrent switches keep one consistent chain, a failed store write
is an error, and money stays prepare-only even when everything else says live.

SYNTHETIC people, gate files and items; fake transports only, no network. Evidence label:
injected-transport.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from operating import autopilot as ap
from operating import autopilot_engine
from operating import autopilot_write as aw
from operating import card_actions
from operating.tests.test_autopilot_write import (
    HIDDEN, HIDDEN_OID, NOW, OWNER, OWNER_OID, Base, Transport, _Sender, admit_judged,
    mc_item, po_item)
from operating import autopilot_write_adapters as wa
from operating import autopilot_commitments as ac

OTHER = "other@plx.example"
OTHER_OID = "55555555-5555-4555-8555-555555555555"
FLOWS = ("internal_approval", "customer_order_intake")


class UnavailableButLenient:
    """An exclusion list that could not load, yet would clear everyone if asked: only the
    ``unavailable`` check stands between it and a live write."""
    unavailable = "exclusion list could not be loaded"

    def decides(self, address):
        return True

    def is_excluded(self, address):
        return False

    def known_domains(self):
        return {"plx.example"}


class Raising:
    """A loaded exclusion list whose lookups fail (the directory is down)."""
    unavailable = None

    def decides(self, address):
        raise OSError("directory down")

    def is_excluded(self, address):
        raise OSError("directory down")

    def known_domains(self):
        return {"plx.example"}


def people_guard() -> PeopleExclusion:
    found = PeopleExclusion([HIDDEN_OID])
    found.attach_directory([(OWNER_OID, OWNER, []), (HIDDEN_OID, HIDDEN, []),
                            (OTHER_OID, OTHER, [])])
    return found


def item_of(person: str, n: int) -> dict:
    return {**mc_item(n), "owner": person}


class Guards(Base):
    def pass_for(self, *people: str, workflows=FLOWS) -> None:
        """A fresh, complete scorer run passing ``workflows`` for each person's own role."""
        run = Path(self.tmp.name) / f"fresh-{time.monotonic_ns()}"
        run.mkdir()
        (run / autopilot_engine.GATE_FILE).write_text(json.dumps({"schema": autopilot_engine.GATE_SCHEMA, "roles": {
            f"person:{p}": {"members": [p], "workflows": {
                w: {"judged": 15, "matched": 14} for w in workflows}} for p in people}}))
        for n, p in enumerate(people):
            (run / f"s{n}").mkdir()
            (run / f"s{n}" / "result.json").write_text(json.dumps(
                {"role": f"person:{p}", "status": "complete",
                 "window": ["2026-11-06T07:00:00Z", "2026-11-10T07:00:00Z"]}))
        rules = aw.load_rules()
        out = aw.record_scores(run, self.state / aw.WRITE_GATE_FILE, rules)
        self.assertEqual(sorted(out["admitted"]), sorted(f"person:{p}" for p in people))
        admit_judged(self.state, Path(self.tmp.name))     # the categories' judged scores pass

    def journal(self) -> aw.Journal:
        return aw.Journal(self.state / aw.AUDIT_FILE)

    def gate(self) -> aw.WriteGate:
        return aw.WriteGate(self.state / aw.WRITE_GATE_FILE, aw.load_rules())

    def switch(self, person: str, workflow: str = "internal_approval", on: bool = True):
        return aw.set_opt_in(self.journal(), self.gate(), person, workflow, on, person, NOW)

    def run_for(self, item: dict, env=None, g=None) -> dict:
        """One run of ``item``; by default with the master switch on and the owner's go-live
        file placed for every system, so only the person's own gate, switch and pause (or the
        guard under test) stand between the item and a live write."""
        if env is None:
            env = {aw.MASTER_ENV: "1"}
            self.flag(*sorted(set(aw.FLAG_SYSTEM.values())))
        return self.executor(env, g=people_guard() if g is None else g).run(item, NOW)[0]


class PerPerson(Guards):
    def test_one_persons_opt_in_never_enables_another_both_eligible(self):
        self.pass_for(OWNER, OTHER)
        self.switch(OTHER)
        r = self.run_for(item_of(OWNER, 1))
        self.assertEqual((r["status"], r["reasons"]), ("dry_run", ["not_switched_on_by_person"]))
        self.assertEqual(self.mc.calls, [])
        # the one who switched it on is live: the gate and switches are otherwise ready
        self.assertEqual(self.run_for(item_of(OTHER, 2))["status"], "done")
        self.assertEqual(len(self.mc.calls), 1)

    def test_one_persons_pause_never_pauses_another(self):
        self.pass_for(OWNER, OTHER)
        self.switch(OWNER)
        self.switch(OTHER)
        aw.set_paused(self.journal(), OTHER, True, OTHER, NOW)
        self.assertEqual(self.run_for(item_of(OWNER, 1))["status"], "done")
        r = self.run_for(item_of(OTHER, 2))
        self.assertEqual((r["status"], r["reasons"]), ("dry_run", ["paused_by_person"]))
        self.assertEqual(len(self.mc.calls), 1)

    def test_an_opt_in_is_for_its_workflow_only(self):
        self.pass_for(OWNER)                  # both workflows pass for the owner
        self.switch(OWNER, "internal_approval")
        r = self.run_for(po_item(draft=False))
        self.assertEqual((r["status"], r["reasons"]), ("dry_run", ["not_switched_on_by_person"]))
        self.assertEqual(self.bc.calls, [])
        self.assertEqual(self.run_for(mc_item())["status"], "done")

    def test_a_blank_or_malformed_person_is_refused_and_writes_nothing(self):
        self.pass_for(OWNER)
        journal, gate = self.journal(), self.gate()
        before = len(journal.rows())
        for person in ("", "   ", None, "person:owner@plx.example", "a b@plx.example"):
            with self.subTest(person=person):
                # off is always allowed for a real person: only the person check refuses it
                with self.assertRaisesRegex(ValueError, "unknown person"):
                    aw.set_opt_in(journal, gate, person, "internal_approval", False, person, NOW)
                with self.assertRaisesRegex(ValueError, "unknown person"):
                    aw.set_paused(journal, person, True, person, NOW)
        self.assertEqual(len(journal.rows()), before)

    def test_by_must_be_the_person_for_off_and_resume_too(self):
        self.pass_for(OWNER, OTHER)
        self.switch(OWNER)
        journal, gate = self.journal(), self.gate()
        before = len(journal.rows())
        for by in (OTHER, "", None, OWNER + "x"):
            with self.subTest(by=by):
                with self.assertRaises(PermissionError):
                    aw.set_opt_in(journal, gate, OWNER, "internal_approval", False, by, NOW)
                with self.assertRaises(PermissionError):
                    aw.set_paused(journal, OWNER, True, by, NOW)
        self.assertEqual(len(journal.rows()), before)
        self.assertEqual(self.run_for(mc_item())["status"], "done")


class Exclusion(Guards):
    def test_an_unavailable_list_fails_closed_even_if_lookups_would_clear(self):
        self.pass_for(OWNER)
        self.switch(OWNER)
        r = self.run_for(mc_item(), g=UnavailableButLenient())
        self.assertEqual((r["status"], r["reasons"]), ("excluded", ["exclusion_unavailable"]))
        self.assertEqual(aw.excluded(None, OWNER, []), "exclusion_unavailable")
        self.assertEqual(self.mc.calls, [])

    def test_a_failing_lookup_clears_nobody(self):
        self.pass_for(OWNER)
        self.switch(OWNER)
        r = self.run_for(mc_item(), g=Raising())
        self.assertEqual((r["status"], r["reasons"]), ("excluded", ["exclusion_error"]))
        self.assertEqual(self.mc.calls, [])

    def test_an_excluded_person_is_never_acted_on_even_eligible_and_switched_on(self):
        # the gate passes for them and their own switch is on: the exclusion still wins
        self.pass_for(OWNER, HIDDEN)
        self.switch(HIDDEN)
        r = self.run_for(item_of(HIDDEN, 1))
        self.assertEqual((r["status"], r["reasons"]), ("excluded", ["owner_not_cleared"]))
        self.assertEqual(self.mc.calls, [])


class Money(Guards):
    def test_money_never_goes_live_on_the_gate_or_an_unacknowledged_approval(self):
        self.pass_for(OWNER)
        self.switch(OWNER, "customer_order_intake")
        # an order entry carries quantities and a date: on the gate alone it is only a
        # dry run (owner: an autopilot never commits money, a quantity or a date)
        r = self.run_for(po_item(draft=False))
        self.assertEqual(r["status"], "dry_run")
        self.assertIn("commits:figure_field:quantity", r["reasons"])
        self.assertEqual(self.bc.calls, [])
        priced = {**po_item(price=True, draft=False), "item_id": "it_po_priced"}
        r = self.run_for(priced)
        self.assertEqual(r["status"], "not_routine")
        self.assertIn("field:unitPrice", r["reasons"])
        # a person's accept that did not acknowledge the money does not move it either
        approval = aw.Approval(by=(OWNER,), confirmed_live=True)
        r = self.executor({aw.MASTER_ENV: "1"}, g=people_guard()).run(priced, NOW, approval)[0]
        self.assertEqual((r["status"], r.get("needs_acknowledgement")), ("not_routine", True))
        self.assertEqual(self.bc.calls, [])


class Commitments(Guards):
    """Write-readiness probes for an unattended mail or Teams message (owner: only purely
    informational ones leave by themselves): a figure, date or day in any form, or what a
    model reads as money, a quantity, a date or a judgment in any words, keeps it a dry run;
    an unreadable model answer keeps it a dry run too; a person's confirmed accept of the
    exact item is not held back by it."""

    PROBES = ("cost of the shipment", "250 fee", "bill them for the freight",
              "total is 1,250", "we owe them", "raise a purchase order for 500 units",
              "budget approved", "1250 dollars", "quantity 500 at 4.20 each",
              "we can ship it next Tuesday", "ETA 11/16", "five hundred cases")

    def mail_item(self, n: int, body: str, kind: str = "email") -> dict:
        item = po_item()
        item["item_id"] = f"it_commit_{n}"
        item["body"]["prepared"]["write"] = None
        item["body"]["prepared"]["draft"].update(channel=kind, body=body)
        return item

    def test_messages_with_figures_or_money_words_stay_dry_runs(self):
        self.pass_for(OWNER)
        self.switch(OWNER, "customer_order_intake")
        env = {aw.MASTER_ENV: "1"}
        self.flag(*sorted(set(aw.FLAG_SYSTEM.values())))
        read: list[str] = []

        def model(text: str) -> list[str]:
            # stands in for the model: it reads money in words the figure rule cannot
            read.append(text)
            words = ("cost", "bill", "owe", "budget")
            return ["commits:money"] if any(w in text for w in words) else []
        mail = Transport()
        for n, body in enumerate(self.PROBES):
            ex = aw.Executor(self.state, environ=env, guard=people_guard(), commits=model,
                             adapters={"send_mail": wa.MailAdapter(_Sender(mail), environ=env)})
            r = ex.run(self.mail_item(n, f"Hi, {body}."), NOW)[0]
            with self.subTest(body=body):
                self.assertEqual(r["status"], "dry_run")
                self.assertTrue(any(x.startswith("commits:") for x in r["reasons"]), r["reasons"])
        self.assertEqual(mail.calls, [])
        # an unreadable reading holds it back; a purely informational one goes
        unread = aw.Executor(self.state, environ=env, guard=people_guard(),
                             commits=lambda text: ac.model_commitments(text, call=lambda *a, **k: None),
                             adapters={"send_mail": wa.MailAdapter(_Sender(mail), environ=env)})
        r = unread.run(self.mail_item(90, "Hi, the CoA is with our QA team."), NOW)[0]
        self.assertEqual((r["status"], r["reasons"]), ("dry_run", ["commits:unchecked"]))
        ok = aw.Executor(self.state, environ=env, guard=people_guard(), commits=lambda t: [],
                         adapters={"send_mail": wa.MailAdapter(_Sender(mail), environ=env)})
        # a reader outside the company makes the message class B (ASSIST specs 1.3): it waits
        # for a person whatever it says
        r = ok.run(self.mail_item(91, "Hi, the CoA is with our QA team."), NOW)[0]
        self.assertEqual((r["status"], r["reasons"]), ("dry_run", ["class_B_needs_a_person"]))
        self.assertEqual(mail.calls, [])
        # an informational message to colleagues only goes by itself
        inside = aw.Executor(self.state, environ=env, guard=people_guard(), commits=lambda t: [],
                             internal_domains=["plx.example"],
                             adapters={"send_mail": wa.MailAdapter(_Sender(mail), environ=env)})
        item = self.mail_item(93, "Hi, the CoA is with our QA team.")
        item["body"]["prepared"]["draft"].update(to=[{"address": OTHER}], subject="Re: documents")
        r = inside.run(item, NOW)[0]
        self.assertEqual(r["status"], "done")
        self.assertEqual(len(mail.calls), 1)

    def test_a_confirmed_accept_is_the_persons_own_commitment(self):
        env = {aw.MASTER_ENV: "1", aw.APPROVED_ENV: "send_mail"}
        mail = Transport()
        ex = aw.Executor(self.state, environ=env, guard=people_guard(),
                         commits=lambda t: ["commits:money"],
                         adapters={"send_mail": wa.MailAdapter(_Sender(mail), environ=env)})
        approve = aw.Approval(by=(OWNER,), confirmed_live=True)
        r = ex.run(self.mail_item(92, "Hi, total is 1,250 for the 500 units."), NOW, approve)[0]
        self.assertEqual(r["status"], "done")
        self.assertEqual(len(mail.calls), 1)

    def test_figures_are_read_structurally(self):
        known = "Re: PO10005674 lot BC-26257D TASK-3485 Nov15"
        self.assertEqual(ac.figures("PO10005674, lot BC-26257D, TASK-3485, a@b.example, "
                                    "https://x.example/1", known), [])
        # a reference the item never named, and numbers inside words, are figures
        for text in ("4.20", "$1,250", "11/16", "2026-10-21", "Tuesday", "next week",
                     "500units", "five hundred", "qty500", "Nov15", "EUR500",
                     "Qty 500@$2.50", "PO10005675"):
            with self.subTest(text=text):
                self.assertTrue(ac.figures(text, known))
        self.assertTrue(ac.figures("PO10005674"))          # nothing named it: held
        self.assertEqual(ac.commitments({"kind": "bc_write", "payload": {
            "documentId": "6f1c", "lines": [{"quantity": 4, "itemId": "x"}]}}),
            ["commits:figure_field:quantity"])
        self.assertEqual(ac.commitments({"kind": "mc_approval", "payload": {"x": 5}}), [])
        # every kind that says something gets the model's reading; too long to read whole: held
        read: list = []
        note = {"kind": "mc_task", "operation": "task_note", "payload": {"notes": "Docs filed."}}
        self.assertEqual(ac.unattended(note, lambda t: read.append(t) or ["commits:judgment"], True),
                         ["commits:judgment"])
        self.assertEqual(read, ["Docs filed."])
        self.assertEqual(ac.unattended(note, lambda t: [], False), [])
        self.assertEqual(ac.model_commitments("x" * (ac.READ_MOST + 1), call=lambda *a, **k: 1 / 0),
                         ["commits:unchecked"])
        self.assertEqual(ac.model_commitments("x", call=lambda *a, **k: type(
            "D", (), {"stdout": '{"result": "{\\"commits\\": [\\"tax\\"]}"}'})()),
            ["commits:unchecked"])


class Concurrency(Guards):
    THREADS, ROUNDS = 8, 12

    def test_concurrent_switches_keep_one_chain_with_no_lost_or_duplicate_row(self):
        self.pass_for(OWNER, OTHER)
        start = len(self.journal().rows())
        barrier = threading.Barrier(self.THREADS)
        written: list[dict] = []
        errors: list[BaseException] = []
        keep = threading.Lock()

        def work(t: int) -> None:
            # each thread is a separate request: its own journal and gate objects
            person = (OWNER, OTHER)[t % 2]
            journal, gate = self.journal(), self.gate()
            try:
                barrier.wait()
                for i in range(self.ROUNDS):
                    if i % 4 == 3:
                        row = aw.set_paused(journal, person, bool(t % 3), person, NOW)
                    else:
                        row = aw.set_opt_in(journal, gate, person, "internal_approval",
                                            (t + i) % 2 == 0, person, NOW)
                    with keep:
                        written.append(row)
            except BaseException as error:  # noqa: BLE001 - reported below
                with keep:
                    errors.append(error)

        threads = [threading.Thread(target=work, args=(t,)) for t in range(self.THREADS)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        self.assertEqual(errors, [])
        rows = self.journal().rows()             # re-checks seq, prev and hash
        mine = rows[start:]
        self.assertEqual(len(mine), self.THREADS * self.ROUNDS)
        self.assertEqual([r["seq"] for r in rows], list(range(1, len(rows) + 1)))
        self.assertEqual(sorted(r["hash"] for r in written), sorted(r["hash"] for r in mine))
        # the final state is each person's last row, nothing else
        found, stopped = aw.opt_ins(rows), aw.paused(rows)
        for person in (OWNER, OTHER):
            last = [r for r in mine if r["person"] == person and r["event"] == "opt_in"][-1]
            self.assertEqual(found[(person, "internal_approval")]["on"], last["on"])
            pause = [r for r in mine if r["person"] == person
                     and r["event"] == "opt_in_paused"][-1]
            self.assertEqual(person in stopped, pause["paused"])


class StoreFailure(Guards):
    def test_a_failed_write_raises_and_records_nothing(self):
        self.pass_for(OWNER)
        journal = self.journal()
        before = journal.rows()
        with mock.patch.object(card_actions.os, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.switch(OWNER)
            with self.assertRaises(OSError):
                aw.set_paused(journal, OWNER, True, OWNER, NOW)
        rows = journal.rows()
        self.assertEqual(rows, before)
        self.assertNotIn((OWNER, "internal_approval"), aw.opt_ins(rows))
        r = self.run_for(mc_item())
        self.assertEqual((r["status"], r["reasons"]), ("dry_run", ["not_switched_on_by_person"]))
        self.assertEqual(self.mc.calls, [])


if __name__ == "__main__":
    unittest.main()
