"""Emailed brief or answer with its chart (Genie item 11, email only): drafted for the
signed-in person, previewed and confirmed through ActionDesk, live send stays gated by
PLX_ACTIONS_LIVE + PLX_ACTIONS_SENDERS (Vince-only on host), and Teams delivery is refused."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from integrations.people_exclusion import PeopleExclusion
from operator_api import charts as ch
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend

VINCE_OID = "11111111-2222-4333-8444-555555555551"
ANA_OID = "11111111-2222-4333-8444-555555555552"
EXCLUDED_OID = "11111111-2222-4333-8444-555555555599"


class _FakeSender:
    def __init__(self, env):
        self.env = env
        self.sent = []

    def gate(self, kind, sender):
        from integrations.graph_send import configured_senders
        if self.env.get("PLX_ACTIONS_LIVE") == "1" and sender and sender.lower() in configured_senders(self.env):
            return [], True
        return ["sender not in PLX_ACTIONS_SENDERS"], False

    def dispatch(self, kind, sender, payload):
        from integrations.graph_send import configured_senders, request_for, shown
        atts = list(payload.get("attachments") or ())
        req = shown(request_for(kind, sender, payload))
        if self.env.get("PLX_ACTIONS_LIVE") == "1" and sender.lower() in configured_senders(self.env):
            self.sent.append((kind, sender, payload, atts))
            return {"status": "sent", "request": req, "missing": []}
        return {"status": "dry_run", "request": req, "missing": ["sender not in PLX_ACTIONS_SENDERS"]}


def _guard():
    g = PeopleExclusion([EXCLUDED_OID])
    g.attach_directory([
        (VINCE_OID, "vince@plx.example", []),
        (ANA_OID, "ana@plx.example", []),
        (EXCLUDED_OID, "excluded@plx.example", []),
    ])
    return g


def _sample_chart():
    return ch.build(
        {"type": "bar", "x": "supplier", "y": ["spend"]},
        ["supplier", "spend"],
        [["Acme", "12500"], ["Beta", "8400"]],
        title="Open PO spend",
        as_of="2026-11-24T12:00:00Z",
        sql="SELECT supplier, sum(total) AS spend FROM purchase_orders GROUP BY supplier",
    )


class BriefEmailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {
            "PLX_ACTIONS_LIVE": "1",
            "PLX_ACTIONS_SENDERS": "vince@plx.example",
            "PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED_OID,
        }
        self.backend = MockBackend()
        self.backend.state_dir = Path(self.tmp.name)
        self.backend.actions_environ = self.env
        self.sender = _FakeSender(self.env)
        self.backend.action_sender = self.sender
        self.backend._instruct_guard = _guard

    def _client(self, principal: str, email: str, oid: str) -> TestClient:
        from dataclasses import replace
        base = fixture_identity("plx", [principal])()
        sess = replace(base, identities=frozenset({email, oid, *base.identities}),
                       subject_oid=oid)
        app = create_app(self.backend, DevAuth(lambda: sess, mode="mock"), secure_cookie=False)
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/api/auth/dev-login", json={}).status_code == 200
        return c

    def test_vince_drafts_previews_and_confirms_answer_email_with_chart_attachment_and_no_teams(self):
        vince = self._client("staff:vince", "vince@plx.example", VINCE_OID)
        ana = self._client("staff:ana", "ana@plx.example", ANA_OID)
        chart = _sample_chart()

        with mock.patch("operating.card_actions.exclusion_for", return_value=_guard()):
            res = vince.post("/api/email-draft", json={
                "title": "Open PO summary",
                "summary": "Acme leads with 12,500 CAD across open POs.",
                "charts": [chart],
                "format": "xlsx",
            })
            self.assertEqual(res.status_code, 200, res.text)
            draft = res.json()
            self.assertTrue(draft["card_id"].startswith("brief:"))
            self.assertEqual(draft["to"], "vince@plx.example")
            self.assertEqual(draft["attachment_name"], "Open PO summary.xlsx")
            self.assertTrue(draft["upload_id"])

            # Teams delivery is refused on brief: cards (email only)
            teams_try = vince.post("/api/actions/preview", json={
                "card_id": draft["card_id"], "kind": "teams_message",
            })
            self.assertEqual(teams_try.status_code, 422)

            # Another reader cannot preview Vince's draft
            self.assertEqual(ana.post("/api/actions/preview", json={
                "card_id": draft["card_id"], "kind": "send_email",
            }).status_code, 404)

            # Vince previews -> sees attachment & payload_hash
            prev = vince.post("/api/actions/preview", json={
                "card_id": draft["card_id"], "kind": "send_email",
            })
            self.assertEqual(prev.status_code, 200, prev.text)
            pbody = prev.json()
            self.assertEqual(pbody["problems"], [])
            atts = pbody["payload"]["attachments"]
            self.assertEqual(len(atts), 1)
            self.assertEqual(atts[0]["name"], "Open PO summary.xlsx")

            # Vince confirms -> sends live with Excel chart attachment
            conf = vince.post("/api/actions/confirm", json={
                "card_id": draft["card_id"], "kind": "send_email",
                "confirm": True, "idempotency_key": "brief-send-key-000001",
                "payload_hash": pbody["payload_hash"],
            })
            self.assertEqual(conf.status_code, 200, conf.text)
            self.assertEqual(conf.json()["status"], "sent")
            self.assertEqual(len(self.sender.sent), 1)
            self.assertEqual(len(self.sender.sent[0][3]), 1)
            self.assertEqual(self.sender.sent[0][3][0]["name"], "Open PO summary.xlsx")

            # Non-Vince sender stays dry_run even on confirm
            ana_draft = ana.post("/api/email-draft", json={
                "title": "Ana summary", "summary": "Two open items.",
            }).json()
            ana_prev = ana.post("/api/actions/preview", json={
                "card_id": ana_draft["card_id"], "kind": "send_email",
            }).json()
            ana_conf = ana.post("/api/actions/confirm", json={
                "card_id": ana_draft["card_id"], "kind": "send_email",
                "confirm": True, "idempotency_key": "brief-send-key-000002",
                "payload_hash": ana_prev["payload_hash"],
            })
            self.assertEqual(ana_conf.status_code, 200, ana_conf.text)
            self.assertEqual(ana_conf.json()["status"], "dry_run")
            self.assertEqual(len(self.sender.sent), 1)   # only Vince actually sent

    def test_caller_supplied_external_to_sets_proposed_and_requires_unknown_domains_confirm(self):
        vince = self._client("staff:vince", "vince@plx.example", VINCE_OID)
        with mock.patch("operating.card_actions.exclusion_for", return_value=_guard()):
            res = vince.post("/api/email-draft", json={
                "title": "External brief",
                "summary": "Summary for partner review.",
                "to": "partner@outside.example",
            })
            self.assertEqual(res.status_code, 200, res.text)
            card_id = res.json()["card_id"]

            reader = self.backend._brief_drafts().get("staff:vince", card_id)
            self.assertIsNotNone(reader)
            self.assertTrue(reader["to_proposed"])

            from operator_api.auth import Reader
            card = self.backend.brief_email_card(
                Reader(
                    session=fixture_identity("plx", ["staff:vince"])(),
                    mode="mock",
                    display_name="Vince",
                    email="vince@plx.example",
                ),
                card_id,
            )
            self.assertIsNotNone(card)
            self.assertEqual(card["draft"]["to"], {
                "address": "partner@outside.example",
                "source": None,
                "proposed": True,
            })

            prev = vince.post("/api/actions/preview", json={
                "card_id": card_id, "kind": "send_email",
            })
            self.assertEqual(prev.status_code, 200, prev.text)
            pbody = prev.json()
            self.assertEqual(pbody["unknown_domains"], ["outside.example"])

            blocked = vince.post("/api/actions/confirm", json={
                "card_id": card_id, "kind": "send_email",
                "confirm": True, "idempotency_key": "brief-ext-key-000001",
                "payload_hash": pbody["payload_hash"],
            })
            self.assertEqual(blocked.status_code, 409, blocked.text)
            self.assertEqual(len(self.sender.sent), 0)

            allowed = vince.post("/api/actions/confirm", json={
                "card_id": card_id, "kind": "send_email",
                "confirm": True, "confirm_unknown_domains": True,
                "idempotency_key": "brief-ext-key-000002",
                "payload_hash": pbody["payload_hash"],
            })
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertEqual(allowed.json()["status"], "sent")
            self.assertEqual(len(self.sender.sent), 1)

    def test_untrusted_chart_fields_and_title_citations_are_scrubbed_and_rechecked(self):
        import io
        import zipfile
        vince = self._client("staff:vince", "vince@plx.example", VINCE_OID)
        bob_oid = "11111111-2222-4333-8444-555555555577"

        def guard_without_bob():
            g = PeopleExclusion([EXCLUDED_OID])
            g.attach_directory([
                (VINCE_OID, "vince@plx.example", []),
                (ANA_OID, "ana@plx.example", []),
                (bob_oid, "bob@plx.example", []),
                (EXCLUDED_OID, "excluded@plx.example", []),
            ])
            return g

        def guard_with_bob():
            g = PeopleExclusion([EXCLUDED_OID, bob_oid])
            g.attach_directory([
                (VINCE_OID, "vince@plx.example", []),
                (ANA_OID, "ana@plx.example", []),
                (bob_oid, "bob@plx.example", []),
                (EXCLUDED_OID, "excluded@plx.example", []),
            ])
            return g

        chart = {
            **_sample_chart(),
            "sql": "SELECT supplier, sum(total) FROM pos WHERE owner='excluded@plx.example' AND approver='bob@plx.example'",
            "x_label": "Supplier (excluded@plx.example / owner-x@outside.example)",
            "y_label": "Spend by excluded@plx.example",
            "y2_label": "Count by excluded@plx.example",
            "open_period": "2026-11 excluded@plx.example",
            "first_period": "2026-10 excluded@plx.example",
            "currency": "CAD excluded@plx.example",
            "as_of": "2026-11-24T12:00:00Z excluded@plx.example",
            "measure": {
                "id": "m1",
                "name": "Open spend",
                "definition": "Total for excluded@plx.example",
                "owner": "excluded@plx.example",
                "period": "2026-11",
            },
        }
        self.backend._instruct_guard = guard_without_bob
        from operating import instruct_files as _files
        captured_charts = []
        real_make = _files.make

        def spy_make(kind, content, charts=(), tables=()):
            captured_charts.extend(charts)
            return real_make(kind, content, charts=charts, tables=tables)

        with mock.patch("operating.card_actions.exclusion_for", side_effect=lambda *_: guard_without_bob()), \
                mock.patch("operator_api.brief_email.files.make", side_effect=spy_make):
            res = vince.post("/api/email-draft", json={
                "title": "PO brief excluded@plx.example and title-bob@outside.example",
                "summary": "Clean summary with token sk_test_1234567890abcdef123456.",
                "charts": [chart],
                "citations": [{
                    "label": "excluded@plx.example source",
                    "sql": "SELECT * FROM t WHERE cc='cite-bob@outside.example' AND x='excluded@plx.example'",
                }],
                "format": "docx",
                "attach_file": True,
            })
            self.assertEqual(res.status_code, 200, res.text)
            draft = res.json()
            self.assertNotIn("excluded@plx.example", draft["subject"])
            self.assertNotIn("excluded@plx.example", draft["body"])
            self.assertNotIn("sk_test_", draft["body"])
            self.assertEqual(len(captured_charts), 1)
            scrubbed_chart = captured_charts[0]
            for field in ("sql", "x_label", "y_label", "y2_label",
                          "open_period", "first_period", "currency", "as_of"):
                self.assertNotIn("excluded@plx.example", scrubbed_chart[field])
            self.assertNotIn("excluded@plx.example", scrubbed_chart["measure"]["definition"])
            self.assertNotIn("excluded@plx.example", scrubbed_chart["measure"]["owner"])

            upload = self.backend._uploads().get("staff:vince", draft["upload_id"])
            self.assertIsNotNone(upload)
            for expected_addr in (
                "bob@plx.example",
                "owner-x@outside.example",
                "title-bob@outside.example",
                "cite-bob@outside.example",
            ):
                self.assertIn(expected_addr, upload["people"])
            self.assertNotIn("excluded@plx.example", upload["people"])

            data = self.backend._uploads().file("staff:vince", upload["file_id"])
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                all_xml = "".join(
                    zf.read(name).decode("utf-8", errors="ignore")
                    for name in zf.namelist()
                    if name.endswith(".xml")
                )
            self.assertNotIn("excluded@plx.example", all_xml)

        # Post-draft exclusion: adding bob to the exclusion list blocks preview & attachment
        self.backend._instruct_guard = guard_with_bob
        with mock.patch("operating.card_actions.exclusion_for", side_effect=lambda *_: guard_with_bob()):
            prev = vince.post("/api/actions/preview", json={
                "card_id": draft["card_id"], "kind": "send_email",
            })
            self.assertEqual(prev.status_code, 404)

    def test_malformed_chart_returns_422_not_500(self):
        vince = self._client("staff:vince", "vince@plx.example", VINCE_OID)
        bad_charts = [
            {"kind": "not_a_chart_kind", "labels": ["A"], "series": [{"name": "S", "values": [1]}]},
            {"kind": "bar", "labels": ["A", "B"], "series": [{"name": "S", "values": [1]}]},
            {"kind": "bar", "labels": ["A"], "series": ["not-a-mapping"]},
            {"kind": "bar", "labels": ["A"], "series": [{"name": "S"}]},
            {"kind": "bar", "labels": 123, "series": [{"name": "S", "values": [1]}]},
            {"kind": "bar", "labels": "AB", "series": [{"name": "S", "values": [1, 2]}]},
            {"kind": "bar", "labels": ["A"], "series": [{"name": "S", "values": [1]}], "trend": "bad"},
        ]
        for bad in bad_charts:
            res = vince.post("/api/email-draft", json={
                "title": "Bad chart test",
                "summary": "Has summary so files.make path is exercised.",
                "charts": [bad],
                "format": "xlsx",
            })
            self.assertEqual(res.status_code, 422, f"Expected 422 for {bad!r}, got {res.status_code}: {res.text}")

    def test_corrupt_drafts_json_fails_loudly_without_overwriting(self):
        from operator_api.brief_email import _DraftStore

        store = _DraftStore.for_state_dir(self.tmp.name)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_payload = "{corrupt json that must not be overwritten"
        store.path.write_text(corrupt_payload, encoding="utf-8")

        with self.assertRaises(ValueError):
            store.get("staff:vince", "brief:any")
        with self.assertRaises(ValueError):
            store.put("staff:vince", "brief:new", {"subject": "New", "body": "Body"})
        self.assertEqual(store.path.read_text(encoding="utf-8"), corrupt_payload)


if __name__ == "__main__":
    unittest.main()
