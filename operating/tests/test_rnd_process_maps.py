"""The three R&D process maps as cited answers on the ontology (synthetic records only).

* ``operating.meeting_agenda`` (``plx.agenda``, spec ``ontology/agendas/rnd_biweekly.json``):
  the bi-weekly agenda from Teams, Mission Control and the not-held sources as named gaps;
* ``operating.material_samples``: sample requests linked to raw materials and suppliers,
  quantity left only from recorded amounts;
* ``operating.formula_versions``: ingredient-list files resolved to one formula version,
  mix-ups flagged, FileMaker formula access a named gap.

One store is built by the real projector from synthetic Teams, Mission Control, Portal and
SharePoint captures; every read goes through ``OntologyTools.call`` as a reader under its
own policy and item gate. Evidence: isolated-fixture.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import quote

from ontology import schema as schema_loader
from ontology import store_ddl
from ontology.project import CapturedRecord, build_store
from ontology.types import Actor
from operating import answer_gate, ontology_tools as ot
from operating.ontology_tools_traverse import TOOL_NAMES as TRAVERSE, TraverseTools
from operating.formula_versions import ingredient_list_versions
from operating.material_samples import link_requests, quantity_left
from operating.meeting_agenda import (AGENDA_DIR, AgendaTool, assemble, check_agenda,
                                      load_agendas)

NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2026-10-26T10:00:00.000000Z"
# Graph chat ids (the case host grants only a unit that names one well-formed chat).
MEETING, QA = "19:synth-meeting@thread.v2", "19:synth-qa@thread.v2"
BUCKET, DRIVE = "BKT-SYNTH", "drive-synth"
FULL = Actor("plx", frozenset({"staff:full"}), "restricted")
NO_PII = Actor("plx", frozenset({"staff:nopii"}), "restricted")
CHAT_ONLY = Actor("plx", frozenset({"staff:chat"}), "restricted")
HIDDEN_MESSAGE = "m-hidden"


class _Static:
    def __init__(self, source, records):
        self.source, self._records = source, records

    def records(self, relations):
        return {rel: [r for r in self._records if r.relation == rel] for rel in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


def _rec(source, relation, key, row, handle, audience):
    return CapturedRecord(source, relation, json.dumps(key, separators=(",", ":")), row,
                          handle, "row", "0" * 64, T0, None, T0, audience)


def chat_handle(chat):
    """The handle prefix of a chat's messages (path parts percent-encoded, as captured)."""
    return f"teams-chat://{quote(chat, safe='')}/"


def chat_n(n):
    return f"19:synth-n{n}@thread.v2"


def _message(chat, mid, at, *, subject=None, kind="message", attachments=()):
    return _rec("teams", "chatMessages", {"chat_id": chat, "id": mid},
                {"chat_id": chat, "id": mid, "created_at": at, "message_type": kind,
                 "subject": subject, "from": {"user": {"displayName": f"Synth {mid}"}},
                 "attachments": [{"name": n} for n in attachments]},
                f"{chat_handle(chat)}{mid}@g1-1", f"teams:chat:{chat}")


def _task(tid, stage, updated):
    return _rec("mission_control", "tasks", {"scope": "ops", "id": tid},
                {"scope": "ops", "id": tid, "title": f"Synthetic project {tid}", "stage": stage,
                 "bucket": BUCKET, "assignee": f"{tid.lower()}@synth.example",
                 "createdAt": "2026-09-01T00:00:00Z", "updatedAt": updated},
                f"mc://ops/tasks/{tid}@g1", "mc:ops/tasks/PRJ-SYNTH")


def _gate(gid, task, status):
    return _rec("mission_control", "approvals", {"scope": "ops", "id": gid},
                {"scope": "ops", "id": gid, "task_id": task, "status": status,
                 "reason": f"Synthetic decision {gid}", "requestedBy": "lead@synth.example",
                 "requestedAt": "2026-10-19T12:00:00Z"},
                f"mc://ops/approvals/{gid}@g1", "mc:ops/approvals/PRJ-SYNTH")


def _portal(relation, row):
    return _rec("portal_api", relation, {"id": row["id"]}, row,
                f"api://{relation}/{row['id']}@g-1", f"api:{relation}")


def _file(fid, name, sha):
    return _rec("sharepoint", "driveItems", [DRIVE, fid],
                {"drive_id": DRIVE, "id": fid, "name": name, "content_sha256": sha,
                 "last_modified": "2026-10-16T13:26:00Z"},
                f"sharepoint://{DRIVE}/{fid}@e1", f"sharepoint:{DRIVE}")


def _teams():
    out = [_message(MEETING, "m-before", "2026-10-13T14:00:00Z"),
           _message(MEETING, "m-posting", "2026-10-14T14:16:00Z",
                    attachments=("R&D Meeting 2026-10-14.pdf",)),
           _message(MEETING, "m-after-1", "2026-10-15T09:00:00Z"),
           _message(MEETING, "m-after-2", "2026-10-20T13:19:00Z", subject="Synthetic note"),
           _message(MEETING, "m-system", "2026-10-21T10:00:00Z", kind="systemEventMessage"),
           _message(MEETING, HIDDEN_MESSAGE, "2026-10-22T10:00:00Z"),
           _message(QA, "q-1", "2026-10-16T10:00:00Z")]
    for n in (1, 10, 60):
        out += [_message(chat_n(n), f"n{n}-{i:02d}", f"2026-10-{15 + i % 10:02d}T10:{i:02d}:00Z")
                for i in range(n)]
    return out


class Fixture:
    """The store, built once for the module."""

    path: Path | None = None
    compiled = None

    @classmethod
    def build(cls):
        if cls.path is not None:
            return
        cls.tmp = tempfile.TemporaryDirectory()
        cls.compiled = schema_loader.load_schema()
        mc = [_task("TASK-S1", "progress", "2026-10-20T00:00:00Z"),
              _task("TASK-S2", "backlog", "2026-10-01T00:00:00Z"),
              _task("TASK-S3", "verified", "2026-10-18T00:00:00Z"),
              _task("TASK-S4", "review", "2026-10-02T00:00:00Z"),
              _gate("apg_s1", "TASK-S1", "approved"), _gate("apg_s4", "TASK-S4", "pending")]
        portal = [_portal("public.raw_material", {"id": 1, "rawcode": "R-9001", "name": "Synth Oil"}),
                  _portal("public.raw_material", {"id": 2, "rawcode": "R-9002", "name": "Synth Wax"}),
                  _portal("public.supplier", {"id": 7, "name": "Synth Supplier One"}),
                  _portal("public.supplier", {"id": 8, "name": "Synth Supplier Two"}),
                  _portal("public.raw_material_supplier",
                          {"id": 3, "idraw": 1, "idsupplier": 7, "itemnumber": "U-90001"}),
                  _portal("public.raw_material_supplier",
                          {"id": 4, "idraw": 2, "idsupplier": 7, "itemnumber": "U-90002"}),
                  _portal("public.raw_material_supplier",
                          {"id": 5, "idraw": 2, "idsupplier": 8, "itemnumber": "U-90002"})]
        files = [_file("f1", "IL - Synth Baby Wash F-26-0001.docx", "a" * 64),
                 _file("f2", "IL- Synth Tonic_F-26-0002 (123456).docx", "b" * 64),
                 _file("f3", "IL - Synth Tonic F-26-0002 copy.docx", "c" * 64),
                 _file("f4", "IL - Synth Lotion.docx", "d" * 64),
                 _file("f5", "IL - Synth Mix F-26-0003 F-26-0004.docx", "e" * 64),
                 _file("f6", "ILLUSTRATION F-26-0001.pdf", "f" * 64)]
        cls.path = Path(cls.tmp.name) / "ontology.sqlite"
        cls.rows = {r.l1_handle: r.row for r in (*_teams(), *mc, *portal, *files)}
        build_store(cls.path, cls.compiled, {
            "teams": _Static("teams", _teams()), "mission_control": _Static("mission_control", mc),
            "portal_api": _Static("portal_api", portal), "sharepoint": _Static("sharepoint", files)},
            clock=lambda: NOW)

    @classmethod
    def tools(cls, grants, *, refuse=()):
        """OntologyTools whose policy grants each actor the units ``grants`` names, with an
        item gate that refuses ``refuse`` handles to everyone but FULL."""
        reader = ot.StoreReader.open(cls.path)
        keys = reader.audience_keys()
        audiences = {k: {next(iter(a.principals)) for a, units in grants.items()
                         if any(k.startswith(u) for u in units)} for k in keys}
        markings = {m: {next(iter(FULL.principals))} for m in store_ddl.MARKINGS}
        policy = ot.ReadPolicy("plx", audiences, markings)

        def gate(actor, handle):
            return actor == FULL or not any(handle.startswith(r) for r in refuse)
        tools = ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(cls.compiled),
                                 grammar=None, item_gate=gate, clock=lambda: NOW)
        return tools, reader


EVERYTHING = ("teams:", "mc:", "api:", "sharepoint:")
SCOPE = {"meeting_chat": {"chat_id": MEETING}, "qa_handoffs": {"chat_id": QA},
         "mc_tasks": {"bucket": BUCKET}}


def _setup(case):
    Fixture.build()
    tools, reader = Fixture.tools({FULL: EVERYTHING, NO_PII: EVERYTHING,
                                   CHAT_ONLY: (f"teams:chat:{MEETING}",)},
                                  refuse=(f"{chat_handle(MEETING)}{HIDDEN_MESSAGE}@",))
    case.addCleanup(reader.conn.close)
    traverse = TraverseTools(tools)

    def call(name, arguments, actor):   # the host's table: around/timeline beside the base
        return (traverse if name in TRAVERSE else tools).call(name, arguments, actor)
    case.call = call
    case.schema = ot.SchemaView.from_compiled(Fixture.compiled)


def _native(line):
    return json.loads(line["object_id"].split(":", 2)[2])["id"]


class MeetingAgendaTests(unittest.TestCase):
    def setUp(self):
        _setup(self)
        self.agenda = load_agendas(AGENDA_DIR, self.schema)["rnd_biweekly"]

    def run_agenda(self, actor, **kw):
        return assemble(self.call, actor, self.agenda, scope=kw.pop("scope", SCOPE), **kw)

    def test_agenda_is_cited_sectioned_and_names_what_it_lacks(self):
        out = self.run_agenda(FULL)
        self.assertEqual(out["since"]["basis"], "previous_agenda")
        self.assertEqual(out["since"]["value"], "2026-10-14T14:16:00Z")
        self.assertTrue(out["since"]["cited"]["citation"]["l1_handle"].startswith(
            f"{chat_handle(MEETING)}m-posting@"))
        changes = {(l["lane"], _native(l)) for l in out["changes"]}
        # After the posting only; the posting, the earlier message and a system event are not lines.
        self.assertEqual(changes, {("meeting_chat", "m-after-1"), ("meeting_chat", "m-after-2"),
                                   ("meeting_chat", HIDDEN_MESSAGE), ("qa_handoffs", "q-1"),
                                   ("mc_tasks", "TASK-S1"), ("mc_tasks", "TASK-S3")})
        self.assertEqual({_native(l) for l in out["open_projects"]},
                         {"TASK-S1", "TASK-S2", "TASK-S4"})
        blockers = {_native(l): l for l in out["blockers"]}
        self.assertEqual({k: b["basis"] for k, b in blockers.items()},
                         {"TASK-S2": "no_change_since_last_meeting",
                          "TASK-S4": "waiting_on_decision"})
        self.assertEqual([_native(l) for l in out["decisions_needed"]], ["apg_s4"])
        decision = out["decisions_needed"][0]
        self.assertEqual(blockers["TASK-S4"]["decisions"], [decision["line"]])
        lines = [l for k in ("open_projects", "changes", "blockers", "decisions_needed")
                 for l in out[k]]
        self.assertEqual(len({l["line"] for l in lines}), len(lines))
        for line in lines:
            self.assertTrue(line["citations"], line)
            for key in ("at", "title", "status", "owner"):
                if line[key] is not None:
                    self.assertIn(line[key]["citation"]["l1_handle"], line["citations"])
        task = next(l for l in out["open_projects"] if _native(l) == "TASK-S1")
        self.assertEqual(task["status"]["value"]["untrusted_text"], "progress")
        self.assertEqual(task["owner"]["value"]["untrusted_text"], "task-s1@synth.example")
        self.assertIn("text_not_projected", next(l for l in out["changes"]
                                                 if _native(l) == "m-after-1")["missing"])
        codes = {(g["code"], g["subject"]) for g in out["gaps"]}
        for expected in (("binding_expected", "meeting_chat"),
                         ("lane_scope_not_given", "kb_channel"),
                         ("lane_scope_not_given", "sample_shipments"),
                         ("source_not_in_ontology", "R&D KB Board (Planner)"),
                         ("source_not_served", "FileMaker R&D > Formulas (samples made, F-numbers)"),
                         ("source_not_served", "Portal R&D project (client + formula, status, "
                                               "waiting on, since when)")):
            self.assertIn(expected, codes)
        self.assertTrue(out["complete"])
        self.assertTrue(any(o["lines"] for o in out["owners"]))

    def test_every_reader_sees_only_what_it_may(self):
        chat_only = self.run_agenda(CHAT_ONLY)
        text = json.dumps(chat_only)
        self.assertNotIn(HIDDEN_MESSAGE, text)   # the item gate refuses it: not even its id
        self.assertNotIn("q-1", text)
        self.assertNotIn("TASK-S", text)
        self.assertNotIn("synth.example", text)
        self.assertEqual({_native(l) for l in chat_only["changes"]}, {"m-after-1", "m-after-2"})
        self.assertIn(("nothing_visible", "mc_tasks"),
                      {(g["code"], g["subject"]) for g in chat_only["gaps"]})
        no_pii = self.run_agenda(NO_PII)
        self.assertNotIn("synth.example", json.dumps(no_pii))
        for line in no_pii["open_projects"] + no_pii["decisions_needed"]:
            self.assertIsNone(line["owner"])
            self.assertIn("owner_withheld", line["missing"])
        self.assertIn("withheld_from_reader", {g["code"] for g in no_pii["gaps"]})

    def test_unknown_or_missing_input_fails_closed(self):
        tool = AgendaTool(self.call, self.schema)
        self.assertEqual(self.run_agenda(FULL, since="2026-10-14T14:16:00")["error"]["code"],
                         "invalid_since")
        for args, code in (({"agenda": "rnd_biweekly", "scope": {"no_lane": {"x": "y"}}},
                            "invalid_scope"),
                           ({"agenda": "rnd_biweekly", "scope": {"mc_tasks": {"stage": "qa"}}},
                            "invalid_scope"),
                           ({"agenda": "unknown"}, "unknown_agenda"),
                           ({"agenda": "rnd_biweekly", "extra": 1}, "invalid_input")):
            self.assertEqual(tool.call(args, FULL)["error"]["code"], code, args)
        # No since and no visible previous agenda: nothing is assembled, never an empty agenda.
        out = tool.call({"agenda": "rnd_biweekly", "scope": {"mc_tasks": {"bucket": BUCKET}}},
                        FULL)
        self.assertFalse(out["complete"])
        self.assertEqual(out["changes"] + out["open_projects"], [])
        self.assertIn("previous_agenda_not_found", {g["code"] for g in out["gaps"]})
        problems = check_agenda({**self.agenda.doc, "lanes": [
            {**self.agenda.doc["lanes"][0], "owner": ["sender"]},
            {**self.agenda.doc["lanes"][6], "section": "minutes"},
            {**self.agenda.doc["lanes"][7], "via": {"lane": "mc_tasks", "direction": "forward",
                                                    "link_type": "MCApproval.gate_of"}},
            {**self.agenda.doc["lanes"][5], "object_type": "RelPublicAppProject",
             "changed": "created_at", "title": ["id"], "scope": ["id"]}]}, self.schema)
        for needle in ("declares no property sender", "unknown section minutes",
                       "does not lead from MCTask", "RelPublicAppProject is not served"):
            self.assertTrue(any(needle in p for p in problems), (needle, problems))

    def test_a_claim_citing_an_agenda_line_passes_the_answer_gate_and_others_are_refused(self):
        """plx.agenda's output contract makes each line's cited records evidence for the
        final-answer gate (answer_gate.evidence_handles); a claim citing a record the agenda
        did not return is refused (cites_what_this_run_did_not_return)."""
        out = AgendaTool(self.call, self.schema).call(
            {"agenda": "rnd_biweekly", "scope": SCOPE}, FULL)
        seen = answer_gate.evidence_handles("plx.agenda", out, arguments={"agenda": "x"})
        lines = [l for k in ("open_projects", "changes", "blockers", "decisions_needed")
                 for l in out[k]]
        self.assertEqual(seen, {h for l in lines for h in l["citations"]}
                         | {out["since"]["cited"]["citation"]["l1_handle"]})
        task = next(l for l in out["open_projects"] if _native(l) == "TASK-S1")

        class Runtime:   # the reader's re-read of a cited record (CaseRuntime.claim_row)
            @staticmethod
            def claim_row(handle, actor):
                return {"citation": handle, "status": "resolved", "changed_since": False,
                        "row": Fixture.rows[handle]}
        server = type("Server", (), {"case_runtime": Runtime(), "actor": FULL})()

        def gate(handle, field, value):
            return answer_gate.gate_answer(
                {"text": "Status {s}.", "claims": [{"id": "s", "value": value,
                                                    "citation": handle, "field": field}]},
                server, seen=seen, returned={}, may_repair=False)
        cell = task["status"]["citation"]
        passed = gate(cell["l1_handle"], cell["field_path"], "progress")
        self.assertEqual((passed["outcome"], [c["id"] for c in passed["claims"]]),
                         ("answered", ["s"]))
        # The message before the previous agenda is no agenda line: never returned here.
        before = next(h for h in Fixture.rows if h.startswith(f"{chat_handle(MEETING)}m-before@"))
        self.assertNotIn(before, seen)
        refused = gate(before, "row.created_at", "2026-10-13T14:00:00Z")
        self.assertEqual(refused["outcome"], "abstained")
        self.assertIn("cites_what_this_run_did_not_return",
                      json.dumps(refused["unsupported"]))

    def test_a_lane_given_no_scope_reads_its_default_scope_as_the_asking_reader(self):
        """Reproduced (2026-10-29): agents asked plx.agenda with no scope (they cannot
        know a chat id) and every lane was lane_scope_not_given. A lane the spec gives a
        default_scope is read with it, named as such in scope_basis; a caller's scope
        replaces it whole; it is a filter only, gated per reader like a given scope."""
        import dataclasses

        shipped = {lane["id"]: lane for lane in self.agenda.doc["lanes"]}
        self.assertEqual({lid for lid, lane in shipped.items() if "default_scope" in lane},
                         {"meeting_chat", "sourcing_chat"})
        out = self.run_agenda(FULL, since="2026-10-01T00:00:00Z",
                              scope={"mc_tasks": {"bucket": BUCKET}})
        self.assertEqual(out["scope_basis"], {"mc_tasks": "argument",
                                              "meeting_chat": "default",
                                              "sourcing_chat": "default"})
        unscoped = {g["subject"] for g in out["gaps"] if g["code"] == "lane_scope_not_given"}
        self.assertEqual(unscoped, {"kb_channel", "qa_handoffs", "sample_shipments",
                                    "knowledge"})

        def with_default(chat):
            lanes = [dict(lane, default_scope={"values": {"chat_id": chat},
                                               "evidence": "test"})
                     if lane["id"] == "meeting_chat" else lane
                     for lane in self.agenda.doc["lanes"]]
            return dataclasses.replace(self.agenda, doc=dict(self.agenda.doc, lanes=lanes))

        def lines(answer):
            return sorted((k, l["lane"], l["object_id"]) for k in
                          ("open_projects", "changes", "blockers", "decisions_needed")
                          for l in answer[k])
        given = {"meeting_chat": {"chat_id": MEETING}, "qa_handoffs": {"chat_id": QA},
                 "mc_tasks": {"bucket": BUCKET}}
        rest = {k: v for k, v in given.items() if k != "meeting_chat"}
        for actor in (FULL, NO_PII, CHAT_ONLY):
            with self.subTest(actor=repr(actor)):
                # The default finds the previous agenda and the lines a given scope finds.
                explicit = assemble(self.call, actor, self.agenda, scope=given)
                default = assemble(self.call, actor, with_default(MEETING), scope=rest)
                self.assertEqual(default["scope_basis"]["meeting_chat"], "default")
                self.assertEqual(default["since"], explicit["since"])
                self.assertEqual(lines(default), lines(explicit))
                # A default naming a chat the reader may not open shows what the same
                # given scope shows.
                other = assemble(self.call, actor, with_default(QA), scope=rest,
                                 since="2026-10-01T00:00:00Z")
                asked = assemble(self.call, actor, self.agenda, since="2026-10-01T00:00:00Z",
                                 scope={**rest, "meeting_chat": {"chat_id": QA}})
                self.assertEqual(lines(other), lines(asked))
        chat_only = assemble(self.call, CHAT_ONLY, with_default(QA), scope=rest,
                             since="2026-10-01T00:00:00Z")
        self.assertFalse([l for l in chat_only["changes"] if l["lane"] == "meeting_chat"])
        # A caller's scope replaces the default whole.
        replaced = assemble(self.call, FULL, with_default(QA), scope=given)
        self.assertEqual(replaced["scope_basis"]["meeting_chat"], "argument")
        self.assertEqual(lines(replaced), lines(assemble(self.call, FULL, self.agenda,
                                                         scope=given)))
        # A default that does not give exactly the lane's scope properties fails the check.
        for values in ({"chat_id": QA, "team_id": "t"}, {"team_id": "t"}):
            lanes = [dict(lane, default_scope={"values": values, "evidence": "test"})
                     if lane["id"] == "meeting_chat" else lane
                     for lane in self.agenda.doc["lanes"]]
            problems = check_agenda(dict(self.agenda.doc, lanes=lanes), self.schema)
            self.assertIn("meeting_chat: default_scope must give every scope property, and "
                          "only those", problems)

    def test_every_record_becomes_one_line_at_any_count(self):
        tool = AgendaTool(self.call, self.schema)
        for n in (1, 10, 60):
            started, ids, offset = time.monotonic(), [], 0
            while offset is not None:   # pages of plx.agenda, each within the result budget
                out = tool.call({"agenda": "rnd_biweekly", "since": "2026-10-01T00:00:00Z",
                                 "scope": {"meeting_chat": {"chat_id": chat_n(n)}},
                                 "offset": offset, "limit": 50}, FULL)
                self.assertLess(len(json.dumps(out)), 48000, n)
                self.assertEqual(out["page"]["total"], n)
                ids += [_native(l) for l in out["changes"]]
                offset = out["page"]["next_offset"]
            self.assertEqual(sorted(ids), sorted(f"n{n}-{i:02d}" for i in range(n)))
            self.assertLess(time.monotonic() - started, 30, n)
            self.assertNotIn("read_truncated", {g["code"] for g in out["gaps"]})


class MaterialSampleTests(unittest.TestCase):
    def setUp(self):
        _setup(self)

    def test_requests_link_to_material_and_supplier_and_name_quantity_left(self):
        cite = f"{chat_handle(QA)}q-1@g1-1"
        requests = [
            {"request_id": "r1", "citation": cite, "supplier_code": "U-90001",
             "r_number": "R-9001", "received": {"amount": "1", "unit": "bottle",
                                                 "citation": cite}},
            {"request_id": "r2", "citation": cite, "supplier_code": "U-90002"},
            {"request_id": "r3", "citation": cite, "trade_name": "Synth new development",
             "based_on": "U-90001"},
            {"request_id": "r4", "citation": cite, "supplier_code": "U-90001", "r_number": "R-9002"},
            {"request_id": "r5", "citation": cite, "supplier_code": "U-90001",
             "received": {"amount": "200", "unit": "g", "citation": cite},
             "used": [{"amount": "1.90", "unit": "g", "citation": cite},
                      {"amount": "4.00", "unit": "grams", "citation": cite}]},
            {"request_id": "r6", "supplier_code": "U-90001"}]
        out = link_requests(self.call, FULL, requests)
        by = {r["request_id"]: r for r in out["requests"]}
        self.assertEqual(set(by), {"r1", "r2", "r3", "r4", "r5"})
        self.assertIn("request_refused", {g["code"] for g in out["gaps"]})
        self.assertIn("sample_requests_not_served", {g["code"] for g in out["gaps"]})
        r1 = by["r1"]
        self.assertEqual(r1["link_basis"], "supplier_code+r_number")
        self.assertEqual(r1["material"]["r_number"]["value"], "R-9001")
        self.assertEqual(r1["supplier"]["name"]["value"]["untrusted_text"], "Synth Supplier One")
        self.assertTrue(r1["material"]["r_number"]["citation"]["l1_handle"].startswith(
            "api://public.raw_material/1@"))
        self.assertIsNone(r1["quantity_left"]["value"])
        self.assertEqual({g["code"] for g in r1["quantity_left"]["gaps"]}, {"received_is_a_count"})
        self.assertIsNone(by["r2"]["material"])
        self.assertEqual(len(by["r2"]["candidates"]), 2)
        self.assertIn("supplier_code_ambiguous", {g["code"] for g in by["r2"]["gaps"]})
        self.assertIn("material_unidentified", {g["code"] for g in by["r3"]["gaps"]})
        self.assertIsNone(by["r4"]["material"])
        self.assertIn("r_number_disagrees", {g["code"] for g in by["r4"]["gaps"]})
        left = by["r5"]["quantity_left"]
        self.assertEqual((left["value"], left["unit"], left["gaps"]), ("194.10", "g", []))
        self.assertEqual(left["steps"], ["200 - 1.90 = 198.10", "198.10 - 4.00 = 194.10"])
        # A reader granted no Portal unit links nothing and is told it saw nothing.
        hidden = link_requests(self.call, CHAT_ONLY, requests[:1])
        self.assertIsNone(hidden["requests"][0]["material"])
        self.assertNotIn("Synth Supplier", json.dumps(hidden))
        self.assertIn("nothing_visible", {g["code"] for g in hidden["gaps"]})

    def test_quantity_left_needs_recorded_amounts_in_one_unit(self):
        cite = "x://synthetic/1@1"
        received = {"amount": "200", "unit": "g", "citation": cite}
        for used, code in (([], "usage_not_recorded"),
                           ([{"amount": "0.1", "unit": "kg", "citation": cite}], "units_disagree"),
                           ([{"amount": "250", "unit": "g", "citation": cite}],
                            "usage_exceeds_received"),
                           ([{"amount": "1", "unit": "g"}], "quantity_uncited"),
                           ([{"amount": "two", "unit": "g", "citation": cite}],
                            "amount_not_a_number")):
            out = quantity_left(received, used)
            self.assertIsNone(out["value"], code)
            self.assertIn(code, {g["code"] for g in out["gaps"]})
        self.assertIn("received_amount_not_recorded",
                      {g["code"] for g in quantity_left(None, [])["gaps"]})


class FormulaVersionTests(unittest.TestCase):
    def setUp(self):
        _setup(self)

    @staticmethod
    def cited(value, n):
        return {"value": value, "citation": f"fm://synthetic/formula/{n}@1"}

    def test_each_il_resolves_to_one_version_and_mix_ups_are_flagged(self):
        out = ingredient_list_versions(self.call, FULL)
        by = {il["name"]["value"]["untrusted_text"]: il for il in out["ingredient_lists"]}
        self.assertNotIn("ILLUSTRATION F-26-0001.pdf", by)
        self.assertEqual({k: v["status"] for k, v in by.items()}, {
            "IL - Synth Baby Wash F-26-0001.docx": "named_unverified",
            "IL- Synth Tonic_F-26-0002 (123456).docx": "named_unverified",
            "IL - Synth Tonic F-26-0002 copy.docx": "named_unverified",
            "IL - Synth Lotion.docx": "il_names_no_formula",
            "IL - Synth Mix F-26-0003 F-26-0004.docx": "il_names_several_formulas"})
        self.assertEqual(out["formula_source"], "not_served")
        self.assertTrue({"formula_source_not_served", "formula_lines_not_readable"}
                        <= {g["code"] for g in out["gaps"]})
        self.assertEqual([(m["code"], m["formula_code"]) for m in out["mix_ups"]],
                         [("several_il_files_for_one_formula", "F-26-0002")])
        formulas = [
            {"code": self.cited("F-26-0001", 1), "product_name": self.cited("Synth Baby Wash", 1)},
            {"code": self.cited("f-26-0002", 2), "product_name": self.cited("Synth Tonic", 2),
             "legacy_number": self.cited("654321", 2)},
            {"code": self.cited("F-26-0005", 5), "product_name": self.cited("synth  tonic", 5)},
            {"code": self.cited("F-26-0006", 6), "product_name": self.cited("Synth Gel", 6)},
            {"code": self.cited("F-26-0006", 7), "product_name": self.cited("Synth Gel Plus", 7)},
            {"code": {"value": "F-26-0007"}}]
        out = ingredient_list_versions(self.call, FULL, formulas=formulas)
        by = {il["name"]["value"]["untrusted_text"]: il for il in out["ingredient_lists"]}
        wash = by["IL - Synth Baby Wash F-26-0001.docx"]
        self.assertEqual(wash["status"], "resolved")
        self.assertEqual(wash["version"]["code"]["citation"], "fm://synthetic/formula/1@1")
        tonic = by["IL- Synth Tonic_F-26-0002 (123456).docx"]
        self.assertEqual(tonic["status"], "resolved")
        self.assertIn("legacy_number_disagrees", {g["code"] for g in tonic["gaps"]})
        flags = {m["code"]: m for m in out["mix_ups"]}
        self.assertEqual(flags["name_shared_by_versions"]["formula_codes"],
                         ["F-26-0002", "F-26-0005"])
        self.assertEqual(flags["formula_code_reused"]["formula_code"], "F-26-0006")
        self.assertIn("several_il_files_for_one_formula", flags)
        self.assertIn("formula_refused", {g["code"] for g in out["gaps"]})
        # A reader granted no SharePoint unit reads no IL and is told it saw nothing.
        hidden = ingredient_list_versions(self.call, CHAT_ONLY)
        self.assertEqual((hidden["ingredient_lists"], hidden["mix_ups"]), ([], []))
        self.assertIn("nothing_visible", {g["code"] for g in hidden["gaps"]})


if __name__ == "__main__":
    unittest.main()
