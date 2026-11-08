"""Watchlists and plain-word alert rules (``operating.watchlists``): rules compile into the
restricted spec or are refused with a reason, follows and rules are judged over what the
delta layers changed, and an alert shows a reader only what their grant admits.

Evidence label: ``isolated-fixture`` (synthetic stores, no source system)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
import unittest.mock

import shutil
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

from operating import proactive_engine as pe
from operating import proactive_incremental as pi
from operating import watchlists as wl
from operating.ontology_tools import Actor
from operating.tests.test_proactive_engine import AS_OF, World, people
from operating.tests.test_proactive_incremental import set_value
from operator_api import watch
from operator_api.errors import UserFacing

T = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
OWNER, OTHER = "plx:employee:owner", "plx:employee:other"


def everyone(_key: str) -> bool:
    return True


def no_refusals(handles):
    return {h: None for h in handles}


class Fixture:
    """A synthetic store rebuilt after each change; the watchlists file beside a fixed
    path, as the served store's is."""

    def __init__(self, tmp: Path) -> None:
        self.tmp, self.w, self.n = tmp, World(), 0
        self.watch = tmp / "served.sqlite.watchlists.sqlite"
        self.clock = T

    def store(self) -> Path:
        self.n += 1
        return self.w.build(self.tmp / f"gen{self.n}.sqlite")

    def run(self, moved=()) -> dict:
        self.last = self.store()
        st = pe._Store(self.last)
        try:
            return wl.evaluate(st, self.watch, moved, clock=lambda: self.clock)
        finally:
            st.close()

    def alerts(self, reader: str = OWNER, may_read=everyone, held=("finance",),
               decide=no_refusals) -> list[dict]:
        rows = wl.WatchStore(self.watch).raw_alerts(reader, 1000)
        return wl.trim(rows, may_read, held, decide, self.current)

    def current(self, ids):
        """The records as the last store built holds them now."""
        st = pe._Store(self.last)
        try:
            return wl._read(st, ids)
        finally:
            st.close()


def vocab() -> wl.Vocabulary:
    return wl.vocabulary()


class RuleCompile(unittest.TestCase):
    def test_the_plain_words_compile_and_read_back(self):
        got = wl.compile_rule("tell me when any order over $50k slips", vocab(), {"finance"})
        self.assertEqual(got["spec"], {"schema": wl.RULE_SCHEMA, "subject": "SalesOrder",
                                       "when": [{"property": "required_date", "op": "later"},
                                                {"property": "total_amount", "op": "gt",
                                                 "value": "50000"}]})
        self.assertIn("50,000 in the record's own currency", got["description"])
        self.assertEqual(got["markings"], ["finance"])
        got = wl.compile_rule("alert me if a supplier is late twice in a month", vocab(),
                              set())
        self.assertEqual(got["spec"]["count"], {"at_least": 2, "within_days": 30,
                                                "per": "PurchaseOrder.ordered_from"})
        self.assertIn("for the same supplier", got["description"])
        got = wl.compile_rule("tell me when a PO over US$10,000 slips", vocab(), {"finance"})
        self.assertEqual(got["spec"]["when"][1]["currency"], "USD")

    def test_a_finance_rule_is_refused_to_a_reader_without_finance(self):
        with self.assertRaises(wl.RuleRefused) as refused:
            wl.compile_rule("tell me when any order over $50k slips", vocab(), {"pii"})
        self.assertIn("only readers cleared for finance", refused.exception.problems[0])

    def test_unsafe_or_unsupported_specs_are_refused_with_a_reason(self):
        good = {"schema": wl.RULE_SCHEMA, "subject": "SalesOrder",
                "when": [{"property": "required_date", "op": "later"}]}
        self.assertEqual(wl.validate(good, vocab(), set()), good)
        bad = [
            ({**good, "code": "__import__('os').system('id')"}, "cannot hold 'code'"),
            ({**good, "subject": "os.system"}, "not a record type"),
            ({**good, "when": [{"property": "__class__", "op": "later"}]}, "not a property"),
            ({**good, "when": [{"property": "required_date", "op": "eval"}]},
             "cannot be used"),
            ({**good, "when": [{"property": "status", "op": "becomes",
                                "value": "${jndi:x}"}]}, "plain word"),
            ({**good, "when": [{"property": "total_amount", "op": "gt", "value": 5e4}]},
             "plain number"),
            ({**good, "when": [{"property": "total_amount", "op": "gt", "value": "1e400"}]},
             "plain number"),
            ({**good, "when": [{"property": "required_date", "op": "later",
                                "lambda": "x"}]}, "cannot hold 'lambda'"),
            ({**good, "when": [good["when"][0]] * 5}, "1 to 4 conditions"),
            ({**good, "count": {"at_least": 1000, "within_days": 30}}, "2 to 20"),
            ({**good, "count": {"at_least": 2, "within_days": 9999}}, "1 to 366"),
            ({**good, "count": {"at_least": 2, "within_days": 30,
                                "per": "PurchaseOrder.ordered_from"}}, "not a link from"),
            ({**good, "schema": "v0"}, "plx-alert-rule/v1"),
            ("when x", "rule spec object"),
        ]
        for spec, reason in bad:
            with self.subTest(reason=reason):
                with self.assertRaises(wl.RuleRefused) as refused:
                    wl.validate(spec, vocab(), {"finance"})
                self.assertIn(reason, " ".join(refused.exception.problems))
        for text, reason in (("alert me when the weather changes", "kind of record"),
                             ("tell me about every order", "what change"),
                             ("x" * 400, "under 300")):
            with self.subTest(text=text[:20]):
                with self.assertRaises(wl.RuleRefused) as refused:
                    wl.compile_rule(text, vocab(), {"finance"})
                self.assertIn(reason, " ".join(refused.exception.problems))

    def test_a_model_proposal_goes_through_the_same_validation(self):
        calls = []

        def model(text, context):
            calls.append(text)
            return {"schema": wl.RULE_SCHEMA, "subject": "SalesOrder",
                    "when": [{"property": "required_date", "op": "later"}],
                    "exec": "rm -rf /"}
        with self.assertRaises(wl.RuleRefused):
            wl.compile_rule("ping me when sales orders wobble", vocab(), set(), model=model)
        self.assertEqual(len(calls), 1)
        # the grammar reads it: no model call at all
        wl.compile_rule("tell me when any order slips", vocab(), set(), model=model)
        self.assertEqual(len(calls), 1)


class Follows(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fx = Fixture(self.tmp)

    def _orders(self, n: int, **scope) -> list[str]:
        return [self.fx.w.obj("PurchaseOrder", {"po_number": f"SYNTH-{i}", "status": "Open",
                                                "expected_date": "2026-11-01"}, **scope)
                for i in range(n)]

    def test_each_followed_change_alerts_its_follower_once_at_1_10_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                pos = self._orders(n)
                store = wl.WatchStore(self.fx.watch)
                for oid in pos:
                    store.follow(OWNER, oid, "PurchaseOrder", None)
                store.follow(OTHER, pos[0], "PurchaseOrder", None)
                self.assertEqual(self.fx.run()["baselined"], n)   # baseline: no alerts
                self.assertEqual(self.fx.alerts(), [])
                for oid in pos:
                    set_value(self.fx.w, oid, "expected_date", "2026-11-20")
                started = time.monotonic()
                done = self.fx.run(pos)
                self.assertLess(time.monotonic() - started, 5.0)
                got = self.fx.alerts()
                self.assertEqual(sorted(a["about"]["object_id"] for a in got), sorted(pos))
                self.assertTrue(all(a["kind"] == "changed" and a["changes"] == [
                    {"property": "expected_date", "before": "2026-11-01",
                     "after": "2026-11-20"}] for a in got))
                self.assertEqual(done["alerts"], n + 1)
                self.assertEqual(len(self.fx.alerts(OTHER)), 1)
                # the same change seen again (a replayed pass) tells nothing new
                self.fx.run(pos)
                self.assertEqual(len(self.fx.alerts()), n)

    def test_a_new_linked_mail_and_a_linked_record_change_are_told(self):
        po = self._orders(1)[0]
        line = self.fx.w.obj("PurchaseOrderLine", {"status": "Open"})
        self.fx.w.link("PurchaseOrderLine.line_of", line, po)
        wl.WatchStore(self.fx.watch).follow(OWNER, po, "PurchaseOrder", None)
        self.fx.run()
        mail = self.fx.w.obj("Email", {"subject": "SYNTH shipment update"})
        self.fx.w.link("Email.mentions_purchase_order", mail, po, "lead")
        set_value(self.fx.w, line, "status", "Received")
        self.fx.run([mail, po, line])
        kinds = {(a["kind"], a["record"]["object_id"]) for a in self.fx.alerts()}
        self.assertEqual(kinds, {("linked", mail), ("linked_changed", line)})
        line_alert = next(a for a in self.fx.alerts() if a["kind"] == "linked_changed")
        self.assertIn("Received", wl.sentence(line_alert))

    def test_a_restricted_reader_sees_only_what_their_grant_admits(self):
        po = self.fx.w.obj("PurchaseOrder", {"status": "Open", "total_cost": "900"},
                           audience="api:buyers")
        self.fx.w.rows["object_values"][-1]["markings"] = json.dumps(["finance"])
        secret = self.fx.w.obj("Email", {"subject": "SYNTH private"}, audience="mail:ceo")
        wl.WatchStore(self.fx.watch).follow(OWNER, po, "PurchaseOrder", None)
        self.fx.run()
        set_value(self.fx.w, po, "status", "Closed")
        set_value(self.fx.w, po, "total_cost", "1200")
        self.fx.w.link("Email.mentions_purchase_order", secret, po, "lead")
        self.fx.run([po, secret])
        full = self.fx.alerts()
        self.assertEqual(len(full), 2)
        buyers = self.fx.alerts(may_read=lambda k: k == "api:buyers", held=())
        # the mail's audience is not theirs: not told; the finance figure is dropped
        self.assertEqual([a["kind"] for a in buyers], ["changed"])
        self.assertEqual([c["property"] for c in buyers[0]["changes"]], ["status"])
        # no grant on the order itself: nothing at all
        self.assertEqual(self.fx.alerts(may_read=lambda k: k == "mail:ceo"), [])
        # an excluded person's mail (the per-item decision) and a failing check: hidden
        refuse_all = lambda hs: {h: "excluded" for h in hs}  # noqa: E731
        self.assertEqual(self.fx.alerts(decide=refuse_all), [])

        def broken(_key):
            raise RuntimeError("grant unavailable")
        self.assertEqual(self.fx.alerts(may_read=broken), [])
        # another person never sees the owner's alerts
        self.assertEqual(self.fx.alerts(OTHER), [])


class Rules(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(Path(tempfile.mkdtemp()))
        self.store = wl.WatchStore(self.fx.watch)

    def _rule(self, text: str, held=("finance",)) -> dict:
        got = wl.compile_rule(text, vocab(), held)
        return self.store.add_rule(OWNER, text, got["spec"], got["description"])

    def test_an_order_over_the_threshold_that_slips_alerts_finance_readers_only(self):
        big = [self.fx.w.obj("SalesOrder", {"order_number": f"SYNTH-SO-{i}",
                                            "required_date": "2026-11-01",
                                            "total_amount": str(60000 + i)})
               for i in range(10)]
        small = self.fx.w.obj("SalesOrder", {"required_date": "2026-11-01",
                                             "total_amount": "49999.99"})
        for row in self.fx.w.rows["object_values"]:
            if row["property"] == "total_amount":
                row["markings"] = json.dumps(["finance"])
        self._rule("tell me when any order over $50k slips")
        self.assertEqual(self.fx.run()["baselined"], 1)
        for oid in big[:7] + [small]:
            set_value(self.fx.w, oid, "required_date", "2026-11-15")
        set_value(self.fx.w, big[7], "required_date", "2026-10-20")   # earlier: no slip
        self.fx.run(big + [small])
        got = self.fx.alerts()
        self.assertEqual(sorted(a["record"]["object_id"] for a in got), sorted(big[:7]))
        self.assertTrue(all(a["origin"] == "rule" for a in got))
        self.assertEqual(self.fx.alerts(held=()), [])   # a reader who lost finance

    def test_a_supplier_late_twice_in_a_month_alerts_once(self):
        supplier = self.fx.w.obj("Supplier", {"name": "SYNTH Supplier"})
        other = self.fx.w.obj("Supplier", {"name": "SYNTH Other"})
        pos = [self.fx.w.obj("PurchaseOrder", {"expected_date": "2026-11-01"})
               for _ in range(4)]
        for po in pos[:3]:
            self.fx.w.link("PurchaseOrder.ordered_from", po, supplier)
        self.fx.w.link("PurchaseOrder.ordered_from", pos[3], other)
        self._rule("alert me if a supplier is late twice in a month", held=())
        self.fx.run()
        set_value(self.fx.w, pos[0], "expected_date", "2026-11-08")
        set_value(self.fx.w, pos[3], "expected_date", "2026-11-08")
        self.fx.run([pos[0], pos[3]])
        self.assertEqual(self.fx.alerts(), [])   # once each: not yet
        self.fx.clock = T + timedelta(days=3)
        set_value(self.fx.w, pos[1], "expected_date", "2026-11-09")
        set_value(self.fx.w, pos[2], "expected_date", "2026-11-09")
        self.fx.run([pos[1], pos[2]])
        got = self.fx.alerts()
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["about"]["object_id"], got[0]["counted"]), (supplier, 3))
        # a reader who may not open one of the counted orders is not told the count
        hidden = {r["audience_key"] for r in self.fx.w.rows["objects"]}
        self.assertEqual(len(hidden), 1)
        self.assertEqual(self.fx.alerts(may_read=lambda k: False), [])


class Hardening(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(Path(tempfile.mkdtemp()))
        self.store = wl.WatchStore(self.fx.watch)

    def test_an_overflowing_number_is_refused_and_a_source_value_of_it_skipped(self):
        for value in ("1e999999999", "-1e-999999999", "1e400", "Infinity", "NaN", "0x10",
                      "9" * 41):
            with self.subTest(value=value):
                with self.assertRaises(wl.RuleRefused) as refused:
                    wl.validate({"schema": wl.RULE_SCHEMA, "subject": "SalesOrder",
                                 "when": [{"property": "total_amount", "op": "gt",
                                           "value": value}]}, vocab(), {"finance"})
                self.assertIn("plain number", refused.exception.problems[0])
        odd = self.fx.w.obj("SalesOrder", {"required_date": "2026-11-01",
                                           "total_amount": "60000"})
        fine = self.fx.w.obj("SalesOrder", {"required_date": "2026-11-01",
                                            "total_amount": "70000"})
        got = wl.compile_rule("tell me when any order over $50k slips", vocab(), {"finance"})
        self.store.add_rule(OWNER, "t", got["spec"], got["description"])
        self.fx.run()
        set_value(self.fx.w, odd, "total_amount", "1e999999999")
        for oid in (odd, fine):
            set_value(self.fx.w, oid, "required_date", "2026-11-20")
        done = self.fx.run([odd, fine])
        self.assertEqual(done["state"], "ran", done)
        self.assertEqual([a["record"]["object_id"] for a in self.fx.alerts()], [fine])

    def test_a_new_rules_first_look_is_paged_and_never_judges_half_read(self):
        orders = [self.fx.w.obj("SalesOrder", {"required_date": "2026-11-01"})
                  for _ in range(10)]
        got = wl.compile_rule("tell me when any order slips", vocab(), set())
        self.store.add_rule(OWNER, "t", got["spec"], got["description"])
        with unittest.mock.patch.multiple(wl, BASELINE_PAGE=4, BASELINE_SECONDS=0.0):
            first = self.fx.run()
            self.assertEqual((first["rules_looking"], first["baselined"]), (1, 0))
            # a record already looked at moves while the look goes on: looked at again,
            # and not told (the rule is not judging yet)
            set_value(self.fx.w, orders[0], "required_date", "2026-11-05")
            self.fx.run([orders[0]])
            self.assertEqual(self.fx.alerts(), [])
            last = self.fx.run()
            self.assertEqual(last["rules_looking"], 0)
        set_value(self.fx.w, orders[0], "required_date", "2026-11-04")   # earlier: no
        set_value(self.fx.w, orders[9], "required_date", "2026-11-09")
        self.fx.run([orders[0], orders[9]])
        self.assertEqual([a["record"]["object_id"] for a in self.fx.alerts()], [orders[9]])

    def test_a_record_narrowed_after_its_alert_hides_the_alert(self):
        po = self.fx.w.obj("PurchaseOrder", {"status": "Open"}, audience="api:buyers")
        self.store.follow(OWNER, po, "PurchaseOrder", None)
        self.fx.run()
        set_value(self.fx.w, po, "status", "Closed")
        self.fx.run([po])
        buyers = lambda k: k == "api:buyers"  # noqa: E731
        self.assertEqual(len(self.fx.alerts(may_read=buyers)), 1)
        next(o for o in self.fx.w.rows["objects"] if o["object_id"] == po)[
            "audience_key"] = "api:board"
        self.fx.store()
        self.fx.last = self.fx.tmp / f"gen{self.fx.n}.sqlite"
        self.assertEqual(self.fx.alerts(may_read=buyers), [])
        self.assertEqual(len(self.fx.alerts(may_read=lambda k: k == "api:board")), 1)


class InThePass(unittest.TestCase):
    def test_the_incremental_pass_tells_a_follower_of_a_new_linked_mail(self):
        tmp = Path(tempfile.mkdtemp())
        w = World()
        po = w.obj("PurchaseOrder", {"po_number": "SYNTH-PO-9", "status": "Open"})

        def build(name: str, digest: str | None = None) -> Path:
            path = tmp / name / "store.sqlite"
            path.parent.mkdir(parents=True)
            w.build(path)
            if digest:
                with sqlite3.connect(path) as conn:
                    conn.execute("UPDATE store_meta SET value = ? WHERE key = "
                                 "'content_digest'", (digest,))
            return path
        before = build("before")
        pe.run(before, pe.inbox_path(before), AS_OF, clock=lambda: T, people=people())
        wl.WatchStore(wl.watch_path(before)).follow(OWNER, po, "PurchaseOrder", None)
        first = pi.run_pass(before, as_of=AS_OF, clock=lambda: T, changed=[],
                            people=people(), wait=True)
        self.assertEqual(first["watchlists"]["baselined"], 1, first)
        with sqlite3.connect(before) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        mail = w.obj("Email", {"subject": "SYNTH where is it"})
        w.link("Email.mentions_purchase_order", mail, po, "lead")
        after = build("after", digest)
        for path in (pe.inbox_path(before), wl.watch_path(before)):
            shutil.copy(path, after.parent / path.name)
        prints = []
        for path in (before, after):
            st = pe._Store(path)
            try:
                prints.append(pi.fingerprints(st, [r[0] for r in st.conn.execute(
                    "SELECT object_id FROM objects")]))
            finally:
                st.close()
        moved = {o for o in set(prints[0]) | set(prints[1])
                 if (prints[0].get(o) or ("-",))[0] != (prints[1].get(o) or ("-",))[0]}
        self.assertEqual(moved, {po, mail})   # a new link moves the followed record too
        done = pi.run_pass(after, as_of=AS_OF, clock=lambda: T, changed=moved, base=before,
                           people=people(), wait=True)
        self.assertEqual(done["watchlists"]["alerts"], 1, done)
        rows = wl.WatchStore(wl.watch_path(after)).raw_alerts(OWNER, 10)
        st = pe._Store(after)
        try:
            got = wl.trim(rows, everyone, (), no_refusals, lambda ids: wl._read(st, ids))
        finally:
            st.close()
        self.assertEqual([(a["kind"], a["record"]["object_id"]) for a in got],
                         [("linked", mail)])

    def test_a_failing_watchlist_never_stops_the_pass(self):
        with unittest.mock.patch.object(wl, "evaluate", side_effect=sqlite3.DatabaseError):
            self.assertEqual(pi._watch(None, Path("x"), set(), lambda: T),
                             {"state": "failed", "error": "DatabaseError"})


class ApiAsTheReader(unittest.TestCase):
    """The /api/watch calls through a reader's view: their grant, their own state."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        w = World()
        self.po = w.obj("PurchaseOrder", {"po_number": "SYNTH-PO-1", "status": "Open"},
                        audience="api:buyers")
        self.ceo = w.obj("PurchaseOrder", {"po_number": "SYNTH-PO-2"}, audience="mail:ceo")
        self.store = w.build(tmp / "served.sqlite")

    def view(self, principal: str, keys=("api:buyers",), held=()):
        access = SimpleNamespace(grant=lambda: ((lambda k: k in keys), frozenset(held)),
                                 decide=no_refusals)
        return SimpleNamespace(actor=Actor("plx", frozenset({principal})), access=access,
                               store_status=SimpleNamespace(store_path=self.store),
                               _request=nullcontext)

    def ctx(self, *args, **kw):
        return watch.view_context(self.view(*args, **kw))

    def test_a_restricted_reader_follows_and_makes_only_what_they_may(self):
        buyer = self.ctx("employee:buyer")
        self.assertTrue(watch.follow(buyer, self.po, True).following)
        for oid in (self.ceo, "PurchaseOrder:fx:999"):   # not theirs, or not there: alike
            with self.assertRaises(UserFacing) as refused:
                watch.follow(buyer, oid, True)
            self.assertEqual(refused.exception.status, 404)
        with self.assertRaises(UserFacing) as refused:
            watch.compile_rule(buyer, "tell me when any order over $50k slips")
        self.assertEqual((refused.exception.status, refused.exception.code),
                         (422, "rule_refused"))
        # a finance spec posted straight past the compile step is refused all the same
        finance = self.ctx("employee:cfo", held=("finance",))
        draft = watch.compile_rule(finance, "tell me when any order over $50k slips")
        with self.assertRaises(UserFacing) as refused:
            watch.create_rule(buyer, draft.text, draft.spec,
                              wl.spec_digest(buyer.reader, draft.spec))
        self.assertEqual(refused.exception.status, 422)
        # the spec shown is the spec kept: another one under its digest is refused
        with self.assertRaises(UserFacing) as refused:
            watch.create_rule(finance, draft.text, {**draft.spec, "when": draft.spec[
                "when"][:1]}, draft.digest)
        self.assertEqual(refused.exception.status, 409)
        rule = watch.create_rule(finance, draft.text, draft.spec, draft.digest)
        # another person sees none of it and cannot delete it
        with self.assertRaises(UserFacing):
            watch.delete_rule(buyer, rule.rule_id)
        mine = watch.state(buyer)
        self.assertEqual(([f.object_id for f in mine.follows], mine.rules),
                         ([self.po], []))
        self.assertEqual([r.rule_id for r in watch.state(finance).rules], [rule.rule_id])
        # a follow whose record the reader's grant no longer admits stays, unlabelled
        later = self.ctx("employee:buyer", keys=())
        self.assertEqual([(f.readable, f.label) for f in watch.state(later).follows],
                         [(False, None)])


class Routes(unittest.TestCase):
    def test_the_watch_routes_answer_by_the_committed_contract(self):
        from operator_api.tests.test_app_api import component, conforms, signed_in
        client = signed_in()
        draft = client.post("/api/watch/rules/compile",
                            json={"text": "alert me if a supplier is late twice in a month"})
        made = client.post("/api/watch/rules", json={k: draft.json()[k] for k in
                                                     ("text", "spec", "digest")})
        follow = client.post("/api/watch/follow",
                             json={"object_id": "PurchaseOrder:demo:90412"})
        listed = client.get("/api/watch")
        read = client.post("/api/watch/alerts/read", json={"alert_ids": ["w_x"]})
        gone = client.delete(f"/api/watch/rules/{made.json()['rule_id']}")
        for response, name in ((draft, "WatchRuleDraft"), (made, "WatchRule"),
                               (follow, "WatchFollow"), (listed, "Watchlist"),
                               (read, "WatchReadResult"), (gone, "WatchRuleDeleted")):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(conforms(response.json(), component(name)), [], name)
        refused = client.post("/api/watch/rules/compile", json={"text": "do my taxes"})
        self.assertEqual((refused.status_code, refused.json()["error"]["code"]),
                         (422, "rule_refused"))


if __name__ == "__main__":
    unittest.main()
