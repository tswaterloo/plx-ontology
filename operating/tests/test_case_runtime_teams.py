"""Teams channel and chat citations through the one case-runtime resolver (evidence:
injected-transport Graph of ``integrations.tests.test_capture_teams``, isolated fixture;
live-unverified).

``CaseRuntime.resolve_citation`` reads a ``teams-channel://`` or ``teams-chat://`` handle from
the Teams capture in the runtime's state directory, so the ontology host's item gate and the
claim gate decide a Teams row by the same read: a channel or chat member gets the rows, an
outsider (granted, not a member) and a withheld actor (not granted) get none, a chat whose
membership was not captured is neither projected nor resolved, and a handle the capture cannot
resolve denies. A chat member's current visible-history start decides every captured version
of the chat, ones saved before starts were recorded and missing ones included. Every read goes through one read-only connection to the store (opened again
only when the file is replaced), and a capture that removes a member withdraws its rows on the
next read.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from integrations.capture_store import CapturedRecord, CaptureStore, make_handle
from integrations.capture_teams import (CHAT_KIND, STORE_DIR, Conversation, TeamsCapture,
                                        open_store)
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AudienceMap
from integrations.tests import test_capture_teams as tct
from ontology import schema as schema_loader
from ontology.project import build_store
from ontology.project_graph import TeamsCaptures
from ontology.types import Actor
from operating import claim_checks
from operating import ontology_tools_investigate as oi
from operating.case_runtime import CaseRuntime, LiveReaders, _EmployeeItemAcl
from operating.employee_permissions import EmployeeEntitlement, EmployeePrincipal
from operating.ontology_host import CaseHostTools

WITHHELD = Actor("plx", frozenset({"staff:teams-withheld"}), "restricted")   # no grant
FIND = {"object_type": "TeamsChannelMessage", "limit": 50,
        "filters": [{"property": "id", "op": "present"}]}
CHAT_FIND = {**FIND, "object_type": "TeamsChatMessage"}


def runtime(state_dir, live) -> CaseRuntime:
    return CaseRuntime(state_dir, client=None, audience=AudienceMap({}, tenant="plx"),
                       tenant="plx", live=live,
                       people_exclusion=PeopleExclusion.from_env(tct.ENV))


class TeamsCitationTests(tct.CaptureCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        self.runtime = runtime(self.tmp.name, LiveReaders(self.audience))
        self.addCleanup(self.runtime.close)
        rows = {r["key"]["id"]: r for r in self.visible(tct.MEMBER, tct.GENERAL)}
        self.root = rows["1001"]
        self.private = self.visible(tct.MEMBER, tct.PRIVATE)[0]["citation"]

    def project(self) -> tuple[Path, dict]:
        """The projection of the capture (built once) and its build report."""
        path = Path(self.tmp.name) / "projection.sqlite"
        if not path.exists():
            with TeamsCaptures(self.store.path) as captured:
                self.report = build_store(path, schema_loader.load_schema(),
                                          {"teams": captured},
                                          clock=lambda: dt.datetime.now(dt.timezone.utc))
        return path, self.report

    def host(self, actor: Actor) -> CaseHostTools:
        host = CaseHostTools(self.runtime, actor, store_path=self.project()[0])
        self.addCleanup(host.close)
        return host

    def claim(self, actor: Actor, value: str, handle: str | None = None) -> dict:
        handle = handle or self.root["citation"]
        claims = claim_checks.parse_value_claims(
            [{"value": value, "citation": handle, "field": "row.body_text"}], [handle], [])
        # The text states the value its claim backs (claim_value_unstated otherwise).
        item = {"id": "F1", "texts": [f"The channel holds the message: {value}"],
                "citations": [{"handle": handle, "status": "resolved"}],
                "value_claims": claims}
        return claim_checks.check_items([item], rows=oi.gate_rows(
            oi.citation_rows(self.runtime), actor))

    def gate(self, actor: Actor, handle: str) -> bool:
        """The host's item gate and the claim gate's row reader, agreeing on ``handle``."""
        readable = oi.citation_rows(self.runtime)(handle, actor) is not None
        try:
            self.runtime.resolve_citation(handle, actor)
        except Exception:  # noqa: BLE001 - any refusal
            self.assertFalse(readable, (actor, handle))
            return False
        self.assertTrue(readable, (actor, handle))
        return True

    def test_the_employee_item_acl_serves_only_its_allowlist(self):
        # The store's audience for one employee: an outsider role the file maps to its own
        # address, an employee whose own identity is cos.
        reader = Actor("plx", frozenset({"staff:teams-outsider"}), "restricted")
        entitlement = EmployeeEntitlement(
            EmployeePrincipal(tct.ENV["MICROSOFT_GRAPH_TENANT_ID"], tct.U_COS), reader, None,
            source_identities=frozenset({tct.COS}))
        base = self.audience
        acl = _EmployeeItemAcl(base, entitlement, "teams-channel")
        served = {"tenant", "allows", "item_visible", "identities_for", "visible_all"}
        public = {n for n in dir(base) if not n.startswith("_")}
        self.assertTrue({"covers_whole", "principals_for", "allows_all"} <= public - served)
        for name in sorted(public - served):
            with self.assertRaises(AttributeError, msg=name):
                getattr(acl, name)
        self.assertEqual(acl.tenant, base.tenant)
        # visible_all is item_visible for every key, decided by the employee's identities.
        channel = f"teams:channel:{tct.TEAM}:{tct.GENERAL}"
        cos, outsider = ({"status": "read", "identities": [i]}
                         for i in (tct.COS, "outsider@petrasoap.com"))
        self.assertTrue(acl.item_visible(reader, channel, cos))
        self.assertTrue(acl.visible_all(reader, [channel], cos))
        self.assertTrue(base.visible_all(reader, [channel], outsider))
        self.assertFalse(acl.item_visible(reader, channel, outsider))
        self.assertFalse(acl.visible_all(reader, [channel], outsider))
        # A key no grant matches: the audience's identity-set check ignores it, this denies.
        self.assertTrue(base.allows_all(reader, [channel, "mail:nobody@example.com"]))
        self.assertFalse(acl.item_visible(reader, "mail:nobody@example.com", cos))
        self.assertFalse(acl.visible_all(reader, [channel, "mail:nobody@example.com"], cos))
        for keys in ([], channel, [channel, None]):
            self.assertFalse(acl.visible_all(reader, keys, cos), keys)

    def test_host_find_serves_a_channel_only_to_its_members(self):
        opened = mock.patch.object(CaptureStore, "open_read_only",
                                   side_effect=CaptureStore.open_read_only)
        opens = opened.start()
        self.addCleanup(opened.stop)
        found = {}
        for actor in (tct.MEMBER, tct.COS_ONLY, tct.OUTSIDER, WITHHELD):
            host = self.host(actor)
            out = host.call("plx.find", FIND, actor)
            found[actor] = {c["object"]["object_id"]: c for c in out["candidates"]}
            hidden = [host.call(name, {"from" if name != "plx.object" else "object_id": o},
                                actor) for o in found[tct.MEMBER] if o not in found[actor]
                      for name in ("plx.object", "plx.timeline")]
            self.assertTrue(all("error" in r for r in hidden), (actor, hidden))
            text = json.dumps([out, hidden])
            if not found[actor]:
                self.assertNotIn(tct.SECRET_TEXT, text, actor)
                self.assertNotIn("teams-channel://", text, actor)
            if actor != tct.MEMBER:
                self.assertNotIn("Board-only", text, actor)
        counts = {a: len(f) for a, f in found.items()}
        # General: 2 roots + 2 replies (team members); private: 1 root (vince only).
        self.assertEqual(counts, {tct.MEMBER: 5, tct.COS_ONLY: 4, tct.OUTSIDER: 0, WITHHELD: 0})
        self.assertLess(set(found[tct.COS_ONLY]), set(found[tct.MEMBER]))
        # Every one of those reads used the runtime's one connection to the store.
        self.assertEqual(opens.call_count, 1)
        # A store replaced on disk (another generation) is opened anew: the old handle
        # names nothing in it, the new capture's handle resolves.
        self.store.close()
        shutil.rmtree(Path(self.tmp.name) / STORE_DIR)
        self.store = open_store(self.tmp.name, self.audience)
        self.capture = TeamsCapture(self.store, self.capture.api, self.audience, tct.CAPTURE)
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        fresh = next(r for r in self.visible(tct.MEMBER, tct.GENERAL)
                     if r["key"]["id"] == "1001")["citation"]
        self.assertNotEqual(fresh, self.root["citation"])
        self.assertEqual((self.gate(tct.MEMBER, self.root["citation"]),
                          self.gate(tct.MEMBER, fresh)), (False, True))
        self.assertEqual(opens.call_count, 2)

    def test_a_quote_of_a_message_passes_the_claim_gate_for_members_only(self):
        self.assertTrue(self.claim(tct.MEMBER, tct.SECRET_TEXT)["passed"])
        self.assertTrue(self.claim(tct.COS_ONLY, tct.SECRET_TEXT)["passed"])
        # A passage of the message is a quote of it, checked like any original's text.
        self.assertTrue(self.claim(tct.MEMBER, "PO12172025 is 4,200 USD")["passed"])
        for wrong in ("Freight quote for PO99999999 is 4,200 USD", "PO12172025 is 4,500 USD"):
            mismatch = self.claim(tct.MEMBER, wrong)
            self.assertEqual({f["code"] for f in mismatch["failures"]},
                             {"claim_value_mismatch"}, wrong)
        for actor in (tct.OUTSIDER, WITHHELD):
            for value in (tct.SECRET_TEXT, "PO12172025 is 4,200 USD"):
                denied = self.claim(actor, value)
                self.assertFalse(denied["passed"], actor)
                self.assertEqual({f["code"] for f in denied["failures"]}, {"claim_uncited"})
        # Cos is a team member, not in the private channel: the same gate denies it there.
        self.assertFalse(self.claim(tct.COS_ONLY, "Board-only pricing", self.private)["passed"])
        self.assertTrue(self.claim(tct.MEMBER, "Board-only pricing", self.private)["passed"])

    def test_an_unresolvable_teams_handle_denies(self):
        handle = self.root["citation"]
        body, _, tail = handle.rpartition("@")
        generation, _, capture_id = tail.rpartition("-")
        unresolvable = [body,                                          # no capture version
                        f"{body}@{generation}-{int(capture_id) + 999}",  # never captured
                        f"{body}@{'0' * len(generation)}-{capture_id}",  # other generation
                        handle.replace("/1001@", "/1009@"),             # another message
                        "teams-chat://" + handle.partition("://")[2],   # no chat path
                        handle + "#x"]
        gate = oi.citation_rows(self.runtime)
        for bad in unresolvable:
            self.assertIsNone(gate(bad, tct.MEMBER), bad)
            self.assertFalse(self.runtime.citation_status(bad, tct.MEMBER)["ok"], bad)
        self.assertEqual(self.runtime.resolve_citation(handle, tct.MEMBER)["row"]["body_text"],
                         tct.SECRET_TEXT)
        # No live audience, or no Teams capture in the state directory: nothing resolves.
        with tempfile.TemporaryDirectory() as empty:
            for other in (runtime(self.tmp.name, None), runtime(empty, LiveReaders(self.audience))):
                self.assertIsNone(oi.citation_rows(other)(handle, tct.MEMBER))
                status = other.citation_status(handle, tct.MEMBER)
                other.close()
                self.assertEqual((status["ok"], status["error"], status["source"]),
                                 (False, "source_not_configured", "teams-channel"))

    def test_a_member_removed_from_the_team_loses_access_after_the_next_capture(self):
        handle, quote = self.root["citation"], "PO12172025 is 4,200 USD"
        self.assertTrue(self.gate(tct.COS_ONLY, handle))
        self.assertTrue(self.claim(tct.COS_ONLY, quote)["passed"])
        self.graph.team_members = [(tct.U_VINCE, tct.VINCE)]
        # Until the next capture the store holds the membership it read.
        self.assertTrue(self.gate(tct.COS_ONLY, handle))
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        # Same runtime, same open connection: the new membership decides the next read, of
        # the cited revision and of the one the membership change made current.
        current = self.runtime.resolve_citation(handle, tct.MEMBER)["current_citation"]
        self.assertNotIn(current, (None, handle))
        for cited in (handle, current):
            self.assertFalse(self.gate(tct.COS_ONLY, cited))
            denied = self.claim(tct.COS_ONLY, quote, cited)
            self.assertEqual({f["code"] for f in denied["failures"]}, {"claim_uncited"})
        self.assertTrue(self.claim(tct.MEMBER, quote, current)["passed"])

    def test_a_private_channel_reply_resolves_for_its_members_only(self):
        text = "Board-only reply: hold the PO12172025 price"
        self.graph.post_root(tct.PRIVATE, "2002", "Board-only pricing, part two")
        self.graph.replies[(tct.PRIVATE, "2002")] = [
            self.graph.message("20021", text, channel=tct.PRIVATE, reply_to="2002")]
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        reply = next(r for r in self.visible(tct.MEMBER, tct.PRIVATE)
                     if r["key"]["id"] == "20021")
        self.assertEqual(reply["row"]["reply_to_id"], "2002")
        handle = reply["citation"]
        self.assertEqual(self.runtime.resolve_citation(handle, tct.MEMBER)["row"]["body_text"],
                         text)
        self.assertTrue(self.claim(tct.MEMBER, "hold the PO12172025 price", handle)["passed"])
        # Cos is in the team, not in the private channel; the others are in neither.
        for actor in (tct.COS_ONLY, tct.OUTSIDER, WITHHELD):
            self.assertFalse(self.gate(actor, handle), actor)
            denied = self.claim(actor, "hold the PO12172025 price", handle)
            self.assertEqual({f["code"] for f in denied["failures"]}, {"claim_uncited"}, actor)


    def test_a_chat_is_served_and_resolved_for_its_members_only(self):
        handle = next(r["citation"] for r in self.visible(tct.COS_ONLY, tct.CHAT)
                      if r["key"]["id"] == "3001")
        self.assertTrue(handle.startswith("teams-chat://"), handle)
        # The chat's members are cos and a guest; vince is in the team and in its private
        # channel, not in this chat, and the outsider is granted Teams but in nothing.
        members = (tct.MEMBER, tct.COS_ONLY)
        found = {}
        for actor in members + (tct.VINCE_ONLY, tct.OUTSIDER, WITHHELD):
            host = self.host(actor)
            out = host.call("plx.find", CHAT_FIND, actor)
            found[actor] = {c["object"]["object_id"] for c in out["candidates"]}
            self.assertEqual(self.gate(actor, handle), actor in members, actor)
            self.assertEqual(self.claim(actor, tct.SECRET_TEXT, handle)["passed"],
                             actor in members, actor)
            # A passage of a chat message is a quote of it, checked against the message.
            self.assertEqual(self.claim(actor, "PO12172025 is 4,200 USD", handle)["passed"],
                             actor in members, actor)
            if actor in members:
                continue
            hidden = [host.call(name, {"from" if name != "plx.object" else "object_id": o},
                                actor) for o in found[tct.MEMBER]
                      for name in ("plx.object", "plx.timeline")]
            self.assertTrue(all("error" in r for r in hidden), (actor, hidden))
            text = json.dumps([out, hidden])
            self.assertNotIn(tct.SECRET_TEXT, text, actor)
            self.assertNotIn("teams-chat://", text, actor)
        self.assertEqual({a: len(f) for a, f in found.items()},
                         {tct.MEMBER: 2, tct.COS_ONLY: 2, tct.VINCE_ONLY: 0, tct.OUTSIDER: 0,
                          WITHHELD: 0})
        mismatch = self.claim(tct.COS_ONLY, "PO12172025 is 4,500 USD", handle)
        self.assertEqual({f["code"] for f in mismatch["failures"]}, {"claim_value_mismatch"})
        self.assertEqual(self.runtime.resolve_citation(handle, tct.COS_ONLY)["row"]["body_text"],
                         tct.SECRET_TEXT)

    def test_a_chat_member_reads_only_the_messages_from_his_visible_history_on(self):
        # Vince joins the chat with history from between its two messages (01:00, 01:10).
        self.graph.chat_members.append((tct.U_VINCE, tct.VINCE))
        self.graph.history[tct.U_VINCE] = "2026-10-01T01:05:00Z"
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        rows = {r["key"]["id"]: r["citation"] for r in self.visible(tct.COS_ONLY, tct.CHAT)}
        host = self.host(tct.VINCE_ONLY)
        found = host.call("plx.find", CHAT_FIND, tct.VINCE_ONLY)
        self.assertEqual([c["object"]["object_id"].endswith('"id":"3002"}')
                          for c in found["candidates"]], [True])
        self.assertNotIn(rows["3001"], json.dumps(found))
        for message, readable in (("3001", False), ("3002", True)):
            self.assertEqual(self.gate(tct.VINCE_ONLY, rows[message]), readable, message)
        self.assertFalse(self.claim(tct.VINCE_ONLY, tct.SECRET_TEXT, rows["3001"])["passed"])
        self.assertTrue(self.claim(tct.COS_ONLY, tct.SECRET_TEXT, rows["3001"])["passed"])

    def test_chat_versions_saved_before_history_starts_obey_the_current_start(self):
        # Vince joins the chat with its whole history, and 3002 is reworded.
        quote = "PO12172025 ships Friday"
        self.graph.chat_members.append((tct.U_VINCE, tct.VINCE))
        self.graph.chat["3002"] = self.graph.message("3002", f"Second note: {quote}")
        self.capture.run(tct.scope())
        # A capture from before history starts were recorded (74bf992e5^1) saved each chat
        # version and the chat's membership whole, with no history_start; 3002 then went
        # missing from the chat, so no later capture saves it again.
        kind, unit = CHAT_KIND.name, Conversation("chat", tct.CHAT).unit
        legacy = {"status": "read", "basis": "chat_members", "identities": sorted(
            {tct.U_COS, tct.COS, tct.U_OTHER, tct.OTHER, tct.U_VINCE, tct.VINCE})}
        current = {r.path[-1]: r for r in self.store.current_records(kind, unit)}
        for seen in (("3001", "3002"), ("3001",)):
            run = self.store.begin_run(kind, unit, [f"teams:chat:{tct.CHAT}"])["run_id"]
            self.store.set_unit_acl(run, legacy)
            self.store.save_page(run, [CapturedRecord(current[m].path, {
                "row": current[m].payload["row"],
                "meta": {"source_acl": legacy, "source_api": "graph.chat_messages"}},
                current[m].source_version, current[m].lineage) for m in seen], None)
            self.store.finish_run(run, "complete")
        with sqlite3.connect(self.store.path) as conn:
            old = {path.rpartition("/")[2]: rest for path, *rest in conn.execute(
                "SELECT path, capture_id, payload, missing_from_source_since FROM versions "
                "WHERE kind = ? AND superseded_at IS NULL AND path NOT LIKE '%/conversation'",
                (kind,))}
        self.assertEqual({m: (json.loads(payload)["meta"]["source_acl"], missing is not None)
                          for m, (_, payload, missing) in old.items()},
                         {"3001": (legacy, False), "3002": (legacy, True)})
        handles = {m: make_handle(CHAT_KIND, current[m].path, self.store.generation, capture)
                   for m, (capture, _, _) in old.items()}
        # A membership without recorded starts admits nobody until it is read again.
        self.assertFalse(any(self.gate(a, h) for a in (tct.COS_ONLY, tct.VINCE_ONLY)
                             for h in handles.values()))
        # Vince is re-added without history; 3002 stays missing.
        self.graph.history[tct.U_VINCE] = "2030-01-01T00:00:00Z"
        del self.graph.chat["3002"]
        self.capture.run(tct.scope())
        now = self.runtime.resolve_citation(handles["3001"], tct.COS_ONLY)["current_citation"]
        self.assertNotIn(now, (None, handles["3001"]))
        self.assertEqual(self.host(tct.VINCE_ONLY).call("plx.find", CHAT_FIND,
                                                        tct.VINCE_ONLY)["candidates"], [])
        for message, value in (("3001", "PO12172025 is 4,200 USD"), ("3002", quote)):
            for cited in {handles[message], now if message == "3001" else handles[message]}:
                self.assertFalse(self.gate(tct.VINCE_ONLY, cited), (message, cited))
                denied = self.claim(tct.VINCE_ONLY, value, cited)
                self.assertEqual({f["code"] for f in denied["failures"]}, {"claim_uncited"})
                # Cos sees the whole history: the same versions still pass for him.
                self.assertTrue(self.gate(tct.COS_ONLY, cited), (message, cited))
        self.assertTrue(self.claim(tct.COS_ONLY, quote, handles["3002"])["passed"])
        self.assertTrue(self.claim(tct.COS_ONLY, "PO12172025 is 4,200 USD", now)["passed"])

    def test_conversations_are_objects_gated_per_unit_with_their_participants(self):
        """TeamsChat, TeamsChannel and Team project from the conversation records, with
        name, times and participants (Entra object ids, display names); each reader sees a
        conversation and its participants only as it sees its messages, a message links to
        its conversation, and a participant excluded after the capture withholds the
        conversation whole (the case runtime's people exclusion on every read)."""
        seen: dict = {}
        for actor in (tct.MEMBER, tct.COS_ONLY, tct.VINCE_ONLY, tct.OUTSIDER, WITHHELD):
            host = self.host(actor)
            seen[actor] = {}
            for kind, key in (("TeamsChat", "chat_id"), ("TeamsChannel", "channel_id"),
                              ("Team", "team_id")):
                out = host.call("plx.find", {"object_type": kind, "limit": 50, "filters": [
                    {"property": key, "op": "present"}]}, actor)
                seen[actor][kind] = [host.call("plx.object", {"object_id": c["object"][
                    "object_id"]}, actor) for c in out["candidates"]]
                if not seen[actor][kind]:
                    text = json.dumps(out)
                    for needle in ("teams-chat://", "teams-channel://", tct.U_OTHER,
                                   "R&D Bi-weekly", "QA hand-offs"):
                        self.assertNotIn(needle, text, (actor, kind))
        counts = {a: {k: len(v) for k, v in kinds.items()} for a, kinds in seen.items()}
        self.assertEqual(counts, {
            tct.MEMBER: {"TeamsChat": 1, "TeamsChannel": 2, "Team": 1},
            tct.COS_ONLY: {"TeamsChat": 1, "TeamsChannel": 1, "Team": 1},
            tct.VINCE_ONLY: {"TeamsChat": 0, "TeamsChannel": 2, "Team": 1},
            tct.OUTSIDER: {"TeamsChat": 0, "TeamsChannel": 0, "Team": 0},
            WITHHELD: {"TeamsChat": 0, "TeamsChannel": 0, "Team": 0}})

        def values(found):
            return {v["property"]: (v["value"]["untrusted_text"] if isinstance(v["value"], dict)
                                    else v["value"]) for v in found["values"]}
        chat = values(seen[tct.COS_ONLY]["TeamsChat"][0])
        self.assertEqual((chat["topic"], chat["chat_type"]), ("R&D Bi-weekly Meeting", "oneOnOne"))
        self.assertEqual([p["id"] for p in json.loads(chat["participants"])],
                         sorted([tct.U_COS, tct.U_OTHER]))
        self.assertIsNotNone(chat["last_activity_at"])
        private = next(values(c) for c in seen[tct.MEMBER]["TeamsChannel"]
                       if values(c)["membership_type"] == "private")
        self.assertEqual((private["display_name"], [p["id"] for p in json.loads(
            private["participants"])]), ("QA hand-offs", [tct.U_VINCE]))
        self.assertEqual(values(seen[tct.MEMBER]["Team"][0])["display_name"], "Operations")
        # A chat message links to its chat; a channel message to its channel, it to its team.
        message = self.host(tct.COS_ONLY).call("plx.find", CHAT_FIND, tct.COS_ONLY)
        around = self.host(tct.COS_ONLY).call("plx.around", {
            "from": message["candidates"][0]["object"]["object_id"],
            "path": [{"link_type": "TeamsChatMessage.in_chat", "direction": "forward"}]},
            tct.COS_ONLY)
        self.assertEqual([o["object_type"] for o in around["objects"]], ["TeamsChat"], around)
        handle = seen[tct.COS_ONLY]["TeamsChat"][0]["values"][0]["citation"]["l1_handle"]
        self.assertIn("/conversation@", handle)
        channel = seen[tct.MEMBER]["TeamsChannel"][0]["values"][0]["citation"]["l1_handle"]
        self.assertTrue(self.gate(tct.COS_ONLY, handle))
        # The chat's other participant is excluded after the capture: the chat object and its
        # participants are withheld from every reader, as its messages are.
        for excluded, cited in ((tct.U_OTHER, handle), (tct.U_VINCE, channel)):
            guarded = CaseRuntime(self.tmp.name, client=None,
                                  audience=AudienceMap({}, tenant="plx"), tenant="plx",
                                  live=LiveReaders(self.audience),
                                  people_exclusion=PeopleExclusion([excluded]))
            self.addCleanup(guarded.close)
            self.assertIsNone(oi.citation_rows(guarded)(cited, tct.MEMBER), excluded)
            with self.assertRaises(Exception):
                guarded.resolve_citation(cited, tct.MEMBER)

    def test_a_chat_without_a_captured_membership_is_not_projected_or_resolved(self):
        handle = self.visible(tct.COS_ONLY, tct.CHAT)[0]["citation"]
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("DELETE FROM unit_acl WHERE kind = 'teams_chat'")
        path, report = self.project()
        with sqlite3.connect(path) as conn:
            types = dict(conn.execute("SELECT object_type, COUNT(*) FROM objects "
                                      "WHERE object_type LIKE 'Teams%' GROUP BY object_type"))
        self.assertEqual(types, {"TeamsChannelMessage": 5, "TeamsChannel": 2})
        for binding in ("gen.teams.chat_messages", "teams.chats"):
            self.assertIn({"code": "source_not_captured", "where": binding,
                           "count": 1, "samples": []}, report["gaps"])
        self.assertEqual(self.host(tct.COS_ONLY).call("plx.find", CHAT_FIND,
                                                      tct.COS_ONLY)["candidates"], [])
        self.assertFalse(self.gate(tct.COS_ONLY, handle))


if __name__ == "__main__":
    unittest.main()
