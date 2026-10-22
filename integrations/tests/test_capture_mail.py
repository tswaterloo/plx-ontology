"""Versioned mail capture over an injected Graph transport.

No network and no credentials: every request goes to an in-process fake Graph
(directory, folder lookup, message delta with skip/delta tokens, message and
attachment GETs, throttling). Evidence label: ``injected-transport``.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest
import unittest.mock
from urllib.parse import parse_qs, unquote, urlsplit

from verify import live_verify as lv
from integrations import capture_mail, graph_read
from integrations.capture_mail import (CaptureError, CaptureScope, MailCapture,
                                       MailCaptureStore, assert_outside_git)
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import (BoundedBackoff, LiveSourceAudience, NotVisible,
                                            SourceUnavailable)
from ontology.json_contract import validate
from ontology.types import Actor
from operating import claim_checks
from operating.case_mail_scope import MailScope

TENANT_ID = "11111111-2222-3333-4444-555555555555"
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": TENANT_ID,
       "MICROSOFT_GRAPH_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
       "MICROSOFT_GRAPH_CLIENT_SECRET": "s3cr3t~CLIENT~value~do-not-leak"}
BOX_A = "12345678-aaaa-bbbb-cccc-00000000000a"
BOX_B = "12345678-aaaa-bbbb-cccc-00000000000b"
UPN_A = "orders@example-plx.com"
UPN_B = "finance@example-plx.com"
FOLDER_A = "AAMkFOLDER-A="
FOLDER_B = "AAMkFOLDER-B="
SUBJECT_SECRET = "Quarterly pricing for Contoso widgets"
BODY_SECRET = "Please confirm 120 cases at the agreed price."
ATTACHMENT_BYTES = b"%PDF-1.4 order confirmation bytes\n" * 40
ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating" / "ontology_tool_contracts.json").read_text())

CAPTURE = Actor("plx", frozenset({"svc:mail-capture"}), "restricted")
READER_A = Actor("plx", frozenset({"staff:orders"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:outsider"}), "restricted")


def j(status, value, headers=None):
    return status, dict(headers or {}), json.dumps(value).encode()


def keyed_delta(user: str, folder: str, query: str) -> str:
    return (f"https://graph.microsoft.com/v1.0/users/{user}/mailFolders('{folder}')"
            f"/messages/delta?{query}")


class FakeGraph:
    """A tiny Graph: two mailboxes, one folder each, message delta with change log."""

    def __init__(self) -> None:
        self.users = {
            BOX_A: {"id": BOX_A, "userPrincipalName": UPN_A, "mail": UPN_A,
                    "proxyAddresses": [f"SMTP:{UPN_A}"]},
            BOX_B: {"id": BOX_B, "userPrincipalName": UPN_B, "mail": UPN_B,
                    "proxyAddresses": [f"SMTP:{UPN_B}"]}}
        self.folders = {BOX_A: {"inbox": FOLDER_A, FOLDER_A.lower(): FOLDER_A},
                        BOX_B: {"inbox": FOLDER_B, FOLDER_B.lower(): FOLDER_B}}
        self.messages: dict[str, dict[str, dict]] = {BOX_A: {}, BOX_B: {}}
        self.attachments: dict[tuple[str, str], list[dict]] = {}
        self.log: list[tuple[int, str, str]] = []   # (seq, user, message id)
        self.seq = 0
        self.rounds: dict[str, tuple[list[dict], int]] = {}
        self.fail_delta: list[tuple[int, dict]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.received = "2026-09-20T09:00:00Z"
        self.fail_get: dict[str, int] = {}   # path regex -> remaining HTTP 500 answers
        self.attachment_page = 20            # attachments per listing page
        self.users_page = 999                # directory users per /users page
        self.forbidden: set[str] = set()     # mailboxes whose mail answers HTTP 403
        # Mailbox ids whose directory lookup answers HTTP 403 ("*": every lookup, an
        # app without User.Read.All) and the error code it sends (None: no error body).
        self.directory_forbidden: set[str] = set()
        self.directory_code: str | None = "Authorization_RequestDenied"
        self.refusal = 403                   # the status both refusals answer (401: token)
        self.token_status = 200              # 401: the token request is refused

    # -- mutations
    def put(self, user: str, mid: str, change_key: str, *, subject: str = SUBJECT_SECRET,
            body: str = BODY_SECRET, attachments: list[dict] | None = None,
            received: str | None = None) -> None:
        folder = self.folders[user]["inbox"]
        self.messages[user][mid] = {
            "id": mid, "changeKey": change_key, "subject": subject,
            "parentFolderId": folder, "lastModifiedDateTime": "2026-09-30T10:00:00Z",
            "receivedDateTime": received or self.received,
            "sentDateTime": received or self.received,
            "from": {"emailAddress": {"address": "supplier@outside.example"}},
            "toRecipients": [{"emailAddress": {"address": self.users[user]["mail"]}}],
            "conversationId": "CONV-" + mid, "internetMessageId": f"<{mid}@x>",
            "hasAttachments": bool(attachments), "webLink": "https://outlook.office365.com/x",
            "bodyPreview": body[:40], "_body": body}
        if attachments is not None:
            self.attachments[(user, mid)] = attachments
        self.seq += 1
        self.log.append((self.seq, user, mid))

    def delete(self, user: str, mid: str) -> None:
        del self.messages[user][mid]
        self.seq += 1
        self.log.append((self.seq, user, mid))

    # -- transport
    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers)))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com" and self.token_status != 200:
            return j(self.token_status, {"error": "invalid_client"})
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": "eyJ.TOKEN-do-not-leak.sig", "expires_in": 3599})
        assert method == "GET", method
        path = unquote(parts.path)
        for pattern, left in self.fail_get.items():
            if left and re.fullmatch(pattern, path):
                self.fail_get[pattern] = left - 1
                return j(500, {"error": {"code": "generalException"}})
        query = parse_qs(parts.query)
        found = re.fullmatch(r"/v1\.0/users/([^/]+)", path)
        if found and "*" in self.directory_forbidden:
            return j(self.refusal, {"error": {"code": self.directory_code}}
                     if self.directory_code else {})
        if found:
            key = found[1].lower()
            for user in self.users.values():
                if key in (user["id"], user["userPrincipalName"].lower()):
                    if user["id"] in self.directory_forbidden:
                        return j(self.refusal, {"error": {"code": self.directory_code}}
                                 if self.directory_code else {})
                    return j(200, user)
            return j(404, {"error": {"code": "ResourceNotFound"}})
        if path == "/v1.0/users":
            listed = [{"id": u["id"], "mail": u["mail"], "userType": u.get("userType", "Member"),
                       "userPrincipalName": u["userPrincipalName"]}
                      for u in self.users.values()]
            select = query.get("$select", ["id,mail,userType"])[0]
            skip = int(query.get("$skiptoken", ["0"])[0])
            body = {"value": listed[skip:skip + self.users_page]}
            if skip + self.users_page < len(listed):
                body["@odata.nextLink"] = (f"https://graph.microsoft.com/v1.0/users?"
                                           f"$select={select}&$top=999"
                                           f"&$skiptoken={skip + self.users_page}")
            return j(200, body)
        found = re.fullmatch(r"/v1\.0/users/([^/]+)/(?:mailFolders|messages)(?:/.*)?", path)
        if found and found[1].lower() in self.forbidden:
            # An Exchange application access policy refusal.
            return j(self.refusal, {"error": {"code": "ErrorAccessDenied"}})
        found = re.fullmatch(r"/v1\.0/users/([^/]+)/mailFolders/([^/]+)/messages/delta", path)
        if found:
            return self._delta(found[1].lower(), found[2], query, headers)
        found = re.fullmatch(r"/v1\.0/users/([^/]+)/mailFolders/([^/]+)", path)
        if found:
            folder = self.folders.get(found[1].lower(), {}).get(found[2].lower())
            return j(200, {"id": folder}) if folder else j(404, {"error": {}})
        found = re.fullmatch(r"/v1\.0/users/([^/]+)/messages/([^/]+)/attachments/([^/]+)",
                             path)
        if found:
            for item in self.attachments.get((found[1].lower(), found[2]), []):
                if item["id"] == found[3]:
                    return j(200, item)
            return j(404, {"error": {}})
        found = re.fullmatch(r"/v1\.0/users/([^/]+)/messages/([^/]+)/attachments", path)
        if found:
            # Graph pages attachment listings; a nextLink names the next page.
            items = self.attachments.get((found[1].lower(), found[2]), [])
            skip = int(query.get("$skip", ["0"])[0])
            page = items[skip:skip + self.attachment_page]
            body = {"value": [{k: v for k, v in a.items() if k != "contentBytes"}
                              for a in page]}
            if skip + self.attachment_page < len(items):
                kept = "&".join(p for p in parts.query.split("&")
                                if not p.startswith("$skip="))
                body["@odata.nextLink"] = (
                    f"https://graph.microsoft.com{parts.path}?{kept}"
                    f"&$skip={skip + self.attachment_page}")
            return j(200, body)
        found = re.fullmatch(r"/v1\.0/users/([^/]+)/messages/([^/]+)", path)
        if found:
            raw = self.messages.get(found[1].lower(), {}).get(found[2])
            if raw is None:
                return j(404, {"error": {}})
            result = {k: v for k, v in raw.items() if k != "_body"}
            if "body" in query.get("$select", [""])[0].split(","):
                result["body"] = {"contentType": "text", "content": raw["_body"]}
            return j(200, result)
        return j(404, {"error": {"code": "unrouted"}})

    def _public(self, raw: dict) -> dict:
        return {k: v for k, v in raw.items() if k != "_body"}

    def _delta(self, user, folder, query, headers):
        if self.fail_delta:
            status, extra = self.fail_delta.pop(0)
            return j(status, {"error": {"code": "throttled"}}, extra)
        size = int(re.search(r"odata\.maxpagesize=(\d+)", headers.get("Prefer", ""))[1])
        if "$skiptoken" in query:
            token = query["$skiptoken"][0]
            round_id, offset = token.split(":")
            entries, final = self.rounds[round_id]
            offset = int(offset)
        else:
            if "$deltatoken" in query:
                if query["$deltatoken"][0] == "expired":
                    return j(410, {"error": {"code": "syncStateNotFound"}})
                since = int(query["$deltatoken"][0])
                changed = []
                for seq, who, mid in self.log:
                    if seq > since and who == user and mid not in changed:
                        changed.append(mid)
                entries = [self._public(self.messages[user][m]) if m in self.messages[user]
                           else {"id": m, "@removed": {"reason": "deleted"}} for m in changed]
            else:
                since_filter = query.get("$filter", [""])[0]
                floor = since_filter.rsplit(" ", 1)[-1] if since_filter else ""
                entries = [self._public(m) for m in self.messages[user].values()
                           if m["receivedDateTime"] >= floor]
            round_id = f"r{len(self.rounds)}"
            self.rounds[round_id] = (entries, self.seq)
            offset = 0
        page = entries[offset:offset + size]
        body = {"value": page}
        if offset + size < len(entries):
            body["@odata.nextLink"] = keyed_delta(user, folder,
                                                  f"$skiptoken={round_id}:{offset + size}")
        else:
            body["@odata.deltaLink"] = keyed_delta(user, folder,
                                                   f"$deltatoken={self.rounds[round_id][1]}")
        return j(200, body)

    def delta_calls(self):
        return [c for c in self.calls if "/messages/delta" in c[1]]


def file_attachment(aid: str, content: bytes = ATTACHMENT_BYTES, name="confirmation.pdf"):
    return {"id": aid, "@odata.type": "#microsoft.graph.fileAttachment", "name": name,
            "contentType": "application/pdf", "size": len(content) + 300, "isInline": False,
            "lastModifiedDateTime": "2026-09-30T10:00:00Z",
            "contentBytes": base64.b64encode(content).decode()}


def item_attachment(aid: str):
    return {"id": aid, "@odata.type": "#microsoft.graph.itemAttachment", "name": "Fwd",
            "contentType": None, "size": 900, "isInline": False,
            "lastModifiedDateTime": "2026-09-30T10:00:00Z"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def capture_audience() -> LiveSourceAudience:
    return LiveSourceAudience({f"mail:{UPN_A}": ["svc:mail-capture"],
                               f"mail:{UPN_B}": ["svc:mail-capture"]}, tenant="plx",
                              classifications={"mail:*": "restricted"})


def reader_audience(**kwargs) -> LiveSourceAudience:
    """Per-reader audience at view time: staff:orders may read mailbox A only."""
    return LiveSourceAudience({f"mail:{UPN_A}": ["staff:orders"],
                               f"mail:{UPN_B}": ["staff:finance"]}, tenant="plx",
                              classifications={"mail:*": "restricted"}, **kwargs)


# Synthetic mailboxes: the allowlist is the explicit test opt-out (the shipped scope is
# bound to operating/case_mail_scope.json; test_capture_breadth covers that rule).
SYNTHETIC = MailScope([UPN_A, UPN_B])
SCOPE = CaptureScope([{"address": UPN_A, "folders": ["inbox"]},
                      {"address": UPN_B, "folders": ["inbox"]}], window_days=90,
                     internal_domains=["example-plx.com"], allowlist=SYNTHETIC)


class CaptureCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.graph = FakeGraph()
        self.clock = Clock()
        self.sleeps: list[float] = []
        self.store = MailCaptureStore(self.state, clock=self.clock)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def capture(self, **kwargs) -> MailCapture:
        api, self.transport = graph_read.build_graph(
            ENV, sender=self.graph,
            retry=BoundedBackoff(sleep=self.sleeps.append, jitter=lambda: 0.0))
        reader = GraphMailReader(api, capture_audience())
        kwargs.setdefault("sleep", self.sleeps.append)
        return MailCapture(self.store, reader, CAPTURE, page_size=2, **kwargs)

    def seed(self, count: int = 5) -> None:
        for n in range(1, count + 1):
            self.graph.put(BOX_A, f"MSG-{n}", f"CK-{n}-1")
        self.graph.put(BOX_B, "MSG-B1", "CKB-1", subject="Finance only subject")

    def folder(self, run, index=0):
        return run["mailboxes"][index]["folders"][0]


class DeltaPagingTests(CaptureCase):
    def test_initial_round_pages_to_delta_link_with_no_page_cap(self):
        self.seed(7)
        run = self.capture().run(SCOPE)
        folder = self.folder(run)
        self.assertTrue(run["complete"])
        self.assertEqual((folder["status"], folder["round"]), ("complete", "initial"))
        self.assertEqual(folder["counts"]["pages"], 4)          # 7 messages, 2 per page
        self.assertEqual(folder["counts"]["versions_new"], 7)
        self.assertEqual(folder["counts"]["bodies_captured"], 7)
        state = self.store.folder_state(BOX_A, "inbox")
        self.assertIsNone(state["next_url"])
        self.assertIn("$deltatoken=", unquote(state["delta_url"]))
        # The stored cursor keeps the grammar-built path (folder by id), not the server's.
        self.assertIn(f"/mailFolders/{FOLDER_A}/messages/delta", unquote(state["delta_url"]))
        first = self.graph.delta_calls()[0]
        query = parse_qs(urlsplit(first[1]).query)
        self.assertIn("parentFolderId", query["$select"][0])
        self.assertTrue(query["$filter"][0].startswith("receivedDateTime ge 2026-07-03T"))
        self.assertIn('IdType="ImmutableId"', first[2]["Prefer"])
        self.assertIn("odata.maxpagesize=2", first[2]["Prefer"])
        # GET-only: the one non-GET is the OAuth token POST.
        methods = {(m, urlsplit(u).hostname) for m, u, _h in self.graph.calls}
        self.assertEqual({m for m, h in methods if h == "graph.microsoft.com"}, {"GET"})
        self.assertEqual({h for m, h in methods if m != "GET"}, {"login.microsoftonline.com"})

    def test_delta_link_resume_reads_only_changes_and_keeps_history(self):
        self.seed(3)
        capture = self.capture()
        capture.run(SCOPE)
        old_handle = graph_read.mail_citation(BOX_A, "MSG-1", "CK-1-1")
        before = self.clock.now
        known_before = capture_mail._stamp(before)
        reads_before = len([c for c in self.graph.calls if "/messages/MSG-" in c[1]])
        self.graph.put(BOX_A, "MSG-1", "CK-1-2", subject="Revised: " + SUBJECT_SECRET)
        self.graph.delete(BOX_A, "MSG-2")
        self.graph.put(BOX_A, "MSG-9", "CK-9-1")
        run = capture.run(SCOPE)
        folder = self.folder(run)
        self.assertEqual((folder["round"], folder["resumed_from"], folder["complete"]),
                         ("incremental", "delta_url", True))
        self.assertEqual(folder["counts"]["messages_seen"], 2)   # only the changes
        self.assertEqual(folder["counts"]["versions_new"], 2)
        self.assertEqual(folder["counts"]["versions_superseded"], 1)
        self.assertEqual(folder["counts"]["removed"], 1)
        reads_after = len([c for c in self.graph.calls if "/messages/MSG-" in c[1]])
        self.assertEqual(reads_after - reads_before, 2)          # MSG-1 v2 and MSG-9 only
        # History: both versions of MSG-1 kept, the old one superseded when v2 arrived.
        audience = reader_audience()
        new_handle = graph_read.mail_citation(BOX_A, "MSG-1", "CK-1-2")
        history = self.store.history(READER_A, audience, new_handle)
        self.assertEqual([h["l1_handle"] for h in history], [old_handle, new_handle])
        self.assertIsNotNone(history[0]["superseded_at"])
        self.assertIsNone(history[1]["superseded_at"])
        old = self.store.resolve(old_handle, READER_A, audience)
        self.assertEqual(old["row"]["subject"], SUBJECT_SECRET)
        self.assertIsNotNone(old["superseded_at"])
        # as_known_at before the change: the old version, MSG-2 still present.
        then = self.store.messages(READER_A, audience, as_known_at=known_before)
        self.assertEqual({i["l1_handle"] for i in then["items"]} & {old_handle, new_handle},
                         {old_handle})
        self.assertIn("MSG-2", {i["message_id"] for i in then["items"]})
        now = self.store.messages(READER_A, audience)
        self.assertNotIn("MSG-2", {i["message_id"] for i in now["items"]})
        removed = self.store.resolve(graph_read.mail_citation(BOX_A, "MSG-2", "CK-2-1"),
                                     READER_A, audience)
        self.assertEqual(removed["removed_reason"], "deleted")

    def test_interrupted_round_resumes_from_the_stored_next_link(self):
        self.seed(5)
        run = self.capture(page_budget=1).run(SCOPE)
        folder = self.folder(run)
        self.assertEqual((folder["status"], folder["reason"]), ("incomplete", "page_budget"))
        self.assertFalse(self.store.completeness(READER_A, reader_audience())["complete"])
        state = self.store.folder_state(BOX_A, "inbox")
        self.assertIn("$skiptoken=", unquote(state["next_url"]))
        run = self.capture().run(SCOPE)
        folder = self.folder(run)
        self.assertEqual((folder["resumed_from"], folder["complete"]), ("next_url", True))
        self.assertEqual(folder["counts"]["pages"], 2)
        self.assertEqual(self.store.counts(READER_A, reader_audience())["messages_current"], 5)

    def test_throttled_page_waits_then_completes_without_loss(self):
        self.seed(5)
        # 429 x3 exhausts the guard's bounded retry (2) once; the capture waits and resumes.
        self.graph.fail_delta = [(429, {"Retry-After": "3"})] * 3
        run = self.capture(throttle_waits=(30.0,)).run(SCOPE)
        folder = self.folder(run)
        self.assertTrue(folder["complete"])
        self.assertEqual(folder["counts"]["throttle_waits"], 1)
        self.assertEqual(folder["counts"]["versions_new"], 5)
        self.assertIn(3.0, self.sleeps)      # Retry-After honoured by the bounded retry
        self.assertIn(30.0, self.sleeps)     # then the capture-level wait

    def test_persistent_throttle_stops_incomplete_and_the_next_run_resumes(self):
        self.seed(5)
        capture = self.capture(throttle_waits=(1.0,), page_budget=None)
        # First page ok, then the second page stays throttled.
        original = self.graph._delta
        calls = {"n": 0}

        def flaky(user, folder, query, headers):
            calls["n"] += 1
            if calls["n"] >= 2 and calls["n"] <= 7:
                return j(429, {"error": {"code": "throttled"}}, {"Retry-After": "1"})
            return original(user, folder, query, headers)

        self.graph._delta = flaky
        folder = self.folder(capture.run(SCOPE))
        self.assertEqual((folder["status"], folder["reason"]), ("incomplete", "throttled"))
        self.assertEqual(folder["counts"]["versions_new"], 2)
        self.assertIsNotNone(self.store.folder_state(BOX_A, "inbox")["next_url"])
        folder = self.folder(capture.run(SCOPE))
        self.assertEqual((folder["resumed_from"], folder["complete"]), ("next_url", True))
        self.assertEqual(self.store.counts(READER_A, reader_audience())["messages_current"], 5)

    def test_expired_delta_token_restarts_the_round(self):
        self.seed(3)
        capture = self.capture()
        capture.run(SCOPE)
        state = self.store.folder_state(BOX_A, "inbox")
        self.store.save_folder(BOX_A, "inbox", delta_url=re.sub(
            r"deltatoken=[^&]+", "deltatoken=expired", state["delta_url"]))
        folder = self.folder(capture.run(SCOPE))
        self.assertTrue(folder["complete"])
        self.assertEqual(folder["counts"]["round_restarts"], 1)
        self.assertEqual(folder["counts"]["unchanged"], 3)   # nothing re-recorded
        self.assertEqual(folder["counts"]["versions_new"], 0)

    def test_a_cursor_for_another_mailbox_is_refused_before_io(self):
        self.seed(1)
        capture = self.capture()
        capture.run(SCOPE)
        self.store.save_folder(BOX_A, "inbox", delta_url=keyed_delta(BOX_B, FOLDER_B,
                                                                     "$deltatoken=1"))
        box_a_reads = lambda: [c for c in self.graph.delta_calls()  # noqa: E731
                               if f"/users/{BOX_A}/" in c[1]]
        sent = len(box_a_reads())
        folder = self.folder(capture.run(SCOPE))
        self.assertEqual((folder["complete"], folder["reason"]),
                         (False, "source_error:nextlink_escaped"))
        self.assertEqual(len(box_a_reads()), sent)
        self.assertFalse(any(f"/users/{BOX_B}/" in c[1] and "deltatoken=1" in c[1]
                             for c in self.graph.delta_calls()))

    def test_a_link_the_guard_would_refuse_is_never_followed_and_the_round_restarts(self):
        """Graph shares the check: a nextLink with a raw space was
        stored as next_url and refused by check_request when followed
        (ReadOnlyGuardError, uncaught: every later run raised on it). Now it is refused
        where it arrives (or, for a stored cursor, before or when it is sent): the
        folder is incomplete, both cursors are dropped and the next run starts a fresh
        round; the refused link is never sent."""
        self.seed(5)
        original = self.graph._delta

        def raw_space(user, folder, query, headers):
            status, extra, raw = original(user, folder, query, headers)
            body = json.loads(raw)
            if query.get("$skiptoken") == ["r0:2"] and "@odata.nextLink" in body:
                body["@odata.nextLink"] = keyed_delta(user, folder, "$skiptoken=r0 4")
            return status, extra, json.dumps(body).encode()

        capture = self.capture()
        stored = keyed_delta(BOX_A, FOLDER_A, "$skiptoken=r9 2")
        for variant in ("server link", "stored cursor", "stored cursor, guard backstop"):
            with self.subTest(variant):
                self.graph._delta = raw_space if variant == "server link" else original
                if variant != "server link":
                    self.store.save_folder(BOX_A, "inbox", next_url=stored)
                patch = unittest.mock.patch.object(
                    capture.reader._api, "continuation_url",
                    side_effect=lambda first, link: link)
                if variant.endswith("backstop"):
                    patch.start()
                try:
                    folder = self.folder(capture.run(SCOPE))
                finally:
                    if variant.endswith("backstop"):
                        patch.stop()
                self.assertEqual((folder["complete"], folder["reason"]),
                                 (False, "source_error:nextlink_escaped"))
                state = self.store.folder_state(BOX_A, "inbox")
                self.assertEqual((state["next_url"], state["delta_url"]), (None, None))
                self.assertFalse([c for c in self.graph.delta_calls() if " " in c[1]])
                self.graph._delta = original
                sent = len(self.graph.delta_calls())
                again = self.folder(capture.run(SCOPE))
                self.assertEqual((again["complete"], again["round"]), (True, "initial"))
                self.assertNotIn("$skiptoken", unquote(self.graph.delta_calls()[sent][1]))

    def test_server_link_leaving_the_folder_is_refused(self):
        self.seed(3)
        original = self.graph._delta

        def escaping(user, folder, query, headers):
            status, extra, raw = original(user, folder, query, headers)
            body = json.loads(raw)
            if "@odata.nextLink" in body:
                body["@odata.nextLink"] = keyed_delta(BOX_B, FOLDER_B, "$skiptoken=r0:2")
            return status, extra, json.dumps(body).encode()

        self.graph._delta = escaping
        folder = self.folder(self.capture().run(SCOPE))
        self.assertEqual(folder["reason"], "source_error:nextlink_escaped")
        self.assertFalse(any(BOX_B in c[1] for c in self.graph.delta_calls()
                             if FOLDER_A in unquote(c[1])))

    def test_message_delta_requires_the_mailbox_grant_before_io(self):
        api, transport = graph_read.build_graph(ENV, sender=self.graph)
        reader = GraphMailReader(api, capture_audience())
        with self.assertRaises(NotVisible):
            next(reader.message_delta(OUTSIDER, UPN_A, "inbox"))
        self.assertEqual(self.graph.calls, [])
        with self.assertRaises(ValueError):
            next(reader.message_delta(CAPTURE, UPN_A, "inbox", page_size=0))


class VersionIntegrityTests(CaptureCase):
    def stamp(self) -> str:
        return capture_mail._stamp(self.clock.now)

    def versions_at(self, moment: str, message_id: str = "MSG-1") -> list[str]:
        items = self.store.messages(READER_A, reader_audience(), as_known_at=moment)["items"]
        return [i["change_key"] for i in items if i["message_id"] == message_id]

    def test_resync_round_supersedes_messages_gone_while_the_token_was_stale(self):
        self.seed(3)
        capture = self.capture()
        capture.run(SCOPE)
        before = self.stamp()
        # MSG-2 is deleted while our delta token is stale; Graph then answers 410.
        self.graph.delete(BOX_A, "MSG-2")
        state = self.store.folder_state(BOX_A, "inbox")
        self.store.save_folder(BOX_A, "inbox", delta_url=re.sub(
            r"deltatoken=[^&]+", "deltatoken=expired", state["delta_url"]))
        folder = self.folder(capture.run(SCOPE))
        self.assertEqual((folder["complete"], folder["counts"]["round_restarts"]), (True, 1))
        self.assertEqual(folder["counts"]["absent_after_resync"], 1)
        audience = reader_audience()
        now = {i["message_id"] for i in self.store.messages(READER_A, audience)["items"]}
        self.assertEqual(now, {"MSG-1", "MSG-3"})
        gone = self.store.resolve(graph_read.mail_citation(BOX_A, "MSG-2", "CK-2-1"),
                                  READER_A, audience)
        self.assertEqual(gone["removed_reason"], "absent_after_resync")
        self.assertEqual(self.versions_at(before, "MSG-2"), ["CK-2-1"])   # history kept
        self.assertTrue(self.store.completeness(READER_A, audience)["complete"])

    def test_resync_leaves_messages_older_than_the_new_window_alone(self):
        self.graph.put(BOX_A, "MSG-OLD", "CK-O", received="2026-08-01T09:00:00Z")
        self.graph.put(BOX_A, "MSG-NEW", "CK-N", received="2026-09-25T09:00:00Z")
        capture = self.capture()
        capture.run(SCOPE)
        narrow = CaptureScope([{"address": UPN_A}], window_days=30, allowlist=SYNTHETIC)
        folder = self.folder(capture.run(narrow))            # window change: full round
        self.assertEqual((folder["round"], folder["complete"]), ("initial", True))
        self.assertEqual(folder["counts"]["absent_after_resync"], 0)
        current = {i["message_id"] for i in
                   self.store.messages(READER_A, reader_audience())["items"]}
        self.assertEqual(current, {"MSG-OLD", "MSG-NEW"})

    def test_a_b_a_change_keys_never_overlap_in_as_known_at(self):
        self.graph.put(BOX_A, "MSG-1", "CK-A")
        capture = self.capture()
        capture.run(SCOPE)
        t1 = self.stamp()
        self.graph.put(BOX_A, "MSG-1", "CK-B")
        capture.run(SCOPE)
        t2 = self.stamp()
        self.graph.put(BOX_A, "MSG-1", "CK-A")          # back to the first changeKey
        folder = self.folder(capture.run(SCOPE))
        t3 = self.stamp()
        self.assertEqual(folder["counts"]["reappeared"], 1)
        self.assertEqual(self.versions_at(t1), ["CK-A"])
        self.assertEqual(self.versions_at(t2), ["CK-B"])
        self.assertEqual(self.versions_at(t3), ["CK-A"])
        handle = graph_read.mail_citation(BOX_A, "MSG-1", "CK-A")
        history = self.store.history(READER_A, reader_audience(), handle)
        self.assertEqual([h["change_key"] for h in history], ["CK-A", "CK-B", "CK-A"])
        self.assertEqual(sum(h["superseded_at"] is None for h in history), 1)

    def test_removal_gap_stays_a_gap_after_the_same_version_reappears(self):
        self.graph.put(BOX_A, "MSG-1", "CK-A")
        capture = self.capture()
        capture.run(SCOPE)
        self.graph.delete(BOX_A, "MSG-1")
        capture.run(SCOPE)
        gap = self.stamp()
        self.graph.put(BOX_A, "MSG-1", "CK-A")          # restored unchanged
        capture.run(SCOPE)
        self.assertEqual(self.versions_at(gap), [])
        self.assertEqual(self.versions_at(self.stamp()), ["CK-A"])

    def test_unavailable_body_and_attachment_are_retried_on_a_later_run(self):
        self.graph.put(BOX_A, "MSG-1", "CK-1-1", attachments=[file_attachment("ATT-1")])
        # Two failures: the first read and the end-of-round retry of the same run.
        self.graph.fail_get = {rf"/v1\.0/users/{BOX_A}/messages/MSG-1": 2}
        capture = self.capture()
        folder = self.folder(capture.run(SCOPE))
        self.assertEqual(folder["counts"]["bodies_unavailable"], 1)
        audience = reader_audience()
        box = self.store.completeness(READER_A, audience)["mailboxes"][0]
        self.assertEqual(box["degraded_bodies"], 1)
        # The next run reads no change from the delta, yet repairs the body.
        folder = self.folder(capture.run(SCOPE))
        self.assertEqual(folder["counts"]["bodies_recovered"], 1)
        box = self.store.completeness(READER_A, audience)["mailboxes"][0]
        self.assertEqual(box["degraded_bodies"], 0)
        item = self.store.messages(READER_A, audience, include_body=True)["items"][0]
        self.assertEqual(item["body"]["untrusted_text"], BODY_SECRET)
        # Attachment: one non-transient failure, then recovered.
        self.graph.put(BOX_A, "MSG-2", "CK-2-1", attachments=[file_attachment("ATT-2")])
        self.graph.fail_get = {rf"/v1\.0/users/{BOX_A}/messages/MSG-2/attachments/ATT-2": 2}
        capture.run(SCOPE)
        self.assertEqual(self.store.completeness(READER_A, audience)["mailboxes"][0]
                         ["degraded_attachments"], 1)
        folder = self.folder(capture.run(SCOPE))
        self.assertEqual(folder["counts"]["attachments_recovered"], 1)
        handle = (graph_read.mail_citation(BOX_A, "MSG-2", "CK-2-1")
                  + f"#attachment=ATT-2&sha256={sha256(ATTACHMENT_BYTES).hexdigest()}")
        self.assertEqual(self.store.attachment_content(handle, READER_A, audience),
                         ATTACHMENT_BYTES)

    def attachment_lists(self) -> int:
        return len([c for c in self.graph.calls
                    if urlsplit(c[1]).path.endswith("/attachments")])

    def stored_attachments(self) -> int:
        return self.store.db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]

    def test_more_than_fifty_attachments_are_paged_and_all_captured(self):
        atts = [file_attachment(f"ATT-{i:02d}", content=f"c{i}".encode()) for i in range(51)]
        self.graph.put(BOX_A, "MSG-1", "CK-1-1", attachments=atts)
        run = self.capture().run(SCOPE)
        self.assertTrue(run["complete"])
        self.assertEqual(self.folder(run)["counts"]["attachment_lists_incomplete"], 0)
        self.assertEqual(self.attachment_lists(), 3)   # 20 + 20 + 11, by nextLink
        self.assertEqual(self.stored_attachments(), 51)
        box = self.store.completeness(READER_A, reader_audience())["mailboxes"][0]
        self.assertTrue(box["complete"])
        self.assertEqual(box["truncated_attachment_lists"], 0)

    def test_truncated_attachment_list_is_durable_retried_and_never_complete(self):
        # capture-mail-attachment-pagination: 51 attachments over a
        # listing cap of 50, then a re-observed unchanged changeKey.
        atts = [file_attachment(f"ATT-{i:02d}", content=f"c{i}".encode()) for i in range(51)]
        self.graph.put(BOX_A, "MSG-1", "CK-1-1", attachments=atts)
        capped = self.capture(attachment_list_cap=50)
        audience = reader_audience()
        for number in (1, 2):
            before = self.attachment_lists()
            if number == 2:   # the same version listed again by the delta
                self.graph.seq += 1
                self.graph.log.append((self.graph.seq, BOX_A, "MSG-1"))
            run = capped.run(SCOPE)
            folder = self.folder(run)
            self.assertFalse(run["complete"], number)
            self.assertFalse(folder["complete"], number)
            self.assertEqual(folder["reason"], "attachment_lists_truncated")
            self.assertGreater(self.attachment_lists(), before, number)   # retried
            self.assertEqual(self.stored_attachments(), 50)
            box = self.store.completeness(READER_A, audience)["mailboxes"][0]
            self.assertFalse(box["complete"], number)
            self.assertEqual(box["truncated_attachment_lists"], 1)
            self.assertFalse(self.store.completeness(READER_A, audience)["complete"])
        self.assertEqual(self.folder(run)["counts"]["unchanged"], 1)
        # A run whose listing reaches the end repairs the same version in place.
        run = self.capture().run(SCOPE)
        folder = self.folder(run)
        self.assertTrue(run["complete"])
        self.assertEqual(folder["counts"]["attachment_lists_recovered"], 1)
        self.assertEqual(folder["counts"]["versions_new"], 0)
        self.assertEqual(self.stored_attachments(), 51)
        box = self.store.completeness(READER_A, audience)["mailboxes"][0]
        self.assertTrue(box["complete"])
        self.assertEqual(box["truncated_attachment_lists"], 0)
        view = self.store.messages(READER_A, audience)["items"][0]
        self.assertEqual(len(view["attachments"]), 51)

    def test_listing_cut_at_the_hard_cap_is_capped_after_two_relists(self):
        # A message with more attachments than the hard cap must
        # not be re-listed forever, yet its folder stays incomplete.
        atts = [file_attachment(f"ATT-{i:02d}", content=f"c{i}".encode()) for i in range(51)]
        self.graph.put(BOX_A, "MSG-1", "CK-1-1", attachments=atts)
        with unittest.mock.patch.object(capture_mail, "MAX_ATTACHMENTS_LISTED", 50):
            capture = self.capture(attachment_list_cap=50)
            # Run 1: the first listing is cut, and the end-of-round repair re-lists
            # it once (re-list 1, still cut).
            folder = self.folder(capture.run(SCOPE))
            self.assertEqual(folder["counts"]["attachment_lists_incomplete"], 2)
            self.assertEqual(folder["counts"]["attachment_lists_capped"], 0)
            folder = self.folder(capture.run(SCOPE))            # re-list 2: capped
            self.assertEqual(folder["counts"]["attachment_lists_capped"], 1)
            status = self.store.db.execute("SELECT attachments_status FROM messages"
                                           ).fetchone()[0]
            self.assertEqual(status, "truncated_capped")
            before = self.attachment_lists()
            run = capture.run(SCOPE)                            # no further re-list
            self.assertEqual(self.attachment_lists(), before)
        folder = self.folder(run)
        self.assertFalse(run["complete"])
        self.assertEqual(folder["reason"], "attachment_lists_truncated")
        box = self.store.completeness(READER_A, reader_audience())["mailboxes"][0]
        self.assertFalse(box["complete"])
        self.assertEqual(box["truncated_attachment_lists"], 1)
        self.assertEqual(self.store.degraded(BOX_A, FOLDER_A, 10), [])

    def test_schema_2_store_marks_possibly_cut_listings_truncated(self):
        atts = [file_attachment(f"ATT-{i:02d}", content=f"c{i}".encode()) for i in range(50)]
        self.graph.put(BOX_A, "MSG-1", "CK-1-1", attachments=atts)
        self.graph.put(BOX_A, "MSG-2", "CK-2-1", attachments=[file_attachment("ATT-X")])
        self.capture().run(SCOPE)
        with self.store.db:   # what a schema 2 file looks like
            self.store.db.execute("ALTER TABLE messages DROP COLUMN attachments_status")
            self.store.db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        self.store.close()
        self.store = MailCaptureStore(self.state, clock=self.clock)
        rows = dict(self.store.db.execute(
            "SELECT message_id, attachments_status FROM messages").fetchall())
        self.assertEqual(rows, {"MSG-1": "truncated", "MSG-2": "complete"})
        box = self.store.completeness(READER_A, reader_audience())["mailboxes"][0]
        self.assertFalse(box["complete"])

    def test_identical_bodies_are_stored_once(self):
        self.graph.put(BOX_A, "MSG-1", "CK-1-1")
        capture = self.capture()
        capture.run(SCOPE)
        for n in range(2, 5):                            # flag/read: changeKey moves
            self.graph.put(BOX_A, "MSG-1", f"CK-1-{n}")
            capture.run(SCOPE)
        body_sha = sha256(BODY_SECRET.encode()).hexdigest()
        rows = self.store.db.execute("SELECT body_sha256, record FROM messages "
                                     "WHERE message_id='MSG-1'").fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["body_sha256"] for r in rows}, {body_sha})
        self.assertFalse(any(BODY_SECRET in r["record"] for r in rows))
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM blobs WHERE kind='body'").fetchone()[0], 1)

    def test_store_bound_stops_capture_and_is_reported(self):
        self.seed(40)
        self.store.max_store_bytes = self.store.store_bytes() + 6000
        folder = self.folder(self.capture().run(SCOPE))
        self.assertEqual((folder["complete"], folder["reason"]), (False, "store_full"))
        report = self.store.completeness(READER_A, reader_audience())
        self.assertEqual(report["store_bound"]["max_bytes"], self.store.max_store_bytes)
        self.assertFalse(report["complete"])
        self.assertGreater(report["mailboxes"][0]["stored_bytes"], 0)
        self.assertLess(report["mailboxes"][0]["current_messages"], 40)
        self.assertTrue(report["store_bound"]["full"] or folder["reason"] == "store_full")
        self.assertNotIn("store_bound", self.store.completeness(OUTSIDER, reader_audience()))

    def test_truncated_directory_listing_is_not_a_complete_run(self):
        self.seed(1)
        scope = CaptureScope([], all_mailboxes=True, allowlist=SYNTHETIC)
        run = self.capture().run(scope, directory=lambda: ([BOX_A], "truncated:page_cap", 0))
        self.assertTrue(run["mailboxes"][0]["complete"])
        self.assertFalse(run["complete"])
        self.assertEqual(run["directory"], "truncated:page_cap")


class SchedulerTests(CaptureCase):
    """The capture scheduler shared with SharePoint (``capture_mail.schedule_units``)."""

    def scope_of(self, small: int) -> tuple[CaptureScope, LiveSourceAudience]:
        """Mailbox A first with a 12-message inbox (six delta pages of two), then
        ``small`` mailboxes of one message each (one page)."""
        upns = [UPN_A]
        for n in range(1, small + 1):
            box, upn, folder = (f"12345678-aaaa-bbbb-cccc-{n:012d}",
                                f"box{n}@example-plx.com", f"AAMkIN-{n:03d}=")
            self.graph.users[box] = {"id": box, "userPrincipalName": upn, "mail": upn,
                                     "proxyAddresses": [f"SMTP:{upn}"]}
            self.graph.folders[box] = {"inbox": folder, folder.lower(): folder}
            self.graph.messages[box] = {}
            self.graph.put(box, f"S-{n}", "CK-1")
            upns.append(upn)
        for n in range(12):
            self.graph.put(BOX_A, f"MSG-{n}", "CK-1")
        scope = CaptureScope([{"address": u, "folders": ["inbox"]} for u in upns],
                             internal_domains=["example-plx.com"], allowlist=MailScope(upns))
        return scope, LiveSourceAudience({f"mail:{u}": ["svc:mail-capture"] for u in upns},
                                         tenant="plx", classifications={"mail:*": "restricted"})

    def test_a_scope_larger_than_one_budget_completes_more_every_run_until_all(self):
        # Live 2026-10-12 (capture.breadth): both cycles ended at complete_bound
        # 4 of 321 with 262 folders cut by the time budget. Every run walked the folders
        # in the same order, so the leading ones (complete folders' deltas and large
        # folders the page budget stops) spent each budget again and the cut ones were
        # never reached. Each delta page costs one second and a run has three (a folder
        # two pages at most): a round cut mid-way resumes first, then the folders not yet
        # started. N=1/10/60 small mailboxes behind one large one (k = 3 runs): the large
        # one completes within k + 1 runs, complete folders grow at least every k + 1 runs
        # and never fall, all complete within the pages' worth of runs, every message
        # captured once, no initial round started twice, and then only deltas.
        for small in (1, 10, 60):
            with self.subTest(small=small), tempfile.TemporaryDirectory() as tmp:
                self.graph = FakeGraph()
                self.store.close()
                self.store = MailCaptureStore(Path(tmp) / "state", clock=self.clock)
                scope, rights = self.scope_of(small)
                api, _t = graph_read.build_graph(ENV, sender=self.graph, retry=BoundedBackoff(
                    sleep=self.sleeps.append, jitter=lambda: 0.0))
                reader = GraphMailReader(api, rights)

                def pages():
                    return len(self.graph.delta_calls())

                bound, runs, stale, large = [], 0, 0, None
                while not bound or bound[-1] < small + 1:
                    runs += 1
                    self.assertLessEqual(runs, -(-(6 + small) // 3) + 2, bound)
                    run = MailCapture(self.store, reader, CAPTURE, page_size=2, page_budget=2,
                                      budget_seconds=3, monotonic=pages,
                                      sleep=self.sleeps.append).run(scope)
                    bound.append(run["complete_bound"])
                    if large is None and run["mailboxes"][0]["folders"][0]["complete"]:
                        large = runs
                    folders = [f for m in run["mailboxes"] for f in m["folders"]]
                    # A complete folder the budget deferred stays complete; the run is not.
                    stale += sum(1 for f in folders if f["complete"] and f.get("deferred"))
                    self.assertEqual(run["complete"], all(
                        f["complete"] and not f.get("deferred") for f in folders))
                self.assertEqual(sorted(bound), bound)
                self.assertLessEqual(large, 4, bound)
                grown = [0] + bound
                self.assertEqual([i for i in range(len(grown) - 4)
                                  if grown[i + 4] <= grown[i]], [], bound)
                if small > 1:
                    self.assertGreater(stale, 0)
                initial = {}
                for _m, url, _h in self.graph.delta_calls():
                    if "$filter" in parse_qs(urlsplit(url).query):
                        box = unquote(url).split("/users/")[1].split("/")[0].lower()
                        initial[box] = initial.get(box, 0) + 1
                self.assertEqual(sorted(set(initial.values())), [1])
                self.assertEqual(len(initial), small + 1)
                current = self.store.db.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT message_id) FROM messages "
                    "WHERE superseded_at IS NULL").fetchone()
                self.assertEqual(tuple(current), (12 + small, 12 + small))
                self.assertEqual(self.store.db.execute(
                    "SELECT COUNT(*) FROM messages").fetchone()[0], 12 + small)
                # A complete folder reads only its delta.
                mark = len(self.graph.delta_calls())
                run = MailCapture(self.store, reader, CAPTURE, page_size=2).run(scope)
                self.assertTrue(run["complete"])
                self.assertEqual({(f["round"], f.get("resumed_from")) for m in run["mailboxes"]
                                  for f in m["folders"]}, {("incremental", "delta_url")})
                calls = self.graph.delta_calls()[mark:]
                self.assertEqual(len(calls), small + 1)
                self.assertTrue(all("$deltatoken" in parse_qs(urlsplit(u).query)
                                    for _m, u, _h in calls))


class RefusedCaptureTests(CaptureCase):
    """A capture the source refuses outright (live since 2026-10-16T21:18Z: every
    in-scope mailbox's directory lookup answered 403 Authorization_RequestDenied once
    MICROSOFT_GRAPH_CLIENT_ID named an app without User.Read.All or Mail.Read) is
    blocked upstream with the refused call and the missing permission, not a product
    failure; a partial refusal stays a capture gap. The parts have the Teams and list
    captures' shape (plx_hub_guard.upstream_blocks, GRAPH_BLOCKS): error_code
    missing_permission and the permission the call needs on 403, unauthorized on 401."""

    LOOKUP = {"part": "mail.mailbox_lookup", "source": "microsoft-graph", "http_status": 403,
              "error_code": "missing_permission", "permission": "User.Read.All",
              "cause": "host_misconfiguration"}
    FOLDER = {"part": "mail.folder_read", "source": "microsoft-graph", "http_status": 403,
              "error_code": "missing_permission", "permission": "Mail.Read",
              "cause": "host_misconfiguration"}
    UNAUTHORIZED = {"part": "mail.mailbox_lookup", "source": "microsoft-graph",
                    "http_status": 401, "error_code": "unauthorized",
                    "cause": "host_misconfiguration"}

    def capture_into(self, scope=SCOPE, name="into"):
        api, _t = graph_read.build_graph(ENV, sender=self.graph, retry=BoundedBackoff(
            sleep=self.sleeps.append, jitter=lambda: 0.0))
        return capture_mail.capture_into(Path(self.tmp.name) / name, scope,
                                         GraphMailReader(api, capture_audience()), CAPTURE,
                                         sleep=self.sleeps.append)

    def test_a_capture_refused_on_every_mailbox_is_blocked_with_the_missing_grant(self):
        outside_b = CaptureScope([{"address": UPN_A, "folders": ["inbox"]},
                                  {"address": UPN_B, "folders": ["inbox"]}],
                                 internal_domains=["example-plx.com"],
                                 allowlist=MailScope([UPN_A]))
        cases = [
            # (name, status, directory refused, its error code, mail refused, mail 500,
            #  scope, parts)
            ("directory_refused_everywhere", 403, {BOX_A, BOX_B},
             "Authorization_RequestDenied", set(), set(), SCOPE, [self.LOOKUP]),
            # The body's error code never changes the part (no code, or another one).
            ("directory_refused_without_a_code", 403, {BOX_A, BOX_B}, None, set(), set(),
             SCOPE, [self.LOOKUP]),
            ("mail_refused_everywhere", 403, set(), None, {BOX_A, BOX_B}, set(), SCOPE,
             [self.FOLDER]),
            ("one_lookup_one_mail_refused", 403, {BOX_A}, "Authorization_RequestDenied",
             {BOX_B}, set(), SCOPE, [self.FOLDER, self.LOOKUP]),
            ("refused_besides_an_allowlist_refusal", 403, set(), None, {BOX_A}, set(),
             outside_b, [self.FOLDER]),
            # A rejected token (401) is blocked too: no permission, the credential is out.
            ("directory_unauthorized_everywhere", 401, {"*"}, "InvalidAuthenticationToken",
             set(), set(), SCOPE, [self.UNAUTHORIZED]),
            ("mail_unauthorized_everywhere", 401, set(), None, {BOX_A, BOX_B}, set(), SCOPE,
             [{**self.UNAUTHORIZED, "part": "mail.folder_read"}]),
            # A partial refusal is a capture gap: some mailbox was read or failed otherwise.
            ("one_mailbox_read", 403, {BOX_A}, "Authorization_RequestDenied", set(), set(),
             SCOPE, []),
            ("one_mailbox_failed_otherwise", 403, {BOX_A}, "Authorization_RequestDenied",
             set(), {BOX_B}, SCOPE, []),
            ("one_mailbox_unauthorized", 401, {BOX_A}, None, set(), set(), SCOPE, []),
        ]
        for name, status, directory, code, mail, failing, scope, parts in cases:
            with self.subTest(name):
                self.graph = FakeGraph()
                self.seed()
                self.graph.refusal = status
                self.graph.directory_forbidden, self.graph.directory_code = directory, code
                self.graph.forbidden = mail
                self.graph.fail_get = {rf"/v1\.0/users/{box}/mailFolders/.*": 99
                                       for box in failing}
                summary = self.capture_into(scope, name)
                self.assertEqual(summary["blocked_upstream"], parts)
                self.assertFalse(summary["complete"])
                self.assertIn("forbidden" if status == 403 else "source_error:http_401",
                              summary["reasons"])
                self.assertEqual(summary["counts"]["messages_seen"],
                                 0 if parts else summary["counts"]["messages_seen"])
                # The live check reads each part as blocked by the host, never a pass.
                self.assertEqual([p["cause"] for p in lv.blocked_parts(summary)],
                                 ["host_misconfiguration"] * len(parts))
                text = json.dumps(summary)
                for leak in (UPN_A, UPN_B, BOX_A, BOX_B, "TOKEN-do-not-leak"):
                    self.assertNotIn(leak, text)

    def test_a_refused_token_request_is_blocked_unauthorized_like_teams_and_lists(self):
        # The credential refused at the token endpoint (401 invalid_client, e.g. a stale
        # client secret) fails every read with HTTP 401: blocked by the same rule the
        # Teams and list captures apply to it, never a capture failure.
        self.seed()
        for status, parts in ((401, [self.UNAUTHORIZED]), (400, [])):
            with self.subTest(status):
                self.graph.token_status = status
                summary = self.capture_into(name=f"token-{status}")
                self.assertEqual(summary["blocked_upstream"], parts)
                self.assertEqual(summary["reasons"], ["source_error:token_refused"])

    def test_a_refused_folder_stays_blocked_on_the_next_run(self):
        # The second run reads the stored terminal reason again (re-probed last): the
        # refusal is re-measured, never turned into a pass or a silent empty capture.
        self.seed()
        self.graph.forbidden = {BOX_A, BOX_B}
        self.assertEqual(self.capture_into()["blocked_upstream"], [self.FOLDER])
        self.assertEqual(self.capture_into()["blocked_upstream"], [self.FOLDER])
        self.graph.forbidden = set()
        again = self.capture_into()
        self.assertEqual(again["blocked_upstream"], [])
        self.assertGreater(again["counts"]["messages_seen"], 0)

    def test_a_deferred_refused_folder_without_diagnostics_still_names_the_grant(self):
        # A folder the budget deferred keeps its stored terminal reason but no fresh
        # diagnostics: the part is read from that reason, the same as a fresh refusal.
        run = {"mailboxes": [{"status": "incomplete", "folders": [
            {"reason": "forbidden", "deferred": True, "complete": False}]}]}
        self.assertEqual(capture_mail.refused_parts(run), [self.FOLDER])
        self.assertEqual(capture_mail.refused_parts({"mailboxes": []}), [])
        self.assertEqual(capture_mail.refused_parts({"mailboxes": [
            {"status": "outside_allowlist", "folders": []}]}), [])


class AttachmentCaptureTests(CaptureCase):
    def test_file_attachment_bytes_are_sha256_pinned_and_resolve_offline(self):
        self.graph.put(BOX_A, "MSG-1", "CK-1-1",
                       attachments=[file_attachment("ATT-1"), item_attachment("ATT-2")])
        folder = self.folder(self.capture().run(SCOPE))
        self.assertEqual(folder["counts"]["attachments_captured"], 1)
        self.assertEqual(folder["counts"]["attachments_metadata_only"], 1)
        digest = sha256(ATTACHMENT_BYTES).hexdigest()
        message = graph_read.mail_citation(BOX_A, "MSG-1", "CK-1-1")
        handle = f"{message}#attachment=ATT-1&sha256={digest}"
        audience = reader_audience()
        self.assertEqual(self.store.attachment_content(handle, READER_A, audience),
                         ATTACHMENT_BYTES)
        resolved = self.store.resolve(handle, READER_A, audience)
        self.assertEqual(resolved["row"]["content_sha256"], digest)
        self.assertEqual(resolved["message_handle"], message)
        blob = self.store.blobs / digest[:2] / digest
        self.assertEqual(blob.read_bytes(), ATTACHMENT_BYTES)
        view = self.store.messages(READER_A, audience)["items"][0]
        kinds = {a["attachment_id"]: a["content_status"] for a in view["attachments"]}
        self.assertEqual(kinds, {"ATT-1": "captured", "ATT-2": "not_file_attachment"})
        # A tampered blob is refused, never served.
        blob.write_bytes(b"tampered")
        with self.assertRaises(CaptureError):
            self.store.attachment_content(handle, READER_A, audience)

    def test_captured_office_attachment_text_resolves_offline(self):
        from integrations.doc_convert import DOCX
        from integrations.tests.office_fixtures import (PURCHASE_ORDER_BODY, PURCHASE_ORDER_MD,
                                                        make_word)
        word = make_word(PURCHASE_ORDER_BODY)
        self.graph.put(BOX_A, "MSG-1", "CK-1-1", attachments=[
            {**file_attachment("ATT-1", word, name="po.docx"), "contentType": DOCX}])
        with unittest.mock.patch("integrations.doc_convert.installed_backend", return_value=None):
            self.capture().run(SCOPE)
            handle = (f"{graph_read.mail_citation(BOX_A, 'MSG-1', 'CK-1-1')}"
                      f"#attachment=ATT-1&sha256={sha256(word).hexdigest()}")
            calls = len(self.graph.calls)
            resolved = self.store.resolve(handle, READER_A, reader_audience(), text=True)
        self.assertEqual(len(self.graph.calls), calls)  # no source read
        self.assertEqual(resolved["row"]["content_text"], PURCHASE_ORDER_MD)
        self.assertIsNone(self.store.resolve(handle, OUTSIDER, reader_audience(), text=True))

    def test_changed_message_reuses_unchanged_attachment_bytes(self):
        self.graph.put(BOX_A, "MSG-1", "CK-1-1", attachments=[file_attachment("ATT-1")])
        capture = self.capture()
        capture.run(SCOPE)
        self.graph.put(BOX_A, "MSG-1", "CK-1-2", attachments=[file_attachment("ATT-1")])
        downloads = lambda: len([c for c in self.graph.calls  # noqa: E731
                                 if c[1].split("?")[0].endswith("/attachments/ATT-1")])
        before = downloads()
        folder = self.folder(capture.run(SCOPE))
        self.assertEqual(downloads(), before)
        self.assertEqual(folder["counts"]["attachments_reused"], 1)
        digest = sha256(ATTACHMENT_BYTES).hexdigest()
        new = graph_read.mail_citation(BOX_A, "MSG-1", "CK-1-2")
        self.assertEqual(self.store.attachment_content(
            f"{new}#attachment=ATT-1&sha256={digest}", READER_A, reader_audience()),
            ATTACHMENT_BYTES)


class AudienceTests(CaptureCase):
    def setUp(self) -> None:
        super().setUp()
        self.graph.put(BOX_A, "MSG-1", "CK-1-1", attachments=[file_attachment("ATT-1")])
        self.graph.put(BOX_B, "MSG-B1", "CKB-1", subject="Finance only subject",
                       attachments=[file_attachment("ATT-B", b"finance bytes")])
        self.capture().run(SCOPE)
        self.handles_b = [graph_read.mail_citation(BOX_B, "MSG-B1", "CKB-1"),
                          graph_read.mail_citation(BOX_B, "MSG-B1", "CKB-1")
                          + f"#attachment=ATT-B&sha256={sha256(b'finance bytes').hexdigest()}"]

    def test_withheld_mailbox_leaks_nothing(self):
        audience = reader_audience()
        # staff:orders sees mailbox A only; mailbox B contributes nothing at all.
        view = self.store.messages(READER_A, audience, include_body=True)
        self.assertEqual({i["mailbox"] for i in view["items"]}, {BOX_A})
        self.assertEqual(view["total"], 1)
        self.assertEqual(self.store.counts(READER_A, audience)["mailboxes"], 1)
        self.assertEqual([m["mailbox"] for m in
                          self.store.completeness(READER_A, audience)["mailboxes"]], [BOX_A])
        for handle in self.handles_b:
            self.assertIsNone(self.store.resolve(handle, READER_A, audience))
        self.assertIsNone(self.store.attachment_content(self.handles_b[1], READER_A, audience))
        self.assertEqual(self.store.history(READER_A, audience, self.handles_b[0]), [])
        # An actor with no grant: 0 rows, 0 counts, no completeness entry.
        outsider = self.store.messages(OUTSIDER, audience)
        self.assertEqual((outsider["items"], outsider["total"]), ([], 0))
        self.assertEqual(sum(self.store.counts(OUTSIDER, audience).values()), 0)
        self.assertEqual(self.store.completeness(OUTSIDER, audience)["mailboxes"], [])
        self.assertEqual(self.store.messages(OUTSIDER, audience, as_known_at=capture_mail
                                             ._stamp(self.clock.now))["total"], 0)
        # Withheld and never-captured handles read the same.
        never = graph_read.mail_citation(BOX_A, "MSG-404", "CK")
        self.assertIsNone(self.store.resolve(never, READER_A, audience))
        # Nothing of mailbox B appears anywhere in what staff:orders receives.
        shown = json.dumps([view, self.store.completeness(READER_A, audience)])
        for secret in (BOX_B, UPN_B, "Finance only subject", "MSG-B1"):
            self.assertNotIn(secret, shown)

    def test_reader_trimming_uses_the_mailbox_owner_acl(self):
        # Per-employee trimming at view time: an audience that requires the owner ACL
        # admits only a reader whose identity is the mailbox's.
        strict = reader_audience(identities={"staff:orders": ["someone@example-plx.com"]},
                                 source_acl_required=["mail:*"])
        self.assertEqual(self.store.messages(READER_A, strict)["total"], 0)
        owner = reader_audience(identities={"staff:orders": [UPN_A]},
                                source_acl_required=["mail:*"])
        self.assertEqual(self.store.messages(READER_A, owner)["total"], 1)

    def test_views_wrap_source_text_in_untrusted_envelopes(self):
        item = self.store.messages(READER_A, reader_audience(), include_body=True)["items"][0]
        schema = {"$ref": "#/$defs/untrusted_text", "$defs": CONTRACTS["$defs"]}
        for field in ("subject", "body_preview", "body"):
            self.assertEqual(validate(item[field], schema, root=CONTRACTS), [], field)
            self.assertEqual(item[field]["origin"]["citation"], item["l1_handle"])
            self.assertEqual(item[field]["origin"]["sender_class"], "external")
        self.assertEqual(item["body"]["untrusted_text"], BODY_SECRET)
        for field in ("from", "original_url", "internet_message_id"):
            self.assertEqual(validate(item[field], schema, root=CONTRACTS), [], field)
        for field in ("to", "cc"):
            for value in item[field]:
                self.assertEqual(validate(value, schema, root=CONTRACTS), [], field)
        self.assertEqual(len(item["to"]), 1)
        for field in ("name", "content_type"):
            self.assertEqual(validate(item["attachments"][0][field], schema, root=CONTRACTS),
                             [], field)
        self.assertEqual(validate(item["l1_handle"], {"$ref": "#/$defs/handle",
                                                      "$defs": CONTRACTS["$defs"]},
                                  root=CONTRACTS), [])

    def test_claim_checks_accepts_captured_handles(self):
        audience = reader_audience()
        handle = graph_read.mail_citation(BOX_A, "MSG-1", "CK-1-1")
        self.assertIsNotNone(claim_checks.HANDLE.fullmatch(handle))
        claims = claim_checks.parse_value_claims(
            [{"value": "MSG-1", "citation": handle, "field": "row.id"},
             {"value": "CONV-MSG-1", "citation": handle, "field": "row.conversation_id"}],
            [handle], [])
        # The text states the values its claims back (claim_value_unstated otherwise).
        item = {"id": "F1", "texts": ["The supplier message MSG-1 of conversation CONV-MSG-1 "
                                      "is captured."],
                "citations": [{"handle": handle, "status": "resolved"}],
                "value_claims": claims}
        result = claim_checks.check_items([item], rows=self.store.rows(READER_A, audience))
        self.assertTrue(result["passed"], result)
        denied = claim_checks.check_items([item], rows=self.store.rows(OUTSIDER, audience))
        self.assertFalse(denied["passed"])
        self.assertEqual({f["code"] for f in denied["failures"]}, {"claim_uncited"})


class StorageTests(unittest.TestCase):
    def test_state_dir_inside_a_git_tree_is_refused_before_anything_is_written(self):
        with self.assertRaises(CaptureError) as caught:
            MailCaptureStore(ROOT / "verify" / "mail-state")
        self.assertEqual(caught.exception.code, "state_dir_inside_git_tree")
        self.assertFalse((ROOT / "verify" / "mail-state").exists())
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            with self.assertRaises(CaptureError):
                MailCaptureStore(Path(tmp) / "nested" / "state")
            self.assertFalse((Path(tmp) / "nested").exists())

    def test_capture_files_live_under_the_state_dir_with_private_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "wb-state"
            self.assertEqual(assert_outside_git(state), state.resolve())
            store = MailCaptureStore(state)
            try:
                self.assertEqual(store.root, state.resolve() / "mail-capture")
                self.assertEqual(stat.S_IMODE(store.root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
                self.assertNotIn(str(ROOT), str(store.path))
            finally:
                store.close()


class ScopeAndCliTests(unittest.TestCase):
    def test_scope_validation(self):
        scope = CaptureScope.from_file(capture_mail.DEFAULT_SCOPE)
        self.assertEqual(scope.window_days, 90)
        self.assertTrue(scope.all_mailboxes)
        every = {"enabled": True, "exclude": [{"address": UPN_B}]}   # an exclusion needs a reason
        for bad in ({"schema": "x", "mailboxes": []},
                    {"schema": capture_mail.SCOPE_SCHEMA, "all_mailboxes": every},
                    {"schema": capture_mail.SCOPE_SCHEMA, "mailboxes": []},
                    {"schema": capture_mail.SCOPE_SCHEMA, "window_days": 0,
                     "mailboxes": [{"address": UPN_A}]},
                    {"schema": capture_mail.SCOPE_SCHEMA,
                     "mailboxes": [{"address": UPN_A, "folders": ["../x"]}]}):
            with self.assertRaises(CaptureError):
                CaptureScope.from_json(bad, allowlist=SYNTHETIC)

    def test_all_mailboxes_scope_lists_the_directory(self):
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-1", "CK-1-1")
        graph.put(BOX_B, "MSG-B1", "CKB-1")
        scope = CaptureScope([], all_mailboxes=True, all_mailbox_folders=["inbox"],
                             allowlist=SYNTHETIC)
        with tempfile.TemporaryDirectory() as tmp:
            store = MailCaptureStore(Path(tmp) / "s")
            try:
                api, _t = graph_read.build_graph(ENV, sender=graph)
                capture = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE)
                run = capture.run(scope, directory=lambda: capture_mail.directory_mailboxes(
                    api, scope.directory_allows))
                self.assertTrue(run["complete"])
                self.assertEqual(run["directory"], "complete")
                self.assertEqual(len(run["mailboxes"]), 2)
            finally:
                store.close()

    def test_cli_reports_counts_only_and_proves_resume_and_no_leak(self):
        graph = FakeGraph()
        for n in range(1, 4):
            graph.put(BOX_A, f"MSG-{n}", f"CK-{n}-1",
                      attachments=[file_attachment(f"ATT-{n}")] if n == 1 else None)
        with tempfile.TemporaryDirectory() as tmp:
            scope = Path(tmp) / "scope.json"
            scope.write_text(json.dumps({"schema": capture_mail.SCOPE_SCHEMA,
                                         "mailboxes": [{"address": UPN_A}]}))
            audience = Path(tmp) / "audience.json"
            audience.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {f"mail:{UPN_A}": ["svc:mail-capture"]},
                "classifications": {"mail:*": "restricted"}}))
            parser = argparse.ArgumentParser()
            capture_mail.add_cli_arguments(parser)
            args = parser.parse_args(["--state-dir", str(Path(tmp) / "state"), "--scope",
                                      str(scope), "--live-audience", str(audience),
                                      "--page-size", "2"])
            with unittest.mock.patch.object(MailScope, "builtin", lambda: SYNTHETIC):
                report = capture_mail.run_cli(args, environ=ENV, sender=graph,
                                              sleep=lambda s: None)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual(report["summary"]["delta_resumes"], 1)
        self.assertEqual(report["summary"]["messages_current"], 3)
        self.assertEqual(report["resolve_check"]["attachment_bytes_verified"], 1)
        spec = json.loads((Path(__file__).resolve().parents[2] / "verify/live-checks"
                           / "graph.mail_capture.json").read_text(encoding="utf-8"))
        self.assertEqual(lv._recorded(spec, report)["relations"],
                         {"messages": 3, "attachments": 1})
        # The Graph reads the run was answered for: the registry credits their endpoints.
        self.assertEqual(lv._recorded(spec, report)["operations_read"], [
            "graph.folder_messages_delta", "graph.mail_folder", "graph.message",
            "graph.message_attachment", "graph.message_attachments", "graph.user",
            "graph.users"])
        self.assertEqual(report["leak_check"], {"withheld_actor_mailboxes": 0,
                                                "withheld_actor_rows": 0,
                                                "withheld_actor_counts": 0,
                                                "withheld_actor_resolved": 0})
        self.assertEqual([r["mailboxes"][0]["folders"][0]["round"] for r in report["runs"]],
                         ["initial", "incremental"])
        text = json.dumps(report)
        for secret in (SUBJECT_SECRET, BODY_SECRET, UPN_A, BOX_A, "MSG-1", "ATT-1",
                       FOLDER_A, "TOKEN-do-not-leak", "do-not-leak", "supplier@"):
            self.assertNotIn(secret, text)

    def test_cli_refused_on_every_mailbox_is_blocked_by_the_live_check(self):
        # Every bound mailbox 403 (or 401: a rejected token) in every run: the check reads
        # blocked upstream with the missing grant (never measured, never a capture
        # failure: nothing was read); a partial refusal stays a fail.
        spec = json.loads((Path(__file__).resolve().parents[2] / "verify/live-checks"
                           / "graph.mail_capture.json").read_text(encoding="utf-8"))
        for name, status, forbidden, blocked in (
                ("all", 403, {BOX_A, BOX_B}, [("mail.folder_read", 403, "missing_permission",
                                               "Mail.Read")]),
                ("all_unauthorized", 401, {BOX_A, BOX_B},
                 [("mail.folder_read", 401, "unauthorized", None)]),
                ("one", 403, {BOX_A}, [])):
            with self.subTest(name):
                graph = FakeGraph()
                graph.put(BOX_A, "MSG-1", "CK-1-1")
                graph.put(BOX_B, "MSG-2", "CK-2-1")
                graph.refusal = status
                graph.forbidden = forbidden
                with tempfile.TemporaryDirectory() as tmp:
                    scope = Path(tmp) / "scope.json"
                    scope.write_text(json.dumps({
                        "schema": capture_mail.SCOPE_SCHEMA,
                        "mailboxes": [{"address": UPN_A}, {"address": UPN_B}]}))
                    audience = Path(tmp) / "audience.json"
                    audience.write_text(json.dumps({
                        "schema": "plx-live-source-audience/v1", "tenant": "plx",
                        "audiences": {f"mail:{u}": ["svc:mail-capture"]
                                      for u in (UPN_A, UPN_B)},
                        "classifications": {"mail:*": "restricted"}}))
                    parser = argparse.ArgumentParser()
                    capture_mail.add_cli_arguments(parser)
                    args = parser.parse_args(["--state-dir", str(Path(tmp) / "state"),
                                              "--scope", str(scope), "--live-audience",
                                              str(audience)])
                    with unittest.mock.patch.object(MailScope, "builtin", lambda: SYNTHETIC):
                        report = capture_mail.run_cli(args, environ=ENV, sender=graph,
                                                      sleep=lambda s: None)
                self.assertFalse(report["ok"])
                self.assertTrue(lv.check_expectations(spec, 1, report))
                if blocked:
                    self.assertEqual([(p["part"], p["http_status"], p["error_code"],
                                       p.get("permission"))
                                      for p in report["blocked_upstream"]], blocked)
                    self.assertEqual([p["cause"] for p in lv.blocked_parts(report)],
                                     ["host_misconfiguration"])
                    self.assertEqual(lv.check_expectations(spec, 1, report,
                                                           spec["expect_blocked"]), [])
                    self.assertFalse(lv._positive_minimum(spec["expect_blocked"]))
                else:
                    self.assertEqual(report["blocked_upstream"], [])
                    self.assertEqual(lv.blocked_parts(report), [])

    def test_graph_read_main_routes_the_mail_capture_command(self):
        graph = FakeGraph()
        graph.put(BOX_A, "MSG-1", "CK-1-1")
        with tempfile.TemporaryDirectory() as tmp:
            audience = Path(tmp) / "audience.json"
            audience.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {f"mail:{UPN_A}": ["svc:mail-capture"]},
                "classifications": {"mail:*": "restricted"}}))
            scope = Path(tmp) / "scope.json"
            scope.write_text(json.dumps({"schema": capture_mail.SCOPE_SCHEMA,
                                         "mailboxes": [{"address": UPN_A}]}))
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    unittest.mock.patch.object(MailScope, "builtin", lambda: SYNTHETIC):
                code = graph_read.main(["mail-capture", "--state-dir", str(Path(tmp) / "st"),
                                        "--scope", str(scope), "--live-audience",
                                        str(audience), "--runs", "1"],
                                       environ=ENV, sender=graph)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["check"], "graph.mail_capture")

    def test_the_live_check_audience_grants_every_mailbox_its_scope_captures(self):
        # Live 2026-10-05: complete_bound 2/22 when the check's scope widened to every
        # allowlisted mailbox while its audience grants one: a run that can never be
        # complete. The check's scope must lie inside its own grant.
        root = Path(__file__).resolve().parents[2]
        command = json.loads((root / "verify/live-checks/graph.mail_capture.json")
                             .read_text(encoding="utf-8"))["command"]
        scope = CaptureScope.from_file(root / command[command.index("--scope") + 1])
        audience = LiveSourceAudience.from_file(
            root / command[command.index("--live-audience") + 1])
        actor = Actor(audience.tenant, frozenset({capture_mail.CAPTURE_PRINCIPAL}),
                      "restricted")
        self.assertFalse(scope.all_mailboxes)
        self.assertTrue(scope.mailboxes)
        self.assertEqual([a for a, _ in scope.mailboxes
                          if not audience.allows(actor, f"mail:{a}")], [])


if __name__ == "__main__":
    unittest.main()
