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


if __name__ == "__main__":
    unittest.main()
