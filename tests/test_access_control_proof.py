"""``access.control_proof`` (the acceptance bar): person A asking for person B's mail, a non-member
asking for Teams content and a non-finance reader asking for a finance-only file are each
refused with a typed reason by every consumer; an excluded person's data reaches nobody.

The world is captured through the GET-only client over fake Graph transports
(isolated-fixture / injected-transport, never live) into a temporary state directory, and
the printed summary is checked against the committed spec with ``check_expectations``.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
import uuid

from devloop import live_verify
from integrations import capture_sharepoint as cs
from integrations import graph_read
from integrations.capture_mail import CaptureScope, MailCapture, MailCaptureStore
from integrations.capture_sharepoint import SharePointCapture, SharePointCaptureStore
from integrations.capture_teams import TeamsCapture, open_store
from integrations.graph_read import GraphMailReader, SharePointReader
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests import test_capture_mail as tcm
from integrations.tests import test_capture_sharepoint as tcs
from integrations.tests import test_capture_teams as tct
from ontology import identifiers
from ontology.project import build_store
from ontology.schema import load_schema
from ontology.types import Actor
from operating import cases as case_store
from operating import host as host_module
from operating import ontology_tools as ot
from operating.case_mail_scope import MailScope
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaseCaptures
from operating.employee_permissions import (EmployeePermissionPolicy,
                                            EmployeePermissionResolver, EntraDirectoryRead,
                                            POLICY_SCHEMA, PORTAL_ACCESS_SNAPSHOT_SCHEMA,
                                            PortalAccessSnapshot)
from operating.tests.test_employee_permissions import TID
from scripts import access_control_proof as proof
from tests.test_customer_po_scope import _Rows, _so

SPEC = json.loads(Path("devloop/live-checks/access.control_proof.json").read_text())
EXCL = "0e0e0e0e-0000-4000-8000-00000000e0e1"   # tcm.ENV's excluded person
UPN_C = "excluded@example-plx.com"
OID_F = "ffff0000-0000-4000-8000-00000000000f"   # finance (directory and Portal)
OID_N = "eeee0000-0000-4000-8000-00000000000e"   # operations
OID_D = "dddd0000-0000-4000-8000-00000000000d"   # a colleague whose mailbox is not captured
UPN_D = "dee@example-plx.com"
NOBACKOFF = dict(retry=BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0))
DEPARTMENTS = {OID_F: "Finance", OID_N: "Operations"}


def directory(principal):
    return EntraDirectoryRead("read", principal.object_id,
                              department=DEPARTMENTS.get(principal.object_id, "Operations"),
                              group_ids=frozenset(), source="graph:directory@fixture",
                              read_at="2026-10-01T09:00:00Z")


def _user(uid, oid, email, codes=()):
    return {"id": uid, "email": email, "role": "STAFF", "accessLevel": "L2", "isActive": True,
            "entraTenantId": TID, "entraObjectId": oid,
            "departmentMemberships": [{"code": c, "provenance": "PORTAL", "departmentActive": True}
                                      for c in codes]}


BASE_USERS = [_user("cmusera", tcm.BOX_A, tcm.UPN_A), _user("cmuserb", tcm.BOX_B, tcm.UPN_B),
              _user("cmusercos", tct.U_COS, tct.COS), _user("cmuservince", tct.U_VINCE, tct.VINCE),
              _user("cmuserf", OID_F, tcs.SALES, ["FIN"]), _user("cmusern", OID_N, tcs.RICARDO, ["OPS"]),
              _user("cmuserc", EXCL, UPN_C)]


def build(root: Path, *, capture_excluded: bool = False, statements=()) -> Path:
    """Mail of A, B and the excluded person C (captured only when ``capture_excluded``: C
    was excluded after capture, as were B's and D's messages to A; D's own mailbox is not
    captured), Teams channel/private/chat messages, SharePoint files of which one is a
    finance-only tax form; the host's config files, a projection of Portal orders
    (``PO_ROWS``) and a stored case (``write_cases``)."""
    state = root / "state"
    graph = tcm.FakeGraph()
    graph.users[EXCL] = {"id": EXCL, "userPrincipalName": UPN_C, "mail": UPN_C,
                         "proxyAddresses": [f"SMTP:{UPN_C}"]}
    graph.folders[EXCL] = {"inbox": "AAMkFOLDER-C=", "aamkfolder-c=": "AAMkFOLDER-C="}
    graph.messages[EXCL] = {}
    graph.put(tcm.BOX_A, "MSG-A1", "CKA-1", body="A note")
    graph.put(tcm.BOX_B, "MSG-B1", "CKB-1", body="B note")
    graph.put(EXCL, "MSG-C1", "CKC-1", body="C note")
    if capture_excluded:
        # B wrote to A before B was excluded (``ExcludedAfterCaptureTest``).
        graph.put(tcm.BOX_A, "MSG-A2", "CKA-2", body="B to A")
        graph.messages[tcm.BOX_A]["MSG-A2"]["from"] = {"emailAddress": {"address": tcm.UPN_B}}
        # D, a colleague whose own mailbox is outside the capture, wrote to A (MSG-A3).
        graph.users[OID_D] = {"id": OID_D, "userPrincipalName": UPN_D, "mail": UPN_D,
                              "proxyAddresses": [f"SMTP:{UPN_D}"]}
        graph.put(tcm.BOX_A, "MSG-A3", "CKA-3", body="D to A")
        graph.messages[tcm.BOX_A]["MSG-A3"]["from"] = {"emailAddress": {"address": UPN_D}}
    boxes = (tcm.UPN_A, tcm.UPN_B, UPN_C)
    scope = CaptureScope([{"address": a, "folders": ["inbox"]} for a in boxes], window_days=90,
                         internal_domains=["example-plx.com"], allowlist=MailScope(list(boxes)))
    audience = LiveSourceAudience({f"mail:{a}": ["svc:mail-capture"] for a in boxes},
                                  tenant="plx", classifications={"mail:*": "restricted"})
    env = dict(tcm.ENV)
    if capture_excluded:
        env["PLX_EXCLUDED_PEOPLE_OIDS"] = "0e0e0e0e-0000-4000-8000-00000000ffff"
    store = MailCaptureStore(state, clock=tcm.Clock())
    api, _ = graph_read.build_graph(env, sender=graph, **NOBACKOFF)
    MailCapture(store, GraphMailReader(api, audience), tcm.CAPTURE, sleep=lambda s: None).run(scope)
    store.close()

    teams = tct.FakeTeamsGraph()
    teams.post_root(tct.GENERAL, "1001")
    teams.post_root(tct.PRIVATE, "2001", "Board-only pricing")
    teams.post_chat("3001")
    taudience = LiveSourceAudience({"teams:*": ["svc:teams-capture"]}, tenant="plx",
                                   classifications={"teams:*": "internal"})
    tstore = open_store(state, taudience)
    api, _ = graph_read.build_graph(tct.ENV, sender=teams)
    TeamsCapture(tstore, api, taudience, tct.CAPTURE).run(tct.scope())
    tstore.close()

    sstore = SharePointCaptureStore(state, clock=lambda: datetime.now(timezone.utc))
    api, transport = graph_read.build_graph(tcs.ENV, sender=tcs.seeded_drive(), **NOBACKOFF)
    SharePointCapture(sstore, lambda d: SharePointReader(
        api, tcs.capture_audience(), d, transport=transport, read_permissions=True,
        max_acl_reads=cs.MAX_ACL_READS), tcs.CAPTURE).run(tcs.scope())
    with sstore.db:
        for rowid, raw in sstore.db.execute(
                "SELECT rowid, record FROM items WHERE item_id = '01FILEB'").fetchall():
            record = json.loads(raw)
            record["list_item_fields"] = {"source": "sharepoint-listitem-fields/v1",
                                          "drive_etag": record["etag"],
                                          "fields": {"DocumentCategory": "TAX_FORM"}}
            sstore.db.execute("UPDATE items SET record = ? WHERE rowid = ?",
                              (json.dumps(record), rowid))
    sstore.close()

    write_users(root, BASE_USERS)
    (root / "policy.json").write_text(json.dumps({
        "schema": POLICY_SCHEMA, "tenant": "plx", "entra_tenant_id": TID,
        "clearance_by_access_level": {"L1": "internal", "L2": "internal", "L3": "confidential",
                                      "L4": "confidential", "L5": "restricted"},
        "principals": {"staff:all": {"portal_roles": ["STAFF", "MANAGER", "ADMIN"]}},
        "service_principal_object_ids": []}))
    (root / "live.json").write_text(json.dumps({
        "schema": "plx-live-source-audience/v1", "tenant": "plx",
        "audiences": {"mail:*": ["staff:all"], "teams:*": ["staff:all"],
                      "sharepoint:*": ["staff:all"]},
        "classifications": {"mail:*": "internal", "teams:*": "internal",
                            "sharepoint:*": "internal"},
        "source_acl_required": ["sharepoint:*"],
        "identities": {"staff:all": [tcs.RICARDO, tcs.SALES]}}))
    (root / "mirror.json").write_text(json.dumps({
        "schema": AUDIENCE_SCHEMA, "tenant": "plx",
        "audiences": {"public.app_sales_order": ["staff:all"]}}))
    write_config(root)
    build_store(host_module.projection_path(state), load_schema(),
                {"portal_api": _Rows(PO_ROWS)}, normalizer=identifiers.normalize)
    write_cases(root, state, statements)
    return state


# Portal orders whose po_reference is a PO number, a person's name and a placeholder.
PO_ROWS = [_so("so-1", "PO-4471", "c-1"), _so("so-2", "Jane Smith", "c-1"),
           _so("so-3", "N/A", "c-2")]
NON_PO = ("Jane Smith", "N/A")


def write_cases(root: Path, state: Path, statements=()) -> None:
    """A case A opened for all staff: a fact and a gap citing A's own mail (no other
    reader resolves them) and an uncited interpretation (``statements`` more of them)."""
    config = host_module.HostConfig.from_file(root / "host.json")
    policy = EmployeePermissionPolicy.from_file(config.employee_policy)
    runtime = CaseRuntime(state, client=None, audience=config.mirror_audience, tenant="plx",
                          live=LiveReaders(LiveSourceAudience.from_file(config.live_audience)),
                          employee_permissions=EmployeePermissionResolver(
                              policy, PortalAccessSnapshot(config.portal_access),
                              directory=directory),
                          require_employee=True,
                          mail_scope=MailScope([tcm.UPN_A, tcm.UPN_B, UPN_C]),
                          people_exclusion=PeopleExclusion.from_env(
                              {"PLX_EXCLUDED_PEOPLE_OIDS": EXCL}))
    try:
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=state,
                                        sharepoint_capture_dir=state)
        db = sqlite3.connect(state / "mail-capture" / "capture.db")
        [handle] = [r[0] for r in db.execute(
            "SELECT l1_handle FROM messages WHERE message_id = 'MSG-A1'")]
        db.close()
        owner = Actor("plx", frozenset({"employee:" + tcm.BOX_A}))
        case = runtime.cases.open(owner, "Has order SO000001 shipped?", audience=["staff:all"])
        runtime.cases.finding(owner, case, SECRET_STATEMENT, "fact", "supported",
                              citations=[handle])
        runtime.cases.gap(owner, case, SECRET_GAP, "A's note is the only record",
                          "the dispatcher", "unknown", citations=[handle])
        for statement in ("Order SO000001 is confirmed", *statements):
            runtime.cases.finding(owner, case, statement, "interpretation", "unknown")
    finally:
        runtime.close()


SECRET_STATEMENT = "A's note says the carrier collected on Tuesday"
SECRET_GAP = "carrier tracking number"


def write_users(root: Path, users) -> None:
    (root / "portal.json").write_text(json.dumps({
        "schema": PORTAL_ACCESS_SNAPSHOT_SCHEMA, "source": "portal:fixture",
        "read_at": "2026-10-01T09:00:00Z", "users": users}))


def write_config(root: Path, **drop) -> Path:
    config = {"schema": "plx-host-config/v1", "tenant": "plx", "mirror_audience": "mirror.json",
              "live_audience": "live.json", "employee_policy": "policy.json",
              "portal_access": "portal.json"}
    for key in drop:
        config.pop(key)
    path = root / "host.json"
    path.write_text(json.dumps(config))
    return path


def environ(root: Path, **extra) -> dict[str, str]:
    env = {"PLX_WORKBENCH_STATE_DIR": str(root / "state"),
           "PLX_WORKBENCH_HOST_CONFIG": str(root / "host.json"),
           "PLX_EXCLUDED_PEOPLE_OIDS": EXCL}
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


def run(env, argv=()) -> tuple[int, dict]:
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = proof.main(list(argv), environ=env, directory=directory,
                          mail_scope=MailScope([tcm.UPN_A, tcm.UPN_B, UPN_C]))
    return code, json.loads(printed.getvalue())


def copy_world(root: Path, target: Path) -> Path:
    """A copy of the world at ``root`` (its own state, config and stores) at ``target``."""
    shutil.copytree(root, target, symlinks=True)
    return target


def append_finding(cases_file: Path, **payload) -> None:
    """Append, past the case store's own API, a finding event to the stored case whose
    payload is the last finding's with ``payload`` over it (the event chain kept intact)."""
    db = sqlite3.connect(cases_file)
    db.row_factory = sqlite3.Row
    try:
        last = db.execute("SELECT * FROM case_event WHERE kind = 'finding.recorded' "
                          "ORDER BY seq DESC LIMIT 1").fetchone()
        top = db.execute("SELECT case_seq, event_hash FROM case_event WHERE case_id = ? "
                         "ORDER BY case_seq DESC LIMIT 1", (last["case_id"],)).fetchone()
        body = json.dumps({**json.loads(last["payload_json"]), "finding_id": "F9", **payload})
        seq = top["case_seq"] + 1
        db.execute("INSERT INTO case_event (case_id, case_seq, kind, actor, at, payload_json, "
                   "prev_hash, event_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (last["case_id"], seq, "finding.recorded", last["actor"], last["at"], body,
                    top["event_hash"], case_store.CaseStore._event_hash(
                        last["case_id"], seq, "finding.recorded", last["actor"], last["at"],
                        body, top["event_hash"])))
        db.commit()
    finally:
        db.close()


def strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)
    else:
        yield str(value)


class AccessControlProofTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        build(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def tearDown(self):
        write_users(self.root, BASE_USERS)
        write_config(self.root)

    def test_three_requests_are_refused_typed_by_every_consumer_and_controls_are_served(self):
        code, document = run(environ(self.root))
        self.assertEqual(live_verify.check_expectations(SPEC, code, document), [], document)
        self.assertNotIn("blocked_reason", document)
        self.assertEqual((document["requests_sent"], document["evidence_label"]),
                         (0, "live-unverified"))
        for kind in proof.REQUEST_KINDS:
            entry = document["kinds"][kind]
            with self.subTest(kind):
                self.assertGreater(entry["requests"], 0)
                self.assertEqual((entry["refused"], entry["leaked"]), (entry["requests"], 0))
                self.assertEqual(sum(entry["reasons"].values()), entry["refused"])
                for reason in entry["reasons"]:
                    self.assertRegex(reason, live_verify.ENUM_STRING)
                    self.assertNotIn(reason, ("untyped", "refused_without_error"))
                # The guards are not simply refusing everyone: the rightful reader is served.
                self.assertGreater(entry["controls_served"], 0)
                self.assertEqual(entry["controls_served"], entry["controls"])
        self.assertEqual(document["kinds"]["mail_other"]["reasons"],
                         {"source_not_configured": document["kinds"]["mail_other"]["requests"]})
        self.assertEqual(document["kinds"]["teams_other"]["reasons"],
                         {"capture_citation_unresolvable":
                          document["kinds"]["teams_other"]["requests"]})
        # Counts and codes only: no object id, e-mail or handle is printed.
        for text in strings(document):
            for secret in (EXCL, UPN_C, tcm.BOX_A, tcm.UPN_B, OID_F, TID, "://",
                           SECRET_STATEMENT, *NON_PO, "PO-4471"):
                self.assertNotIn(secret.lower(), text.lower())

    def test_reader_cardinality_loses_and_duplicates_no_probe(self):
        per_kind = {}
        for n in (1, 10, 60, 70):
            users = [_user(f"cmextra{i}", str(uuid.UUID(int=(i + 1) << 64 | 0x4000_8000_0000_0000)),
                           f"x{i}@example-plx.com") for i in range(n)]
            write_users(self.root, users)
            started = time.monotonic()
            code, document = run(environ(self.root))
            elapsed = time.monotonic() - started
            with self.subTest(n=n):
                readers = min(n, proof.MAX_READERS)
                self.assertEqual((code, document["readers"]), (0, readers), document)
                self.assertEqual(document["denied_leaks"], 0)
                for kind in proof.REQUEST_KINDS:
                    entry = document["kinds"][kind]
                    # Strangers: every stored record of each kind is a request, once per reader.
                    self.assertEqual(entry["controls"], 0)
                    self.assertEqual(entry["requests"] % readers, 0)
                    per_kind.setdefault(kind, entry["requests"] // readers)
                    self.assertEqual(entry["requests"], per_kind[kind] * readers)
                    self.assertGreater(per_kind[kind], 0)
                self.assertEqual(document["probes"], readers * sum(per_kind.values()))
                # Every stranger reads the case once and is withheld A's two cited items,
                # and is served each of the three po_reference values' orders once.
                self.assertEqual(document["cases_read"], readers)
                self.assertEqual(document["case_items_unresolved"], 2 * readers)
                self.assertEqual(document["withheld_unresolved"], 2 * readers)
                self.assertEqual(document["po_reference_scanned"], 3 * readers)
                self.assertEqual(document["po_like_served"], readers)
                self.assertEqual(document["echoed_non_po"], 0)
                # Per non-PO-like value (two, each on an order and its customer PO): both
                # objects and the case for every reader; the value whole and its first
                # word ("Jane") searched by the first readers, one answer each for fewer.
                self.assertEqual(document["po_probed_by"], {
                    "object": 4 * readers, "case": 2 * readers,
                    "find": 3 * min(readers, proof.MAX_FIND_READERS),
                    "answer": 2 * min(readers, proof.MAX_ANSWER_READERS)})
                self.assertLess(elapsed, SPEC["timeout_seconds"])
        self.assertEqual(per_kind["mail_other"], 2)   # A's and B's message, once each

    def test_a_refusal_without_a_typed_reason_fails_closed(self):
        with mock.patch.object(CaseRuntime, "resolve_citation", side_effect=RuntimeError()), \
                self.assertLogs("operating.cases", "WARNING"):
            code, document = run(environ(self.root))
        self.assertEqual(code, 1)
        self.assertFalse(document["ok"])
        self.assertGreater(document["untyped"], 0)
        self.assertEqual(document["denied_leaks"], 0)
        self.assertNotEqual(live_verify.check_expectations(SPEC, code, document), [])

    def test_without_host_inputs_it_reads_blocked_with_a_cause(self):
        cases = {
            "state_dir_unset": environ(self.root, PLX_WORKBENCH_STATE_DIR=None),
            "host_config_unset": environ(self.root, PLX_WORKBENCH_HOST_CONFIG=None),
            "state_dir_absent": environ(self.root, PLX_WORKBENCH_STATE_DIR=str(self.root / "no")),
            "excluded_people_oids_absent": environ(self.root, PLX_EXCLUDED_PEOPLE_OIDS=None),
            "excluded_people_oids_invalid": environ(self.root, PLX_EXCLUDED_PEOPLE_OIDS="nope"),
        }
        for reason, env in cases.items():
            with self.subTest(reason):
                code, document = run(env)
                self.assertEqual((code, document["ok"], document["blocked_reason"]),
                                 (0, False, reason))
                self.assertIn(SPEC["blocked_causes"].get(reason, SPEC["blocked_cause"]),
                              live_verify.CAUSES)
        # A host that names no state dir or config is the host's own misconfiguration, never
        # a gap in the source (the verifier's run on main 1e2f02bd read source_gap).
        for reason in ("state_dir_unset", "host_config_unset", "state_dir_absent",
                       "employee_policy_unset", "live_audience_unset"):
            self.assertEqual(SPEC["blocked_causes"].get(reason), "host_misconfiguration", reason)
        # The verifier's run on main 1e2f02bd: a state dir but no host config in the
        # environment. The spec names the verifier's host config, read only when the
        # environment names none, so the proof reaches the host's real blocker: no Portal
        # access snapshot until the role tables are served (later_ask portal-role-tables).
        args = SPEC["command"][3:]
        code, document = run(environ(self.root, PLX_WORKBENCH_HOST_CONFIG=None), args)
        self.assertEqual((code, document["blocked_reason"], document["host_config_from"]),
                         (0, "portal_role_tables_unshared", "default"))
        self.assertEqual(SPEC["blocked_causes"]["portal_role_tables_unshared"], "later_ask")
        self.assertIn("host_config_from", SPEC["record_enums"])
        code, document = run(environ(self.root), args)   # the host's own config wins
        self.assertEqual(document["host_config_from"], "environment")
        self.assertTrue(document["ok"], document)
        self.assertEqual(live_verify.check_expectations(SPEC, code, document), [])
        write_config(self.root, portal_access=True)
        code, document = run(environ(self.root))
        self.assertEqual((code, document["blocked_reason"]), (0, "portal_role_tables_unshared"))
        (self.root / "host.json").write_text("{not json")
        code, document = run(environ(self.root))
        self.assertEqual(document["blocked_reason"], "host_config_invalid")
        self.assertEqual(SPEC["blocked_causes"]["host_config_invalid"], "host_misconfiguration")

    def test_a_reason_shaped_like_an_object_id_is_untyped(self):
        from operating.case_runtime import CitationUnresolvable
        from operating.employee_permissions import EmployeeAccessRefused
        for code in (EXCL, EXCL.upper(), f"refused:{EXCL}", f"x{EXCL}"):
            with self.subTest(code=code):
                self.assertEqual(proof._reason(CitationUnresolvable(code)), "untyped")
        self.assertEqual(proof._reason(EmployeeAccessRefused(EXCL)), "untyped")
        self.assertEqual(proof._reason(CitationUnresolvable("excluded_people_oids_absent")),
                         "excluded_people_oids_absent")

    def test_an_unresolvable_citations_values_are_withheld_from_every_other_reader(self):
        """GOV-19: A's fact and gap cite A's own mail; every other reader of the case is
        served them withheld, and the check fails the moment one is served."""
        code, document = run(environ(self.root))
        self.assertEqual(live_verify.check_expectations(SPEC, code, document), [], document)
        readers = document["readers"]
        self.assertEqual(document["cases_read"], readers)
        # Two items (the fact and the gap) for each reader but A; F2 is uncited.
        self.assertEqual(document["case_items_unresolved"], 2 * (readers - 1))
        self.assertEqual(document["withheld_unresolved"], document["case_items_unresolved"])
        self.assertEqual((document["unresolved_served"], document["case_items_unreadable"]),
                         (0, 0))
        self.assertGreater(document["case_items_resolved"], 0)   # A is served its own items
        for text in strings(document):
            for secret in (SECRET_STATEMENT, SECRET_GAP, "carrier", "dispatcher"):
                self.assertNotIn(secret.lower(), text.lower())

        # The store serving content its reader cannot resolve is caught as a leak.
        with mock.patch.object(case_store.CaseStore, "_current_digest",
                               lambda self, handle, actor: "served"):
            code, document = run(environ(self.root))
        self.assertEqual((code, document["ok"]), (1, False))
        self.assertEqual(document["unresolved_served"], 2 * (readers - 1))
        self.assertEqual(document["withheld_unresolved"], 0)
        self.assertGreaterEqual(document["denied_leaks"], document["unresolved_served"])
        self.assertNotEqual(live_verify.check_expectations(SPEC, code, document), [])

    def test_a_stored_item_of_unknown_shape_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = copy_world(self.root, Path(tmp) / "w")
            append_finding(world / "state" / "case-runtime" / "cases.sqlite",
                           citations=[{"handle": 5, "digest": "d"}])
            code, document = run(environ(world))
        self.assertEqual((code, document["ok"]), (1, False))
        self.assertEqual(document["case_items_unreadable"], document["readers"])
        self.assertNotEqual(live_verify.check_expectations(SPEC, code, document), [])

    def test_a_po_like_po_reference_is_served_and_passes(self):
        """REC-07: the PO number is served (the guard is not withholding everything), the
        name and the placeholder are stated by no served object, find result or answer."""
        code, document = run(environ(self.root))
        self.assertEqual(live_verify.check_expectations(SPEC, code, document), [], document)
        readers = document["readers"]
        self.assertEqual((document["po_reference_values"], document["po_reference_non_po_like"]),
                         (3, len(NON_PO)))
        self.assertEqual(document["po_reference_scanned"], 3 * readers)
        self.assertEqual(document["po_like_served"], readers)
        self.assertEqual(document["echoed_non_po"], 0)
        self.assertEqual(document["echoed_by"], {"object": 0, "find": 0, "answer": 0, "case": 0})
        for where, probed in document["po_probed_by"].items():
            with self.subTest(where):
                self.assertGreater(probed, 0)   # every consumer was asked and answered
        for text in strings(document):
            for value in (*NON_PO, "PO-4471"):
                self.assertNotIn(value.lower(), text.lower())

    def test_a_non_po_like_po_reference_echoed_by_any_consumer_fails_the_proof(self):
        # Every ontology consumer stating the raw text (the guard removed).
        with mock.patch.object(ot, "withheld_po_text", lambda field_path, text: False):
            code, document = run(environ(self.root))
        self.assertEqual((code, document["ok"]), (1, False))
        for where in ("object", "find", "answer"):
            with self.subTest(where):
                self.assertGreater(document["echoed_by"][where], 0)
        self.assertEqual(document["echoed_non_po"], sum(document["echoed_by"].values()))
        self.assertNotEqual(live_verify.check_expectations(SPEC, code, document), [])
        for text in strings(document):
            self.assertNotIn("jane", text.lower())

    def test_a_served_case_stating_a_po_reference_fails_only_when_it_is_not_po_like(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "like"
            build(root, statements=("Customer PO given as PO-4471",))
            code, document = run(environ(root))
            self.assertEqual(live_verify.check_expectations(SPEC, code, document), [],
                             document)
            self.assertEqual(document["echoed_by"]["case"], 0)
            root = Path(tmp) / "name"
            build(root, statements=("Customer PO given as Jane Smith",))
            code, document = run(environ(root))
        self.assertEqual((code, document["ok"]), (1, False))
        # One served case states it, once per reader (each lists the case).
        self.assertEqual(document["echoed_by"]["case"], document["readers"])
        self.assertEqual(document["echoed_non_po"], document["readers"])
        self.assertNotEqual(live_verify.check_expectations(SPEC, code, document), [])

    def test_without_a_case_store_or_po_text_it_reads_blocked_with_a_cause(self):
        def drop_cases(world):
            (world / "state" / "case-runtime" / "cases.sqlite").unlink()

        def drop_projection(world):
            host_module.projection_path(world / "state").unlink()

        def drop_po_text(world):
            db = sqlite3.connect(host_module.projection_path(world / "state"))
            with db:
                db.execute("DELETE FROM object_values WHERE field_path LIKE '%po_reference'")
            db.close()

        for reason, prepare in (("case_store_absent", drop_cases),
                                ("projection_absent", drop_projection),
                                ("po_reference_values_absent", drop_po_text)):
            with self.subTest(reason), tempfile.TemporaryDirectory() as tmp:
                world = copy_world(self.root, Path(tmp) / "w")
                prepare(world)
                code, document = run(environ(world))
                self.assertEqual((code, document["ok"], document["blocked_reason"]),
                                 (0, True, reason))
                self.assertEqual(document["denied_leaks"], 0)
                self.assertEqual(SPEC["blocked_causes"].get(reason, SPEC["blocked_cause"]),
                                 "source_gap")

    def test_spec_is_valid(self):
        live_verify.validate_spec(SPEC)
        self.assertIn("scripts.access_control_proof", live_verify.ALLOWED_MODULES)


class ExcludedAfterCaptureTest(unittest.TestCase):
    """C's mail was captured before C was excluded: the check must fail, not pass quietly,
    and no other reader may be served it."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        build(cls.root, capture_excluded=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def tearDown(self):
        write_users(self.root, BASE_USERS)

    def test_the_clean_world_holds_nothing_of_an_excluded_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp))
            _code, document = run(environ(Path(tmp)))
        self.assertEqual(document["excluded_held"], 0)

    def test_an_excluded_persons_stored_record_fails_the_check(self):
        code, document = run(environ(self.root))
        self.assertGreater(document["excluded_held"], 0)
        self.assertFalse(document["ok"])
        self.assertEqual(code, 1)
        self.assertNotEqual(live_verify.check_expectations(SPEC, code, document), [])

    def test_no_other_reader_is_served_an_excluded_persons_record(self):
        write_users(self.root, [u for u in BASE_USERS if u["entraObjectId"] != EXCL])
        _code, document = run(environ(self.root))
        entry = document["kinds"]["excluded_person"]
        self.assertGreater(entry["requests"], 0)
        self.assertEqual((entry["leaked"], entry["refused"]), (0, entry["requests"]))

    def test_the_excluded_person_is_not_served_their_own_record_after_exclusion(self):
        # The capture holds C's mailbox from before C was excluded; serving it re-checks
        # the exclusion, so C is not served their own stored mail either.
        write_users(self.root, [u for u in BASE_USERS if u["entraObjectId"] == EXCL])
        _code, document = run(environ(self.root))
        entry = document["kinds"]["excluded_person"]
        self.assertGreater(entry["requests"], 0)
        self.assertEqual((entry["leaked"], entry["refused"]), (0, entry["requests"]))

    def served(self, env, handles, prepare=None):
        """{handle: {principal oid: served by any consumer}} for the stored records named,
        read in the proof's own runtime (``prove`` reads them the same way; ``prepare``
        adjusts that runtime first)."""
        seen = {}
        real = proof.prove

        def probe(runtime, tenant, readers, found):
            from ontology.types import Actor
            if prepare is not None:
                prepare(runtime)
            others = proof.consumers(runtime)
            for handle in handles:
                for reader in readers:
                    actor = Actor(tenant, frozenset({"employee:" + reader["oid"]}))
                    try:
                        hit = runtime.resolve_citation(handle, actor) is not None
                    except Exception:  # noqa: BLE001 - refused
                        hit = False
                    for _name, consumer in others:
                        try:
                            hit = consumer(handle, actor) or hit
                        except Exception:  # noqa: BLE001 - refused
                            pass
                    seen.setdefault(handle, {})[reader["oid"]] = hit
            return real(runtime, tenant, readers, found)

        with mock.patch.object(proof, "prove", side_effect=probe):
            _code, document = run(env)
        return seen, document

    def stored(self, sql):
        import sqlite3
        db = sqlite3.connect(self.root / "state" / sql[0])
        try:
            return [r[0] for r in db.execute(sql[1])]
        finally:
            db.close()

    def test_mail_from_a_person_excluded_after_capture_is_refused_at_serve_time(self):
        # B wrote to A (MSG-A2) and was excluded after the capture: A's mailbox is A's,
        # but the message names B, so nobody is served it; A's other mail still is.
        [b_to_a] = self.stored(("mail-capture/capture.db",
                                "SELECT l1_handle FROM messages WHERE message_id = 'MSG-A2'"))
        [a_own] = self.stored(("mail-capture/capture.db",
                               "SELECT l1_handle FROM messages WHERE message_id = 'MSG-A1'"))
        env = environ(self.root, PLX_EXCLUDED_PEOPLE_OIDS=",".join([EXCL, tcm.BOX_B]))
        seen, document = self.served(env, [b_to_a, a_own])
        self.assertEqual(seen[b_to_a][tcm.BOX_A.lower()], False)
        self.assertFalse(any(seen[b_to_a].values()), seen)
        self.assertTrue(seen[a_own][tcm.BOX_A.lower()])     # control: A's own mail
        found, held, _unresolved = proof.targets(self.root / "state",
                                                 PeopleExclusion.from_env(env))
        self.assertIn(b_to_a, {t["handle"] for t in found["excluded_person"]})
        self.assertNotIn(b_to_a, {t["handle"] for t in found["mail_other"]})
        entry = document["kinds"]["excluded_person"]
        self.assertEqual((entry["leaked"], entry["refused"]), (0, entry["requests"]))
        self.assertGreaterEqual(held, 3)                    # C's, B's own and B's to A
        other = document["kinds"]["mail_other"]
        self.assertEqual(other["controls_served"], other["controls"])
        self.assertGreater(other["controls"], 0)

    def test_one_kept_mail_store_serves_every_reader_as_a_store_per_read_did(self):
        # Every captured message (the excluded person's mail to A, mail of mailboxes a
        # reader may not open, each reader's own) for every reader and consumer: the
        # runtime's kept read-only store decides each one as a store opened per read did,
        # and the per-read writer store (schema and journal statements) is not opened.
        from operating.case_runtime import CaseRuntime
        handles = self.stored(("mail-capture/capture.db", "SELECT l1_handle FROM messages"))
        env = environ(self.root, PLX_EXCLUDED_PEOPLE_OIDS=",".join([EXCL, tcm.BOX_B]))
        writer = MailCaptureStore.__init__
        counts = {"writer": 0}

        def counted(store, *args, **kwargs):
            counts["writer"] += 1
            writer(store, *args, **kwargs)

        with mock.patch.object(MailCaptureStore, "__init__", counted):
            kept, _ = self.served(env, handles)
            kept_writers, counts["writer"] = counts["writer"], 0
            with mock.patch.object(CaseRuntime, "mail_reader", None):
                fresh, _ = self.served(env, handles)
        self.assertEqual(kept, fresh)
        decisions = [hit for by in kept.values() for hit in by.values()]
        self.assertTrue(any(decisions) and not all(decisions), kept)
        self.assertGreater(counts["writer"], kept_writers)

    def test_teams_content_of_a_person_excluded_after_capture_is_refused_at_serve_time(self):
        handles = proof.targets(self.root / "state",
                                PeopleExclusion.from_env(environ(self.root)))[0]["teams_other"]
        handles = [t["handle"] for t in handles]
        chat = [h for h in handles if h.startswith("teams-chat://")]
        channel = [h for h in handles if h.startswith("teams-channel://")]
        self.assertTrue(chat and channel, handles)
        # Before anyone in Teams is excluded, the members are served (the controls).
        seen, _document = self.served(environ(self.root), handles)
        self.assertTrue(all(any(by.values()) for by in seen.values()), seen)
        # The private channel's own record names only its member Vince, never Cos.
        private = [h for h in channel if "/conversation@" in h and "private" in h]
        self.assertEqual(len(private), 1, channel)
        cases = {
            # Cos sent every message and is a participant of every other conversation:
            # none of them is served to anyone.
            "sender": (tct.U_COS, [h for h in handles if h not in private], private),
            # The guest is only a member of the chat: the chat is refused, channels served.
            "chat_member": (tct.U_OTHER, chat, channel),
        }
        for name, (person, refused, still) in cases.items():
            with self.subTest(name):
                env = environ(self.root, PLX_EXCLUDED_PEOPLE_OIDS=",".join([EXCL, person]))
                seen, document = self.served(env, handles)
                for handle in refused:
                    self.assertFalse(any(seen[handle].values()), (handle, seen[handle]))
                for handle in still:
                    self.assertTrue(any(seen[handle].values()), (handle, seen[handle]))
                found, _held, _unresolved = proof.targets(self.root / "state",
                                                          PeopleExclusion.from_env(env))
                self.assertEqual({t["handle"] for t in found["excluded_person"]
                                  if "teams-" in t["handle"]}, set(refused))
                entry = document["kinds"]["excluded_person"]
                self.assertEqual((entry["leaked"], entry["refused"]), (0, entry["requests"]))
                self.assertEqual(document["kinds"]["teams_other"]["leaked"], 0)

    def test_without_an_exclusion_list_no_captured_mail_or_teams_is_served(self):
        # Fail closed: a runtime whose guard cannot decide serves no captured mail or Teams
        # message to anyone, the rightful readers included.
        real = proof.prove

        def blind(runtime, *args):
            runtime.people_exclusion = PeopleExclusion(unavailable="excluded_people_oids_absent")
            return real(runtime, *args)

        with mock.patch.object(proof, "prove", side_effect=blind):
            _code, document = run(environ(self.root))
        for kind in ("mail_other", "teams_other", "excluded_person"):
            entry = document["kinds"][kind]
            with self.subTest(kind):
                self.assertEqual((entry["leaked"], entry["controls_served"]), (0, 0), entry)
        # The refusal names its cause (no exclusion list), not a generic code.
        for kind in ("mail_other", "teams_other", "sender_unresolved"):
            entry = document["kinds"][kind]
            with self.subTest(kind):
                self.assertEqual(entry["reasons"],
                                 {"excluded_people_oids_absent": entry["requests"]})
        self.assertGreater(document["kinds"]["mail_other"]["controls"], 0)
        self.assertGreater(document["kinds"]["teams_other"]["controls"], 0)

    def test_mail_from_an_uncaptured_person_excluded_after_capture_is_refused(self):
        # D (whose own mailbox the capture never held) wrote to A and was excluded after
        # the capture. Offline nothing records whose D's address is, so a tenant-domain
        # sender the serve time cannot resolve is withheld from everyone (fail closed),
        # and the proof counts it as a request every reader must be refused.
        [d_to_a] = self.stored(("mail-capture/capture.db",
                                "SELECT l1_handle FROM messages WHERE message_id = 'MSG-A3'"))
        [a_own] = self.stored(("mail-capture/capture.db",
                               "SELECT l1_handle FROM messages WHERE message_id = 'MSG-A1'"))
        for excluded in ([EXCL, OID_D], [EXCL]):
            env = environ(self.root, PLX_EXCLUDED_PEOPLE_OIDS=",".join(excluded))
            with self.subTest(excluded=len(excluded)):
                seen, document = self.served(env, [d_to_a, a_own])
                self.assertFalse(any(seen[d_to_a].values()), seen)
                # Control: A's mail from an outside sender is still served to A.
                self.assertTrue(seen[a_own][tcm.BOX_A.lower()])
                found, _held, _unresolved = proof.targets(self.root / "state",
                                                          PeopleExclusion.from_env(env))
                self.assertIn(d_to_a, {t["handle"] for t in found["sender_unresolved"]})
                self.assertNotIn(d_to_a, {t["handle"] for t in found["mail_other"]})
                entry = document["kinds"]["sender_unresolved"]
                self.assertGreater(entry["requests"], 0)
                self.assertEqual((entry["leaked"], entry["refused"], entry["controls"]),
                                 (0, entry["requests"], 0))
                self.assertEqual(document["senders_unresolved"], 1)

    def test_a_sender_that_is_not_one_address_is_withheld_from_everyone(self):
        # Unexpected sender values fail closed: neither an object id nor one address.
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root)
            db = sqlite3.connect(root / "state" / "mail-capture" / "capture.db")
            with db:
                [(handle, raw)] = db.execute("SELECT l1_handle, record FROM messages "
                                             "WHERE message_id = 'MSG-A1'").fetchall()
            db.close()
            for sender in ("Accounts Payable", "a@b@example-plx.com", "x@"):
                record = json.loads(raw)
                record["from"] = sender
                db = sqlite3.connect(root / "state" / "mail-capture" / "capture.db")
                with db:
                    db.execute("UPDATE messages SET record = ? WHERE l1_handle = ?",
                               (json.dumps(record), handle))
                db.close()
                case = ExcludedAfterCaptureTest()
                case.root = root
                with self.subTest(sender=sender):
                    seen, document = case.served(environ(root), [handle])
                    self.assertFalse(any(seen[handle].values()), seen)
                    found, _held, unresolved = proof.targets(
                        root / "state", PeopleExclusion.from_env(environ(root)))
                    self.assertIn(handle, {t["handle"] for t in found["sender_unresolved"]})
                    self.assertEqual(unresolved, 1)
                    self.assertEqual(document["kinds"]["sender_unresolved"]["leaked"], 0)

    def test_a_bound_guard_decides_an_uncaptured_sender_at_serve_time(self):
        # With the host's GET-only Graph client the runtime binds a copy of its guard once
        # (one directory listing) and decides D's address exactly.
        [d_to_a] = self.stored(("mail-capture/capture.db",
                                "SELECT l1_handle FROM messages WHERE message_id = 'MSG-A3'"))
        users = [{"id": OID_D, "userPrincipalName": UPN_D, "mail": UPN_D,
                  "proxyAddresses": [f"SMTP:{UPN_D}"]},
                 {"id": EXCL, "userPrincipalName": UPN_C, "mail": UPN_C}]

        class Directory:
            calls = 0

            def __init__(self, fail=False):
                self.fail = fail

            def get_pages(self, url, **caps):
                Directory.calls += 1
                if self.fail:
                    raise OSError("listing failed")
                return users, {"complete": True}

        cases = {"D excluded": ([EXCL, OID_D], Directory(), False),
                 "D not excluded": ([EXCL], Directory(), True),
                 "listing failed": ([EXCL], Directory(fail=True), False)}
        for name, (excluded, api, served_to_a) in cases.items():
            env = environ(self.root, PLX_EXCLUDED_PEOPLE_OIDS=",".join(excluded))

            def bind(runtime, api=api):
                runtime.live.graph_api = api

            Directory.calls = 0
            with self.subTest(name):
                seen, _document = self.served(env, [d_to_a], prepare=bind)
                self.assertEqual(seen[d_to_a][tcm.BOX_A.lower()], served_to_a, seen)
                others = {k: v for k, v in seen[d_to_a].items() if k != tcm.BOX_A.lower()}
                self.assertFalse(any(others.values()), others)
                # One listing per runtime, however many reads (7 readers x consumers).
                self.assertEqual(Directory.calls, 1)


if __name__ == "__main__":
    unittest.main()
