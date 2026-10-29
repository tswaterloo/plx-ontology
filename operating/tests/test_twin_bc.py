"""The Business Central twin in test mode (lane twin-bc; owner directive 746d96497341).

A BC-targeted fixture action (``twin.bc.lifecycle``) runs the lifecycle the Portal and MC
twins pass, its write a PATCH with the cited etag as If-Match, with a socket guard counting
every connection attempt; company B's records, its own company record included, are
invisible from company A on every consumer (OData listing, read, PATCH, lines, citation
resolver, the action path); ``If-Match: *``, which every upstream BC PATCH sends, is
applied to a record and to a line over any save, as BC does; a stale or absent If-Match is
refused with nothing written, on OData and on the action path, which is the Workbench
owner's own stricter path only (never evidence of real BC write safety); any input the
pinned ``$metadata`` does not declare, a property it declares computed or an impossible
date fails closed. Evidence label: ``isolated-fixture``.
"""

from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ontology.core import Actor
from operating import action_adapters as adapters
from operating import action_path as ap
from operating.action_adapters import OwnerRequest
from operating.cases import CitationError, CitationUnresolvable
from operating.goal2_exec_owner import DISPATCH_SCHEMA, OwnerRejected
from operating.goal2_exec_owner import handle as owner_handle
from operating.tests.test_twin_lifecycle import NetworkGuard
from twin import mode
from twin.bc.lifecycle import (TARGET_FIELD, TARGET_SET, WORKBENCH_OWNER_ONLY, odata_path,
                                run_bc_lifecycle, targets)
from twin.bc.odata import ANY_ETAG, VERSION_FIELD
from twin.portal import TwinError
from twin.world import ACTOR, SESSION, TwinWorld


class BcTwinTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in mode.credentialed():
            os.environ.pop(name)
        os.environ.pop(adapters.ALLOW_ENV, None)

    def world(self) -> TwinWorld:
        context = mode.test_mode()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return TwinWorld(self.dir / "world")

    @staticmethod
    def send(world, method, url, headers=None, body=None):
        return world.transport.send(OwnerRequest("bc", method, url, headers or {}, body))


class LifecycleTests(BcTwinTestCase):
    def test_a_bc_targeted_fixture_action_runs_its_lifecycle_and_nothing_leaves(self):
        with NetworkGuard() as guard:
            report = run_bc_lifecycle(self.dir / "run")
        self.assertEqual(guard.attempts, [])   # 0 production-host (or any) connections
        rows = {row["scenario"]: row for row in report["scenarios"]}
        for row in rows.values():
            self.assertTrue(row["chain_ok"] and row["case_chain_ok"], row)
            self.assertEqual(row["replay_owner_calls"], 0, row)   # a replay sends nothing
        verified, edited, conflict = (rows[name] for name in
                                      ("verified", "edited_after_approval", "etag_mismatch"))
        # The refusals below rest on the Workbench owner's own cited etag, which no
        # upstream BC writer sends (ANY_ETAG): they are labelled so in the report.
        self.assertEqual(WORKBENCH_OWNER_ONLY, {"edited_after_approval", "etag_mismatch"})
        self.assertEqual([row["workbench_owner_only"] for row in (verified, edited, conflict)],
                         [False, True, True])
        # Written once by one PATCH carrying the cited etag, read back, and BC's own OData
        # shows it under a new etag.
        self.assertEqual((verified["state"], verified["outcome"], verified["read_back"],
                          verified["owner_dispatch_calls"], verified["bc_shows_value"],
                          verified["etag_moved"], verified["action_odata"],
                          verified["action_if_match_cited"]),
                         ("verified", "verified", ["effect_observed"], 1, True, True,
                          ["GET 200", "PATCH 200"], [True]))
        # Another BC user saved after the approval: refused before any send; the etag the
        # case cited is refused by BC's OData too.
        self.assertEqual((edited["state"], edited["first_reason"],
                          edited["owner_dispatch_calls"], edited["bc_shows_value"],
                          edited["stale_if_match_status"], edited["action_odata"]),
                         ("dispatch_refused", "precondition_citation_changed", 0, False, 412, []))
        # A save between the checks and the apply: the action's PATCH with the cited etag
        # is refused (412), nothing written.
        self.assertEqual((conflict["state"], conflict["first_reason"],
                          conflict["owner_dispatch_calls"], conflict["bc_shows_value"],
                          conflict["action_odata"], conflict["action_if_match_cited"]),
                         ("failed", "etag_mismatch", 1, False, ["GET 200", "PATCH 412"],
                          [True]))
        self.assertEqual(report["owner_operations"], {"applied": 1, "rejected": 1})
        self.assertEqual(report["evidence_label"], "isolated-fixture")


class CompanyScopeTests(BcTwinTestCase):
    """Company B's records are invisible from company A, whatever reads them."""

    def test_company_b_rows_are_invisible_from_company_a(self):
        world = self.world()
        company_a, company_b = world.bc.companies()
        reader_a, reader_b = (Actor(ACTOR.tenant, frozenset({p})) for p in ("ops", "ops-b"))
        template = {name: value for name, value in world.bc.resolve(
            world.bc.handles(company_b, TARGET_SET)[0], reader_b)["row"].items()
            if name != VERSION_FIELD}
        own = {row["id"] for row in self.send(
            world, "GET", f"/companies({company_a})/{TARGET_SET}")["body"]["value"]}
        seeded = 1
        for count in (1, 10, 60):
            with self.subTest(count=count):
                for index in range(count):
                    world.bc.seed(company_b, TARGET_SET, {
                        **template, "id": f"0000b0c3-0000-4000-{count:04x}-{index:012x}",
                        "number": f"PO-N{count}-{index}"})
                seeded += count
                listed_a = [row["id"] for row in self.send(
                    world, "GET", f"/companies({company_a})/{TARGET_SET}")["body"]["value"]]
                listed_b = [row["id"] for row in self.send(
                    world, "GET", f"/companies({company_b})/{TARGET_SET}")["body"]["value"]]
                # No row lost, duplicated or crossing companies.
                self.assertEqual(sorted(listed_a), sorted(own))
                self.assertEqual(len(listed_b), seeded)
                self.assertEqual(len(set(listed_b)), seeded)
                self.assertFalse(set(listed_b) & set(listed_a))
                handles_b = world.bc.handles(company_b, TARGET_SET)
                for handle in handles_b:
                    with self.assertRaises(CitationError):
                        world.resolve(handle, reader_a)
                    with self.assertRaises(CitationError):   # its owner reads nothing
                        world.bc.owner.resolve(handle, reader_b)
                self.assertEqual(len([world.resolve(h, reader_b) for h in handles_b]), seeded)
        # By OData under company A, a company B record is not there: read, lines, PATCH.
        foreign = listed_b[0]
        before = self.send(world, "GET", f"/companies({company_b})/{TARGET_SET}({foreign})")
        for method, url, headers, body in (
                ("GET", f"/companies({company_a})/{TARGET_SET}({foreign})", {}, None),
                ("GET", f"/companies({company_a})/{TARGET_SET}({foreign})/purchaseOrderLines",
                 {}, None),
                ("PATCH", f"/companies({company_a})/{TARGET_SET}({foreign})",
                 {"If-Match": "*"}, {"purchaser": "company A"})):
            with self.subTest(method=method, url=url):
                self.assertEqual(self.send(world, method, url, headers, body)["status"], 404)
        self.assertEqual(self.send(world, "GET", f"/companies({company_b})/{TARGET_SET}"
                                                 f"({foreign})"), before)
        self.assertEqual(world.bc.writes, 0)
        # A company B document's lines are company B's only, and need both grants.
        line = world.bc.handles(company_b, "purchaseOrderLines")[0]
        with self.assertRaises(CitationError):
            world.resolve(line, reader_a)
        self.assertEqual(world.resolve(line, reader_b)["key"]["company_id"], company_b)
        # Each company's own record is its readers' only, as the live capture keeps it.
        record = world.bc.sets["companies"].relation
        for company, reader, other in ((company_a, reader_a, reader_b),
                                       (company_b, reader_b, reader_a)):
            handle = owner_handle(record, {"id": company})
            self.assertEqual(world.resolve(handle, reader)["row"]["id"], company)
            with self.assertRaises(CitationError):
                world.resolve(handle, other)
        # The action path: a company A reader cannot cite company B's order, so nothing is
        # proposed, registered or sent.
        target = world.bc.handles(company_b, TARGET_SET)[0]
        cited = world.resolve(target, reader_b)
        case_id = world.path.open_case(ACTOR, "Set the requested receipt date of BC purchase "
                                              "order PO-TWIN-B1 to 2026-11-09?",
                                       objective_author_kind="human", session=SESSION)
        with self.assertRaises(CitationUnresolvable):
            world.path.record_proposal(
                ACTOR, case_id, ap.FIXTURE_CAPABILITY,
                {"field": TARGET_FIELD, "value": "2026-11-09"},
                [{"check": "field_empty", "citation": target, "field": TARGET_FIELD}],
                citations=[target], session=SESSION,
                expected_effect={"relation": cited["relation"], "key": cited["key"],
                                 "fields": [TARGET_FIELD], "equals": "2026-11-09"})
        self.assertEqual((world.bc.owner.dispatch_calls, world.bc.owner.operations()), (0, []))


class EtagAndFailClosedTests(BcTwinTestCase):
    def test_a_stale_if_match_is_refused_and_nothing_undeclared_is_taken(self):
        world = self.world()
        target = targets(world)[0]
        cited = world.resolve(target, ACTOR)
        url = odata_path(cited)
        held = self.send(world, "GET", url)["body"]
        # Money stays exact: Decimal on OData, its decimal text in the citation.
        self.assertIsInstance(held["totalAmountIncludingTax"], Decimal)
        self.assertEqual(str(held["totalAmountIncludingTax"]),
                         cited["row"]["totalAmountIncludingTax"])
        etag = held["@odata.etag"]
        other = "0000b0c1-0000-4000-8000-0000000000ff"
        for headers, body, status in (
                # 428 and 412 serve the Workbench owner's own stricter path only (its door
                # sends the cited etag); upstream always sends ANY_ETAG.
                ({}, {"purchaser": "x"}, 428),
                ({"If-Match": 'W/"JzE7Jw=="'}, {"purchaser": "x"}, 412),
                ({"If-Match": etag}, {"noSuchProperty": "x"}, 400),
                ({"If-Match": etag}, {"id": other}, 400),
                ({"If-Match": etag}, {TARGET_FIELD: "11/02/2026"}, 400),
                ({"If-Match": etag}, {TARGET_FIELD: "2026-13-45"}, 400),
                ({"If-Match": etag}, {"discountAmount": "a lot"}, 400),
                # Computed by BC (BC_CALCULATED_FIELDS, lastModifiedDateTime), however well
                # typed: the live pin declares no Core.Computed.
                ({"If-Match": etag}, {"totalAmountIncludingTax": "130.25"}, 400),
                ({"If-Match": etag}, {"status": "Released"}, 400),
                ({"If-Match": etag}, {"lastModifiedDateTime": "2026-11-01T00:00:00Z"}, 400),
                ({"If-Match": etag}, {}, 400)):
            with self.subTest(headers=headers, body=body):
                self.assertEqual(self.send(world, "PATCH", url, headers, body)["status"], status)
        self.assertEqual(self.send(world, "GET", url)["body"], held)   # nothing applied
        saved = self.send(world, "PATCH", url, {"If-Match": etag}, {"purchaser": "x"})
        self.assertEqual((saved["status"], saved["body"]["purchaser"]), (200, "x"))
        self.assertNotEqual(saved["body"]["@odata.etag"], etag)
        # The etag read before that save is stale now: refused, the save stands.
        stale = self.send(world, "PATCH", url, {"If-Match": etag}, {"purchaser": "y"})
        self.assertEqual((stale["status"], stale["body"]["error"]["code"]),
                         (412, "Request_EntityChanged"))
        self.assertEqual(self.send(world, "GET", url)["body"], saved["body"])
        self.assertEqual(self.send(world, "PATCH", url, {"if-match": "*"},
                                   {"purchaser": "y"})["status"], 200)
        # Routes: a query option, another method, an unknown set or company fail closed.
        company = cited["key"]["company_id"]
        for method, path, status in (("GET", url + "?$filter=number eq 'x'", 400),
                                     ("DELETE", url, 405),
                                     ("GET", f"/companies({company})/employees", 404),
                                     ("GET", f"/companies({other})/{TARGET_SET}", 404)):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.send(world, method, path)["status"], status)
        # Seeding: only what the pin declares, in its own company, of its Edm type.
        row = {name: value for name, value in cited["row"].items() if name != VERSION_FIELD}
        for company_id, entity_set, record, code in (
                (company, TARGET_SET, {**row, "id": other, "noSuch": 1},
                 "property_not_in_metadata"),
                (company, TARGET_SET, {**row, "id": "not-a-guid"}, "property_value_invalid"),
                (company, TARGET_SET, {**row, "id": other, "orderDate": "Sept 1"},
                 "property_value_invalid"),
                (None, TARGET_SET, {**row, "id": other}, "company_scope_mismatch"),
                (company, "employees", {"id": other}, "entity_set_not_served"),
                (company, "purchaseOrderLines", {"id": other, "documentId": other},
                 "line_without_its_document")):
            with self.subTest(code=code), self.assertRaises(TwinError) as caught:
                world.bc.seed(company_id, entity_set, record)
            self.assertTrue(caught.exception.code.startswith(code), caught.exception.code)
        # The action path takes a BC twin write only to a declared, typed, writable field.
        for field, value, code in (("purchaser", "Someone", "effect_field_role_undeclared"),
                                   (VERSION_FIELD, 5, "effect_field_read_only"),
                                   ("id", other, "effect_field_read_only"),
                                   ("status", "Released", "effect_field_read_only"),
                                   (TARGET_FIELD, "2026-13-45", "effect_value_type_mismatch"),
                                   ("noSuch", "x", "effect_field_not_declared")):
            with self.subTest(field=field):
                target = targets(world)[0]
                cited = world.resolve(target, ACTOR)
                case_id = world.path.open_case(
                    ACTOR, f"Set {field} of BC purchase order {cited['row']['number']} to "
                           f"{value}?", objective_author_kind="human", session=SESSION)
                proposal = world.path.record_proposal(
                    ACTOR, case_id, ap.FIXTURE_CAPABILITY, {"field": field, "value": value},
                    [{"check": "field_equals", "citation": target, "field": VERSION_FIELD,
                      "value": cited["row"][VERSION_FIELD]}], citations=[target],
                    session=SESSION,
                    expected_effect={"relation": cited["relation"], "key": cited["key"],
                                     "fields": [field], "equals": value})
                with self.assertRaises(ap.ActionPathError) as refused:
                    world.path.register(ACTOR, case_id, proposal["proposal_id"],
                                        session=SESSION)
                self.assertTrue(str(refused.exception.code).startswith(code),
                                refused.exception.code)
        self.assertEqual(world.bc.owner.operations(), [])
        # The owner's door sends no PATCH without the cited version as its If-Match.
        owner, sent = world.bc.owner, len(world.bc.owner.door.answers)
        for number, expect in enumerate(([], [{"check": "field_equals", "field": VERSION_FIELD,
                                               "value": "1"}])):
            with self.subTest(expect=expect), self.assertRaises(OwnerRejected) as refused:
                owner.dispatch({"schema": DISPATCH_SCHEMA, "capability": ap.FIXTURE_CAPABILITY,
                                "relation": cited["relation"], "key": cited["key"],
                                "set": {TARGET_FIELD: "2026-11-20"}, "expect": expect},
                               f"wb-{number:040x}")
            self.assertEqual(refused.exception.code, "odata_if_match_needs_the_cited_version")
        self.assertEqual(owner.door.answers[sent:], [])
        self.assertEqual([op["status"] for op in owner.operations()], ["rejected", "rejected"])

    def test_if_match_any_as_upstream_sends_it_is_applied_over_any_save(self):
        """Every upstream BC PATCH sends ``If-Match: *`` (plx-customer-portal@549f6f0e
        outbound.ts:184-204; outbox-drain.ts:807,892,918,1054,1485), of a document and of
        one of its lines: BC applies it whatever the version, so another user's save is
        overwritten. The twin does the same; it proves no write safety."""
        world = self.world()
        company_a, company_b = world.bc.companies()
        line = world.resolve(world.bc.handles(company_a, "purchaseOrderLines")[0], ACTOR)["row"]
        order = f"/companies({company_a})/{TARGET_SET}({line['documentId']})"
        line_url = f"{order}/purchaseOrderLines({line['id']})"
        for url, field, theirs, ours in ((order, "purchaser", "another BC user", "Portal outbox"),
                                         (line_url, "quantity", 11, 13)):
            with self.subTest(url=url):
                read = self.send(world, "GET", url)["body"]
                saved = self.send(world, "PATCH", url, {"If-Match": read["@odata.etag"]},
                                  {field: theirs})
                self.assertEqual((saved["status"], saved["body"][field]), (200, theirs))
                outbox = self.send(world, "PATCH", url, {"If-Match": ANY_ETAG}, {field: ours})
                self.assertEqual((outbox["status"], outbox["body"][field]), (200, ours))
                self.assertNotEqual(outbox["body"]["@odata.etag"], saved["body"]["@odata.etag"])
                self.assertEqual(self.send(world, "GET", url)["body"], outbox["body"])
                # The etag read before the other user's save is refused by the Workbench
                # owner's stricter path only.
                self.assertEqual(self.send(world, "PATCH", url, {"If-Match": read["@odata.etag"]},
                                           {field: theirs})["status"], 412)
        self.assertEqual(world.bc.writes, 4)
        # A line only under its own document and company; its document field is not
        # writable; nothing applied.
        foreign = world.resolve(world.bc.handles(company_b, "purchaseOrderLines")[0],
                                Actor(ACTOR.tenant, frozenset({"ops-b"})))["row"]
        other_id = next(row["id"] for row in self.send(
            world, "GET", f"/companies({company_a})/{TARGET_SET}")["body"]["value"]
            if row["id"] != line["documentId"])
        held = self.send(world, "GET", line_url)["body"]
        for url, body, status in (
                (f"/companies({company_a})/{TARGET_SET}({other_id})/purchaseOrderLines"
                 f"({line['id']})", {"quantity": 1}, 404),
                (f"{order}/purchaseOrderLines({foreign['id']})", {"quantity": 1}, 404),
                (f"/companies({company_a})/{TARGET_SET}({foreign['documentId']})"
                 f"/purchaseOrderLines({foreign['id']})", {"quantity": 1}, 404),
                (line_url, {"documentId": other_id}, 400),
                (line_url, {"id": other_id}, 400)):
            with self.subTest(url=url, body=body):
                self.assertEqual(self.send(world, "PATCH", url, {"If-Match": ANY_ETAG},
                                           body)["status"], status)
                if status == 404:
                    self.assertEqual(self.send(world, "GET", url)["status"], 404)
        self.assertEqual(self.send(world, "GET", line_url)["body"], held)
        self.assertEqual(world.bc.writes, 4)

    def test_if_match_list_and_line_delete(self):
        """If-Match is an RFC 7232 list; a line DELETE (outbox-drain.ts:828) honours it."""
        world = self.world()
        company_a, _ = world.bc.companies()
        line = world.resolve(world.bc.handles(company_a, "purchaseOrderLines")[0], ACTOR)["row"]
        url = (f"/companies({company_a})/{TARGET_SET}({line['documentId']})"
               f"/purchaseOrderLines({line['id']})")
        current = self.send(world, "GET", url)["body"]["@odata.etag"]
        listed = self.send(world, "PATCH", url, {"If-Match": f'W/"nope", {current}'},
                           {"quantity": 21})
        self.assertEqual((listed["status"], listed["body"]["quantity"]), (200, 21))
        self.assertEqual(self.send(world, "PATCH", url, {"If-Match": f'W/"a", W/"b"'},
                                   {"quantity": 1})["status"], 412)
        self.assertEqual(self.send(world, "DELETE", url, {"If-Match": current})["status"], 412)
        self.assertEqual(self.send(world, "DELETE", url)["status"], 428)
        self.assertEqual(self.send(world, "GET", url)["body"]["quantity"], 21)
        gone = self.send(world, "DELETE", url, {"If-Match": ANY_ETAG})
        self.assertEqual(gone["status"], 204)
        self.assertEqual(self.send(world, "GET", url)["status"], 404)
        self.assertEqual(self.send(world, "DELETE", url, {"If-Match": ANY_ETAG})["status"], 404)
        self.assertNotIn(line["id"], [r["id"] for r in self.send(
            world, "GET", url.rsplit("(", 1)[0])["body"]["value"]])

    def test_a_door_answer_never_heard_stays_pending_and_is_never_sent_again(self):
        world = self.world()
        owner, door = world.bc.owner, world.bc.owner.door
        cited = world.resolve(targets(world)[0], ACTOR)

        def answer_lost(payload):   # BC applied the PATCH; its answer never arrived
            door(payload)
            raise TimeoutError("answer lost")

        owner.door = answer_lost
        payload = {"schema": DISPATCH_SCHEMA, "capability": ap.FIXTURE_CAPABILITY,
                   "relation": cited["relation"], "key": cited["key"],
                   "set": {TARGET_FIELD: "2026-11-21"},
                   "expect": [{"check": "field_equals", "field": VERSION_FIELD,
                               "value": cited["row"][VERSION_FIELD]}]}
        with self.assertRaises(TimeoutError):
            owner.dispatch(payload, "wb-" + "a" * 40)
        sent = len(door.answers)
        with self.assertRaises(RuntimeError):   # outcome unknown: the replay sends nothing
            owner.dispatch(payload, "wb-" + "a" * 40)
        self.assertEqual(len(door.answers), sent)
        seen = owner.read_back({"idempotency_key": "wb-" + "a" * 40,
                                "relation": cited["relation"], "key": cited["key"]})
        self.assertEqual((seen["operation"], seen["fenced"], seen["record"][TARGET_FIELD]),
                         ("pending", False, "2026-11-21"))


if __name__ == "__main__":
    unittest.main()
