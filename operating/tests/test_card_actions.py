"""Card actions at their boundary: the exact Graph request a confirmed send makes (a fake
Graph), the live gate (flag, sender mailbox, token permission), idempotency, people
exclusion, extra confirm for unknown domains, cited figures, and the append-only journal.
No network: every send goes to an injected fake. Evidence: ``injected-transport``."""

from __future__ import annotations

import base64
from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from integrations import live_source_guard as guard
from integrations.graph_send import ActionSender
from integrations.people_exclusion import PeopleExclusion
from operating import card_actions as ca

VINCE = "vince@plx.example"
VINCE_OID = "11111111-1111-4111-8111-111111111111"
EXCLUDED_OID = "22222222-2222-4222-8222-222222222222"
COLLEAGUE_OID = "33333333-3333-4333-8333-333333333333"
MAIL = f"graph-mail://{VINCE_OID}/AAMkAD-msg_1=@ck1"
BC = "bc://demo/purchaseOrders/90388"
LIVE = {"PLX_ACTIONS_LIVE": "1", "PLX_ACTIONS_SENDERS": VINCE}


def token(**claims) -> str:
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{part}.s"


def card(mail: str = MAIL) -> dict:
    src = lambda h: {"system": "BC", "handle": h, "label": "x", "open_url": None}  # noqa: E731
    return {"id": "pe-90388", "kind": "vendor_chase", "headline": "PO 90388 is 5 days late",
            "subject": {"object_id": "PurchaseOrder:demo:90388", "label": "PO 90388"},
            "action": {"text": "Ask the vendor to confirm the delivery date."},
            "draft": {"channel": "email", "audience": "vendor",
                      "to": {"name": "Demo Aromatics", "address": "orders@supplier.example"},
                      "subject": "PO 90388: please confirm the delivery date",
                      "body": "Hello,\n\nOur purchase order PO 90388 was needed by 2026-10-24."
                              "\n\nOpen items:\n- Lavender oil: ordered 200 kg, received 0 kg"
                              "\n\nFollowing up on your message of 2026-10-20.",
                      "facts": [{"fact": "order_number", "value": "PO 90388", "source": src(BC)},
                                {"fact": "need_date", "value": "2026-10-24", "source": src(BC)},
                                {"fact": "ordered_quantity", "value": "200 kg",
                                 "source": src(BC + "/lines/1")},
                                {"fact": "received_quantity", "value": "0 kg",
                                 "source": src(BC + "/lines/1")},
                                {"fact": "last_vendor_message", "value": "2026-10-20",
                                 "source": src(mail)}]}}


def exclusion() -> PeopleExclusion:
    found = PeopleExclusion([EXCLUDED_OID])
    found.attach_directory([(VINCE_OID, VINCE, []),
                            (EXCLUDED_OID, "hidden@plx.example", []),
                            (COLLEAGUE_OID, "ana@plx.example", [])])
    return found


class FakeGraph:
    def __init__(self, status: int = 202) -> None:
        self.status, self.calls = status, []

    def __call__(self, url, bearer, body):
        # the one real check every send passes before I/O, then recorded instead of sent
        guard.check_action_request("POST", url, body, environ=LIVE)
        self.calls.append((url, bearer.reveal(), json.loads(body)))
        return self.status, {}, b""


def desk(env=None, *, roles=("Mail.Send",), scp="", graph=None, journal=None):
    graph = graph if graph is not None else FakeGraph()
    sender = ActionSender(LIVE if env is None else env,
                          bearer=lambda: token(roles=list(roles), scp=scp), post=graph)
    clock = lambda: datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)  # noqa: E731
    return ca.ActionDesk(journal or ca.Journal(None), sender, clock=clock), graph


def ctx(**over) -> ca.Context:
    base = dict(actor_name="Vince", sender=VINCE, sender_oid=VINCE_OID,
                exclusion=exclusion(), today=date(2026, 11, 4), environ=LIVE)
    return ca.Context(**{**base, **over})


def confirm(d, kind="send_email", edits=None, c=None, key="k" * 20, **kw):
    c = c or ctx()
    shown = d.preview(card(), kind, edits, c)["payload_hash"]
    return d.confirm(card(), kind, edits, c, confirmed=True, idempotency_key=key,
                     shown_hash=kw.pop("shown_hash", shown), **kw)


class SendShape(unittest.TestCase):
    def test_a_confirmed_email_goes_from_the_senders_mailbox_in_graphs_exact_shape(self):
        d, graph = desk()
        result = confirm(d, edits={"cc": ["ana@plx.example"]})
        self.assertEqual(result["status"], "sent")
        url, bearer, body = graph.calls[0]
        self.assertEqual(url, "https://graph.microsoft.com/v1.0/users/vince%40plx.example/sendMail")
        self.assertEqual(body, {"message": {
            "subject": "PO 90388: please confirm the delivery date",
            "body": {"contentType": "Text", "content": card()["draft"]["body"]},
            "toRecipients": [{"emailAddress": {"address": "orders@supplier.example"}}],
            "ccRecipients": [{"emailAddress": {"address": "ana@plx.example"}}]},
            "saveToSentItems": True})
        row = d.journal.rows()[-1]
        self.assertEqual((row["actor"], row["card_id"], row["status"], row["at"]),
                         ({"name": "Vince", "email": VINCE}, "pe-90388", "sent",
                          "2026-11-04T15:00:00+00:00"))
        self.assertEqual(len(row["payload_hash"]), 64)
        self.assertIn({"fact": "need_date", "value": "2026-10-24", "handle": BC},
                      row["citations"])
        self.assertNotIn("Lavender", json.dumps(d.journal.rows()))   # no body in the journal

    def test_a_reply_keeps_the_thread_and_needs_the_message_in_the_senders_mailbox(self):
        d, graph = desk()
        self.assertEqual(confirm(d, "reply_in_thread")["status"], "sent")
        url, _, body = graph.calls[0]
        self.assertEqual(url, "https://graph.microsoft.com/v1.0/users/vince%40plx.example/"
                              "messages/AAMkAD-msg_1=/reply")
        self.assertEqual(set(body), {"message", "comment"})
        other = ctx(sender_oid=COLLEAGUE_OID, sender="ana@plx.example")
        found = d.preview(card(), "reply_in_thread", None, other)
        self.assertIn("not in your mailbox", " ".join(found["problems"]))


class Gate(unittest.TestCase):
    def test_without_flag_sender_or_scope_the_confirm_is_a_dry_run_naming_what_is_missing(self):
        cases = [({}, ("Mail.Send",), "PLX_ACTIONS_LIVE=1 not set"),
                 ({"PLX_ACTIONS_LIVE": "1"}, ("Mail.Send",), "PLX_ACTIONS_SENDERS"),
                 (LIVE, ("Mail.Read",), "Mail.Send not granted")]
        for n, (env, roles, missing) in enumerate(cases):
            d, graph = desk(env, roles=roles)
            result = confirm(d, key=f"key-{n}".ljust(20, "x"))
            self.assertEqual(result["status"], "dry_run")
            self.assertIn(missing, " ".join(result["missing"]))
            self.assertEqual(result["request"]["url"].rsplit("/", 1)[1], "sendMail")
            self.assertEqual(graph.calls, [])
        with self.assertRaises(guard.ReadOnlyGuardError):   # the guard itself, flag off
            guard.check_action_request("POST", result["request"]["url"],
                                       json.dumps(result["request"]["body"]).encode(),
                                       environ={})

    def test_teams_needs_a_delegated_chat_scope_and_a_configured_chat(self):
        chat = "19:abc_def@thread.v2"
        env = {**LIVE, "PLX_ACTIONS_TEAMS_CHATS": f"Purchasing={chat}"}
        d, graph = desk(env, roles=("Mail.Send", "Chat.ReadWrite.All"))
        result = confirm(d, "teams_message", c=ctx(environ=env))
        self.assertEqual(result["status"], "dry_run")
        self.assertIn("ChatMessage.Send not granted", result["missing"][0])
        d, graph = desk(env, scp="ChatMessage.Send")
        self.assertEqual(confirm(d, "teams_message", c=ctx(environ=env))["status"], "sent")
        self.assertEqual(graph.calls[0][0], "https://graph.microsoft.com/v1.0/chats/"
                                            "19%3Aabc_def%40thread.v2/messages")
        found = d.preview(card(), "teams_message", {"chat_id": "19:other@thread.v2"},
                          ctx(environ=env))
        self.assertIn("Pick a Teams chat", " ".join(found["problems"]))

    def test_the_one_sender_refuses_a_graph_write_without_the_flag_or_off_the_shapes(self):
        url = "https://graph.microsoft.com/v1.0/users/vince%40plx.example/sendMail"
        body = json.dumps({"message": {}, "saveToSentItems": True}).encode()
        with self.assertRaises(guard.ReadOnlyGuardError):
            guard._refuse_non_read_send("POST", url, body)          # flag unset here
        for bad_url, bad_body in [(url.replace("sendMail", "messages/x/move"), body),
                                  (url, json.dumps({"message": {}, "x": 1}).encode())]:
            with self.assertRaises(guard.ReadOnlyGuardError):
                guard.check_action_request("POST", bad_url, bad_body, environ=LIVE)
        with self.assertRaises(guard.ReadOnlyGuardError):
            guard.check_action_request("PATCH", url, body, environ=LIVE)


class OnceOnly(unittest.TestCase):
    def test_a_double_click_sends_once_and_a_reused_key_never_sends_another_payload(self):
        d, graph = desk()
        first = confirm(d)
        again = confirm(d)
        self.assertEqual((again["replayed"], again["seq"]), (True, first["seq"]))
        self.assertEqual(len(graph.calls), 1)
        with self.assertRaises(ca.ActionRefused) as refused:
            confirm(d, edits={"cc": ["ana@plx.example"]})
        self.assertEqual(refused.exception.status, 409)
        with self.assertRaises(ca.ActionRefused) as refused:   # a second dialog, new key
            confirm(d, key="n" * 20)
        self.assertIn("already sent", refused.exception.problems[0])
        self.assertEqual(len(graph.calls), 1)

    def test_a_send_that_never_recorded_its_outcome_is_not_sent_again(self):
        d, graph = desk()

        def crash(*_a):
            raise KeyboardInterrupt
        d.sender.dispatch = crash
        with self.assertRaises(KeyboardInterrupt):
            confirm(d)
        d.sender.dispatch = ActionSender(LIVE, bearer=lambda: token(roles=["Mail.Send"]),
                                         post=graph).dispatch
        self.assertEqual(confirm(d)["status"], "pending")      # same key: outcome unknown
        with self.assertRaises(ca.ActionRefused):
            confirm(d, key="m" * 20)
        self.assertEqual(graph.calls, [])


class Recipients(unittest.TestCase):
    def test_excluded_people_are_never_sender_or_recipient_and_no_guard_refuses_all(self):
        d, graph = desk()
        found = d.preview(card(), "send_email", {"cc": ["hidden@plx.example"]}, ctx())
        self.assertIn("hidden@plx.example cannot receive", " ".join(found["problems"]))
        found = d.preview(card(), "send_email", {"cc": ["nobody@plx.example"]}, ctx())
        self.assertIn("could not be checked", " ".join(found["problems"]))  # undecided
        found = d.preview(card(), "send_email", None,
                          ctx(sender="hidden@plx.example", sender_oid=EXCLUDED_OID))
        self.assertIn("cannot send from this mailbox", " ".join(found["problems"]))
        for guard_state in (PeopleExclusion([]), None):
            found = d.preview(card(), "send_email", None, ctx(exclusion=guard_state))
            self.assertIn("exclusion list is not available", " ".join(found["problems"]))
            with self.assertRaises(ca.ActionRefused):
                confirm(d, c=ctx(exclusion=guard_state))
        self.assertEqual(graph.calls, [])

    def test_an_unknown_domain_needs_one_more_confirm_and_a_forward_stays_inside(self):
        d, graph = desk()
        edits = {"to": ["someone@elsewhere.example"]}
        self.assertEqual(d.preview(card(), "send_email", edits, ctx())["unknown_domains"],
                         ["elsewhere.example"])
        with self.assertRaises(ca.ActionRefused) as refused:
            confirm(d, edits=edits)
        self.assertIn("elsewhere.example", refused.exception.problems[0])
        self.assertEqual(confirm(d, edits=edits, confirm_unknown_domains=True)["status"],
                         "sent")
        found = d.preview(card(), "forward", {"to": ["orders@supplier.example"]}, ctx())
        self.assertIn("colleagues only", " ".join(found["problems"]))


class WhatIsSent(unittest.TestCase):
    def test_every_date_and_quantity_stays_cited_and_the_previewed_payload_is_what_goes(self):
        d, graph = desk()
        body = card()["draft"]["body"].replace("2026-10-24", "2026-12-01")
        found = d.preview(card(), "send_email", {"body": body}, ctx())
        self.assertEqual(found["uncited"], ["2026-12-01"])
        self.assertFalse(next(c for c in found["citations"] if c["fact"] == "need_date")
                         ["present"])
        with self.assertRaises(ca.ActionRefused):
            confirm(d, edits={"body": body})
        with self.assertRaises(ca.ActionRefused) as refused:
            confirm(d, shown_hash="0" * 64)
        self.assertEqual(refused.exception.status, 409)
        for bad in [dict(confirmed=False), dict(idempotency_key="short")]:
            with self.assertRaises(ca.ActionRefused):
                d.confirm(card(), "send_email", None, ctx(), **{
                    "confirmed": True, "idempotency_key": "k" * 20,
                    "shown_hash": found["payload_hash"], **bad})
        with self.assertRaises(ca.ActionRefused):
            d.preview(card(), "post_to_portal", None, ctx())
        with self.assertRaises(ca.ActionRefused):
            d.preview(card(), "send_email", {"in_reply_to": "graph-mail://x/y@z"}, ctx())
        self.assertEqual(graph.calls, [])


class JournalAndState(unittest.TestCase):
    def test_local_actions_are_journalled_states_follow_and_a_tampered_journal_stops_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = ca.Journal.for_state_dir(Path(tmp))
            d, graph = desk(journal=journal)
            with self.assertRaises(ca.ActionRefused):
                confirm(d, "dismiss", key="d" * 20)               # a reason is required
            confirm(d, "snooze", {"until": "2026-11-06"}, key="s" * 20)
            self.assertEqual(d.states(VINCE, "Vince", date(2026, 11, 5))["pe-90388"]["status"],
                             "snoozed")
            self.assertEqual(d.states(VINCE, "Vince", date(2026, 11, 7)), {})
            self.assertEqual(d.states("ana@plx.example", "Ana", date(2026, 11, 5)), {})
            confirm(d)
            self.assertEqual([r["status"] for r in d.log(VINCE, "Vince")], ["sent", "snoozed"])
            self.assertEqual(d.log("ana@plx.example", "Ana"), [])
            lines = journal.path.read_text().splitlines()
            journal.path.write_text("\n".join([lines[0].replace("2026-11-06", "2026-12-06"),
                                               *lines[1:]]) + "\n")
            with self.assertRaises(ca.ActionRefused) as refused:
                confirm(d, "mark_done", key="z" * 20)
            self.assertEqual(refused.exception.status, 503)
            self.assertEqual(len(graph.calls), 1)


if __name__ == "__main__":
    unittest.main()
