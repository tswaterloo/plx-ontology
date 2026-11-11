"""The raw Portal, FileMaker-silver and Sage relations as business types
(``ontology/schema/overrides/business.json``, ``ontology/raw_business.py``): every raw-only
relation is declared (nothing dropped), a malformed declaration fails closed, the declared
joins are links, the declared text is searchable under the row's own markings, and the
salesperson (pay-related) rows carry the payroll marking pair."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from ontology import raw_business, schema as schema_loader
from ontology.generator.overrides import OVERRIDES_DIR, OverrideError, load_overrides
from operating.chunking.projection import open_ro
from operating.chunking.records import RecordChunker
from tests.test_chunks_embed import Counts, Fixture, exclusion


def compiled():
    return schema_loader.load_schema()


class DeclarationTests(unittest.TestCase):
    def test_every_raw_relation_is_declared_and_reaches_what_it_names(self):
        c = compiled()
        self.assertEqual(raw_business.check(c), [])
        decl = raw_business.declared()
        self.assertEqual(set(decl), set(raw_business.raw_only_relations(c)))
        for rel, spec in decl.items():
            if spec["decision"] == "skip":
                self.assertRegex(spec["reason"], r"^(system|log): ", rel)
            else:   # a kept relation reads as its business name in Ask and on its page
                tid = raw_business.type_of(rel)
                self.assertTrue(c.object_types[tid]["description"].startswith(spec["label"]),
                                rel)
        # the highest-value families are business types, never skipped
        for rel in ("sage.gl_je_detail", "sage.gl_je_header", "sage.gl_pjd", "sage.ar_tcr",
                    "sage.ap_tcr", "sage.ar_obp", "sage.ap_obp", "sage.ar_open_document",
                    "public.app_production_task", "public.app_assembly_order_damage"):
            self.assertNotEqual(decl[rel]["decision"], "skip", rel)

    def test_declared_joins_are_links_with_their_tier(self):
        c = compiled()
        links = c.link_types
        for rel, spec in raw_business.declared().items():
            for join in spec.get("joins", ()):
                lid = f"{raw_business.type_of(rel)}.{join['name']}"
                self.assertIn(lid, links, lid)
                binding = links[lid]["bindings"][0]
                self.assertEqual(binding["tier_ceiling"], join["tier"], lid)
                self.assertEqual(binding["to_binding"], join["to_binding"], lid)
        # AR/AP posting headers reach the GL entry they created; their lines reach it
        # through the header (the lines carry no GL batch or entry: 0 matches measured)
        self.assertEqual(links["RelSageArPjh.gl_entry"]["to"], "RelSageGlJeHeader")
        self.assertEqual(links["RelSageApPjh.gl_entry"]["to"], "RelSageGlJeHeader")
        self.assertNotIn("RelSageArPjd.gl_entry", links)

    def test_salesperson_rows_carry_the_payroll_marking_pair(self):
        c = compiled()
        for tid in ("RelSageArSalesperson", "RelSageArSps"):
            self.assertEqual(set(c.object_types[tid].get("markings", ())), {"finance", "pii"},
                             tid)
        self.assertNotIn("RelSageArSalesperson", raw_business.text_fields())
        self.assertNotIn("RelSageArSps", raw_business.text_fields())

    def test_label_is_the_readable_type_name(self):
        from operating.explanations import type_words
        self.assertEqual(type_words("RelSageGlJeDetail"), "GL journal entry line")
        self.assertEqual(type_words("PurchaseOrderLine"), "purchase order line")


class FailClosedTests(unittest.TestCase):
    def mutated(self, change) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "overrides"
        shutil.copytree(OVERRIDES_DIR, root)
        path = root / "business.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        change(doc["sources"]["portal_api"])
        path.write_text(json.dumps(doc), encoding="utf-8")
        return root

    def test_malformed_declarations_fail_the_load(self):
        cases = {
            "unknown decision": lambda r: r["sage.gl_je_detail"].update(decision="maybe"),
            "unknown key": lambda r: r["sage.gl_je_detail"].update(colour="red"),
            "skip without a system/log reason": lambda r: r["sage.ap_p01"].update(
                reason="not needed"),
            "skip carrying text": lambda r: r["sage.ap_p01"].update(text=["x"]),
            "extends without a target": lambda r: r["sage.gl_afs"].pop("extends"),
            "lead join without unique_target": lambda r: r["sage.gl_jec"]["joins"][0].update(
                tier="lead"),
            "identifier compare without kind": lambda r: r["sage.gl_jec"]["joins"][0][
                "pairs"][0].update(compare="identifier"),
            "unknown compare": lambda r: r["sage.gl_jec"]["joins"][0]["pairs"][0].update(
                compare="text"),
            "display of four columns": lambda r: r["sage.gl_je_detail"].update(
                display=["a", "b", "c", "d"]),
        }
        for what, change in cases.items():
            with self.subTest(what), self.assertRaises(OverrideError):
                load_overrides(self.mutated(change))
        load_overrides(self.mutated(lambda r: None))     # the unmutated copy loads

    def test_generator_refuses_a_column_the_relation_lacks(self):
        from ontology.generator.build import _apply_business
        obj = {"description": "raw"}
        with self.assertRaises(OverrideError):
            _apply_business(obj, {"decision": "type", "label": "L", "about": "a",
                                  "display": ["nope"]}, "sage.x", {"id": "id"})
        _apply_business(obj, {"decision": "skip", "label": "L", "reason": "system: s"},
                        "sage.x", {"id": "id"})
        self.assertEqual(obj, {"description": "raw"})   # a skipped table stays as generated


class RecordTextTests(unittest.TestCase):
    """Declared text per family at N=1/10/60: one chunk per wordy value, none lost or
    duplicated, titled by the business label, keyed by the row's markings."""

    FAMILIES = (("RelSageArCmm", "textcmnt1", "api:sage.ar_cmm"),
                ("RelPublicAppAssemblyOrderDamage", "description",
                 "api:public.app_assembly_order_damage"),
                ("RelSilverBatchSchedule", "title", "api:silver.batch_schedule"))

    def chunks(self, n: int):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "fx.sqlite"
        fx = Fixture(path)
        for tid, prop, audience in self.FAMILIES:
            for i in range(n):
                finance = i % 2 == 0
                fx.obj(f"{tid}:{i:03d}", tid, audience,
                       marks='["finance"]' if finance else "[]",
                       values={prop: (f"Freight damaged pallet replaced number {i} "
                                      "at the customer site", "[]"),
                               "audtorg": ("PLXDAT company database org", "[]")})
        # Sage GL/AR/AP free text (pay documents name staff) is never search text
        for tid, prop, aud in (("RelSageGlJeDetail", "transdesc", "api:sage.gl_je_detail"),
                               ("RelSageApIbh", "invcdesc", "api:sage.ap_ibh"),
                               ("RelSageArRfh", "docdesc", "api:sage.ar_rfh"),
                               ("RelSageArUnpsth", "textdesc", "api:sage.ar_unpsth")):
            for i in range(n):
                fx.obj(f"{tid}:{i:03d}", tid, aud,
                       marks='["finance","pii"]' if i % 2 else '["finance"]',
                       values={prop: ("Payroll run for the plant crew this week", "[]")})
        # a payroll-marked row is chunked only with both markings, never fewer
        fx.obj("RelPublicAppAssemblyOrderDamage:pay", "RelPublicAppAssemblyOrderDamage",
               "api:public.app_assembly_order_damage", marks='["finance","pii"]',
               values={"description": ("Overtime pay adjustment noted for the crew", "[]")})
        fx.done()
        conn = open_ro(path)
        self.addCleanup(conn.close)
        return list(RecordChunker(conn, exclusion(), Counts()).record_chunks())

    def test_text_per_family_n_1_10_60(self):
        labels = raw_business.labels()
        for n in (1, 10, 60):
            with self.subTest(n=n):
                got = self.chunks(n)
                for tid, prop, audience in self.FAMILIES:
                    # the family's declared text column (main's schema rule chunks the
                    # row's other text columns too, words-only: not this test's subject)
                    mine = [c for c in got if c.object_id.startswith(tid + ":")
                            and not c.object_id.endswith(":pay")
                            and f"Where: field row.{prop}\n" in c.header]
                    self.assertEqual(sorted(c.object_id for c in mine),
                                     [f"{tid}:{i:03d}" for i in range(n)], tid)
                    for c in mine:
                        i = int(c.object_id.rsplit(":", 1)[1])
                        self.assertEqual(c.audience_key, audience)
                        self.assertEqual(set(c.markings),
                                         {"finance"} if i % 2 == 0 else set(), c.object_id)
                        self.assertIn(labels[tid], c.header)
                        self.assertNotIn("PLXDAT", c.body)   # undeclared columns stay out
                self.assertFalse([c for c in got if c.object_id.startswith(
                    ("RelSageGl", "RelSageApIbh", "RelSageArRfh", "RelSageArUnpsth"))])
                pay = [c for c in got if c.object_id.endswith(":pay")]
                self.assertEqual(len(pay), 1)
                self.assertEqual(set(pay[0].markings), {"finance", "pii"})


if __name__ == "__main__":
    unittest.main()


class PayrollGlTests(unittest.TestCase):
    """A GL line posted to a payroll account is payroll whole (finance+pii on its version
    and every value); its free text is never search text; other GL lines keep their own
    markings."""

    def projector_with(self, account_row):
        from types import SimpleNamespace
        from ontology.project import Projector
        p = Projector.__new__(Projector)
        p._row_marks = {}
        target = None if account_row is None else SimpleNamespace(
            rec=SimpleNamespace(row=account_row))
        p._hop = lambda version, link, where: target
        return p

    def line(self, vid, tid="RelSageGlJeDetail"):
        from types import SimpleNamespace
        binding = compiled().object_types[tid]["bindings"][0]
        return SimpleNamespace(vid=vid, binding=binding,
                               rec=SimpleNamespace(row={"acctid": "1"}))

    def test_payroll_line_is_marked_whole_and_others_are_not(self):
        cases = {"Salaries and wages - plant": {"finance", "pii"},
                 "Commission payable": {"finance", "pii"},
                 "Sales commissions": {"finance", "pii"},
                 "Freight in": None,
                 None: {"finance", "pii"}}     # unresolved account: marked, never open
        for name, want in cases.items():
            with self.subTest(account_named=name is not None):
                p = self.projector_with(None if name is None else {"acctdesc": name})
                p._row_marks_of(self.line(7))
                self.assertEqual(set(p._row_marks[7]) if 7 in p._row_marks else None, want)

    def test_gl_text_and_person_names_are_never_search_text(self):
        from operating.chunking.records import load_config
        fields = load_config()["fields"]
        self.assertFalse([t for t in fields if t.startswith("RelSageGl")])
        names = {"vendname", "namecust", "namermit", "rmitname", "namecity", "nameacct"}
        self.assertFalse([(t, f) for t, fs in raw_business.text_fields().items()
                          for f in fs if f in names])
        c = compiled()
        for tid, cols in (("RelSageGlJeDetail", ("transdesc", "transref")),
                          ("RelSageGlPjd", ("jnldtldesc", "jnldtlref"))):
            props = {p["name"]: p for p in c.object_types[tid]["properties"]}
            for col in cols:
                self.assertTrue({"finance", "pii"} <= set(props[col].get("markings", ())),
                                (tid, col))
            # a non-payroll line's other values keep only their own markings: findable
            self.assertNotIn("pii", c.object_types[tid].get("markings", ()))
            self.assertTrue(c.object_types[tid]["bindings"][0]["marking_rules"])

    def test_ap_and_ar_lines_on_pay_accounts_are_payroll_and_their_text_is_not_searchable(self):
        c = compiled()
        for tid, link in (("RelSageApPjd", "gl_account_idacct"),
                          ("RelSageApIbd", "gl_account_idglacct"),
                          ("RelSageArPjd", "gl_account_idacct"),
                          ("RelSageArIbd", "gl_account_idacctrev"),
                          ("RelSageApTcn", "gl_account_idacct"),
                          ("RelSageArTcn", "gl_account_idacct")):
            with self.subTest(tid=tid):
                rules = c.object_types[tid]["bindings"][0]["marking_rules"]
                self.assertEqual(rules[0]["when"][0]["via_link"], f"{tid}.{link}")
                self.assertIn(f"{tid}.{link}", c.link_types)
                props = {p["name"]: p for p in c.object_types[tid]["properties"]}
                texty = [n for n in props if (any(k in n for k in ("desc", "comment"))
                         or n.startswith("text")) and n != "descomp"]   # company code
                self.assertTrue(texty)
                for n in texty:
                    self.assertTrue({"finance", "pii"} <= set(props[n].get("markings", ())),
                                    (tid, n))
                self.assertFalse(set(texty) & set(raw_business.text_fields().get(tid, ())))
        for tid, name, want in (("RelSageApPjd", "Sales commissions", {"finance", "pii"}),
                                ("RelSageArIbd", "Wages - warehouse", {"finance", "pii"}),
                                ("RelSageArPjd", "Freight revenue", None)):
            with self.subTest(tid=tid, pay=want is not None):
                p = self.projector_with({"acctdesc": name})
                p._row_marks_of(self.line(9, tid))
                self.assertEqual(set(p._row_marks[9]) if 9 in p._row_marks else None, want)

    def test_an_ap_header_with_a_pay_line_is_payroll_whole_and_its_text_is_not_search_text(self):
        import sqlite3
        from types import SimpleNamespace
        from ontology.project import Projector
        c = compiled()
        header = c.object_types["RelSageApIbh"]
        rules = [r for r in header["bindings"][0]["marking_rules"] if "from_lines" in r]
        self.assertTrue(rules and "invoice_entry" in rules[0]["from_lines"])
        props = {p["name"]: p for p in header["properties"]}
        self.assertTrue({"finance", "pii"} <= set(props["invcdesc"].get("markings", ())))
        self.assertNotIn("invcdesc", raw_business.text_fields().get("RelSageApIbh", ()))
        self.assertNotIn("idinvc", [n for n, p in props.items()
                                    if "pii" in p.get("markings", ())])   # ids stay open
        for line_marks, want in (({"finance", "pii"}, {"finance", "pii"}), (set(), None)):
            with self.subTest(pay_line=bool(line_marks)):
                p = Projector.__new__(Projector)
                p.links = {lid: link for lid, link in c.link_types.items()
                           if lid == "RelSageApIbd.invoice_entry"}
                p._line_kinds = {}
                p._row_marks = {5: frozenset(line_marks)} if line_marks else {}
                p._conn = db = sqlite3.connect(":memory:")
                db.execute("CREATE TABLE links (link_type TEXT, from_vid INT, to_object TEXT,"
                              " tier TEXT)")
                db.execute("INSERT INTO links VALUES ('RelSageApIbd.invoice_entry', 5, "
                              "'RelSageApIbh:h', 'verified')")
                version = SimpleNamespace(vid=9, type_id="RelSageApIbh", object_id="RelSageApIbh:h",
                                          binding=header["bindings"][0])
                p._header_marks_of(version)
                self.assertEqual(set(p._row_marks[9]) if 9 in p._row_marks else None, want)
