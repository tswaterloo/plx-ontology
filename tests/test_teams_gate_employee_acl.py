"""Teams channel and chat rows for an employee are decided by the employee's own item ACL.

``CaseRuntime`` reads a ``teams-channel://`` or ``teams-chat://`` handle from the host's Teams
capture: the live audience grants the conversation (the unit grant ``policy_now`` makes) and,
for an employee, the message's and the conversation's captured membership is matched against
the employee's own live
identities (``EmployeeEntitlement.item_acl_allows``, the check mail and SharePoint use), never
against the audience file's static ``identities`` map summed over role principals. The host's
item gate and the claim gate read through that one resolver, so each refusal below withholds
the row from both, and from the host's tools.

Evidence: injected-transport (the fake Graph of ``integrations.tests.test_capture_teams``
behind the real GET-only client), isolated-fixture Portal and Entra inputs; live-unverified.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from integrations import graph_read
from integrations.capture_store import CaptureError
from integrations.capture_teams import (DB_FILE, STORE_DIR, Conversation, TeamsCapture,
                                        open_store)
from integrations.live_source_guard import LiveSourceAudience
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AudienceMap
from integrations.tests import test_capture_teams as tct
from ontology import schema as schema_loader
from ontology.project import build_store
from ontology.project_graph import TeamsCaptures
from ontology.types import Actor
from operating import ontology_tools_investigate as oi
from operating.case_runtime import CaseRuntime, LiveReaders, _EmployeeItemAcl
from operating.case_tools_host import CaptureCaseHostTools
from operating.cases import CitationUnresolvable
from operating.employee_permissions import EmployeePermissionPolicy, EmployeePermissionResolver
from operating.ontology_tools import item_gate_from_case_runtime
from operating.tests.test_employee_permissions import OID, POLICY, Calls, member, portal_user

EMPLOYEE = Actor("plx", frozenset({"employee:" + OID}))
ROLE = "staff:operations"   # a role principal the fixture employee holds (POLICY, MANAGER)
PROBE = Actor("plx", frozenset({"staff:probe"}), "restricted")
GROUP = f"group:{tct.TEAM}"
TEAM_MEMBERS = rf"^/v1\.0/teams/{tct.TEAM}/members$"
FIND = {"object_type": "TeamsChannelMessage", "limit": 50,
        "filters": [{"property": "id", "op": "present"}]}
UNITS = {thread: Conversation("channel", thread, tct.TEAM).unit
         for thread in (tct.GENERAL, tct.PRIVATE)}
CHAT_UNIT = Conversation("chat", tct.CHAT).unit


def live_audience(identities, readers=(ROLE,)) -> LiveSourceAudience:
    """Teams granted to the capture principal, the probe and ``readers`` (None: no
    ``teams:`` key at all, mail only)."""
    grants = ({"mail:*": [ROLE]} if readers is None
              else {"teams:*": ["svc:teams-capture", "staff:probe", *readers]})
    return LiveSourceAudience(grants, tenant="plx",
                              classifications={"teams:*": "internal", "mail:*": "internal"},
                              identities=identities)


class TeamsEmployeeAclTests(unittest.TestCase):
    def capture(self, *, fallback: bool = False, roots: int | None = None
                ) -> dict[str, list[str]]:
        """A Teams capture in a fresh state directory (``self.state``): the fixture's
        team (a standard and a private channel) and a chat of cos's, or ``roots`` root
        messages in the standard channel. ``fallback``: the team members read answers 403,
        so the standard channel is captured under ``group:<team>``. Returns the current
        handles per channel and of the chat."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        graph = tct.FakeTeamsGraph()
        if roots is None:
            graph.post_root(tct.GENERAL, "1001", replies=2)
            graph.post_root(tct.GENERAL, "1002")
            graph.post_root(tct.PRIVATE, "2001", "Board-only pricing")
            graph.post_chat("3001")
        for n in range(roots or 0):
            graph.post_root(tct.GENERAL, str(5000 + n))
        if fallback:
            graph.forbidden.append(TEAM_MEMBERS)
        audience = live_audience({"staff:probe": [tct.COS, tct.VINCE, GROUP]},
                                 readers=("staff:probe",))
        store = open_store(self.state, audience)
        try:
            api, _transport = graph_read.build_graph(tct.ENV, sender=graph)
            TeamsCapture(store, api, audience, tct.CAPTURE).run(tct.scope())
            handles, self.acls = {}, {}
            for thread, unit in UNITS.items():
                try:
                    items = store.records(PROBE, "teams_channel", unit, limit=200)["items"]
                except CaptureError:   # a channel the capture is blocked on
                    items = []
                # The messages (the channel's own record is gated alike: not counted here).
                items = [i for i in items if "/conversation@" not in i["citation"]]
                handles[thread] = [item["citation"] for item in items]
                self.acls[thread] = {item["meta"]["source_acl"].get("basis") for item in items}
            handles[tct.CHAT] = [item["citation"] for item in store.records(
                PROBE, "teams_chat", CHAT_UNIT, limit=200)["items"]
                if "/conversation@" not in item["citation"]]
        finally:
            store.close()
        return handles

    def runtime(self, identities, *, readers=(ROLE,), email: str | None = None,
                groups=()) -> CaseRuntime:
        """A case runtime over ``self.state`` whose employee (``EMPLOYEE``: Portal
        MANAGER, holding ``ROLE``) has the Portal ``email`` and the live ``groups``;
        ``self.portal`` and ``self.groups`` are its counted Portal and group inputs."""
        user = portal_user("MANAGER", **({"email": email} if email else {}))
        self.portal, self.groups = Calls(user), Calls(member(*groups))
        resolver = EmployeePermissionResolver(EmployeePermissionPolicy.from_json(POLICY),
                                              self.portal, self.groups)
        runtime = CaseRuntime(self.state, client=None, audience=AudienceMap({}, tenant="plx"),
                              tenant="plx", live=LiveReaders(live_audience(identities, readers)),
                              employee_permissions=resolver,
                              people_exclusion=PeopleExclusion.from_env(tct.ENV))
        self.addCleanup(runtime.close)
        return runtime

    def readable(self, runtime: CaseRuntime, actor: Actor, handles: list[str]) -> list[str]:
        """The handles ``actor`` may read: the host's item gate, with the claim gate's row
        reader agreeing on every one."""
        gate, rows = item_gate_from_case_runtime(runtime), oi.citation_rows(runtime)
        out = []
        for handle in handles:
            allowed = gate(actor, handle)
            self.assertEqual(allowed, rows(handle, actor) is not None, handle)
            if allowed:
                out.append(handle)
        return out

    def test_a_role_principal_mapped_to_a_member_address_grants_no_employee(self):
        handles = self.capture()
        general, chat = handles[tct.GENERAL], handles[tct.CHAT]
        every = general + handles[tct.PRIVATE] + chat
        self.assertEqual((len(general), len(handles[tct.PRIVATE]), len(chat)), (4, 1, 1))
        # The audience file maps the employee's role to both members' addresses.
        mapped = self.runtime({ROLE: [tct.COS, tct.VINCE]})
        self.assertEqual(self.readable(mapped, EMPLOYEE, every), [])
        # The audience the store reads for this employee answers the identity calls with
        # the employee's own: the map grants nothing.
        gated, base = mapped.employee_gate(EMPLOYEE), mapped.live.audience
        acl = _EmployeeItemAcl(base, mapped.employee_gate.entitlement(gated), "teams-channel")
        resource, member_acl = f"teams:channel:{tct.TEAM}:{tct.GENERAL}", {
            "status": "read", "identities": [tct.COS]}
        self.assertTrue(base.visible_all(gated, [resource], member_acl))
        self.assertFalse(acl.visible_all(gated, [resource], member_acl))
        self.assertNotIn(tct.COS, acl.identities_for(gated))
        # A member employee reads its channel and its chat with no static entry at all, not
        # the private channel it is not in; vince's employee reads that channel, not the chat.
        self.assertEqual(self.readable(self.runtime({}, email=tct.COS), EMPLOYEE, every),
                         general + chat)
        self.assertEqual(self.readable(self.runtime({}, email=tct.VINCE), EMPLOYEE, every),
                         general + handles[tct.PRIVATE])

    def test_a_group_fallback_channel_follows_the_employees_live_groups(self):
        general = self.capture(fallback=True)[tct.GENERAL]
        self.assertEqual((len(general), self.acls[tct.GENERAL]), (4, {"group_fallback"}))
        self.assertEqual(self.readable(self.runtime({}, groups=(tct.TEAM,)), EMPLOYEE,
                                       general), general)
        # A former member: the static entries remain, the live membership is gone.
        former = self.runtime({"employee:" + OID: [GROUP],
                               ROLE: [GROUP]}, groups=(tct.TEAM,))
        self.assertEqual(self.readable(former, EMPLOYEE, general), general)
        self.groups.value = member()
        self.assertEqual(self.readable(former, EMPLOYEE, general), [])

    def test_every_deny_case_withholds_the_row(self):
        general = self.capture()[tct.GENERAL]
        host_actor = Actor("plx", frozenset({"staff:host"}), "restricted")
        denied = {
            # A non-employee reader the audience grants but maps to no identity.
            "no_identity_mapping": (self.runtime({}, readers=("staff:host",)), host_actor),
            "non_member": (self.runtime({}), EMPLOYEE),
            "no_teams_audience_key": (self.runtime({}, readers=None, email=tct.COS), EMPLOYEE),
        }
        for case, (runtime, actor) in denied.items():
            self.assertEqual(self.readable(runtime, actor, general), [], case)
        member_runtime = self.runtime({}, email=tct.COS)
        self.assertEqual(self.readable(member_runtime, EMPLOYEE, general), general)
        store = self.state / STORE_DIR / DB_FILE
        for suffix in ("-wal", "-shm"):
            Path(f"{store}{suffix}").unlink(missing_ok=True)
        store.write_bytes(b"not a capture store\n" * 64)
        self.assertEqual(self.readable(member_runtime, EMPLOYEE, general), [])
        with self.assertRaises(CitationUnresolvable):
            member_runtime.resolve_citation(general[0], EMPLOYEE)

    def test_the_host_grants_the_channel_unit_and_serves_its_rows_to_members_only(self):
        self.capture()
        path = self.state / "projection.sqlite"
        with TeamsCaptures(self.state / STORE_DIR / DB_FILE) as captured:
            build_store(path, schema_loader.load_schema(), {"teams": captured},
                        clock=lambda: dt.datetime.now(dt.timezone.utc))
        readers = {"member": self.runtime({}, email=tct.COS),
                   "role_mapped": self.runtime({ROLE: [tct.COS, tct.VINCE]}),
                   "no_teams_key": self.runtime({}, readers=None, email=tct.COS)}
        found, member_objects = {}, []
        for name, runtime in readers.items():
            gated = runtime.employee_gate(EMPLOYEE)
            host = CaptureCaseHostTools(runtime, gated, store_path=path)
            self.addCleanup(host.close)
            # The unit grant is the audience's alone (membership is the item gate's).
            grants = {unit: host.policy_now(gated).audiences.get(unit)
                      for unit in UNITS.values()}
            held = frozenset() if name == "no_teams_key" else gated.principals
            self.assertEqual(grants, {unit: held for unit in UNITS.values()}, name)
            out = host.call("plx.find", FIND, gated)
            found[name] = sorted(c["object"]["object_id"] for c in out["candidates"])
            if name == "member":
                member_objects = found[name]
                continue
            hidden = [host.call(tool, {"from" if tool != "plx.object" else "object_id": o},
                                gated)
                      for o in member_objects for tool in ("plx.object", "plx.timeline")]
            self.assertTrue(all("error" in r for r in hidden), (name, hidden))
            text = json.dumps([out, hidden])
            self.assertNotIn(tct.SECRET_TEXT, text, name)
            self.assertNotIn("teams-channel://", text, name)
        self.assertEqual({name: len(objects) for name, objects in found.items()},
                         {"member": 4, "role_mapped": 0, "no_teams_key": 0})

    def test_each_row_resolves_the_entitlement_a_fixed_number_of_times_at_1_10_60_rows(self):
        # Per row, ``readable`` makes two decisions (item gate, claim row) and each resolves
        # the employee once: the Teams read in ``CaseRuntime._capture_resolve`` reuses the
        # entitlement ``EmployeeActorGate`` resolved for the call. Counted at the resolver's
        # Portal and group inputs, so a per-row re-resolve or a lost row shows, never a
        # wall-clock budget.
        per_row = 2
        for n in (1, 10, 60):
            general = self.capture(roots=n)[tct.GENERAL]
            self.assertEqual(len(general), n)
            for identities, email, expected in (({}, tct.COS, general),
                                                ({ROLE: [tct.COS]}, None, [])):
                runtime = self.runtime(identities, email=email)
                self.assertEqual(self.readable(runtime, EMPLOYEE, general), expected, n)
                self.assertEqual((self.portal.count, self.groups.count),
                                 (per_row * n, per_row * n), n)


if __name__ == "__main__":
    unittest.main()
