"""plx.table on Postgres: one reader-scoped query per request.

A store with invoices in two currencies, one Business Central invoice a narrow reader may
not open, finance-marked records, a finance-marked amount cell, a hidden customer, a mail
capture typed as an invoice, a lead link and an ambiguous link, and customers with 1, 10
and 60 invoices, loaded into a throwaway Postgres database. Checked at the tool's
boundary: what each reader sees through every request shape, partial answers, currency
splits, exact counts and sums, refusals, the read-only transaction, the output contract,
and what the answer gate binds."""
from __future__ import annotations

import json
import unittest
from decimal import Decimal
from unittest import mock

from ontology.json_contract import validate
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_table as tt
from operating.claim_checks_values import returned_evidence
from tests import test_ontology_tools_evidence as ev

CO = ev.CO


def _api(rel, key):
    return f"api://public.{rel}/%7B%22id%22%3A%22{key}%22%7D@0000000000000001-1"


def _type(tid, props, bindings, **kw):
    return ev._type(tid, props, bindings, **kw)


DOMAIN = {
    "format": "plx-ontology-domain/v1", "domain": "table", "origin": "curated",
    "description": "plx.table fixture.",
    "object_types": [
        _type("SalesInvoice", [("invoice_number", "text"), ("invoice_date", "date"),
                               ("total_amount", "money"), ("status", "enum"),
                               ("posted_at", "timestamp")],
              [("portal.inv", "portal_api"), ("bc.inv", "bc")], display=["invoice_number"]),
        _type("Customer", [("name", "text")], [("portal.cust", "portal_api")],
              display=["name"]),
        _type("Email", [("subject", "text")], [("graph_mail.messages", "graph_mail")],
              display=["subject"], interfaces=["Document"]),
    ],
    "link_types": [ev._link("SalesInvoice.billed_to", "SalesInvoice", "Customer"),
                   ev._link("SalesInvoice.same_record_as", "SalesInvoice", "SalesInvoice",
                            "same_record")],
}
for _spec in DOMAIN["object_types"]:   # each binding reads its own source relation
    for _binding in _spec["bindings"]:
        _binding["relation"] = _binding["id"]
DOMAIN["link_types"][0].update(name="billed_to", cardinality="N:1")
DOMAIN["link_types"][1].update(name="same_record_as", cardinality="1:1")
SCHEMA = ot.SchemaView.from_documents([DOMAIN])
POLICY = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops", "full"},
                                                 "bc:*": {"full"},
                                                 "mail:*": {"full"}},
                       markings={"finance": {"full"}})
FULL = Actor("plx", frozenset({"full"}))
OPS = Actor("plx", frozenset({"ops"}))
NOBODY = Actor("plx", frozenset({"nobody"}))

# Records OPS may never learn anything from (ids, handles, names, amounts).
SECRET = ("SECRET", "inv-bc", "inv-fin", "cust-hidden", "Hidden Co", "mail-inv", "777.77",
          "4444.44", "5555.55")


class Store(ev.Store):
    def val(self, oid, prop, value_type, value, *, currency=None, markings=None):
        handle, lineage, marks = self.handles[oid]
        self.conn.execute(
            "INSERT INTO object_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, prop, value_type, value, None, None, currency, None, None, handle,
             f"row.{prop}", ev._h(f"{handle}|{prop}|{value}"), lineage, oid.split(":")[1],
             "as_is", 0, markings or marks, ev.T0, None))


def inv(s, key, number, date, amount, currency, customer=None, *, binding="portal.inv",
        audience="api:public.inv", handle=None, markings="[]", amount_markings=None,
        tier="verified"):
    oid = f"SalesInvoice:{binding}:{key}"
    s.obj(oid, audience, handle or _api("inv", key), "gold_etl_from_fm", markings=markings)
    s.val(oid, "invoice_number", "text", number)
    s.val(oid, "invoice_date", "date", date)
    s.val(oid, "total_amount", "money", amount, currency=currency, markings=amount_markings)
    s.val(oid, "status", "enum", "Open")
    if customer:
        s.link(f"l-{key}", "SalesInvoice.billed_to", oid, customer, tier)
    return oid


def cust(s, key, name, markings="[]"):
    oid = f"Customer:portal.cust:{key}"
    s.obj(oid, "api:public.cust", _api("cust", key), "gold_etl_from_fm", markings=markings)
    s.val(oid, "name", "text", name)
    return oid


def removed(s, oid, basis="source_soft_delete", since=ev.T0, until=None, *, mark=False):
    """The source stopped holding ``oid``: a removal stretch, or (``mark``) only the
    record's missing mark (a missing record with no removal row)."""
    if mark:
        s.conn.execute("UPDATE objects SET missing_from_source_since = ? WHERE object_id = ?",
                       (since, oid))
    else:
        s.conn.execute("INSERT INTO object_removals VALUES (?,?,?,?,?,?)",
                       (oid, basis, since, until, None, None))
    return oid


def build():
    s = Store()
    acme, beta = cust(s, "acme", "Acme Ltd"), cust(s, "beta", "Beta Inc")
    hidden = cust(s, "cust-hidden", "Hidden Co", markings='["finance"]')
    inv(s, "i1", "INV-1", "2026-01-10", "100.10", "CAD", acme)
    inv(s, "i2", "INV-2", "2026-02-11", "200.20", "CAD", acme)
    inv(s, "i3", "INV-3", "2026-02-12", "300.00", "USD", acme)
    inv(s, "i4", "INV-4", "2025-12-30", "50.00", "CAD", beta)
    inv(s, "inv-bc", "SECRET-BC", "2026-01-15", "777.77", "CAD", acme, binding="bc.inv",
        audience=f"bc:{CO}/salesInvoices",
        handle=ev.make_handle(ev.BC_KIND, ("production", CO, "salesInvoices", "b1"),
                              "00000000000000a1", 1))
    inv(s, "inv-fin", "SECRET-FIN", "2026-01-16", "4444.44", "CAD", acme,
        markings='["finance"]')
    inv(s, "i-amt", "INV-AMT", "2026-01-17", "5555.55", "CAD", beta,
        amount_markings='["finance"]')
    inv(s, "i-hid", "INV-HID", "2026-01-18", "60.00", "CAD", hidden)
    inv(s, "i-lead", "INV-LEAD", "2026-01-19", "70.00", "CAD", beta, tier="lead")
    amb = inv(s, "i-amb", "INV-AMB", "2026-01-20", "80.00", "CAD", acme)
    s.link("l-amb2", "SalesInvoice.billed_to", amb, beta, "verified")
    # a mail capture typed as an invoice: an item-ACL scheme, never counted
    inv(s, "mail-inv", "SECRET-MAIL", "2026-01-21", "999.99", "CAD", acme,
        binding="graph_mail.messages", audience="mail:mbx-a",
        handle="graph-mail://mbx-a/mail-inv@ck1")
    # amounts whose records state no currency (a BC aged row's): one CAD, one USD in truth
    nocur, solo = cust(s, "nocur", "Nocur Co"), cust(s, "solo", "Solo Co")
    inv(s, "nc1", "NC-1", "2023-03-01", "10.00", None, nocur)
    inv(s, "nc2", "NC-2", "2023-03-02", "20.00", None, nocur)
    inv(s, "so1", "SO-1", "2023-03-03", "5.00", None, solo)
    # one invoice held in two systems (portal + BC, verified identity link), a lead-tier
    # pair (two records), and a chain portal - BC - portal (one record for a reader of BC,
    # two for one who may not read the BC middle)
    twin = cust(s, "twin", "Twin Co")

    def bc_inv(key, number, date, amount):
        return inv(s, key, number, date, amount, "CAD", twin, binding="bc.inv",
                   audience=f"bc:{CO}/salesInvoices",
                   handle=ev.make_handle(ev.BC_KIND, ("production", CO, "salesInvoices", key),
                                         "00000000000000a2", 1))
    tp, tb = inv(s, "tw-p", "TW-1", "2023-04-01", "100.00", "CAD", twin), \
        bc_inv("tw-b", "SECRET-TW1", "2023-04-01", "101.00")
    s.link("l-tw1", "SalesInvoice.same_record_as", tp, tb, "verified")
    l1, l2 = (inv(s, "tw-l1", "TW-2", "2023-04-02", "50.00", "CAD", twin),
              inv(s, "tw-l2", "TW-2B", "2023-04-02", "50.00", "CAD", twin))
    s.link("l-tw2", "SalesInvoice.same_record_as", l1, l2, "lead")
    c1, c2, c3 = (inv(s, "tw-c1", "TW-3", "2023-04-03", "30.00", "CAD", twin),
                  bc_inv("tw-c2", "SECRET-TW3", "2023-04-03", "31.00"),
                  inv(s, "tw-c3", "TW-3P", "2023-04-03", "30.00", "CAD", twin))
    s.link("l-tw3", "SalesInvoice.same_record_as", c1, c2, "verified")
    s.link("l-tw4", "SalesInvoice.same_record_as", c3, c2, "verified")
    # a customer with a credit among its invoices (an amount below zero)
    credit = cust(s, "credit", "Credit Co")
    inv(s, "cr1", "CR-1", "2022-05-01", "-15.00", "CAD", credit)
    inv(s, "cr2", "CR-2", "2022-05-02", "25.00", "CAD", credit)
    # records their source no longer holds (soft deleted, missing, a removal of unknown
    # time) beside ones it holds again or not yet removed: only the held ones count
    kept = cust(s, "kept", "Kept Co")
    inv(s, "dl1", "DL-1", "2021-01-04", "10.00", "CAD", kept)
    removed(s, inv(s, "dl2", "DL-2", "2021-01-05", "20.00", "CAD", kept))
    removed(s, inv(s, "dl3", "DL-3", "2021-01-06", "40.00", "CAD", kept), mark=True)
    removed(s, inv(s, "dl4", "DL-4", "2021-01-07", "80.00", "CAD", kept),
            "missing_from_source", "2099-01-02T00:00:00Z", "2099-01-03T00:00:00Z")
    removed(s, inv(s, "dl5", "DL-5", "2021-01-08", "160.00", "CAD", kept),
            "missing_from_source", "not a time")
    removed(s, inv(s, "dl6", "DL-6", "2021-01-09", "320.00", "CAD", kept),
            since="2099-12-01T00:00:00Z")
    # a customer its source deleted: no group of its own; its held invoice is in the group
    # with no customer
    gone = removed(s, cust(s, "gone", "Gone Co"), "missing_from_source", mark=True)
    inv(s, "gn1", "GN-1", "2021-02-01", "5.00", "CAD", gone)
    # a portal invoice whose BC copy the source deleted: one member, never merged with it
    dt = cust(s, "dt", "Dt Co")
    dtp = inv(s, "dt-p", "DT-1", "2021-03-01", "7.00", "CAD", dt)
    dtb = removed(s, inv(s, "dt-b", "DT-1B", "2021-03-01", "9.00", "CAD", dt,
                         binding="bc.inv", audience=f"bc:{CO}/salesInvoices",
                         handle=ev.make_handle(ev.BC_KIND, ("production", CO,
                                                            "salesInvoices", "dt-b"),
                                               "00000000000000a3", 1)))
    s.link("l-dt", "SalesInvoice.same_record_as", dtp, dtb, "verified")
    # a portal - BC - portal chain whose BC middle its source deleted: the two portal
    # copies may be one invoice, so their count is no exact one
    mid = cust(s, "mid", "Mid Co")
    m1, m3 = (inv(s, "mid-1", "MD-1", "2020-05-01", "3.00", "CAD", mid),
              inv(s, "mid-3", "MD-1P", "2020-05-01", "3.00", "CAD", mid))
    m2 = removed(s, inv(s, "mid-2", "MD-1B", "2020-05-01", "3.00", "CAD", mid,
                        binding="bc.inv", audience=f"bc:{CO}/salesInvoices",
                        handle=ev.make_handle(ev.BC_KIND, ("production", CO,
                                                           "salesInvoices", "mid-2"),
                                              "00000000000000a4", 1)))
    s.link("l-mid1", "SalesInvoice.same_record_as", m1, m2, "verified")
    s.link("l-mid3", "SalesInvoice.same_record_as", m3, m2, "verified")
    # an invoice date that is no calendar day
    inv(s, "bad-date", "BD-1", "2024-13-45", "1.00", "CAD", cust(s, "bd", "Bad Date Co"))
    for n in (1, 10, 60):
        c = cust(s, f"bulk{n}", f"Bulk {n}")
        for k in range(n):
            inv(s, f"b{n}-{k}", f"B{n}-{k}", "2024-06-01", f"{k}.25", "USD", c)
    s.conn.commit()
    return s


class TableTool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_store_mode
        cls.store = build()
        cls.dsn = pg_store_mode._load("", cls.store.conn)

    def tool(self):
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.store.conn))
        base = ot.OntologyTools(reader, POLICY, item_gate=lambda a, h: True, schema=SCHEMA,
                                grammar=None, contracts=ev.CONTRACTS, clock=lambda: ev.NOW)
        return tt.TableTool(base)

    def call(self, args, actor=FULL, tool=None):
        out = (tool or self.tool()).call("plx.table", args, actor)
        errors = validate(out, tt._CONTRACT["output"], root=ev.CONTRACTS)
        self.assertEqual(errors, [], json.dumps(out)[:1500])
        return out

    @staticmethod
    def keys(out, name):
        return [(g["key"][name]["untrusted_text"] if isinstance(g["key"][name], dict)
                 else g["key"][name]) for g in out["groups"]]

    @staticmethod
    def by(out, name, measure=0):
        return {(g["key"][name]["untrusted_text"] if isinstance(g["key"][name], dict)
                 else g["key"][name], g["measures"][measure].get("currency")):
                g["measures"][measure]["value"] for g in out["groups"]}

    # ---- permissions -----------------------------------------------------------

    def test_restricted_reader_sees_no_other_row_through_any_request_shape(self):
        tool = self.tool()
        shapes = [
            {"from": "SalesInvoice", "measures": [{"fn": "count"}]},
            {"from": "SalesInvoice", "measures": [{"fn": "sum", "field": "total_amount"}]},
            {"from": "SalesInvoice", "group_by": ["billed_to.name"],
             "measures": [{"fn": "sum", "field": "total_amount"}, {"fn": "count"}]},
            {"from": "SalesInvoice", "group_by": ["billed_to.object_id"]},
            {"from": "SalesInvoice", "group_by": ["system", "invoice_number"], "limit": 200},
            {"from": "SalesInvoice", "group_by": [{"field": "invoice_date", "bucket": "month"}],
             "measures": [{"fn": "max", "field": "total_amount"},
                          {"fn": "count_distinct", "field": "billed_to.name"}]},
            {"from": "SalesInvoice", "select": ["invoice_number", "total_amount",
                                                 "billed_to.name"], "limit": 200},
            {"from": "SalesInvoice", "where": [{"field": "billed_to.name", "op": "contains",
                                                "value": "Hidden"}],
             "select": ["invoice_number"]},
            {"from": "SalesInvoice", "where": [{"field": "invoice_number", "op": "starts_with",
                                                "value": "SECRET"}],
             "measures": [{"fn": "count"}]},
            {"from": "SalesInvoice", "where": [{"field": "total_amount", "op": ">",
                                                "value": 700}],
             "select": ["invoice_number"]},
            {"from": "Customer", "group_by": ["name"]},
            {"from": "Customer", "select": ["name", "object_id"], "limit": 200},
        ]
        for args in shapes:
            with self.subTest(args=args):
                text = json.dumps(self.call(args, OPS, tool))
                for secret in SECRET:
                    self.assertNotIn(secret, text)
        # the control: the full reader does see them
        text = json.dumps(self.call({"from": "SalesInvoice", "select": [
            "invoice_number", "billed_to.name"], "limit": 200}, FULL, tool))
        for secret in ("SECRET-BC", "SECRET-FIN", "Hidden Co"):
            self.assertIn(secret, text)

    def test_counts_per_reader(self):
        tool = self.tool()
        count = {"from": "SalesInvoice", "measures": [{"fn": "count"}]}
        full = self.call(count, FULL, tool)
        ops = self.call(count, OPS, tool)
        # 8 portal + 3 with no currency + 1 BC + 1 finance-marked + Credit Co's 2 + 71 bulk
        # + Twin Co's 7 records (4 for the full reader, its twins merged; 5 portal ones for
        # OPS) + 8 held removal-fixture ones (Kept Co's 3, GN-1, DT-1, BD-1, Mid Co's 2);
        # never the mail capture nor a record its source no longer holds
        self.assertEqual(full["groups"][0]["measures"][0]["value"], str(15 + 71 + 4 + 8))
        self.assertEqual(ops["groups"][0]["measures"][0]["value"], str(13 + 71 + 5 + 8))
        # OPS: nothing withheld; Twin Co's chain through a BC copy it may not read may
        # count one invoice twice, so the count is partial (never bound as exact)
        self.assertNotIn("withheld", ops)
        self.assertIs(ops["partial"], True)
        self.assertEqual([g["reason"] for g in ops["gaps"] if g["reason"] == "twins_unresolved"],
                         ["twins_unresolved"])
        self.assertEqual(self.call(count, NOBODY, tool)["groups"][0]["measures"][0]["value"],
                         "0")

    def test_withheld_value_or_linked_record_makes_the_answer_partial(self):
        tool = self.tool()
        total = self.call({"from": "SalesInvoice", "where": [
            {"field": "invoice_date", "op": ">=", "value": "2025-01-01"}],
            "measures": [{"fn": "sum", "field": "total_amount"}]}, OPS, tool)
        self.assertIs(total["partial"], True)
        self.assertEqual(total["withheld"]["objects"], 1)          # i-amt's amount cell
        sums = {g["measures"][0]["currency"]: g["measures"][0]["value"]
                for g in total["groups"]}
        self.assertEqual(sums, {"CAD": "560.3", "USD": "300"})
        named = self.call({"from": "SalesInvoice", "group_by": ["billed_to.name"]}, OPS, tool)
        self.assertIs(named["partial"], True)                      # i-hid's customer
        self.assertNotIn("Hidden Co", self.keys(named, "billed_to.name"))
        # (Mid Co's chain, broken at a deleted middle, is partial on its own: 2020)
        full = self.call({"from": "SalesInvoice", "where": [
            {"field": "invoice_date", "op": ">=", "value": "2021-01-01"}],
            "group_by": ["billed_to.name"]}, FULL, tool)
        self.assertIs(full["partial"], False)
        self.assertNotIn("withheld", full)

    def test_item_acl_types_and_unknown_requests_fail_closed(self):
        tool = self.tool()
        bad = [
            ({"from": "Email", "measures": [{"fn": "count"}]}, "not_served"),
            ({"from": "Nope", "measures": [{"fn": "count"}]}, "unknown_type"),
            ({"from": "SalesInvoice", "group_by": ["nope"]}, "unknown_property"),
            ({"from": "SalesInvoice", "group_by": ["nope.name"]}, "undeclared_link"),
            ({"from": "SalesInvoice", "group_by": ["billed_to.nope"]}, "unknown_property"),
            ({"from": "SalesInvoice", "group_by": ["billed_to.name.x"]}, "invalid_input"),
            ({"from": "SalesInvoice", "group_by": ["same_record_as.invoice_number"]},
             "invalid_input"),
            ({"from": "SalesInvoice", "where": [{"field": "status", "op": "~", "value": "x"}],
              "measures": [{"fn": "count"}]}, "invalid_input"),
            ({"from": "SalesInvoice", "measures": [{"fn": "sum", "field": "status"}]},
             "invalid_input"),
            ({"from": "SalesInvoice", "measures": [{"fn": "median", "field": "status"}]},
             "invalid_input"),
            ({"from": "SalesInvoice", "group_by": [{"field": "status", "bucket": "month"}]},
             "invalid_input"),
            ({"from": "SalesInvoice", "measures": [{"fn": "count"}], "sql": "DROP"},
             "invalid_input"),
            ({"from": "SalesInvoice", "measures": [{"fn": "count"}], "limit": 0},
             "invalid_input"),
            ({"from": "SalesInvoice"}, "invalid_input"),
            ({"from": "SalesInvoice", "select": ["status"], "measures": [{"fn": "count"}]},
             "invalid_input"),
            ({"from": "SalesInvoice", "group_by": ["status"],
              "order_by": [{"by": "m9"}]}, "invalid_input"),
            ({"from": "SalesInvoice", "where": [{"field": "total_amount", "op": ">",
                                                 "value": "lots"}],
              "measures": [{"fn": "count"}]}, "invalid_input"),
        ]
        for args, code in bad:
            with self.subTest(args=args):
                self.assertEqual(self.call(args, FULL, tool)["error"]["code"], code)
        # a store that is not Postgres is refused, never read part way
        base = ot.OntologyTools(ot.StoreReader(self.store.conn), POLICY, schema=SCHEMA,
                                grammar=None, contracts=ev.CONTRACTS, clock=lambda: ev.NOW)
        self.assertEqual(tt.TableTool(base).call("plx.table", {"from": "SalesInvoice",
                         "measures": [{"fn": "count"}]}, FULL)["error"]["code"], "not_served")

    def test_values_are_bound_never_spliced(self):
        tool = self.tool()
        out = self.call({"from": "Customer", "where": [
            {"field": "name", "op": "=", "value": "x' OR '1'='1"}], "measures": [
            {"fn": "count"}]}, FULL, tool)
        self.assertEqual(out["groups"][0]["measures"][0]["value"], "0")
        out = self.call({"from": "Customer", "where": [
            {"field": "name", "op": "contains", "value": "%"}], "measures": [
            {"fn": "count"}]}, FULL, tool)
        self.assertEqual(out["groups"][0]["measures"][0]["value"], "0")

    def test_the_statement_runs_read_only(self):
        tool = self.tool()
        with mock.patch.object(tt, "_compile", return_value=(
                "DELETE FROM objects RETURNING '{}'::text", {})):
            out = tool.call("plx.table", {"from": "SalesInvoice",
                                          "measures": [{"fn": "count"}]}, FULL)
        self.assertEqual(out["error"]["code"], "query_failed")
        self.assertEqual(self.call({"from": "Customer", "measures": [{"fn": "count"}]})[
            "groups"][0]["measures"][0]["value"], "14")     # nothing deleted

    # ---- money, links, cardinality ---------------------------------------------

    def test_money_is_split_by_currency_and_links_resolve_conservatively(self):
        out = self.call({"from": "SalesInvoice", "where": [
            {"field": "invoice_date", "op": "between", "value": ["2026-01-01", "2026-02-28"]}],
            "group_by": ["billed_to.name"], "measures": [
            {"fn": "sum", "field": "total_amount"}, {"fn": "count"}],
            "order_by": [{"by": "billed_to.name", "dir": "asc"}]}, FULL)
        sums = self.by(out, "billed_to.name")
        self.assertEqual(sums[("Acme Ltd", "CAD")], "5522.51")   # 100.10+200.20+777.77+4444.44
        self.assertEqual(sums[("Acme Ltd", "USD")], "300")
        self.assertEqual(sums[("Beta Inc", "CAD")], "5555.55")
        # the lead link and the two-customer link give no key; the latter is a gap
        self.assertEqual(sums[(None, "CAD")], "150")
        self.assertIn("ambiguous_link", [g["reason"] for g in out["gaps"]])
        for g in out["groups"]:
            self.assertRegex(g["measures"][0]["currency"], "^[A-Z]{3}$")
            self.assertEqual(len(g["members_cited"]), int(g["members"]))

    def test_amounts_with_no_stated_currency_are_never_combined(self):
        args = {"from": "SalesInvoice", "where": [
            {"field": "invoice_date", "op": "between", "value": ["2023-03-01", "2023-03-31"]}],
            "group_by": ["billed_to.name"], "measures": [
            {"fn": "sum", "field": "total_amount"}, {"fn": "max", "field": "total_amount"},
            {"fn": "avg", "field": "total_amount"}, {"fn": "count"}],
            "order_by": [{"by": "billed_to.name", "dir": "asc"}]}
        out = self.call(args, FULL)
        nocur, solo = out["groups"]
        # two records with no currency: no sum, maximum or average (they may be CAD + USD)
        for m in nocur["measures"][:3]:
            self.assertIsNone(m["value"])
            self.assertEqual((m["currency_basis"], m["inputs"]), ("not_stated", 2))
        self.assertEqual(nocur["measures"][3]["value"], "2")
        # one record's own amount stands, said to state no currency
        self.assertEqual([m["value"] for m in solo["measures"]], ["5", "5", "5", "1"])
        self.assertEqual(solo["measures"][0]["currency_basis"], "not_stated")
        self.assertIn("currency_unknown", [g["reason"] for g in out["gaps"]])
        # the gate binds no combined sum, and nothing as complete past the gap
        bound = returned_evidence("plx.table", args, out)["counts"]
        self.assertEqual([e["value"] for e in bound if e.get("op") == "sum"], ["5"])
        self.assertEqual(sorted(e["value"] for e in bound if "op" not in e), ["1", "2"])
        self.assertFalse(any(e["complete"] for e in bound))

    def test_a_record_held_in_two_systems_counts_once(self):
        def twin_co(actor, **kw):
            args = {"from": "SalesInvoice", "where": [
                {"field": "billed_to.name", "op": "=", "value": "Twin Co"}],
                "group_by": kw.pop("group_by", ["billed_to.name"]),
                "measures": [{"fn": "count"}, {"fn": "sum", "field": "total_amount"}], **kw}
            out = self.call(args, actor)
            return args, out, {(self.keys(out, args["group_by"][0])[i]):
                               (g["members"], g.get("twins", 0),
                                [m["value"] for m in g["measures"]])
                               for i, g in enumerate(out["groups"])}
        # the portal + BC pair once, the lead pair twice, the portal - BC - portal chain once
        args, out, got = twin_co(FULL)
        self.assertEqual(got, {"Twin Co": (4, 3, ["4", "230"])})
        self.assertEqual(len(out["groups"][0]["members_cited"]), 4)
        bound = returned_evidence("plx.table", args, out)["counts"]
        self.assertEqual([(e["value"], e["complete"]) for e in bound if "op" not in e],
                         [("4", True)])
        # a reader who may not read the BC copies: never merged through them, nothing of
        # them shown
        # them shown; the chain broken at its middle may count one invoice twice, so the
        # answer is partial and the gate binds nothing of it
        args, out, got = twin_co(OPS)
        self.assertEqual(got, {"Twin Co": (5, 0, ["5", "260"])})
        self.assertNotIn("SECRET", json.dumps(out))
        self.assertIs(out["partial"], True)
        self.assertIn("twins_unresolved", [g["reason"] for g in out["gaps"]])
        self.assertTrue(all(not e["complete"] for e in
                            returned_evidence("plx.table", args, out)["counts"]))
        # every copy when asked; within a group only (by system, the chain's two portal
        # copies are one)
        self.assertEqual(twin_co(FULL, merge_twins=False)[2],
                         {"Twin Co": (7, 0, ["7", "392"])})
        self.assertEqual(twin_co(FULL, group_by=["system"])[2],
                         {"portal": (4, 1, ["4", "230"]), "bc": (2, 0, ["2", "132"])})
        self.assertEqual(self.tool().call("plx.table", {"from": "SalesInvoice", "measures": [
            {"fn": "count"}], "merge_twins": "yes"}, FULL)["error"]["code"], "invalid_input")

    def test_records_their_source_no_longer_holds_never_count(self):
        # Kept Co: held (DL-1), held again after a closed absence (DL-4), removed only
        # after now (DL-6); soft deleted (DL-2), missing by its mark alone (DL-3) and
        # removed at a time that does not parse (DL-5) are no members
        args = {"from": "SalesInvoice", "where": [
            {"field": "billed_to.name", "op": "=", "value": "Kept Co"}],
            "measures": [{"fn": "count"}, {"fn": "sum", "field": "total_amount"}]}
        out = self.call(args, FULL)
        self.assertEqual([Decimal(m["value"]) for m in out["groups"][0]["measures"]],
                         [3, Decimal("410")])
        self.assertIs(out["partial"], False)
        bound = returned_evidence("plx.table", args, out)["counts"]
        self.assertEqual([(e["value"], e["complete"]) for e in bound if "op" not in e],
                         [("3", True)])
        listed = self.call({"from": "SalesInvoice", "select": ["invoice_number"], "where": [
            {"field": "billed_to.name", "op": "=", "value": "Kept Co"}],
            "order_by": [{"by": "invoice_number", "dir": "asc"}]}, FULL)
        self.assertEqual([r["values"]["invoice_number"]["value"]["untrusted_text"]
                          for r in listed["rows"]], ["DL-1", "DL-4", "DL-6"])
        # a deleted customer is no group (its held invoice has no customer) and no record
        out = self.call({"from": "SalesInvoice", "where": [
            {"field": "invoice_number", "op": "in", "value": ["GN-1", "DL-1", "DL-2"]}],
            "group_by": ["billed_to.name"], "measures": [{"fn": "count"}]}, FULL)
        self.assertEqual(self.by(out, "billed_to.name"),
                         {("Kept Co", None): "1", (None, None): "1"})
        self.assertNotIn("withheld", out)
        self.assertNotIn("Gone Co", json.dumps(out))
        out = self.call({"from": "Customer", "where": [
            {"field": "name", "op": "=", "value": "Gone Co"}],
            "measures": [{"fn": "count"}]}, FULL)
        self.assertEqual([g["measures"][0]["value"] for g in out["groups"]], ["0"])
        # a twin end its source deleted: never merged in, never shown; the member is
        # counted once, never as a twin left unresolved (the deleted copy links to
        # nothing else)
        args = {"from": "SalesInvoice", "where": [
            {"field": "billed_to.name", "op": "=", "value": "Dt Co"}],
            "measures": [{"fn": "count"}, {"fn": "sum", "field": "total_amount"}]}
        for actor in (FULL, OPS):
            out = self.call(args, actor)
            self.assertEqual([Decimal(m["value"]) for m in out["groups"][0]["measures"]],
                             [1, Decimal("7")])
            # (OPS's answer is partial for Hidden Co's invoice, whose customer may be any)
            self.assertNotIn("twins_unresolved", [g["reason"] for g in out["gaps"]])
            self.assertIs(out["partial"], actor is OPS)
            self.assertNotIn("DT-1B", json.dumps(out))
            self.assertNotIn("dt-b", json.dumps(out))
        # a chain whose deleted middle joined two held copies: both counted, the answer
        # partial so the gate binds no exact count
        args["where"][0]["value"] = "Mid Co"
        out = self.call(args, FULL)
        self.assertEqual(out["groups"][0]["measures"][0]["value"], "2")
        self.assertIs(out["partial"], True)
        self.assertIn("twins_unresolved", [g["reason"] for g in out["gaps"]])
        self.assertNotIn("MD-1B", json.dumps(out))
        self.assertTrue(all(not e["complete"] for e in
                            returned_evidence("plx.table", args, out)["counts"]))

    def test_a_value_that_is_no_calendar_day_has_no_bucket(self):
        for bucket in ("day", "week", "month", "quarter", "year"):
            out = self.call({"from": "SalesInvoice", "where": [
                {"field": "billed_to.name", "op": "=", "value": "Bad Date Co"}],
                "group_by": [{"field": "invoice_date", "bucket": bucket}],
                "measures": [{"fn": "count"}]}, FULL)
            self.assertNotIn("error", out, bucket)
            self.assertEqual(self.by(out, f"invoice_date@{bucket}"), {(None, None): "1"},
                             bucket)

    def test_cardinality_1_10_60_exact_and_bound_by_the_gate(self):
        args = {"from": "SalesInvoice", "where": [
            {"field": "billed_to.name", "op": "starts_with", "value": "Bulk"}],
            "group_by": ["billed_to.name"],
            "measures": [{"fn": "count"}, {"fn": "sum", "field": "total_amount"}],
            "order_by": [{"by": "m0", "dir": "asc"}]}
        # the full reader: a narrow one's answer is partial (a record whose customer it may
        # not read could be a Bulk customer's), so nothing of it binds
        out = self.call(args, FULL)
        self.assertIs(out["partial"], False)
        self.assertEqual(self.keys(out, "billed_to.name"), ["Bulk 1", "Bulk 10", "Bulk 60"])
        for g, n in zip(out["groups"], (1, 10, 60)):
            count, total = g["measures"]
            self.assertEqual(count["value"], str(n))
            self.assertEqual(Decimal(total["value"]),
                             sum(Decimal(f"{k}.25") for k in range(n)))
            self.assertEqual(total["inputs"], n)
            self.assertEqual(len(g["members_cited"]), min(n, 50))
            self.assertEqual(len({c["l1_handle"] for c in total["inputs_citations"]}),
                             min(n, 50))
        self.assertIn("citations_capped", [g["reason"] for g in out["gaps"]])
        bound = returned_evidence("plx.table", args, out)["counts"]
        counts = [e for e in bound if "op" not in e]
        sums = [e for e in bound if e.get("op") == "sum"]
        self.assertEqual([(e["value"], e["complete"]) for e in counts],
                         [("1", True), ("10", True), ("60", True)])
        self.assertEqual([(e["currency"], e["complete"]) for e in sums],
                         [("USD", True)] * 3)

    def test_gate_binds_nothing_from_a_partial_answer(self):
        args = {"from": "SalesInvoice", "measures": [{"fn": "count"},
                                                     {"fn": "sum", "field": "total_amount"}]}
        out = self.call(args, OPS)
        self.assertIs(out["partial"], True)
        bound = returned_evidence("plx.table", args, out)["counts"]
        self.assertTrue(bound)
        self.assertFalse(any(e["complete"] for e in bound))

    def partly_read(self, tool, unread):
        """The tool's store as a Business Central capture read in full but for the
        relation ``unread``."""
        base = tool.base
        meta = base._store_meta()
        fresh = [dict(item, **({"completeness": "partial"} if item["source"] == "bc" else {}))
                 for item in json.loads(meta["freshness"])]
        facts = {r: {"completeness": "complete", "stale": False} for r in ("bc.inv", "bc.gl")}
        facts[unread] = {"completeness": "partial", "stale": False}
        patched = {**meta, "freshness": json.dumps(fresh),
                   "relation_completeness": json.dumps({"bc": facts})}
        base._store_meta = lambda: patched

    def test_a_source_read_in_part_bounds_a_sum_only_where_it_holds_its_records(self):
        # (no link read: a link whose binding names no stored key keeps the part not read
        # able to hold its records, as plx.query decides)
        args = {"from": "SalesInvoice", "where": [
            {"field": "invoice_number", "op": "starts_with", "value": "TW"}],
            "measures": [{"fn": "sum", "field": "total_amount"}]}
        # Business Central read in part, but only in a relation no invoice comes from:
        # nothing of this answer is missing, the sum binds exactly
        tool = self.tool()
        self.partly_read(tool, "bc.gl")
        out = self.call(args, FULL, tool)
        reasons = {g["reason"] for g in out["gaps"]}
        self.assertIn("partly_read_elsewhere", reasons)
        self.assertNotIn("source_partly_read", reasons)
        sums = returned_evidence("plx.table", args, out)["counts"]
        self.assertEqual([(e["complete"], e.get("floor")) for e in sums], [(True, None)])
        # the invoices' own relation read in part: the sum is every input held, a floor
        tool = self.tool()
        self.partly_read(tool, "bc.inv")
        out = self.call(args, FULL, tool)
        self.assertIn("source_partly_read", {g["reason"] for g in out["gaps"]})
        self.assertEqual(out["groups"][0]["measures"][0]["negative_inputs"], 0)
        sums = returned_evidence("plx.table", args, out)["counts"]
        self.assertEqual([(e["complete"], e.get("floor")) for e in sums], [(False, True)])
        # a credit among the inputs: no floor (the part not read may hold more credits)
        credit = {**args, "where": [{"field": "invoice_number", "op": "starts_with",
                                     "value": "CR-"}]}
        out = self.call(credit, FULL, tool)
        measure = out["groups"][0]["measures"][0]
        self.assertEqual((Decimal(measure["value"]), measure["negative_inputs"]),
                         (Decimal("10.00"), 1))
        sums = returned_evidence("plx.table", credit, out)["counts"]
        self.assertEqual([(e["complete"], e.get("floor")) for e in sums], [(False, None)])

    def test_list_mode_cites_each_value(self):
        out = self.call({"from": "SalesInvoice", "where": [
            {"field": "invoice_number", "op": "in", "value": ["inv-1", "INV-3"]}],
            "select": ["invoice_number", "total_amount", "billed_to.name"],
            "order_by": [{"by": "total_amount", "dir": "desc"}]}, OPS)
        self.assertEqual(out["total_rows"], 2)
        first, second = out["rows"]
        self.assertEqual(first["values"]["total_amount"]["value"], "300")
        self.assertEqual(first["values"]["total_amount"]["currency"], "USD")
        self.assertEqual(second["values"]["invoice_number"]["value"]["untrusted_text"], "INV-1")
        self.assertEqual(first["values"]["billed_to.name"]["citation"]["l1_handle"],
                         _api("cust", "acme"))


def build_parties():
    """Invoices whose customer is held as several copies (one party in several records):
    copies giving one name, copies giving two, lead-only links, a solid link beside a lead
    one, a lead to a customer OPS may not read, and invoices linked to 1, 10 and 60 copies
    of one party."""
    s = Store()
    gp, gb = cust(s, "gam-p", "Gamma Co"), cust(s, "gam-b", "Gamma Co")
    d1, d2 = cust(s, "del-1", "Delta Ltd"), cust(s, "del-2", "DELTA LTD.")
    hidden = cust(s, "cust-hidden", "Hidden Co", markings='["finance"]')
    two = inv(s, "two", "P-TWO", "2025-05-01", "10.00", "CAD", gp)
    s.link("l-two-b", "SalesInvoice.billed_to", two, gb, "corroborated")
    dd = inv(s, "dd", "P-DD", "2025-05-02", "20.00", "CAD", d1)
    s.link("l-dd-2", "SalesInvoice.billed_to", dd, d2, "verified")
    inv(s, "lead", "P-LEAD", "2025-05-03", "30.00", "CAD", gp, tier="lead")
    mix = inv(s, "mix", "P-MIX", "2025-05-04", "40.00", "CAD", gb)
    s.link("l-mix-d", "SalesInvoice.billed_to", mix, d1, "lead")
    inv(s, "hl", "P-HL", "2025-05-05", "50.00", "CAD", hidden, tier="lead")
    seen = inv(s, "seen", "P-SEEN", "2025-05-06", "60.00", "CAD", gp)
    s.link("l-seen-h", "SalesInvoice.billed_to", seen, hidden, "lead")
    for n in (1, 10, 60):
        copies = [cust(s, f"copy{n}-{k}", f"Copies {n}") for k in range(n)]
        one = inv(s, f"c{n}", f"P-C{n}", "2025-06-01", "1.00", "CAD", copies[0])
        for k, c in enumerate(copies[1:]):
            s.link(f"l-c{n}-{k}", "SalesInvoice.billed_to", one, c, "verified")
        for k in range(n):
            inv(s, f"ld{n}-{k}", f"P-LD{n}-{k}", "2025-07-01", "1.00", "CAD", copies[0],
                tier="lead")
    s.conn.commit()
    return s


class LinkedParties(unittest.TestCase):
    """A link read through several copies of one party, and lead-tier links (``leads``)."""
    MAY = {"field": "invoice_date", "op": "between", "value": ["2025-05-01", "2025-05-31"]}

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_store_mode
        cls.store = build_parties()
        cls.dsn = pg_store_mode._load("", cls.store.conn)

    tool, call, keys = TableTool.tool, TableTool.call, staticmethod(TableTool.keys)

    def counts(self, out, name="billed_to.name"):
        return {k: (g["measures"][0]["value"], g.get("lead_members"))
                for k, g in zip(self.keys(out, name), out["groups"])}

    def test_copies_giving_one_value_resolve_and_leads_count_only_when_asked(self):
        tool = self.tool()
        args = {"from": "SalesInvoice", "where": [self.MAY], "group_by": ["billed_to.name"],
                "measures": [{"fn": "count"}], "limit": 200}
        out = self.call(args, FULL, tool)
        # two (two Gamma copies, one name), mix and seen (solid Gamma; leads not followed);
        # dd's copies give two names: no key, a gap; lead and hl: no solid link, no key
        self.assertEqual(self.counts(out), {"Gamma Co": ("3", None), None: ("3", None)})
        reasons = [g["reason"] for g in out["gaps"]]
        self.assertIn("ambiguous_link", reasons)
        self.assertIn("possibly_related", reasons)
        self.assertNotIn("lead_basis", reasons)
        leads = self.call(dict(args, leads=True), FULL, tool)
        # lead joins Gamma by its lead link, hl the hidden customer (FULL holds finance);
        # mix and seen keep their solid Gamma link (a lead beside a solid link is unused)
        self.assertEqual(self.counts(leads), {"Gamma Co": ("4", 1), "Hidden Co": ("1", 1),
                                              None: ("1", None)})
        reasons = [g["reason"] for g in leads["gaps"]]
        self.assertIn("lead_basis", reasons)
        self.assertNotIn("possibly_related", reasons)
        self.assertIs(leads["partial"], False)

    def test_a_lead_to_a_record_the_reader_may_not_read_withholds_never_shows(self):
        tool = self.tool()
        args = {"from": "SalesInvoice", "where": [self.MAY], "group_by": ["billed_to.name"],
                "measures": [{"fn": "count"}], "limit": 200, "leads": True}
        out = self.call(args, OPS, tool)
        self.assertNotIn("Hidden Co", json.dumps(out))
        self.assertNotIn("cust-hidden", json.dumps(out))
        # hl: its only link reaches a customer OPS may not read: withheld; seen: its solid
        # Gamma link stands, the hidden lead beside it unused
        self.assertIs(out["partial"], True)
        self.assertEqual(out["withheld"]["objects"], 1)
        self.assertEqual(self.counts(out), {"Gamma Co": ("4", 1), None: ("1", None)})
        rows = self.call({"from": "SalesInvoice", "where": [self.MAY], "leads": True,
                          "select": ["invoice_number", "billed_to.name"], "limit": 200},
                         OPS, tool)
        self.assertNotIn("Hidden Co", json.dumps(rows))
        lead_rows = {r["values"]["invoice_number"]["value"]["untrusted_text"]
                     for r in rows["rows"] if r.get("lead")}
        self.assertEqual(lead_rows, {"P-LEAD"})
        # without leads the hidden lead is not followed: nothing withheld
        plain = self.call({k: v for k, v in args.items() if k != "leads"}, OPS, tool)
        self.assertIs(plain["partial"], False)

    def test_existence_of_a_link_needs_no_single_target(self):
        out = self.call({"from": "SalesInvoice", "where": [
            self.MAY, {"field": "billed_to.object_id", "op": "not_null"}],
            "group_by": ["status"], "measures": [{"fn": "count"}]}, FULL)
        # two, dd, mix, seen link to a customer (two and dd to two copies); no gap
        self.assertEqual(out["groups"][0]["measures"][0]["value"], "4")
        self.assertNotIn("ambiguous_link", [g["reason"] for g in out["gaps"]])
        none = self.call({"from": "SalesInvoice", "where": [
            self.MAY, {"field": "billed_to.object_id", "op": "is_null"}],
            "measures": [{"fn": "count"}]}, FULL)
        self.assertEqual(none["groups"][0]["measures"][0]["value"], "2")   # lead, hl
        # the object id itself is still one target's only
        ids = self.call({"from": "SalesInvoice", "where": [
            {"field": "invoice_number", "op": "=", "value": "P-TWO"}],
            "select": ["billed_to.object_id"]}, FULL)
        self.assertIsNone(ids["rows"][0]["values"]["billed_to.object_id"]["value"])

    def test_cardinality_1_10_60_copies_and_lead_records(self):
        tool = self.tool()
        out = self.call({"from": "SalesInvoice", "where": [
            {"field": "invoice_date", "op": "=", "value": "2025-06-01"}],
            "group_by": ["billed_to.name"], "measures": [{"fn": "count"}]}, FULL, tool)
        self.assertEqual(self.counts(out), {"Copies 1": ("1", None), "Copies 10": ("1", None),
                                            "Copies 60": ("1", None)})
        self.assertNotIn("ambiguous_link", [g["reason"] for g in out["gaps"]])
        leads = self.call({"from": "SalesInvoice", "where": [
            {"field": "invoice_date", "op": "=", "value": "2025-07-01"}], "leads": True,
            "group_by": ["billed_to.name"], "measures": [{"fn": "count"}]}, FULL, tool)
        self.assertEqual(self.counts(leads), {"Copies 1": ("1", 1), "Copies 10": ("10", 10),
                                              "Copies 60": ("60", 60)})

    def test_the_answer_gate_binds_lead_counts_as_leads(self):
        tool = self.tool()
        args = {"from": "SalesInvoice", "where": [{"field": "invoice_number", "op": "in",
                                                   "value": ["P-TWO", "P-LEAD", "P-MIX"]}],
                "group_by": ["billed_to.name"], "measures": [{"fn": "count"}]}
        plain = returned_evidence("plx.table", args, self.call(args, FULL, tool))
        # records no solid link keys are set apart (possibly_related): the counts stand
        self.assertTrue(plain["counts"])
        self.assertTrue(all(e["complete"] for e in plain["counts"] if "op" not in e))
        self.assertFalse(any(plain.get("lead_records", {}).values()))
        leads = dict(args, leads=True)
        bound = returned_evidence("plx.table", leads, self.call(leads, FULL, tool))
        self.assertTrue(bound["lead_records"])
        self.assertTrue(all(bound["lead_records"].values()))
        self.assertFalse(any(e["complete"] for e in bound["counts"]))

    def test_leads_is_a_boolean(self):
        for value in ("yes", 1, None):
            with self.subTest(value=value):
                out = self.tool().call("plx.table", {"from": "SalesInvoice", "leads": value,
                                                     "measures": [{"fn": "count"}]}, FULL)
                self.assertEqual(out["error"]["code"], "invalid_input")


def build_zones():
    """Invoices posted around the November 2026 month edges, stored as UTC or another
    zone's text, and one time stated without a zone."""
    s = Store()
    for key, at in (("oct31", "2026-11-01T02:30:00Z"),      # 10:30 PM Oct 31 ET
                     ("nov1", "2026-11-01T05:00:00Z"),       # 1 AM Nov 1 EDT
                     ("west", "2026-10-31T23:00:00-08:00"),  # 2 AM Nov 1 EST
                     ("bare", "2026-11-15T10:00:00"),        # no zone: as stored
                     ("nov30", "2026-12-01T04:59:59.250000Z"),  # 11:59 PM Nov 30 EST
                     ("dec1", "2026-12-01T05:00:00Z")):      # midnight Dec 1 EST
        oid = inv(s, key, f"Z-{key}", "2026-11-01", "1.00", "CAD")
        s.val(oid, "posted_at", "timestamp", at)
    s.conn.commit()
    return s


class EasternDays(unittest.TestCase):
    """A timestamp's day, month and bucket are the company's (America/New_York)."""

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_store_mode
        cls.store = build_zones()
        cls.dsn = pg_store_mode._load("", cls.store.conn)

    tool, call, keys = TableTool.tool, TableTool.call, staticmethod(TableTool.keys)

    def numbers(self, where):
        out = self.call({"from": "SalesInvoice", "where": where,
                         "select": ["invoice_number"], "limit": 200})
        return {r["values"]["invoice_number"]["value"]["untrusted_text"] for r in out["rows"]}

    def test_this_month_is_the_eastern_month_at_both_edges(self):
        november = {"field": "posted_at", "op": "between", "value": ["2026-11-01", "2026-11-30"]}
        self.assertEqual(self.numbers([november]), {"Z-nov1", "Z-west", "Z-bare", "Z-nov30"})
        self.assertEqual(self.numbers([{"field": "posted_at", "op": ">=",
                                        "value": "2026-11-01"},
                                       {"field": "posted_at", "op": "<",
                                        "value": "2026-12-01"}]),
                         {"Z-nov1", "Z-west", "Z-bare", "Z-nov30"})
        self.assertEqual(self.numbers([{"field": "posted_at", "op": "=",
                                        "value": "2026-10-31"}]), {"Z-oct31"})
        self.assertEqual(self.numbers([{"field": "posted_at", "op": "in",
                                        "value": ["2026-12-01"]}]), {"Z-dec1"})
        out = self.call({"from": "SalesInvoice", "group_by": [
            {"field": "posted_at", "bucket": "month"}], "measures": [{"fn": "count"}]})
        self.assertEqual(dict(zip(self.keys(out, "posted_at@month"),
                                  (g["measures"][0]["value"] for g in out["groups"]))),
                         {"2026-10": "1", "2026-11": "4", "2026-12": "1"})

    def test_a_bound_stated_with_its_zone_is_the_same_instant(self):
        self.assertEqual(self.numbers([{"field": "posted_at", "op": ">=",
                                        "value": "2026-12-01T05:00:00Z"}]), {"Z-dec1"})
        self.assertEqual(self.numbers([{"field": "posted_at", "op": "<",
                                        "value": "2026-11-01T01:30:00-05:00"}]),
                         {"Z-oct31", "Z-nov1"})
        out = self.tool().call("plx.table", {"from": "SalesInvoice", "where": [
            {"field": "posted_at", "op": ">", "value": "2026-13-45T00:00:00Z"}],
            "measures": [{"fn": "count"}]}, FULL)
        self.assertEqual(out["error"]["code"], "invalid_input")


class DocumentMarksDecode(unittest.TestCase):
    """The marks a reader carries hold JSON lists and one plain tag; only the lists are read."""

    def test_a_plain_people_tag_beside_the_lists_does_not_break_the_read(self):
        from unittest import mock
        from operating.search import document_marks

        class _Scope:
            held = frozenset()

        tool = tt.TableTool(base=None)
        for tag in ("", "full:abc123"):
            marks = {"dm_ids": '["a", "b"]', "dm_types": "[]", "dm_own": '["o"]', "dm_people": tag}
            with mock.patch.object(document_marks, "params", return_value=marks):
                self.assertEqual(tool._documents(_Scope()),
                                 {"dm_ids": ["a", "b"], "dm_types": [], "dm_own": ["o"]})


if __name__ == "__main__":
    unittest.main()
