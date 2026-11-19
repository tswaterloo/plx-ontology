"""Autopilot drafts take the person's memory and matching template
(``operator_api.autopilot_context``), record only the ids they used, and show them in the
Autopilot item as "used for this draft" only as far as the reader may read them.

SYNTHETIC fixtures only (``World`` stores, synthetic addresses, in-memory stand-ins for the
memory and template stores); evidence label: isolated-fixture.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from operating import autopilot as ap
from operating import proactive_engine as pe
from operating.tests.test_autopilot import NOW, Fixture, OURS_OID
from operating.tests.test_proactive_threads import people
from operator_api import autopilot_context as ac
from operator_api import memory as mem

KEY = f"synth-tenant:employee:{OURS_OID}"
CLOSING = "All the best,\nSam from Synth"


class FakeMemory:
    """``Memory.core`` as the provider calls it: rows already cleared by the exclusion;
    a row citing a source passes only when ``decide`` opens it."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def core(self, reader, *, decide=None, **_kw):
        self.calls.append(reader.key)
        if reader.key != KEY:
            return []
        out = []
        for row in self.rows:
            handles = [c["handle"] for c in row.get("citations") or ()]
            if handles:
                decided = decide(handles) if decide else {}
                if not all(h in decided and decided[h] is None for h in handles):
                    continue
            out.append(row)
        return out


class FakeTemplates:
    def __init__(self, rows):
        self.rows = rows

    def list(self, reader, *, doc_type=None, roles=None):
        return [r for r in self.rows if r["doc_type"] == doc_type
                and (r["owner"] == reader.key or reader.key in r.get("shared", ()))]


def row(i, kind, text, *, records=(), citations=()):
    return {"id": i, "kind": kind, "text": text, "subject": None, "records": list(records),
            "citations": list(citations)}


def template(i, body, *, doc_type="customer_email", owner=KEY, **extra):
    return {"id": i, "doc_type": doc_type, "body": body, "owner": owner,
            "mine": owner == KEY, "name": f"T{i}", **extra}


WORK = {"summary": "Confirm PO-4512 for Synth customer",
        "draft": {"to": [{"address": "x@synth-customer.example"}], "subject": "RE: New PO",
                  "body": "Hi Pat,\n\nWe confirm PO-4512 for 10 cases.\n\nBest regards,"}}
EVENT = {"object_id": "obj-email-1", "side": "customer", "records": ["obj-order-1"]}


def provider(rows=(), templates=(), key=KEY):
    return ac.DraftContext(FakeMemory(list(rows)), FakeTemplates(list(templates)),
                           keys=lambda oid: key if oid == OURS_OID else None)


class ForDraft(unittest.TestCase):
    def test_memory_about_writing_or_the_records_is_used_and_only_ids_are_recorded(self):
        p = provider([row(1, "instruction", "Keep my replies short and sign with my first name."),
                      row(2, "fact", "Synth customer orders arrive palletised.",
                          records=["obj-order-1"]),
                      row(3, "fact", "The plant closes for the holidays on the 24th."),
                      row(4, "preference", "Weekly report on Mondays.",
                          citations=[{"handle": "mail:h1"}])],
                     [template(9, "Dear {{customer_name}},\n\n{{items}}\n\n" + CLOSING)])
        work, used = p.for_draft(OURS_OID, "customer_po", WORK, EVENT)
        # neither entry changed the draft (no filler to cut; "my first name" is no
        # stated sign-off): both are related, not applied; the template's closing applied
        self.assertEqual(used, {"memory": [], "related": [1, 2], "template": 9})
        self.assertTrue(work["draft"]["body"].endswith("10 cases.\n\n" + CLOSING))
        self.assertNotIn("Best regards", work["draft"]["body"])
        # ids only: no memory text and no template body is carried into the item
        self.assertNotIn("palletised", json.dumps(used))
        self.assertNotIn("palletised", json.dumps(work))
        self.assertEqual(WORK["draft"]["body"].count("Best regards"), 1)   # input untouched

    def test_a_standing_preference_reaches_every_draft_and_shows_applied_when_followed(self):
        """Owner 11-18: "never mention Y to Z" said once holds for every later draft. A
        preference or instruction is given to the model with its id whatever the message
        says; the ids the model says it followed are recorded as applied, only the
        person's own rows read here; the model's list is never kept on the work."""
        p = provider([row(5, "preference", "Never mention pricing to Kim."),
                      row(6, "fact", "The plant closes for the holidays on the 24th.")])
        lines = p.writing(OURS_OID, {"text": "Can you confirm the Croda order?"}, WORK)
        self.assertEqual(lines, ["[pref 5] Never mention pricing to Kim."])
        for followed, applied in (([5], [5]), ([5, 6, 99], [5]), ([], []), (["5"], [])):
            with self.subTest(followed=followed):
                work, used = p.for_draft(OURS_OID, "teams_reply",
                                         {**WORK, "memory_followed": followed}, EVENT)
                self.assertNotIn("memory_followed", work)
                self.assertEqual(used["memory"] if used else [], applied)
                if used:
                    self.assertNotIn(5, used["related"]) if applied else \
                        self.assertIn(5, used["related"])
        # a person with no memory key: nothing read, the model's list still dropped
        work, used = provider([row(5, "preference", "x")], key=None).for_draft(
            OURS_OID, "teams_reply", {**WORK, "memory_followed": [5]}, EVENT)
        self.assertEqual((used, "memory_followed" in work), (None, False))

    def test_memory_the_person_stated_changes_the_draft_and_only_that_is_used(self):
        filler = {**WORK, "draft": {**WORK["draft"], "body": (
            "Hi Pat,\n\nHope you are well.\n\nWe confirm PO-4512 for 10 cases. Let me know "
            "if you have any questions.\n\nBest regards,\nSam")}}
        p = provider([row(1, "instruction", "Keep my email replies short."),
                      row(2, "preference", "Sign off with Cheers, Sam"),
                      row(3, "instruction", "Never write \"we confirm\" in a reply.")],
                     [template(9, "Dear {{customer_name}},\n\nX\n\n" + CLOSING)])
        work, used = p.for_draft(OURS_OID, "customer_po", filler, EVENT)
        body = work["draft"]["body"]
        # the filler is gone, the figure sentence stays word for word (the never-write
        # rule cannot touch a line with a figure), the stated sign-off wins over the
        # template's, and the drafter's "Sam" under its sign-off is not doubled
        self.assertEqual(body, "Hi Pat,\n\nWe confirm PO-4512 for 10 cases.\n\nCheers, Sam")
        self.assertEqual(used, {"memory": [2, 1], "related": [3], "template": None})

    def test_short_never_leaves_a_draft_with_nothing_to_say(self):
        bare = {**WORK, "draft": {**WORK["draft"], "body": "Hi Pat,\n\nThanks!\n\nBest,"}}
        p = provider([row(1, "instruction", "Keep replies brief.")])
        work, used = p.for_draft(OURS_OID, "customer_po", bare, EVENT)
        self.assertEqual(work, bare)
        self.assertEqual(used, {"memory": [], "related": [1], "template": None})

    def test_a_teams_draft_keeps_its_form_but_loses_filler(self):
        chat = {"summary": "Teams ping", "teams_draft": {
            "body": "Done, PO-4512 is booked. Happy to help."}}
        p = provider([row(1, "instruction", "Keep my teams replies short."),
                      row(2, "instruction", "Sign off with Thanks, Sam")])
        work, used = p.for_draft(OURS_OID, "x", chat, EVENT)
        self.assertEqual(work["teams_draft"]["body"], "Done, PO-4512 is booked.")
        self.assertEqual(used, {"memory": [1], "related": [2], "template": None})

    def test_short_keeps_a_figure_line_as_written(self):
        draft = {**WORK, "draft": {**WORK["draft"], "body": (
            "Hi Pat,\n\nWe ship 1.5 pallets on 11.20. Have a great day!\n\nThanks,")}}
        p = provider([row(1, "instruction", "Keep my replies short."),
                      row(2, "instruction", "Sign off with Thanks")])
        work, used = p.for_draft(OURS_OID, "customer_po", draft, EVENT)
        self.assertEqual(work["draft"]["body"],
                         "Hi Pat,\n\nWe ship 1.5 pallets on 11.20.\n\nThanks,")
        self.assertEqual(used, {"memory": [1], "related": [2], "template": None})

    def test_related_and_applied_hook_ids_are_checked(self):
        class P:
            def __init__(self, used):
                self.used = used

            def for_draft(self, *a):
                return WORK, self.used
        for bad in ({"memory": [], "related": ["x"], "template": None},
                    {"memory": [], "related": 3, "template": None}):
            self.assertEqual(ap._with_context(P(bad), OURS_OID, "a", WORK, EVENT), (WORK, None))
        self.assertEqual(ap._with_context(P({"memory": [1], "related": [1, 2], "template": None}),
                                          OURS_OID, "a", WORK, EVENT)[1],
                         {"memory": [1], "related": [2], "template": None})

    def test_a_missing_template_store_still_records_memory(self):
        class Broken:
            def list(self, *a, **k):
                raise RuntimeError('schema "templates" does not exist')
        p = ac.DraftContext(FakeMemory([row(1, "instruction", "Keep replies short.")]),
                            Broken(), keys=lambda oid: KEY)
        filler = {**WORK, "draft": {**WORK["draft"],
                                    "body": "Hi,\n\nHope you are well. PO-4512 ships.\n\nThanks,"}}
        work, used = p.for_draft(OURS_OID, "customer_po", filler, EVENT)
        self.assertEqual(used, {"memory": [1], "related": [], "template": None})
        self.assertNotIn("Hope", work["draft"]["body"])

    def test_a_content_line_is_never_taken_as_a_template_sign_off(self):
        for closing in ("Let us know if anything changes.", "Thanks,\nWe ship Monday.",
                        "Regards from elsewhere"):
            p = provider([], [template(9, "Dear X,\n\nBody\n\n" + closing)])
            self.assertEqual(p.for_draft(OURS_OID, "customer_po", WORK, EVENT), (WORK, None),
                             closing)
        # a drafter's sign-off with a name under it is replaced whole, never doubled
        named = {**WORK, "draft": {**WORK["draft"], "body": "Hi\n\nBody\n\nThanks,\nSam"}}
        work, _ = provider([], [template(9, "Dear\n\nX\n\n" + CLOSING)]).for_draft(
            OURS_OID, "customer_po", named, EVENT)
        self.assertEqual(work["draft"]["body"], "Hi\n\nBody\n\n" + CLOSING)

    def test_a_cited_memory_entry_is_withheld_offline(self):
        # no read policy decides in the autopilot: a row citing a source never passes
        p = provider([row(4, "instruction", "Reply to Synth in writing only.",
                          citations=[{"handle": "mail:h1"}])])
        self.assertEqual(p.for_draft(OURS_OID, "customer_po", WORK, EVENT), (WORK, None))

    def test_no_reader_key_reads_nothing(self):
        p = provider([row(1, "instruction", "Keep replies short.")], key=None)
        self.assertEqual(p.for_draft(OURS_OID, "customer_po", WORK, EVENT), (WORK, None))
        self.assertEqual(p.memory.calls, [])
        for owner in ("", "a,b", "synth-tenant:employee:x"):
            self.assertEqual(p.for_draft(owner, "customer_po", WORK, EVENT)[1], None)

    def test_a_template_closing_with_a_placeholder_figure_or_address_is_not_used(self):
        for closing in ("Thanks,\n{{sender_name}}", "Call 555 0100", "Sam\nsam@synth.example",
                        "[check: name]"):
            p = provider([], [template(9, "Dear X,\n\nBody\n\n" + closing)])
            self.assertEqual(p.for_draft(OURS_OID, "customer_po", WORK, EVENT), (WORK, None),
                             closing)

    def test_only_the_draft_kind_template_of_the_owner_is_used(self):
        shared = template(8, "Hi\n\nBody\n\nRegards from elsewhere", owner="other",
                          shared=(KEY,))
        supplier = template(7, "Hi\n\nBody\n\nThanks,\nSupplier desk", doc_type="supplier_email")
        p = provider([], [shared, supplier])
        self.assertEqual(p.for_draft(OURS_OID, "customer_po", WORK, EVENT), (WORK, None))
        work, used = p.for_draft(OURS_OID, "x", WORK, {**EVENT, "side": "supplier"})
        self.assertEqual(used, {"memory": [], "related": [], "template": 7})
        self.assertTrue(work["draft"]["body"].endswith("Thanks,\nSupplier desk"))

    def test_a_draft_already_ending_with_the_closing_is_not_counted(self):
        done = {**WORK, "draft": {**WORK["draft"], "body": "Hi\n\nBody\n\n" + CLOSING}}
        p = provider([], [template(9, "Dear\n\nX\n\n" + CLOSING)])
        self.assertEqual(p.for_draft(OURS_OID, "customer_po", done, EVENT), (done, None))

    def test_a_system_write_uses_memory_but_never_a_template(self):
        write = {"summary": "Post receipt for PO-4512", "write": {"system": "bc",
                                                                 "note": "PO-4512 receipt"}}
        p = provider([row(2, "fact", "PO-4512 is received in two lots.",
                          records=["obj-order-1"])],
                     [template(9, "Dear\n\nX\n\n" + CLOSING, doc_type="phrasing")])
        work, used = p.for_draft(OURS_OID, "bc_receipt", write, {"object_id": "obj-order-1"})
        self.assertEqual((work, used), (write, {"memory": [], "related": [2], "template": None}))

    def test_the_real_memory_with_the_exclusion_unavailable_reads_nothing(self):
        def never():
            raise AssertionError("no connection may be opened")
        closed = mem.Memory(never, guard=lambda: None, sync=True, names=lambda: frozenset())
        p = ac.DraftContext(closed, None, keys=lambda oid: KEY)
        self.assertEqual(p.for_draft(OURS_OID, "customer_po", WORK, EVENT), (WORK, None))


class Hook(unittest.TestCase):
    def test_anything_but_ids_from_a_provider_records_nothing(self):
        class P:
            def __init__(self, used, work=WORK):
                self.used, self.work = used, work

            def for_draft(self, *_a):
                return self.work, self.used

        for bad in ({"memory": ["text"], "template": None}, {"memory": [1], "template": "x"},
                    {"memory": [True], "template": None}, {"memory": [0], "template": None},
                    "ids", {"memory": None}):
            self.assertEqual(ap._with_context(P(bad), OURS_OID, "a", WORK, EVENT),
                             (WORK, None), bad)
        self.assertEqual(ap._with_context(P({"memory": [3], "template": 4}), OURS_OID, "a",
                                          WORK, EVENT), (WORK, {"memory": [3], "related": [], "template": 4}))

        class Boom:
            def for_draft(self, *_a):
                raise RuntimeError("down")
        self.assertEqual(ap._with_context(Boom(), OURS_OID, "a", WORK, EVENT), (WORK, None))
        self.assertEqual(ap._with_context(None, OURS_OID, "a", WORK, EVENT), (WORK, None))

    def test_factory_needs_its_own_dsn(self):
        self.assertIsNone(ac.factory({"PLX_PG_DSN": "postgresql:///plx"}))
        self.assertIsNotNone(ac.factory({ac.DSN_ENV: "postgresql:///plx"}))


class Cycle(Fixture):
    def run_live(self, msg, *, queue_dir="autopilot"):
        path = self.dir / "s.sqlite"
        if not path.exists():
            self.w.build(path)
        st = pe._Store(path, threads=1)
        q = ap.Queue(self.dir / queue_dir / ap.QUEUE_FILE)
        try:
            ap.run_cycle(st, q, [msg], people=people(), environ=self.env, now=NOW,
                         landed_at=ap._iso(NOW - timedelta(minutes=2)))
            return q.items()
        finally:
            st.close()
            q.close()

    def test_a_live_draft_records_the_memory_and_template_it_used(self):
        self.order("PO-4512", on_hand="5", need="10")
        msg = self.message(1, "Please find attached our PO PO-4512 for 10 cases.",
                           subject="New PO PO-4512")
        made = []

        def factory():
            made.append(1)
            return provider([row(5, "instruction", "Keep my email replies brief.")],
                            [template(9, "Dear\n\nX\n\n" + CLOSING)])
        with mock.patch.object(ap, "DRAFT_CONTEXT_FACTORY", factory):
            (item,) = self.run_live(msg)
        # "brief" changed nothing in this draft (no filler): related, not applied
        self.assertEqual(item["body"]["context_used"],
                         {"memory": [], "related": [5], "template": 9})
        self.assertTrue(item["body"]["prepared"]["draft"]["body"].endswith(CLOSING))
        self.assertNotIn("brief", json.dumps(item))
        self.assertEqual(made, [1])

    def test_a_replay_queue_never_reads_anyones_memory(self):
        self.order("PO-4512", on_hand="5", need="10")
        msg = self.message(1, "Please find attached our PO PO-4512 for 10 cases.",
                           subject="New PO PO-4512")

        def factory():
            raise AssertionError("a replay made a draft-context provider")
        with mock.patch.object(ap, "DRAFT_CONTEXT_FACTORY", factory):
            (item,) = self.run_live(msg, queue_dir="replay")
        self.assertNotIn("context_used", item["body"])

    def test_cardinality_one_ten_sixty_items_each_recorded_once(self):
        self.order("PO-4512", on_hand="5", need="10")
        for n in (1, 10, 60):
            with self.subTest(n=n):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                self.dir = Path(tmp.name)
                msgs = [self.message(1000 * n + i, f"Our PO PO-{4600 + i} for {i + 1} cases.",
                                     subject=f"New PO PO-{4600 + i}") for i in range(n)]
                calls = []

                class P:
                    def for_draft(self, owner, autopilot, work, event):
                        calls.append(event["object_id"])
                        return work, {"memory": [7], "template": None}
                path = self.dir / "s.sqlite"
                self.w.build(path)
                st = pe._Store(path, threads=1)
                q = ap.Queue(self.dir / "autopilot" / ap.QUEUE_FILE)
                try:
                    with mock.patch.object(ap, "DRAFT_CONTEXT_FACTORY", P):
                        ap.run_cycle(st, q, msgs, people=people(), environ=self.env, now=NOW,
                                     landed_at=ap._iso(NOW - timedelta(minutes=2)))
                    items = q.items()
                finally:
                    st.close()
                    q.close()
                self.assertEqual(len(calls), len(set(calls)))
                self.assertEqual(len(items), len(calls))
                self.assertTrue(all(i["body"]["context_used"] == {"memory": [7], "related": [],
                                                                  "template": None}
                                    for i in items))


class Prepared(unittest.TestCase):
    def test_a_prepared_item_goes_into_its_owners_log_once_without_finance(self):
        store = mem.Memory(lambda: None, guard=lambda: None, sync=True,
                           names=lambda: frozenset())
        logged = []
        store.log_event = lambda reader, **kw: logged.append(kw) or len(logged)
        reader = mem.KeyReader(KEY)
        store.observe("prepared", reader, {"item_id": "i1", "title": "Customer PO: PO-4512",
                                           "happened": {"object_id": "obj-1"}})
        store.observe("prepared", reader, {"item_id": "i2", "title": "Invoice total $4,200"})
        self.assertEqual(len(logged), 1)
        self.assertEqual((logged[0]["kind"], logged[0]["records"], logged[0]["source_ref"]),
                         ("draft", ["obj-1"], "ap:i1:prepared"))


class RelatedPanel(unittest.TestCase):
    """``AutopilotAreaMixin._apa_used``: what the Autopilot item shows as used."""

    def setUp(self):
        from operator_api.autopilot_area import AutopilotAreaMixin

        class Store:
            def __init__(self):
                self.readers = []

            def items(self, reader, decide=None):
                self.readers.append(reader.key)
                return [{"id": 5, "kind": "instruction", "text": "Keep replies brief."}]

        class Backend(AutopilotAreaMixin):
            pass
        self.backend = Backend()
        self.backend.memory = Store()
        self.item = {"mine": True, "body": {"context_used": {"memory": [5, 6],
                                                             "template": 9}}}

    def reader(self, view_as=False):
        return type("R", (), {"key": KEY, "session": type("S", (), {"view_as": view_as})()})()

    def used(self, item, reader=None):
        with mock.patch("operator_api.templates.TemplateStore.for_backend",
                        lambda _b: type("T", (), {"get": lambda _s, _r, i: {
                            "name": "Mine", "doc_type": "customer_email"} if i == 9
                            else None})()):
            return self.backend._apa_used(reader or self.reader(), item, {})

    def test_the_owner_reads_their_entries_and_the_template(self):
        got = self.used(self.item)
        self.assertEqual([(u.id, u.text) for u in got.memory],
                         [(5, "Keep replies brief."), (6, None)])     # 6: deleted since
        self.assertEqual((got.template.id, got.template.name), (9, "Mine"))

    def test_related_entries_are_shown_apart_from_the_applied_ones(self):
        item = {"mine": True, "body": {"context_used": {"memory": [6], "related": [5, 6],
                                                        "template": None}}}
        got = self.used(item)
        self.assertEqual([u.id for u in got.memory], [6])
        self.assertEqual([(u.id, u.text) for u in got.related], [(5, "Keep replies brief.")])
        other = self.used({**item, "mine": False})
        self.assertEqual([(u.id, u.text) for u in other.related], [(5, None)])

    def test_anyone_else_sees_ids_only(self):
        got = self.used({**self.item, "mine": False})
        self.assertEqual([(u.id, u.kind, u.text) for u in got.memory],
                         [(5, None, None), (6, None, None)])

    def test_view_as_reads_neither_store(self):
        got = self.used(self.item, self.reader(view_as=True))
        self.assertEqual([u.text for u in got.memory], [None, None])
        self.assertIsNone(got.template.name)
        self.assertEqual(self.backend.memory.readers, [])

    def test_a_store_failure_or_a_bad_record_shows_ids_or_nothing(self):
        def boom(*_a, **_k):
            raise RuntimeError("down")
        self.backend.memory.items = boom
        self.assertEqual([u.text for u in self.used(self.item).memory], [None, None])
        for body in ({}, {"context_used": "x"}, {"context_used": {"memory": ["a"]}}):
            self.assertIsNone(self.used({"mine": True, "body": body}))


class EnvAllowlist(unittest.TestCase):
    def test_only_the_allowlisted_names_of_plx_env_are_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plx.env"
            path.write_text("MICROSOFT_GRAPH_CLIENT_ID=graph-id\n"
                            "export PLX_EXCLUDED_PEOPLE_OIDS='a,b'\n"
                            "BC_CLIENT_SECRET=never\nPLX_WORKBENCH_API_KEY=never\n"
                            "SOME_NEW_TOKEN=never\nMICROSOFT_GRAPH_TENANT_ID=from-file\n")
            got = ac.allowed_env({ac.ENV_FILE_ENV: str(path),
                                  "MICROSOFT_GRAPH_TENANT_ID": "the-unit-wins"})
            self.assertEqual(got, {"MICROSOFT_GRAPH_CLIENT_ID": "graph-id",
                                   "PLX_EXCLUDED_PEOPLE_OIDS": "a,b"})
            self.assertEqual(ac.allowed_env({ac.ENV_FILE_ENV: str(Path(tmp) / "absent")}), {})
            self.assertEqual(ac.allowed_env({}), {})


if __name__ == "__main__":
    unittest.main()


class DraftLookups(unittest.TestCase):
    """A draft looks facts up as its person (``Lookup``) only when everything that makes
    that safe is there: the readers file names the person with exactly their four claims,
    the session secret, the host and a model home outside every user's login. Anything
    missing or malformed: no session (the draft is the one plain call)."""

    OID = "11111111-1111-4111-8111-111111111111"

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.claims = {"uid": "u1", "email": "vince@plx.example",
                       "entraTenantId": "t1", "entraObjectId": self.OID}
        (self.dir / "host.json").write_text(json.dumps({"tenant": "plx"}))
        (self.dir / "state").mkdir()

    def env(self, readers) -> dict:
        path = self.dir / "readers.json"
        path.write_text(readers if isinstance(readers, str) else json.dumps(readers))
        return {ac.READERS_ENV: str(path), "PLX_WORKBENCH_SESSION_SECRET": "s" * 40,
                ac.HOST_CONFIG_ENV: str(self.dir / "host.json"),
                ac.HOST_STATE_ENV: str(self.dir / "state"),
                "PLX_AUTOPILOT_DRAFT_MODEL": "opus"}

    def test_only_a_person_named_with_exactly_their_claims_gets_a_session(self) -> None:
        good = {self.OID: self.claims}
        self.assertEqual(ac.readers(self.env(good)[ac.READERS_ENV]),
                         {self.OID: self.claims})
        for bad in ({self.OID: {**self.claims, "entraObjectId": "someone-else"}},
                    {self.OID: {**self.claims, "extra": "x"}},
                    {self.OID: {**self.claims, "email": ""}},
                    {self.OID: {k: v for k, v in self.claims.items() if k != "uid"}},
                    [self.claims], "{not json"):
            with self.subTest(bad=bad):
                self.assertEqual(ac.readers(self.env(bad)[ac.READERS_ENV]), {})
        self.assertEqual(ac.readers(self.dir / "missing.json"), {})

    def test_no_session_without_secret_readers_host_or_a_safe_home(self) -> None:
        env = self.env({self.OID: self.claims})
        self.assertIsNotNone(ac.lookup_factory(env))
        for gone in (ac.READERS_ENV, "PLX_WORKBENCH_SESSION_SECRET"):
            with self.subTest(gone=gone):
                self.assertIsNone(ac.lookup_factory({k: v for k, v in env.items()
                                                     if k != gone}))
        started = []
        with mock.patch("operating.agent_session.ReaderHost",
                        lambda *a, **k: started.append(a) or object()):
            self.assertIsNone(ac.Lookup(env).run("someone-not-listed", "x"))
            for change in ({ac.HOST_CONFIG_ENV: str(self.dir / "nope.json")},
                           {ac.HOST_STATE_ENV: str(self.dir / "nope")},
                           {"PLX_AUTOPILOT_DRAFT_MODEL": ""},
                           {"PLX_CHAT_ASK_CLAUDE_HOME": str(Path.home() / ".claude-x")}):
                with self.subTest(change=change):
                    self.assertIsNone(ac.Lookup({**env, **change}).run(self.OID, "x"))
        self.assertEqual(started, [])

    def test_the_session_runs_as_the_person_on_the_autopilots_own_home(self) -> None:
        from operating import agent_session
        env = self.env({self.OID: self.claims})
        seen = {}

        class Host:
            def __init__(self, state, config, claims, secret, **kw):
                seen.update(claims=claims, agent=kw.get("agent"))

            def close(self):
                seen["closed"] = True

        class Agent:
            def __init__(self, **kw):
                seen["spawn"] = kw["spawn"]

            def run(self, prompt, host):
                return type("Ran", (), {
                    "final_text": '{"kind": "reply", "reply": "ok"}', "usage": {},
                    "events": [{"type": "tool_call", "name": "plx.sql", "result": "row 1"},
                               {"type": "tool_call", "name": "plx.sql", "is_error": True,
                                "result": "refused"},
                               {"type": "tool_call", "name": "plx.other", "result": "x"}]})()

        with mock.patch.object(agent_session, "ReaderHost", Host), \
                mock.patch.object(agent_session, "ClaudeCodeAgent", Agent):
            got = ac.Lookup(env).run(self.OID.upper(), "draft this")
        self.assertEqual(got, ('{"kind": "reply", "reply": "ok"}', ["row 1"]))
        self.assertEqual((seen["claims"], seen["agent"], seen.get("closed")),
                         (self.claims, "plx.agent.ask", True))
        # the model process runs on the autopilot's home, never the user's login
        ran = []
        homed = ac._homed(lambda argv, cwd, env, timeout, **kw: ran.append(env), "/srv/h")
        homed(["claude"], "/", {"HOME": "/home/x", "A": "1"}, 5)
        self.assertEqual(ran, [{"HOME": "/srv/h", "A": "1",
                                "CLAUDE_CONFIG_DIR": "/srv/h/.claude"}])
