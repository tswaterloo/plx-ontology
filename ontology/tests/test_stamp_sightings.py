"""Write-stamp sightings and link stretches (``ontology.stamps``, ``Projector``).

A capture that only restamps a record is a sighting of the version it holds, never a new
version; a link is one row per stretch of equal compared values, whatever end versions
show it; a stamp on a path records are identified or joined by is refused."""

from __future__ import annotations

import unittest

from ontology import stamps
from ontology.project import CapturedRecord, ProjectionError, Projector
from ontology.schema import load_schema
from ontology.tests.test_projection import StaticCaptures, compiled_schema, normalizer, oid

PO = oid("PurchaseOrder", "t.gold_po", id="po-a")
SUP = oid("Supplier", "t.supplier", code="BOLT")
T = ["2026-10-0%dT00:00:00.000000Z" % day for day in range(1, 7)]
DECLARED = {("portal_api", "public.app_purchase_order"): stamps.Stamps(["updated_at"]),
            ("portal_api", "public.app_supplier"): stamps.Stamps(["updated_at"])}


def _rec(relation, key, n, row, start, end, *, audience=None, seen=None):
    return CapturedRecord(
        source="portal_api", relation=relation, record_key=key, row=row,
        l1_handle=f"api://{relation}/{key}@g-{n}", field_prefix="row", digest=f"d{n}",
        recorded_at=start, superseded_at=end, last_seen_at=seen or end or start,
        audience_key=audience or f"api:{relation}")


def _po(n, start, end, status, updated, supplier="s1", **extra):
    return _rec("public.app_purchase_order", "po-a", n,
                {"id": "po-a", "po_number": "P-1", "status": status, "supplier_id": supplier,
                 "created_at": "2026-09-01T00:00:00Z", "updated_at": updated}, start, end,
                **extra)


def _sup(n, start, end, name, updated):
    return _rec("public.app_supplier", "s1", 100 + n,
                {"id": "s1", "code": "BOLT", "name": name, "updated_at": updated}, start, end)


# po-a: restamped (T1), changed status (T2), restamped (T3); the supplier renamed at T1.5
WORLD = [_po(1, T[0], T[1], "OPEN", "u0", seen="2026-10-01T12:00:00.000000Z"),
         _po(2, T[1], T[2], "OPEN", "u1", seen="2026-10-02T18:00:00.000000Z"),
         _po(3, T[2], T[3], "RECEIVED", "u2"),
         _po(4, T[3], None, "RECEIVED", "u3", seen=T[5]),
         _sup(1, T[0], "2026-10-02T12:00:00.000000Z", "Bolt Ltd", "x0"),
         _sup(2, "2026-10-02T12:00:00.000000Z", None, "Bolt Limited", "x1")]


def project(records, declared=DECLARED, **options):
    projector = Projector(compiled_schema(), {"portal_api": StaticCaptures(records)},
                          normalizer=normalizer, stamps=declared, **options)
    return projector.project()


class SightingTests(unittest.TestCase):
    def test_restamps_are_sightings_and_a_real_change_is_a_version(self):
        rows = project(WORLD)
        versions = [v for v in rows["object_versions"] if v["object_id"] == PO]
        # four captures, two contents: one version per content, each starting at the
        # capture that first held it and citing the newest that still saw it
        self.assertEqual([(v["recorded_at"], v["superseded_at"], v["l1_handle"])
                          for v in versions],
                         [(T[0], T[2], "api://public.app_purchase_order/po-a@g-2"),
                          (T[2], None, "api://public.app_purchase_order/po-a@g-4")])
        # current values are the newest capture's, the stamp included
        current = {v["property"]: v for v in rows["object_values"]
                   if v["object_id"] == PO and v["superseded_at"] is None}
        self.assertEqual(current["status"]["value"], "RECEIVED")
        self.assertEqual(current["status"]["l1_handle"],
                         "api://public.app_purchase_order/po-a@g-4")
        # the change event cites the capture that first held the new status, and its
        # window opens when the old status was last seen (unchanged by the fold)
        [event] = [e for e in rows["events"] if e["event_type"] ==
                   "PurchaseOrder.status_changed"]
        self.assertEqual((event["l1_handle"], event["digest"], event["window_start"],
                          event["window_end"]),
                         ("api://public.app_purchase_order/po-a@g-3", "d3",
                          "2026-10-02T18:00:00.000000Z", T[2]))
        # without declared stamps every capture stays a version (and the same event)
        plain = project(WORLD, declared={})
        self.assertEqual(len([v for v in plain["object_versions"] if v["object_id"] == PO]),
                         4)
        self.assertEqual([e["event_id"] for e in plain["events"]
                          if e["event_type"] == "PurchaseOrder.status_changed"],
                         [event["event_id"]])

    def test_any_number_of_restamps_is_one_version_and_one_link(self):
        for n in (1, 10, 60):   # the audit's product: 60 captures, 53,014 link rows
            times = ["2026-10-01T%02d:%02d:00.000000Z" % divmod(i * 7, 60) for i in range(n + 1)]
            world = [_po(i, times[i], times[i + 1] if i + 1 < n else None, "OPEN", f"u{i}")
                     for i in range(n)] + WORLD[4:]
            rows = project(world)
            versions = [v for v in rows["object_versions"] if v["object_id"] == PO]
            links = [l for l in rows["links"] if l["from_object"] == PO]
            with self.subTest(n=n):
                self.assertEqual([(v["recorded_at"], v["superseded_at"], v["l1_handle"])
                                  for v in versions],
                                 [(times[0], None,
                                   f"api://public.app_purchase_order/po-a@g-{n - 1}")])
                self.assertEqual([(l["link_type"], l["superseded_at"]) for l in links],
                                 [("PurchaseOrder.ordered_from", None)])

    def test_audience_change_or_gap_is_never_folded(self):
        world = [_po(1, T[0], T[1], "OPEN", "u0"),
                 _po(2, T[1], T[2], "OPEN", "u1", audience="api:narrowed"),
                 _po(3, T[3], None, "OPEN", "u2")]   # absent between T2 and T3
        versions = [v for v in project(world)["object_versions"] if v["object_id"] == PO]
        self.assertEqual(len(versions), 3)


class LinkStretchTests(unittest.TestCase):
    def test_one_link_row_per_stretch_with_an_id_no_later_capture_moves(self):
        def links(rows):
            return [link for link in rows["links"]
                    if link["link_type"] == "PurchaseOrder.ordered_from"]
        # five pairs of end versions (four PO captures, a renamed supplier), one key
        [link] = links(project(WORLD))
        self.assertEqual((link["from_object"], link["to_object"], link["recorded_at"],
                          link["superseded_at"]), (PO, SUP, T[0], None))
        self.assertIn("api://public.app_purchase_order/po-a@g-4", link["evidence_handles"])
        self.assertIn("api://public.app_supplier/s1@g-102", link["evidence_handles"])
        # as known before the last restamp and the status change: the same link, same id
        [earlier] = links(project(WORLD, as_known_at="2026-10-02T13:00:00Z"))
        self.assertEqual(earlier["link_id"], link["link_id"])
        # the key moving to another supplier ends the stretch; moving back starts another
        moved = [_po(1, T[0], T[1], "OPEN", "u0"), _po(2, T[1], T[2], "OPEN", "u1", "s2"),
                 _po(3, T[2], None, "OPEN", "u2"), *WORLD[4:]]
        spans = sorted((l["recorded_at"], l["superseded_at"]) for l in links(project(moved)))
        self.assertEqual(spans, [(T[0], T[1]), (T[2], None)])
        self.assertEqual(len({l["link_id"] for l in links(project(moved))}), 2)


class DeclarationTests(unittest.TestCase):
    def test_a_stamp_records_are_read_by_is_refused(self):
        for path in ("supplier_id", "custom_fields.source", "created_at"):
            with self.subTest(path=path), self.assertRaises(ProjectionError) as caught:
                project(WORLD, declared={("portal_api", "public.app_purchase_order"):
                                         stamps.Stamps([path])})
            self.assertEqual(caught.exception.code, "stamp_declaration_invalid")

    def test_the_declared_stamps_hold_for_the_served_schema(self):
        declared = stamps.load()
        schema = load_schema()
        bindings = {b["id"]: (tid, b) for tid, t in schema.object_types.items()
                    for b in t.get("bindings", [])}
        self.assertEqual(stamps.unbound(declared, bindings), [])
        stamps.check(declared, bindings, schema.link_types, schema.event_types,
                     schema.object_types)
        # a Teams card's regenerated attachment ids, in the list and in the body markup
        teams = declared[("teams", "chatMessages")]
        a = {"id": "m1", "body_content": 'hi <attachment id="aa11"></attachment>',
             "attachments": [{"id": "aa11", "content_type": "card"}]}
        b = {"id": "m1", "body_content": 'hi <attachment id="bb22"></attachment>',
             "attachments": [{"id": "bb22", "content_type": "card"}]}
        self.assertEqual(teams.sighting_key(a), teams.sighting_key(b))
        self.assertNotEqual(teams.sighting_key(a),
                            teams.sighting_key({**b, "body_content": "edited"}))


if __name__ == "__main__":
    unittest.main()
