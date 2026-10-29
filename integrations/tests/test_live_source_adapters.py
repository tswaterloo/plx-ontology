"""Injected-transport tests for the read-only Graph mail / SharePoint / BC adapters.

No network and no credentials: every request goes to an in-process fake
sender that records what would have been sent. Evidence label:
``injected-transport``.
"""

from __future__ import annotations

import contextlib
import io
import json
import pickle
import re
import unittest
from hashlib import sha256
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from integrations import bc_read, graph_read
from integrations.bc_read import BusinessCentralReader
from integrations.graph_read import GraphMailReader, SharePointReader
from integrations.http import SourceUnavailable
from integrations.live_source_guard import (BC_SCOPE, GRAPH_SCOPE, ClientCredentials,
                                            GuardedTransport, LiveSourceAudience, NotVisible,
                                            ReadOnlyGuardError, Secret, SourceApi,
                                            TokenProvider, _RefuseRedirect, check_request)
from ontology.core import Actor


TENANT_ID = "11111111-2222-3333-4444-555555555555"
SITE_ID = ("petralabx.sharepoint.com,11111111-0000-0000-0000-000000000001,"
           "11111111-0000-0000-0000-000000000002")
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_SECRET = "s3cr3t~CLIENT~value~do-not-leak"
ACCESS_TOKEN = "eyJ0eXAi.ACCESS-TOKEN-do-not-leak.sig"
TEMPAUTH = "tempauth=DOWNLOAD-TOKEN-do-not-leak"
MAILBOX = "vince@petralabx.com"
DRIVE = "b!drive-AbC123"
COMPANY = "0f0e0d0c-0b0a-0908-0706-050403020100"
USER_ID = "99999999-8888-7777-6666-555555555555"
PO_ID = "aaaaaaaa-0000-0000-0000-000000000001"
SP_HOST = "petralabx.sharepoint.com"
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": TENANT_ID, "MICROSOFT_GRAPH_CLIENT_ID": CLIENT_ID,
       "MICROSOFT_GRAPH_CLIENT_SECRET": CLIENT_SECRET, "BC_TENANT_ID": TENANT_ID,
       "BC_CLIENT_ID": CLIENT_ID, "BC_CLIENT_SECRET": CLIENT_SECRET,
       "BC_ENVIRONMENT": "Production", "SHAREPOINT_SITE_ID":
           SITE_ID, "SHAREPOINT_DRIVE_ID": DRIVE}


Response = tuple[int, dict, bytes]


def j(status: int, value: Any) -> Response:
    return status, {}, json.dumps(value).encode()


def live_drive_item(item: dict, url: str, download: str) -> Response:
    """A drive-item GET as live Graph answers it (PLXCUSTOMERS, 2026-10-01).

    ``$select`` instance-annotation terms (``@microsoft.graph.downloadUrl``)
    are silently ignored: 200 with only the plain selected properties plus
    ``@odata.context``/``@odata.etag``. The download URL is returned, under
    the annotation key, only when ``content.downloadUrl`` is selected.
    """
    terms = [t for t in parse_qs(urlsplit(url).query).get("$select", [""])[0].split(",") if t]
    if not terms:
        return j(200, item)
    body = {"@odata.context": "https://graph.microsoft.com/v1.0/$metadata#driveItem",
            "@odata.etag": item.get("eTag")}
    body.update({key: value for key, value in item.items() if key in terms})
    if "content.downloadUrl" in terms:
        body["@microsoft.graph.downloadUrl"] = download
    return j(200, body)


class FakeSender:
    """Routes by URL path (Graph/BC) or host+path (downloads); records every call."""

    def __init__(self, routes: dict[str, Any] | None = None,
                 directory: dict[str, dict] | None = None) -> None:
        self.routes = routes if routes is not None else {}
        # Graph resolves /users/{upn|id} case-insensitively to one directory object.
        self.directory = dict(DIRECTORY if directory is None else directory)
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.timeouts: list[float] = []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers), body))
        self.timeouts.append(timeout)
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": ACCESS_TOKEN, "expires_in": 3599})
        path = unquote(parts.path)
        if path == "/v1.0/users" and parts.hostname == "graph.microsoft.com" \
                and path not in self.routes:
            # The tenant listing the people guard binds from (one page, every user).
            unique = {u["id"]: u for u in self.directory.values() if u.get("id")}
            return j(200, {"value": list(unique.values())})
        user = re.fullmatch(r"/v1\.0/users/([^/]+)", path)
        if user and parts.hostname == "graph.microsoft.com":
            found = self.directory.get(user[1].lower())
            return j(200, found) if found else j(404, {"error": {"code": "ResourceNotFound"}})
        folder = re.fullmatch(r"/v1\.0/users/([^/]+)/mailFolders/([^/]+)", path)
        if folder and parts.hostname == "graph.microsoft.com":
            found = FOLDERS.get(folder[1].lower(), {}).get(folder[2].lower())
            return j(200, {"id": found}) if found else j(404, {"error": {"code": "NotFound"}})
        key = path if parts.hostname != SP_HOST else SP_HOST + path
        route = self.routes.get(key)
        if route is None:
            return j(404, {"error": {"code": "itemNotFound"}})
        return route(url) if callable(route) else route

    def api_calls(self) -> list[tuple[str, str, dict, bytes | None]]:
        return [c for c in self.calls if "login.microsoftonline.com" not in c[1]]


def graph_api(sender: FakeSender, environ=None) -> tuple[SourceApi, GuardedTransport]:
    return graph_read.build_graph(environ or ENV, sender=sender)


ACTOR = Actor("plx", frozenset({"staff:vince"}), "restricted")
OTHER = Actor("plx", frozenset({"staff:ops"}), "restricted")


def audience(**kwargs) -> LiveSourceAudience:
    audiences = kwargs.pop("audiences", {f"mail:{MAILBOX}": ["staff:vince", "staff:ops"],
                                         f"sharepoint:{DRIVE}:/Customers/*": ["staff:vince",
                                                                              "staff:ops"],
                                         f"sharepoint:{DRIVE}:/Customers/Secret*": [],
                                         "bc:production:*": ["staff:vince"]})
    return LiveSourceAudience(audiences, tenant="plx", **kwargs)


def message(mid: str, change_key: str, subject: str = "Customer PO: PO12172025",
            folder: str = "INBOX-V") -> dict:
    return {"id": mid, "changeKey": change_key, "subject": subject, "parentFolderId": folder,
            "lastModifiedDateTime": "2026-09-30T10:00:00Z",
            "receivedDateTime": "2026-09-30T09:00:00Z",
            "from": {"emailAddress": {"address": "Supplier@Example.com", "name": "S"}},
            "toRecipients": [{"emailAddress": {"address": MAILBOX}}],
            "webLink": "https://outlook.office365.com/owa/?ItemID=x",
            "bodyPreview": "Confirmed", "hasAttachments": False}


VINCE_ID = "12345678-aaaa-bbbb-cccc-000000000001"
CEO_ID = "12345678-aaaa-bbbb-cccc-0000000000ce"
CEO_UPN = "c.eo@petralabx.onmicrosoft.com"  # UPN differs from the SMTP address
_VINCE = {"id": VINCE_ID, "userPrincipalName": "Vince@PetraLabX.com",
          "mail": "Vince@PetraLabX.com", "proxyAddresses": ["SMTP:vince@petralabx.com"]}
_CEO = {"id": CEO_ID, "userPrincipalName": CEO_UPN, "mail": "CEO@petralabx.com",
        "proxyAddresses": ["SMTP:ceo@petralabx.com", "smtp:chief@petralabx.com",
                           "SIP:ceo@petralabx.com", "X500:/o=x"]}
_FIRST = {"id": USER_ID, "userPrincipalName": "first@petralabx.com"}
# Graph's /users/{x} accepts an object id or a UPN (not an SMTP alias).
DIRECTORY = {MAILBOX: _VINCE, VINCE_ID: _VINCE, CEO_UPN: _CEO, CEO_ID: _CEO,
             USER_ID: _FIRST, "first@petralabx.com": _FIRST}
MSG_PATH = f"/v1.0/users/{VINCE_ID}/messages"
CEO_MSG_PATH = f"/v1.0/users/{CEO_ID}/messages"
# Mail folders that exist under each user (folder id lookups by id or name).
FOLDERS = {VINCE_ID: {"inbox-v": "INBOX-V", "inbox": "INBOX-V"},
           CEO_ID: {"inbox-c": "INBOX-C", "inbox": "INBOX-C"},
           USER_ID: {"inbox-v": "INBOX-V"}}


class GuardTests(unittest.TestCase):
    def transport(self, environ=None) -> tuple[GuardedTransport, FakeSender]:
        sender = FakeSender({"/v1.0/users": j(200, {"value": []})})
        return GuardedTransport(sender, environ=environ or {}), sender

    def test_non_get_methods_refused_before_any_io(self):
        transport, sender = self.transport()
        for method in ("POST", "PATCH", "PUT", "DELETE", "MERGE", "OPTIONS", "HEAD"):
            with self.assertRaises(ReadOnlyGuardError, msg=method):
                transport.request(method, f"https://graph.microsoft.com{MSG_PATH}", {})
            with self.assertRaises(ReadOnlyGuardError, msg=method):
                transport.request(method, "https://api.businesscentral.dynamics.com/v2.0/t/"
                                  "Production/api/v2.0/companies", {})
        self.assertEqual(sender.calls, [])
        self.assertEqual(transport.requests_sent, 0)

    def test_foreign_hosts_and_url_tricks_refused_before_io(self):
        transport, sender = self.transport()
        for url in ("https://evil.example.com/v1.0/users",
                    "https://graph.microsoft.com.evil.com/v1.0/users",
                    "https://petra.cognitiveservices.azure.com/formrecognizer/x",
                    "https://petra.blob.core.windows.net/c/x",
                    "https://petralabx.sharepoint.com/sites/x",
                    "http://graph.microsoft.com/v1.0/users",
                    "https://graph.microsoft.com:8443/v1.0/users",
                    "https://user:pw@graph.microsoft.com/v1.0/users",
                    "https://graph.microsoft.com/v1.0/users#frag",
                    "https://graph.microsoft.com/v1.0/users\n"):
            with self.assertRaises(ReadOnlyGuardError, msg=url):
                transport.request("GET", url, {})
        self.assertEqual(sender.calls, [])

    def test_action_like_and_out_of_scope_paths_refused(self):
        transport, sender = self.transport()
        graph = "https://graph.microsoft.com"
        bc = ("https://api.businesscentral.dynamics.com/v2.0/t/Production/api/v2.0/"
              f"companies({COMPANY})/purchaseOrders({PO_ID})")
        for url in (f"{graph}/v1.0/users/u/sendMail",
                    f"{graph}/v1.0/users/u/messages/m/microsoft.graph.send",
                    f"{graph}/v1.0/users/u/messages/m/reply",
                    f"{graph}/v1.0/users/u/messages/m/createForward",
                    f"{graph}/v1.0/users/u/messages/m/move",
                    f"{graph}/v1.0/users/u/messages/m/%73end",
                    f"{graph}/v1.0/users/u/messages/m%2Fsend",
                    f"{graph}/v1.0/users/../teams/t/channels",
                    f"{graph}/v1.0/users/%2E%2E/me/messages",
                    f"{graph}/v1.0/users/x\\..\\..\\groups",
                    f"{graph}/v1.0/users/x%5C..%5C..%5Cgroups",
                    f"{graph}/v1.0/drives/d/items/i/createUploadSession",
                    f"{graph}/v1.0/drives/d/items/i/copy",
                    f"{graph}/v1.0/drives/d/items/i/permanentDelete",
                    f"{graph}/v1.0/drives/d/root:/Folder:/createUploadSession",
                    f"{bc}/Microsoft.NAV.post",
                    f"{bc}/Microsoft.NAV.receiveAndInvoice",
                    f"{graph}/v1.0/teams/t/channels/c/messages",
                    f"{graph}/v1.0/me/messages",
                    f"{graph}/beta/users",
                    f"{graph}/v1.0/$batch",
                    f"{graph}/v1.0/subscriptions",
                    "https://login.microsoftonline.com/x/oauth2/v2.0/token"):
            with self.assertRaises(ReadOnlyGuardError, msg=url):
                transport.request("GET", url, {})
        self.assertEqual(sender.calls, [])

    def test_folder_names_inside_path_addressing_are_data(self):
        self.assertEqual(check_request("GET", "https://graph.microsoft.com/v1.0/drives/d/"
                                       "root:/Customers/Move:", environ={}),
                         "graph.microsoft.com")

    def test_teams_urls_never_called(self):
        target = f"https://graph.microsoft.com/v1.0/users/teams-bot@petralabx.com/messages"
        environ = {"MC_GO_LIVE_TEAMS_WEBHOOK_URL": target}
        transport, sender = self.transport(environ)
        with self.assertRaises(ReadOnlyGuardError):
            transport.request("GET", target + "?$top=1", {})
        self.assertEqual(sender.calls, [])

    def test_only_exact_token_post_is_permitted(self):
        transport, sender = self.transport()
        url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
        good = {"grant_type": "client_credentials", "scope": GRAPH_SCOPE,
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        for bad_url, form in ((url, {**good, "scope": "https://outlook.office.com/.default"}),
                              (url, {**good, "grant_type": "password"}),
                              (url, {**good, "username": "x"}),
                              (url, None),
                              (url + "?x=1", good),
                              (f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/authorize",
                               good),
                              (f"https://graph.microsoft.com/v1.0/users", good)):
            with self.assertRaises(ReadOnlyGuardError):
                transport.request("POST", bad_url, {}, form=form)
        self.assertEqual(sender.calls, [])
        status, _h, _b = transport.request("POST", url, {}, form=good)
        self.assertEqual(status, 200)
        self.assertEqual(sender.calls[0][0], "POST")

    def test_redirects_refused(self):
        sender = FakeSender({"/v1.0/users": (302, {"location": "https://evil.example.com/"}, b"")})
        transport = GuardedTransport(sender, environ={})
        with self.assertRaises(ReadOnlyGuardError):
            transport.request("GET", "https://graph.microsoft.com/v1.0/users", {})
        handler = _RefuseRedirect()
        with self.assertRaises(ReadOnlyGuardError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/x")
        with self.assertRaises(ReadOnlyGuardError):
            handler.redirect_request(None, None, 302, "Found", {},
                                     "https://graph.microsoft.com/v1.0/users/x")

    def test_bounds(self):
        sender = FakeSender({"/v1.0/users": (200, {}, b"x" * 101)})
        transport = GuardedTransport(sender, environ={}, timeout_seconds=12)
        with self.assertRaises(SourceUnavailable):
            transport.request("GET", "https://graph.microsoft.com/v1.0/users", {}, max_bytes=100)
        self.assertEqual(sender.timeouts, [12.0])
        with self.assertRaises(ValueError):
            GuardedTransport(sender, timeout_seconds=600)
        with self.assertRaises(ValueError):
            transport.request("GET", "https://graph.microsoft.com/v1.0/users", {},
                              max_bytes=10**9)

    def test_secrets_never_render(self):
        secret = Secret(CLIENT_SECRET)
        self.assertNotIn(CLIENT_SECRET, repr(secret) + str(secret))
        with self.assertRaises(TypeError):
            pickle.dumps(secret)
        creds = ClientCredentials.from_env("BC", ENV)
        self.assertNotIn(CLIENT_SECRET, repr(creds))

        def exploding(method, url, headers, body, timeout, max_bytes):
            raise RuntimeError(f"boom {headers.get('Authorization')} {body!r}")
        transport = GuardedTransport(exploding, environ={})
        tokens = TokenProvider(creds, BC_SCOPE, transport)
        with self.assertRaises(SourceUnavailable) as caught:
            tokens.bearer()
        rendered = str(caught.exception) + repr(caught.exception)
        self.assertNotIn(CLIENT_SECRET, rendered)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(CLIENT_SECRET, repr(tokens) + repr(transport))

    def test_token_refusal_and_missing_env_do_not_echo_values(self):
        def refused(method, url, headers, body, timeout, max_bytes):
            return 401, {}, json.dumps({"error_description": CLIENT_SECRET}).encode()
        transport = GuardedTransport(refused, environ={})
        tokens = TokenProvider(ClientCredentials.from_env("BC", ENV), BC_SCOPE, transport)
        with self.assertRaises(SourceUnavailable) as caught:
            tokens.bearer()
        self.assertEqual(str(caught.exception), "token request refused: HTTP 401")
        with self.assertRaises(SourceUnavailable) as missing:
            ClientCredentials.from_env("BC", {"BC_CLIENT_SECRET": CLIENT_SECRET})
        self.assertNotIn(CLIENT_SECRET, str(missing.exception))
        self.assertIn("BC_TENANT_ID", str(missing.exception))

    def test_token_is_cached_until_near_expiry(self):
        now = [1000.0]
        sender = FakeSender({"/v1.0/users": j(200, {"value": []})})
        transport = GuardedTransport(sender, environ={})
        tokens = TokenProvider(ClientCredentials.from_env("MICROSOFT_GRAPH", ENV), GRAPH_SCOPE,
                               transport, clock=lambda: now[0])
        self.assertEqual(tokens.bearer(), ACCESS_TOKEN)
        tokens.bearer()
        now[0] += 3500
        tokens.bearer()
        posts = [c for c in sender.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 2)
        form = parse_qs(posts[0][3].decode())
        self.assertEqual(form["scope"], [GRAPH_SCOPE])
        self.assertEqual(form["grant_type"], ["client_credentials"])


class MailTests(unittest.TestCase):
    def setUp(self):
        self.pages = {
            MSG_PATH: lambda url: (j(200, {"value": [message("m1", "ck1")],
                "@odata.nextLink": f"https://graph.microsoft.com{MSG_PATH}?$skip=1"})
                if "$skip" not in url else j(200, {"value": [message("m2", "ck2")]})),
            f"{MSG_PATH}/m1": j(200, message("m1", "ck1")),
            f"{CEO_MSG_PATH}": j(200, {"value": [message("c1", "cc1", "Board pay")]}),
            f"{CEO_MSG_PATH}/c1": j(200, message("c1", "cc1", "Board pay")),
        }
        self.sender = FakeSender(self.pages)
        self.api, self.transport = graph_api(self.sender)

    def test_no_grant_in_family_means_no_io(self):
        reader = GraphMailReader(self.api, audience())
        stranger = Actor("plx", frozenset({"staff:other"}), "restricted")
        for call in (lambda: reader.list_messages(stranger, MAILBOX),
                     lambda: reader.get_message(stranger, MAILBOX, "m1"),
                     lambda: reader.list_messages(Actor("other", frozenset({"staff:vince"}),
                                                        "restricted"), MAILBOX),
                     lambda: reader.list_messages(ACTOR, "not a mailbox")):
            with self.assertRaises(NotVisible):
                call()
        self.assertEqual(self.sender.calls, [])

    def test_denied_mailbox_reads_only_directory_metadata(self):
        reader = GraphMailReader(self.api, audience())
        for call in (lambda: reader.list_messages(ACTOR, "ceo@petralabx.com"),
                     lambda: reader.list_messages(Actor("plx", frozenset({"staff:vince"}),
                                                        "internal"), MAILBOX)):
            with self.assertRaises(NotVisible):
                call()
        self.assertTrue(self.sender.api_calls())
        for _m, url, _h, _b in self.sender.api_calls():
            self.assertNotIn("/messages", url)
            self.assertEqual(parse_qs(urlsplit(url).query)["$select"],
                             ["id,userPrincipalName,mail,proxyAddresses"])

    def test_listing_records_citations_completeness_and_immutable_ids(self):
        reader = GraphMailReader(self.api, audience())
        full = reader.list_messages(ACTOR, MAILBOX, page_size=1, max_pages=3)
        self.assertEqual([r["id"] for r in full["items"]], ["m1", "m2"])
        self.assertEqual(full["completeness"]["complete"], True)
        self.assertEqual(full["completeness"]["truncated"], False)
        self.assertEqual(full["completeness"]["pages_read"], 2)
        record = full["items"][0]
        self.assertEqual(record["citation"], f"graph-mail://{VINCE_ID}/m1@ck1")
        self.assertEqual(record["source_acl"], {"status": "derived", "kind": "mailbox_owner",
                                                "identities": [VINCE_ID, MAILBOX]})
        self.assertEqual(record["mailbox"], VINCE_ID)
        self.assertEqual(record["mailbox_upn"], MAILBOX)
        self.assertEqual(record["from"], "supplier@example.com")
        self.assertEqual(len(record["content_sha256"]), 64)
        message_calls = [c for c in self.sender.api_calls() if "/messages" in c[1]]
        self.assertTrue(all(f"/users/{VINCE_ID}/messages" in c[1] for c in message_calls))
        for _m, _u, headers, _b in message_calls:
            self.assertIn('IdType="ImmutableId"', headers["Prefer"])
            self.assertEqual(headers["Authorization"], "Bearer " + ACCESS_TOKEN)
        capped = reader.list_messages(ACTOR, MAILBOX, page_size=1, max_pages=1)
        self.assertEqual(capped["completeness"]["truncated"], True)
        self.assertEqual(capped["completeness"]["truncation_reason"], "page_cap")
        self.assertFalse(capped["completeness"]["complete"])
        item_capped = reader.list_messages(ACTOR, MAILBOX, max_items=1)
        self.assertEqual(item_capped["completeness"]["truncation_reason"], "item_cap")

    def test_every_alias_yields_the_same_canonical_mailbox_and_citation(self):
        reader = GraphMailReader(self.api, audience())
        handles = {reader.get_message(ACTOR, alias, "m1")["citation"]
                   for alias in (MAILBOX, "Vince@PetraLabX.com", VINCE_ID, VINCE_ID.upper())}
        self.assertEqual(handles, {f"graph-mail://{VINCE_ID}/m1@ck1"})

    def test_resolve_citation_reports_current_and_changed(self):
        reader = GraphMailReader(self.api, audience())
        handle = reader.get_message(ACTOR, MAILBOX, "m1")["citation"]
        self.assertEqual(reader.resolve_citation(handle, ACTOR)["status"], "current")
        self.pages[f"{MSG_PATH}/m1"] = j(200, message("m1", "ck9"))
        resolved = reader.resolve_citation(handle, ACTOR)
        self.assertEqual(resolved["status"], "changed_since")
        self.assertEqual(resolved["current_citation"], f"graph-mail://{VINCE_ID}/m1@ck9")
        with self.assertRaises(NotVisible):
            reader.resolve_citation(handle, Actor("plx", frozenset({"staff:other"}), "restricted"))
        with self.assertRaises(ValueError):
            reader.resolve_citation("api://public.x/%7B%7D@1", ACTOR)

    def test_missing_message_or_mailbox_reads_as_not_visible(self):
        reader = GraphMailReader(self.api, audience())
        with self.assertRaises(NotVisible):
            reader.get_message(ACTOR, MAILBOX, "nope")
        with self.assertRaises(NotVisible):
            reader.list_messages(Actor("plx", frozenset({"staff:vince"}), "restricted"),
                                 "ghost@petralabx.com")

    def test_directory_answer_for_another_user_is_refused(self):
        self.sender.directory["vince@petralabx.com"] = {"id": CEO_ID,
                                                        "userPrincipalName": "ceo@petralabx.com"}
        with self.assertRaises(NotVisible):
            GraphMailReader(self.api, audience()).list_messages(ACTOR, MAILBOX)

    def test_source_acl_required_enforces_mailbox_owner(self):
        gated = audience(identities={"staff:vince": [MAILBOX]},
                         source_acl_required=["mail:*"])
        reader = GraphMailReader(self.api, gated)
        self.assertEqual(len(reader.list_messages(ACTOR, MAILBOX)["items"]), 2)
        # The owner ACL is known from the directory, so a non-owner is refused
        # before any message request (listing or single read).
        before = len(self.sender.calls)
        with self.assertRaises(NotVisible):
            reader.list_messages(OTHER, MAILBOX)
        with self.assertRaises(NotVisible):
            reader.get_message(OTHER, MAILBOX, "m1")
        self.assertFalse(any("/messages" in c[1] for c in self.sender.calls[before:]))
        # A declared identity in any address form of the set matches.
        by_id = GraphMailReader(self.api, audience(identities={"staff:vince": [VINCE_ID]},
                                                   source_acl_required=["mail:*"]))
        self.assertEqual(len(by_id.list_messages(ACTOR, MAILBOX)["items"]), 2)

    def test_body_text_is_bounded(self):
        body = dict(message("m1", "ck1"), body={"contentType": "text", "content": "x" * 50})
        self.pages[f"{MSG_PATH}/m1"] = j(200, body)
        record = GraphMailReader(self.api, audience()).get_message(
            ACTOR, MAILBOX, "m1", include_body=True, max_body_chars=10)
        self.assertEqual(record["body_text"], "x" * 10)
        self.assertTrue(record["body_truncated"])

    def test_next_link_outside_collection_refused(self):
        self.pages[MSG_PATH] = j(200, {"value": [message("m1", "ck1")],
            "@odata.nextLink": f"https://graph.microsoft.com{CEO_MSG_PATH}"})
        with self.assertRaises(SourceUnavailable):
            GraphMailReader(self.api, audience()).list_messages(ACTOR, MAILBOX)


def drive_item(item_id: str, name: str, folder: str = "/Customers", *, etag: str = "\"{E1},1\"",
               content: bytes | None = b"%PDF-1.7 original", is_folder: bool = False,
               parent_id: str | None = None) -> dict:
    item = {"id": item_id, "name": name, "eTag": etag, "cTag": "\"c:{E1},1\"",
            "webUrl": f"https://{SP_HOST}/sites/PLXCUSTOMERS/Shared%20Documents{folder}/{name}",
            "lastModifiedDateTime": "2026-09-29T12:00:00Z",
            "parentReference": {"driveId": DRIVE, "path": f"/drives/{DRIVE}/root:{folder}"},
            "lastModifiedBy": {"user": {"email": "ricardo@petralabx.com"}}}
    if parent_id:
        item["parentReference"]["id"] = parent_id
    if is_folder:
        item["folder"] = {"childCount": 2}
    else:
        item["size"] = len(content or b"")
        item["file"] = {"mimeType": "application/pdf",
                        "hashes": {"sha256Hash": sha256(content or b"").hexdigest().upper()}}
    return item


DRIVE_BASE = f"/v1.0/drives/{DRIVE}"
CUSTOMERS = drive_item("FOLDER-CUST", "Customers", folder="", is_folder=True)


class SharePointTests(unittest.TestCase):
    def setUp(self):
        self.content = b"%PDF-1.7 original"
        self.file = drive_item("F1", "AO-26005B.pdf", content=self.content,
                               parent_id="FOLDER-CUST")
        self.download = f"https://{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx?{TEMPAUTH}"
        perms = {"value": [{"roles": ["read"], "grantedToV2": {"user": {
            "email": "Vince@petralabx.com", "id": USER_ID}}},
            {"roles": ["write"], "grantedToV2": {"group": {"id": "G-OPS"}},
             "inheritedFrom": {"path": "/"}}]}
        self.routes = {
            f"{DRIVE_BASE}/root:/Customers:": j(200, CUSTOMERS),
            f"{DRIVE_BASE}/items/FOLDER-CUST/children": lambda url: j(200, {"value": [
                self.file, drive_item("D1", "Acme", is_folder=True, parent_id="FOLDER-CUST"),
                drive_item("S1", "Secret-pricing.xlsx", parent_id="FOLDER-CUST")]}),
            f"{DRIVE_BASE}/items/F1": lambda url: live_drive_item(self.file, url,
                                                                  self.download),
            f"{DRIVE_BASE}/items/F1/permissions": j(200, perms),
            f"{DRIVE_BASE}/items/D1/permissions": j(200, perms),
            f"{DRIVE_BASE}/items/S1/permissions": j(200, perms),
            f"{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx": (200, {}, self.content),
        }
        self.sender = FakeSender(self.routes)
        self.api, self.transport = graph_api(self.sender)

    def reader(self, aud=None, **kwargs) -> SharePointReader:
        return SharePointReader(self.api, aud or audience(), DRIVE, transport=self.transport,
                                **kwargs)

    def test_no_grant_on_drive_means_no_io(self):
        reader = self.reader()
        nobody = Actor("plx", frozenset({"staff:x"}), "restricted")
        for call in (lambda: reader.list_folder(nobody, "/Customers"),
                     lambda: reader.get_item(nobody, "F1"),
                     lambda: reader.get_content(nobody, "F1"),
                     lambda: reader.resolve_citation(f"sharepoint://{DRIVE}/F1@x", nobody)):
            with self.assertRaises(NotVisible):
                call()
        self.assertEqual(self.sender.calls, [])

    def test_folder_audience_uses_resolved_folder(self):
        reader = self.reader()
        with self.assertRaises(NotVisible):
            reader.list_folder(ACTOR, "/Documents")  # not found → not_visible
        with self.assertRaises(NotVisible):
            reader.list_folder(ACTOR, "/")
        self.assertFalse(any("/children" in c[1] for c in self.sender.calls))

    def test_listing_records_acl_citation_and_withheld(self):
        listing = self.reader(read_permissions=True).list_folder(ACTOR, "/Customers")
        by_id = {r["id"]: r for r in listing["items"]}
        self.assertEqual(sorted(by_id), ["D1", "F1"])
        self.assertEqual(listing["path"], "/Customers")
        self.assertEqual(listing["completeness"]["withheld_count"], 1)
        self.assertTrue(listing["completeness"]["complete"])
        record = by_id["F1"]
        self.assertEqual(record["path"], "/Customers/AO-26005B.pdf")
        self.assertEqual(record["kind"], "file")
        self.assertEqual(by_id["D1"]["kind"], "folder")
        self.assertEqual(record["source_acl"]["status"], "read")
        self.assertIn("vince@petralabx.com", record["source_acl"]["identities"])
        self.assertIn("group:g-ops", record["source_acl"]["identities"])
        self.assertEqual(record["citation"],
                         "sharepoint://b%21drive-AbC123/F1@%22%7BE1%7D%2C1%22")
        # The withheld item's permissions were never requested.
        self.assertFalse(any("/items/S1/" in c[1] for c in self.sender.calls))

    def test_permissions_not_read_unless_required_or_requested(self):
        listing = self.reader().list_folder(ACTOR, "/Customers")
        self.assertEqual(listing["completeness"]["acl_reads"], 0)
        self.assertFalse(any("/permissions" in c[1] for c in self.sender.calls))

    def test_acl_reads_are_bounded_per_listing(self):
        gated = audience(identities={"staff:vince": ["vince@petralabx.com"]},
                         source_acl_required=[f"sharepoint:{DRIVE}:*"])
        listing = self.reader(gated, max_acl_reads=1).list_folder(ACTOR, "/Customers")
        self.assertEqual(listing["completeness"]["acl_reads"], 1)
        self.assertTrue(listing["completeness"]["acl_reads_capped"])
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["completeness"]["withheld_count"], 2)

    def test_source_acl_required_matches_declared_identities(self):
        gated = audience(identities={"staff:vince": ["vince@petralabx.com"],
                                     "staff:ops": ["group:g-other"]},
                         source_acl_required=[f"sharepoint:{DRIVE}:*"])
        reader = self.reader(gated)
        self.assertEqual(len(reader.list_folder(ACTOR, "/Customers")["items"]), 2)
        hidden = reader.list_folder(OTHER, "/Customers")
        self.assertEqual(hidden["items"], [])
        self.assertEqual(hidden["completeness"]["withheld_count"], 3)
        with self.assertRaises(NotVisible):
            reader.get_item(OTHER, "F1")
        # Unreadable permissions deny where the source ACL is required.
        self.routes[f"{DRIVE_BASE}/items/F1/permissions"] = j(403, {})
        with self.assertRaises(NotVisible):
            reader.get_item(ACTOR, "F1")

    def test_content_read_is_credential_free_bounded_and_hash_cited(self):
        reader = self.reader()
        read = reader.get_content(ACTOR, "F1")
        self.assertEqual(read["content"], self.content)
        digest = sha256(self.content).hexdigest()
        self.assertEqual(read["content_sha256"], digest)
        self.assertTrue(read["record"]["citation"].endswith("#sha256=" + digest))
        downloads = [c for c in self.sender.calls if SP_HOST in urlsplit(c[1]).netloc]
        self.assertEqual(len(downloads), 1)
        self.assertNotIn("Authorization", downloads[0][2])
        self.assertNotIn(TEMPAUTH, json.dumps(read["record"]))
        # The first metadata read never asks for the download URL.
        item_reads = [c[1] for c in self.sender.api_calls() if "/items/F1?" in c[1]]
        self.assertNotIn("downloadUrl", item_reads[0])
        resolved = reader.resolve_citation(read["record"]["citation"], ACTOR, verify_content=True)
        self.assertEqual(resolved["status"], "current")
        self.assertTrue(resolved["content_verified"])
        self.assertEqual(resolved["current_citation"], read["record"]["citation"])

    def test_a_named_version_reads_through_its_own_content_route_only(self):
        old = b"%PDF-1.7 revision 3"
        self.routes[f"{DRIVE_BASE}/items/F1/versions/3.0/content"] = (
            302, {"Location": self.download}, b"")
        self.routes[f"{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx"] = (200, {}, old)
        read = self.reader().get_version_content(ACTOR, "F1", "3.0")
        self.assertEqual((read["content"], read["content_sha256"], read["version_id"]),
                         (old, sha256(old).hexdigest(), "3.0"))
        downloads = [c for c in self.sender.calls if SP_HOST in urlsplit(c[1]).netloc]
        self.assertNotIn("Authorization", downloads[0][2])
        # visibility first: a reader the item is hidden from sends no version GET
        sent = len(self.sender.calls)
        with self.assertRaises(NotVisible):
            self.reader().get_version_content(Actor("plx", frozenset({"staff:x"}), "restricted"), "F1", "3.0")
        self.assertFalse(any("/versions/" in c[1] for c in self.sender.calls[sent:]))
        # only a version id parses: no listing, restore or other tail
        from integrations import live_source_grammar as g
        for bad in ("3.0/restoreVersion", "current", "3", "../F2"):
            with self.assertRaises(ValueError):
                g.url("graph.drive_item_version_content", drive=DRIVE, item="F1", version=bad)
        for path in (f"{DRIVE_BASE}/items/F1/versions", f"{DRIVE_BASE}/items/F1/versions/3.0",
                     f"{DRIVE_BASE}/items/F1/versions/3.0/restoreVersion"):
            self.assertNotEqual(g.match("graph.microsoft.com", path),
                                "graph.drive_item_version_content")
        with self.assertRaises(ReadOnlyGuardError):
            self.api.get_content(f"https://graph.microsoft.com{DRIVE_BASE}/items/F1/versions",
                                 max_bytes=10)

    def test_download_url_is_selected_as_content_property(self):
        """Live audit 2026-10-01: ``$select=...,@microsoft.graph.downloadUrl``
        is silently ignored by Graph, so every content read failed with
        "drive item lacks a download URL". The URL is selected through
        ``content.downloadUrl`` and only after the visibility decision."""
        read = self.reader().get_content(ACTOR, "F1")
        self.assertEqual(read["content_status"], "read")
        item_reads = [c[1] for c in self.sender.api_calls() if "/items/F1?" in c[1]]
        selects = [parse_qs(urlsplit(u).query)["$select"][0].split(",") for u in item_reads]
        self.assertEqual(len(selects), 3)  # open, download URL, eTag re-read
        self.assertNotIn("content.downloadUrl", selects[0])
        self.assertIn("content.downloadUrl", selects[1])
        self.assertNotIn("content.downloadUrl", selects[2])
        self.assertFalse(any(t.startswith("@") for terms in selects for t in terms))
        # The permission/visibility reads precede the download-URL request.
        order = [c[1] for c in self.sender.api_calls()]
        self.assertLess(order.index(item_reads[0]), order.index(item_reads[1]))

    def test_annotation_only_select_would_yield_no_download_url(self):
        """The live behaviour the fake reproduces: the annotation term alone
        returns the item without the URL; content.downloadUrl returns it."""
        url = f"https://graph.microsoft.com{DRIVE_BASE}/items/F1?$select=id,@microsoft.graph.downloadUrl"
        self.assertNotIn("@microsoft.graph.downloadUrl",
                         json.loads(live_drive_item(self.file, url, self.download)[2]))
        url = f"https://graph.microsoft.com{DRIVE_BASE}/items/F1?$select=id,content.downloadUrl"
        self.assertEqual(json.loads(live_drive_item(self.file, url, self.download)[2])
                         ["@microsoft.graph.downloadUrl"], self.download)
        from integrations import live_source_grammar as g
        with self.assertRaises(ValueError):
            g.drive_item_select("id,@microsoft.graph.downloadUrl", download_url=False)
        self.assertEqual(g.drive_item_select("id,eTag", download_url=True),
                         "id,eTag,content.downloadUrl")

    def test_download_secret_registry_stays_bounded(self):
        reader = self.reader()
        for _ in range(GuardedTransport.MAX_SECRETS + 5):
            reader.get_content(ACTOR, "F1")
        self.assertLessEqual(len(self.transport._secrets), 2)
        with self.assertRaises(RuntimeError):
            for _ in range(GuardedTransport.MAX_SECRETS + 1):
                self.transport.register_secret(Secret("x" * 8))

    def test_content_cap_skips_download(self):
        read = self.reader().get_content(ACTOR, "F1", max_bytes=5)
        self.assertEqual(read["content_status"], "exceeds_cap")
        self.assertIsNone(read["content"])
        self.assertFalse(any(SP_HOST in urlsplit(c[1]).netloc for c in self.sender.calls))
        self.assertFalse(any("downloadUrl" in c[1] for c in self.sender.calls))

    def test_download_host_must_be_the_configured_site_host(self):
        self.download = f"https://contoso.sharepoint.com/x?{TEMPAUTH}"
        with self.assertRaises(ReadOnlyGuardError) as caught:
            self.reader().get_content(ACTOR, "F1")
        self.assertNotIn(TEMPAUTH, str(caught.exception))
        self.assertFalse(any("contoso" in c[1] for c in self.sender.calls))
        unpinned = GuardedTransport(FakeSender(), environ={})
        with self.assertRaises(ReadOnlyGuardError):
            unpinned.download_preauthenticated(f"https://{SP_HOST}/x", max_bytes=10)
        with self.assertRaises(ValueError):
            GuardedTransport(FakeSender(), environ={}, sharepoint_host="evil.example.com")

    def test_changed_during_read_and_changed_citation(self):
        reader = self.reader()
        record = reader.get_item(ACTOR, "F1")
        calls = {"n": 0}
        original = self.routes[f"{DRIVE_BASE}/items/F1"]

        def moving(url):
            calls["n"] += 1
            if calls["n"] > 1:
                self.file = drive_item("F1", "AO-26005B.pdf", etag="\"{E1},2\"",
                                       content=self.content)
            return original(url)
        self.routes[f"{DRIVE_BASE}/items/F1"] = moving
        with self.assertRaises(SourceUnavailable):
            reader.get_content(ACTOR, "F1")
        resolved = reader.resolve_citation(record["citation"], ACTOR)
        self.assertEqual(resolved["status"], "changed_since")
        self.assertIn("%7BE1%7D%2C2", resolved["current_citation"])

    def test_content_hash_mismatch_refused(self):
        self.routes[f"{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx"] = (
            200, {}, b"%PDF-1.7 tampered")
        with self.assertRaises(SourceUnavailable):
            self.reader().get_content(ACTOR, "F1")
        # Business drives often publish only quickXorHash: same-size bytes that differ
        # are refused by it, and the bytes it names are read.
        from integrations.graph_read import quick_xor_hash
        self.assertEqual(quick_xor_hash(b"hello world"),       # libqxh's published vector
                         "aCgDG9jwBhDc4Q1yawMZAAAAAAA=")
        self.file["file"]["hashes"] = {"quickXorHash": quick_xor_hash(self.content)}
        self.routes[f"{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx"] = (
            200, {}, b"%PDF-1.7 0riginal")
        with self.assertRaises(SourceUnavailable):
            self.reader().get_content(ACTOR, "F1")
        self.routes[f"{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx"] = (
            200, {}, self.content)
        self.assertEqual(self.reader().get_content(ACTOR, "F1")["content"], self.content)

    def test_malformed_source_path_reads_as_not_visible(self):
        self.file["parentReference"]["path"] = "garbage-without-root"
        with self.assertRaises(NotVisible):
            self.reader().get_item(ACTOR, "F1")
        self.file["parentReference"]["path"] = f"/drives/{DRIVE}/root:/Customers/../Finance"
        with self.assertRaises(NotVisible):
            self.reader().get_item(ACTOR, "F1")


class CanonicalIdentityMatrixTests(unittest.TestCase):
    """Regression family: the audience is judged on the server-resolved identity.

    Every alias of a denied mailbox and every path variant of a denied folder
    is refused consistently, with no message, children, permissions or
    download-URL request before the decision (asserted on the transport log).
    """

    MAIL_AUDIENCE = {"mail:*": ["staff:vince"], "mail:ceo@petralabx.com": []}
    SP_AUDIENCE = {f"sharepoint:{DRIVE}:/Finance/*": ["staff:vince"],
                   f"sharepoint:{DRIVE}:/Finance/Payroll/*": []}

    def setUp(self):
        payroll = drive_item("FOLDER-PAY", "Payroll", folder="/Finance", is_folder=True)
        self.salary = drive_item("PAY1", "salaries.xlsx", folder="/Finance/Payroll",
                                 parent_id="FOLDER-PAY")
        self.routes = {
            MSG_PATH: j(200, {"value": [message("m1", "ck1"), message("m2", "ck2")]}),
            CEO_MSG_PATH: j(200, {"value": [message("c1", "cc1", "Board pay")]}),
            f"{CEO_MSG_PATH}/c1": j(200, message("c1", "cc1", "Board pay")),
            f"{DRIVE_BASE}/root:/Finance/Payroll:": j(200, payroll),
            # Server-side normalisation of variants that reach Graph.
            f"{DRIVE_BASE}/root:/finance/PAYROLL:": j(200, payroll),
            f"{DRIVE_BASE}/root:/FINANCE/payroll:": j(200, payroll),
            f"{DRIVE_BASE}/items/FOLDER-PAY": j(200, payroll),
            f"{DRIVE_BASE}/items/FOLDER-PAY/children": j(200, {"value": [self.salary]}),
            f"{DRIVE_BASE}/items/PAY1": lambda url: live_drive_item(
                self.salary, url, f"https://{SP_HOST}/dl?{TEMPAUTH}"),
            f"{DRIVE_BASE}/items/PAY1/permissions": j(200, {"value": []}),
            f"{SP_HOST}/dl": (200, {}, b"%PDF-1.7 original"),
        }
        self.sender = FakeSender(self.routes)
        self.api, self.transport = graph_api(self.sender)
        self.mail = GraphMailReader(self.api, LiveSourceAudience(self.MAIL_AUDIENCE, tenant="plx"))
        self.drive = SharePointReader(self.api, LiveSourceAudience(self.SP_AUDIENCE, tenant="plx"),
                                      DRIVE, transport=self.transport, read_permissions=True)

    def assert_no_content_requests(self):
        for _m, url, _h, _b in self.sender.api_calls():
            path = unquote(urlsplit(url).path)
            for forbidden in ("/messages", "/children", "/permissions", "downloadUrl"):
                self.assertNotIn(forbidden, path + urlsplit(url).query, url)
        self.assertFalse(any(urlsplit(c[1]).hostname == SP_HOST for c in self.sender.calls))

    def test_mailbox_aliases_denied_consistently(self):
        aliases = ("ceo@petralabx.com", "CEO@PetraLabX.com", "Ceo@petralabx.COM",
                   CEO_UPN, CEO_UPN.upper(), CEO_ID, CEO_ID.upper())
        for alias in aliases:
            for call in (lambda: self.mail.list_messages(ACTOR, alias),
                         lambda: self.mail.get_message(ACTOR, alias, "c1"),
                         lambda: self.mail.resolve_citation(
                             f"graph-mail://{quote(alias, safe='')}/c1@cc1", ACTOR)):
                with self.assertRaises(NotVisible, msg=alias):
                    call()
        self.assert_no_content_requests()
        # The granted family still works through the same entry point.
        self.assertEqual(len(self.mail.list_messages(ACTOR, VINCE_ID.upper())["items"]), 2)

    def test_folder_path_variants_denied_consistently(self):
        refused_before_io = ("/Finance/Payroll ", "/Finance/Payroll.", "/Finance/Payroll\\",
                             "/Finance//Payroll", "/Finance/./Payroll", "/Finance/x/../Payroll",
                             " /Finance/Payroll", "/Finance/Payroll\t", "/Finance/Payroll%20",
                             "/Finance/Payroll%2F..", "/Finance/Payroll:", "/Finance/Payroll\u00a0",
                             "/Finance/..Payroll")
        for variant in refused_before_io:
            with self.assertRaises(NotVisible, msg=repr(variant)):
                self.drive.list_folder(ACTOR, variant)
        self.assertEqual(self.sender.calls, [])
        server_resolved = ("/Finance/Payroll", "/Finance/Payroll/", "finance/PAYROLL",
                           "/FINANCE/payroll")
        for variant in server_resolved:
            with self.assertRaises(NotVisible, msg=repr(variant)):
                self.drive.list_folder(ACTOR, variant)
        self.assert_no_content_requests()

    def test_item_reads_in_denied_folder_never_fetch_content(self):
        for call in (lambda: self.drive.get_item(ACTOR, "PAY1"),
                     lambda: self.drive.get_content(ACTOR, "PAY1"),
                     lambda: self.drive.resolve_citation(
                         f"sharepoint://{quote(DRIVE, safe='')}/PAY1@x#sha256={'0' * 64}",
                         ACTOR, verify_content=True)):
            with self.assertRaises(NotVisible):
                call()
        self.assert_no_content_requests()
        for _m, url, _h, _b in self.sender.api_calls():
            self.assertNotIn("downloadUrl", url)


class BusinessCentralTests(unittest.TestCase):
    BASE = f"/v2.0/{TENANT_ID}/Production/api/v2.0"

    def setUp(self):
        self.po = {"@odata.etag": "W/\"JzE5OzEyMzQ1Njc4OTA7MDA7Jw==\"", "id": PO_ID,
                   "number": "PO5447", "totalAmountIncludingTax": 1234.10, "lineCount": 3,
                   "lastModifiedDateTime": "2026-09-28T08:00:00Z"}
        self.routes = {
            f"{self.BASE}/companies": j(200, {"value": [
                {"id": COMPANY, "name": "PETRA", "@odata.etag": "W/\"1\""},
                {"id": "0f0e0d0c-0b0a-0908-0706-0504030201ff", "name": "TEST"},
                {"id": "0f0e0d0c-0b0a-0908-0706-0504030201fe", "name": "OTHER"}]}),
            f"{self.BASE}/companies({COMPANY})/purchaseOrders": lambda url: (200, {},
                json.dumps({"value": [self.po]}).replace("1234.1", "1234.10").encode()),
            f"{self.BASE}/companies({COMPANY})/purchaseOrders({PO_ID})": lambda url: (200, {},
                json.dumps(self.po).encode()),
        }
        self.sender = FakeSender(self.routes)
        self.api, self.transport, tenant, environment = bc_read.build_bc(ENV, sender=self.sender)
        self.reader = BusinessCentralReader(self.api, audience(), tenant_id=tenant,
                                            environment=environment)

    def test_audience_denies_before_io(self):
        with self.assertRaises(NotVisible):
            self.reader.companies(OTHER)
        with self.assertRaises(NotVisible):
            self.reader.list_entities(OTHER, COMPANY, "purchaseOrders")
        with self.assertRaises(ValueError):
            self.reader.list_entities(ACTOR, COMPANY, "purchaseOrders/Microsoft.NAV.post")
        self.assertEqual(self.sender.calls, [])

    def test_companies_and_entities_with_exact_decimals_and_citations(self):
        self.assertEqual(len(self.reader.companies(ACTOR)["items"]), 3)
        listing = self.reader.list_entities(ACTOR, COMPANY, "purchaseOrders", page_size=1,
                                            equals=("number", "PO5447"))
        record = listing["items"][0]
        self.assertEqual(record["fields"]["totalAmountIncludingTax"], "1234.10")
        self.assertEqual(record["fields"]["lineCount"], "3")  # integers exact text too
        self.assertEqual(record["source_acl"]["status"], "not_exposed")
        self.assertTrue(listing["completeness"]["complete"])
        self.assertTrue(record["citation"].startswith(f"bc://production/{COMPANY}/purchaseOrders/"
                                                      f"{PO_ID}@"))
        entity_calls = [c for c in self.sender.api_calls() if "purchaseOrders" in c[1]]
        self.assertEqual(entity_calls[0][2]["Prefer"], "odata.maxpagesize=1")
        self.assertEqual(parse_qs(urlsplit(entity_calls[0][1]).query)["$filter"],
                         ["number eq 'PO5447'"])
        self.assertTrue(all(c[0] == "GET" for c in self.sender.api_calls()))
        self.assertEqual(self.reader.resolve_citation(record["citation"], ACTOR)["status"],
                         "current")
        self.po["@odata.etag"] = "W/\"JzE5Ozk5OTk7MDA7Jw==\""
        changed = self.reader.resolve_citation(record["citation"], ACTOR)
        self.assertEqual(changed["status"], "changed_since")
        self.assertNotEqual(changed["current_citation"], record["citation"])

    def test_source_acl_required_withholds_bc_records(self):
        reader = BusinessCentralReader(self.api, audience(source_acl_required=["bc:*"]),
                                       tenant_id=TENANT_ID, environment="Production")
        listing = reader.list_entities(ACTOR, COMPANY, "purchaseOrders")
        self.assertEqual(listing["items"], [])
        self.assertEqual(listing["completeness"]["withheld_count"], 1)
        with self.assertRaises(NotVisible):
            reader.get_entity(ACTOR, COMPANY, "purchaseOrders", PO_ID)


class ClosedGrammarTests(unittest.TestCase):
    """Every template x every parameter x every bad-parameter class is refused,
    both when building the URL and when a raw URL carrying it reaches the guard."""

    VALID = {"user_ref": MAILBOX, "user_id": VINCE_ID, "mail_id": "AAMkAD-_=1",
             "mail_folder": "inbox", "drive_id": DRIVE, "item_id": "01ABC!x", "version_id": "195.0",
             "site_id": SITE_ID, "drive_path": "/Customer Documents/A.pdf",
             "tenant": TENANT_ID, "environment": "Production", "guid": COMPANY,
             "entity_set": "purchaseOrders", "sp_host": "petrasoap.sharepoint.com",
             "site_name": "AGENTS", "teams_thread": "19:a1B2-c_3@thread.tacv2",
             "teams_message_id": "1616965872395", "search_text": "PO#12345",
             "share_id": "u!aHR0cHM6Ly9wZXRyYWxhYnguc2hhcmVwb2ludC5jb20vOmI6L3MvUUMvRWFC",
             "freightcom_id": "fc-Test_1", "amazon_shipment_id": "FBA1TEST0001",
             "bc_company_name": "My Company"}
    # External operations whose closed query grammar requires parameters
    # (live_source_grammar.SP_QUERIES): a valid query, so only the path is under test.
    VALID_QUERY = {
        "inbound_shipments": {"MarketplaceId": "ATESTMKT0001", "QueryType": "SHIPMENT",
                              "ShipmentIdList": "FBA1TEST0001"},
        "inbound_shipment_items": {"MarketplaceId": "ATESTMKT0001"},
        "inbound_labels": {"MarketplaceId": "ATESTMKT0001", "PageType": "PackageLabel_Thermal",
                           "LabelType": "UNIQUE"},
        "inventory_summaries": {"granularityType": "Marketplace",
                                "granularityId": "ATESTMKT0001",
                                "marketplaceIds": "ATESTMKT0001"},
        "order_metrics": {"marketplaceIds": "ATESTMKT0001", "granularity": "Total",
                          "interval": "2026-09-19T00:00:00Z--2026-10-19T00:00:00Z"}}

    def valid_query(self, op: str) -> dict:
        if op == "bc.odata_purchase_orders":  # its closed grammar requires the fixed $select
            from integrations import live_source_grammar as g
            return g.bc_odata_purchase_order_query()
        return self.VALID_QUERY.get(op.rsplit(".", 1)[-1], {}) if op.startswith("sp_api.") \
            else {}
    GENERIC_BAD = ("", "/", "a/b", "..", "../../x", "%2e%2e", "a%b", "a%2Fb", "a\\b",
                   "a\nb", "a\x00b", "x" * 3000, " a", "a ")
    TYPE_BAD = {
        "user_ref": ("a..b@petralabx.com", "a@b", "a/b@petralabx.com", "me"),
        "user_id": ("not-a-guid", VINCE_ID + "x", "me"),
        "mail_id": ("a.b", "a+b", "inbox/../x", "a b"),
        "mail_folder": ("../../" + CEO_ID + "/messages", "inbox.", "a.b"),
        "drive_id": ("b!a.b", "a/b", "a%20b"),
        "item_id": ("a.b", "x/children", "a b"),
        "version_id": ("1", "1.0.0", "a.0", "1.0/restoreVersion", "current"),
        "site_id": ("evil.example.com,11111111-0000-0000-0000-000000000001,"
                    "11111111-0000-0000-0000-000000000002", "petralabx.sharepoint.com"),
        "drive_path": ("/a/../b", "/a/./b", "//a", "/a/", "/a ", "/a.", "/ a", "/a:b",
                       "/a%2Fb", "/a\\b", "/a..b", "/a\tb", "a"),
        "tenant": ("a..b.com", "a/b", "-a.com"),
        "environment": ("Prod/../x", "a.b", "a b"),
        "guid": ("{" + COMPANY + "}", COMPANY[:-1], "x"),
        "sp_host": ("evil.example.com", "petrasoap.sharepoint.com.evil.com",
                    "petrasoap.sharepoint.com:443", "a:b.sharepoint.com", "PETRASOAP"),
        "site_name": ("AGENTS:", "A.B", "-x", "a b", "A:/drive", "x" * 129),
        "entity_set": ("employees", "companies", "purchaseOrders/Microsoft.NAV.post",
                       "PurchaseOrders", "purchaseOrders(x)"),
        "search_text": ("a'b", "a)", "#12", "12#", "a..b", "a\"b", "x" * 65),
        "teams_thread": ("19:a@evil.example.com", "a@thread.v2", "19:a.b@thread.v2",
                         "19:a@thread.tacv2:x", "19:@thread.v2", "19:a@thread.v2/messages",
                         "19:a@thread.v2'"),
        "teams_message_id": ("1.2", "1 2", "1+2", "1:2"),
        # Not in the pinned map, doubled space, quote, parentheses.
        "bc_company_name": ("CRONUS Canada", "My  Company", "My Company'", "a(b)"),
        # s! token, raw id, padding, non-https or space in the decoded URL, '+' / '/'
        # (base64, not base64url), a non-canonical last character.
        "share_id": ("s!AbCd", "AbCd", "u!", "u!aHR0cHM6Ly94LmNvbQ==", "u!aHR0cDovL3guY29t",
                     "u!aHR0cHM6Ly94LmNvbS9hIGI", "u!aHR0cHM6Ly9+LmNvbQ", "u!aHR0cHM/Ly94",
                     "u!aHR0cHM6Ly94LmNvbR"),
        "freightcom_id": ("-a", "a.b", "a b", "a:b", "x" * 65),
        "amazon_shipment_id": ("fba1test0001", "FBA12", "XBA1TEST0001", "FBA1TEST-001",
                               "FBA1TEST0001ABCD"),
    }

    def raw_url(self, op: str, target: str, encoded: str) -> str:
        from integrations import live_source_grammar as g
        host, template = g.TEMPLATES[op]
        path = template
        for name, type_name in re.findall(r"\{([a-z]+):([a-z_]+)\}", template):
            value = encoded if name == target else g.TYPES[type_name].encode(
                self.VALID[type_name])
            path = path.replace("{" + f"{name}:{type_name}" + "}", value)
        query = self.valid_query(op)
        return f"https://{host}{path}" + (f"?{urlencode(query)}" if query else "")

    def test_every_valid_template_passes_the_guard(self):
        from integrations import live_source_grammar as g
        for op, (_host, template) in g.TEMPLATES.items():
            params = {name: self.VALID[t] for name, t in
                      re.findall(r"\{([a-z]+):([a-z_]+)\}", template)}
            url = g.url(op, self.valid_query(op), **params)
            check_request("GET", url, environ={})
            self.assertEqual(g.match(urlsplit(url).hostname, urlsplit(url).path), op)

    def test_every_bad_parameter_refused_at_build_and_guard(self):
        from integrations import live_source_grammar as g
        checked = 0
        for op, (_host, template) in g.TEMPLATES.items():
            fields = re.findall(r"\{([a-z]+):([a-z_]+)\}", template)
            for name, type_name in fields:
                for bad in self.GENERIC_BAD + self.TYPE_BAD[type_name]:
                    params = {n: self.VALID[t] for n, t in fields}
                    params[name] = bad
                    with self.assertRaises(ValueError, msg=(op, name, bad)):
                        g.url(op, self.valid_query(op), **params)
                    safe = "/" if type_name == "drive_path" else ""
                    for encoded in {quote(bad, safe=safe), quote(bad, safe="@,!()")}:
                        url = self.raw_url(op, name, encoded)
                        with self.assertRaises(ReadOnlyGuardError, msg=(op, name, bad, encoded)):
                            check_request("GET", url, environ={})
                    # Unencoded, a value with '/' re-segments the path. The guard
                    # may then only see a *different* well-typed operation, never
                    # this operation carrying the bad value.
                    url = self.raw_url(op, name, bad)
                    try:
                        check_request("GET", url, environ={})
                    except ReadOnlyGuardError:
                        pass
                    else:
                        self.assertIn("/", bad, (op, name, bad))
                        self.assertNotEqual(g.match(urlsplit(url).hostname,
                                                    urlsplit(url).path), op, (op, name, bad))
                    checked += 1
        self.assertGreater(checked, 300)

    def test_unknown_operations_and_parameter_mismatch_refused(self):
        from integrations import live_source_grammar as g
        with self.assertRaises(ValueError):
            g.url("graph.sendMail", user=VINCE_ID)
        with self.assertRaises(ValueError):
            g.url("graph.messages", user=VINCE_ID, extra="x")


class AliasAndProvenanceTests(unittest.TestCase):
    """Caller-shaped input reaching a resource the audience did not decide on."""

    def setUp(self):
        self.pages = {
            MSG_PATH: j(200, {"value": [message("m1", "ck1"),
                                        message("x1", "xk1", "Board pay", folder="INBOX-C")]}),
            f"/v1.0/users/{VINCE_ID}/mailFolders/INBOX-V/messages": j(200, {"value": [
                message("m1", "ck1"), message("x2", "xk2", folder="INBOX-C")]}),
            f"{MSG_PATH}/m1": j(200, message("m1", "ck1")),
            f"{MSG_PATH}/x1": j(200, message("x1", "xk1", folder="INBOX-C")),
            CEO_MSG_PATH: j(200, {"value": [message("c1", "cc1", "Board pay", folder="INBOX-C")]}),
        }
        self.sender = FakeSender(self.pages)
        self.api, self.transport = graph_api(self.sender)

    def reader(self, audiences, **kwargs) -> GraphMailReader:
        return GraphMailReader(self.api, LiveSourceAudience(audiences, tenant="plx", **kwargs))

    def test_folder_traversal_string_refused_before_io(self):
        reader = self.reader({"mail:*": ["staff:vince"]})
        for folder in (f"../../{CEO_ID}/messages", f"..%2F..%2F{CEO_ID}%2Fmessages",
                       "inbox/../../x", "AAMk.x", "inbox\\x"):
            with self.assertRaises(NotVisible, msg=folder):
                reader.list_messages(ACTOR, MAILBOX, folder=folder)
        self.assertEqual(self.sender.calls, [])

    def test_smtp_alias_deny_applies_when_upn_differs(self):
        for audiences, kwargs in (
                ({"mail:*": ["staff:vince"], "mail:ceo@petralabx.com": []}, {}),
                ({"mail:*": ["staff:vince"], "mail:chief@petralabx.com": []}, {}),
                ({"mail:*": ["staff:vince"]},
                 {"classifications": {"mail:ceo@petralabx.com": "restricted"}})):
            reader = self.reader(audiences, **kwargs)
            actor = ACTOR if not kwargs else Actor("plx", frozenset({"staff:vince"}),
                                                   "confidential")
            for alias in (CEO_UPN, CEO_UPN.upper(), CEO_ID, CEO_ID.upper(),
                          "ceo@petralabx.com", "chief@petralabx.com"):
                for call in (lambda: reader.list_messages(actor, alias),
                             lambda: reader.get_message(actor, alias, "c1"),
                             lambda: reader.resolve_citation(f"graph-mail://{CEO_ID}/c1@cc1",
                                                             actor)):
                    with self.assertRaises(NotVisible, msg=(audiences, alias)):
                        call()
        self.assertFalse(any("/messages" in c[1] for c in self.sender.calls))
        # The same audience still grants a mailbox none of whose addresses is denied.
        reader = self.reader({"mail:*": ["staff:vince"], "mail:ceo@petralabx.com": []})
        self.assertEqual(reader.list_messages(ACTOR, MAILBOX)["mailbox"], VINCE_ID)

    def test_messages_not_under_the_resolved_user_are_dropped(self):
        reader = self.reader({"mail:*": ["staff:vince"]})
        listing = reader.list_messages(ACTOR, MAILBOX)
        self.assertEqual([r["id"] for r in listing["items"]], ["m1"])
        self.assertEqual(listing["completeness"]["provenance_dropped"], 1)
        folder = reader.list_messages(ACTOR, MAILBOX, folder="inbox")
        self.assertEqual([r["id"] for r in folder["items"]], ["m1"])
        self.assertEqual(folder["completeness"]["provenance_dropped"], 1)
        listed = [c[1] for c in self.sender.api_calls() if "/messages" in c[1]]
        self.assertTrue(all(f"/users/{VINCE_ID}/" in u for u in listed))
        with self.assertRaises(NotVisible):
            reader.get_message(ACTOR, MAILBOX, "x1")
        with self.assertRaises(NotVisible):
            reader.list_messages(ACTOR, MAILBOX, folder="drafts")  # not under this user

    def test_old_upn_form_citations_are_not_accepted(self):
        reader = self.reader({"mail:*": ["staff:vince"]})
        with self.assertRaises(NotVisible):
            reader.resolve_citation("graph-mail://vince%40petralabx.com/m1@ck1", ACTOR)
        self.assertEqual(reader.resolve_citation(f"graph-mail://{VINCE_ID}/m1@ck1",
                                                 ACTOR)["status"], "current")

    def test_audience_identity_set_semantics(self):
        aud = LiveSourceAudience({"mail:a@x.com": ["staff:vince"], "mail:b@x.com": []},
                                 tenant="plx")
        self.assertTrue(aud.allows_all(ACTOR, ["mail:a@x.com", "mail:unmatched@x.com"]))
        self.assertFalse(aud.allows_all(ACTOR, ["mail:a@x.com", "mail:b@x.com"]))
        self.assertFalse(aud.allows_all(ACTOR, ["mail:unmatched@x.com"]))
        self.assertFalse(aud.allows_all(ACTOR, []))


class FolderItemDenyTests(unittest.TestCase):
    def setUp(self):
        finance = drive_item("FOLDER-FIN", "Finance", folder="", is_folder=True)
        self.payroll = drive_item("FOLDER-PAY", "Payroll", folder="/Finance", is_folder=True,
                                  parent_id="FOLDER-FIN")
        budget = drive_item("B1", "budget.xlsx", folder="/Finance", parent_id="FOLDER-FIN")
        foreign = drive_item("Z1", "planted.pdf", folder="/Finance", parent_id="OTHER-FOLDER")
        other_drive = drive_item("Z2", "other.pdf", folder="/Finance", parent_id="FOLDER-FIN")
        other_drive["parentReference"]["driveId"] = "b!another"
        self.routes = {
            f"{DRIVE_BASE}/root:/Finance:": j(200, finance),
            f"{DRIVE_BASE}/items/FOLDER-FIN/children": j(200, {"value": [
                self.payroll, budget, foreign, other_drive]}),
            f"{DRIVE_BASE}/items/FOLDER-PAY": j(200, self.payroll),
            f"{DRIVE_BASE}/items/B1": j(200, budget),
        }
        self.sender = FakeSender(self.routes)
        self.api, self.transport = graph_api(self.sender)
        self.reader = SharePointReader(self.api, LiveSourceAudience(
            CanonicalIdentityMatrixTests.SP_AUDIENCE, tenant="plx"), DRIVE,
            transport=self.transport)

    def test_deny_on_folder_contents_also_hides_the_folder_item(self):
        with self.assertRaises(NotVisible):
            self.reader.get_item(ACTOR, "FOLDER-PAY")
        with self.assertRaises(NotVisible):
            self.reader.resolve_citation(f"sharepoint://{quote(DRIVE, safe='')}/FOLDER-PAY@x",
                                         ACTOR)
        listing = self.reader.list_folder(ACTOR, "/Finance")
        self.assertEqual([r["id"] for r in listing["items"]], ["B1"])
        self.assertEqual(self.reader.get_item(ACTOR, "B1")["path"], "/Finance/budget.xlsx")

    def test_children_with_foreign_provenance_are_dropped(self):
        listing = self.reader.list_folder(ACTOR, "/Finance")
        self.assertEqual(listing["completeness"]["provenance_dropped"], 2)
        self.assertEqual(listing["completeness"]["withheld_count"], 3)


class NoLeakAndAudienceTests(unittest.TestCase):
    def test_refused_url_causes_no_token_request(self):
        sender = FakeSender({})
        api, _transport = graph_api(sender)
        with self.assertRaises(ReadOnlyGuardError):
            api.get_json(f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/sendMail")
        self.assertEqual(sender.calls, [])

    def test_no_secret_in_records_citations_or_reprs(self):
        mail = MailTests(); mail.setUp()
        sp = SharePointTests(); sp.setUp()
        bc = BusinessCentralTests(); bc.setUp()
        mail_reader = GraphMailReader(mail.api, audience())
        sp_reader = sp.reader()
        outputs = [mail_reader.list_messages(ACTOR, MAILBOX),
                   mail_reader.resolve_citation(
                       mail_reader.get_message(ACTOR, MAILBOX, "m1")["citation"], ACTOR),
                   sp_reader.list_folder(ACTOR, "/Customers"),
                   sp_reader.get_content(ACTOR, "F1")["record"],
                   bc.reader.list_entities(ACTOR, COMPANY, "purchaseOrders")]
        rendered = json.dumps(outputs) + "".join(repr(o) for o in (
            mail.api, mail.transport, mail_reader, sp_reader, bc.reader, bc.api, bc.transport))
        for leaked in (CLIENT_SECRET, ACCESS_TOKEN, TEMPAUTH, "Bearer"):
            self.assertNotIn(leaked, rendered)

    def test_example_audience_document_loads_and_defaults_deny(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        loaded = LiveSourceAudience.from_file(root / "examples/live_source_audience.example.json")
        vince = Actor("plx", frozenset({"staff:vince"}), "restricted")
        self.assertTrue(loaded.allows(vince, "mail:vince@petralabx.com"))
        self.assertTrue(loaded.source_acl_required("mail:vince@petralabx.com"))
        self.assertFalse(loaded.allows(vince, "mail:ceo@petralabx.com"))
        self.assertFalse(loaded.allows(Actor("plx", frozenset({"staff:vince"}), "confidential"),
                                       "mail:vince@petralabx.com"))
        self.assertFalse(loaded.allows(vince, "bc:production:companies"))
        ops = Actor("plx", frozenset({"staff:operations"}), "confidential")
        self.assertTrue(loaded.allows(ops, "sharepoint:<drive-id>:/Customer Documents/PO.pdf"))
        self.assertFalse(loaded.allows(ops, "sharepoint:<drive-id>:/Documents/PO.pdf"))
        self.assertFalse(loaded.allows(ops, "sharepoint:<drive-id>:/Customers/a\nb"))
        with self.assertRaises(ValueError):
            LiveSourceAudience.from_json({"schema": "plx-live-source-audience/v1",
                                          "tenant": "plx", "audiences": {"mail:*": ["*"]}})
        with self.assertRaises(ValueError):
            LiveSourceAudience.from_json({"schema": "other", "tenant": "plx", "audiences": {}})
        from integrations.live_source_guard import _strict_json
        with self.assertRaises(ValueError):
            _strict_json(b'{"a": 1, "a": 2}')


class ProbeCliTests(unittest.TestCase):
    def run_main(self, module, argv, sender) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(argv, environ=ENV, sender=sender)
        return code, out.getvalue(), err.getvalue()

    def test_graph_probe_prints_shapes_only(self):
        content = b"%PDF-1.7 original"
        file = drive_item("F1", "Root.pdf", folder="", content=content, parent_id="ROOT")
        download = f"https://{SP_HOST}/dl?{TEMPAUTH}"
        routes = {
            "/v1.0/users": j(200, {"value": [{"id": USER_ID}]}),
            f"/v1.0/users/{USER_ID}/messages": j(200, {"value": [message("m1", "ck1")]}),
            f"/v1.0/users/{USER_ID}/messages/m1": j(200, message("m1", "ck1")),
            f"/v1.0/sites/{SITE_ID}": j(200, {"id": "site"}),
            f"/v1.0/sites/{SITE_ID}/drives": j(200, {"value": [{"id": DRIVE}]}),
            f"/v1.0/drives/{DRIVE}/root": j(200, {"id": "ROOT", "name": "root", "eTag": "r",
                                                  "root": {}, "folder": {"childCount": 2}}),
            f"/v1.0/drives/{DRIVE}/items/ROOT/children": j(200, {"value": [
                drive_item("D1", "Customers", folder="", is_folder=True, parent_id="ROOT"),
                file]}),
            f"/v1.0/drives/{DRIVE}/items/F1": lambda url: live_drive_item(file, url, download),
            f"/v1.0/drives/{DRIVE}/items/D1": j(200, drive_item("D1", "Customers", folder="",
                                                                is_folder=True)),
            f"/v1.0/drives/{DRIVE}/items/F1/permissions": j(200, {"value": []}),
            f"/v1.0/drives/{DRIVE}/items/D1/permissions": j(403, {}),
            f"{SP_HOST}/dl": (200, {}, content),
        }
        code, out, err = self.run_main(graph_read, ["probe", "--content"], FakeSender(routes))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["mail"]["messages_returned"], 1)
        self.assertEqual(report["mail"]["citation_roundtrip"], "current")
        self.assertEqual(report["sharepoint"]["folders"], 1)
        self.assertEqual(report["sharepoint"]["files"], 1)
        self.assertEqual(report["sharepoint"]["content_probe"]["bytes"], len(content))
        self.assertEqual(report["site"]["drives_count"], 1)
        # The Graph reads answered below 400, as the live check records them for the
        # registry; the content arrives through the pre-authenticated download, so no
        # ``/content`` operation is named.
        from devloop import live_verify
        with open(f"{live_verify.ROOT}/{live_verify.SPECS_DIR_NAME}/"
                  "graph.adapter_probe_content.json", encoding="utf-8") as fh:
            spec = json.load(fh)
        self.assertEqual(live_verify._recorded(spec, report)["operations_read"], [
            "graph.drive_children", "graph.drive_item", "graph.drive_permissions",
            "graph.drive_root", "graph.mail_folder", "graph.message", "graph.messages",
            "graph.site", "graph.site_drives", "graph.user", "graph.users"])
        for leaked in (CLIENT_SECRET, ACCESS_TOKEN, TEMPAUTH, "PO12172025", "Supplier",
                       "Customers", "Root.pdf", USER_ID, MAILBOX, "petralabx", "first@"):
            self.assertNotIn(leaked.lower(), (out + err).lower(), leaked)

    def test_bc_probe_prints_counts_only(self):
        case = BusinessCentralTests()
        case.setUp()
        code, out, err = self.run_main(bc_read, ["probe"], FakeSender(case.routes))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["companies_count"], 3)
        self.assertTrue(report["environment_is_production"])
        self.assertEqual(report["entity"]["records_returned"], 1)
        self.assertEqual(report["entity"]["citation_roundtrip"], "current")
        for leaked in (CLIENT_SECRET, ACCESS_TOKEN, "PETRA", "PO5447", "1234", COMPANY):
            self.assertNotIn(leaked, out + err, leaked)

    def test_probe_failure_messages_carry_no_secret(self):
        def failing(method, url, headers, body, timeout, max_bytes):
            raise OSError(f"tls failure with {headers.get('Authorization')} {body!r}")
        code, out, err = self.run_main(bc_read, ["probe"], failing)
        self.assertEqual(code, 1)
        self.assertNotIn(CLIENT_SECRET, out + err)
        self.assertIn("SourceUnavailable", err)


if __name__ == "__main__":
    unittest.main()
