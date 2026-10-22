"""The committed MC bucket audience file (docs/decisions/2026-10-08-mc-follow-up-audience.md).

``operating/mc_bucket_audiences.json`` names who reads each MC follow-up bucket; every host
passes ``load_bucket_audiences`` as the action path's ``mc_follow_up_audience``. A follow-up
quotes a cited source only when its bucket is restricted, every MC member token is an
Entra oid and every member, read as ``employee:<oid>`` through the console's own employee
gate, reads it; one who cannot withholds it; a shared or unparented bucket (every MC
principal reads it, the MCP agents included), an email (any MCP agent may name it), an
``sp_*`` or directory-id token, a null oid, a bucket not in the file or a read past its age
gives citations only, at the send too; a malformed or missing file stops each host at its
start. Seeded fixture mirror, in-process console on 127.0.0.1, signed test tickets, the
isolated MC of ``operating.tests.test_goal2_e2e``; evidence: isolated-fixture.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from operating import action_path as ap
from operating import case_workspace_live
from operating import goal2_mc_approval as g2
from operating import mcp_server
from operating.tests import test_goal2_e2e as e2e
from operator_ui import case_console
from operator_ui.tests import case_fixtures
from tests import test_object_workspace_http as http_tests

FOLLOW_UP_BUCKET = http_tests.FOLLOW_UP_BUCKET
READ_AT = "2026-10-01T09:00:00Z"
READ = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
OID = "aaaaaaaa-0000-4000-8000-0000000000aa"


def member(oid: str | None, read_at: str = READ_AT) -> dict:
    return {"oid": oid, "oid_source": None if oid is None else "test: Entra /users read",
            "read_from": "test: MC project members", "read_at": read_at}


def document(members: dict, bucket: str = FOLLOW_UP_BUCKET,
             visibility: str = "restricted") -> dict:
    return {"schema": g2.MC_BUCKET_AUDIENCE_SCHEMA, "note": "test",
            "buckets": {bucket: {"source_ref": "test: MC project members",
                                 "visibility": visibility, "members": members}}}


class BucketAudienceQuotingTest(unittest.TestCase):
    """The live console's follow-up through the loaded file (the person-controls console of
    ``tests.test_object_workspace_http``, its follow-up bucket's audience the loader's)."""

    setUpClass = http_tests.PersonControlsTest.__dict__["setUpClass"]
    tearDownClass = http_tests.PersonControlsTest.__dict__["tearDownClass"]
    console = http_tests.PersonControlsTest.console
    open_po = http_tests.PersonControlsTest.open_po

    def test_source_text_is_quoted_only_for_a_known_fresh_audience_that_reads_it(self):
        tmp = Path(tempfile.mkdtemp(prefix="bucket-audiences-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        now = [READ]
        loaded: list[g2.BucketAudiences] = []

        def load(content: dict) -> None:
            path = tmp / f"audiences-{len(loaded)}.json"
            path.write_text(json.dumps(content), encoding="utf-8")
            loaded.append(g2.load_bucket_audiences(path, tenant="plx", clock=lambda: now[0]))

        call = self.console(live=True, audience=lambda bucket: loaded[-1](bucket))
        po, _view = self.open_po(call)
        author = {self.ids.OID_A: member(self.ids.OID_A)}
        stranger = {self.ids.OID_B: member(self.ids.OID_B)}
        unread = {"cccccccc-0000-4000-8000-0000000000cc": member(None)}
        mailbox = {"ricardo@isolated-fixture.example": member(self.ids.OID_A)}
        day = timedelta(days=1)
        for number, (content, age, reason) in enumerate((
                (document(author), day, None),   # the author reads the cited PO
                # any MCP agent naming the author's email reads the bucket
                (document(mailbox), day, g2.WITHHELD_UNRESOLVED),
                (document({**mailbox, **author}), day, g2.WITHHELD_UNRESOLVED),
                (document({**author, **stranger}), day, g2.WITHHELD_NOT_READABLE),
                (document({**author, **unread}), day, g2.WITHHELD_UNRESOLVED),
                (document(author, bucket="BKT-OTHER"), day, g2.WITHHELD_UNRESOLVED),
                (document(author), 31 * day, g2.WITHHELD_UNRESOLVED),   # past its re-read age
                (document(author), timedelta(minutes=-2), None),   # within the clock skew
                (document(author), 30 * day, None),
                # every MC principal reads it, the MCP agents included
                (document(author, visibility="shared"), day, g2.WITHHELD_UNRESOLVED),
                (document(author, visibility="unparented"), day, g2.WITHHELD_UNRESOLVED),
                # an agent, or every agent its person operates, reads it
                (document({**author, "sp_mcp_claude_code": member(None)}), day,
                 g2.WITHHELD_UNRESOLVED),
                (document({**author, "b.reader": member(None)}), day,
                 g2.WITHHELD_UNRESOLVED))):
            with self.subTest(number=number, reason=reason):
                now[0] = READ + age
                load(content)
                status, proposed = call("POST", "/api/object-follow-up", {
                    "object_id": po, "missing": f"The confirmed ship date ({number}).",
                    "why": "Production waits.", "who_can_answer": ["purchasing"]})
                self.assertEqual(status, 200, proposed)
                proposal = proposed["proposal"]
                description = proposal["prepared_request"]["arguments"]["description"]
                self.assertTrue(proposal["cited"])
                for index, handle in enumerate(proposal["cited"], 1):
                    line = f"\n\n[{index}] {handle}\n"
                    self.assertIn(line, description)
                    shown = description.split(line, 1)[1].split("\n\n", 1)[0]
                    if reason is None:   # the row's fields, as the author reads them
                        self.assertIn("\nstatus: ", shown)
                        self.assertNotIn("text withheld", shown)
                    else:
                        self.assertEqual(shown, reason)


class HostStartTest(unittest.TestCase):
    """Every host serving the action path takes the loaded file as its follow-up audience,
    and refuses to start (exit 2, a typed error, no traceback) on a malformed or missing
    one."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = self.tmp / "state"
        self.seeded = case_fixtures.seed(self.state, "plx")
        self.built: list[ap.ActionPath] = []
        init = ap.ActionPath.__init__

        def spy(path, *args, **kwargs):
            init(path, *args, **kwargs)
            self.built.append(path)
        patch = mock.patch.object(ap.ActionPath, "__init__", spy)
        patch.start()
        self.addCleanup(patch.stop)

    def start(self, host: str) -> None:
        audience = str(self.state / case_fixtures.MIRROR_AUDIENCE_FILE)
        if host == "console":
            with mock.patch.object(case_console, "serve", lambda **served: None):
                case_console.main([
                    "--state-dir", str(self.state), "--fixture",
                    "--principal", case_fixtures.OPERATIONS,
                    "--action-store", str(self.state / case_fixtures.ACTIONS_FILE),
                    "--mc-follow-up-bucket", FOLLOW_UP_BUCKET], fixtures=case_fixtures)
        elif host == "mcp":
            started, _server = mcp_server.session_server([
                "--state-dir", str(self.state), "--tenant", "plx", "--fixture",
                "--principal", case_fixtures.OPERATIONS, "--source-mirror-audience",
                audience, "--enable-action-path"])
            started.case_runtime.close()
        else:
            case_workspace_live.main([
                "--state-dir", str(self.state), "--mirror-audience", audience,
                "--principal", case_fixtures.OPERATIONS, "--case-id", self.seeded["po"]])

    def test_each_host_serves_the_file_and_refuses_a_malformed_one(self):
        good = self.tmp / "good.json"
        good.write_text(json.dumps(document(
            {"a.reader@isolated-fixture.example": member(OID)})),
            encoding="utf-8")
        valid = document({"a.reader@isolated-fixture.example": member(None)})
        reader = valid["buckets"][FOLLOW_UP_BUCKET]["members"]["a.reader@isolated-fixture.example"]
        future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with_oid = {**reader, "oid": OID, "oid_source": "test"}
        entry = json.dumps(valid["buckets"][FOLLOW_UP_BUCKET])
        broken = (
            ("mc_bucket_audience_document_invalid", document(  # the v1 file
                {"a@b.example": reader}) | {"schema": "plx-mc-bucket-audience/v1"}),
            ("mc_bucket_audience_document_invalid", {**valid, "schema": "other/v1"}),
            ("mc_bucket_audience_document_invalid", {**valid, "extra": 1}),
            ("mc_bucket_audience_bucket_invalid", document({}, bucket=FOLLOW_UP_BUCKET)),
            ("mc_bucket_audience_bucket_invalid", document({"a@b.example": reader},
                                                           bucket="not-a-bucket")),
            ("mc_bucket_audience_bucket_invalid", document({"a@b.example": reader},
                                                           visibility="private")),
            ("mc_bucket_audience_bucket_invalid", document({"a@b.example": reader},
                                                           visibility=["restricted"])),
            ("mc_bucket_audience_duplicate_key",
             f'{{"schema": "{g2.MC_BUCKET_AUDIENCE_SCHEMA}", "note": "t", "buckets": '
             f'{{"{FOLLOW_UP_BUCKET}": {entry}, "{FOLLOW_UP_BUCKET}": {entry}}}}}'),
            ("mc_bucket_audience_member_invalid", document({"not a token": reader})),
            ("mc_bucket_audience_member_invalid", document(
                {"a@b.example": {**reader, "role": "admin"}})),
            ("mc_bucket_audience_duplicate_member", document(
                {"a@b.example": reader, " A@b.example": reader})),
            ("mc_bucket_audience_duplicate_member", document(
                {OID: with_oid, OID.upper(): with_oid})),
            ("mc_bucket_audience_oid_not_a_person", document({"sp_mcp_cos": with_oid})),
            ("mc_bucket_audience_oid_invalid", document(
                {"a@b.example": {**reader, "oid": "someone", "oid_source": "test"}})),
            ("mc_bucket_audience_oid_without_source", document(
                {"a@b.example": {**reader, "oid": OID}})),
            ("mc_bucket_audience_read_at_invalid", document(
                {"a@b.example": {**reader, "read_at": "2026-10-01"}})),
            ("mc_bucket_audience_read_at_future", document(
                {"a@b.example": {**reader, "read_at": future}})),
            ("Expecting property name", "{"),
            ("No such file", "missing"))
        for host in ("console", "mcp", "live"):
            with self.subTest(host=host):
                self.built.clear()
                with mock.patch.object(g2, "DEFAULT_MC_BUCKET_AUDIENCES", good), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.start(host)
                self.assertEqual(len(self.built), 1)
                served = self.built[0].mc_follow_up_audience
                self.assertIsInstance(served, g2.BucketAudiences)
                self.assertEqual(set(served.buckets), {FOLLOW_UP_BUCKET})
            for code, content in broken:
                with self.subTest(host=host, code=code):
                    path = self.tmp / f"broken-{host}.json"
                    path.unlink(missing_ok=True)
                    if content != "missing":
                        path.write_text(content if isinstance(content, str)
                                        else json.dumps(content), "utf-8")
                    self.built.clear()
                    errors = io.StringIO()
                    with mock.patch.object(g2, "DEFAULT_MC_BUCKET_AUDIENCES", path), \
                            contextlib.redirect_stderr(errors), \
                            contextlib.redirect_stdout(io.StringIO()), \
                            self.assertRaises(SystemExit) as stopped:
                        self.start(host)
                    self.assertEqual(stopped.exception.code, 2)
                    self.assertIn(code, errors.getvalue())
                    self.assertNotIn("Traceback", errors.getvalue())
                    self.assertEqual(self.built, [])

    def test_the_tokens_of_one_person_are_one_member_and_a_plus_tag_is_its_own(self):
        """MC compares trimmed lower-case tokens (project-acl.ts ``normalizeMemberToken``):
        an email and the oid token of one person are one member, a plus-tagged email is
        another token, as MC reads it."""
        path = self.tmp / "people.json"
        later = "2026-10-02T09:00:00Z"
        for members, people in (
                ({"a@b.example": {**member(OID), "read_at": later}, OID: member(OID)},
                 ((OID, READ),)),   # read at the oldest of its reads
                ({"a@b.example": member(None), "a+tag@b.example": member(None)},
                 ((None, READ), (None, READ)))):
            with self.subTest(members=sorted(members)):
                path.write_text(json.dumps(document(members)), "utf-8")
                audiences = g2.load_bucket_audiences(path, tenant="plx",
                                                     clock=lambda: READ + timedelta(days=1))
                self.assertEqual(audiences.buckets[FOLLOW_UP_BUCKET],
                                 ("test: MC project members", False, people))
                self.assertIsNone(audiences(FOLLOW_UP_BUCKET))   # an email token

    def test_the_committed_file_invents_no_member(self):
        """Placeholder-free: the committed file loads, and every member it may name has an
        oid only with its source (the loader's rule); none is read yet, so it is empty."""
        audiences = g2.load_bucket_audiences(tenant="plx")
        self.assertEqual(dict(audiences.buckets), {})
        self.assertIsNone(audiences(FOLLOW_UP_BUCKET))


class SharedBucketSendTest(e2e.Goal2World):
    """At the send, through the real client's write door into the isolated MC: a follow-up
    to a shared bucket carries its citations, never the source text, while the same path
    quotes for an audience that reads the source."""

    cited_follow_up = e2e.McFollowUpTests.cited_follow_up
    audience_path = e2e.McFollowUpTests.audience_path
    members = e2e.McFollowUpTests.members
    prepared_description = e2e.McFollowUpTests.prepared_description

    def setUp(self) -> None:
        super().setUp()
        self.world.mc.buckets.add(e2e.gx.FOLLOW_UP_BUCKET)
        self.writer = self.world.mc.client(self.dir / "mc-create-ledger.jsonl",
                                           write_tools=frozenset({"mc_create_task"}))

    def sent(self, number: int) -> tuple[str, str]:
        iid, handle, _description = self.prepared_description(number)
        self.world.approve_follow_up(iid)
        sent = self.iso.send_call(e2e.ACTOR, iid, self.writer, session=e2e.gx.SESSION)
        return handle, self.world.mc.tasks[sent["task_id"]]["description"]

    def test_a_shared_bucket_sends_citations_only(self):
        path = self.dir / "shared.json"
        path.write_text(json.dumps(document({OID: member(OID)}, bucket=e2e.gx.FOLLOW_UP_BUCKET,
                                            visibility="shared")), "utf-8")
        shared = g2.load_bucket_audiences(path, tenant="plx",
                                          clock=lambda: READ + timedelta(days=1))
        self.audience_path()
        self.members(e2e.ACTOR)   # the control: an audience that reads it is quoted
        handle, description = self.sent(51)
        self.assertIn(f"[1] {handle}\nid: 5100\nnote: Supplier wrote", description)
        self.audience = shared
        handle, description = self.sent(52)
        self.assertIn(f"\n\n[1] {handle}\n{g2.WITHHELD_UNRESOLVED}", description)
        self.assertNotIn("Supplier wrote", description)


if __name__ == "__main__":
    unittest.main()
