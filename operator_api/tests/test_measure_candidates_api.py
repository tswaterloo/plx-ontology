"""API tests for candidate business measure definitions: capture, masking, owner-only
review (403 for staff, non-owner admins, and view_as), live overlay promotion, and OpenAPI."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from integrations.people_exclusion import PeopleExclusion
from operating import measure_candidates as mc
from operating import measures as ms
from operating.employee_permissions import FULL_READ_SCOPES, EmployeePrincipal
from operating.session_identity import (
    AUTHOR_HUMAN,
    CHANNEL_CONSOLE,
    PROOF_CONFIGURED,
    PROOF_VIEW_AS,
    SessionIdentity,
)
from operator_api import feedback as fb
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend
from operator_api.view_as import HEADER as VIEW_AS_HEADER, ViewAs, key_of

TID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
EXCLUDED_OID = "11111111-2222-4333-8444-555555555555"
VIEWED_OID = "22222222-2222-4222-8222-222222222222"
STAFF_OID = "33333333-3333-4333-8333-333333333333"
ADMIN_OID = "44444444-4444-4444-8444-444444444444"
OWNER_OID = "55555555-5555-4555-8555-555555555555"


def _make_session(
    email: str,
    oid: str,
    *,
    owner: bool = False,
    view_as: bool = False,
    exp: int | None = None,
) -> SessionIdentity:
    emp = EmployeePrincipal(TID, oid)
    access = ("full_read:" + "+".join(sorted(FULL_READ_SCOPES)) + ":mail_all") if owner else None
    return SessionIdentity(
        tenant="plx",
        channel=CHANNEL_CONSOLE,
        author_kind=AUTHOR_HUMAN,
        proof=PROOF_VIEW_AS if view_as else PROOF_CONFIGURED,
        employee=emp,
        principals=frozenset({emp.principal}),
        email=email,
        subject_oid=oid,
        identities=frozenset({email.lower(), oid.lower()}),
        access=access,
        expires_at=(exp if exp is not None else int(time.time()) + 600) if view_as else None,
    )


def _guard() -> PeopleExclusion:
    g = PeopleExclusion([EXCLUDED_OID])
    g.attach_directory([
        (EXCLUDED_OID, "secretperson@plx.example", []),
        (OWNER_OID, "owner@plx.example", []),
        (ADMIN_OID, "admin@plx.example", []),
        (STAFF_OID, "staff@plx.example", []),
        (VIEWED_OID, "other@plx.example", []),
    ])
    return g


class MeasureCandidatesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._prev_env = os.environ.get(mc.ENV)
        os.environ[mc.ENV] = self._tmp.name
        self.addCleanup(self._restore_env)

        self.backend = MockBackend()
        self.backend.candidate_store = mc.CandidateStore(Path(self._tmp.name))

        self.owner_session = _make_session("owner@plx.example", OWNER_OID, owner=True)
        self.admin_session = _make_session("admin@plx.example", ADMIN_OID, owner=False)
        self.staff_session = _make_session("staff@plx.example", STAFF_OID, owner=False)
        self.other_session = _make_session("other@plx.example", VIEWED_OID, owner=False)

        self.owner_key = next(iter(self.owner_session.principals))
        self.admin_key = next(iter(self.admin_session.principals))
        self.staff_key = next(iter(self.staff_session.principals))
        self.other_key = next(iter(self.other_session.principals))

        self.backend.held_markings = {
            self.owner_key: frozenset({"finance", "pii", "purchasing_prices"}),
            self.admin_key: frozenset(),
            self.staff_key: frozenset({"finance"}),
            self.other_key: frozenset(),
        }

        self.view_as_key = key_of(VIEWED_OID)
        self.backend.view_as = ViewAs(
            candidates=lambda: [(VIEWED_OID, "other@plx.example")],
            identity=lambda _o, exp: _make_session("other@plx.example", VIEWED_OID, view_as=True, exp=exp),
            excluded=lambda: (lambda _o: False),
        )

        self.guard = _guard()
        self._p_excl = patch.object(fb, "exclusion", return_value=self.guard)
        self._p_excl.start()
        self.addCleanup(self._p_excl.stop)

        self._prev_admins = os.environ.get("PLX_FEEDBACK_ADMINS")
        os.environ["PLX_FEEDBACK_ADMINS"] = "admin@plx.example"
        self.addCleanup(self._restore_admins)

    def _restore_env(self) -> None:
        if self._prev_env is None:
            os.environ.pop(mc.ENV, None)
        else:
            os.environ[mc.ENV] = self._prev_env
        mc.clear_memory_overlay()

    def _restore_admins(self) -> None:
        if self._prev_admins is None:
            os.environ.pop("PLX_FEEDBACK_ADMINS", None)
        else:
            os.environ["PLX_FEEDBACK_ADMINS"] = self._prev_admins

    def _client_for(self, session: SessionIdentity) -> TestClient:
        app = create_app(
            self.backend,
            DevAuth(lambda: session, mode="mock"),
            secure_cookie=False,
        )
        client = TestClient(app, base_url="http://testserver")
        res = client.post("/api/auth/dev-login", json={})
        self.assertEqual(res.status_code, 200, res.text)
        return client

    def test_propose_list_masking_and_owner_only_approve_dismiss(self) -> None:
        staff = self._client_for(self.staff_session)
        other = self._client_for(self.other_session)
        admin = self._client_for(self.admin_session)
        owner = self._client_for(self.owner_session)

        # Staff proposes a finance-marked candidate.
        res = staff.post(
            "/api/measures/candidates",
            json={
                "sql": "SELECT customer, sum(revenue) FROM sales_invoice_lines WHERE line_type = 'item' GROUP BY customer",
                "title": "Item revenue by customer",
                "question": "What is item revenue per customer?",
                "source_kind": "saved_tile",
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        cand = res.json()
        cid = cand["id"]
        self.assertEqual(cand["status"], "pending")
        self.assertEqual(cand["usage_count"], 1)

        # Submitting same SQL again deduplicates and increments usage_count.
        dup = staff.post(
            "/api/measures/candidates",
            json={"sql": "select customer, SUM(revenue) from sales_invoice_lines where line_type = 'item' group by customer"},
        )
        self.assertEqual(dup.status_code, 200)
        self.assertEqual(dup.json()["id"], cid)
        self.assertEqual(dup.json()["usage_count"], 2)

        # Another staff user cannot see staff's pending proposal.
        other_list = other.get("/api/measures/candidates")
        self.assertEqual(other_list.status_code, 200)
        self.assertFalse(other_list.json()["can_review"])
        self.assertEqual(other_list.json()["candidates"], [])

        # Non-owner admin without finance marking sees the row, with all sensitive fields withheld.
        admin_list = admin.get("/api/measures/candidates")
        self.assertEqual(admin_list.status_code, 200)
        self.assertFalse(admin_list.json()["can_review"])
        self.assertEqual(len(admin_list.json()["candidates"]), 1)
        admin_row = admin_list.json()["candidates"][0]
        self.assertIsNone(admin_row["sql"])
        self.assertIsNone(admin_row["question"])
        self.assertIsNone(admin_row["tile_title"])
        self.assertIsNone(admin_row["source_ref"])
        self.assertEqual(admin_row["proposed_name"], "Withheld candidate definition")
        self.assertEqual(admin_row["proposed_id"], "withheld")
        self.assertIsNone(admin_row["proposed_description"])
        self.assertEqual(admin_row["proposed_where"], "")
        self.assertEqual(admin_row["proposed_value"], "")
        self.assertEqual(admin_row["proposed_per"], "")
        self.assertEqual(admin_row["proposed_date"], "")
        self.assertEqual(admin_row["proposed_view"], "")
        self.assertEqual(admin_row["proposed_dimensions"], [])
        self.assertEqual(admin_row["views"], [])
        self.assertIn("finance", admin_row["withheld"])

        # Owner sees full SQL and can_review=True.
        owner_list = owner.get("/api/measures/candidates")
        self.assertEqual(owner_list.status_code, 200)
        self.assertTrue(owner_list.json()["can_review"])
        self.assertEqual(owner_list.json()["counts"]["pending"], 1)
        self.assertIsNotNone(owner_list.json()["candidates"][0]["sql"])

        # Non-owner (staff and non-owner admin) are refused approve & dismiss with 403.
        for client in (staff, admin):
            self.assertEqual(client.post(f"/api/measures/candidates/{cid}/approve", json={}).status_code, 403)
            self.assertEqual(client.post(f"/api/measures/candidates/{cid}/dismiss", json={}).status_code, 403)

        # Owner viewing as someone else is refused GET, propose, approve, and dismiss with 403 view_as_refused.
        va_headers = {VIEW_AS_HEADER: self.view_as_key}
        for resp in (
            owner.get("/api/measures/candidates", headers=va_headers),
            owner.post("/api/measures/candidates", json={"sql": "SELECT count(*) FROM sales_orders"}, headers=va_headers),
            owner.post(f"/api/measures/candidates/{cid}/approve", json={}, headers=va_headers),
            owner.post(f"/api/measures/candidates/{cid}/dismiss", json={}, headers=va_headers),
        ):
            self.assertEqual(resp.status_code, 403, resp.text)
            self.assertEqual(resp.json()["error"]["code"], "view_as_refused")

        # A forged/direct view_as session cookie is refused before reaching any route (401).
        va_session = _make_session("owner@plx.example", OWNER_OID, owner=True, view_as=True)
        va_client = self._client_for(va_session)
        self.assertEqual(va_client.post(f"/api/measures/candidates/{cid}/approve", json={}).status_code, 401)
        self.assertEqual(va_client.post(f"/api/measures/candidates/{cid}/dismiss", json={}).status_code, 401)

        # Owner approves with edited name, description, owner -> immediately visible in operating.measures.measures().
        app_res = owner.post(
            f"/api/measures/candidates/{cid}/approve",
            json={
                "id": "api_item_revenue",
                "name": "Item revenue (governed)",
                "description": "Agreed item-line revenue across posted invoices.",
                "owner": "Finance",
            },
        )
        self.assertEqual(app_res.status_code, 200, app_res.text)
        body = app_res.json()
        self.assertEqual(body["status"], "approved")
        self.assertEqual(body["approved_measure_id"], "api_item_revenue")
        self.assertIn("api_item_revenue", ms.measures())
        self.assertEqual(ms.measures()["api_item_revenue"]["name"], "Item revenue (governed)")

        # Owner can also dismiss another candidate.
        second = owner.post(
            "/api/measures/candidates",
            json={"sql": "SELECT status, count(*) FROM sales_orders GROUP BY status", "title": "Orders by status"},
        ).json()
        dis = owner.post(
            f"/api/measures/candidates/{second['id']}/dismiss",
            json={"reason": "One-off query"},
        )
        self.assertEqual(dis.status_code, 200)
        self.assertEqual(dis.json()["status"], "dismissed")
        self.assertEqual(dis.json()["dismiss_reason"], "One-off query")

    def test_thumbs_up_on_accepted_answer_auto_captures_adhoc_sql(self) -> None:
        owner = self._client_for(self.owner_session)
        staff = self._client_for(self.staff_session)
        aid = "ans-sql-1"
        with self.backend._lock:
            self.backend._answers[aid] = self.owner_key
            self.backend._kept[aid] = {
                "question": "Count open POs by supplier",
                "answer": "Here are the counts.",
                "facts": [],
                "subjects": [],
                "markings": [],
                "queries": [
                    {
                        "title": "Open POs by supplier",
                        "sql": "SELECT supplier, count(*) FROM purchase_orders WHERE status = 'open' GROUP BY supplier",
                        "measure_id": None,
                    },
                    {
                        "title": "Revenue",
                        "sql": "SELECT sum(revenue) FROM sales_invoice_lines",
                        "measure_id": "revenue",
                    },
                ],
            }
        res = owner.post("/api/feedback", json={"answer_id": aid, "rating": 1})
        self.assertEqual(res.status_code, 200, res.text)

        listed = owner.get("/api/measures/candidates").json()
        self.assertEqual(listed["total"], 1)
        row = listed["candidates"][0]
        self.assertEqual(row["source_kind"], "accepted_answer")
        self.assertEqual(row["source_ref"], aid)
        self.assertIn("purchase_orders", row["views"])
        self.assertEqual(row["question"], "Count open POs by supplier")

        # Proposing directly from answer_id works and increments usage_count.
        prop = owner.post("/api/measures/candidates", json={"answer_id": aid})
        self.assertEqual(prop.status_code, 200)
        self.assertEqual(prop.json()["id"], row["id"])
        self.assertEqual(prop.json()["usage_count"], 2)

        # Another reader cannot propose from owner's answer_id (404).
        self.assertEqual(staff.post("/api/measures/candidates", json={"answer_id": aid}).status_code, 404)

    def test_list_has_no_silent_cap_past_fifty_candidates(self) -> None:
        owner = self._client_for(self.owner_session)
        for i in range(60):
            self.backend.candidate_store.capture(
                sql=f"SELECT count(*) AS c{i} FROM sales_orders WHERE status = 's{i}'",
                title=f"Candidate {i}",
                proposer_key=self.owner_key,
                guard=self.guard,
            )
        listed = owner.get("/api/measures/candidates").json()
        self.assertEqual(listed["total"], 60)
        self.assertEqual(len(listed["candidates"]), 60)
        self.assertEqual(listed["counts"]["pending"], 60)

    def test_personal_content_in_name_description_and_where_redacted_for_non_owner_admin(self) -> None:
        admin = self._client_for(self.admin_session)
        owner = self._client_for(self.owner_session)
        self.backend.candidate_store.capture(
            sql="SELECT count(*) FROM sales_orders WHERE status = 'Pay stub gross pay net pay CPP EI'",
            name="Pay stub gross pay net pay CPP EI count",
            description="Pay stub gross pay net pay CPP EI details",
            proposer_key=self.owner_key,
            markings=[],
            guard=self.guard,
        )
        admin_rows = admin.get("/api/measures/candidates").json()["candidates"]
        self.assertEqual(len(admin_rows), 1)
        row = admin_rows[0]
        self.assertEqual(row["proposed_name"], "Withheld candidate definition")
        self.assertEqual(row["proposed_id"], "withheld")
        self.assertIsNone(row["proposed_description"])
        self.assertEqual(row["proposed_where"], "")
        self.assertIsNone(row["sql"])
        self.assertEqual(row["withheld"], ["finance", "pii"])

        owner_rows = owner.get("/api/measures/candidates").json()["candidates"]
        self.assertIn("Pay stub gross pay net pay CPP EI", owner_rows[0]["proposed_name"])
        self.assertIn("Pay stub gross pay net pay CPP EI", owner_rows[0]["proposed_where"])

    def test_approve_rejects_malicious_and_excluded_where_and_second_proposer_cannot_spoof(self) -> None:
        staff = self._client_for(self.staff_session)
        other = self._client_for(self.other_session)
        owner = self._client_for(self.owner_session)

        created = staff.post(
            "/api/measures/candidates",
            json={
                "sql": "SELECT customer, sum(revenue) FROM sales_invoice_lines WHERE line_type = 'item' GROUP BY customer",
                "name": "Staff item revenue",
                "description": "Original staff description",
                "owner": "Finance",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        cid = created.json()["id"]

        # Second proposer submitting same SQL does not overwrite A's metadata or 500.
        spoof = other.post(
            "/api/measures/candidates",
            json={
                "sql": "SELECT customer, sum(revenue) FROM sales_invoice_lines WHERE line_type = 'item' GROUP BY customer",
                "name": "Spoofed name",
                "description": "Spoofed desc",
                "owner": "Spoofed owner",
            },
        )
        self.assertEqual(spoof.status_code, 200, spoof.text)
        self.assertEqual(spoof.json()["proposed_name"], "Existing candidate definition")
        self.assertIsNone(spoof.json()["proposed_description"])

        owner_row = owner.get("/api/measures/candidates").json()["candidates"][0]
        self.assertEqual(owner_row["proposed_name"], "Staff item revenue")
        self.assertEqual(owner_row["proposed_description"], "Original staff description")
        self.assertEqual(owner_row["proposed_owner"], "Finance")
        self.assertEqual(owner_row["usage_count"], 2)

        # Malicious or excluded where on approve returns 422 and never persists.
        for bad_where in (
            "customer = 'secretperson@plx.example'",
            "EXISTS (SELECT 1 FROM biz_person.emails)",
            "line_type = 'item' UNION ALL SELECT 1",
        ):
            bad = owner.post(f"/api/measures/candidates/{cid}/approve", json={"where": bad_where})
            self.assertEqual(bad.status_code, 422, bad.text)
        self.assertEqual(self.backend.candidate_store.get(cid)["status"], "pending")
        self.assertEqual(mc.governed_overlay(), [])


if __name__ == "__main__":
    unittest.main()
