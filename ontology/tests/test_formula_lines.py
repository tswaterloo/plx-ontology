"""Formula lines as links (``ontology.formula_lines``, build step ``with_formula_lines``).

SYNTHETIC fixtures only (``operating.tests.test_composition_lines``' sample cards in a
``World`` store: made-up codes, names and numbers); evidence label: isolated-fixture.

* The step writes, for every product, the lines the ``formula_lines`` view answers for it:
  one ``Product.formula_line`` per resolved sheet line, product -> raw material, the raw
  material's supplier items one ``SupplierItem.for_item`` hop on; ``corroborated`` on a
  sheet stating the product's code, ``lead`` on one matched by its NAME only; each link
  cites the sheet's version and carries the line's share, quantity and check.
* A sheet stating another product's code, an unresolved part, a file outside a formula
  name or folder are never linked; a file in a formulas folder is a sheet.
* People first: an excluded audience's sheet is never read; no guard, nothing written
  (the rows it wrote before removed).
* Every read before the first write (``ontology.pg_step_inplace`` reads Postgres in place).
* The schema declares the two link types with the step binding; the step migration plans
  the step's links (prefix, writes, inputs).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ontology import formula_lines as fl
from ontology import step_migrate as sm
from ontology.schema import load_schema
from operating import business_documents as bd
from operating.tests.test_business_documents import AS_OF, BOX, EVERY, Fixture, Guard
from operating.tests.test_composition_lines import GOOD, card

PROJECTOR = SimpleNamespace(links={
    lt: {"bindings": [{"kind": "mention", "written_by_step": fl.STEP}]}
    for lt in fl.LINK_TYPES.values()})


class Builder:
    """A builder over a store connection that records the statements and added links."""

    def __init__(self, conn) -> None:
        self.inner = conn
        self.log: list[str] = []
        self.added: list[dict] = []
        self.connection = self

    def execute(self, sql, params=()):
        self.log.append(sql.split()[0].upper())
        return self.inner.execute(sql, params)

    def cursor(self):
        builder = self

        class Cur:
            row_factory = None

            def execute(self, sql, params=()):
                builder.log.append(sql.split()[0].upper())
                cur = builder.inner.cursor()
                cur.row_factory = self.row_factory
                return cur.execute(sql, params)
        return Cur()

    def add(self, table, rows) -> None:
        self.log.append("ADD")
        self.added.extend(rows)


class FormulaLinks(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.n = 0

    def world(self, *, name_only: bool = False, audience: str = BOX, second: bool = False):
        f = Fixture()
        product = f.w.obj("Product", {"code": "SYN-B01", "name": "Synth Calm Lotion"})
        f.codes[product] = "SYN-B01"
        if second:  # the F-26-0002 sheet's own product: two sheets, two stated codes
            self.second = f.w.obj("Product", {"code": "SYN-B02",
                                              "name": "Synth Calm Lotion Unscented"})
            f.codes[self.second] = "SYN-B02"
        raws = {part: f.item(part) for _, part, _, _ in GOOD}
        supplier_item = f.w.obj("SupplierItem", {"supplier_item_number": "SUP-1"},
                                audience="api:purchasing")
        f.w.link("SupplierItem.for_item", supplier_item, raws["R-0101"])
        text = card("SYN-B01", GOOD)
        if name_only:
            text = "\n".join(ln for ln in text.splitlines() if "CODE:" not in ln)
        f.mailed("F-26-0001 - Synth Brand - Synth Calm Lotion.pdf", text,
                 sender="rnd@synth-tenant.example", audience=audience)
        # same name words, another product's code: never this product's
        f.mailed("F-26-0002 - Synth Brand - Synth Calm Lotion Unscented.pdf",
                 card("SYN-B02", GOOD, formula="F-26-0002"), sender="rnd@synth-tenant.example")
        # a sheet's text under a name that is no formula sheet's: never read
        f.mailed("Synth Calm Lotion notes.pdf", card("SYN-B01", [("1A", "R-0777", "100.00",
                                                                  "200.000")]),
                 sender="rnd@synth-tenant.example")
        self.n += 1
        conn = f.build(self.dir / f"store{self.n}.sqlite")
        self.addCleanup(conn.close)
        return conn, product, raws, supplier_item

    def step(self, conn, guard=Guard()):
        b = Builder(conn)
        out = fl.formula_lines_step(b, PROJECTOR, guard=guard)
        return b, out

    def test_links_are_the_views_lines_and_reach_the_supplier_items(self) -> None:
        conn, product, raws, supplier_item = self.world()
        b, out = self.step(conn)
        view = bd.answer(conn, view="formula_lines", may_read=lambda k: True, held=EVERY,
                         guard=Guard(), as_of=AS_OF, args={"subject": product})
        parts = [(r["part"], r["item"], r["share_percent"], r["quantity"], r["check"])
                 for r in view["rows"] if r["kind"] == "component"]
        links = sorted(b.added, key=lambda r: json.loads(r["reason"])[0]["line"])
        self.assertEqual([(json.loads(r["reason"])[0]["part"], r["to_object"],
                           json.loads(r["reason"])[0]["share_percent"],
                           json.loads(r["reason"])[0]["quantity"],
                           json.loads(r["reason"])[0]["check"]) for r in links], parts)
        self.assertEqual(len(parts), 3)
        self.assertEqual({(r["link_type"], r["from_object"], r["tier"], r["binding_kind"])
                          for r in links},
                         {("Product.formula_line", product, "corroborated", "mention")})
        self.assertTrue(all(r["link_id"].startswith(fl.PREFIX) for r in links))
        reason = json.loads(links[0]["reason"])[0]
        self.assertEqual((reason["basis"], reason["formula_code"], reason["sheet_status"]),
                         ("code", "F-26-0001", "complete"))
        sheet_handle = conn.execute("SELECT l1_handle FROM object_versions WHERE object_id = ?",
                                    (reason["sheet"],)).fetchone()[0]
        self.assertIn(sheet_handle, json.loads(links[0]["evidence_handles"]))
        self.assertEqual(reason["citations"]["sheet"], sheet_handle)
        # Product -> formula line -> raw material -> supplier item
        (offer,), = conn.execute(
            "SELECT from_object FROM links WHERE link_type = 'SupplierItem.for_item' "
            "AND to_object = ?", (links[0]["to_object"],)).fetchall()
        self.assertEqual((links[0]["to_object"], offer), (raws["R-0101"], supplier_item))
        counts = out["counts"]
        self.assertEqual(counts["sheet_other_product"], 1)
        self.assertEqual(counts["part_resolved"], 3)
        self.assertNotIn("R-0777", {json.loads(r["reason"])[0]["part"] for r in b.added})
        self.assertEqual(out["link_tiers"], {"corroborated": 3})

    def test_each_link_cites_its_own_sheets_stated_code(self) -> None:
        # two sheets stating different codes: a link's product_code is its own sheet's
        conn, product, _raws, _si = self.world(second=True)
        b, out = self.step(conn)
        self.assertEqual(out["counts"].get("sheet_other_product", 0), 0)
        cited = {}
        for r in b.added:
            reason = json.loads(r["reason"])
            self.assertEqual(reason[1]["feature"], "product_code")
            cited.setdefault(r["from_object"], set()).add(
                (reason[0]["formula_code"], reason[1]["value"]))
        self.assertEqual(cited, {product: {("F-26-0001", "SYN-B01")},
                                 self.second: {("F-26-0002", "SYN-B02")}})

    def test_the_rows_go_into_the_in_place_steps_store(self) -> None:
        # the store the in-place run gives a step (``pg_step_inplace._local_store``): its
        # link constraints (reason a feature list, evidence a handle list) hold every row
        from ontology import pg_step_inplace as ip
        conn, *_ = self.world()
        rows, _ = fl.formula_links(conn, Guard())
        local = ip._local_store(self.dir / "local.sqlite")
        try:
            self.assertEqual(local.add("links", rows), 3)
            got = local.connection.execute(
                "SELECT count(*) FROM links WHERE link_type = 'Product.formula_line'").fetchone()
            self.assertEqual(got[0], 3)
            self.assertTrue(all(json.loads(r["reason"])[0]["feature"] == "formula_line"
                                for r in rows))
        finally:
            local.abort()

    def test_every_read_comes_before_the_first_write(self) -> None:
        conn, *_ = self.world()
        b, _ = self.step(conn)
        first_write = min(i for i, s in enumerate(b.log) if s in ("DELETE", "ADD"))
        self.assertTrue(all(s in ("SELECT", "WITH") for s in b.log[:first_write]))
        self.assertEqual(set(b.log[first_write:]), {"DELETE", "ADD"})

    def test_a_sheet_naming_the_product_without_its_code_is_a_lead(self) -> None:
        conn, product, *_ = self.world(name_only=True)
        b, _ = self.step(conn)
        self.assertEqual({(r["from_object"], r["tier"]) for r in b.added}, {(product, "lead")})
        self.assertEqual({json.loads(r["reason"])[0]["basis"] for r in b.added}, {"name"})
        self.assertTrue(all(json.loads(r["why_not"]) for r in b.added))

    def test_people_first_and_fail_closed(self) -> None:
        conn, *_ = self.world()
        b, out = self.step(conn, Guard(blocked={BOX}))
        self.assertEqual(b.added, [])
        self.assertGreater(out["counts"]["excluded_person"], 0)
        cols = [c[1] for c in conn.execute("PRAGMA table_info(link_rows)")]
        conn.execute(f"INSERT INTO link_rows ({', '.join(cols)}) SELECT "
                     + ", ".join("'formula:old'" if c == "link_id" else c for c in cols)
                     + " FROM link_rows LIMIT 1")
        b, out = self.step(conn, None)
        self.assertEqual((b.added, out["counts"]), ([], {"exclusion_unavailable": 1}))
        self.assertEqual(conn.execute("SELECT count(*) FROM link_rows WHERE link_id LIKE "
                                      "'formula:%'").fetchone()[0], 0)

    def test_sheet_names_by_number_word_or_folder_only(self) -> None:
        self.assertTrue(fl.formula_named("F-26-0001 Synth Calm.pdf"))
        self.assertTrue(fl.formula_named("Synth Calm sample card.pdf"))
        self.assertTrue(fl.formula_named("/R&D/2025 New R&D Formulas/250633 - Calm.pdf"))
        for name in ("Synth Calm Lotion.pdf", "fulfilment.xlsx", "samples-2026.pdf x",
                     "F-2-0001.pdf", "AF-26-0001.pdf", "formal offer.pdf",
                     "F-26-00012.pdf"):
            self.assertFalse(fl.formula_named(name), name)


class Declared(unittest.TestCase):

    def test_schema_declares_the_step_links_and_the_plan_knows_the_step(self) -> None:
        links = sm._get(load_schema(), "link_types")
        for frm, lid in fl.LINK_TYPES.items():
            link = links[lid]
            self.assertEqual((link["from"], link["to"], link["cardinality"]),
                             (frm, "Product", "N:M"))
            self.assertTrue(fl.declared({lid: link}))
        spec = sm.STEPS[fl.STEP]
        self.assertEqual(spec.prefixes, (fl.PREFIX,))
        self.assertEqual(spec.writes(load_schema()), set(fl.LINK_TYPES.values()))
        self.assertEqual(set(spec.inputs(load_schema())), {
            "Attachment", "ContainerMember", "SharePointFile", "Product", "FgItem"})


if __name__ == "__main__":
    unittest.main()
