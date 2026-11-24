"""The History tab served from stored items (speed specs 9.3 V2, 8.3 ``history``): on the business
views fixture every purchase order page's history is built once by the tab's own code as the owner
(``history_batch.history_of``'s call: ``object_history`` in its builder mode), loaded by
``record_history.load``, and read by ``served.history``; for each reader (a full one, a restricted
one, one with no grant) every page of the served tab equals every page of the old tab (the same
entries in the same order, the same "some are not shown", the same not-found), a change marked
beyond a reader is withheld from them then and since, a change resting on mail they may not read is
left out and said, a corroborated link whose corroboration they may not read reads as a lead, and
N = 1, 10, 60 changes cost the same statements."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import psycopg

from operating import ontology_tools_history as oth
from operating import ontology_tools_sql as ots
from operating.ontology_tools_identity import ToolError
from operating.pg import business_views as bv
from operating.pg import record_history as rh
from operator_api import history as hist
from operator_api import served
from operator_api.errors import RecordNotFound
from operator_api.serving import object_url
from operator_api.tests import test_served_read as tsr

FULL, OPS, NOBODY = tsr.FULL, tsr.OPS, tsr.NOBODY


def line_of(tools, oid):
    """``history_batch.history_of``'s line for ``oid`` read through ``tools`` as the owner."""
    got = oth.object_history(tools, {"object_id": oid, "history": True, "marks": True}, FULL)
    kind, label = hist._label(got, oid)
    return {"record_id": oid, "type": kind, "label": label,
            "label_marks": [], "events": got["history"]}


class View:
    """The old tab's view of ``actor``: ``plx.object`` as the host answers it."""

    def __init__(self, tools, actor):
        self.tools, self.actor = tools, actor

    def _call(self, name, args):
        assert name == "plx.object"
        try:
            return self.tools._object(args, self.actor)
        except ToolError as exc:
            return {"error": {"code": exc.code}}


class ServedHistory(tsr.Fixture, unittest.TestCase):
    maxDiff = None
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.loaded = None

    def decided(self):
        """The people exclusion decided up to every version (as on the served store): a row
        resting on mail is served or counted from then (``generic_views.UNDECIDED``)."""
        from operating.pg import door
        with psycopg.connect(self.dsn, autocommit=True) as c:
            door.ensure(c, bv.SCHEMA)
            c.execute("DELETE FROM ontology.people_excluded_state")
            c.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, objects, "
                      "handles, covered_ver) SELECT 't', 'now', 0, 0, max(ver) FROM "
                      "ontology.object_versions")
            c.execute(f"UPDATE {bv.SCHEMA}.door_state SET excluded_at = %s",
                      (door.exclusion_stamp(c),))

    def load(self):
        if ServedHistory.loaded is None:
            tools = self.base()
            keys = self.keys()
            with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
                for k in keys:
                    fh.write(json.dumps(line_of(tools, k), default=str) + "\n")
            self.addCleanup(os.unlink, fh.name)
            ServedHistory.loaded = rh.load(self.dsn, [fh.name])
            self.decided()
        return ServedHistory.loaded

    def backend(self, door):
        return SimpleNamespace(doors=SimpleNamespace(get=lambda r: (door, self.dsn, {}),
                                                     decide_now=lambda r: None))

    def old(self, actor, key):
        """Every page of the old tab: (entries as JSON, partial on any page) or 'not_found'. The
        fixture has no mail views, so the served read can read no item-gated mail, file or chat
        (fail closed); the old tab here is read with an item gate that denies them too (what
        the two say for a reader who may read some is the 40 x 6 gate's, on real mail)."""
        tools = self.base()
        tools.item_gate = lambda actor, handle: False
        view, entries, partial, cursor = View(tools, actor), [], False, None
        while True:
            try:
                page = hist.history(view, key, cursor, open_url=object_url)
            except RecordNotFound:
                return "not_found"
            entries += [e.model_dump(mode="json") for e in page.entries]
            partial = partial or page.partial
            if page.next_cursor is None:
                return entries, partial
            cursor = page.next_cursor

    def new(self, door, key):
        entries, partial, cursor = [], False, None
        while True:
            try:
                page = served.history(self.backend(door), object(), key, cursor)
            except RecordNotFound:
                return "not_found"
            entries += [e.model_dump(mode="json") for e in page.entries]
            partial = partial or page.partial
            if page.next_cursor is None:
                return entries, partial
            cursor = page.next_cursor

    def test_every_page_of_the_tab_equals_the_old_tab_for_each_reader(self):
        got = self.load()
        self.assertEqual((got["failed"], got["refused"]), (0, {}))
        self.assertGreaterEqual(got["records"], 60)
        keys = self.keys()
        seen = {}
        for name, actor in (("FULL", FULL), ("OPS", OPS), ("NOBODY", NOBODY)):
            door = self.door(actor)
            for key in keys:
                old, new = self.old(actor, key), self.new(door, key)
                self.assertEqual(new, old, (name, key))
                seen[(name, key)] = new
        full = [v for (n, _), v in seen.items() if n == "FULL"]
        self.assertTrue(all(v != "not_found" and v[0] for v in full))
        self.assertTrue(any(v == "not_found" for (n, _), v in seen.items() if n == "NOBODY"))

    def test_a_change_marked_beyond_the_reader_is_withheld_then_and_since(self):
        """A value marked finance on a later capture: the earlier change of that property is
        withheld from OPS too, and OPS's tab says some are not shown."""
        self.load()
        key = next(k for k in self.keys() if self.new(self.door(OPS), k) != "not_found"
                   and self.new(self.door(OPS), k)[0])
        with psycopg.connect(self.dsn, autocommit=True) as c:
            rows = c.execute(
                f"SELECT seq, payload, col_class FROM {bv.SCHEMA}.page_items WHERE page_kind = "
                "'history' AND page_key = %s AND payload ? 'change' ORDER BY seq", (key,)).fetchall()
            self.assertTrue(rows)
            seq, payload, before = rows[0]
            cc = c.execute(f"SELECT id FROM {bv.SCHEMA}.col_classes WHERE marks = "
                           "jsonb_build_object('payload', %s::text) AND needs = '{}'",
                           (rh.canon(["finance"]),)).fetchone()
            if cc is None:
                from operating.pg import access_classes as ac
                with c.transaction():
                    ac.assign_cols(c, bv.SCHEMA, "SELECT jsonb_build_object('payload', "
                                   f"'{rh.canon(['finance'])}'::text)")
                cc = c.execute(f"SELECT id FROM {bv.SCHEMA}.col_classes WHERE marks = "
                               "jsonb_build_object('payload', %s::text)",
                               (rh.canon(["finance"]),)).fetchone()
            c.execute(f"UPDATE {bv.SCHEMA}.page_items SET col_class = %s, cellcol = 'payload' "
                      "WHERE page_kind = 'history' AND page_key = %s AND seq = %s",
                      (cc[0], key, seq))
            from operating.pg import door, generic_views as gv
            door.recount(c, bv.SCHEMA, "page_items")
            gv.make_views(c, bv.SCHEMA, follow=False)
        try:
            prop = payload["change"]["property"]
            ops = self.new(self.door(OPS), key)
            full = self.new(self.door(FULL), key)
            shown = lambda got: [c["property"] for e in got[0] for c in e["changes"]]
            self.assertIn(prop, shown(full))
            self.assertEqual(shown(ops).count(prop), shown(full).count(prop) - 1)
            self.assertTrue(ops[1])
        finally:
            ServedHistory.loaded = None   # the next test loads the items again

    def test_a_page_costs_one_read_at_1_10_60_changes_and_pages_reach_every_one(self):
        """N = 1, 10, 60 changes on one record: the first page is one read (one transaction of
        fixed statements), shows min(N, 25) entries, and the cursor pages reach all N."""
        self.load()
        key = self.keys()[0]
        door = self.door(FULL)
        real = ots.scoped_read
        try:
            for n in (1, 10, 60):
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    c.execute(f"DELETE FROM {bv.SCHEMA}.page_items WHERE page_kind = 'history' "
                              "AND page_key = %s AND section = 'history'", (key,))
                    for i in range(n):
                        c.execute(
                            f"INSERT INTO {bv.SCHEMA}.page_items (page_kind, page_key, section, "
                            "seq, payload, access_class, col_class, src_handle, handle, as_of, "
                            "code_fp) SELECT page_kind, page_key, 'history', %s, %s::jsonb, "
                            "access_class, 0, src_handle, handle || '|' || %s, as_of, code_fp "
                            f"FROM {bv.SCHEMA}.page_items WHERE page_kind = 'history' AND "
                            "page_key = %s AND section = 'history_head'",
                            (i, json.dumps({"ev": i, "event": {
                                "at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
                                "kind": "removed_from_source", "basis": "source_deleted"}}),
                             str(i), key))
                calls = []

                def counting(*a, **k):
                    calls.append(1)
                    return real(*a, **k)
                with mock.patch.object(ots, "scoped_read", counting):
                    page = served.history(self.backend(door), object(), key)
                self.assertEqual((len(calls), len(page.entries)), (1, min(n, 25)), n)
                every, cursor = [], None
                while True:
                    got = served.history(self.backend(door), object(), key, cursor)
                    every += got.entries
                    if got.next_cursor is None:
                        break
                    cursor = got.next_cursor
                self.assertEqual(len(every), n)
                self.assertEqual([e.at for e in every], sorted((e.at for e in every),
                                                               reverse=True))
        finally:
            ServedHistory.loaded = None


class Shaping(unittest.TestCase):
    """``served.history``'s reading of stored items, per reader, without a store: the chunk read
    is replaced by rows as the door would return them."""

    def run_rows(self, rows, held=(), readable=(), hidden=0):
        def fake(pg, door, work, seconds, **kw):
            return ({"rows": rows, "head": ({"type": "PurchaseOrder", "label": "PO 1"}, [], None, None),
                     "hidden": hidden, "held": set(held)}, set(readable))
        backend = SimpleNamespace(doors=SimpleNamespace(
            get=lambda r: (SimpleNamespace(grant=None), "dsn", {}), decide_now=lambda r: None))
        pool = mock.MagicMock()
        with mock.patch.object(ots, "scoped_read", fake), \
                mock.patch("operating.page_reads.pool_for", return_value=pool):
            return served.history(backend, object(), "PurchaseOrder:x:1")

    @staticmethod
    def link(ev, tier="lead", cites=(), corr=()):
        return {"ev": ev, "event": {"at": "2026-01-02T00:00:00Z", "kind": "link_added", "link": {
            "link_type": "Email.mentions_purchase_order", "direction": "reverse", "tier": tier,
            "basis": "mention", "other": {"object_id": f"Email:m:{ev}", "object_type": "Email",
                                          "display": {"untrusted_text": "Re: PO"}}}},
                "need": {"cites": list(cites), "corr": list(corr)}}

    def test_a_link_resting_on_mail_the_reader_may_not_read_is_left_out_and_said(self):
        rows = [(1, self.link(1, cites=["graph-mail://a/1"]), []),
                (0, self.link(0, cites=["graph-mail://a/2"]), [])]
        page = self.run_rows(rows, readable={"graph-mail://a/2"})
        self.assertEqual([e.link.object_id for e in page.entries], ["Email:m:0"])
        self.assertTrue(page.partial)
        page = self.run_rows(rows, readable={"graph-mail://a/1", "graph-mail://a/2"})
        self.assertEqual(len(page.entries), 2)
        self.assertFalse(page.partial)

    def test_a_corroborated_link_reads_as_a_lead_unless_every_corroboration_is_readable(self):
        corr = [{"c": 7, "cite": "graph-mail://a/9"}]
        rows = [(0, self.link(0, tier="corroborated", corr=corr), [])]
        firm = self.run_rows(rows, held={7}, readable={"graph-mail://a/9"})
        self.assertEqual(firm.entries[0].link.tier, "firm")
        for held, readable in (((), {"graph-mail://a/9"}), ({7}, ())):
            lead = self.run_rows(rows, held=held, readable=readable)
            self.assertEqual(lead.entries[0].link.tier, "suggested")
        # a corroboration no reader reads (not in the store, or not even the owner's): -1
        never = [(0, self.link(0, tier="corroborated", corr=[*corr, {"c": -1, "cite": None}]), [])]
        self.assertEqual(self.run_rows(never, held={7, -1}, readable={"graph-mail://a/9"})
                         .entries[0].link.tier, "suggested")

    def test_a_changed_event_with_every_change_withheld_is_not_listed_but_a_first_record_is(self):
        rows = [(3, {"ev": 1, "change": {"property": "po_number", "before": [], "after": []}}, []),
                (2, None, ["payload"]),
                (1, {"ev": 1, "event": {"at": "2026-01-03T00:00:00Z", "kind": "changed"}}, []),
                (0, {"ev": 0, "event": {"at": "2026-01-01T00:00:00Z", "kind": "first_recorded"}},
                 [])]
        page = self.run_rows(rows)
        self.assertEqual([e.kind for e in page.entries], ["changed", "first_recorded"])
        self.assertTrue(page.partial)
        only = self.run_rows([rows[1], (1, rows[2][1], []), rows[3]])
        self.assertEqual([e.kind for e in only.entries], ["first_recorded"])
        self.assertTrue(only.partial)

    def test_what_even_the_owners_read_left_out_is_said_to_every_reader(self):
        rows = [(0, {"ev": 0, "event": {"at": "2026-01-01T00:00:00Z", "kind": "first_recorded"}},
                 [])]

        def fake(pg, door, work, seconds, **kw):
            return ({"rows": rows, "head": ({"type": "PurchaseOrder", "label": "PO 1",
                                             "partial": True}, [], None, None),
                     "hidden": 0, "held": set()}, set())
        backend = SimpleNamespace(doors=SimpleNamespace(
            get=lambda r: (SimpleNamespace(grant=None), "dsn", {}), decide_now=lambda r: None))
        with mock.patch.object(ots, "scoped_read", fake), \
                mock.patch("operating.page_reads.pool_for", return_value=mock.MagicMock()):
            self.assertTrue(served.history(backend, object(), "PurchaseOrder:x:1").partial)

    def test_a_hidden_count_says_some_are_not_shown_and_an_unknown_kind_fails_loudly(self):
        rows = [(0, {"ev": 0, "event": {"at": "2026-01-01T00:00:00Z", "kind": "first_recorded"}},
                 [])]
        self.assertTrue(self.run_rows(rows, hidden=2).partial)
        with self.assertRaises(RuntimeError):
            self.run_rows([(0, {"ev": 0, "event": {"at": "2026-01-01T00:00:00Z",
                                                   "kind": "renamed"}}, [])])

    def test_a_forged_cursor_is_refused(self):
        for bad in ("x1", "s", "s-1", "s99999999999"):
            with self.assertRaises(ValueError):
                served.history(SimpleNamespace(), object(), "k", bad)


class LoaderClasses(unittest.TestCase):
    def test_a_links_shown_display_text_carries_its_values_markings_as_the_column_class(self):
        link = {"link_type": "Supplier.ordered_from", "direction": "forward", "tier": "verified",
                "basis": "fk", "other": {"object_id": "Supplier:x:1", "object_type": "Supplier",
                                         "display": {"untrusted_text": "ACME"}},
                "need": {"marks": [], "evidence": [], "corr": [],
                         "other": {"id": "Supplier:x:1", "key": "k", "marks": [], "cite": None}}}
        line = {"record_id": "PurchaseOrder:x:1", "type": "PurchaseOrder", "label": "PO",
                "events": [{"at": "2026-01-01T00:00:00Z", "kind": "link_added", "link": link}]}
        plain = rh.items(line)[0][1]
        self.assertIsNone(plain["col"])
        link["need"]["display_marks"] = ["finance"]
        marked = rh.items(line)[0][1]
        self.assertEqual(marked["col"], rh.canon(["finance"]))
        self.assertNotIn("need", marked["payload"]["event"]["link"])


class LoaderRefuses(unittest.TestCase):
    def test_an_unknown_marking_or_change_kind_is_refused_never_read_as_none(self):
        with self.assertRaises(ValueError):
            rh.canon(["no_such_marking"])
        line = {"record_id": "PurchaseOrder:x:1", "type": "PurchaseOrder", "label": "PO",
                "events": [{"at": "2026-01-01T00:00:00Z", "kind": "renamed"}]}
        with self.assertRaises(ValueError):
            rh.items(line)
        line["events"] = [{"at": "2026-01-01T00:00:00Z", "kind": "changed",
                           "changes": [{"property": "p", "before": [], "after": []}]}]
        with self.assertRaises(ValueError):   # a change without its markings: refused
            rh.items(line)


if __name__ == "__main__":
    unittest.main()
