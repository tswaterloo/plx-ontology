"""Host follow-ups: Teams channel messages served on the real host, the review store read
if present (never created by a build, picked up by a running MCP host), and one mention
link per object when a reviewed alias and the identifier index name it alike.

Evidence label: ``injected-transport`` (fake Graph, BC and Portal Workbench senders behind
the real GET-only clients, as ``operating/tests/test_host.py``; no network, no credential)
and ``isolated-fixture`` for the step-list builds.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from integrations.tests import test_capture_mail as mail_t
from integrations.tests import test_capture_teams as teams_t
from ontology import identifiers, mentions, reviews
from ontology import schema as schema_loader
from ontology.project import CapturedRecord, build_store
from operating import build_steps
from operating import host as host_module
from operating import session_identity as si
from operating.tests.test_host import ENV, INIT, SVC, Directory, HostCase, Lines, call
from operating.tests.test_session_identity import EMAIL_A, OID_A, SECRET, Fixture
from tests.test_host_one_build import AUTHOR, BOX, PO_ID, StaticMail, StaticPortal

EMPLOYEE = f"employee:{OID_A}"
EXCLUDED = "PLX_EXCLUDED_PEOPLE_OIDS"
ALIAS = "PI-TST900222"   # a synthetic supplier number


class HostTeamsAndReviewsTests(HostCase):
    """``host run`` captures the Teams channels of ``test_host``'s fake Graph (a standard
    channel the employee is a member of, a private one it is not) and projects them; ``host
    mcp`` serves them per reader."""

    def setUp(self) -> None:
        super().setUp()
        self.router.teams.team_members += [("0000aaaa-0000-0000-0000-00000000000e",
                                            mail_t.UPN_A), (OID_A, EMAIL_A)]
        self.config = self.write_config(entra_directory=True)
        self.grant_teams(["staff:operations"])
        self.cycle = self.run_cycle()

    def grant_teams(self, readers) -> None:
        """The live audience: Teams granted to the capture principal and ``readers``. The
        employee is a team member by its own directory identity (the employee item ACL
        decides on it); the file's static identity for it names another member, which
        grants nothing."""
        path = self.root / "config" / "capture-audience.json"
        document = json.loads(path.read_text("utf-8"))
        document["audiences"]["teams:*"] = [SVC, *readers]
        document["classifications"]["teams:*"] = "internal"
        document["identities"] = {EMPLOYEE: [mail_t.UPN_A]}
        path.write_text(json.dumps(document), "utf-8")

    def mcp(self, lines) -> dict:
        output = io.BytesIO()
        # The MCP session reads the people exclusion its captured Teams are served under
        # from its process environment, as ``host mcp`` does.
        with patch.dict(os.environ, {"T": Fixture.ticket(author=si.AUTHOR_AGENT),
                                     si.SESSION_TICKET_SECRET_ENV: SECRET,
                                     EXCLUDED: ENV[EXCLUDED]}):
            code, _out, err = self.main(
                "mcp", "--state-dir", str(self.state), "--config", str(self.config),
                "--session-ticket-env", "T", sender=Directory(), monotonic=lambda: 0.0,
                input_stream=lines, output_stream=output)
        self.assertEqual(code, 0, err)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        return {r["id"]: r["result"] for r in replies if "id" in r}

    def query(self, sql, *params):
        conn = sqlite3.connect(host_module.projection_path(self.state))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_channel_messages_reach_a_member_reader_and_no_one_else(self):
        self.assertIn("teams", self.cycle["projection"]["sources"], self.cycle)
        units = dict(self.query("SELECT object_id, audience_key FROM objects "
                                "WHERE object_type = 'TeamsChannelMessage'"))
        general = sorted(o for o, unit in units.items() if teams_t.GENERAL in unit)
        private = sorted(set(units) - set(general))
        self.assertEqual((len(general), len(private)), (2, 1), units)   # root + reply; root
        find = {"object_type": "TeamsChannelMessage", "limit": 50,
                "filters": [{"property": "id", "op": "present"}]}
        requests = [call(2, "plx.find", find), call(3, "plx.object", {"object_id": general[0]}),
                    call(4, "plx.object", {"object_id": private[0]}),
                    call(5, "plx.timeline", {"from": private[0]})]
        seen = {}
        for readers in (["staff:operations"], []):   # granted, then not (default deny)
            self.grant_teams(readers)
            replies = self.mcp(Lines(INIT + requests))
            found = replies[2]["structuredContent"]
            seen[bool(readers)] = (
                sorted(c["object"]["object_id"] for c in found["candidates"]),
                not replies[3]["isError"] and "error" not in replies[3]["structuredContent"])
            for withheld in (replies[4], replies[5]):   # outside the private channel
                self.assertTrue(withheld["isError"]
                                or "error" in withheld["structuredContent"], withheld)
            text = json.dumps(replies)
            if not readers:
                self.assertNotIn(teams_t.SECRET_TEXT, text)
            self.assertNotIn("Board-only", text)
        self.assertEqual(seen, {True: (general, True), False: ([], False)}, seen)
        # Neither the host's builds nor the MCP host created a review store.
        self.assertFalse(reviews.store_path(self.state).exists())

    def test_a_review_store_created_after_start_is_read_without_a_restart(self):
        ((subject, handle, digest),) = self.query(
            "SELECT o.object_id, v.l1_handle, v.digest FROM objects o JOIN object_versions v "
            "ON v.object_id = o.object_id WHERE o.object_type = 'SalesOrder' "
            "AND v.superseded_at IS NULL ORDER BY o.object_id LIMIT 1")

        def review():
            reviews.ReviewStore(reviews.store_path(self.state)).record(
                tenant="plx", author=AUTHOR, decision="alias", subject=subject,
                alias={"identifier_kind": "vendor_confirmation", "normalized": ALIAS,
                       "raw": ALIAS},
                evidence=[{"object_id": subject, "l1_handle": handle, "digest": digest,
                           "field_path": "row.id"}])

        self.assertFalse(reviews.store_path(self.state).exists())
        replies = self.mcp(Lines(INIT + [call(2, "plx.find", {"query": ALIAS}),
                                         call(3, "plx.find", {"query": ALIAS})],
                                 hooks={3: review}))
        found = [[c["object"]["object_id"] for c in replies[n]["structuredContent"]
                  .get("candidates", ())] for n in (2, 3)]
        self.assertEqual(found, [[], [subject]], replies)

    def test_an_unreadable_review_store_is_a_typed_gap_and_fails_the_cycle_by_path(self):
        """Directive 1c0959b3d22e (1, 2): a review store that is there but cannot be read
        is never answered as "no reviews": plx.find (MCP) and plx.review status (served
        tools) say why they hold none, and the host's cycle record names the store."""
        from ontology.core import Actor
        from operating import ontology_tools_review
        ((subject,),) = self.query("SELECT object_id FROM objects WHERE object_type = "
                                   "'SalesOrder' ORDER BY object_id LIMIT 1")
        path = reviews.store_path(self.state)
        path.write_bytes(b"not a database\n" * 256)
        found = self.mcp(Lines(INIT + [call(2, "plx.find", {"query": ALIAS})]))[2]
        self.assertFalse(found["isError"], found)
        found = found["structuredContent"]
        self.assertEqual((found["gaps"][0]["reason"], found["gaps"][0]["subject"]),
                         ("review_store_unreadable", path.name), found)
        self.assertEqual(found["completeness"]["complete"], False, found)
        host = host_module.Host(self.state, host_module.HostConfig.from_file(self.config),
                                environ={}, sender=Directory())
        reader = Actor("plx", frozenset({"staff:operations"}), "internal")
        with host.served_tools(reader) as served:
            ontology_tools_review.register(served, None)
            status = served.call("plx.review", {"mode": "status", "subject": subject},
                                 reader)
        self.assertEqual((status["reviews"], status["completeness"]["complete"],
                          status["completeness"]["reason"]),
                         ([], False, "review_store_unreadable"), status)
        cycle = self.run_cycle()
        self.assertEqual((cycle["state"], cycle["projection"]["state"],
                          cycle["projection"]["reasons"]),
                         ("failed", "failed", [f"ReviewStoreUnreadable:review_store_unreadable:"
                                               f"{path}"]), cycle["projection"])


T0, T1, T2, T3 = ("2026-09-01T10:00:00.000000Z", "2026-09-05T10:00:00.000000Z",
                  "2026-09-12T10:00:00.000000Z", "2026-09-20T10:00:00.000000Z")
PO_NUMBER = "PO100236"
RELATION = "public.generated_purchase_order"


def po_versions():
    """The PO names ``PO_NUMBER`` itself only in its middle version [T1, T2)."""
    rows = [(T0, T1, "PO999001"), (T1, T2, PO_NUMBER), (T2, None, "PO999002")]
    return [CapturedRecord("portal_api", RELATION, json.dumps({"id": 9}),
                           {"id": 9, "ponumber": number}, f"api://{RELATION}/9@g-{n}", "row",
                           str(n) * 64, start, end, start, f"api:{RELATION}")
            for n, (start, end, number) in enumerate(rows, 1)]


def mail(mid, subject):
    handle = f"graph-mail://{BOX}/{mid}@CK-1"
    return CapturedRecord("graph_mail", "messages", json.dumps([mid, BOX]),
                          {"mailbox": BOX, "id": mid, "subject": subject,
                           "from": "sales@supplier.test", "received_at": "2026-09-01T09:59:00Z"},
                          handle, "row", "1" * 64, T0, None, T0, f"mail:{BOX}")


class BuildStepTests(unittest.TestCase):
    def build(self, state: Path, mails) -> sqlite3.Connection:
        path = state / "projection.sqlite"
        build_store(path, schema_loader.load_schema(),
                    {"portal_api": StaticPortal(po_versions()), "graph_mail": StaticMail(mails)},
                    normalizer=identifiers.normalize,
                    clock=lambda: dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
                    post_project=build_steps.post_project(state, "plx"))
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        return conn

    def test_a_check_build_in_an_empty_directory_creates_no_review_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.build(Path(tmp), [mail("M-1", f"Shipping {PO_NUMBER}")])
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 0)
            self.assertFalse(reviews.store_path(tmp).exists())
            with self.assertRaises(reviews.ReviewError):
                build_steps.ReviewsIfPresent(reviews.store_path(tmp)).record(
                    tenant="plx", author=AUTHOR, decision="alias", subject=PO_ID, evidence=[])
            self.assertFalse(reviews.store_path(tmp).exists())

    def test_one_lead_link_per_object_when_a_reviewed_alias_and_the_index_both_match(self):
        """The PO number confirmed as the PO's alias from T0 on while the PO's own field
        names it only over [T1, T2): every later mail naming it links to the PO once at
        every time, through the alias before and after, the index in between."""
        (found,) = mentions.extract(f"Shipping {PO_NUMBER}")
        kind = found["identifier_kind"]
        evidence = mail("M-0", f"Order confirmation {PO_NUMBER}")
        for n in (1, 10, 60):
            with tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                reviews.ReviewStore(reviews.store_path(state), clock=lambda: dt.datetime(
                    2026, 9, 1, 10, tzinfo=dt.timezone.utc)).record(
                    tenant="plx", author=AUTHOR, decision="alias", subject=PO_ID,
                    alias={"identifier_kind": kind, "normalized": PO_NUMBER,
                           "raw": PO_NUMBER},
                    evidence=[{"object_id": 'Email:graph_mail.messages:{"id":"M-0",'
                                            '"mailbox":"orders@plx.test"}',
                               "l1_handle": evidence.l1_handle, "digest": evidence.digest,
                               "field_path": "row.subject"}])
                later = [mail(f"M-{i}", f"Shipping {PO_NUMBER} booked") for i in range(1, n + 1)]
                conn = self.build(state, [evidence, *later])
                links: dict[str, list] = {}
                for source, start, end, reason in conn.execute(
                        "SELECT from_object, recorded_at, superseded_at, reason FROM links "
                        "WHERE to_object = ? AND tier = 'lead' ORDER BY recorded_at",
                        (PO_ID,)):
                    via = [f.get("via") for f in json.loads(reason) if "via" in f]
                    links.setdefault(source, []).append((start, end, via))
                mails = [s for s in links if '"M-0"' not in s]
                self.assertEqual(len(mails), n)
                for source in mails:
                    self.assertEqual(links[source], [
                        (T0, T1, ["reviewed_alias"]), (T1, T2, ["identifier_index"]),
                        (T2, None, ["reviewed_alias"])], (n, source))


if __name__ == "__main__":
    unittest.main()
