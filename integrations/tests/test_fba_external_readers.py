"""The FBA external readers, on injected
transports only, never a live read.

Boundary: what the one sender lets out (GET only on the Freightcom *test* host and the
SP-API region hosts, plus the single Login with Amazon refresh-token POST; every other
verb, host, path or query refused before I/O), what the readers map payloads to (the
ontology types they bind, Decimal money with its own currency, no URL or token in a
record), the conservative Amazon-shipment-id join across sources, and the live checks'
``credentials_absent`` block naming settings and never a value. All ids and SKUs are
synthetic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qsl, urlsplit

from verify import live_verify
from integrations import amazon_sp_read as sp
from integrations import freightcom_read as fc
from integrations import live_source_grammar as grammar
from integrations.live_source_guard import (
    FREIGHTCOM_PRODUCTION_HOST, GuardedTransport, ReadOnlyGuardError, _refuse_non_read_send,
    check_request)

ROOT = Path(__file__).resolve().parents[2]

MARKET = "ATESTMKT0001"
SHIP_A, SHIP_B = "FBA1TEST0001", "FBA1TEST0002"
FC_HOST = grammar.FREIGHTCOM_TEST
SP_HOST = grammar.SP_API_HOSTS["na"]
TOKEN_VALUE = "synthetic-freightcom-token-value"
SP_ENV = {"AMAZON_SP_API_LWA_CLIENT_ID": "synthetic-client-id-value",
          "AMAZON_SP_API_LWA_CLIENT_SECRET": "synthetic-client-secret-value",
          "AMAZON_SP_API_REFRESH_TOKEN": "synthetic-refresh-token-value",
          "AMAZON_SP_API_MARKETPLACE_IDS": MARKET}
ACCESS_TOKEN = "synthetic-access-token-value"


class Recorder:
    """An injected sender: records each request and answers from ``route``."""

    def __init__(self, route):
        self.route = route
        self.sent: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.sent.append((method, url, dict(headers), body))
        status, payload = self.route(method, url, body)
        return status, {"Content-Type": "application/json"}, json.dumps(payload).encode()


def _sp_route(n_inventory=3, page_size=25, inbound_rows=None):
    inventory = [{"sellerSku": f"TEST-SKU-{i}", "fnSku": f"X00TEST{i:04d}", "asin": "B0TEST0000",
                  "condition": "NewItem", "totalQuantity": 10 + i,
                  "inventoryDetails": {"fulfillableQuantity": 5 + i,
                                       "inboundShippedQuantity": 2,
                                       "reservedQuantity": {"totalReservedQuantity": 1}}}
                 for i in range(n_inventory)]
    inbound = inbound_rows if inbound_rows is not None else [
        {"ShipmentId": SHIP_A, "ShipmentName": "synthetic", "ShipmentStatus": "IN_TRANSIT",
         "DestinationFulfillmentCenterId": "TST1", "LabelPrepType": "SELLER_LABEL"}]

    def route(method, url, body):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        if method == "POST":
            return 200, {"access_token": ACCESS_TOKEN, "expires_in": 3600}
        if parts.path == "/fba/inbound/v0/shipments":
            return 200, {"payload": {"ShipmentData": inbound}}
        if parts.path.endswith("/items"):
            return 200, {"payload": {"ItemData": [
                {"ShipmentId": SHIP_A, "SellerSKU": "TEST-SKU-0", "QuantityShipped": 24,
                 "QuantityReceived": 0, "QuantityInCase": 12}]}}
        if parts.path == "/fba/inventory/v1/summaries":
            start = int(query.get("nextToken", "0"))
            page = inventory[start:start + page_size]
            more = start + page_size < len(inventory)
            return 200, {"payload": {"inventorySummaries": page},
                         "pagination": {"nextToken": str(start + page_size)} if more else {}}
        if parts.path == "/sales/v1/orderMetrics":
            return 200, {"payload": [{"interval": query["interval"], "unitCount": 7,
                                      "orderCount": 5,
                                      "totalSales": {"amount": 70.5, "currencyCode": "USD"}}]}
        return 404, {}
    return route


def _fc_route(bookings):
    def route(method, url, body):
        path = urlsplit(url).path
        if path == "/services":
            return 200, [{"id": "synthetic.ground"}]
        if path == "/shipments":
            return 200, {"shipments": [{"id": k} for k in bookings]}
        if path.endswith("/tracking-events"):
            return 200, {"events": [{"type": "in-transit", "when": "2026-10-01T00:00:00Z"}]}
        shipment = path.rsplit("/", 1)[-1]
        if shipment in bookings:
            return 200, {"shipment": bookings[shipment]}
        return 404, {}
    return route


def _booking(shipment_id, references, total=None):
    return {"id": shipment_id, "state": "booked", "primary_tracking_number": "PRO-TEST-1",
            "tracking_numbers": ["PRO-TEST-1"], "pro_number": "PRO-TEST-1", "scac": "TSTC",
            "labels": [{"type": "BOL", "url": "https://files.invalid/presigned?sig=abc"}],
            "rate": {"carrier_name": "Synthetic Freight", "total": total},
            "details": {"expected_ship_date": "2026-10-01",
                        "reference_codes": references, "notes": f"see {SHIP_B}"}}


class GuardEgress(unittest.TestCase):
    """Only GET plus the one LWA token POST leaves the guard."""

    def test_allowed_reads_pass_the_guard(self):
        self.assertEqual(check_request("GET", f"https://{FC_HOST}/shipment/fc-test-1"), FC_HOST)
        url = grammar.url("sp_api.na.inventory_summaries",
                          {"granularityType": "Marketplace", "granularityId": MARKET,
                           "marketplaceIds": MARKET, "details": "true"})
        self.assertEqual(check_request("GET", url), SP_HOST)
        form = {"grant_type": "refresh_token", "refresh_token": "r", "client_id": "c",
                "client_secret": "s"}
        self.assertEqual(check_request("POST", "https://api.amazon.com/auth/o2/token",
                                       body=form), "api.amazon.com")

    def test_other_verbs_hosts_paths_and_queries_are_refused(self):
        form = {"grant_type": "refresh_token", "refresh_token": "r", "client_id": "c",
                "client_secret": "s"}
        refused = [
            ("POST", f"https://{FC_HOST}/rate", {"a": "b"}),        # rating
            ("POST", f"https://{FC_HOST}/shipment", {"a": "b"}),    # booking
            ("GET", f"https://{FC_HOST}/rate/fc-test-1", None),     # rate payloads not read
            ("DELETE", f"https://{FC_HOST}/shipment/fc-test-1", None),
            ("GET", f"https://{FREIGHTCOM_PRODUCTION_HOST}/shipment/fc-test-1", None),
            ("GET", f"https://{FC_HOST}/shipment/fc-test-1?expand=all", None),
            ("GET", f"https://{FC_HOST}/shipment/..%2Fadmin", None),
            ("PUT", f"https://{SP_HOST}/fba/inbound/v0/shipments/{SHIP_A}", {"a": "b"}),
            ("POST", f"https://{SP_HOST}/fba/inbound/v0/plans", {"a": "b"}),
            ("GET", f"https://{SP_HOST}/fba/inbound/v0/shipments/{SHIP_A}/labels"
                    f"?MarketplaceId={MARKET}", None),             # getLabels' required query
            ("GET", f"https://{SP_HOST}/orders/v0/orders?MarketplaceIds={MARKET}", None),
            ("GET", f"https://{SP_HOST}/fba/inbound/v0/shipments?MarketplaceId={MARKET}"
                    "&QueryType=DATE_RANGE&Extra=1", None),
            ("GET", f"https://{SP_HOST}/fba/inbound/v0/shipments?MarketplaceId={MARKET}"
                    f"&MarketplaceId={MARKET}&QueryType=DATE_RANGE", None),
            ("GET", f"https://{SP_HOST}/fba/inbound/v0/shipments?MarketplaceId={MARKET}"
                    "&QueryType=NEXT_TOKEN", None),
            ("GET", f"https://{SP_HOST}/sales/v1/orderMetrics?marketplaceIds={MARKET}"
                    "&interval=2026-10-02T00:00:00Z--2026-10-01T00:00:00Z&granularity=Total",
             None),
            ("GET", "https://sellingpartnerapi-xx.amazon.com/fba/inventory/v1/summaries", None),
            ("GET", "https://api.amazon.com/auth/o2/token", None),
            ("POST", "https://api.amazon.com/auth/o2/token?x=1", form),
            ("POST", "https://api.amazon.com/auth/o2/other", form),
            ("POST", "https://api.amazon.com/auth/o2/token",
             {**form, "grant_type": "client_credentials"}),
            ("POST", "https://api.amazon.com/auth/o2/token", {**form, "scope": "sellingpartnerapi::notifications"}),
            ("POST", "https://api.amazon.com/auth/o2/token", {**form, "client_secret": ""}),
        ]
        for method, url, body in refused:
            with self.subTest(method=method, url=url, body=sorted(body or {})):
                sender = Recorder(lambda *a: (200, {}))
                transport = GuardedTransport(sender)
                with self.assertRaises(ReadOnlyGuardError):
                    transport.request(method, url, {}, form=body)
                self.assertEqual(sender.sent, [])

    def test_get_labels_passes_only_in_its_exact_form(self):
        """getLabels (inbound box/pallet labels): GET of one closed query, nothing near it."""
        query = {"MarketplaceId": MARKET, "PageType": "PackageLabel_Thermal",
                 "LabelType": "UNIQUE", "NumberOfPackages": "2"}
        url = grammar.url("sp_api.na.inbound_labels", query, shipment=SHIP_A)
        self.assertEqual(check_request("GET", url), SP_HOST)
        base = f"https://{SP_HOST}/fba/inbound/v0/shipments/{SHIP_A}/labels"
        good = "?MarketplaceId=ATESTMKT0001&PageType=PackageLabel_Thermal&LabelType=PALLET"
        self.assertEqual(check_request("GET", base + good + "&NumberOfPallets=1"), SP_HOST)
        neighbours = [
            ("GET", base + f"?MarketplaceId={MARKET}"),                    # no page/label type
            ("GET", base + good.replace("PALLET", "SHIPPING")),            # unknown label type
            ("GET", base + good.replace("PackageLabel_Thermal", "Plain")),  # unknown page type
            ("GET", base + good + "&NumberOfPackages=two"),
            ("GET", base + good + "&NumberOfPackages=0"),
            ("GET", base + good + "&PackageLabelsToPrint=CARTON1"),        # not in the grammar
            ("GET", base + good + "&LabelType=UNIQUE"),                    # repeated parameter
            ("GET", base + good + "&Extra=1"),
            ("GET", base.replace(SHIP_A, "NOTANID") + good),
            ("GET", base.replace(SHIP_A, SHIP_A.lower()) + good),
            ("GET", base + "/extra" + good),
            ("GET", base.replace("/labels", "/billOfLading") + f"?MarketplaceId={MARKET}"),
            ("GET", base.replace("/v0/", "/v1/") + good),
            ("POST", base + good), ("PUT", base + good), ("DELETE", base + good),
        ]
        for method, refused in neighbours:
            with self.subTest(method=method, url=refused):
                sender = Recorder(lambda *a: (200, {}))
                with self.assertRaises(ReadOnlyGuardError):
                    GuardedTransport(sender).request(method, refused, {},
                                                     form={"a": "b"} if method != "GET" else None)
                self.assertEqual(sender.sent, [])

    def test_last_line_sender_check_admits_only_the_lwa_form(self):
        good = b"grant_type=refresh_token&refresh_token=r&client_id=c&client_secret=s"
        _refuse_non_read_send("POST", "https://api.amazon.com/auth/o2/token", good)
        _refuse_non_read_send("GET", f"https://{FC_HOST}/services", None)
        for method, url, body in [
                ("POST", f"https://{FC_HOST}/rate", b"{}"),
                ("POST", f"https://{SP_HOST}/fba/inbound/v0/plans", good),
                ("POST", "https://api.amazon.com/auth/o2/token", good + b"&scope=x"),
                ("POST", "https://api.amazon.com/auth/o2/token", good + b"&client_id=d"),
                ("POST", "https://api.amazon.com/auth/o2/token",
                 good.replace(b"refresh_token&", b"authorization_code&", 1))]:
            with self.subTest(method=method, url=url):
                with self.assertRaises(ReadOnlyGuardError):
                    _refuse_non_read_send(method, url, body)


class AmazonReader(unittest.TestCase):
    def test_probe_sends_one_token_post_then_gets_only_and_prints_no_value(self):
        sender = Recorder(_sp_route())
        transport = GuardedTransport(sender)
        sleeps: list[float] = []
        out = sp.probe(SP_ENV, transport=transport, sleep=sleeps.append,
                       now=datetime(2026, 10, 19, tzinfo=timezone.utc))
        self.assertTrue(out["ok"], out)
        methods = [m for m, *_ in sender.sent]
        self.assertEqual(methods.count("POST"), 1)
        self.assertEqual(set(methods), {"GET", "POST"})
        self.assertEqual({urlsplit(u).hostname for _, u, *_ in sender.sent},
                         {"api.amazon.com", SP_HOST})
        self.assertTrue(all(h.get("x-amz-access-token") == ACCESS_TOKEN
                            for m, _u, h, _b in sender.sent if m == "GET"))
        self.assertEqual((out["inbound_shipments"], out["inbound_items"], out["inventory_skus"],
                          out["sales_skus_read"], out["sales_skus_with_units"]), (1, 1, 3, 3, 3))
        self.assertEqual(out["evidence_label"], "injected-transport")
        text = json.dumps(out)
        for secret in (*SP_ENV.values(), ACCESS_TOKEN):
            if secret != MARKET:
                self.assertNotIn(secret, text)
        # getOrderMetrics is throttled to 0.5 rps: a pause before every call but the first.
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(0 < s <= sp.ORDER_METRICS_INTERVAL_SECONDS for s in sleeps))

    def test_sales_window_is_the_30_days_before_now_for_one_sku(self):
        sender = Recorder(_sp_route())
        reader = sp.SpApiReader(sp.settings_from_env(SP_ENV), GuardedTransport(sender))
        now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        sales = reader.sku_sales(MARKET, "TEST-SKU-0", now=now)
        query = dict(parse_qsl(urlsplit(sender.sent[-1][1]).query))
        self.assertEqual(query["interval"], "2026-09-19T12:00:00Z--2026-10-19T12:00:00Z")
        self.assertEqual((query["sku"], query["granularity"]), ("TEST-SKU-0", "Total"))
        self.assertEqual(sales["amazon"]["units_ordered"], "7")
        self.assertEqual(sales["amazon"]["total_sales"], {"amount": "70.5", "currency": "USD"})
        self.assertEqual(sales["record_type"], "SkuSalesMetric")
        self.assertIsNone(sp.map_sku_sales([], "TEST-SKU-0", MARKET, "a", "b")["amazon"][
            "units_ordered"])
        self.assertIsNone(sp._money({"amount": "1.00"}))  # no currency, no money

    def test_inventory_pages_lose_and_duplicate_no_row_and_report_a_page_cap(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                reader = sp.SpApiReader(sp.settings_from_env(SP_ENV),
                                        GuardedTransport(Recorder(_sp_route(n, page_size=7))))
                got = reader.inventory_summaries(MARKET)
                skus = [r["join_keys"]["seller_sku"] for r in got["records"]]
                self.assertEqual(skus, [f"TEST-SKU-{i}" for i in range(n)])
                self.assertTrue(got["completeness"]["complete"])
        reader = sp.SpApiReader(sp.settings_from_env(SP_ENV),
                                GuardedTransport(Recorder(_sp_route(60, page_size=7))))
        capped = reader.inventory_summaries(MARKET, max_pages=3)
        self.assertEqual(len(capped["records"]), 21)
        self.assertEqual(capped["completeness"], {"complete": False, "pages": 3,
                                                  "reason": "page_cap"})

    def test_labels_record_says_a_document_exists_and_never_keeps_its_url(self):
        url = "https://labels.invalid/presigned?X-Amz-Signature=synthetic"
        route = lambda m, u, b: ((200, {"access_token": ACCESS_TOKEN}) if m == "POST"
                                 else (200, {"payload": {"DownloadURL": url}}))
        sender = Recorder(route)
        reader = sp.SpApiReader(sp.settings_from_env(SP_ENV), GuardedTransport(sender))
        got = reader.inbound_labels(MARKET, SHIP_A.lower(), page_type="PackageLabel_Thermal",
                                    label_type="UNIQUE", number_of_packages=2)
        self.assertEqual([m for m, *_ in sender.sent], ["POST", "GET"])
        self.assertEqual(urlsplit(sender.sent[-1][1]).path,
                         f"/fba/inbound/v0/shipments/{SHIP_A}/labels")
        self.assertEqual(got["join_keys"], {"amazon_shipment_id": SHIP_A})
        self.assertEqual(got["citation"], f"amazon-sp://{MARKET}/inbound/{SHIP_A}/labels/UNIQUE")
        self.assertTrue(got["amazon"]["document_available"])
        self.assertNotIn("labels.invalid", json.dumps(got))
        self.assertNotIn("Signature", json.dumps(got))
        empty = Recorder(lambda m, u, b: ((200, {"access_token": ACCESS_TOKEN}) if m == "POST"
                                          else (200, {"payload": {}})))
        reader = sp.SpApiReader(sp.settings_from_env(SP_ENV), GuardedTransport(empty))
        self.assertFalse(reader.inbound_labels(MARKET, SHIP_A, page_type="PackageLabel_Thermal",
                                               label_type="UNIQUE")["amazon"]["document_available"])
        for bad in ({"page_type": "Plain", "label_type": "UNIQUE"},
                    {"page_type": "PackageLabel_Thermal", "label_type": "UNIQUE",
                     "number_of_packages": 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reader.inbound_labels(MARKET, SHIP_A, **bad)
        with self.assertRaises(ValueError):
            reader.inbound_labels(MARKET, "NOTANID", page_type="PackageLabel_Thermal",
                                  label_type="UNIQUE")
        bad_body = Recorder(lambda m, u, b: ((200, {"access_token": ACCESS_TOKEN}) if m == "POST"
                                             else (200, {"payload": []})))
        reader = sp.SpApiReader(sp.settings_from_env(SP_ENV), GuardedTransport(bad_body))
        with self.assertRaises(sp.SourceUnavailable):
            reader.inbound_labels(MARKET, SHIP_A, page_type="PackageLabel_Thermal",
                                  label_type="UNIQUE")

    def test_unexpected_payload_fails_closed(self):
        route = lambda m, u, b: ((200, {"access_token": ACCESS_TOKEN}) if m == "POST"
                                 else (200, {"payload": {"Shipments": []}}))
        out = sp.probe(SP_ENV, transport=GuardedTransport(Recorder(route)))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "SourceUnavailable")


class FreightcomReader(unittest.TestCase):
    def test_probe_sends_gets_only_on_the_test_host_and_prints_no_value(self):
        bookings = {"fc-test-1": _booking("fc-test-1", [{"type": "po", "value": SHIP_A}])}
        sender = Recorder(_fc_route(bookings))
        out = fc.probe({"FREIGHTCOM_API_TOKEN": TOKEN_VALUE},
                       transport=GuardedTransport(sender))
        self.assertTrue(out["ok"], out)
        self.assertEqual({m for m, *_ in sender.sent}, {"GET"})
        self.assertEqual({urlsplit(u).hostname for _, u, *_ in sender.sent}, {FC_HOST})
        self.assertTrue(all(h["Authorization"] == TOKEN_VALUE for _m, _u, h, _b in sender.sent))
        self.assertEqual((out["shipments"], out["bookings_read"], out["with_amazon_shipment_id"],
                          out["with_bol"]), (1, 1, 1, 1))
        self.assertNotIn(TOKEN_VALUE, json.dumps(out))

    def test_booking_maps_to_shipment_with_licence_flag_and_cents_as_decimal(self):
        record = fc.map_booking({"shipment": _booking(
            "fc-test-1", [{"value": " fba1test0001 "}], {"currency": "CAD", "value": "123456"})})
        self.assertEqual((record["record_type"], record["ontology_type"]),
                         ("FreightBooking", "Shipment"))
        self.assertEqual(record["join_keys"]["amazon_shipment_id"], SHIP_A)
        self.assertEqual(record["restricted"]["freight_cost"],
                         {"amount": "1234.56", "currency": "CAD"})
        self.assertTrue(record["licence"]["restricted"])
        self.assertEqual(record["freightcom"]["scac"], "TSTC")
        self.assertNotIn("presigned", json.dumps(record))  # document URLs never kept
        for total in ({"value": "100"}, {"currency": "CAD", "value": "12.50"},
                      {"currency": "cad", "value": "100"}, None):
            with self.subTest(total=total):
                self.assertIsNone(fc.map_booking(_booking("fc-test-1", [], total))[
                    "restricted"]["freight_cost"])

    def test_production_base_url_is_refused_before_any_request(self):
        sender = Recorder(_fc_route({}))
        for base in (f"https://{FREIGHTCOM_PRODUCTION_HOST}", f"http://{FC_HOST}",
                     f"https://{FC_HOST}/v2", f"https://{FC_HOST}:8443"):
            with self.subTest(base=base):
                out = fc.probe({"FREIGHTCOM_API_TOKEN": TOKEN_VALUE,
                                "FREIGHTCOM_API_BASE_URL": base},
                               transport=GuardedTransport(sender))
                self.assertEqual(out["blocked"], "setting_invalid")
                self.assertEqual(out["invalid_settings"], ["FREIGHTCOM_API_BASE_URL"])
        self.assertEqual(sender.sent, [])


class OntologyBinding(unittest.TestCase):
    def test_bound_types_and_properties_exist_in_the_schema(self):
        types = {}
        for path in sorted((ROOT / "ontology/schema/domains").glob("*.json")):
            for obj in json.loads(path.read_text()).get("object_types", []):
                types.setdefault(obj["id"], {p["name"] for p in obj.get("properties", [])})
        for bindings in (sp.BINDINGS, fc.BINDINGS):
            for record_type, binding in bindings.items():
                with self.subTest(record_type=record_type):
                    if binding["ontology_type"] is None:
                        self.assertTrue(binding["why_unbound"])
                        continue
                    self.assertIn(binding["ontology_type"], types)
                    self.assertLessEqual(set(binding["properties"].values()),
                                         types[binding["ontology_type"]])

    def test_mapped_records_fill_only_bound_properties(self):
        inbound = sp.map_inbound_shipment({"ShipmentId": SHIP_A, "ShipmentStatus": "WORKING"},
                                          MARKET)
        booking = fc.map_booking(_booking("fc-test-1", []))
        line = sp.map_inbound_item({"ShipmentId": SHIP_A, "SellerSKU": "TEST-SKU-0",
                                    "QuantityShipped": 24}, SHIP_A, MARKET)
        for record, bindings in ((inbound, sp.BINDINGS), (line, sp.BINDINGS),
                                 (booking, fc.BINDINGS)):
            self.assertEqual(set(record["properties"]),
                             set(bindings[record["record_type"]]["properties"]))
        self.assertEqual(line["properties"]["shipped_quantity"], "24")


class CrossSourceJoin(unittest.TestCase):
    def test_amazon_shipment_id_joins_exactly_and_conservatively(self):
        inbound = [sp.map_inbound_shipment({"ShipmentId": s, "ShipmentStatus": "SHIPPED"}, MARKET)
                   for s in (SHIP_A, SHIP_B, "not-an-id")]
        bookings = [fc.map_booking(_booking("fc-test-1", [{"value": SHIP_A.lower()}])),
                    # two ids in the declared references: ambiguous, never joined
                    fc.map_booking(_booking("fc-test-2", [{"value": SHIP_A}, {"value": SHIP_B}])),
                    # SHIP_B appears only in free-text notes: not a declared reference
                    fc.map_booking(_booking("fc-test-3", []))]
        result = sp.join_on_amazon_shipment_id(inbound, bookings)
        self.assertEqual(result["joined"][SHIP_A], {
            "amazon_sp_api": [f"amazon-sp://{MARKET}/inbound/{SHIP_A}"],
            "freightcom": ["freightcom://shipment/fc-test-1"]})
        self.assertEqual(result["joined"][SHIP_B], {
            "amazon_sp_api": [f"amazon-sp://{MARKET}/inbound/{SHIP_B}"]})
        self.assertEqual(result["ambiguous"], ["freightcom://shipment/fc-test-2"])
        self.assertEqual(len(result["unjoined"]), 2)

    def test_an_item_naming_another_shipment_does_not_join(self):
        line = sp.map_inbound_item({"ShipmentId": SHIP_B, "SellerSKU": "TEST-SKU-0"},
                                   SHIP_A, MARKET)
        self.assertIsNone(line["join_keys"]["amazon_shipment_id"])


class CredentialsAbsentLiveChecks(unittest.TestCase):
    """The live specs block ``credentials_absent`` naming the settings, never a value."""

    def test_probes_block_naming_missing_settings_without_io(self):
        sender = Recorder(lambda *a: (200, {}))
        cases = [(sp.probe, {}, list(sp.REQUIRED_SETTINGS)),
                 (sp.probe, {"AMAZON_SP_API_LWA_CLIENT_SECRET": "synthetic-only-value"},
                  ["AMAZON_SP_API_LWA_CLIENT_ID", "AMAZON_SP_API_REFRESH_TOKEN",
                   "AMAZON_SP_API_MARKETPLACE_IDS"]),
                 (fc.probe, {"FREIGHTCOM_API_BASE_URL": f"https://{FC_HOST}"},
                  ["FREIGHTCOM_API_TOKEN"])]
        for probe, env, missing in cases:
            with self.subTest(probe=probe.__module__, env=sorted(env)):
                out = probe(env, transport=GuardedTransport(sender))
                self.assertEqual(out["blocked"], "credentials_absent")
                self.assertEqual(out["missing_settings"], missing)
                self.assertNotIn("synthetic-only-value", json.dumps(out))
        self.assertEqual(sender.sent, [])

    def test_invalid_region_or_marketplace_is_refused_before_io(self):
        for env in ({**SP_ENV, "AMAZON_SP_API_REGION": "us"},
                    {**SP_ENV, "AMAZON_SP_API_MARKETPLACE_IDS": "bad id"},
                    {**SP_ENV, "AMAZON_SP_API_MARKETPLACE_IDS": f"{MARKET},{MARKET}"}):
            with self.subTest(env=sorted(env.items())[-1][0]):
                out = sp.probe(env, transport=GuardedTransport(Recorder(lambda *a: (200, {}))))
                self.assertEqual(out["blocked"], "setting_invalid")
                self.assertEqual(out["requests_sent"], 0)

    def test_live_specs_block_credentials_absent_naming_unset_settings_only(self):
        for spec_id, source in (("freightcom.bookings", "freightcom"),
                                ("amazon.inbound_shipments", "amazon-sp-api")):
            spec = json.loads((ROOT / f"devloop/live-checks/{spec_id}.json").read_text())
            live_verify.validate_spec(spec)
            required = list(fc.REQUIRED_SETTINGS if source == "freightcom"
                            else sp.REQUIRED_SETTINGS)
            self.assertEqual(spec["blocked_without_env"], required)
            # every setting reaches the command (declared), required or optional
            self.assertEqual(set(spec["optional_env"]),
                             set(fc.SETTINGS if source == "freightcom" else sp.SETTINGS))
            partial = {name: "synthetic-only-value" for name in required[1:]}
            for env, missing in (({}, required), (partial, required[:1])):
                with self.subTest(spec=spec_id, set=sorted(env)):
                    entry = live_verify.run_spec(spec, environ={"PATH": "/usr/bin:/bin", **env})
                    self.assertEqual(entry["result"], "blocked", entry)
                    self.assertEqual(entry["detail"]["reason"],
                                     f"upstream {source} credentials_absent")
                    self.assertEqual(entry["detail"]["cause"], "later_ask")
                    self.assertEqual(entry["detail"]["missing_settings"], missing)
                    self.assertNotIn("exit_code", entry)  # no command started
                    self.assertNotIn("synthetic-only-value", json.dumps(entry))

    def test_probe_cli_blocks_with_exit_zero_and_names_only(self):
        import subprocess
        for module, missing in (("integrations.freightcom_read", ["FREIGHTCOM_API_TOKEN"]),
                                ("integrations.amazon_sp_read", list(sp.REQUIRED_SETTINGS))):
            with self.subTest(module=module):
                done = subprocess.run([sys.executable, "-m", module, "probe"], cwd=ROOT,
                                      env={"PATH": "/usr/bin:/bin"}, capture_output=True,
                                      text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
                out = json.loads(done.stdout)
                self.assertEqual((out["blocked"], out["missing_settings"], out["requests_sent"]),
                                 ("credentials_absent", missing, 0))

    def test_injected_transport_output_never_satisfies_a_live_spec(self):
        out = sp.probe(SP_ENV, transport=GuardedTransport(Recorder(_sp_route())), sleep=lambda s: None)
        spec = json.loads((ROOT / "devloop/live-checks/amazon.inbound_shipments.json").read_text())
        failures = live_verify.check_expectations(spec, 0, json.loads(json.dumps(out)))
        self.assertTrue(any("evidence_label" in f for f in failures), failures)
        bookings = {"fc-test-1": _booking("fc-test-1", [])}
        out = fc.probe({"FREIGHTCOM_API_TOKEN": TOKEN_VALUE},
                       transport=GuardedTransport(Recorder(_fc_route(bookings))))
        spec = json.loads((ROOT / "devloop/live-checks/freightcom.bookings.json").read_text())
        failures = live_verify.check_expectations(spec, 0, json.loads(json.dumps(out)))
        self.assertEqual([f for f in failures if "evidence_label" not in f], [])
        self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main()
