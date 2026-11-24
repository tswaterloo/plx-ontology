"""The GPU image OCR pass (``integrations.image_ocr_gpu``) and its Spark worker, with a
fake model behind the real worker protocol (no GPU, no ssh, no network)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

from integrations import image_ocr_gpu as g
from integrations import image_ocr_gpu_worker as w
from integrations.address_forms import decode_imceaex, normalize_address
from integrations.people_exclusion import with_directory

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is on every test host
    Image = None

REPO = Path(__file__).resolve().parents[2]

# A worker with the real protocol and page preparation; the model is a fake that says
# each page's size and records every image it was sent (by hash).
FAKE = textwrap.dedent('''
    import hashlib, io, json, os, sys
    sys.path.insert(0, {repo!r})
    from integrations import image_ocr_gpu_worker as w
    mode, log = sys.argv[1], sys.argv[2]
    hello = w.hello_of("cuda")
    if mode == "other_model":
        hello["model"] = "someone/else"
    served = [0]
    real = w.pages_of
    def pages_of(content):
        with open(log, "a") as f:
            f.write(hashlib.sha256(content).hexdigest() + "\\n")
        return real(content)
    w.pages_of = pages_of
    def generate(pages):
        served[0] += 1
        if mode == "die" and served[0] > 1:
            os._exit(3)
        if mode == "hang":
            import time; time.sleep(60)
        if mode == "dirty":
            return [("A\\x00B\\x1b[31m" + "x" * 70000 + "\\x07", False) for p in pages]
        return [("NO_TEXT", False) if p.size[0] == 64 else
                ("PAGE %dx%d" % p.size, p.size[0] == 99) for p in pages]
    w.serve(sys.stdin.buffer, sys.stdout.buffer, generate, hello)
''')


def png(width: int, height: int = 40, mode: str = "RGB", frames: int = 1) -> bytes:
    out = io.BytesIO()
    if frames > 1:
        pages = [Image.new("L", (width, height), 255) for _ in range(frames)]
        pages[0].save(out, "TIFF", save_all=True, append_images=pages[1:])
    else:
        Image.new(mode, (width, height), (255, 255, 255, 0) if mode == "RGBA" else
                  (255, 255, 255)).save(out, "PNG")
    return out.getvalue()


# Object ids of the fixture's people (the real guard decides by them).
OID_A, OID_B, OID_X = ("11111111-1111-1111-1111-111111111111",
                       "22222222-2222-2222-2222-222222222222",
                       "99999999-9999-9999-9999-999999999999")
MB1, MB_X = OID_A, OID_X   # mailboxes are their owners' object ids
PEOPLE = ((OID_A, "a@petra.example", ["a@petra.example"]),
          (OID_B, "b@petra.example", ["b@petra.example", "orders@petra.example"]),
          (OID_X, "excluded.person@petra.example", ["excluded.person@petra.example"]))


class FakeMail:
    """The mail store's tables the pass reads, at the capture's own path
    (``<state>/mail-capture/capture.db``, where ``people_exclusion.with_directory`` reads
    the recorded directory), and its re-hashed ``blob``."""

    def __init__(self, state: Path, *, people=PEOPLE, checked_hours_ago: float = 1.0):
        from datetime import datetime, timedelta, timezone
        (state / "mail-capture").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state / "mail-capture" / "capture.db",
                                  check_same_thread=False)
        for ddl in (
                "CREATE TABLE messages (l1_handle TEXT, record TEXT, body_sha256 TEXT)",
                "CREATE TABLE attachments (message_handle TEXT, mailbox_id TEXT, "
                "metadata TEXT, content_sha256 TEXT, content_status TEXT, text_status TEXT, "
                "l1_handle TEXT, text_sha256 TEXT)",
                "CREATE TABLE attachment_text_versions (l1_handle TEXT, version INTEGER, "
                "text_status TEXT, text_sha256 TEXT, recorded_at TEXT)",
                "CREATE TABLE mailboxes (mailbox_id TEXT, upn TEXT, identities TEXT, "
                "identity_checked_at TEXT)",
                "CREATE TABLE undecided_addresses (address TEXT PRIMARY KEY, since TEXT, "
                "checked_at TEXT)",
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
                "CREATE TABLE tenant_listing (address TEXT, object_id TEXT, kind TEXT)",
                "CREATE TABLE people_withheld (message_key TEXT PRIMARY KEY, at TEXT)"):
            self.db.execute(ddl)
        at = (datetime.now(timezone.utc) - timedelta(hours=checked_hours_ago)).isoformat()
        self.db.executemany("INSERT INTO mailboxes VALUES (?,?,?,?)",
                            [(oid, upn, json.dumps(ids), at) for oid, upn, ids in people])
        self.db.commit()
        self.blobs: dict[str, bytes] = {}
        self.state = state

    def put_blob(self, content: bytes) -> str:
        sha = hashlib.sha256(content).hexdigest()
        self.blobs[sha] = content
        return sha

    def add(self, content: bytes, *, name="scan.png", status="extracted_ocr", mailbox=MB1,
            sender="a@petra.example", to="b@petra.example", subject="Order", handle=None,
            body: str | None = None, ocr: str | None = None, **extra) -> str:
        sha = self.put_blob(content)
        handle = handle or f"h{len(self.blobs)}-{name}-{mailbox}"
        body_sha = self.put_blob(body.encode()) if body is not None else None
        text_sha = self.put_blob(ocr.encode()) if ocr is not None else None
        self.db.execute("INSERT INTO messages VALUES (?, ?, ?)", (handle, json.dumps(
            {"from": {"emailAddress": {"address": sender}}, "subject": subject,
             "toRecipients": [{"emailAddress": {"address": to}}], **extra}), body_sha))
        self.db.execute("INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?)", (
            handle, mailbox, json.dumps({"name": name, "content_type": "image/png"}), sha,
            "captured", status, "att:" + handle, text_sha))
        self.db.commit()
        return sha

    def blob(self, sha: str) -> bytes:
        return self.blobs[sha]

    def is_withheld(self, internet_message_id) -> bool:
        """As the store: a copy of this message was withheld in some mailbox."""
        from integrations.graph_read import withheld_message_key
        key = withheld_message_key(internet_message_id)
        return key is not None and self.db.execute(
            "SELECT 1 FROM people_withheld WHERE message_key=?", (key,)).fetchone() is not None

    def record_listing(self, guard) -> None:
        """What the capture records of a bound guard's listing (``record_tenant_listing``)."""
        from integrations.people_exclusion import tenant_listing_rows
        at, rows = tenant_listing_rows(guard)
        self.db.execute("DELETE FROM tenant_listing")
        self.db.executemany("INSERT INTO tenant_listing VALUES (?,?,?)", rows)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('tenant_listing_at', ?)", (at,))
        self.db.commit()

    def withheld_mailboxes(self, people):
        """As the store: the captured mailboxes the exclusion withholds now."""
        if getattr(people, "unavailable", None):
            return None
        return [m for (m,) in self.db.execute("SELECT mailbox_id FROM mailboxes")
                if people.is_excluded(m)]


def is_image(name: str, kind: str) -> bool:
    return name.endswith((".png", ".tif"))


def exclusion_file(root: Path, oids) -> Path:
    path = root / "excluded.json"
    path.write_text(json.dumps({"schema": "plx-people-exclusion/v1", "object_ids": list(oids)}))
    os.chmod(path, 0o600)
    return path


@unittest.skipIf(Image is None, "Pillow missing")
class GpuOcrPassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        (self.tmp / "fake.py").write_text(FAKE.format(repo=str(REPO)), encoding="utf-8")
        self.state = self.tmp / "state"
        self.mail = FakeMail(self.state)
        self.store = g.Store(self.tmp / "out")
        self.sent_log = self.tmp / "sent.txt"
        self.sent_log.touch()
        self.list_file = exclusion_file(self.tmp, [OID_X])

    def gate(self, env=None):
        """The pass's own gate: the real exclusion from the environment, re-read, with the
        capture's recorded directory attached (no fake that already knows addresses)."""
        env = {"PLX_EXCLUDED_PEOPLE_FILE": str(self.list_file)} if env is None else env
        return g.Gate.from_env(env, self.mail, self.state)

    def host(self, name="h1", mode="ok", answer_seconds=30.0):
        cmd = f"{shlex.quote(sys.executable)} {self.tmp / 'fake.py'} {mode} {self.sent_log}"
        h = g.Host(name, shlex.split(cmd), answer_seconds=answer_seconds)
        self.addCleanup(h.close)
        h.start()
        return h

    def run_pass(self, hosts, gate=None, batch=4):
        self.store.plan(self.mail.db, is_image)
        return g.read_pass(self.store, self.mail, hosts, gate or self.gate(), batch=batch,
                           budget_seconds=60)

    def sent(self) -> list[str]:
        return self.sent_log.read_text().split()

    def readings(self) -> dict[str, sqlite3.Row]:
        return {r["content_sha256"]: r for r in self.store.db.execute(
            "SELECT * FROM readings")}

    def test_every_planned_image_read_once_at_any_count_and_a_second_run_sends_nothing(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                shas = [self.mail.add(png(200 + i)) for i in range(n)]
                # a scanned PDF's OCR and an unread image are not planned
                self.mail.add(b"%PDF-1.4 x", name="scan.pdf")
                self.mail.add(png(150), status="unsupported")
                out = self.run_pass([self.host()])
                got = self.readings()
                self.assertEqual(sorted(got), sorted(shas))
                self.assertEqual(sorted(self.sent()), sorted(shas))   # each sent once
                for i, sha in enumerate(shas):
                    self.assertEqual(got[sha]["text"], f"PAGE {200 + i}x40")
                    self.assertEqual(got[sha]["status"], "text")
                self.assertEqual((out["stored"], out["refused"], out["left_unsent"]), (n, 0, 0))
                again = self.run_pass([self.host("h2")])
                self.assertEqual(again["sent"], 0)
                self.assertEqual(len(self.sent()), n)

    def test_withheld_excluded_and_undecidable_images_never_leave_the_box_and_freightcom_is_read(self):
        ok = self.mail.add(png(201))
        external = self.mail.add(png(210), sender="ap@vendor.example", to="a@petra.example")
        held = self.mail.add(png(202), mailbox=MB_X)
        # the excluded person by an address only the recorded directory maps (any case)
        person = self.mail.add(png(203), sender="Excluded.Person@petra.example")
        cc_person = self.mail.add(png(211), ccRecipients=[
            {"emailAddress": {"address": "excluded.person@petra.example"}}])
        # an address on the tenant's domains nobody can decide offline (a list, a leaver)
        unknown = self.mail.add(png(212), to="all-staff@petra.example")
        self.mail.db.execute("INSERT INTO undecided_addresses VALUES "
                             "('vendor-list@outside.example', '', '')")
        undecided = self.mail.add(png(213), to="Vendor-List@outside.example")
        freight = self.mail.add(png(204), subject="Re: Freightcom shipment 88")
        freight_name = self.mail.add(png(205), name="freightcom-label.png")
        freight_body = self.mail.add(png(214), body="Hello,\n" + "x " * 4000 +
                                     "Booked through Freight com today.")
        freight_ocr = self.mail.add(png(215), ocr="FREIGHTCOM INC  BILL OF LADING")
        freight_field = self.mail.add(png(216), uniqueBody={"content": "via freightcom"})
        # Freightcom is read like any other (owner, 11-23): its images go to the reader too
        # an earlier OCR version naming Freightcom no longer withholds the image
        old_ocr = self.mail.add(png(217), ocr="PACKING SLIP", handle="old-ocr")
        self.mail.db.execute("INSERT INTO attachment_text_versions VALUES (?,1,'x',?,'')", (
            "att:old-ocr", self.mail.put_blob(b"freightcom terms of service")))
        # one carrier passes, another copy of the same bytes is in a withheld mailbox
        shared = png(206)
        self.mail.add(shared)
        self.mail.add(shared, mailbox=MB_X, handle="other-copy")
        orphan = self.mail.add(png(207))
        self.mail.db.execute("DELETE FROM messages WHERE l1_handle = (SELECT message_handle "
                             "FROM attachments WHERE content_sha256 = ?)", (orphan,))
        self.mail.db.commit()
        out = self.run_pass([self.host()])
        read = [ok, external, freight, freight_name, freight_body, freight_ocr, freight_field,
                old_ocr]
        self.assertEqual(sorted(self.sent()), sorted(read))
        self.assertEqual(sorted(self.readings()), sorted(read))
        sha_shared = hashlib.sha256(shared).hexdigest()
        reasons = dict(self.store.db.execute("SELECT content_sha256, reason FROM refused"))
        self.assertEqual(reasons, {
            held: "withheld_mailbox", person: "excluded_person", cc_person: "excluded_person",
            unknown: "exclusion_undecided", undecided: "exclusion_undecided",
            sha_shared: "withheld_mailbox", orphan: "message_missing"})
        self.assertEqual(out["refused"], 7)

    def test_a_person_added_to_the_list_during_the_run_is_refused_from_then_on(self):
        sha = self.mail.add(png(201), sender="b@petra.example", to="orders@petra.example")
        gate = self.gate()
        self.assertIsNone(gate.why_not(self.mail.db, sha))
        exclusion_file(self.tmp, [OID_X, OID_B])     # the list changes mid-run
        self.assertEqual(gate.why_not(self.mail.db, sha), "excluded_person")

    def test_x500_quoted_and_bare_identities_are_decided_or_refused_never_skipped(self):
        """Live records name internal people by Exchange X.500 identity and hold quoted
        addresses: each is decided through the directory or refuses. The exclusion keys
        SMTP addresses only, so no X.500 identity (bare, ``X500:``, ``IMCEAEX``-encoded) is
        decidable and every one refuses, even when it is an ``X500:`` proxy of a listed
        user."""
        from integrations.people_exclusion import PeopleExclusion
        x500_a = "/o=ExchangeLabs/ou=Exchange Administrative Group (FYDIBOHF23SPDLT)" \
                 "/cn=Recipients/cn=0a1b2c3d4e5f-a"
        x500_x = x500_a[:-1] + "x"
        x500_at = x500_a + "@vendor.example"   # an X.500 identity ending in "@<word>"

        class Directory:   # GET /users, every page: what the capture binds with
            def get_all_pages(self, url, **kw):
                return self.get_pages(url, **kw)

            def get_pages(self, url, **_):
                return [{"id": OID_A, "userPrincipalName": "a@petra.example",
                         "mail": "a@petra.example",
                         "proxyAddresses": ["SMTP:a@petra.example", "X500:" + x500_a,
                                            "X500:" + x500_at]},
                        {"id": OID_B, "userPrincipalName": "b@petra.example",
                         "proxyAddresses": ["smtp:orders@petra.example"]},
                        {"id": OID_X, "userPrincipalName": "excluded.person@petra.example",
                         "proxyAddresses": ["x500:" + x500_x]}], {"complete": True}

        bound = PeopleExclusion([OID_X])
        bound.bind(Directory())
        self.mail.record_listing(bound)

        def stored(sender, to=(), cc=(), **extra):   # the capture's record: bare strings
            sha = self.mail.add(png(300 + len(self.mail.blobs)))
            self.mail.db.execute(
                "UPDATE messages SET record=? WHERE l1_handle=(SELECT message_handle FROM "
                "attachments WHERE content_sha256=?)", (json.dumps(
                    {"from": sender, "to": list(to), "cc": list(cc), "subject": "Order",
                     **extra}), sha))
            return sha

        mapped = stored(x500_a.upper(), to=["b@petra.example"])
        quoted_ok = stored("a@petra.example'", to=['"Orders" <ORDERS@petra.example>'])
        graph_name = self.mail.add(png(330), sender="a@petra.example", toRecipients=[
            {"emailAddress": {"name": "Order Desk", "address": "orders@petra.example"}}])
        excluded_x500 = stored(x500_x, to=["a@petra.example"])
        unmapped = stored("/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS"
                          "/CN=NOBODY-KNOWN", to=["a@petra.example"])
        mapped_at = stored(x500_at.upper(), to=["a@petra.example"])
        unmapped_at = stored("/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS"
                             "/CN=SOMEONE@vendor.example", to=["a@petra.example"])
        def imceaex(dn, host="ab1pr01mb0001.canprd01.prod.outlook.com"):   # Exchange's form
            enc = "".join("_" if c == "/" else c if c.isalnum() or c in "-=." else
                          "+%02X" % ord(c) for c in dn)
            return "IMCEAEX-" + enc + "@" + host
        self.assertEqual(imceaex(x500_a)[:28], "IMCEAEX-_o=ExchangeLabs_ou=E")
        # decoded, the encoded form is the same identity as the bare one
        self.assertEqual(normalize_address("<" + imceaex(x500_x) + ">."), x500_x.lower())
        self.assertIsNone(decode_imceaex(imceaex(x500_a).replace("+20", "+Z", 1)))
        enc_mapped = stored(imceaex(x500_a).upper(), to=["b@petra.example"])
        enc_unknown = stored("a@petra.example", cc=[imceaex(
            "/o=ExchangeLabs/ou=Exchange Administrative Group/cn=Recipients/cn=nobody",
            host="fr1p264mb0001.frap264.prod.outlook.com")])
        enc_excluded = stored("a@petra.example", to=["<" + imceaex(x500_x).lower() + ">"])
        enc_bad_hex = stored("a@petra.example", to=[imceaex(x500_a).replace("+20", "+Z", 1)])
        enc_no_host = stored("a@petra.example", to=[imceaex(x500_a).split("@")[0] + "@"])
        enc_not_x500 = stored("a@petra.example", to=["IMCEAEX-_cn+3Dsomeone@host.example"])
        enc_smtp = stored("a@petra.example", to=["IMCEASMTP-a+40petra+2Eexample@host.example"])
        trailing = stored("a@petra.example", to=["(excluded.person@petra.example)."])
        trailing_ok = stored("a@petra.example.", to=["(B@petra.example)", "b@petra.example."])
        two_ok = stored("a@petra.example", to=["ap@vendor.example, <B@petra.example>"])
        two_one_unknown = stored("a@petra.example",
                                 to=["ap@vendor.example; nobody@petra.example"])
        two_unsplit = stored("a@petra.example", to=["ap@vendor.example b@petra.example"])
        bare = stored("a@petra.example", cc=["Accounts Team"])
        quoted_x = stored("a@petra.example", cc=["'excluded.person@petra.example'"])
        sub_domain = stored("a@petra.example", to=["someone@mail.petra.example"])
        odd_type = stored("a@petra.example", to=[7])
        withheld = stored("a@petra.example", internet_message_id="<m1@petra.example>")
        self.mail.db.execute("INSERT INTO people_withheld VALUES (?, '')", (
            __import__("integrations.graph_read", fromlist=["x"]).withheld_message_key(
                "<m1@petra.example>"),))
        self.mail.db.commit()
        out = self.run_pass([self.host()])
        self.assertEqual(sorted(self.sent()),
                         sorted([quoted_ok, graph_name, two_ok, trailing_ok]))
        self.assertEqual(dict(self.store.db.execute(
            "SELECT content_sha256, reason FROM refused")), {
            mapped: "exclusion_undecided", mapped_at: "exclusion_undecided",
            enc_mapped: "exclusion_undecided",
            excluded_x500: "exclusion_undecided", unmapped: "exclusion_undecided",
            bare: "exclusion_undecided", quoted_x: "excluded_person",
            sub_domain: "exclusion_undecided", odd_type: "exclusion_undecided",
            withheld: "people_withheld", unmapped_at: "exclusion_undecided",
            two_one_unknown: "exclusion_undecided", two_unsplit: "exclusion_undecided",
            enc_unknown: "exclusion_undecided", enc_excluded: "exclusion_undecided",
            enc_bad_hex: "exclusion_undecided", enc_no_host: "exclusion_undecided",
            enc_not_x500: "exclusion_undecided", enc_smtp: "exclusion_undecided",
            trailing: "excluded_person"})
        self.assertEqual(out["refused"], 20)
        # the bound guard itself (the capture's, online) refuses them too in this gate
        gate = g.Gate(bound, self.mail)
        for sha in (mapped, mapped_at, enc_mapped, excluded_x500):
            self.assertEqual(gate.why_not(self.mail.db, sha), "exclusion_undecided")
        self.assertIsNone(gate.why_not(self.mail.db, quoted_ok))

    def test_an_x500_identity_the_directory_maps_is_decided_by_its_object_id(self):
        """When the guard's directory maps an X.500 identity to an object id, that id
        decides it; any other X.500 identity still refuses, even from a guard that calls
        an identity it does not hold "no excluded person's" (as a bound one does)."""
        from integrations.people_exclusion import PeopleExclusion
        x500_b, x500_x = "/o=org/cn=recipients/cn=b", "/o=org/cn=recipients/cn=x"

        class Mapped(PeopleExclusion):
            def object_id_of(self, address):
                return {x500_b: OID_B, x500_x: OID_X}.get(address) \
                    or super().object_id_of(address)

            def is_excluded(self, value):
                return False if "/o=" in str(value) else super().is_excluded(value)
        guard = with_directory(Mapped([OID_X]), self.state)
        ok = self.mail.add(png(401), sender="X500:" + x500_b.upper())
        excluded = self.mail.add(png(402), to="<IMCEAEX-_o=org_cn=recipients_cn=x@h.example>")
        unknown = self.mail.add(png(403), to="/o=org/cn=recipients/cn=nobody")
        gate = g.Gate(guard, self.mail)
        self.assertIsNone(gate.why_not(self.mail.db, ok))
        self.assertEqual(gate.why_not(self.mail.db, excluded), "excluded_person")
        self.assertEqual(gate.why_not(self.mail.db, unknown), "exclusion_undecided")

    def test_a_person_added_to_the_env_files_inline_list_mid_run_is_refused(self):
        sha = self.mail.add(png(201), sender="b@petra.example", to="orders@petra.example")
        env_file = self.tmp / "proactive.env"

        def write(oids):
            env_file.write_text(f"PLX_EXCLUDED_PEOPLE_OIDS={','.join(oids)}\n")
            os.chmod(env_file, 0o600)
        write([OID_X])
        gate = self.gate({"PLX_EXCLUSION_ENV_FILE": str(env_file)})
        self.assertIsNone(gate.why_not(self.mail.db, sha))
        write([OID_X, OID_B])   # the inline list changes mid-run
        self.assertEqual(gate.why_not(self.mail.db, sha), "excluded_person")

    def test_a_failed_hosts_batch_is_decided_again_before_it_is_resent(self):
        shas = [self.mail.add(png(200 + i)) for i in range(12)]
        gate = self.gate()
        seen: dict[str, int] = {}
        real = gate.why_not

        def why_not(db, sha):   # the list changes after the first decision of each image
            seen[sha] = seen.get(sha, 0) + 1
            return "excluded_person" if seen[sha] > 1 else real(db, sha)
        gate.why_not = why_not
        out = self.run_pass([self.host("same", "die"), self.host("same")], gate, batch=2)
        self.assertEqual(out["hosts_failed"], 1)
        refused = {r[0] for r in self.store.db.execute("SELECT content_sha256 FROM refused")}
        self.assertTrue(refused)                       # the dead host's batch, decided again
        self.assertTrue(refused.isdisjoint(self.readings()))
        self.assertEqual(sorted(refused | set(self.readings())), sorted(shas))
        self.assertTrue(all(seen[s] == 2 for s in refused))

    def test_without_a_usable_list_directory_or_tenant_domains_nothing_leaves(self):
        self.mail.add(png(201))
        for label, env, mail in (
                ("no list", {"PLX_EXCLUSION_ENV_FILE": ""}, None),
                ("list file unreadable", {"PLX_EXCLUDED_PEOPLE_FILE": str(self.tmp / "nope")},
                 None),
                # the recorded directory is older than the bound: tenant addresses undecided
                ("stale directory", None, FakeMail(self.tmp / "stale", checked_hours_ago=24 * 30)),
                ("no tenant domains", None, FakeMail(self.tmp / "none", people=()))):
            with self.subTest(label), mock.patch.dict(os.environ, {}, clear=False):
                for k in ("PLX_EXCLUDED_PEOPLE_OIDS", "PLX_EXCLUDED_PEOPLE_FILE"):
                    os.environ.pop(k, None)
                os.environ["PLX_EXCLUSION_ENV_FILE"] = ""
                if mail is not None:   # the store and its recorded directory together
                    self.mail, self.state = mail, mail.state
                    self.mail.add(png(201))
                self.store = g.Store(self.tmp / f"out-{label}")
                gate = self.gate(env)
                out = self.run_pass([self.host()], gate)
                self.assertEqual((out["sent"], out["refused"]), (0, 1))

    def test_a_dropped_original_is_read_from_the_fetched_copy_only_when_it_hashes(self):
        from integrations.attach_fetch import blob_file
        fetch = self.tmp / "fetch"
        kept, dropped, corrupt = (self.mail.add(png(201)), self.mail.add(png(202)),
                                  self.mail.add(png(203)))
        for sha, body in ((dropped, self.mail.blobs.pop(dropped)),
                          (corrupt, self.mail.blobs.pop(corrupt) + b"x")):
            path = blob_file(fetch, sha)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.store.plan(self.mail.db, is_image)
        out = g.read_pass(self.store, self.mail, [self.host()], self.gate(), batch=4,
                          budget_seconds=60, fetch_dir=fetch)
        self.assertEqual(sorted(self.sent()), sorted([kept, dropped]))
        self.assertEqual(out["bytes_missing"], 1)

    def test_a_broken_guard_refuses_every_image(self):
        self.mail.add(png(201))
        gate = self.gate()
        with mock.patch.object(type(gate.guard), "is_excluded", side_effect=RuntimeError):
            out = self.run_pass([self.host()], gate)
        self.assertEqual((self.sent(), out["refused"]), ([], 1))

    def test_a_host_text_is_stored_capped_and_without_control_characters(self):
        sha = self.mail.add(png(201))
        self.run_pass([self.host(mode="dirty")])
        got = self.readings()[sha]
        self.assertNotRegex(got["text"], "[\x00-\x08\x0b\x0d-\x1f\x7f-\x9f]")
        self.assertTrue(got["text"].startswith("AB[31mx"))
        self.assertEqual((len(got["text"]), got["truncated"]), (g.MAX_TEXT_PER_PAGE, 1))

    def test_pages_no_text_truncated_and_unreadable_answers_are_kept_as_said(self):
        multi = self.mail.add(png(300, frames=3), name="fax.tif")
        empty = self.mail.add(png(64))
        cut = self.mail.add(png(99))
        clear = self.mail.add(png(120, mode="RGBA"))
        junk = self.mail.add(b"\x89PNG not really", name="broken.png")
        self.run_pass([self.host()])
        got = self.readings()
        self.assertEqual(got[multi]["text"], "PAGE 300x40\fPAGE 300x40\fPAGE 300x40")
        self.assertEqual(got[multi]["pages"], 3)
        self.assertEqual((got[empty]["status"], got[empty]["text"]), ("no_text", None))
        self.assertEqual((got[cut]["status"], got[cut]["truncated"]), ("text", 1))
        self.assertEqual(got[clear]["text"], "PAGE 120x40")
        self.assertEqual((got[junk]["status"], got[junk]["text"]), ("unreadable", None))

    def test_a_dying_host_hands_its_batch_on_and_nothing_is_lost(self):
        shas = [self.mail.add(png(200 + i)) for i in range(12)]
        out = self.run_pass([self.host("same", "die"), self.host("same")], batch=2)
        self.assertEqual(out["hosts_failed"], 1)
        self.assertEqual(sorted(self.readings()), sorted(shas))
        self.assertEqual(out["left_unsent"], 0)

    def test_with_every_host_gone_the_unsent_images_stay_pending_for_the_next_run(self):
        shas = [self.mail.add(png(200 + i)) for i in range(12)]
        out = self.run_pass([self.host("dies", "die")], batch=2)
        self.assertEqual(out["hosts_failed"], 1)
        self.assertEqual(len(self.readings()) + out["left_unsent"] <= 12, True)
        self.assertLess(len(self.readings()), 12)
        self.run_pass([self.host("ok")], batch=2)
        self.assertEqual(sorted(self.readings()), sorted(shas))

    def test_a_hanging_host_is_dropped_instead_of_waited_on_forever(self):
        self.mail.add(png(201))
        out = self.run_pass([self.host("hangs", "hang", answer_seconds=2.0)])
        self.assertEqual((out["hosts_failed"], out["stored"], out["left_unsent"]), (1, 0, 1))

    def test_a_host_running_another_engine_is_refused_before_any_image(self):
        self.mail.add(png(201))
        with self.assertRaises(g.HostFailed) as caught:
            self.host("other", "other_model")
        self.assertIn("host_model_differs", str(caught.exception))
        self.assertEqual(self.sent(), [])

    def test_the_ssh_child_gets_no_credential(self):
        env = {"PATH": "/bin", "PLX_WORKBENCH_API_KEY": "x", "AZURE_CLIENT_SECRET": "y",
               "PGPASSWORD": "z", "HOME": "/h", "GH_TOKEN": "t"}
        self.assertEqual(g._scrubbed(env), {"PATH": "/bin", "HOME": "/h"})


class CleanResultTests(unittest.TestCase):
    """The model's artefacts as stored readings show them, cleaned before a text counts."""

    def clean(self, text, **kw):
        out = g.clean_result({"status": "text", "text": text, "pages": kw.pop("pages", 1),
                              **kw})
        self.assertEqual(g.clean_result(out), out)   # the apply cleans again: idempotent
        return out["status"], out["text"]

    def test_a_no_text_marker_after_a_transcription_is_dropped_and_the_text_kept(self):
        for said in ("PO 4500012345\nSHIP TO DOCK 4\nNO_TEXT",
                     "PO 4500012345\nSHIP TO DOCK 4.\n\nno_text",
                     "PO 4500012345\nSHIP TO DOCK 4\n-- No Text --",
                     "NO_TEXT PO 4500012345 SHIP TO DOCK 4",
                     "PO 4500012345 (NO_TEXT) SHIP TO DOCK 4"):
            status, text = self.clean(said)
            self.assertEqual(status, "text", said)
            self.assertNotRegex(text, r"(?i)no[_ -]text")
            self.assertIn("4500012345", text)

    def test_a_made_up_word_beside_no_text_is_no_text(self):
        for said in ("Zorblax\nNO_TEXT", "NO_TEXT", "NO TEXT", "AB 12\n\nNO_TEXT\f",
                     "\u00ae\nno_text."):
            self.assertEqual(self.clean(said), ("no_text", None), said)

    def test_each_page_is_judged_on_its_own(self):
        status, text = self.clean("Zorblax\nNO_TEXT\fINVOICE 77 TOTAL 410.00", pages=2)
        self.assertEqual((status, text), ("text", "INVOICE 77 TOTAL 410.00"))
        self.assertEqual(self.clean("Qx\nNO_TEXT\fNO_TEXT", pages=2), ("no_text", None))

    def test_real_words_that_resemble_the_marker_stay(self):
        for said in ("Reply with no text in the subject line", "FIELD INFO_TEXT_2 set",
                     "NO_TEXTURE coating, grade 5", "1 EA\n1 EA\n1 EA\n1 EA\n1 EA\nEND"):
            self.assertEqual(self.clean(said), ("text", said), said)

    def test_a_cut_reading_stored_with_its_loop_is_trimmed_a_finished_one_is_not(self):
        looped = "TOTAL 9\n" + "Item 4 Item 4\n" * 3 + "x\n" * 40
        status, text = self.clean(looped, truncated=True)
        self.assertEqual((status, text), ("text", w.trim_loops(looped).strip()))
        self.assertLess(text.count("x"), 4)
        self.assertEqual(self.clean(looped)[1].count("x"), 40)

    def test_an_answer_other_than_text_carries_no_text(self):
        for status in ("no_text", "unreadable", "something_new"):
            out = g.clean_result({"status": status, "text": "PO 1 NO_TEXT", "pages": 1})
            self.assertEqual((out["status"], out["text"]), (status, None))


class WorkerTests(unittest.TestCase):
    def test_a_finished_page_keeps_every_repeated_row_and_only_a_cut_one_is_trimmed(self):
        rows = "PICKING SLIP\n" + "1 EA BOLT M8\n" * 12 + "END"
        def generate(cut):
            return lambda pages: [(rows, cut) for _ in pages]
        whole = w.results_of([png(200)], generate(False))[0]
        self.assertEqual(whole["text"].count("1 EA BOLT M8"), 12)    # all 12 rows counted
        looped = w.results_of([png(200)], generate(True))[0]
        self.assertEqual((looped["text"].count("1 EA BOLT M8"), looped["truncated"]), (1, True))

    def test_loops_are_cut_and_values_kept(self):
        self.assertEqual(w.trim_loops("Total\nx\nx\nx\nx\nx\nEnd"), "Total\nx\nEnd")
        self.assertEqual(w.trim_loops("ab ab ab ab ab ab ab end"), "ab end")
        for value in ("PO 100000", "ABABABABABAB", "1 EA\n1 EA\n1 EA"):
            self.assertEqual(w.trim_loops(value), value)

    def test_an_out_of_step_request_is_answered_once_and_ends(self):
        stdin = io.BytesIO()
        w.write_frame(stdin, json.dumps({"n": 2}).encode())
        w.write_frame(stdin, b"one image only")
        stdin.write(b"\0\0\0\0")
        stdin.seek(0)
        out = io.BytesIO()
        w.serve(stdin, out, lambda pages: [], w.hello_of("cpu"))
        out.seek(0)
        hello, answer = w.read_frame(out), w.read_frame(out)
        self.assertEqual(json.loads(hello)["model"], w.ENGINE["model"])
        self.assertIn("error", json.loads(answer))

    def test_engine_key_moves_with_the_prompt_and_bounds(self):
        base = w.hello_of("cuda")
        self.assertEqual(g.engine_key(base), g.engine_key(dict(base, device="cpu")))
        for key, value in (("prompt_sha256", "0"), ("max_pixels", 1), ("revision", "r2")):
            self.assertNotEqual(g.engine_key(base), g.engine_key(dict(base, **{key: value})))


from integrations import attach_fetch, capture_mail, captured_text as ct, graph_read  # noqa: E402
from integrations.graph_read import GraphMailReader  # noqa: E402
from integrations.tests import test_capture_mail as tm  # noqa: E402
from integrations.tests import test_capture_mail_compact as tcm  # noqa: E402


@unittest.skipIf(Image is None, "Pillow missing")
class GpuApplyTests(tcm.CompactCase):
    """Ingest's apply of the GPU readings: new text versions over tesseract's, never a
    worse one, never in a withheld mailbox, once."""

    def setUp(self):
        super().setUp()
        self.imgs = {n: png(300 + n) for n in range(1, 7)}
        self.graph.put(tm.BOX_A, "MSG-1", "CK-1", attachments=[
            tcm.attachment(f"ATT-{n}", self.imgs[n], f"p{n}.png", "image/png")
            for n in range(1, 6)] + [
            tcm.attachment("ATT-7", b"%PDF-1.4 scan", "scan.pdf", "application/pdf")])
        self.graph.put(tm.BOX_B, "MSG-2", "CK-2", attachments=[
            tcm.attachment("ATT-6", self.imgs[1], "p1.png", "image/png")])
        with mock.patch.object(capture_mail, "image_worth_reading", return_value=False):
            self.capture(defer_ocr=True).run(tm.SCOPE)
        old = self.store.put_text("TESSERACT OLD")
        with self.store.db:
            for att, status in (("ATT-1", "extracted_ocr"), ("ATT-2", "ocr_no_text"),
                                ("ATT-3", "extracted_ocr"), ("ATT-4", "extracted_ocr"),
                                ("ATT-5", "ocr_no_text"), ("ATT-6", "extracted_ocr"),
                                ("ATT-7", "extracted_ocr")):
                self.store.db.execute("UPDATE attachments SET text_status=?, text_sha256=? "
                                      "WHERE attachment_id=?",
                                      (status, old if status == "extracted_ocr" else None, att))
        self.fetch_dir = Path(self.tmp.name) / "fetch"
        attach_fetch.FetchStore(self.fetch_dir).close()
        gpu = g.Store(self.fetch_dir)
        key = g.engine_key(g.expected_hello())
        sha = {n: hashlib.sha256(b).hexdigest() for n, b in self.imgs.items()}
        gpu.put(key, [sha[1], sha[2], sha[3], sha[4], sha[5]], [
            {"status": "text", "text": "GPU PO 4500012345", "pages": 1},          # replaces
            {"status": "text", "text": "GPU LABEL", "pages": 1},                  # fills a blank
            {"status": "no_text", "text": None, "pages": 1},                      # never erases
            {"status": "text", "text": "GPU CUT", "pages": 1, "truncated": True},  # kept: had text
            {"status": "text", "text": "GPU CUT 2", "pages": 1, "truncated": True}])  # fills blank
        # another engine's reading is not this one's
        gpu.put("other-engine", [sha[6]], [{"status": "text", "text": "X", "pages": 1}])
        gpu.db.close()

    def apply(self, env, switch="1"):
        api, _ = graph_read.build_graph(env, sender=self.graph)
        on = {} if switch is None else {capture_mail.GPU_OCR_APPLY_ENV: switch}
        with mock.patch.dict(os.environ, on, clear=False):
            if switch is None:
                os.environ.pop(capture_mail.GPU_OCR_APPLY_ENV, None)
            os.environ.pop("PLX_WINDOW_DIR", None)   # the test state's own window dir
            return capture_mail.apply_fetched_pass(
                self.store, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
                self.fetch_dir, budget_seconds=600)

    def texts(self):
        out = {}
        for att, status, digest in self.store.db.execute(
                "SELECT attachment_id, text_status, text_sha256 FROM attachments"):
            out[att] = (status, None if digest is None else self.store.blob(digest).decode())
        return out

    def test_a_walk_that_spends_the_whole_budget_no_longer_starves_the_gpu_apply(self):
        # 11-22: last in line, the apply never ran (the walks used the budget every run)
        env = {**tm.ENV, "PLX_EXCLUDED_PEOPLE_OIDS": tm.BOX_B}
        now = [0.0]
        real = self.store.walk_cursor

        def walk_cursor(walk):
            if walk == "fetched":      # the fetched walk begins: it takes every second left
                now[0] += 10_000
            return real(walk)
        api, _ = graph_read.build_graph(env, sender=self.graph)
        with mock.patch.dict(os.environ, {capture_mail.GPU_OCR_APPLY_ENV: "1"}), \
                mock.patch.object(self.store, "walk_cursor", side_effect=walk_cursor):
            os.environ.pop("PLX_WINDOW_DIR", None)
            counts = capture_mail.apply_fetched_pass(
                self.store, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
                self.fetch_dir, budget_seconds=600, monotonic=lambda: now[0])
        self.assertEqual(counts["gpu_ocr"]["applied"], 3)
        self.assertEqual(self.texts()["ATT-1"], ("extracted_ocr", "GPU PO 4500012345"))

    def test_gpu_readings_replace_only_where_not_worse_and_only_once(self):
        env = {**tm.ENV, "PLX_EXCLUDED_PEOPLE_OIDS": tm.BOX_B}
        counts = self.apply(env)["gpu_ocr"]
        self.assertEqual(self.texts(), {
            "ATT-1": ("extracted_ocr", "GPU PO 4500012345"),
            "ATT-2": ("extracted_ocr", "GPU LABEL"),
            "ATT-3": ("extracted_ocr", "TESSERACT OLD"),
            "ATT-4": ("extracted_ocr", "TESSERACT OLD"),
            "ATT-5": ("extracted_ocr", "GPU CUT 2"),
            "ATT-6": ("extracted_ocr", "TESSERACT OLD"),     # withheld mailbox: untouched
            "ATT-7": ("extracted_ocr", "TESSERACT OLD")})    # not an image
        self.assertEqual((counts["applied"], counts["attachments_filled"], counts["kept"],
                          counts["withheld"], counts["lap_done"]), (3, 3, 1, 1, 1))
        handle = self.store.db.execute("SELECT l1_handle FROM attachments WHERE "
                                       "attachment_id='ATT-1'").fetchone()[0]
        chain = [v["text_status"] for v in self.store.text_chain(handle)]
        self.assertEqual(chain, ["extracted_ocr", "extracted_ocr"])   # tesseract's kept
        key = g.engine_key(g.expected_hello())
        marked = {h for (h,) in self.store.db.execute(
            "SELECT l1_handle FROM message_events WHERE event=?",
            (capture_mail.GPU_OCR_EVENT + key,))}
        filled = {h for (h,) in self.store.db.execute(
            "SELECT l1_handle FROM attachments WHERE attachment_id IN ('ATT-1','ATT-2','ATT-5')")}
        self.assertEqual(marked, filled)    # the engine that made each new version
        events = self.store.db.execute("SELECT COUNT(*) FROM message_events").fetchone()[0]
        again = self.apply(env)["gpu_ocr"]
        # ATT-1, ATT-2 hold the text already; ATT-5 now has text, so a cut reading is not
        # eligible over it
        self.assertEqual((again["applied"], again["same_text"]), (0, 2))
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM message_events").fetchone()[0], events)
        # a lap that applied nothing: not walked again until the readings change
        self.assertEqual(self.apply(env)["gpu_ocr"].get("unchanged"), 1)
        gpu = g.Store(self.fetch_dir)
        gpu.put(key, [hashlib.sha256(self.imgs[3]).hexdigest()],
                [{"status": "text", "text": "GPU NEW", "pages": 1}])
        gpu.db.close()
        later = self.apply(env)["gpu_ocr"]
        self.assertNotIn("unchanged", later)
        self.assertEqual(self.texts()["ATT-3"], ("extracted_ocr", "GPU NEW"))

    def test_the_apply_is_off_unless_switched_on_and_never_runs_in_a_window(self):
        from ontology import window_lock as wl
        env = {**tm.ENV, "PLX_EXCLUDED_PEOPLE_OIDS": tm.BOX_B}
        before = self.texts()
        for switch in (None, "0", "yes", "true"):
            self.assertNotIn("gpu_ocr", self.apply(env, switch=switch), switch)
        state = self.store.root.parent
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLX_WINDOW_DIR", None)
            wl.take("rebuild-test", reason="rebuild", state_dir=state)
        counts = self.apply(env)["gpu_ocr"]
        self.assertEqual((counts["window"], counts["applied"]), ("held by rebuild-test", 0))
        self.assertEqual(self.texts(), before)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLX_WINDOW_DIR", None)
            wl.release("rebuild-test", state_dir=state)
        self.assertEqual(self.apply(env)["gpu_ocr"]["applied"], 3)

    def test_stored_text_is_bounded_again_when_applied(self):
        env = {**tm.ENV, "PLX_EXCLUDED_PEOPLE_OIDS": tm.BOX_B}
        gpu = g.Store(self.fetch_dir)
        gpu.put(g.engine_key(g.expected_hello()), [hashlib.sha256(self.imgs[1]).hexdigest()],
                [{"status": "text", "text": "PO\x00 77\x1b" + "y" * (g.MAX_TEXT_PER_PAGE + 5),
                  "pages": 1}])
        gpu.db.close()
        self.apply(env)
        # cut at the bound -> truncated -> never replaces tesseract's text
        self.assertEqual(self.texts()["ATT-1"], ("extracted_ocr", "TESSERACT OLD"))

    def test_stored_no_text_markers_are_cleaned_when_applied_without_a_re_read(self):
        env = {**tm.ENV, "PLX_EXCLUDED_PEOPLE_OIDS": tm.BOX_B}
        gpu = g.Store(self.fetch_dir)
        gpu.put(g.engine_key(g.expected_hello()),
                [hashlib.sha256(self.imgs[n]).hexdigest() for n in (1, 2)],
                [{"status": "text", "text": "GPU PO 4500012345 DOCK 4\nNO_TEXT", "pages": 1},
                 {"status": "text", "text": "Zorblax\nNO_TEXT", "pages": 1}])   # a logo
        gpu.db.close()
        self.apply(env)
        texts = self.texts()
        self.assertEqual(texts["ATT-1"], ("extracted_ocr", "GPU PO 4500012345 DOCK 4"))
        self.assertEqual(texts["ATT-2"], ("ocr_no_text", None))   # the made-up word: nothing

    def test_no_usable_exclusion_applies_nothing(self):
        unguarded = {k: v for k, v in tm.ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
        before = self.texts()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLX_EXCLUDED_PEOPLE_OIDS", None)
            out = self.apply(unguarded)
        self.assertNotIn("gpu_ocr", out)
        self.assertEqual(self.texts(), before)


if __name__ == "__main__":
    unittest.main()
