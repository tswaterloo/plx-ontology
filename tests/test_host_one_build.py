"""One projection build on the real host: document-field links, reviewed links and reviewed
aliases, through ``python3 -m operating.host`` over one state directory.

A supplier's proforma invoice (a PDF attachment) names a Portal purchase order and the
supplier's own number. The host's ``check`` cycles project it as a ``SupplierConfirmation``
linked to the PO (``SupplierConfirmation.confirms``, lead). A person then accepts that link
and confirms the supplier number as the PO's alias (the review store ``plx.review``
writes); the next cycle, with no capture change, projects the reviewed link; a later mail
naming only the supplier number links to the PO; and after a restart the MCP host's
``plx.find`` answers the number with the PO. A reader who may not read the reviewed
evidence sees none of it. Every build (host and checks) runs ``operating.build_steps``.

Evidence label: ``injected-transport`` (fake Graph, BC and Portal Workbench senders behind
the real GET-only clients; no network, no credential).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from devloop import live_verify as lv
from integrations.tests import test_capture_mail as mail_t
from integrations.tests.test_source_mirror import BASE
from ontology import identifiers, reviews, store_ddl
from ontology import schema as schema_loader
from ontology.project import CapturedRecord, build_store
from ontology.mentions import Document, TextField
from ontology.resolve import ReviewedAliases
from ontology.types import Actor
from operating import build_steps, ontology_tools as ot
from operating import host as host_module
from operating.ontology_tools_traverse import TraverseTools
from operating import session_identity as si
from operating.tests.test_host import (CASE_COWORKER, ENV, HANGING_RUN, INIT, Directory,
                                      HostCase, Lines, call)
from operating.tests.test_session_identity import (OID_A, SECRET, Fixture, identity_files,
                                                   user_row)

ROOT = Path(__file__).resolve().parents[1]
NUMBER = "PI-TST900111"          # the supplier's own number (synthetic)
PO_NUMBER = "PO100236"
PO_RELATION = "public.app_purchase_order"
CONFIRMS = "SupplierConfirmation.confirms"
AUTHOR = {"principal": f"employee:{OID_A}", "kind": "employee"}
HAS_PDF = bool(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"))


def one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()


def current_version(conn, object_id):
    return one(conn, "SELECT l1_handle, digest FROM object_versions WHERE object_id = ? AND "
               "superseded_at IS NULL", object_id)


@unittest.skipUnless(HAS_PDF, "the confirmation is read from a PDF: needs pypdf and reportlab")
class HostOneBuildTests(HostCase):
    def setUp(self) -> None:
        from ontology.tests.test_document_fields import pdf

        super().setUp()
        self.workbench.catalog[PO_RELATION] = {
            "columns": ["id", "po_number", "status", "custom_fields", "updated_at"],
            "key": ["id"], "domain": "test",
            "since": {"column": "updated_at", "mode": "timestamp-utc"}}
        self.workbench.tables[PO_RELATION] = [
            {"id": "po-1", "po_number": PO_NUMBER, "status": "SUBMITTED",
             "custom_fields": {"source": "etl-silver-to-gold"},
             "updated_at": "2026-09-28T08:00:00"}]
        invoice = pdf([["PROFORMA INVOICE", f"PI No: {NUMBER}", "Date: 2026-09-14",
                        f"Buyer PO: {PO_NUMBER}", "Total Amount: USD 1,200.00"]])
        received = self.router.mail.received
        self.router.mail.put(mail_t.BOX_A, "m-pi", "ck1", received=received,
                             subject=f"Proforma invoice {NUMBER} for {PO_NUMBER}",
                             body="Please find our PI attached.",
                             attachments=[mail_t.file_attachment("ATT-PI", invoice, "pi.pdf")])
        self.config = self.write_config(portal={"base_url": BASE, "relations": sorted(
            self.workbench.catalog)}, case_coworker=CASE_COWORKER)

    def write_config(self, **extra) -> Path:
        """The host config; the employee owns the captured mailbox (mail is read only
        through the mailbox owner ACL: the Portal user's email and the live audience's
        identity), classified internal so the employee's clearance (STAFF) reads it."""
        path = super().write_config(**extra)
        identity_files(self.root / "config" / "identity",
                       {"staff:operations": ("VIEW_OPERATIONS", "operations.sales", "ops")},
                       users=[user_row(OID_A, mail_t.UPN_A)])
        audience = self.root / "config" / "capture-audience.json"
        document = json.loads(audience.read_text("utf-8"))
        document["identities"] = {f"employee:{OID_A}": [mail_t.UPN_A]}
        document["classifications"] = {"mail:*": "internal"}
        audience.write_text(json.dumps(document), "utf-8")
        return path

    def projection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(host_module.projection_path(self.state))
        self.addCleanup(conn.close)
        return conn

    def objects(self, conn, object_type, text=""):
        return [r[0] for r in conn.execute(
            "SELECT object_id FROM objects WHERE object_type = ? AND object_id LIKE ? "
            "ORDER BY object_id", (object_type, f"%{text}%"))]

    def test_the_host_builds_document_links_reviews_and_reviewed_aliases(self):
        spec = json.loads((ROOT / "devloop/live-checks/host.cycles.json").read_text())
        self.probe_dir(self.state)
        code, out, err = self.main("check", "--state-dir", str(self.state),
                                   "--config", str(self.config),
                                   "--interrupt-after-seconds", "5", child=HANGING_RUN)
        document = json.loads(out)
        self.assertEqual(lv.check_expectations(spec, code, document), [], (err, document))
        # The live spec's own recorded summary of this run carries the step counts.
        recorded = lv._recorded(spec, document)
        self.assertEqual([sorted(s) for s in recorded["cycles[*].projection.steps"]],
                         [["confirmations", "document_fields", "mentions", "reviews",
                           "text_index", "twins", "your_reference"]] * 2)
        steps = document["cycles"][-1]["projection"]["steps"]
        self.assertGreaterEqual(steps["document_fields"]["links"], 1, steps)

        conn = self.projection()
        (po,) = self.objects(conn, "PurchaseOrder", "po-1")
        (confirmation,) = self.objects(conn, "SupplierConfirmation")
        (mail,) = self.objects(conn, "Email", "m-pi")
        self.assertEqual(conn.execute(
            "SELECT tier, binding_kind FROM links WHERE link_type = ? AND from_object = ? AND "
            "to_object = ?", (CONFIRMS, confirmation, po)).fetchall(), [("lead", "mention")])

        # A person accepts the link and confirms the supplier number as the PO's alias
        # (as plx.review records them: the versions reviewed, the field that states it).
        store = reviews.ReviewStore(reviews.store_path(self.state))
        handle, digest = current_version(conn, confirmation)
        store.record(tenant="plx", author=AUTHOR, decision="accept_link", subject=confirmation,
                     target=po, link_type=CONFIRMS, evidence=[
                         {"object_id": confirmation, "l1_handle": handle, "digest": digest}])
        handle, digest = current_version(conn, mail)
        store.record(tenant="plx", author=AUTHOR, decision="alias", subject=po,
                     alias={"identifier_kind": "vendor_confirmation", "normalized": NUMBER,
                            "raw": NUMBER},
                     evidence=[{"object_id": mail, "l1_handle": handle, "digest": digest,
                                "field_path": "row.subject"}])
        digest_before = one(conn, "SELECT value FROM store_meta WHERE key = 'content_digest'")
        conn.close()

        # Adding a review rebuilds: the next cycle, with no capture change, projects it.
        self.clock.advance(900)
        cycle = self.run_cycle()
        self.assertEqual(cycle["projection"]["state"], "built", cycle)
        self.assertEqual(cycle["projection"]["steps"]["reviews"]["links"], 1, cycle)
        conn = self.projection()
        self.assertIn(("reviewed", "reviewed"), conn.execute(
            "SELECT tier, binding_kind FROM links WHERE link_type = ? AND from_object = ? AND "
            "to_object = ?", (CONFIRMS, confirmation, po)).fetchall())
        self.assertNotEqual(one(conn, "SELECT value FROM store_meta WHERE key = "
                                "'content_digest'"), digest_before)
        conn.close()

        # A later mail naming only the confirmed supplier number links to the PO.
        self.router.mail.put(mail_t.BOX_A, "m-later", "ck1", received=self.router.mail.received,
                             subject=f"Booking for {NUMBER}", body="Cargo ready next week.")
        self.clock.advance(900)
        cycle = self.run_cycle()
        # The reviewed mail, its proforma attachment and the later mail name the number.
        self.assertEqual(cycle["projection"]["steps"]["mentions"]["reviewed_alias_links"], 3,
                         cycle)
        conn = self.projection()
        (later,) = self.objects(conn, "Email", "m-later")
        links = conn.execute("SELECT reason, evidence_handles, tier FROM links WHERE "
                             "from_object = ? AND to_object = ?", (later, po)).fetchall()
        self.assertEqual(len(links), 1, links)
        reason, evidence, tier = links[0]
        self.assertEqual(tier, "lead")
        self.assertIn("reviewed_alias", [f.get("via") for f in json.loads(reason)])
        self.assertIn(handle, json.loads(evidence))   # the reviewed evidence is cited

        # After a restart the MCP host's plx.find answers the number with the PO. The check
        # left its probe case here, so no host serves this directory until the operator
        # takes it over (the marker removed); the probe case is then the operator's.
        (self.state / host_module.PROBE_STATE_FILE).unlink()
        self.config = self.write_config(entra_directory=True, portal={
            "base_url": BASE, "relations": sorted(self.workbench.catalog)})
        output = io.BytesIO()
        # The MCP session reads its people exclusion from its process environment.
        with patch.dict(os.environ, {"T": Fixture.ticket(author=si.AUTHOR_AGENT),
                                     si.SESSION_TICKET_SECRET_ENV: SECRET,
                                     "PLX_EXCLUDED_PEOPLE_OIDS":
                                         ENV["PLX_EXCLUDED_PEOPLE_OIDS"]}):
            code, _out, err = self.main(
                "mcp", "--state-dir", str(self.state), "--config", str(self.config),
                "--session-ticket-env", "T", sender=Directory(), monotonic=lambda: 0.0,
                input_stream=Lines(INIT + [call(2, "plx.find", {"query": NUMBER})]),
                output_stream=output)
        self.assertEqual(code, 0, err)
        found = [json.loads(line) for line in output.getvalue().splitlines()
                 if json.loads(line).get("id") == 2][0]["result"]
        self.assertFalse(found["isError"], found)
        self.assertIn(po, [c["object"]["object_id"]
                           for c in found["structuredContent"]["candidates"]], found)

        # A reader who may not read the reviewed mail sees neither the alias nor the link.
        self.assertRestrictedReaderSeesNothing(po, later, handle)

    def assertRestrictedReaderSeesNothing(self, po, later, reviewed_handle):
        reader = Actor("plx", frozenset({"staff:reader"}), "restricted")
        policy = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"staff:reader"},
                                                        "mail:*": {"staff:reader"}},
                               markings={m: {"staff:reader"} for m in store_ddl.MARKINGS})

        def tools(gate):
            store = ot.StoreReader.open(host_module.projection_path(self.state))
            self.addCleanup(store.conn.close)
            return ot.OntologyTools(
                store, policy, item_gate=gate, grammar=identifiers,
                schema=ot.SchemaView.from_compiled(schema_loader.load_schema()),
                contracts=ot.load_contracts(),
                aliases=ReviewedAliases(reviews.ReviewStore(reviews.store_path(self.state))))

        conn = self.projection()
        link_type = one(conn, "SELECT link_type FROM links WHERE from_object = ? AND "
                        "to_object = ?", later, po)[0]
        conn.close()
        reviewed_mail = reviewed_handle.rsplit("@", 1)[0]
        seen = {}
        for name, gate in (("full", lambda actor, handle: True),
                           ("restricted", lambda actor, handle:
                            not handle.startswith(reviewed_mail))):
            ontology = tools(gate)
            found = ontology.call("plx.find", {"query": NUMBER}, reader)
            around = TraverseTools(ontology).call("plx.around", {"from": later, "path": [
                {"link_type": link_type, "direction": "forward"}], "tier_floor": "lead"},
                reader)
            self.assertNotIn("error", around, around)
            seen[name] = (po in [c["object"]["object_id"] for c in found.get("candidates", [])],
                          po in [edge["to"] for edge in around["edges"]])
        self.assertEqual(seen, {"full": (True, True), "restricted": (False, False)}, seen)


class StaticMail:
    """Mail records with their subjects as mention documents (``documents()``)."""

    source = "graph_mail"

    def __init__(self, records):
        self._records = records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}

    def documents(self):
        for r in self._records:
            yield Document(r.l1_handle, "email", (TextField(r.l1_handle, "row.subject",
                                                            r.row["subject"]),),
                           digest=r.digest, recorded_at=r.recorded_at,
                           superseded_at=r.superseded_at)


class StaticPortal(StaticMail):
    source = "portal_api"

    def documents(self):
        return iter(())


T0, T1, T2 = ("2026-09-01T10:00:00.000000Z", "2026-09-05T10:00:00.000000Z",
              "2026-10-03T09:00:00.000000Z")
BOX = "orders@plx.test"
PO_ID = 'PurchaseOrder:portal.fm_generated_purchase_order:{"id":9}'


def po_record():
    relation = "public.generated_purchase_order"
    return CapturedRecord("portal_api", relation, json.dumps({"id": 9}),
                          {"id": 9, "ponumber": PO_NUMBER}, f"api://{relation}/9@g-1", "row",
                          "0" * 64, T0, None, T0, f"api:{relation}")


def mail_record(mid, subject, start, end=None, digest="1"):
    handle = f"graph-mail://{BOX}/{mid}@CK-{digest}"
    row = {"mailbox": BOX, "id": mid, "subject": subject, "from": "sales@supplier.test",
           "received_at": "2026-09-01T09:59:00Z"}
    return CapturedRecord("graph_mail", "messages", json.dumps([mid, BOX]), row, handle, "row",
                          digest * 64, start, end, start, f"mail:{BOX}")


class StepListCardinalityTests(unittest.TestCase):
    """The one step list over N later mails naming a reviewed alias: one alias link per
    mail, none lost or duplicated; evidence that changes ends every one of them."""

    def build(self, state: Path, mails) -> sqlite3.Connection:
        path = state / f"projection-{len(mails)}.sqlite"
        build_store(path, schema_loader.load_schema(),
                    {"portal_api": StaticPortal([po_record()]), "graph_mail": StaticMail(mails)},
                    normalizer=identifiers.normalize,
                    clock=lambda: dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
                    post_project=build_steps.post_project(state, "plx"))
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return conn

    def test_build_and_find_give_one_alias_row_for_the_same_decision(self):
        # One alias decision, superseded by a later one and back in force when that one is
        # retracted: the row the build resolves mentions with and the row plx.find answers
        # are the same row (ontology.reviews.alias_row), from when it was back in force.
        evidence = mail_record("M-0", f"Order confirmation {NUMBER} for {PO_NUMBER}", T0)
        mail_id = 'Email:graph_mail.messages:{"id":"M-0","mailbox":"orders@plx.test"}'
        times = iter(dt.datetime(2026, 10, 2, 12, n, tzinfo=dt.timezone.utc) for n in range(3))
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            store = reviews.ReviewStore(reviews.store_path(state), clock=lambda: next(times))
            alias = {"identifier_kind": "vendor_confirmation", "normalized": NUMBER,
                     "raw": NUMBER}
            cite = {"object_id": mail_id, "l1_handle": evidence.l1_handle,
                    "digest": evidence.digest}
            store.record(tenant="plx", author=AUTHOR, decision="alias", subject=PO_ID,
                         alias=alias, evidence=[dict(cite, field_path="row.subject")])
            later, _ = store.record(tenant="plx", author=AUTHOR, decision="alias",
                                    subject=PO_ID, alias=alias, evidence=[cite])
            back, _ = store.record(tenant="plx", author=AUTHOR, decision="retract",
                                   subject=PO_ID, evidence=[], retracts=later["review_id"])
            conn = self.build(state, [evidence])
            built = reviews.BuildAliases(store, "plx").rows(conn, "vendor_confirmation", NUMBER)
            params = {"aud": json.dumps([k for (k,) in conn.execute(
                          "SELECT DISTINCT audience_key FROM objects")]),
                      "marks": json.dumps([m for (m,) in conn.execute(
                          "SELECT DISTINCT markings FROM object_versions")]),
                      "at": "2026-10-04T00:00:00Z"}
            found = ReviewedAliases(store).rows(conn, "plx", [("vendor_confirmation", NUMBER)],
                                                params, lambda handle: True)
        shape = ("object_id", "object_type", "identifier_kind", "normalized", "raw",
                 "property", "role", "l1_handle", "field_path", "lineage", "digest",
                 "markings", "recorded_at", "superseded_at", "review_id", "evidence_handles")
        self.assertEqual(len(built), 2, built)   # before the later decision, and again after
        self.assertEqual([{k: row[k] for k in shape} for row in found],
                         [{k: row[k] for k in shape} for row in built[-1:]])
        self.assertEqual((found[0]["lineage"], found[0]["field_path"], found[0]["recorded_at"],
                          found[0]["superseded_at"]),
                         ("workbench", "row.subject", back["recorded_at"], None))

    def test_n_later_mails_each_get_one_alias_link_until_the_evidence_changes(self):
        evidence = mail_record("M-0", f"Order confirmation {NUMBER} for {PO_NUMBER}", T0)
        for n in (1, 10, 60):
            with tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                later = [mail_record(f"M-{i}", f"Shipping {NUMBER} booked", T1)
                         for i in range(1, n + 1)]
                store = reviews.ReviewStore(reviews.store_path(state), clock=lambda: dt.datetime(
                    2026, 10, 2, 12, tzinfo=dt.timezone.utc))
                store.record(tenant="plx", author=AUTHOR, decision="alias", subject=PO_ID,
                             alias={"identifier_kind": "vendor_confirmation",
                                    "normalized": NUMBER, "raw": NUMBER},
                             evidence=[{"object_id": 'Email:graph_mail.messages:{"id":"M-0",'
                                                     '"mailbox":"orders@plx.test"}',
                                        "l1_handle": evidence.l1_handle,
                                        "digest": evidence.digest,
                                        "field_path": "row.subject"}])
                # The same alias of a record the projection does not hold: no candidate.
                store.record(tenant="plx", author=AUTHOR, decision="alias",
                             subject=PO_ID.replace('"id":9', '"id":404'),
                             alias={"identifier_kind": "vendor_confirmation",
                                    "normalized": NUMBER, "raw": NUMBER},
                             evidence=[{"object_id": 'Email:graph_mail.messages:{"id":"M-0",'
                                                     '"mailbox":"orders@plx.test"}',
                                        "l1_handle": evidence.l1_handle,
                                        "digest": evidence.digest,
                                        "field_path": "row.subject"}])
                conn = self.build(state, [evidence, *later])
                rows = conn.execute(
                    "SELECT from_object, superseded_at FROM links WHERE "
                    "reason LIKE '%reviewed_alias%' AND to_object = ?", (PO_ID,)).fetchall()
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM links WHERE reason LIKE "
                                              "'%reviewed_alias%'").fetchone()[0], n + 1)
                self.assertEqual(len(rows), n + 1, n)
                self.assertEqual(len({r[0] for r in rows}), n + 1, n)
                self.assertTrue(all(r[1] is None for r in rows), rows)
                # The reviewed mail captured again with other content: the review no
                # longer holds from then on (and a store without the review links nothing).
                changed = [mail_record("M-0", evidence.row["subject"], T0, T2),
                           mail_record("M-0", "Revised confirmation", T2, digest="2"), *later]
                ended = self.build(state, changed).execute(
                    "SELECT DISTINCT superseded_at FROM links WHERE to_object = ? AND "
                    "reason LIKE '%reviewed_alias%'", (PO_ID,)).fetchall()
                self.assertEqual(ended, [(T2,)], n)
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(self.build(Path(tmp), [evidence, *later]).execute(
                    "SELECT COUNT(*) FROM links WHERE reason LIKE '%reviewed_alias%'"
                ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
