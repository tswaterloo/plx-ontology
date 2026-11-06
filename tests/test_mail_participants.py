"""plx.documents participant: the mail from or to an address, a domain or a record's
addresses, newest first, with its direction, under the same gates as every document.

The store is the evidence-tools fixture (``tests.test_ontology_tools_evidence``) with
mail senders and recipients and a supplier whose addresses are PII-marked. All
addresses, mailboxes and texts are synthetic. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import json
import unittest

from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_evidence as ev
from tests import test_ontology_tools_evidence as fx

OURS = "plx-example.test"
VENDOR = "acme-supply.test"
S1 = "Supplier:portal.fm_supplier:s-1"
S2 = "Supplier:bc.vendors:v-1"          # S1's firm twin
S3 = "Supplier:portal.fm_supplier:s-3"  # holds only a shared-provider address
S4 = "Supplier:portal.fm_supplier:s-4"  # holds only our own colleague's address

DOMAIN = json.loads(json.dumps(fx.DOMAIN))
DOMAIN["object_types"][1]["properties"] += [
    {"name": "sender", "value_type": "text"}, {"name": "to_addresses", "value_type": "text"},
    {"name": "cc_addresses", "value_type": "text"}]
DOMAIN["object_types"].append(fx._type(
    "Supplier", [("name", "text"), ("rep_email", "text"), ("csr_email", "text")],
    [("portal.fm_supplier", "portal_api"), ("bc.vendors", "bc")], display=["name"]))
DOMAIN["link_types"].append(
    fx._link("Supplier.same_record_as", "Supplier", "Supplier", "same_record"))
SCHEMA = ot.SchemaView.from_documents([DOMAIN])

POLICY = ot.ReadPolicy(
    tenant="plx",
    audiences={"api:*": {"ops", "full", "nopii"}, "bc:*": {"ops", "full", "nopii"},
               "mail:mbx-a": {"ops", "full", "nopii"}, "mail:*": {"full"}},
    markings={"finance": {"full"}, "pii": {"ops", "full"}})
OPS, FULL = fx.OPS, fx.FULL
NOPII = Actor("plx", frozenset({"nopii"}))


class Reader(fx.StubReader):
    def __init__(self, tenant=frozenset({OURS})):
        super().__init__()
        self.tenant = tenant

    def tenant_domains(self):
        return self.tenant


def handle(mailbox, key):
    return f"graph-mail://{mailbox}/{key}@ck1"


def message(store, key, *, mailbox="mbx-a", sender, to=(), cc=(), at, subject=None):
    oid = f"Email:graph_mail.messages:{key}"
    store.obj(oid, f"mail:{mailbox}", handle(mailbox, key), "mail")
    store.val(oid, "subject", "text", subject or f"Message {key}")
    store.val(oid, "received_at", "timestamp", at)
    store.val(oid, "sender", "text", sender)
    store.val(oid, "to_addresses", "text", json.dumps(list(to)))
    store.val(oid, "cc_addresses", "text", json.dumps(list(cc)))
    return oid


def pii(store, oid, prop, value):
    """A PII-marked value (``Store.val`` gives a value its object's markings)."""
    handle_, lineage, _ = store.handles[oid]
    store.conn.execute(
        "INSERT INTO object_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, prop, "text", value, None, None, None, None, None, handle_, f"row.{prop}",
         fx._h(f"{handle_}|{prop}|{value}"), lineage, oid.split(":")[1], "trim", 0,
         '["pii"]', fx.T0, None))


def mail(store):
    vendor = f"rep@{VENDOR}"
    message(store, "v-old", sender=vendor, to=[f"buyer@{OURS}"], at="2099-01-10T09:00:00Z")
    message(store, "v-new", sender=f"buyer@{OURS}", to=[f"other@{OURS}"], cc=[vendor],
            at="2099-02-10T09:00:00Z")
    message(store, "v-sub", sender=f"ops@eu.{VENDOR}", to=[f"buyer@{OURS}"],
            at="2099-01-20T09:00:00Z")
    # Look-alikes of the vendor's domain and address: never a match.
    message(store, "fake", sender=f"rep@{VENDOR}.evil.test", to=[f"buyer@{OURS}"],
            at="2099-02-20T09:00:00Z")
    message(store, "near", sender=f"xrep@not{VENDOR}", to=[f"buyer@{OURS}"],
            at="2099-02-21T09:00:00Z")
    message(store, "other", sender=f"buyer@{OURS}", to=["someone@elsewhere.test"],
            at="2099-02-25T09:00:00Z")
    message(store, "ours-only", sender=f"other@{OURS}", to=["someone@elsewhere.test"],
            at="2099-02-26T09:00:00Z")
    # The newest vendor message is in a mailbox only FULL may read.
    message(store, "v-private", mailbox="mbx-b", sender=vendor, to=[f"ceo@{OURS}"],
            at="2099-02-28T09:00:00Z")
    # A message naming the order (fixture P1) the vendor sent.
    message(store, "v-po", sender=vendor, to=[f"buyer@{OURS}"], at="2099-01-05T09:00:00Z",
            subject=f"Confirming {fx.NUMBER}")
    start = len("Confirming ")
    store.mention("l-vpo", "Email:graph_mail.messages:v-po", fx.P1, "row.subject",
                  (start, start + len(fx.NUMBER)), "gv")
    # A supplier with the vendor's rep address; its BC twin holds the CSR address.
    store.obj(S1, "api:public.supplier", fx.api("supplier", "s-1"), "fm_bronze")
    store.val(S1, "name", "text", "Acme Supply")
    pii(store, S1, "rep_email", f"Rep <rep@{VENDOR}>; backup@{VENDOR}")
    store.obj(S2, f"bc:{fx.CO}/vendors", fx.make_handle(
        fx.BC_KIND, ("production", fx.CO, "vendors", "1e0f9a8b-7c6d-4a3b-8c2d-9b8a7c6d5e4f"),
        "00000000000000a2", 1), "bc_manufacturing")
    pii(store, S2, "csr_email", "csr@shared-mail.test")
    store.link("l-sup", "Supplier.same_record_as", S1, S2, "verified", "same_record",
               reason=[{"feature": "supplier_code", "agrees": True,
                        "citations": [store.cite(S1, "row.name"), store.cite(S2, "row.name")]}])
    store.obj(S3, "api:public.supplier", fx.api("supplier", "s-3"), "fm_bronze")
    pii(store, S3, "rep_email", "someone@gmail.com")
    store.obj(S4, "api:public.supplier", fx.api("supplier", "s-4"), "fm_bronze")
    pii(store, S4, "rep_email", f"buyer@{OURS}")


def tools(reader=None, extend=None):
    store = fx.build()
    store.conn.execute("UPDATE store_meta SET value = ? WHERE key = 'schema_digest'",
                       ("sha256:" + SCHEMA.digest_hex,))
    mail(store)
    if extend is not None:
        extend(store)
    store.conn.commit()
    base = ot.OntologyTools(ot.StoreReader(store.conn), POLICY, item_gate=lambda a, h: True,
                            schema=SCHEMA, grammar=fx.identifiers, contracts=fx.CONTRACTS,
                            clock=lambda: fx.NOW)
    return ev.EvidenceTools(base, reader=reader if reader is not None else Reader())


class ParticipantTests(unittest.TestCase):
    def setUp(self):
        self.tools = tools()

    def call(self, actor=OPS, tools_=None, **arguments):
        out = (tools_ or self.tools).call("plx.documents", arguments, actor)
        return fx.ok(self, "plx.documents", out)

    def listed(self, out):
        return [(d["citation"].split("/")[3].split("@")[0], d.get("direction"),
                 d.get("matched_by")) for d in out["documents"]]

    def test_an_address_lists_its_mail_newest_first_with_direction(self):
        out = self.call(participant=f"Rep@{VENDOR}")
        self.assertEqual(self.listed(out), [("v-new", "to_them", "address"),
                                            ("v-old", "from_them", "address"),
                                            ("v-po", "from_them", "address")])
        self.assertEqual([d["date"] for d in out["documents"]],
                         sorted((d["date"] for d in out["documents"]), reverse=True))
        self.assertEqual(out["searched"]["participants"], [f"rep@{VENDOR}"])
        self.assertEqual(out["searched"]["sources"], ["graph_mail"])

    def test_a_domain_matches_its_subdomains_never_look_alikes(self):
        out = self.call(participant=f"@{VENDOR}")
        self.assertEqual([k for k, _, _ in self.listed(out)], ["v-new", "v-sub", "v-old",
                                                                "v-po"])
        self.assertEqual({m for _, _, m in self.listed(out)}, {"domain"})

    def test_mail_to_an_outside_party_counts_only_when_one_of_us_sent_it(self):
        def extend(store):
            # a third party's mailing that lists the vendor among its recipients
            message(store, "mass", sender="news@elsewhere.test",
                    to=[f"rep@{VENDOR}", "x@elsewhere.test"], at="2099-03-01T09:00:00Z")
            message(store, "mass-cc", sender="news@elsewhere.test", to=[f"buyer@{OURS}"],
                    cc=[f"ops@{VENDOR}"], at="2099-03-02T09:00:00Z")
            # our reply to the vendor, the newest message with them
            message(store, "reply", sender=f"Buyer <buyer@{OURS}>", to=[f"rep@{VENDOR}"],
                    at="2099-03-03T09:00:00Z")
        both = tools(extend=extend)
        for participant in (f"rep@{VENDOR}", f"@{VENDOR}", S1):
            with self.subTest(participant=participant):
                out = self.call(tools_=both, participant=participant)
                keys = [k for k, _, _ in self.listed(out)]
                self.assertEqual(keys[0], "reply")
                self.assertEqual(self.listed(out)[0][1], "to_them")
                self.assertNotIn("mass", keys)
                self.assertNotIn("mass-cc", keys)
                self.assertIn("v-new", keys)   # ours, the vendor in CC
        # a colleague participant: mail to them from outside still counts
        colleague = [k for k, _, _ in self.listed(self.call(tools_=both,
                                                            participant=f"buyer@{OURS}"))]
        self.assertIn("mass-cc", colleague)
        self.assertIn("v-old", colleague)

    def test_our_help_desk_counts_as_us_and_a_free_mail_domain_never_does(self):
        """Our help desk's host under our name (``plx-example.zendesk.com`` for
        ``plx-example.test``) answering the vendor is our mail with them; a free-mail
        sender's mailing to the vendor is not, even when one of our mailboxes has an
        address at that provider (it is not one of our own domains)."""
        def extend(store):
            message(store, "desk", sender="support@plx-example.zendesk.com",
                    to=[f"rep@{VENDOR}"], at="2099-03-04T09:00:00Z")
            message(store, "free", sender="someone@gmail.com",
                    to=[f"rep@{VENDOR}", "x@gmail.com"], at="2099-03-05T09:00:00Z")
            message(store, "lookalike", sender="support@plx.zendesk.com",
                    to=[f"rep@{VENDOR}"], at="2099-03-06T09:00:00Z")
        for tenant in (frozenset({OURS}), frozenset({OURS, "gmail.com"})):
            with self.subTest(tenant=sorted(tenant)):
                out = self.call(tools_=tools(Reader(tenant=tenant), extend=extend),
                                participant=f"@{VENDOR}")
                keys = [k for k, _, _ in self.listed(out)]
                self.assertEqual(self.listed(out)[0], ("desk", "to_them", "domain"))
                self.assertNotIn("free", keys)
                self.assertNotIn("lookalike", keys)

    def test_our_own_domains_never_include_a_free_mail_provider(self):
        from types import SimpleNamespace
        from operating.ontology_tools_evidence import RuntimeCitations
        scope = lambda *d: SimpleNamespace(mail_scope=SimpleNamespace(internal_domains=d))
        self.assertEqual(RuntimeCitations(scope(OURS, "gmail.com", "Hotmail.co.uk"))
                         .tenant_domains(), frozenset({OURS}))
        self.assertIsNone(RuntimeCitations(scope("gmail.com")).tenant_domains())

    def test_unknown_own_domains_leave_received_only_mail_out_and_say_so(self):
        out = self.call(tools_=tools(Reader(tenant=None)), participant=f"rep@{VENDOR}")
        self.assertEqual(self.listed(out), [("v-old", "from_them", "address"),
                                            ("v-po", "from_them", "address")])
        self.assertIn("recipient_matches_unchecked", [g["reason"] for g in out["gaps"]])

    def test_a_sender_the_reader_may_not_see_never_makes_mail_ours(self):
        def extend(store):
            oid = message(store, "hidden", sender="placeholder", to=[f"rep@{VENDOR}"],
                          at="2099-03-03T09:00:00Z")
            store.conn.execute("DELETE FROM object_values WHERE object_id = ? AND "
                               "property = 'sender'", (oid,))
            pii(store, oid, "sender", f"buyer@{OURS}")
        hidden = tools(extend=extend)
        self.assertEqual(self.listed(self.call(tools_=hidden, participant=f"rep@{VENDOR}"))
                         [0][0], "hidden")
        self.assertNotIn("hidden", [k for k, _, _ in self.listed(
            self.call(NOPII, tools_=hidden, participant=f"rep@{VENDOR}"))])

    def test_a_mailbox_the_reader_may_not_read_stays_hidden(self):
        ops = self.call(participant=f"rep@{VENDOR}")
        self.assertNotIn("v-private", [k for k, _, _ in self.listed(ops)])
        self.assertIn("audience_not_granted", [g["reason"] for g in ops["gaps"]])
        full = self.call(FULL, participant=f"rep@{VENDOR}")
        self.assertEqual(self.listed(full)[0], ("v-private", "from_them", "address"))

    def test_a_record_reads_its_and_its_twins_addresses_and_their_domains(self):
        out = self.call(participant=S1)
        self.assertEqual([k for k, _, _ in self.listed(out)], ["v-new", "v-sub", "v-old",
                                                                "v-po"])
        roles = {k: (d, m) for k, d, m in self.listed(out)}
        self.assertEqual(roles["v-sub"], ("from_them", "domain"))
        self.assertEqual(roles["v-new"], ("to_them", "address"))
        # The twin's shared-provider address is matched exactly, never widened.
        self.assertEqual(out["searched"]["participants"],
                         [f"backup@{VENDOR}", "csr@shared-mail.test", f"rep@{VENDOR}",
                          VENDOR, "shared-mail.test"])
        shared = self.call(participant=S3)
        self.assertEqual(shared["searched"]["participants"], ["someone@gmail.com"])
        self.assertEqual(shared["documents"], [])

    def test_a_colleagues_address_on_a_record_is_never_widened_to_our_domain(self):
        out = self.call(participant=S4)
        self.assertEqual(out["searched"]["participants"], [f"buyer@{OURS}"])
        # Only the colleague's own messages: other@ (in v-new's To) is not widened to.
        self.assertEqual({m for _, _, m in self.listed(out)}, {"address"})
        self.assertNotIn("ours-only", [k for k, _, _ in self.listed(out)])
        self.assertIn("other", [k for k, _, _ in self.listed(out)])

    def test_unknown_own_domains_keep_a_records_addresses_exact(self):
        unknown = tools(Reader(tenant=None))
        out = self.call(tools_=unknown, participant=S1)
        self.assertEqual({m for _, _, m in self.listed(out)}, {"address"})
        self.assertNotIn("v-sub", [k for k, _, _ in self.listed(out)])

    def test_a_reader_without_the_pii_marking_reads_no_address_of_the_record(self):
        out = self.call(NOPII, participant=S1)
        self.assertEqual(out["documents"], [])
        self.assertIn("participant_has_no_address", [g["reason"] for g in out["gaps"]])
        self.assertEqual(out["searched"]["participants"], [])

    def test_with_an_object_only_its_mail_from_or_to_the_participant(self):
        out = self.call(participant=S1, object_id=fx.P1)
        self.assertEqual(self.listed(out), [("v-po", "from_them", "address")])
        out = self.call(participant=f"rep@{VENDOR}", query=fx.NUMBER)
        self.assertEqual([k for k, _, _ in self.listed(out)], ["v-po"])

    def test_since_and_until_bound_the_window(self):
        out = self.call(participant=f"@{VENDOR}", since="2099-01-15", until="2099-01-31")
        self.assertEqual([k for k, _, _ in self.listed(out)], ["v-sub"])

    def test_unknown_participant_shapes_fail_closed(self):
        for bad in ("acme", "not an address", "a@b", "@", "rep@@acme.test",
                    "x" * 250 + "@acme.test"):
            result = self.tools.call("plx.documents", {"participant": bad}, OPS)
            self.assertEqual(result["error"]["code"], "invalid_input", bad)
        result = self.tools.call("plx.documents", {"participant": f"@{VENDOR}",
                                                   "sources": ["sharepoint"]}, OPS)
        self.assertEqual(result["error"]["code"], "invalid_input")
        result = self.tools.call("plx.documents",
                                 {"participant": "Supplier:portal.fm_supplier:nope"}, OPS)
        self.assertEqual(result["error"]["code"], "object_not_found")

    def test_the_scan_without_the_word_index_finds_the_same_mail(self):
        indexed = self.listed(self.call(participant=f"@{VENDOR}"))
        self.tools.store.conn.execute("DROP TABLE value_text")
        self.assertEqual(self.listed(self.call(participant=f"@{VENDOR}")), indexed)

    def test_pages_of_many_messages_lose_and_repeat_nothing(self):
        for count in (1, 10, 60):
            def extend(store, count=count):
                for i in range(count):
                    message(store, f"bulk-{i:03d}", sender=f"bulk@{VENDOR}",
                            to=[f"buyer@{OURS}"], at=f"2099-03-{1 + i % 28:02d}T"
                            f"{i // 28:02d}:00:00Z")
            many = tools(extend=extend)
            seen, cursor = [], None
            while True:
                args = {"participant": f"bulk@{VENDOR}", "limit": 25}
                if cursor:
                    args["cursor"] = cursor
                out = self.call(tools_=many, **args)
                seen += [d["citation"] for d in out["documents"]]
                cursor = out["completeness"].get("next_cursor")
                if not cursor:
                    break
            self.assertEqual(len(seen), count)
            self.assertEqual(len(set(seen)), count)

    def test_a_query_names_an_old_message_of_a_vendor_with_more_mail_than_the_cap(self):
        # The query's candidates narrow the participant's mail before its newest-first
        # cap: a busy vendor's newer mail never cuts the older message the query names
        # (before, the cap kept the newest and the search found nothing, "scan_cap").
        from unittest import mock
        def extend(store):
            for i in range(30):
                message(store, f"busy-{i:02d}", sender=f"rep@{VENDOR}",
                        to=[f"buyer@{OURS}"], at=f"2099-04-{1 + i % 28:02d}T{i // 28:02d}:00:00Z")
        for indexed in (True, False):
            busy = tools(extend=extend)
            if not indexed:
                busy.store.conn.execute("DROP TABLE value_text")
            with self.subTest(indexed=indexed), mock.patch.object(ev, "DOC_SCAN_CAP", 20):
                out = self.call(tools_=busy, participant=f"@{VENDOR}", query=fx.NUMBER)
                self.assertEqual(self.listed(out), [("v-po", "from_them", "domain")])
                self.assertFalse(out["completeness"]["truncated"])
                self.assertNotIn("scan_cap", [g["reason"] for g in out["gaps"]])
                # without a query the cap still keeps the newest and says it cut
                out = self.call(tools_=busy, participant=f"@{VENDOR}", limit=50)
                self.assertIn("scan_cap", [g["reason"] for g in out["gaps"]])
                self.assertNotIn("v-po", json.dumps(out))
                # another participant's mail the query names: none of it
                out = self.call(tools_=busy, participant="@elsewhere.test", query=fx.NUMBER)
                self.assertEqual(out["documents"], [])

    def test_a_query_matched_in_an_attachment_finds_the_message_it_travels_with(self):
        """What a participant's mail says includes its attachments: a query only an
        attachment holds lists the message it was sent with, its snippet the
        attachment's; another party's message holding the word is never listed."""
        att = "Attachment:graph_mail.attachments:sds-1"

        def extend(store):
            store.obj(att, "mail:mbx-a", handle("mbx-a", "v-old") + "#attachment=sds-1",
                      "mail")
            store.val(att, "name", "text", "SDS Orbignya Oleifera Seed Oil")
            store.link("l-sds", "Attachment.attached_to", att,
                       "Email:graph_mail.messages:v-old")
        out = self.call(tools_=tools(extend=extend), participant=f"rep@{VENDOR}",
                        query="Orbignya")
        self.assertEqual(self.listed(out), [("v-old", "from_them", "address")])
        self.assertIn("Orbignya", out["documents"][0]["snippet"]["untrusted_text"])
        out = self.call(tools_=tools(extend=extend), participant="@elsewhere.test",
                        query="Orbignya")
        self.assertEqual(out["documents"], [])


class OwnDomainSenderTest(unittest.TestCase):
    def test_only_a_known_service_host_carries_our_name(self):
        from ontology.mentions import sent_from_own_domain
        own = ["petrasoap.com"]
        self.assertTrue(sent_from_own_domain("petrasoap.com", own))
        self.assertTrue(sent_from_own_domain("mail.petrasoap.com", own))
        self.assertTrue(sent_from_own_domain("petrasoap.zendesk.com", own))
        for lookalike in ("petrasoap.example.com", "petrasoap.evil.net", "gmail.com",
                          "petrasoap.com.evil.com"):
            self.assertFalse(sent_from_own_domain(lookalike, own), lookalike)
        self.assertFalse(sent_from_own_domain("petrasoap.zendesk.com", []))


if __name__ == "__main__":
    unittest.main()
