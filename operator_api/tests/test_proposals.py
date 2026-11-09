"""Proposed follow-ups and record updates (``operator_api.proposals``) at the app boundary:
drafted only from what the gated trail and page show this reader, every date and figure a
draft states is a cited figure, an unknown contact is said and left empty, a reader who
can't open the vendor's message sees neither its sender, its subject nor its date, and
nothing is sent (isolated-fixture: the trail's fake view and the mock backend)."""

from __future__ import annotations

import json
import re
import unittest

from operator_api import models as m
from operator_api import proposals as pr
from operator_api.mock import MockBackend
from operator_api.tests.test_trail import MAIL, OPEN, SHUT, View, mapper, raw_trail

FIGURE = re.compile(r"\d{4}-\d{2}-\d{2}|\d[\d,.]*")


def page_of(trail: m.PoTrail, *, kind="PurchaseOrder", lines=(), facts=()) -> m.ObjectPage:
    return m.ObjectPage(object_id=trail.subject.object_id, type=kind, label=trail.subject.label,
                        summary="", status=m.ObjectStatus(state="late", text="late"),
                        facts=list(facts), late_lines=list(lines),
                        trail=m.EvidenceTrail(kind="purchase_order", title="t", trails=[trail]),
                        freshness=m.Freshness())


def grounded(test: unittest.TestCase, proposal: m.Proposal) -> None:
    """Every date and number the draft states is one of its cited figures."""
    from operating.copy_gate import say_date
    # a cited date is written "Thu 15 Jan 2099" in the draft
    cited = " ".join(f"{f.value} {say_date(f.value)}" for f in proposal.figures
                     if f.source is not None)
    labels = " ".join(f"{f.value} {say_date(f.value)}" for f in proposal.figures)
    for said in FIGURE.findall(proposal.body or ""):
        said = said.rstrip(".,")
        test.assertTrue(said in cited or said in labels, f"{said!r} is not a cited figure")
    test.assertFalse(proposal.sent)


class ProposalBoundaryTests(unittest.TestCase):
    def test_late_po_chase_cites_every_figure_and_names_the_cleared_contact(self):
        trail = mapper(View()).po(raw_trail())
        line = m.LateLine(line="10000", item="ITEM-1", quantity="18 KG", due_date="2099-01-15",
                          days_late=17, sources=[trail.steps[0].source])
        got = pr.for_page(page_of(trail, lines=[line]))
        chase = got[0]
        self.assertEqual((chase.kind, chase.to, chase.to_name),
                         ("vendor_chase", "bob@vendor.example", "Bob"))
        for said in ("PO 1", "Thu 15 Jan 2099", "ITEM-1: 18 KG", "Wed 28 Jan 2099",
                     "confirm the date these items will ship"):
            self.assertIn(said, chase.body)
        # the body stays within the staff's p75 length: the last message is a cited figure
        # beside the draft rather than a sixth sentence in it
        self.assertLessEqual(len(chase.body.split("\n- ")[0].split()), 50)
        shown = json.dumps([f.model_dump() for f in chase.figures])
        for said in ("2099-01-25", "SECRET-SUBJECT"):
            self.assertIn(said, shown)
        self.assertEqual(chase.missing, [])
        grounded(self, chase)
        # the confirmed date is read from a BC record, not a vendor message: no update
        self.assertEqual([p.kind for p in got], ["vendor_chase"])

    def test_a_reader_who_cant_open_the_vendor_mail_gets_no_contact_nor_its_date(self):
        trail = mapper(View(refused={SHUT, MAIL})).po(raw_trail())
        got = pr.for_page(page_of(trail))
        chase = got[0]
        self.assertIsNone(chase.to)
        self.assertIn("No vendor contact found", chase.contact_note)
        self.assertIn("The vendor's contact address", chase.missing)
        self.assertIn("We have no message from you about this order on record.", chase.body)
        body = json.dumps([p.model_dump() for p in got])
        for secret in ("SECRET", "msg-secret", "vendor.example", "Bob", "2099-01-28",
                       "2099-01-25", SHUT):
            self.assertNotIn(secret, body)
        grounded(self, chase)

    def test_a_mail_confirmation_the_record_does_not_reflect_is_a_proposed_update(self):
        raw = raw_trail()
        raw["eta"]["handle"] = MAIL   # the vendor confirmed by mail
        raw["steps"].insert(1, {"kind": "due_date", "title": "Expected on a line by 2099-01-15",
                                "handle": OPEN, "check": "read_from_record", "at": None})
        update = [p for p in pr.for_page(page_of(mapper(View()).po(raw)))
                  if p.kind == "record_update"]
        self.assertEqual(len(update), 1)
        change = update[0].change
        self.assertEqual((change.system, change.field, change.before, change.after),
                         ("Portal", "Expected receipt date", "2099-01-15", "2099-01-28"))
        self.assertEqual((change.before_source.handle, change.evidence.handle), (OPEN, MAIL))
        self.assertFalse(update[0].sent)
        # the reader who can't open that mail sees no proposed update at all
        self.assertEqual([p.kind for p in pr.for_page(page_of(
            mapper(View(refused={MAIL})).po(raw)))], ["vendor_chase"])

    def test_nothing_is_proposed_for_a_po_that_is_not_late_or_another_page(self):
        raw = raw_trail()
        for state in ("on_time", "not_due", "undetermined", None, "unknown-state"):
            raw["state"] = state
            self.assertEqual(pr.for_page(page_of(mapper(View()).po(raw))), [], state)
        trail = mapper(View()).po(raw_trail())
        self.assertEqual(pr.for_page(page_of(trail, kind="SalesOrder")), [])

    def test_customer_heads_up_per_order_at_any_count(self):
        for n in (1, 10, 60):
            ref = lambda kind, i: m.ObjectRef(object_id=f"{kind}:x:{i}", type=kind,
                                              label=f"{kind} {i}", open_url="/x")
            rows = [m.MaterialRow(order=ref("SalesOrder", i % n), assembly=ref("Assembly", i),
                                  item=ref("Item", i), po=ref("PurchaseOrder", i),
                                  state="late", days_late=3) for i in range(n * 2)]
            rows.append(m.MaterialRow(order=ref("SalesOrder", n + 1), assembly=ref("A", 0),
                                      item=ref("Item", 0), po=ref("PurchaseOrder", 0),
                                      state="on_time"))
            answer = m.AnswerPayload(
                answer_id="a", question="q", outcome="answered", direct_answer="d",
                hidden=m.Hidden(count=0), freshness=m.Freshness(), conversation_id="c",
                latency_ms=1, trail=m.EvidenceTrail(kind="materials", title="t",
                                                    materials=rows))
            got = pr.for_answer(answer)
            self.assertEqual(sorted(p.subject.object_id for p in got),
                             sorted(f"SalesOrder:x:{i}" for i in range(n)), n)
            self.assertTrue(all(p.kind == "customer_heads_up" and p.to is None
                                and "No customer contact found" in p.contact_note
                                for p in got))
            for p in got:
                grounded(self, p)

    def test_a_po_pages_drafts_come_with_its_trail_section_as_the_full_page_has_them(self):
        """The app shows the quick read with each section merged: the trail section
        carries the drafts built from the record, its late lines (the late section) and
        the trail, so the merged page has the full page's drafts."""
        import contextlib
        from types import SimpleNamespace
        from unittest import mock
        from operator_api import backend as be
        trail = mapper(View()).po(raw_trail())
        line = m.LateLine(line="10000", item="ITEM-1", quantity="18 KG", due_date="2099-01-15",
                          days_late=17, sources=[trail.steps[0].source])
        full = page_of(trail, lines=[line])
        quick = full.model_copy(update={"trail": None, "late_lines": [], "complete": False})
        late = m.ObjectSection(object_id=quick.object_id, section="late", late_lines=[line])
        view = SimpleNamespace(_request=contextlib.nullcontext)
        backend = SimpleNamespace(today=None, page_cache=None)
        with mock.patch("operator_api.trail.for_page", lambda *a, **k: full.trail), \
                mock.patch.object(be, "listed_late", lambda *a, **k: None), \
                mock.patch.object(be, "kept_section",
                                  lambda b, v, page, name: late if name == "late" else None):
            section = be.read_section(backend, view, quick, "trail")
        expected = pr.for_page(full)
        self.assertEqual([p.kind for p in expected], ["vendor_chase"])
        self.assertEqual(section.proposals, expected)
        self.assertEqual(be.merged(be.merged(quick, late), section).proposals, expected)
        # the quick read alone has no trail: no drafts until the trail section lands
        self.assertEqual(pr.for_page(quick), [])

    def test_mock_po_page_serves_a_chase_and_a_proposed_update(self):
        page = MockBackend().object_page(None, "PurchaseOrder", "PurchaseOrder:demo:90412")
        m.ObjectPage.model_validate(page.model_dump())
        self.assertEqual([p.kind for p in page.proposals], ["vendor_chase", "record_update"])
        for p in page.proposals:
            grounded(self, p)


if __name__ == "__main__":
    unittest.main()
