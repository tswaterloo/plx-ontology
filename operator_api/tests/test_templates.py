"""Saved templates (``operator_api.templates``) over a scratch Postgres, through the API:
private by default, shared only with a role of the owner's, the people exclusion fail-closed
on write and read, pay and finance never kept, unknown placeholders refused, placeholders
filled from the records with citations and everything else marked check (never invented),
a draft turned into a template, suggestions when a document of that type is started, the
one-box's "use my ... template for ...", and N=1/10/60 cardinality."""
from __future__ import annotations

import datetime as dt
import unittest

from operating import doc_fill as df
from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

from operator_api import memory as mem
from operator_api import templates as tp
from operator_api.tests.test_memory import EXCLUDED, OID, Guard, reader

TERMS = df.load_terms()


def cite(record, field):
    return df.Citation(record, field, f"api://portal/{record}#{field}")


ORDER = [df.Fact("SalesOrder.customer_po", "PO-7781", cite("SalesOrder:1", "customer_po")),
         df.Fact("Customer.name", "Acme Foods", cite("Customer:9", "name")),
         df.Fact("SalesOrder.order_number", "SO-5512", cite("SalesOrder:1", "order_number"))]


def line(n):
    r = f"SalesOrderLine:{n}"
    return [df.Fact("SalesOrderLine.ordered_quantity", str(n), cite(r, "ordered_quantity")),
            df.Fact("SalesOrderLine.uom", "kg", cite(r, "uom")),
            df.Fact("Product.code", f"AB-{n}", cite(f"Product:{n}", "code")),
            df.Fact("Product.name", "Oat flour", cite(f"Product:{n}", "name"))]


BODY = ("Dear {{customer_name}},\nWe confirm order {{sales_order_number}} on your PO "
        "{{po_number}}. It ships {{ship_date}}.\n{{items}}")


class Pure(unittest.TestCase):
    def test_fill_cites_what_the_records_settle_and_marks_the_rest_check(self):
        got = tp.fill(BODY, ORDER, TERMS, lines=[line(1)], today=dt.date(2026, 11, 14))
        self.assertIn("Dear Acme Foods,", got["text"])
        self.assertIn("order SO-5512 on your PO PO-7781.", got["text"])
        self.assertIn("ships [check: ship date].", got["text"])     # nothing holds it
        self.assertIn("- 1 kg AB-1 Oat flour", got["text"])
        by = {f["name"]: f for f in got["fields"]}
        self.assertEqual(by["po_number"]["citation"]["record"], "SalesOrder:1")
        self.assertEqual(by["ship_date"]["status"], "check")
        self.assertEqual(got["checks"], 1)
        # two values for one field: check, never one picked
        two = ORDER + [df.Fact("Customer.name", "Acme Foods Ltd", cite("Customer:8", "name"))]
        got = tp.fill("{{customer_name}}", two, TERMS)
        self.assertEqual(got["text"], "[check: customer name]")
        # lines not all read: the list is check, never part of it
        got = tp.fill("{{items}}", ORDER, TERMS, lines=None)
        self.assertEqual(got["text"], "[check: items]")
        # no record at all: every placeholder check, nothing invented
        got = tp.fill(BODY, [], TERMS, lines=None)
        self.assertEqual(got["checks"], 5)
        self.assertNotIn("Acme", got["text"])
        # a money value is never filled
        money = [df.Fact("SalesOrder.ship_via", "$40.00", cite("SalesOrder:1", "ship_via"))]
        self.assertEqual(tp.fill("{{SalesOrder.ship_via}}", money, TERMS)["checks"], 1)

    def test_unknown_and_finance_placeholders_are_refused(self):
        for body, code in (("Hi {{customer_nmae}}", "placeholder_unknown"),
                           ("Hi {{ customer name }}", "placeholder_unknown"),
                           ("Total {{SalesOrder.total_amount}}", "finance"),
                           ("Unit {{SalesOrder.unit_price}}", "finance"),
                           ("Line {{SalesOrderLine.line_amount}}", "finance"),
                           ("Freight {{Shipment.freight_charge}}", "finance")):
            with self.assertRaises(tp.TemplateRefused) as caught:
                tp.placeholders(body, TERMS)
            self.assertEqual(caught.exception.code, code, body)

    def test_lines_n_1_10_60(self):
        for n in (1, 10, 60):
            got = tp.fill("{{items}}", ORDER, TERMS, lines=[line(i) for i in range(1, n + 1)])
            rows = got["text"].splitlines()
            shown = [r for r in rows if r.startswith("- ")]
            self.assertEqual(len(shown), min(n, tp.MAX_LINES), n)
            self.assertEqual(len({r for r in shown}), len(shown))       # none duplicated
            if n > tp.MAX_LINES:
                self.assertEqual(rows[-1], f"[check: {n - tp.MAX_LINES} more lines]")

    def test_a_draft_becomes_a_template(self):
        got = tp.propose("Hi Acme Foods, PO PO-7781 is booked as SO-5512. Thanks", ORDER, TERMS)
        self.assertEqual(got["body"], "Hi {{customer_name}}, PO {{po_number}} is booked as "
                                      "{{sales_order_number}}. Thanks")
        self.assertEqual(tp.fill(got["body"], ORDER, TERMS)["text"],
                         "Hi Acme Foods, PO PO-7781 is booked as SO-5512. Thanks")

    def test_the_one_box_line(self):
        from operating.instruct import route
        self.assertEqual(tp.template_request("use my order confirmation template for SO 5512"),
                         {"name": "order confirmation", "for": "SO 5512"})
        self.assertEqual(route("use my order confirmation template for SO 5512"), "do")
        self.assertEqual(route("which template do I use for quotes?"), "ask")
        self.assertIsNone(tp.template_request("where is SO 5512?"))


@unittest.skipIf(psycopg is None, "psycopg not installed")
class Store(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.dsn = pg_testdb.create_db()
        from operating.pg import migrate
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 3})
        cls.memory = mem.Memory.for_dsn(cls.dsn, sync=True)

    @classmethod
    def tearDownClass(cls):
        cls.memory.pool.close()
        pg_testdb.drop_db(cls.db)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DELETE FROM templates.template")
        self.guard = Guard()
        self.memory.guard = lambda: self.guard
        self.memory.names = lambda: frozenset({"quinn harrow", "quinn", "harrow"})
        self.store = tp.TemplateStore(self.memory.pool.connection, self.memory)
        from operator_api.app import create_app
        from operator_api.auth import DevAuth, fixture_identity
        from operator_api.mock import MockBackend
        from fastapi.testclient import TestClient
        self.backend = MockBackend()
        self.backend.memory = self.memory
        self.backend._template_store = self.store
        both = {"purchasing", "quality"}
        self.roles = {"ana": (set(), both), "ben": ({"purchasing"}, both),
                      "cal": ({"quality"}, both)}
        self.backend.template_roles = lambda who: self.roles.get(who.display_name, (set(), set()))
        self.backend.record_facts = lambda who, kind, oid: (
            ORDER, [], self._ref()) if oid == "SalesOrder:1" else (_ for _ in ()).throw(
                KeyError("object_not_found"))
        self.backend._fill_record = lambda who, words, ctx: ("SalesOrder", "SalesOrder:1") \
            if "5512" in (words or "") else None
        self.backend.record_lines = lambda who, kind, oid: [line(1), line(2)]
        self.backend._instruct_guard = lambda: self.memory._guard()
        self.clients = {}
        for who in ("ana", "ben", "cal"):
            app = create_app(self.backend, DevAuth(fixture_identity("plx", [f"staff:{who}"]),
                                                   mode="mock"), secure_cookie=False)
            client = TestClient(app, base_url="http://testserver")
            self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
            self.clients[who] = client

    @staticmethod
    def _ref():
        from operator_api import models as m
        return m.ObjectRef(object_id="SalesOrder:1", type="SalesOrder", label="SO-5512",
                           open_url="/object/SalesOrder/1")

    def as_(self, who):
        c = self.clients[who]
        name = {"ana": "Ana", "ben": "Ben", "cal": "Cal"}[who]
        # the fixture readers' display names are their principals; roles keyed by name
        self.backend.template_roles = lambda r, n=who: self.roles[n]
        return c

    def save(self, who, **body):
        body.setdefault("name", "Order confirmation")
        body.setdefault("doc_type", "order_confirmation")
        body.setdefault("body", BODY)
        return self.as_(who).post("/api/templates", json=body)

    def test_private_by_default_and_shared_with_whom_the_owner_chooses(self):
        made = self.save("ana")
        self.assertEqual(made.status_code, 200, made.text)
        tid = made.json()["id"]
        self.assertEqual([p["name"] for p in made.json()["placeholders"]],
                         ["customer_name", "sales_order_number", "po_number", "ship_date",
                          "items"])
        for other in ("ben", "cal"):
            c = self.as_(other)
            self.assertEqual(c.get("/api/templates").json()["templates"], [])
            self.assertEqual(c.post(f"/api/templates/{tid}/fill",
                                    json={"record": "SO 5512"}).status_code, 404)
            self.assertEqual(c.get("/api/templates/suggest",
                                   params={"doc_type": "order_confirmation"}
                                   ).json()["suggestions"], [])
            self.assertEqual(c.delete(f"/api/templates/{tid}").status_code, 404)
            self.assertEqual(c.put(f"/api/templates/{tid}", json={
                "name": "x", "doc_type": "other", "body": "x"}).status_code, 404)
        self.assertEqual(self.as_("ben").post("/api/templates/propose", json={
            "text": "Hi Acme Foods", "record": "SO 5512"}).json()["body"],
            "Hi {{customer_name}}")
        # sharing: any role there is (the owner need not belong to it); nothing else
        for bad in (["role:payroll_team"], ["purchasing"], ["person:someone@outside.example"],
                    [f"person:{EXCLUDED}"], ["team:all"]):
            r = self.as_("ana").put(f"/api/templates/{tid}", json={
                "name": "Order confirmation", "doc_type": "order_confirmation", "body": BODY,
                "shared_with": bad})
            self.assertEqual(r.status_code, 422, bad)
        ok = self.as_("ana").put(f"/api/templates/{tid}", json={
            "name": "Order confirmation", "doc_type": "order_confirmation", "body": BODY,
            "shared_with": ["role:purchasing"]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["shared_with"], ["role:purchasing"])
        listed = self.as_("ben").get("/api/templates").json()["templates"]
        self.assertEqual([(t["id"], t["mine"], t["source_ref"], t["shared_with"])
                          for t in listed], [(tid, False, None, [])])
        self.assertEqual(self.as_("cal").get("/api/templates").json()["templates"], [])
        filled = self.as_("ben").post(f"/api/templates/{tid}/fill", json={"record": "SO 5512"})
        self.assertEqual(filled.status_code, 200, filled.text)
        self.assertEqual(self.as_("ben").delete(f"/api/templates/{tid}").status_code, 404)
        # the roles cannot be read: nothing shared shows
        self.roles["ben"] = (set(), set())
        self.assertEqual(self.as_("ben").get("/api/templates").json()["templates"], [])
        self.assertEqual(len(self.as_("ana").get("/api/templates").json()["templates"]), 1)

    def test_shared_with_a_person_by_address(self):
        ana, ben, cal = reader("ana"), reader("ben"), reader("cal")
        row = self.store.save(ana, name="Chase", doc_type="supplier_email",
                              body="Following up on {{po_number}}",
                              shared_with=["person:Ben@plx.example", "person:ana@plx.example"])
        self.assertEqual(row["shared_with"], ["person:ben@plx.example"])   # never oneself
        self.assertEqual([t["id"] for t in self.store.list(ben)], [row["id"]])
        self.assertEqual(self.store.list(cal), [])
        self.assertIsNone(self.store.get(cal, row["id"]))
        with self.assertRaises(tp.TemplateRefused) as caught:
            self.store.save(ben, name="Chase", doc_type="supplier_email", body="x",
                            template_id=row["id"])
        self.assertEqual(caught.exception.code, "not_found")
        # an excluded person or a name the exclusion keeps out: refused, nothing kept
        for who in (f"person:{EXCLUDED}", "person:quinn@plx.example"):
            self.guard.is_excluded = (lambda v, w=who[7:]: str(v).lower() in (OID, EXCLUDED, w))
            with self.assertRaises(tp.TemplateRefused) as caught:
                self.store.save(ana, name="Other", doc_type="other", body="x", shared_with=[who])
            self.assertEqual(caught.exception.code, "share_refused")
        # Ben excluded since: he reads nothing, the owner no longer sees him, purge drops him
        self.guard.is_excluded = lambda v: str(v).lower() in (OID, EXCLUDED, "ben@plx.example")
        self.assertIsNone(self.store.list(ben))
        self.assertEqual(self.store.list(ana)[0]["shared_with"], [])
        self.assertEqual(self.store.purge_excluded(), 0)
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT shared_with FROM templates.template"
                                          ).fetchone()[0], [])

    def test_filled_from_the_record_with_citations_and_check_for_the_rest(self):
        tid = self.save("ana").json()["id"]
        got = self.as_("ana").post(f"/api/templates/{tid}/fill", json={"record": "SO 5512"})
        self.assertEqual(got.status_code, 200, got.text)
        out = got.json()
        self.assertIn("PO PO-7781", out["text"])
        self.assertIn("ships [check: ship date]", out["text"])
        self.assertIn("- 2 kg AB-2 Oat flour", out["text"])
        by = {f["name"]: f for f in out["fields"]}
        self.assertEqual(by["po_number"]["citation"]["source"],
                         "api://portal/SalesOrder:1#customer_po")
        self.assertTrue(by["po_number"]["citation"]["open_url"].startswith("/source?handle="))
        self.assertEqual((by["ship_date"]["status"], by["ship_date"]["value"]), ("check", ""))
        self.assertEqual(out["checks"], 1)
        # no record named: nothing invented
        bare = self.as_("ana").post(f"/api/templates/{tid}/fill", json={}).json()
        self.assertEqual(bare["checks"], 5)
        # a record the reader cannot open
        self.backend._fill_record = lambda who, words, ctx: ("SalesOrder", "SalesOrder:2")
        self.assertEqual(self.as_("ana").post(f"/api/templates/{tid}/fill",
                                              json={"record": "SO 9"}).status_code, 404)
        listed = self.as_("ana").get("/api/templates").json()["templates"]
        self.assertEqual(listed[0]["uses"], 2)

    def test_pay_finance_and_unknown_placeholders_are_never_kept(self):
        for body in ("Price is $40.00 per kg {{po_number}}", "Your payroll run {{po_number}}",
                     "Net 30 payment terms apply."):
            r = self.save("ana", body=body)
            self.assertEqual(r.status_code, 422, body)
        self.assertEqual(self.save("ana", style_notes="Mention the discount").status_code, 422)
        self.assertEqual(self.save("ana", name="Invoice total").status_code, 422)
        r = self.save("ana", body="Hi {{customer_nmae}}")
        self.assertEqual(r.status_code, 422)
        self.assertIn("customer_nmae", r.json()["error"]["message"])
        self.assertEqual(self.save("ana", doc_type="payroll").status_code, 422)
        with psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM templates.template").fetchone()[0], 0)

    def test_the_people_exclusion_fails_closed(self):
        tid = self.save("ana").json()["id"]
        # unavailable: nothing written, nothing read
        self.guard.unavailable = "not loaded"
        self.assertEqual(self.save("ana", name="Other").status_code, 503)
        self.assertFalse(self.as_("ana").get("/api/templates").json()["available"])
        self.assertEqual(self.as_("ana").post(f"/api/templates/{tid}/fill",
                                              json={}).status_code, 404)
        self.assertIsNone(self.store.purge_excluded())
        self.guard.unavailable = None
        # a text naming an excluded person (address, object id, name): refused
        for body in (f"Copy {EXCLUDED} on this {{{{po_number}}}}", f"Ask {OID}",
                     "Check with Quinn Harrow first"):
            self.assertEqual(self.save("ana", name="X", body=body).status_code, 422, body)
        # an excluded person saves nothing
        x = mem.KeyReader(f"employee:{OID}")
        with self.assertRaises(tp.TemplateRefused):
            self.store.save(x, name="Mine", doc_type="other", body="Hello")
        # excluded since: hidden at once, purged
        self.save("ana", name="Later", body="Copy later@plx.example please")
        self.assertEqual(len(self.as_("ana").get("/api/templates").json()["templates"]), 2)
        self.guard.is_excluded = lambda v: str(v).lower() in (OID, EXCLUDED, "later@plx.example")
        names = [t["name"] for t in self.as_("ana").get("/api/templates").json()["templates"]]
        self.assertEqual(names, ["Order confirmation"])
        self.assertEqual(self.store.purge_excluded(), 1)
        # a view-as session reads and writes nothing
        viewing = reader("ana")
        viewing.session.view_as = True
        self.assertIsNone(self.store.list(viewing))
        with self.assertRaises(tp.TemplateRefused):
            self.store.save(viewing, name="V", doc_type="other", body="Hello")

    def test_suggested_when_that_kind_of_document_starts(self):
        a = self.save("ana").json()["id"]
        self.save("ana", name="Quote short", doc_type="quote", body="Hello {{customer_name}}")
        self.as_("ana").post(f"/api/templates/{a}/fill", json={})
        got = self.as_("ana").get("/api/templates/suggest",
                                  params={"doc_type": "order_confirmation"}).json()
        self.assertEqual([(s["kind"], s["label"]) for s in got["suggestions"]],
                         [("saved", "Order confirmation")])
        self.assertIn("used 1 times", got["suggestions"][0]["detail"])
        self.assertEqual(self.as_("ana").get("/api/templates/suggest",
                                             params={"doc_type": "nope"}).status_code, 422)

    def test_use_my_template_in_the_one_box(self):
        self.save("ana")
        said = self.backend.template_answer(
            reader("ana"), "use my order confirmation template for SO 5512", {}, "c1", 5)
        self.assertIsNone(said)     # reader("ana") is not the fixture reader: not theirs
        from operator_api.auth import fixture_identity  # noqa: F401
        c = self.as_("ana")
        r = c.post("/api/ask", json={"question": "use my order confirmation template for SO 5512"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("event: answer", r.text)
        self.assertIn("PO-7781", r.text)
        self.assertIn("[check: ship date]", r.text)
        # no template by that name: worked as any other instruction (no template part)
        self.assertIsNone(self.backend.template_answer(
            reader("ana"), "use my supplier chase template for SO 5512", {}, "c1", 5))

    def test_listing_n_1_10_60(self):
        ana = self.clients["ana"]
        made = 0
        for n in (1, 10, 60):
            while made < n:
                made += 1
                r = self.save("ana", name=f"T{made}", body=f"Note {made} {{{{po_number}}}}")
                self.assertEqual(r.status_code, 200, r.text)
            rows = ana.get("/api/templates").json()["templates"]
            self.assertEqual(sorted(t["name"] for t in rows),
                             sorted(f"T{i}" for i in range(1, n + 1)))


if __name__ == "__main__":
    unittest.main()
