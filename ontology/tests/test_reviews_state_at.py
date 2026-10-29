"""A person's link decision pins a cited purchase order's identity for every reader (ER-09).

Directive ecd78798fbbe: ``ontology.reviews.state_at`` (what ``plx.review`` reads) and the
projection's reviewed link (``link_step``, what ``plx.around`` walks and ``case_mail``
relates by) apply one rule, ``ontology.reviews.same_po_identity``: a status-only change of
the PO row or an in-place Portal renumber to its BC number (number and supplier otherwise
unchanged) keeps the decision; a supplier or other number change withdraws it in every
reader. The one PO row history is the mirror's, read by the projection's readers over a
store built from the cited and current row versions (``projected``). A review store that
is there but unreadable refuses a case mail read with ``CaseMailError``, never a raw
exception.

Evidence label: ``injected-transport`` (the fixtures and fakes of ``test_case_mail``;
sockets refused) and isolated-fixture (the built store).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from ontology import identifiers
from ontology import reviews as rv
from ontology import schema as schema_loader
from ontology import store_ddl
from ontology.project import CapturedRecord, build_store
from operating import case_mail
from operating import ontology_tools as ot
from operating import ontology_tools_review as rt
from operating.ontology_tools_traverse import TraverseTools
from operating.tests.test_case_mail import PERSON, PURCHASING, CaseMailTestCase
from tests.test_ontology_tools_review import CONTRACTS, OID_A, Static, employee_session

LINK = "Email.mentions_purchase_order"
PO_RELATION = "public.app_purchase_order"
# The projection's store times (``projected``): the cited row version, its change, the
# person's decision between them and the read after.
T_CITED, T_MOVED = "2026-09-01T00:00:00.000000Z", "2026-09-29T08:00:00.000000Z"
REVIEWED_AT = dt.datetime(2026, 9, 15, 12, tzinfo=dt.timezone.utc)
READ_AT = dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone.utc)
# Portal's BC-align renumber of the row whose number was PO100123 (align-portal-po-number.ts).
RENUMBERED = {"po_number": "PO100500", "custom_fields": {
    "fmPoNumber": "PO100123", "renumberedTo": "PO100500",
    "renumberedAt": "2026-09-29T08:00:00Z", "renumberSource": "bc-drain-align"}}


class EveryReaderTests(CaseMailTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Zoned PO timestamps, so the mirror's incremental refresh reads each later change.
        self.fake.catalog[PO_RELATION]["since"]["mode"] = "timestamptz"
        self.fake.tables["public.app_supplier"].append({
            "id": "sup-other", "code": "OTHER", "name": "Other Vendor",
            "email": "other@gmail.com", "website": None, "updated_at": "2026-09-01T00:00:00Z"})
        self.reviews = rv.ReviewStore(self.root / "reviews.sqlite", clock=lambda: REVIEWED_AT)
        self.changes = 0

    def accepted(self) -> tuple[dict, dict]:
        """A person's accepted link of m-confirm to its PO (the case relation's ``review``),
        recorded at ``REVIEWED_AT`` citing the PO row version the mirror holds."""
        runtime = self.runtime()
        report = self.evidence(runtime, self.po_case(runtime))
        [relation] = self.mention_relations(report, "m-confirm")
        [message] = [m for m in report["messages"] if "/m-confirm@" in m["metadata_citation"]]
        [row] = [item for item in runtime.mirror.rows(PURCHASING, PO_RELATION, limit=50)["rows"]
                 if item["citation"] == relation["po_citation"]]
        link = {"review": relation["review"], "message": message["metadata_citation"],
                "po": {"citation": row["citation"], "key": row["key"]}}
        review = relation["review"]
        recorded = self.reviews.record(
            tenant="plx", author=PERSON, decision="accept_link", subject=review["subject"],
            target=review["target"], link_type=review["link_type"], evidence=[
                {"object_id": review["subject"], "l1_handle": link["message"],
                 "digest": "d" * 64},
                {"object_id": review["target"], "l1_handle": row["citation"],
                 "digest": row["content_sha256"]}])[0]
        return link, recorded

    def change_po(self, link: dict, **columns) -> None:
        """The cited PO row changes at the source; the mirror reads the change."""
        row = next(r for r in self.fake.tables[PO_RELATION] if r["id"] == link["po"]["key"]["id"])
        self.changes += 1
        self.clock.advance(3600)
        row.update(columns, updated_at=f"2026-09-29T{self.changes:02d}:00:00Z")
        self.runtime().mirror.rows(PURCHASING, PO_RELATION, limit=50, max_staleness=0)

    def projected(self, link: dict, review: dict) -> dict[str, Any]:
        """``review`` as the projection's readers hold it now: a store built (with
        ``link_step``) from the message, the supplier rows and the cited and current
        versions of the PO row the mirror holds, read by ``state_at`` with that store's
        identities, by ``plx.review`` status and by ``plx.around`` (the reason features of
        its edges to the order)."""
        mirror = self.runtime().mirror
        cited = mirror.resolve_citation(link["po"]["citation"], PURCHASING)
        [now] = mirror.rows(PURCHASING, PO_RELATION, where=[
            ["id", "eq", link["po"]["key"]["id"]]], limit=2)["rows"]
        digest = review["evidence"][1]["digest"]
        moved = now["content_sha256"] != digest

        def row(relation, values, handle, sha, start, end=None):
            return CapturedRecord("portal_api", relation, json.dumps({"id": values["id"]}),
                                  values, handle, "row", sha, start, end, start,
                                  f"api:{relation}")

        records = [row(PO_RELATION, cited["row"], link["po"]["citation"], digest, T_CITED,
                       T_MOVED if moved else None)]
        if moved:
            records.append(row(PO_RELATION, now["row"], now["citation"], now["content_sha256"],
                               T_MOVED))
        records += [row("public.app_supplier", item["row"], item["citation"],
                        item["content_sha256"], T_CITED)
                    for item in mirror.rows(PURCHASING, "public.app_supplier", limit=50)["rows"]]
        message = json.loads(review["subject"].split(":", 2)[2])
        # The message names no order itself: its only link to the order is the person's.
        mail = CapturedRecord(
            "graph_mail", "messages", json.dumps([message["id"], message["mailbox"]]),
            {**message, "subject": "Order confirmation", "received_at": "2026-09-01T00:00:00Z"},
            review["evidence"][0]["l1_handle"], "row", review["evidence"][0]["digest"], T_CITED,
            None, T_CITED, f"mail:{message['mailbox']}")
        path = self.root / f"projection-{self.changes}.sqlite"
        build_store(path, schema_loader.load_schema(),
                    {"portal_api": Static("portal_api", records),
                     "graph_mail": Static("graph_mail", [mail])},
                    normalizer=identifiers.normalize, clock=lambda: READ_AT,
                    post_project={"reviews": rv.link_step(self.reviews, "plx")})
        principal = f"employee:{OID_A}"
        base = ot.OntologyTools(
            ot.StoreReader.open(path), ot.ReadPolicy(
                tenant="plx", audiences={"api:*": {principal}, "mail:*": {principal}},
                markings={m: {principal} for m in store_ddl.MARKINGS}),
            item_gate=lambda actor, handle: True, grammar=identifiers,
            schema=ot.SchemaView.from_compiled(schema_loader.load_schema()),
            contracts=CONTRACTS, clock=lambda: READ_AT)
        self.addCleanup(base.store.conn.close)
        conn = base.store.conn
        current = {oid: sha for oid, sha in conn.execute(
            "SELECT object_id, digest FROM object_versions WHERE superseded_at IS NULL")}
        session = employee_session(OID_A)
        status = rt.ReviewTools(base, self.reviews, session=session).call(
            "plx.review", {"mode": "status", "subject": review["subject"]}, session.actor)
        [shown] = [r for r in status["reviews"] if r["review_id"] == review["review_id"]]
        around = TraverseTools(base).call("plx.around", {"from": review["subject"], "path": [
            {"link_type": LINK, "direction": "forward"}], "tier_floor": "lead"}, session.actor)
        return {"state_at": rv.state_at(self.reviews.history("plx", review["subject"]),
                                        current.get, identity=rv.version_identity(conn)
                                        )[review["review_id"]],
                "plx.review": (shown["state"], shown.get("state_reason")),
                "plx.around": [f["feature"] for e in around["edges"]
                               if e["to"] == review["target"] for f in e["reason"]]}

    def test_a_status_only_change_keeps_the_decision_and_an_identity_change_withdraws_it(self):
        link, review = self.accepted()
        kept = {"state_at": ("effective", None), "plx.review": ("effective", None),
                "plx.around": ["person_reviewed"]}
        gone = {"state_at": ("withdrawn", "evidence_changed"),
                "plx.review": ("withdrawn", "evidence_changed"), "plx.around": []}
        self.assertEqual(self.projected(link, review), kept)
        # Each step changes the row from the one before; the decision compares the
        # version it cites with the current one, so a restored identity holds again.
        for name, columns, expected in (
                ("status only", {"status": "CLOSED"}, kept),
                ("supplier", {"supplier_id": "sup-other"}, gone),
                ("supplier restored", {"supplier_id": "sup-acme"}, kept),
                ("bc renumber in place", RENUMBERED, kept),
                ("supplier changed during a renumber", {"supplier_id": "sup-other"}, gone),
                ("number", {"supplier_id": "sup-acme", "custom_fields": {
                    "fmPoNumber": "PO#9999"}}, gone)):
            with self.subTest(name):
                self.change_po(link, **columns)
                self.assertEqual(self.projected(link, review), expected)


class CaseMailReviewStoreTests(CaseMailTestCase):
    def test_an_unreadable_review_store_refuses_the_case_read(self):
        runtime = self.runtime()
        case_id = self.po_case(runtime)
        # The projection is built while the store is readable (a build over an unreadable
        # store fails by itself); the store breaks before the case read.
        self.project(runtime)
        path = rv.store_path(self.state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a review store, " * 64)
        with self.assertRaises(case_mail.CaseMailError) as caught:
            case_mail.case_mail_evidence(runtime, PURCHASING, case_id,
                                         mail_capture_dir=self.state)
        self.assertEqual(caught.exception.code, "review_store_unreadable")


if __name__ == "__main__":
    import unittest

    unittest.main()
