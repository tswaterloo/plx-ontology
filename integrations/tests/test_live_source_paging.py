"""nextLink paging across OData key syntax.

Graph answers a folder listing requested as
``/v1.0/users/<guid>/mailFolders/<id>/messages`` with a nextLink on
``/v1.0/users/<guid>/mailFolders('<id>')/messages`` (live).
Every mail listing with a second page used to raise
``nextLink escaped the collection``. These tests are keyless and use an
injected transport (evidence label: injected-transport, never live).
"""

from __future__ import annotations

import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlencode, urlsplit

from integrations import bc_read, live_source_grammar as grammar
from integrations.bc_read import BusinessCentralReader
from integrations.graph_read import GraphMailReader, SharePointReader
from integrations.http import SourceUnavailable
from integrations.live_source_guard import ReadOnlyGuardError, _same_collection_next, check_request
from integrations.tests import test_live_source_adapters as shared
from integrations.tests.test_live_source_adapters import (
    ACTOR, CEO_ID, COMPANY, CUSTOMERS, DRIVE, DRIVE_BASE, ENV, MAILBOX, TENANT_ID, VINCE_ID,
    FakeSender, audience, drive_item, graph_api, j, message)

GRAPH = "https://graph.microsoft.com"
# A realistic opaque folder id (base64url-like, case-sensitive, '=' padded).
FOLDER = "AAMkAGI2TG93AAA="
OTHER_FOLDER = "AAMkAGI2TG94AAA="
FOLDER_PATH = f"/v1.0/users/{VINCE_ID}/mailFolders/{FOLDER}/messages"
FOLDER_URL = f"{GRAPH}{FOLDER_PATH}?%24top=1"


def keyed(path_tail: str, *, user: str = VINCE_ID, folder: str = FOLDER) -> str:
    return f"{GRAPH}/v1.0/users/{user}/mailFolders('{folder}'){path_tail}"


class CanonicalCollectionTests(unittest.TestCase):
    """``_same_collection_next``: what continuation is the same collection."""

    def ok(self, link: str, request: str = FOLDER_URL) -> str:
        result = _same_collection_next(request, link)
        self.assertIsNotNone(result, link)
        # The sent URL keeps the request's own (grammar-built) path; only the
        # continuation query comes from the server.
        self.assertEqual(urlsplit(result).path, urlsplit(request).path)
        self.assertEqual(urlsplit(result).query, urlsplit(link).query)
        return result

    def refused(self, link, request: str = FOLDER_URL) -> None:
        self.assertIsNone(_same_collection_next(request, link), link)

    def test_exact_graph_odata_key_shape_is_the_same_collection(self):
        link = keyed("/messages?%24top=1&%24skip=1")
        self.assertEqual(self.ok(link), f"{GRAPH}{FOLDER_PATH}?%24top=1&%24skip=1")

    def test_equivalent_spellings_of_the_one_folder(self):
        for link in (
            f"{GRAPH}{FOLDER_PATH}?$skip=1",                                 # path form
            keyed("/messages?$skip=1", user=VINCE_ID.upper()),               # GUID case
            f"{GRAPH}/v1.0/users('{VINCE_ID}')/mailFolders('{FOLDER}')/messages?$skip=1",
            f"{GRAPH}/v1.0/users/{VINCE_ID}/mailfolders('{FOLDER}')/Messages?$skip=1",
            f"{GRAPH}/v1.0/users/{VINCE_ID}/mailFolders(%27{FOLDER}%27)/messages?$skip=1",
            f"{GRAPH}/v1.0/users/{VINCE_ID}/mailFolders('{quote(FOLDER, safe='')}')"
            "/messages?$skip=1",                                             # '=' as %3D
        ):
            self.ok(link)

    def test_other_collections_and_tricks_still_refused(self):
        refused = [
            keyed("/messages?$skip=1", user=CEO_ID),                     # another mailbox
            f"{GRAPH}/v1.0/users/{CEO_ID}/mailFolders/{FOLDER}/messages?$skip=1",
            keyed("/messages?$skip=1", folder=OTHER_FOLDER),             # another folder
            keyed("/messages?$skip=1", folder=FOLDER.lower()),           # opaque id case
            keyed("/messages?$skip=1", folder=FOLDER.upper()),
            keyed("?$skip=1"),                                           # parent collection
            f"{GRAPH}/v1.0/users/{VINCE_ID}/messages?$skip=1",           # parent collection
            f"{GRAPH}/v1.0/users/{VINCE_ID}/mailFolders?$skip=1",
            keyed("/messages/m1?$skip=1"),                               # one item, deeper
            keyed("/childFolders?$skip=1"),                              # sibling collection
            keyed("/../messages?$skip=1"),                               # traversal
            keyed("/%2E%2E/messages?$skip=1"),
            f"{GRAPH}/v1.0/users/{VINCE_ID}/mailFolders('..')/messages?$skip=1",
            keyed("/messages?$skip=1", folder="AAMk%2FAGI2"),            # encoded slash
            keyed("/messages?$skip=1", folder="AAMk%252FAGI2"),          # double-encoded
            keyed("/messages?$skip=1", folder="AAMk%5CAGI2"),            # encoded backslash
            keyed("/messages?$skip=1", folder="a''b"),                   # escaped quote
            f"{GRAPH}/v1.0/users/{VINCE_ID}/mailFolders(id='{FOLDER}')/messages",  # composite
            f"{GRAPH}/v1.0/users/{VINCE_ID}/mailFolders('{FOLDER}'/messages",       # unbalanced
            f"{GRAPH}/v1.0/users/{VINCE_ID}/mailFolders/{FOLDER}()/messages",
            keyed("/messages?$skip=1").replace(GRAPH, "https://graph.example.com"),
            keyed("/messages?$skip=1").replace(GRAPH, "https://evil.com/graph.microsoft.com"),
            keyed("/messages?$skip=1").replace("https://", "http://"),
            keyed("/messages?$skip=1").replace("https://", "https://u:p@"),
            keyed("/messages?$skip=1").replace(".com", ".com:443", 1),
            keyed("/messages?$skip=1") + "#frag",
            f"{GRAPH}//v1.0/users/{VINCE_ID}/mailFolders/{FOLDER}/messages",
            "/v1.0/users/x/messages", "", None, 42, "https://[::1",
        ]
        for link in refused:
            self.refused(link)

    def test_graph_continuation_parameters_are_a_fixed_set(self):
        """Graph paging adds $skip/$skiptoken/$deltatoken (and restates $top);
        the request's own options may repeat only with the request's value."""
        request = f"{GRAPH}{FOLDER_PATH}?%24select=id%2CchangeKey&%24top=1"
        for link in (keyed("/messages?%24select=id%2CchangeKey&%24top=1&%24skip=1"),
                     keyed("/messages?$select=id,changeKey&$top=1&$skip=2"),
                     keyed("/messages?$skiptoken=RFNwdAIAAQAAAD8%3D"),
                     keyed("/messages?$top=1&$skip=10"),
                     keyed("/messages?$deltatoken=abc123")):
            self.ok(link, request)
        for link in (keyed("/messages?$top=1&$skip=1&$expand=attachments"),   # new option
                     keyed("/messages?$search=%22invoice%22&$skip=1"),
                     keyed("/messages?$filter=isRead%20eq%20false&$skip=1"),  # not requested
                     keyed("/messages?$select=id,body&$skip=1"),              # other value
                     keyed("/messages?$skip=1&$skip=2"),                      # repeated
                     keyed("/messages?$skip=-1"), keyed("/messages?$skip=abc"),
                     keyed("/messages?$top=0&$skip=1"),
                     keyed("/messages?$skiptoken="),                          # empty token
                     keyed("/messages?$skiptoken=" + "A" * 2049),
                     keyed("/messages?$skip=1&foo=bar"),                      # custom name
                     keyed("/messages?$skip=1&api-version=2"),
                     keyed("/messages?$skip")):                               # no value
            self.refused(link, request)

    def test_graph_top_is_capped_at_the_request_top(self):
        request = f"{GRAPH}{FOLDER_PATH}?%24top=10"
        for top in ("1", "10"):
            self.ok(keyed(f"/messages?$top={top}&$skip=10"), request)
        for top in ("11", "999"):
            self.refused(keyed(f"/messages?$top={top}&$skip=10"), request)

    def test_repeated_options_compare_raw_or_decoded_without_plus(self):
        """'+' is a space in our urlencode-built request but may be literal to an
        OData server: a repeated option matches byte-identically, or decodes
        (without '+' handling) to the request's value."""
        request = (f"{GRAPH}{FOLDER_PATH}?%24top=1&"
                   "%24filter=receivedDateTime+ge+2026-09-01T00%3A00%3A00Z")
        for link in (keyed("/messages?%24top=1&%24filter=receivedDateTime+ge+"
                           "2026-09-01T00%3A00%3A00Z&%24skip=1"),          # byte-identical
                     keyed("/messages?$top=1&$filter=receivedDateTime%20ge%20"
                           "2026-09-01T00:00:00Z&$skip=1"),                # %20 = space
                     keyed("/messages?$filter=receivedDateTime+ge+2026-09-01T00:00:00Z"
                           "&$skip=1")):                 # Graph's echo: '+' kept, ':' raw
            self.ok(link, request)
        for link in (keyed("/messages?$filter=receivedDateTime%2Bge%2B"
                           "2026-09-01T00:00:00Z&$skip=1"),                # literal plus
                     keyed("/messages?$filter=receivedDateTime+ge+2026-09-01T00:00:01Z"
                           "&$skip=1")):                                   # other instant
            self.refused(link, request)
        spaced = f"{GRAPH}{FOLDER_PATH}?%24filter=receivedDateTime%20ge%20x"
        self.refused(keyed("/messages?$filter=receivedDateTime+ge+x&$skip=1"), spaced)
        self.ok(keyed("/messages?$filter=receivedDateTime%20ge%20x&$skip=1"), spaced)

    def test_graph_echo_of_a_filtered_request_is_accepted(self):
        """Live shape: the request is urlencode-built
        ('+' for spaces, ':' as %3A, ',' as %2C); Graph echoes $orderby
        byte-identically and $filter with ':' unescaped but '+' kept, adds
        $skip. Raw '+' positions coincide and the rest decodes to the same
        bytes, so the filter is the same under either reading of '+'."""
        query = {"$select": "id,changeKey,receivedDateTime", "$top": "1",
                 "$orderby": "receivedDateTime asc",
                 "$filter": "receivedDateTime ge 2026-07-03T09:00:00Z"}
        request = f"{GRAPH}{FOLDER_PATH}?{urlencode(query)}"
        self.assertIn("receivedDateTime+ge+2026-07-03T09%3A00%3A00Z", request)
        echo = ("/messages?%24select=id%2cchangeKey%2creceivedDateTime"
                "&%24orderby=receivedDateTime+asc"
                "&%24filter=receivedDateTime+ge+2026-07-03T09:00:00Z&%24top=1&%24skip=1")
        self.ok(keyed(echo), request)
        self.ok(keyed(echo.replace("09:00:00Z", "09%3a00%3a00Z")), request)  # hex case
        for changed in (
            echo.replace("ge+2026", "ge%202026"),           # '+' moved to %20: count differs
            echo.replace("ge+2026", "ge%2B2026"),           # literal plus where '+' was
            echo.replace("Time+ge+", "Time+ge++"),          # extra '+' (empty segment)
            echo.replace("Time+ge+", "Time+gt+"),           # other operator
            echo.replace("+ge+2026-07-03T09:00:00Z", "+ge+2026-07-03T09:00:00Z+or+true"),
            echo.replace("Z&%24top", "Z%20or%20isRead+eq+true&%24top"),
            echo.replace("09:00:00Z", "09%3A00%3A00Z%2"),   # malformed escape
            echo.replace("09:00:00Z", "09:00:00Z%"),
            echo.replace("09:00:00Z", "09:00:00%5A%25"),    # decodes to another value
            echo.replace("2026-07-03", "2026%2D07%2D03%FF"),
            echo.replace("receivedDateTime+asc", "receivedDateTime+desc"),
        ):
            self.refused(keyed(changed), request)
        # A request value with a literal '+' (%2B) never matches a raw '+'.
        plus = f"{GRAPH}{FOLDER_PATH}?{urlencode({'$filter': 'subject eq a+b'})}"
        self.assertIn("a%2Bb", plus)
        self.refused(keyed("/messages?$filter=subject+eq+a+b&$skip=1"), plus)
        self.ok(keyed("/messages?$filter=subject+eq+a%2bb&$skip=1"), plus)
        # Two different invalid UTF-8 bytes do not both compare as U+FFFD.
        odd = f"{GRAPH}{FOLDER_PATH}?$filter=a+%FE"
        self.refused(keyed("/messages?$filter=a+%FF&$skip=1"), odd)
        # A lone surrogate (JSON accepts "\\ud800") refuses instead of raising.
        self.refused(keyed("/messages?$filter=a+\ud800&$skip=1"),
                     f"{GRAPH}{FOLDER_PATH}?$filter=a+%3A")

    def test_sharepoint_drive_children_key_shapes(self):
        request = f"{GRAPH}{DRIVE_BASE}/items/FOLDER-CUST/children?%24top=1"
        drive = quote(DRIVE, safe="")  # b%21drive-AbC123
        for link in (f"{GRAPH}/v1.0/drives/{drive}/items/FOLDER-CUST/children?$skiptoken=X",
                     f"{GRAPH}/v1.0/drives('{DRIVE}')/items('FOLDER-CUST')/children"
                     "?$skiptoken=X"):
            self.ok(link, request)
        for link in (f"{GRAPH}/v1.0/drives/{DRIVE}/items('FOLDER-OTHER')/children?$skiptoken=X",
                     f"{GRAPH}/v1.0/drives('b!other')/items('FOLDER-CUST')/children",
                     f"{GRAPH}/v1.0/drives/{DRIVE.lower()}/items/FOLDER-CUST/children",
                     f"{GRAPH}/v1.0/drives/{DRIVE}/items('folder-cust')/children",
                     f"{GRAPH}/v1.0/drives/{DRIVE}/items/FOLDER-CUST/permissions",
                     f"{GRAPH}/v1.0/drives/{DRIVE}/root/children",
                     f"{GRAPH}/v1.0/drives/{DRIVE}/items('FOLDER-CUST%2F..%2FX')/children"):
            self.refused(link, request)

    def test_business_central_company_key_shapes(self):
        base = f"https://api.businesscentral.dynamics.com/v2.0/{TENANT_ID}/Production/api/v2.0"
        request = f"{base}/companies({COMPANY})/purchaseOrders?%24top=5"
        for link in (f"{base}/companies({COMPANY.upper()})/purchaseOrders?aid=FIN&$skiptoken=x",
                     f"{base}/companies('{COMPANY}')/purchaseOrders?$skiptoken=x",
                     f"{base}/companies/{COMPANY}/purchaseorders?$skiptoken=x"):
            self.ok(link, request)
        other = "0f0e0d0c-0b0a-0908-0706-0504030201ff"
        for link in (f"{base}/companies({other})/purchaseOrders?$skiptoken=x",
                     f"{base}/companies({COMPANY})/salesOrders?$skiptoken=x",
                     f"{base}/companies?$skiptoken=x",
                     f"{base}/companies({COMPANY})/purchaseOrders({COMPANY})?$skiptoken=x",
                     base.replace(TENANT_ID, CEO_ID) +
                     f"/companies({COMPANY})/purchaseOrders?$skiptoken=x",
                     base.replace("Production", "Sandbox") +
                     f"/companies({COMPANY})/purchaseOrders?$skiptoken=x",
                     f"{base}/companies({COMPANY})/purchaseOrders".replace(
                         "api.businesscentral", "graph") + "?$skiptoken=x"):
            self.refused(link, request)

    def test_business_central_nextlink_keeps_the_request_query(self):
        """p0-f-bc: a BC nextLink on the right path but another query was followed
        (and committed as a capture cursor). Documented shape, UNVERIFIED live
        (``live_source_grammar._BC_CONTINUATION``): the request's own options with
        their values, plus ``aid`` and an opaque ``$skiptoken``."""
        since = "lastModifiedDateTime ge 2026-09-30T09:50:00.000Z"
        request = grammar.url("bc.entities", {"$filter": since}, tenant=TENANT_ID,
                              environment="Production", company=COMPANY, entity="items")
        collection = request.split("?", 1)[0]
        token = quote("'{B1C2D3E4-0000-4000-8000-00000000002A}'", safe="")
        for link in (f"{collection}?aid=FIN&$filter={quote(since)}&$skiptoken={token}",
                     f"{request}&aid=FIN&%24skiptoken={token}",
                     f"{collection}?%24filter={quote_plus(since)}&$skiptoken={token}"):
            self.ok(link, request)
        widened = quote("lastModifiedDateTime ge 2020-01-01T00:00:00.000Z")
        for link in (f"{collection}?aid=FIN&$filter={widened}&$skiptoken={token}",
                     f"{collection}?aid=FIN&$skiptoken={token}",                 # filter dropped
                     f"{request}&$select=id,lastModifiedDateTime&$skiptoken={token}",
                     f"{request}&$expand=dimensionSetLines&$skiptoken={token}",
                     f"{request}&$top=5&$skiptoken={token}",                    # $top not asked
                     f"{request}&$skiptoken=a%20b",                             # token shape
                     f"{request}&$filter={quote(since)}&$skiptoken={token}"):   # repeated
            self.refused(link, request)
        top = grammar.url("bc.entities", {"$top": "5"}, tenant=TENANT_ID,
                          environment="Production", company=COMPANY, entity="items")
        self.ok(f"{top}&aid=FIN&$skiptoken={token}", top)
        self.refused(f"{collection}?$top=6&aid=FIN&$skiptoken={token}", top)

    def test_an_accepted_continuation_is_always_one_the_guard_sends(self):
        """A link repeating ``$filter`` with RAW spaces matched the
        request's decoded value, was accepted (and stored as a cursor), then refused
        by ``check_request`` when followed. Raw spaces, control characters and a
        malformed ``%`` escape now refuse where the link arrives, BC and Graph."""
        since = "lastModifiedDateTime ge 2026-09-30T09:50:00.000Z"
        request = grammar.url("bc.entities", {"$filter": since}, tenant=TENANT_ID,
                              environment="Production", company=COMPANY, entity="items")
        collection = request.split("?", 1)[0]
        token = quote("'{B1C2D3E4-0000-4000-8000-00000000002A}'", safe="")
        self.ok(f"{collection}?aid=FIN&$filter={quote(since)}&$skiptoken={token}", request)
        for link in (f"{collection}?aid=FIN&$filter={since}&$skiptoken={token}",
                     f"{collection}?aid=FIN&$filter={quote(since)}&$skiptoken={token}\t",
                     f"{collection}?aid=FIN&$filter={quote(since)}%2&$skiptoken={token}"):
            self.assertRaises(ReadOnlyGuardError, check_request, "GET",
                              collection + "?" + link.split("?", 1)[1])
            self.refused(link, request)
        for link in (keyed("/messages?%24top=1&%24skip=1&$skiptoken=a b"),
                     keyed("/messages?%24top=1&%24skip=1&$skiptoken=a%zz")):
            self.refused(link)


class MailFolderPagingTests(unittest.TestCase):
    """The live repro through the reader, injected transport."""

    def setUp(self):
        self.folders_seen: list[str] = []

        def page(url: str):
            query = parse_qs(urlsplit(url).query)
            self.folders_seen.append(urlsplit(url).path)
            if "$skip" not in query:
                link = keyed("/messages?" + urlencode({**{k: v[0] for k, v in query.items()},
                                                       "$skip": "1"}))
                return j(200, {"value": [message("m1", "ck1", folder=FOLDER)],
                               "@odata.nextLink": self.next_link or link})
            if query["$skip"] == ["1"]:
                link = keyed("/messages?" + urlencode({**{k: v[0] for k, v in query.items()},
                                                       "$skip": "2"}))
                return j(200, {"value": [message("m2", "ck2", folder=FOLDER)],
                               "@odata.nextLink": link})
            return j(200, {"value": [message("m3", "ck3", folder=FOLDER)]})

        self.next_link = None
        self.sender = FakeSender({FOLDER_PATH: page})
        api, _transport = graph_api(self.sender)
        self.reader = GraphMailReader(api, audience())
        # The folder lookup by id (mailFolders/<id>) resolves to itself.
        patcher = mock.patch.dict(shared.FOLDERS[VINCE_ID], {FOLDER.lower(): FOLDER})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_and_third_pages_follow_the_odata_key_nextlink(self):
        listing = self.reader.list_messages(ACTOR, MAILBOX, folder=FOLDER, page_size=1,
                                            max_pages=3)
        self.assertEqual([r["id"] for r in listing["items"]], ["m1", "m2", "m3"])
        completeness = listing["completeness"]
        self.assertEqual(completeness["pages_read"], 3)
        self.assertTrue(completeness["complete"])
        self.assertEqual(self.folders_seen, [FOLDER_PATH] * 3)  # never the server's path
        self.assertTrue(all(c[0] == "GET" for c in self.sender.api_calls()))

    def test_page_cap_reports_truncation_not_escape(self):
        listing = self.reader.list_messages(ACTOR, MAILBOX, folder=FOLDER, page_size=1,
                                            max_pages=2)
        self.assertEqual(listing["completeness"]["pages_read"], 2)
        self.assertEqual(listing["completeness"]["truncation_reason"], "page_cap")

    def test_page_cap_is_checked_before_the_continuation_is_inspected(self):
        """A max_pages=1 read never validates or
        follows the nextLink; it reports honest page_cap truncation."""
        for link in (keyed("/messages?$skip=1", user=CEO_ID),          # would escape
                     keyed("/messages?$expand=attachments&$skip=1"),  # would refuse
                     "not a url at all"):
            self.next_link = link
            self.folders_seen.clear()
            listing = self.reader.list_messages(ACTOR, MAILBOX, folder=FOLDER, page_size=1,
                                                max_pages=1)
            completeness = listing["completeness"]
            self.assertEqual((completeness["pages_read"], completeness["truncated"],
                              completeness["complete"], completeness["truncation_reason"]),
                             (1, True, False, "page_cap"))
            self.assertEqual([r["id"] for r in listing["items"]], ["m1"])
            self.assertEqual(self.folders_seen, [FOLDER_PATH])  # one GET only

    def test_graph_nextlink_with_a_non_continuation_parameter_is_refused(self):
        self.next_link = keyed("/messages?$expand=attachments&$skip=1")
        with self.assertRaisesRegex(SourceUnavailable, "escaped the collection"):
            self.reader.list_messages(ACTOR, MAILBOX, folder=FOLDER, page_size=1, max_pages=3)
        self.assertEqual(self.folders_seen, [FOLDER_PATH])  # no second GET

    def test_keyed_nextlink_to_another_mailbox_or_folder_refused(self):
        for link in (keyed("/messages?$skip=1", user=CEO_ID),
                     keyed("/messages?$skip=1", folder=OTHER_FOLDER),
                     keyed("/messages?$skip=1", folder="AAMk%2F..%2Fx")):
            self.next_link = link
            self.folders_seen.clear()
            with self.assertRaisesRegex(SourceUnavailable, "escaped the collection"):
                self.reader.list_messages(ACTOR, MAILBOX, folder=FOLDER, page_size=1)
            self.assertEqual(self.folders_seen, [FOLDER_PATH])  # no second GET


class SharePointChildrenPagingTests(unittest.TestCase):
    def test_keyed_children_nextlink_pages_on_the_request_path(self):
        children = f"{DRIVE_BASE}/items/FOLDER-CUST/children"
        seen = []

        def page(url):
            seen.append(urlsplit(url).path)
            if "$skiptoken" not in url:
                return j(200, {"value": [drive_item("F1", "a.pdf", parent_id="FOLDER-CUST")],
                               "@odata.nextLink": f"{GRAPH}/v1.0/drives('{DRIVE}')/items("
                                                  "'FOLDER-CUST')/children?$skiptoken=P2"})
            return j(200, {"value": [drive_item("F2", "b.pdf", parent_id="FOLDER-CUST")]})

        sender = FakeSender({f"{DRIVE_BASE}/root:/Customers:": j(200, CUSTOMERS),
                             children: page})
        api, transport = graph_api(sender)
        listing = SharePointReader(api, audience(), DRIVE, transport=transport).list_folder(
            ACTOR, "/Customers", page_size=1, max_pages=3)
        self.assertEqual([r["id"] for r in listing["items"]], ["F1", "F2"])
        self.assertEqual(listing["completeness"]["pages_read"], 2)
        self.assertEqual(seen, [children, children])


    def test_a_listing_resumes_at_its_next_page_and_position_and_refuses_a_foreign_start(self):
        # a listing stopped at its caps had no way on. ``next`` is the
        # page and position of the first child not handed back; passed as ``start`` the
        # listing continues there, one child at a time here (max_items 1, pages of 2).
        children = f"{DRIVE_BASE}/items/FOLDER-CUST/children"
        seen = []

        def page(url):
            seen.append(url)
            if "$skiptoken" not in url:
                return j(200, {"value": [drive_item(f"F{n}", f"{n}.pdf", parent_id="FOLDER-CUST")
                                         for n in (1, 2)],
                               "@odata.nextLink": f"{GRAPH}/v1.0/drives('{DRIVE}')/items("
                                                  "'FOLDER-CUST')/children?$skiptoken=P2"})
            return j(200, {"value": [drive_item("F3", "3.pdf", parent_id="FOLDER-CUST")]})

        sender = FakeSender({f"{DRIVE_BASE}/root:/Customers:": j(200, CUSTOMERS),
                             children: page})
        api, transport = graph_api(sender)
        reader = SharePointReader(api, audience(), DRIVE, transport=transport)
        ids, start, nexts = [], None, []
        while True:
            listing = reader.list_folder(ACTOR, "/Customers", page_size=2, max_items=1,
                                         start=start)
            ids += [r["id"] for r in listing["items"]]
            start = listing["completeness"]["next"]
            nexts.append(start and (urlsplit(start["url"]).query.endswith("P2"),
                                    start["offset"]))
            if start is None:
                break
        self.assertEqual(ids, ["F1", "F2", "F3"])
        self.assertEqual(nexts, [(False, 1), (True, 0), None])
        self.assertEqual(len(seen), 3)   # page one twice (to its second child), page two
        self.assertTrue(listing["completeness"]["complete"])
        first = seen[0]
        for bad in ({"url": first.replace("FOLDER-CUST", "FOLDER-HR"), "offset": 0},
                    {"url": f"{GRAPH}/v1.0/drives/{DRIVE}/items/FOLDER-CUST/permissions",
                     "offset": 0},
                    {"url": first, "offset": True}, {"url": first, "offset": -1},
                    {"url": first, "offset": 0, "skip": 1}, first):
            with self.subTest(start=bad), self.assertRaises(ValueError):
                reader.list_folder(ACTOR, "/Customers", page_size=2, start=bad)
        self.assertEqual(len(seen), 3)   # a refused start reads no page

class BusinessCentralPagingTests(unittest.TestCase):
    BASE = f"/v2.0/{TENANT_ID}/Production/api/v2.0"

    def test_skiptoken_nextlink_with_company_key_syntax(self):
        path = f"{self.BASE}/companies({COMPANY})/purchaseOrders"
        seen = []
        ids = ["aaaaaaaa-0000-0000-0000-00000000000" + str(n) for n in (1, 2)]

        def page(url):
            seen.append(url)
            query = urlsplit(url).query
            n = 1 if "$skiptoken" in unquote(query) else 0
            body = {"value": [{"id": ids[n], "number": f"PO{n}", "@odata.etag": "W/\"1\"",
                               "lastModifiedDateTime": "2026-09-28T08:00:00Z"}]}
            if n == 0:
                body["@odata.nextLink"] = (
                    f"https://api.businesscentral.dynamics.com/v2.0/{TENANT_ID}/Production/api/"
                    f"v2.0/companies({COMPANY.upper()})/purchaseOrders?"
                    + "&".join(filter(None, (query, "aid=FIN", "$skiptoken=" + ids[0]))))
            return 200, {}, json.dumps(body).encode()

        sender = FakeSender({path: page})
        api, _transport, tenant, environment = bc_read.build_bc(ENV, sender=sender)
        reader = BusinessCentralReader(api, audience(), tenant_id=tenant,
                                       environment=environment)
        listing = reader.list_entities(ACTOR, COMPANY, "purchaseOrders", page_size=1,
                                       max_pages=3)
        self.assertEqual([r["id"] for r in listing["items"]], ids)
        self.assertEqual(listing["completeness"]["pages_read"], 2)
        self.assertEqual([urlsplit(u).path for u in seen], [path, path])
        self.assertIn("$skiptoken", unquote(seen[1]))


class MailPagingLiveCheckShapeTests(unittest.TestCase):
    """verify/live-checks/graph.mail_paging.json against an injected Graph
    (shape of the check only; the live run is the live check's, never this)."""

    MAILBOX_ID = "12345678-aaaa-bbbb-cccc-0000000000aa"

    def run_check(self, second_link):
        import tempfile
        from pathlib import Path

        from verify import live_verify
        from operating import ingestion

        root = Path(__file__).resolve().parents[2]
        spec = json.loads((root / "verify/live-checks/graph.mail_paging.json").read_text())
        live_verify.validate_spec(spec)
        command = spec["command"]
        sources_path = root / command[command.index("--sources") + 1]
        audience_path = root / command[command.index("--live-audience") + 1]
        folder_path = f"/v1.0/users/{self.MAILBOX_ID}/mailFolders/{FOLDER}/messages"

        def page(url):
            query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            skip = int(query.pop("$skip", "0"))
            body = {"value": [message(f"p{skip}", f"ck{skip}", folder=FOLDER)]}
            body["value"][0]["receivedDateTime"] = f"2026-09-30T09:0{skip}:00Z"
            if skip < 3:
                body["@odata.nextLink"] = second_link(urlencode({**query, "$skip": skip + 1}))
            return j(200, body)

        box = {"id": self.MAILBOX_ID, "userPrincipalName": "purchase-order@petrasoap.com",
               "mail": "purchase-order@petrasoap.com"}
        sender = FakeSender({folder_path: page},
                            directory={"purchase-order@petrasoap.com": box,
                                       self.MAILBOX_ID: box})
        api, _transport = graph_api(sender)
        env = {k: v for k, v in ENV.items() if not k.startswith("SHAREPOINT")}
        with mock.patch.dict(shared.FOLDERS, {self.MAILBOX_ID: {"inbox": FOLDER,
                                                                FOLDER.lower(): FOLDER}}), \
                tempfile.TemporaryDirectory() as tmp:
            tenant, actor, sources = ingestion.load_sources_file(sources_path, env)
            reader = GraphMailReader(api, ingestion.load_live_audience(audience_path, env))
            ingestor = ingestion.Ingestor(Path(tmp) / "state", sources, actor=actor,
                                          mail=reader)
            try:
                result = ingestor.run()
            finally:
                ingestor.close()
            report = {"runs": [{"summary": result["summary"],
                                "units": [{k: u[k] for k in ("unit_id", "state", "reasons",
                                                             "counts")}
                                          for u in result["units"]]}],
                      "status": ingestion.status(Path(tmp) / "state", sources=sources)}
            report["row_data_check"] = ingestion.row_data_check(
                Path(tmp) / "state", json.dumps(report, sort_keys=True))
        return live_verify.check_expectations(spec, 0, report), report

    def test_keyed_nextlink_satisfies_the_check(self):
        failures, report = self.run_check(lambda q: keyed(f"/messages?{q}",
                                                          user=self.MAILBOX_ID))
        self.assertEqual(failures, [])
        self.assertEqual(report["runs"][0]["units"][0]["counts"]["pages"], 3)
        self.assertEqual(report["runs"][0]["units"][0]["reasons"], ["cap:page_cap"])

    def test_graph_echo_nextlink_satisfies_the_check(self):
        """Graph re-escapes the echoed $filter (':' raw) but keeps '+' for
        spaces: followed to the page cap."""
        def echo(query):
            parsed = {k: v[0] for k, v in parse_qs(query).items()}
            return keyed("/messages?" + urlencode(
                parsed, quote_via=lambda v, safe, enc, err: quote_plus(v, safe=":,")),
                user=self.MAILBOX_ID)

        failures, report = self.run_check(echo)
        self.assertEqual(failures, [])
        unit = report["runs"][0]["units"][0]
        self.assertEqual(unit["reasons"], ["cap:page_cap"])
        self.assertEqual((unit["counts"]["pages"], unit["counts"]["items_read"]), (3, 3))

    def test_escaping_nextlink_fails_the_check(self):
        failures, report = self.run_check(lambda q: keyed(f"/messages?{q}", user=CEO_ID))
        self.assertTrue(any("counts.pages" in f for f in failures), failures)
        unit = report["runs"][0]["units"][0]
        self.assertEqual(unit["reasons"], ["source_error:SourceUnavailable:nextlink_escaped"])
        # The first page was read before the refusal; its counts are kept.
        self.assertEqual((unit["counts"]["pages"], unit["counts"]["items_read"]), (1, 1))
        self.assertEqual(unit["state"], "incomplete")

    def test_second_nextlink_escaping_fails_the_check(self):
        """First nextLink accepted, second escapes: the partial counts (pages 2,
        items_read 2) satisfy json_min, so the reasons check must fail it."""
        def link(query):
            skip = int(parse_qs(query)["$skip"][0])
            return keyed(f"/messages?{query}", user=self.MAILBOX_ID if skip < 2 else CEO_ID)

        failures, report = self.run_check(link)
        unit = report["runs"][0]["units"][0]
        self.assertEqual((unit["counts"]["pages"], unit["counts"]["items_read"]), (2, 2))
        self.assertEqual(unit["reasons"], ["source_error:SourceUnavailable:nextlink_escaped"])
        self.assertEqual(failures, ["runs[*].units[*].reasons[*] != expected"])

    def test_a_complete_read_without_page_cap_fails_the_check(self):
        """Too few messages for the cap (no reason at all) is reported, not passed."""
        failures, report = self.run_check(
            lambda q: None if int(parse_qs(q)["$skip"][0]) > 1
            else keyed(f"/messages?{q}", user=self.MAILBOX_ID))
        self.assertEqual(report["runs"][0]["units"][0]["reasons"], [])
        self.assertEqual(failures, ["missing runs[*].units[*].reasons[*]"])

    def test_sources_file_reads_a_two_year_window(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        doc = json.loads((root / "examples/mail_paging_sources.live-check.json").read_text())
        (source,) = doc["sources"]
        self.assertEqual((source["initial_window_days"], source["page_size"],
                          source["max_pages"], source["max_items"]), (730, 1, 3, 3))


if __name__ == "__main__":
    unittest.main()
