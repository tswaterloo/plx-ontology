"""Zendesk, Shopify, Granola and EDI readers on recorded fixtures and
injected transports only, never a live read. Fixtures follow each vendor's public API
reference; every id, address and amount is synthetic.

Boundary: what the one sender lets out (GET on one configured tenant host, closed paths and
queries), disabled-until-configured, no secret or download URL in a record, incremental
cursors that never skip on failure, Decimal money with its own currency, conservative
identity joins, EDI control-total checks that refuse a malformed file whole, and bindings
that name real ontology types and properties."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from integrations import edi_read, granola_read, shopify_read, zendesk_read
from integrations import capture_vendor as cv
from integrations import live_source_grammar as grammar
from integrations.capture_store import CaptureError, CaptureStore
from integrations import meeting_transcripts as mt
from integrations import vendor_sweep as vs
from integrations.http import SourceUnavailable
from integrations.live_source_guard import (Actor, BoundedBackoff, GuardedTransport,
                                            LiveSourceAudience, ReadOnlyGuardError,
                                            check_request)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
ZD_ENV = {"ZENDESK_SUBDOMAIN": "synthetic", "ZENDESK_EMAIL": "agent@example.test",
          "ZENDESK_API_TOKEN": "synthetic-zendesk-token-value"}
SH_ENV = {"SHOPIFY_SHOP": "synthetic-shop", "SHOPIFY_ACCESS_TOKEN": "synthetic-shopify-token"}
GR_ENV = {"GRANOLA_API_KEY": "synthetic-granola-key-value"}
ZD_HOST, SH_HOST = "synthetic.zendesk.com", "synthetic-shop.myshopify.com"
CAUGHT = (SourceUnavailable, ReadOnlyGuardError)


class Recorder:
    """An injected sender answering from ``route(url) -> (status, payload, headers)``."""

    def __init__(self, route):
        self.route, self.sent = route, []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.sent.append((method, url, dict(headers)))
        status, payload, extra = self.route(url)
        return status, {"Content-Type": "application/json", **extra}, json.dumps(payload).encode()


def transport(route):
    rec = Recorder(route)
    return GuardedTransport(rec), rec


def tmp_state(test):
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    return Path(d.name) / "state.json"


class GuardBoundary(unittest.TestCase):
    def test_only_the_configured_shape_of_tenant_host_and_path_passes(self):
        ok = [f"https://{ZD_HOST}/api/v2/incremental/tickets/cursor.json?start_time=1&per_page=100",
              f"https://{ZD_HOST}/api/v2/tickets/42/comments.json?page%5Bsize%5D=100",
              f"https://{SH_HOST}/admin/api/2025-10/orders.json?status=any"
              "&updated_at_min=2026-11-01T00%3A00%3A00Z&limit=250",
              f"https://{SH_HOST}/admin/api/2025-10/orders.json?page_info=abc_DEF&limit=250",
              "https://public-api.granola.ai/v1/notes?page_size=30",
              "https://public-api.granola.ai/v1/notes/not_1d3tmYTlCICgjy?include=transcript"]
        for url in ok:
            self.assertEqual(check_request("GET", url, environ={}), urlsplit(url).hostname, url)
        refused = [
            "https://evil.example.test/api/v2/tickets/42/comments.json",
            "https://a.b.zendesk.com/api/v2/tickets/42/comments.json",      # two labels
            "https://zendesk.com.evil.test/api/v2/tickets/42/comments.json",
            "https://-bad.zendesk.com/api/v2/tickets/42/comments.json",
            f"https://{ZD_HOST}/api/v2/tickets.json",                       # not an operation
            f"https://{ZD_HOST}/api/v2/tickets/42.json",                    # no op (single read)
            f"https://{ZD_HOST}/api/v2/tickets/4x/comments.json",
            f"https://{ZD_HOST}/api/v2/incremental/tickets/cursor.json?start_time=1&cursor=a",
            f"https://{ZD_HOST}/api/v2/incremental/tickets/cursor.json",    # needs a start
            f"https://{ZD_HOST}:8443/api/v2/tickets/42/comments.json",
            f"https://{SH_HOST}/admin/api/2025-10/orders.json?status=any"
            "&updated_at_min=2026-11-01T00%3A00%3A00Z&limit=999",
            f"https://{SH_HOST}/admin/api/2025-10/orders.json?page_info=abc&status=any",
            f"https://{SH_HOST}/admin/api/2025-10/orders.json?limit=250",   # no window
            f"https://{SH_HOST}/admin/api/latest/orders.json?page_info=abc",
            f"https://{SH_HOST}/admin/api/2025-10/orders/1.json",
            f"https://{SH_HOST}/admin/api/2025-10/orders/1/cancel.json",
            "https://public-api.granola.ai/v1/notes/12345",
            "https://public-api.granola.ai/v1/notes?foo=bar",
        ]
        for url in refused:
            with self.assertRaises(ReadOnlyGuardError, msg=url):
                check_request("GET", url, environ={})
        for host in (ZD_HOST, SH_HOST, "public-api.granola.ai"):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.assertRaises(ReadOnlyGuardError):
                    check_request(method, f"https://{host}/v1/notes", environ={})

    def test_a_tenant_host_not_named_by_the_operation_is_never_built(self):
        with self.assertRaises(ValueError):
            grammar.url("zendesk.ticket_comments", tenant_host=SH_HOST, ticket="1")
        with self.assertRaises(ValueError):
            grammar.url("zendesk.ticket_comments", ticket="1")


class Disabled(unittest.TestCase):
    def test_every_connector_sends_nothing_without_its_settings(self):
        state = tmp_state(self)
        t, rec = transport(lambda u: (500, {}, {}))
        for result in (zendesk_read.sweep({}, state_path=state, transport=t),
                       shopify_read.sweep({}, state_path=state, transport=t),
                       granola_read.sweep({}, state_path=state, transport=t),
                       edi_read.sweep({}, state_path=state)):
            self.assertEqual(result["blocked"], "credentials_absent")
            self.assertEqual(result["requests_sent"], 0)
            self.assertTrue(result["missing_settings"])
        self.assertEqual(rec.sent, [])
        self.assertFalse(state.exists())

    def test_an_invalid_host_label_is_refused_before_any_request(self):
        t, rec = transport(lambda u: (500, {}, {}))
        bad = zendesk_read.sweep({**ZD_ENV, "ZENDESK_SUBDOMAIN": "a.evil.test"},
                                 state_path=tmp_state(self), transport=t)
        self.assertEqual(bad["blocked"], "setting_invalid")
        bad = shopify_read.sweep({**SH_ENV, "SHOPIFY_API_VERSION": "latest"},
                                 state_path=tmp_state(self), transport=t)
        self.assertEqual(bad["invalid_settings"], ["SHOPIFY_API_VERSION"])
        self.assertEqual(rec.sent, [])


def zd_route(fail_comments=False, orders_total=2):
    ticket = lambda i, to: {"id": i, "subject": f"Order {i}", "description": "2 cases please",
                            "status": "open", "recipient": to, "requester_id": 9,
                            "created_at": "2026-11-13T10:00:00Z",
                            "updated_at": "2026-11-13T10:05:00Z",
                            "via": {"channel": "email", "source": {"from": {
                                "address": "Buyer@Example.Test"}}}}

    def route(url):
        parts = urlsplit(url)
        q = parse_qs(parts.query)
        if parts.path.endswith("/incremental/tickets/cursor.json"):
            if q.get("cursor") == ["c1"]:
                return 200, {"tickets": [ticket(3, "order@petrasoap.com")],
                             "after_cursor": "c2", "end_of_stream": True}, {}
            first = [ticket(i, "order@petrasoap.com") for i in range(1, orders_total + 1)]
            return 200, {"tickets": first + [ticket(99, "support@petrasoap.com")],
                         "after_cursor": "c1", "end_of_stream": False}, {}
        if parts.path.endswith("/comments.json"):
            if fail_comments:
                return 503, {}, {}
            return 200, {"comments": [{"id": 700, "author_id": 9, "public": True,
                                       "plain_body": "Please ship 2 cases of SKU-1",
                                       "created_at": "2026-11-13T10:00:00Z",
                                       "attachments": [{"id": 55, "file_name": "po.pdf",
                                                        "content_type": "application/pdf",
                                                        "size": 1234, "inline": False,
                                                        "content_url": "https://x.test/att/SECRETTOKEN"}]}],
                         "meta": {"has_more": False}}, {}
        return 404, {}, {}
    return route


class Zendesk(unittest.TestCase):
    def test_order_queue_tickets_messages_attachments_and_incremental_cursor(self):
        state = tmp_state(self)
        t, rec = transport(zd_route())
        out = zendesk_read.sweep(ZD_ENV, state_path=state, transport=t, now=NOW)
        self.assertTrue(out["complete"] and "error" not in out)
        kinds = [r["record_type"] for r in out["records"]]
        # 3 queue tickets (the support@ ticket is not the order queue), 1 message each
        self.assertEqual(kinds.count("OrderRequestTicket"), 3)
        self.assertEqual(kinds.count("OrderRequestMessage"), 3)
        ticket = out["records"][0]
        self.assertEqual(ticket["join_keys"]["requester_email"], "buyer@example.test")
        self.assertEqual(ticket["audience_unit"], "zendesk:order-queue")
        message = out["records"][1]
        self.assertEqual(message["attachments"][0], {
            "attachment_id": "55", "file_name": "po.pdf", "content_type": "application/pdf",
            "size_bytes": 1234, "inline": False})
        blob = json.dumps(out["records"])
        for secret in ("SECRETTOKEN", "synthetic-zendesk-token-value", "content_url"):
            self.assertNotIn(secret, blob)
        self.assertTrue(all(m == "GET" for m, _u, _h in rec.sent))
        self.assertEqual(out["freshness"]["state"], "fresh")
        # the next sweep resumes at the saved cursor, not at the epoch
        t2, rec2 = transport(lambda u: (200, {"tickets": [], "after_cursor": "c3",
                                              "end_of_stream": True}, {}))
        zendesk_read.sweep(ZD_ENV, state_path=state, transport=t2, now=NOW)
        self.assertIn("cursor=c2", rec2.sent[0][1])

    def test_a_failed_read_never_advances_the_cursor_and_is_reported(self):
        state = tmp_state(self)
        t, _ = transport(zd_route(fail_comments=True))
        out = zendesk_read.sweep(ZD_ENV, state_path=state, transport=t, now=NOW)
        self.assertEqual(out["error"], "SourceUnavailable")
        self.assertEqual(out["http_status"] if "http_status" in out else 503, 503)
        self.assertFalse(out["complete"])
        self.assertNotIn("cursor", vs.SweepState(state).get("zendesk"))
        self.assertNotEqual(out["freshness"]["state"], "fresh")

    def test_rate_limit_is_retried_within_the_bounded_backoff(self):
        calls = {"n": 0}

        def route(url):
            calls["n"] += 1
            return (429, {}, {"Retry-After": "1"}) if calls["n"] == 1 else (
                200, {"comments": [], "meta": {"has_more": False}}, {})
        t, _ = transport(route)
        sleeps = []
        reader = zendesk_read.ZendeskReader(
            ZD_HOST, {"Authorization": "x"}, t, zendesk_read.load_scope(),
            backoff=BoundedBackoff(sleep=sleeps.append, jitter=lambda: 0.0))
        self.assertEqual(reader.comments("1", "u"), [])
        self.assertEqual(len(sleeps), 1)

    def test_malformed_payloads_fail_closed(self):
        for payload in ({"tickets": "x", "end_of_stream": True},
                        {"tickets": [], "end_of_stream": True},          # no cursor
                        {"tickets": [], "after_cursor": "a b", "end_of_stream": True}):
            t, _ = transport(lambda u, p=payload: (200, p, {}))
            out = zendesk_read.sweep(ZD_ENV, state_path=tmp_state(self), transport=t, now=NOW)
            self.assertEqual(out["error"], "SourceUnavailable", payload)

    def test_scope_file_names_only_valid_addresses(self):
        self.assertEqual(zendesk_read.load_scope()["audience_unit"], "zendesk:order-queue")
        bad = Path(tempfile.mkdtemp()) / "s.json"
        bad.write_text('{"order_recipients": ["not-an-address"]}')
        with self.assertRaises(vs.SettingInvalid):
            zendesk_read.load_scope(bad)


def sh_order(i, total="19.90"):
    return {"id": 1000 + i, "name": f"#{1000 + i}", "email": "Shopper@Example.Test",
            "created_at": "2026-11-13T09:00:00-05:00",
            "updated_at": f"2026-11-13T10:{i % 60:02d}:00-05:00",
            "currency": "USD", "subtotal_price": "17.90", "total_price": total,
            "financial_status": "paid", "fulfillment_status": None,
            "customer": {"id": 5, "email": "shopper@example.test"},
            "line_items": [{"id": 1, "sku": "SKU-A", "title": "Bar", "quantity": 2,
                            "price": "5.95"}, {"id": 2, "sku": "SKU-B", "title": "Jar",
                                               "quantity": 1, "price": "6.00"}],
            "fulfillments": [{"id": 77, "status": "success", "tracking_company": "UPS",
                              "tracking_number": "1Z0000", "created_at": "2026-11-14T08:00:00-05:00"}]}


def sh_route(n, page=25, headers=None):
    def route(url):
        q = parse_qs(urlsplit(url).query)
        start = int(q["page_info"][0].split("_")[1]) if "page_info" in q else 0
        rows = [sh_order(i) for i in range(start, min(n, start + page))]
        extra = dict(headers or {})
        if start + page < n:
            extra["Link"] = (f'<https://{SH_HOST}/admin/api/2025-10/orders.json'
                             f'?limit=250&page_info=p_{start + page}>; rel="next"')
        return 200, {"orders": rows}, extra
    return route


class Shopify(unittest.TestCase):
    def test_cardinality_no_order_line_or_fulfilment_lost_or_duplicated(self):
        for n in (1, 10, 60):
            t, rec = transport(sh_route(n))
            out = shopify_read.sweep(SH_ENV, state_path=tmp_state(self), transport=t, now=NOW)
            kinds = [r["record_type"] for r in out["records"]]
            self.assertEqual((kinds.count("ShopifyOrder"), kinds.count("ShopifyOrderLine"),
                              kinds.count("ShopifyFulfillment")), (n, 2 * n, n), n)
            cites = [r["citation"] for r in out["records"]]
            self.assertEqual(len(cites), len(set(cites)), n)
            self.assertEqual(len(rec.sent), -(-n // 25))

    def test_money_is_decimal_with_its_own_currency_and_identity_is_an_exact_email(self):
        t, _ = transport(sh_route(1))
        order, line, _line2, fulfilment = shopify_read.sweep(
            SH_ENV, state_path=tmp_state(self), transport=t, now=NOW)["records"]
        self.assertEqual(order["properties"]["total_amount"],
                         {"amount": "19.90", "currency": "USD"})
        self.assertEqual(line["properties"]["unit_price"], {"amount": "5.95", "currency": "USD"})
        self.assertEqual(order["join_keys"]["customer_email"], "shopper@example.test")
        self.assertEqual(fulfilment["properties"]["tracking_number"], "1Z0000")
        for bad in (("1,990", "USD"), ("19.9e1", "USD"), ("19.90", None), (19.9, "USD"),
                    ("19.90", "usd")):
            self.assertIsNone(shopify_read.money(*bad), bad)
        self.assertIn('"19.90"', json.dumps(order))

    def test_next_link_must_be_this_shops_own_page(self):
        for link in (f'<https://other-shop.myshopify.com/admin/api/2025-10/orders.json'
                     '?page_info=zz>; rel="next"',
                     f'<https://{SH_HOST}/admin/api/2025-10/customers.json?page_info=zz>; rel="next"',
                     f'<https://{SH_HOST}/admin/api/2025-10/orders.json?page_info=a%20b>; rel="next"'):
            t, _ = transport(lambda u, link=link: (200, {"orders": []}, {"Link": link}))
            out = shopify_read.sweep(SH_ENV, state_path=tmp_state(self), transport=t, now=NOW)
            self.assertEqual(out["error"], "SourceUnavailable", link)

    def test_watermark_moves_only_on_success_and_bucket_pressure_slows_the_sweep(self):
        state = tmp_state(self)
        t, _ = transport(sh_route(3, headers={"X-Shopify-Shop-Api-Call-Limit": "38/40"}))
        slept = []
        reader = shopify_read.ShopifyReader(SH_HOST, "2025-10", {"X-Shopify-Access-Token": "x"},
                                            t, sleep=slept.append)
        result = reader.orders({})
        self.assertEqual(slept, [2.0])
        self.assertEqual(result["cursor"], "2026-11-13T10:02:00-05:00")
        bad, _ = transport(lambda u: (200, {"orders": "nope"}, {}))
        out = shopify_read.sweep(SH_ENV, state_path=state, transport=bad, now=NOW)
        self.assertEqual(out["error"], "SourceUnavailable")
        self.assertNotIn("cursor", vs.SweepState(state).get("shopify_orders"))

    def test_customers_stream_and_order_without_line_items_is_refused(self):
        t, _ = transport(lambda u: (200, {"customers": [
            {"id": 5, "email": "A@Example.Test", "first_name": "Ann", "last_name": "Lee",
             "updated_at": "2026-11-12T10:00:00-05:00", "currency": "USD"}]}, {}))
        out = shopify_read.sweep(SH_ENV, state_path=tmp_state(self), transport=t, now=NOW,
                                 stream="customers")
        self.assertEqual(out["records"][0]["properties"]["name"], "Ann Lee")
        with self.assertRaises(SourceUnavailable):
            shopify_read.map_order({"id": 1, "line_items": None})


GRANOLA_NOTE = {
    "id": "not_1d3tmYTlCICgjy", "title": "Weekly ops", "created_at": "2026-11-12T15:00:00Z",
    "updated_at": "2026-11-12T16:00:00Z",
    "owner": {"name": "Olive Owner", "email": "olive@example.test"},
    "attendees": [{"name": "Pat Peer", "email": "pat@example.test"}],
    "summary_markdown": ("# Summary\nWe reviewed stock.\n\n## Decisions\n- Move the lid order "
                         "to Friday\n- Hold the label change\n\n## Action items\n"
                         "- Pat Peer: send the revised quote\n- [Olive] call the carrier\n"
                         "- check the dock\n\n## Notes\n- Not an action item\n"),
    "transcript": [{"speaker": {"name": "Olive Owner"}, "start_time": "00:00:05",
                    "text": "Let's start."},
                   {"speaker": {"name": "Pat Peer"}, "start_time": "00:00:20",
                    "text": "Stock looks fine."}]}


def gr_route(notes, empty_transcript_id=None):
    def route(url):
        parts = urlsplit(url)
        if parts.path == "/v1/notes":
            q = parse_qs(parts.query)
            if "cursor" in q:
                return 200, {"notes": [{"id": notes[-1]["id"]}], "hasMore": False}, {}
            return 200, {"notes": [{"id": n["id"]} for n in notes[:-1]],
                         "hasMore": True, "cursor": "next_1"}, {}
        note = next(n for n in notes if n["id"] == parts.path.rsplit("/", 1)[1])
        return 200, note, {}
    return route


class Granola(unittest.TestCase):
    def test_note_becomes_a_marked_transcript_with_sectioned_decisions_and_actions(self):
        other = {**GRANOLA_NOTE, "id": "not_2d3tmYTlCICgjy"}
        t, _ = transport(gr_route([GRANOLA_NOTE, other]))
        out = granola_read.sweep(GR_ENV, state_path=tmp_state(self), transport=t, now=NOW)
        self.assertEqual(len(out["records"]), 2)
        note = out["records"][0]
        self.assertEqual([d["text"] for d in note["decisions"]],
                         ["Move the lid order to Friday", "Hold the label change"])
        self.assertEqual([(a["owner"], a["text"]) for a in note["action_items"]],
                         [("Pat Peer", "send the revised quote"), ("Olive", "call the carrier"),
                          (None, "check the dock")])
        self.assertTrue(all(d["citation"] == "granola://note/not_1d3tmYTlCICgjy"
                            for d in note["decisions"] + note["action_items"]))
        meeting = note["meeting"]
        self.assertEqual(meeting["schema"], mt.SCHEMA)
        self.assertIn("meeting", meeting["markings"])
        self.assertEqual(len(meeting["cues"]), 2)
        self.assertNotIn("synthetic-granola-key-value", json.dumps(out))

    def test_sensitive_notes_are_marked_hr_and_unreadable_notes_are_counted(self):
        pay = {**GRANOLA_NOTE, "id": "not_3d3tmYTlCICgjy", "transcript": [
            {"speaker": {"name": "Olive Owner"}, "start_time": "00:00:05",
             "text": "We need to talk about his salary."}]}
        empty = {**GRANOLA_NOTE, "id": "not_4d3tmYTlCICgjy", "transcript": []}
        t, _ = transport(gr_route([pay, empty]))
        out = granola_read.sweep(GR_ENV, state_path=tmp_state(self), transport=t, now=NOW)
        self.assertIn("hr", out["records"][0]["meeting"]["markings"])
        self.assertEqual(out["refused"], {"unknown_format": 1})
        self.assertEqual(len(out["records"]), 1)

    def test_text_outside_headed_sections_is_never_classified(self):
        found = granola_read.extract_sections("- Decided to buy lids\n## Notes\n- action: x",
                                              "not_1d3tmYTlCICgjy")
        self.assertEqual(found, {"decisions": [], "action_items": []})

    def test_a_note_id_outside_the_grammar_is_never_requested(self):
        t, rec = transport(lambda u: (200, {"notes": [{"id": "../../admin"}],
                                            "hasMore": False}, {}))
        out = granola_read.sweep(GR_ENV, state_path=tmp_state(self), transport=t, now=NOW)
        self.assertEqual(out["refused"], {"note_id_invalid": 1})
        self.assertEqual(len(rec.sent), 1)


def x12(sets, *, isa_ctrl="000000905", group_ctrl="1", elem="*", term="~", sub=">"):
    def seg(*e):
        return elem.join(e) + term
    isa = (f"ISA{elem}00{elem}{' ' * 10}{elem}00{elem}{' ' * 10}{elem}ZZ{elem}"
           f"{'SENDERID':<15}{elem}ZZ{elem}{'PETRAID':<15}{elem}261114{elem}1200{elem}U{elem}"
           f"00401{elem}{isa_ctrl}{elem}0{elem}P{elem}{sub}{term}")
    assert len(isa) == 106
    body = ""
    for n, (kind, segs) in enumerate(sets, 1):
        ctrl = f"{n:04d}"
        inner = [seg("ST", kind, ctrl)] + [seg(*s) for s in segs]
        body += "".join(inner) + seg("SE", str(len(inner) + 1), ctrl)
    gs = seg("GS", "PO", "SENDER", "PETRA", "20261114", "1200", group_ctrl, "X", "004010")
    return (isa + gs + body + seg("GE", str(len(sets)), group_ctrl)
            + seg("IEA", "1", isa_ctrl)).encode()


PO_850 = ("850", [("BEG", "00", "SA", "PO-7001", "", "20261113"), ("CUR", "BY", "USD"),
                  ("N1", "ST", "Dock One", "92", "DC01"),
                  ("PO1", "1", "24", "CA", "11.50", "PE", "VP", "SKU-A", "UP", "012345678905"),
                  ("PO1", "2", "6", "EA", "4.25", "PE", "BP", "CUST-9")])
ACK_855 = ("855", [("BAK", "00", "AD", "PO-7001", "20261113"),
                   ("ACK", "IA", "24", "CA"), ("ACK", "IR", "6", "EA")])
ASN_856 = ("856", [("BSN", "00", "SHIP-55", "20261114", "1030"),
                   ("TD5", "B", "2", "UPSN", "M", "UPS Ground"),
                   ("REF", "CN", "1Z999"), ("PRF", "PO-7001"), ("SN1", "", "24", "CA")])


class Edi(unittest.TestCase):
    def test_850_855_856_parse_to_their_ontology_records(self):
        records, skipped = edi_read.parse_interchange(
            x12([PO_850, ACK_855, ASN_856, ("997", [("AK1", "PO", "1")])]))
        self.assertEqual(skipped, {"997": 1})
        po, l1, l2, ack, asn = records
        self.assertEqual(po["properties"], {"customer_po": "PO-7001", "order_date": "2026-11-13",
                                            "purpose": "00", "currency": "USD"})
        self.assertEqual(l1["properties"]["unit_price"], {"amount": "11.50", "currency": "USD"})
        self.assertEqual(l1["product_ids"], {"VP": "SKU-A", "UP": "012345678905"})
        self.assertEqual(l2["properties"]["ordered_quantity"], "6")
        self.assertEqual([a["status"] for a in ack["lines"]], ["IA", "IR"])
        self.assertEqual((asn["properties"]["tracking_number"], asn["properties"]["carrier"],
                          asn["join_keys"]["customer_po"]), ("1Z999", "UPS Ground", "PO-7001"))
        self.assertEqual(po["join_keys"]["trading_partner_sender"], "SENDERID")

    def test_no_currency_means_no_price_and_two_pos_in_one_notice_never_join(self):
        no_cur = ("850", [s for s in PO_850[1] if s[0] != "CUR"])
        recs, _ = edi_read.parse_interchange(x12([no_cur]))
        self.assertIsNone(recs[1]["properties"]["unit_price"])
        two = ("856", ASN_856[1] + [("PRF", "PO-7002")])
        (asn,), _ = edi_read.parse_interchange(x12([two]))
        self.assertIsNone(asn["join_keys"]["customer_po"])
        self.assertTrue(asn["join_keys"]["ambiguous_customer_pos"])

    def test_a_malformed_interchange_is_refused_whole(self):
        good = x12([PO_850]).decode()
        cases = {
            "se_segment_count_wrong": good.replace("SE*7*", "SE*9*"),
            "iea_group_count_wrong": good.replace("IEA*1*", "IEA*2*"),
            "ge_set_count_wrong": good.replace("GE*1*", "GE*2*"),
            "iea_missing_or_control_mismatch": good.replace("IEA*1*000000905", "IEA*1*000000906"),
            "no_isa": "GS*PO~" + good,
            "850_no_po_number": good.replace("PO-7001", ""),
        }
        for reason, text in cases.items():
            with self.assertRaises(edi_read.EdiRefused, msg=reason) as ctx:
                edi_read.parse_interchange(text.encode())
            self.assertEqual(str(ctx.exception), reason)
        with self.assertRaises(edi_read.EdiRefused):
            edi_read.parse_interchange(b"x" * (edi_read.MAX_FILE_BYTES + 1))
        with self.assertRaises(edi_read.EdiRefused):
            edi_read.parse_interchange("ISA*é".encode())

    def test_other_separators_are_read_from_the_isa(self):
        recs, _ = edi_read.parse_interchange(x12([PO_850], elem="|", term="\n", sub=":"))
        self.assertEqual(recs[0]["properties"]["customer_po"], "PO-7001")

    def test_drop_folder_cardinality_dedupe_and_read_only(self):
        for n in (1, 10, 60):
            root = Path(tempfile.mkdtemp())
            self.addCleanup(lambda r=root: [p.unlink() for p in r.iterdir()] or r.rmdir())
            for i in range(n):
                po = ("850", [("BEG", "00", "SA", f"PO-{i}", "", "20261113")] + PO_850[1][1:])
                (root / f"in{i:03d}.edi").write_bytes(x12([po]))
            (root / "bad.edi").write_bytes(b"ISA*junk")
            (root / "notes.pdf").write_bytes(b"%PDF")
            env = {"PLX_EDI_DROP_DIR": str(root)}
            state = tmp_state(self)
            before = sorted((p.name, p.read_bytes()) for p in root.iterdir())
            out = edi_read.sweep(env, state_path=state, now=NOW)
            pos = [r for r in out["records"] if r["record_type"] == "EdiPurchaseOrder"]
            self.assertEqual(len(pos), n)
            self.assertEqual(len({r["citation"] for r in out["records"]}), len(out["records"]))
            self.assertEqual(out["refused"], {"no_isa": 1})
            self.assertEqual(out["files_read"], n + 1)
            again = edi_read.sweep(env, state_path=state, now=NOW)
            self.assertEqual((again["records"], again["files_read"]), ([], 0))
            self.assertEqual(before, sorted((p.name, p.read_bytes()) for p in root.iterdir()))

    def test_unset_or_missing_folder_is_blocked_and_symlinks_are_ignored(self):
        self.assertEqual(edi_read.sweep({"PLX_EDI_DROP_DIR": "/nonexistent-edi"},
                                        state_path=tmp_state(self))["blocked"], "setting_invalid")
        root = Path(tempfile.mkdtemp())
        outside = Path(tempfile.mkdtemp()) / "x.edi"
        outside.write_bytes(x12([PO_850]))
        (root / "link.edi").symlink_to(outside)
        self.assertEqual(edi_read.DropFolder(root).list_names(), [])

    def test_a_remote_source_plugs_in_through_the_protocol(self):
        class Remote:
            def list_names(self): return ["a.edi"]
            def read(self, name, max_bytes): return x12([PO_850])
        out = edi_read.sweep({}, state_path=tmp_state(self), source=Remote(), now=NOW)
        self.assertEqual(len(out["records"]), 3)


class People:
    """A people-exclusion stand-in (``meeting_transcripts.admit``'s protocol)."""

    unavailable = None

    def __init__(self, excluded=()):
        self.excluded = set(excluded)

    def decides(self, address):
        return True

    def is_excluded(self, address):
        return address in self.excluded


def capture_store(test):
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    audience = LiveSourceAudience({"mail:order@petrasoap.com": ["staff:orders"],
                                   "mail:olive@example.test": ["staff:olive"],
                                   "shopify:synthetic-shop": ["staff:orders"],
                                   "edi:drop": ["staff:orders"]}, tenant="plx")
    store = CaptureStore(Path(d.name) / "capture.sqlite", audience)
    test.addCleanup(store.close)
    return store


def seen(store, actor, kind, unit):
    """The records ``actor`` reads of one unit ([] when the unit is not visible)."""
    try:
        return store.records(actor, kind, unit, limit=1000)["items"]
    except CaptureError as exc:
        assert exc.code == "not_visible", exc.code
        return []


ORDERS = Actor("plx", frozenset({"staff:orders"}), "restricted")
OLIVE = Actor("plx", frozenset({"staff:olive"}), "restricted")
NOBODY = Actor("plx", frozenset({"staff:other"}), "restricted")


class CaptureStoreWrites(unittest.TestCase):
    """Sweeps save into the versioned capture store before their cursor moves; readers see
    only what the audience grants; a failed save never moves the cursor."""

    def units(self, store, kind):
        return sorted(store.units(kind))

    def test_zendesk_saves_by_order_mailbox_and_rereads_add_no_version(self):
        store, state = capture_store(self), tmp_state(self)
        t, _ = transport(zd_route())
        out = zendesk_read.sweep(ZD_ENV, state_path=state, transport=t, now=NOW, store=store)
        self.assertEqual(out["capture"]["saved"], 6)
        self.assertEqual(out["capture"]["new_versions"], 6)
        self.assertEqual(self.units(store, "zendesk_order"), ["zendesk:order@petrasoap.com"])
        rows = seen(store, ORDERS, "zendesk_order", "zendesk:order@petrasoap.com")
        self.assertEqual(len(rows), 6)
        self.assertNotIn("SECRETTOKEN", json.dumps(rows))
        self.assertEqual(seen(store, NOBODY, "zendesk_order", "zendesk:order@petrasoap.com"), [])
        # the same tickets read again (a reset cursor) refresh, never duplicate
        t2, _ = transport(zd_route())
        again = zendesk_read.sweep(ZD_ENV, state_path=tmp_state(self), transport=t2, now=NOW,
                                   store=store)
        self.assertEqual((again["capture"]["new_versions"], again["capture"]["unchanged"]),
                         (0, 6))
        run = store.run("zendesk_order", "zendesk:order@petrasoap.com")
        self.assertEqual((run["state"], run["reason"]), ("partial", "incremental"))

    def test_a_failed_save_keeps_the_cursor(self):
        state = tmp_state(self)

        class Broken:
            def bind_source(self, *a):
                raise CaptureError("binding_mismatch")
        t, _ = transport(zd_route())
        out = zendesk_read.sweep(ZD_ENV, state_path=state, transport=t, now=NOW,
                                 store=Broken())
        self.assertEqual((out["error"], out["capture_error"]), ("capture_failed",
                                                                "binding_mismatch"))
        self.assertFalse(out["complete"])
        self.assertNotIn("cursor", vs.SweepState(state).get("zendesk"))
        self.assertNotEqual(out["freshness"]["state"], "fresh")

    def test_a_store_bound_to_another_tenant_host_refuses(self):
        store = capture_store(self)
        t, _ = transport(zd_route())
        zendesk_read.sweep(ZD_ENV, state_path=tmp_state(self), transport=t, now=NOW,
                           store=store)
        other = {**ZD_ENV, "ZENDESK_SUBDOMAIN": "another"}
        t2, _ = transport(lambda u: (200, {"tickets": [], "after_cursor": "x",
                                           "end_of_stream": True}, {}))
        out = zendesk_read.sweep(other, state_path=tmp_state(self), transport=t2, now=NOW,
                                 store=store)
        self.assertEqual(out["capture_error"], "binding_mismatch")

    def test_shopify_cardinality_1_10_60_no_row_lost_or_duplicated(self):
        for n in (1, 10, 60):
            store = capture_store(self)
            t, _ = transport(sh_route(n))
            out = shopify_read.sweep(SH_ENV, state_path=tmp_state(self), transport=t, now=NOW,
                                     sleep=lambda s: None, store=store)
            self.assertNotIn("error", out, n)
            self.assertEqual(out["capture"]["saved"], len(out["records"]), n)
            self.assertEqual(out["capture"]["duplicate_paths"], 0)
            rows = seen(store, ORDERS, "shopify_record", "shopify:synthetic-shop")
            self.assertEqual(len(rows), len({r["citation"] for r in out["records"]}), n)
            self.assertEqual(seen(store, NOBODY, "shopify_record", "shopify:synthetic-shop"), [])

    def test_granola_saves_only_admitted_notes_under_their_owner(self):
        store = capture_store(self)
        pat_out = {**GRANOLA_NOTE, "id": "not_2d3tmYTlCICgjy"}
        t, _ = transport(gr_route([GRANOLA_NOTE, pat_out]))
        blocked = granola_read.sweep(GR_ENV, state_path=tmp_state(self), transport=t, now=NOW,
                                     store=store)
        self.assertEqual((blocked["blocked"], blocked["requests_sent"]),
                         ("people_exclusion_unavailable", 0))
        out = granola_read.sweep(GR_ENV, state_path=tmp_state(self), transport=t, now=NOW,
                                 store=store, people=People(excluded={"pat@example.test"}))
        # Pat is in both notes: neither is saved, both are counted
        self.assertEqual(out["capture"]["saved"], 0)
        self.assertEqual(out["capture"]["withheld"], {"excluded_person": 2})
        t2, _ = transport(gr_route([GRANOLA_NOTE, pat_out]))
        ok = granola_read.sweep(GR_ENV, state_path=tmp_state(self), transport=t2, now=NOW,
                                store=store, people=People())
        self.assertEqual(ok["capture"]["saved"], 2)
        self.assertEqual(self.units(store, "granola_note"), ["granola:olive@example.test"])
        self.assertEqual(len(seen(store, OLIVE, "granola_note", "granola:olive@example.test")), 2)
        self.assertEqual(seen(store, ORDERS, "granola_note", "granola:olive@example.test"), [])

    def test_edi_failed_save_remembers_no_file_and_a_good_save_is_idempotent(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in root.iterdir()] or root.rmdir())
        (root / "a.edi").write_bytes(x12([PO_850, ASN_856]))
        env, state = {"PLX_EDI_DROP_DIR": str(root)}, tmp_state(self)

        class Broken:
            def bind_source(self, *a):
                raise OSError("disk full")
        out = edi_read.sweep(env, state_path=state, now=NOW, store=Broken())
        self.assertEqual(out["error"], "capture_failed")
        store = capture_store(self)
        out = edi_read.sweep(env, state_path=state, now=NOW, store=store)
        self.assertEqual(out["files_read"], 1)           # read again after the failure
        self.assertEqual(out["capture"]["saved"], 4)
        self.assertEqual(len(seen(store, ORDERS, "edi_record", "edi:drop")), 4)

    def test_records_without_a_unit_or_citation_are_withheld_and_counted(self):
        store = capture_store(self)
        records = [{"citation": "shopify://order/1"}, {"citation": "zendesk://ticket/1"},
                   {"citation": "shopify://order/2"}, {"citation": "shopify://order/2",
                                                       "x": 1}]
        counts = cv.save(store, cv.SHOPIFY_KIND, records, lambda r: "synthetic-shop",
                         binding="b", source_api="t")
        self.assertEqual(counts["withheld"], {"path_invalid": 1})
        self.assertEqual((counts["saved"], counts["duplicate_paths"]), (2, 1))
        counts = cv.save(store, cv.ZENDESK_KIND, [{"citation": "zendesk://ticket/1"}],
                         lambda r: None, binding="b", source_api="t")
        self.assertEqual((counts["saved"], counts["withheld"]), (0, {"no_unit": 1}))


class LiveCheckSpecs(unittest.TestCase):
    """Each spec's declared ``json_fields`` are exactly what the module's probe emits."""

    def fields(self, spec_id):
        spec = json.loads((ROOT / f"devloop/live-checks/{spec_id}.json").read_text())
        self.assertEqual(spec["blocked_without_env"][0] in spec["optional_env"], True)
        return spec["expect"]["json_fields"], set(spec["record"])

    def test_probe_output_carries_every_declared_field_and_only_recorded_keys(self):
        t, _ = transport(zd_route())
        t2, _ = transport(sh_route(2))
        t3, _ = transport(gr_route([GRANOLA_NOTE, {**GRANOLA_NOTE, "id": "not_2d3tmYTlCICgjy"}]))
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in root.iterdir()] or root.rmdir())
        (root / "a.edi").write_bytes(x12([PO_850]))
        for spec_id, result in (
                ("zendesk.order_queue", zendesk_read.probe(ZD_ENV, transport=t)),
                ("shopify.orders", shopify_read.probe(SH_ENV, transport=t2)),
                ("granola.notes", granola_read.probe(GR_ENV, transport=t3)),
                ("edi.drop_folder", edi_read.probe({"PLX_EDI_DROP_DIR": str(root)}))):
            fields, recorded = self.fields(spec_id)
            self.assertTrue(result["ok"], (spec_id, result))
            self.assertEqual([f for f in fields if f not in result], [], spec_id)
            self.assertEqual(sorted(set(result) - recorded), [], spec_id)
            self.assertNotIn("records", result)


class BindingsAndFreshness(unittest.TestCase):
    def schema_types(self):
        types = {}
        for f in (ROOT / "ontology/schema/domains").glob("*.json"):
            for t in json.loads(f.read_text()).get("object_types", []):
                props = t.get("properties") or []
                types[t["id"]] = {p if isinstance(p, str) else p.get("name") or p.get("id")
                                  for p in (props if isinstance(props, list) else props)}
        return types

    def test_bindings_name_real_ontology_types_and_properties(self):
        types = self.schema_types()
        for module in (shopify_read, granola_read, zendesk_read, edi_read):
            for name, binding in module.BINDINGS.items():
                if binding["ontology_type"] is None:
                    self.assertTrue(binding.get("why_unbound"), name)
                    continue
                self.assertIn(binding["ontology_type"], types, name)
                for prop in binding["properties"].values():
                    self.assertIn(prop, types[binding["ontology_type"]], f"{name}.{prop}")

    def test_freshness_reads_the_last_complete_sweep_only(self):
        self.assertEqual(vs.freshness({}, NOW)["state"], "never_captured")
        self.assertEqual(vs.freshness({"last_complete_at": "2026-11-14T11:50:00Z"},
                                      NOW)["state"], "fresh")
        old = (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(vs.freshness({"last_complete_at": old}, NOW)["state"], "stale")
        self.assertEqual(vs.freshness({"last_complete_at": "garbage"}, NOW)["state"],
                         "never_captured")


if __name__ == "__main__":
    unittest.main()
