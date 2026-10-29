"""Guarded shared-link read: the two Graph GETs, everything else refused, and the
original returned with a drive citation over the bytes read (fixture transport)."""

from __future__ import annotations

import base64
from email.message import Message
from hashlib import sha256
import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request

from integrations import live_source_guard as guard
from integrations import shared_link
from integrations.graph_read import build_graph, drive_citation, quick_xor_hash
from integrations.live_source_guard import (LiveSourceAudience, NotVisible, ReadOnlyGuardError,
                                            RequestBudget, RequestBudgetExhausted,
                                            SourceUnavailable, check_request, urllib_sender)
from ontology.core import Actor

TENANT = "11111111-2222-3333-4444-555555555555"
SECRET = "s3cr3t~CLIENT~value~do-not-leak"
TOKEN = "eyJ0eXAi.ACCESS-TOKEN-do-not-leak.sig"
TEMPAUTH = "tempauth=DOWNLOAD-TOKEN-do-not-leak"
SP_HOST = "petralabx.sharepoint.com"
OD_HOST = "petralabx-my.sharepoint.com"
DRIVE, PERSONAL = "b!drive-AbC123", "b!personal-OnE_drive"
ITEM = "01ABCDEFITEM"
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": TENANT, "MICROSOFT_GRAPH_CLIENT_ID": TENANT,
       "MICROSOFT_GRAPH_CLIENT_SECRET": SECRET,
       "SHAREPOINT_SITE_ID": f"{SP_HOST},11111111-0000-0000-0000-000000000001,"
                             "22222222-0000-0000-0000-000000000002"}
LINK = f"https://{SP_HOST}/:b:/s/QCQA/EaBcD-xyz_123?e=AbCdEf"
PERSONAL_LINK = f"https://{OD_HOST}/:b:/g/personal/vince_petralabx_com/EfGh?e=Zz9"
BYTES = b"%PDF-1.7 AO-26005B packing list" * 40
ACTOR = Actor("plx", frozenset({"staff:vince"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:ops"}), "restricted")


def documented_share_id(url: str) -> str:
    """Microsoft Graph 'Encoding sharing URLs': base64, '=' trimmed, '/'->'_', '+'->'-'."""
    encoded = base64.b64encode(url.encode()).decode().rstrip("=")
    return "u!" + encoded.replace("/", "_").replace("+", "-")


def j(status, value, headers=None):
    return status, dict(headers or {}), json.dumps(value).encode()


class Drive:
    """One file reachable by a sharing link; routes by path, records every call."""

    def __init__(self, drive=DRIVE, link=LINK, download_host=SP_HOST, path="/Customers/Acme"):
        self.drive, self.link, self.download_host = drive, link, download_host
        self.paths = [path]
        self.etags = ["\"{E1},1\""]
        self.location = f"https://{download_host}/_layouts/15/download.aspx?{TEMPAUTH}"
        self.content_status = 302
        self.calls: list[tuple[str, str, dict]] = []

    def item(self):
        etag = self.etags.pop(0) if len(self.etags) > 1 else self.etags[0]
        path = self.paths.pop(0) if len(self.paths) > 1 else self.paths[0]
        return {"id": ITEM, "name": "AO-26005B.pdf", "eTag": etag, "size": len(BYTES),
                "webUrl": f"https://{self.download_host}/x/AO-26005B.pdf",
                "file": {"mimeType": "application/pdf",
                         "hashes": {"quickXorHash": quick_xor_hash(BYTES)}},
                "parentReference": {"driveId": self.drive,
                                    "path": f"/drives/{self.drive}/root:{path}"}}

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers)))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": TOKEN, "expires_in": 3599})
        if parts.hostname == self.download_host:
            return 200, {}, BYTES
        path = unquote(parts.path)
        if path == f"/v1.0/shares/{documented_share_id(self.link)}/driveItem":
            assert parse_qs(parts.query) == {"$select": ["id,parentReference"]}
            return j(200, {"id": ITEM, "parentReference": {"driveId": self.drive}})
        if path == f"/v1.0/drives/{self.drive}/items/{ITEM}":
            return j(200, self.item())
        if path == f"/v1.0/drives/{self.drive}/items/{ITEM}/content":
            if self.content_status == 302:
                return 302, {"Location": self.location}, b""
            return self.content_status, {}, BYTES
        return j(404, {"error": {"code": "itemNotFound"}})

    def sent(self):
        return [c for c in self.calls if "login.microsoftonline.com" not in c[1]]


def audience(drive=DRIVE):
    return LiveSourceAudience({f"sharepoint:{drive}:/Customers/*": ["staff:vince"],
                               f"sharepoint:{drive}:/Customers/Secret*": []}, tenant="plx")


def read(fake, actor=ACTOR, link=None, environ=ENV, **kwargs):
    api, transport = build_graph(environ, sender=fake)
    result = shared_link.read_shared_link(api, transport, audience(fake.drive), actor,
                                          fake.link if link is None else link, **kwargs)
    return result, transport


SHARE = documented_share_id(LINK)
GRAPH = "https://graph.microsoft.com/v1.0"
SHARE_URL = f"{GRAPH}/shares/{SHARE}/driveItem"
CONTENT_URL = f"{GRAPH}/drives/{DRIVE}/items/{ITEM}/content"


class GuardGrammarTests(unittest.TestCase):
    def test_only_the_two_gets_pass_the_guard(self):
        personal = f"{GRAPH}/drives/{PERSONAL}/items/{ITEM}/content"
        for url in (SHARE_URL, SHARE_URL + "?$select=id,parentReference", CONTENT_URL, personal):
            self.assertEqual(check_request("GET", url, environ={}), "graph.microsoft.com")
        refused_paths = [
            f"/shares/{SHARE}", f"/shares/{SHARE}/root", f"/shares/{SHARE}/list",
            f"/shares/{SHARE}/listItem", f"/shares/{SHARE}/permission", f"/shares/{SHARE}/items",
            f"/shares/{SHARE}/driveItem/children", f"/shares/{SHARE}/driveItem/content",
            f"/shares/{SHARE}/driveItem/createUploadSession", f"/shares/{SHARE}/driveItem/invite",
            "/shares/s!AbCdEf/driveItem",  # bad share ids: test_live_source_adapters grammar
            f"/drives/{DRIVE}/items/{ITEM}/content/x", f"/drives/{DRIVE}/root:/a.pdf:/content",
            f"/drives/{DRIVE}/items/{ITEM}/createLink", f"/drives/{DRIVE}/items/{ITEM}/copy",
            f"/drives/{DRIVE}/items/{ITEM}:/new.pdf:/content",
        ]
        for path in refused_paths:
            with self.subTest(path=path), self.assertRaises(ReadOnlyGuardError):
                check_request("GET", GRAPH + path, environ={})
        for method in ("POST", "PUT", "PATCH", "DELETE", "MERGE", "OPTIONS", "HEAD"):
            for url in (SHARE_URL, CONTENT_URL):
                with self.subTest(method=method, url=url), self.assertRaises(ReadOnlyGuardError):
                    check_request(method, url, body=None, environ={})

    def test_redirect_handed_back_only_for_content_and_never_followed(self):
        handler = guard._RefuseRedirect()
        self.assertIsNone(handler.redirect_request(Request(CONTENT_URL), None, 302, "Found", {},
                                                   f"https://{SP_HOST}/d?{TEMPAUTH}"))
        for url, code in ((SHARE_URL, 302), (CONTENT_URL, 301), (CONTENT_URL, 307)):
            with self.subTest(url=url, code=code), self.assertRaises(ReadOnlyGuardError):
                handler.redirect_request(Request(url), None, code, "x", {}, "https://evil.example/")

        def send(url, status):
            message = Message()
            message["Location"] = f"https://{SP_HOST}/d?{TEMPAUTH}"
            error = HTTPError(url, status, "Found", message, io.BytesIO(b""))
            opener = mock.Mock(open=mock.Mock(side_effect=error))
            with mock.patch.object(guard, "build_opener", lambda *handlers: opener):
                return urllib_sender("GET", url, {}, None, 5.0, 1000)

        self.assertEqual(send(CONTENT_URL, 302),
                         (302, {"location": f"https://{SP_HOST}/d?{TEMPAUTH}"}, b""))
        for url, status in ((SHARE_URL, 302), (CONTENT_URL, 303)):
            with self.subTest(url=url, status=status), self.assertRaises(ReadOnlyGuardError):
                send(url, status)
        _api, transport = build_graph(ENV, sender=lambda *a: (302, {"location": "x"}, b""))
        with self.assertRaises(ReadOnlyGuardError):  # any other operation's 302 stays refused
            transport.request("GET", SHARE_URL, {})


class SharedLinkReadTests(unittest.TestCase):
    def test_reader_returns_original_with_citation_over_bytes_read(self):
        for fake in (Drive(), Drive(drive=PERSONAL, link=PERSONAL_LINK, download_host=OD_HOST)):
            with self.subTest(drive=fake.drive):
                result, transport = read(fake)
                digest = sha256(BYTES).hexdigest()
                self.assertEqual(result["content"], BYTES)
                self.assertEqual(result["content_sha256"], digest)
                self.assertEqual((result["drive_id"], result["item_id"], result["etag"]),
                                 (fake.drive, ITEM, "\"{E1},1\""))
                self.assertEqual(result["drive_citation"],
                                 drive_citation(fake.drive, ITEM, "\"{E1},1\"", digest))
                self.assertEqual(result["content_status"], "read")
                sent = fake.sent()
                self.assertTrue(all(method == "GET" for method, _u, _h in sent))
                self.assertEqual([urlsplit(u).path.rsplit("/", 1)[-1] for _m, u, _h in sent],
                                 ["driveItem", ITEM, "content", "download.aspx", ITEM])
                content_get, download = sent[2], sent[3]
                self.assertEqual(content_get[2]["Authorization"], "Bearer " + TOKEN)
                self.assertEqual(urlsplit(download[1]).hostname, fake.download_host)
                self.assertNotIn("Authorization", download[2])
                rendered = repr(transport) + json.dumps(
                    {k: v for k, v in result.items() if k != "content"}, default=str)
                for leak in (TEMPAUTH, TOKEN, SECRET, fake.link):
                    self.assertNotIn(leak, rendered)
                self.assertFalse(any(s.reveal() == fake.location for s in transport._secrets))

    def test_refusals_fail_closed(self):
        cases = {
            "no grant": dict(actor=OUTSIDER),
            "foreign host": dict(link="https://evil.sharepoint.com/:b:/s/x/Eab"),
            "http link": dict(link=LINK.replace("https", "http")),
            "userinfo": dict(link=f"https://u@{SP_HOST}/:b:/s/x/Eab"),
        }
        for name, kwargs in cases.items():
            fake = Drive()
            with self.subTest(name), self.assertRaises(NotVisible):
                read(fake, **kwargs)
            self.assertEqual(fake.sent(), [], name)  # refused before any source I/O
        fake = Drive(path="/Customers/Secret Plans")
        with self.assertRaises(NotVisible):
            read(fake)
        self.assertEqual([urlsplit(u).path.rsplit("/", 1)[-1] for _m, u, _h in fake.sent()],
                         ["driveItem", ITEM])  # audience denied: no content, no download
        fake = Drive()
        with self.assertRaises(ReadOnlyGuardError):
            read(fake, environ={k: v for k, v in ENV.items() if k != "SHAREPOINT_SITE_ID"})
        self.assertEqual(fake.calls, [])
        fake = Drive()
        fake.location = f"https://evil.example.com/d?{TEMPAUTH}"
        with self.assertRaises(ReadOnlyGuardError) as caught:
            read(fake)
        self.assertNotIn(TEMPAUTH, str(caught.exception))
        self.assertFalse(any("evil.example.com" in u for _m, u, _h in fake.calls))
        for status in (200, 403):
            fake = Drive()
            fake.content_status = status
            with self.subTest(status=status), self.assertRaises(SourceUnavailable):
                read(fake)
        fake = Drive()
        fake.etags = ["\"{E1},1\"", "\"{E1},2\""]  # edited while the bytes were read
        with self.assertRaisesRegex(SourceUnavailable, "changed during content read"):
            read(fake)
        fake = Drive()
        fake.paths = ["/Customers/Acme", "/Customers/Other"]  # moved, same eTag
        with self.assertRaisesRegex(SourceUnavailable, "changed during content read"):
            read(fake)

    def test_request_budget_bounds_the_read(self):
        fake = Drive()
        api, transport = build_graph(ENV, sender=fake)
        budget = RequestBudget(transport, 4)  # token POST, driveItem, metadata, content
        with budget.active(), self.assertRaises(RequestBudgetExhausted):
            shared_link.read_shared_link(api, transport, audience(), ACTOR, LINK)
        self.assertEqual(budget.used, 4)
        self.assertFalse(any(urlsplit(u).hostname == SP_HOST for _m, u, _h in fake.calls))

    def test_probe_cross_checks_both_routes(self):
        fake = Drive(path="")
        env = {**ENV, "SHAREPOINT_DRIVE_ID": DRIVE}
        root = {"id": "ROOT", "root": {}, "name": "root"}

        def sender(method, url, headers, body, timeout, max_bytes):
            path = unquote(urlsplit(url).path)
            if path == f"/v1.0/drives/{DRIVE}/root":
                return j(200, root)
            if path == f"/v1.0/drives/{DRIVE}/items/ROOT/children":
                child = fake.item()
                child["parentReference"]["id"] = "ROOT"
                return j(200, {"value": [child]})
            if path == f"/v1.0/drives/{DRIVE}/items/{ITEM}" and "downloadUrl" in url:
                return j(200, {**fake.item(), "@microsoft.graph.downloadUrl": fake.location})
            return fake(method, url, headers, body, timeout, max_bytes)

        fake.link = fake.item()["webUrl"]
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(shared_link.main(["probe"], environ=env, sender=sender), 0)
        report = json.loads(out.getvalue())
        self.assertEqual({k: report[k] for k in ("token", "status", "same_item", "sha256_cited",
                                                 "sha256_matches_download_url_read", "bytes")},
                         {"token": "ok", "status": "read", "same_item": True,
                          "sha256_cited": True, "sha256_matches_download_url_read": True,
                          "bytes": len(BYTES)})
        for leak in (ITEM, DRIVE, fake.link, TEMPAUTH):
            self.assertNotIn(leak, out.getvalue())
        # The run names the reads Graph answered, the /shares/ resolution and the /content
        # read included: the source registry credits those endpoints only through them.
        self.assertTrue({"graph.share_drive_item", "graph.drive_item_content"}
                        <= set(report["operations_read"]), report["operations_read"])
        from devloop import live_verify
        spec = json.loads((live_verify.ROOT / live_verify.SPECS_DIR_NAME
                           / "graph.shared_link_read.json").read_text("utf-8"))
        self.assertEqual(live_verify.validate_spec(spec, stem=spec["id"]), [])
        self.assertEqual(live_verify.check_expectations(spec, 0, report), [])
        self.assertEqual(live_verify._recorded(spec, report)["operations_read"],
                         report["operations_read"])


if __name__ == "__main__":
    unittest.main()
