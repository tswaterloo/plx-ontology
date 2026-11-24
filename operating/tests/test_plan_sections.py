"""The stored plan sections (``operating.pg.plan_sections``) through the page door, on the business
views fixture (``test_page_sql``): an answer is stored with the classes of what it cites and read
back only by a reader who holds all of them (one lacking the order's class, or one cited record's,
reads no row), an answer citing something the table cannot decide is not stored, a failed or
repeated run is not stored, no row is lost or repeated at N = 1, 10 and 60, the AI's schema holds
no such view, a cited excluded person's record makes the row the owner's alone, and an unknown
read fails closed."""
from __future__ import annotations

import json
import unittest
from unittest import mock

import psycopg

from operating import page_reads as pr
from operating.pg import business_views as bv
from operating.pg import plan_sections as ps
from operating.pg import record_pages as rp
from operating.tests import test_ontology_tools_table as tab
from operator_api.tests import test_page_sql as tps

FULL, OPS, NOBODY = tab.FULL, tab.OPS, tab.NOBODY


OK_RUN = {"status": "ok", "reasons": []}
# what a run that cites nothing still opened (it read at least its record): a record every reader of
# the fixture may read, set by ``Door.setUp``
DEFAULT_OPENED: list = []


def _opened(cites, opened):
    return list(opened) if opened is not None else (list(cites) or list(DEFAULT_OPENED))


def entry(record_id, cites=(), state="late", family="late_po", opened=None):
    return {"record_id": record_id, "family": family, "run": OK_RUN,
            "opened": _opened(cites, opened), "verdict": {
        "state": state, "text": f"{state} (the plan's words)",
        "facts": [{"text": f"fact {n}", "citation": h, "openable": True}
                  for n, h in enumerate(cites)]}}


def people(record_id, cites, roles="approver, sender", read=None, opened=None):
    """The plan's own entries naming each cited person; ``read`` is every handle the whole run
    cited (the runner's ``read``; by default the people's own)."""
    return {"record_id": record_id, "family": "people_on_record", "run": OK_RUN,
            "opened": _opened(cites, opened),
            "read": list(cites) if read is None else list(read), "entries": [
        {"kind": "computed", "name": f"person_{n}", "value": {"untrusted_text": f"Person {n}",
                                                              "origin": {"citation": h}},
         "steps": [f"roles: {roles}", "other step"]} for n, h in enumerate(cites)]}


def landed(record_id, cites=(), answerability="answerable", missing=(), opened=None):
    """The plan's landed-cost run as the runner extracts it: a total and one line per citation."""
    entries = [{"name": f"landed_cost_line_{n}", "kind": "computed", "value": "10.00",
                "currency": "CAD", "citation": h} for n, h in enumerate(cites, 1)]
    entries.append({"name": "landed_cost", "kind": "computed", "value": f"{10 * len(cites)}.00",
                    "currency": "CAD", "inputs": [e["name"] for e in entries]})
    return {"record_id": record_id, "family": "landed_cost", "run": OK_RUN,
            "opened": _opened(cites, opened), "landed": {
        "entries": entries, "answerability": answerability, "missing": list(missing)}}


def trail(record_id, cites=(), opened=None, **extra):
    """A raw purchase order trail as ``evidence_trail.po_trail`` returns it, one step per cite."""
    steps = [{"kind": "due_date", "at": None, "title": f"Requested by 2026-0{n}-01", "handle": h,
              "check": "read_from_record"} for n, h in enumerate(cites, 1)]
    raw = {"subject": record_id, "label": None, "state": "late", "late": True, "days_late": 3,
           "steps": steps, "hidden": 0, "may_hide": False, "gaps": [], **extra}
    return {"record_id": record_id, "family": "po_trail", "trail": raw,
            "opened": _opened(cites, opened)}


class Door(unittest.TestCase):
    """The fixture's door and the helpers both classes read it with."""
    @classmethod
    def setUpClass(cls):
        tps.PageDoor.setUpClass.__func__(cls)
        rp.build(cls.dsn)

    base = tps.PageDoor.base
    truth = tps.PageDoor.truth
    page = tps.PageDoor.page

    def setUp(self):
        # every test starts from the same two orders and the records they may cite
        rows = self.truth(
            "SELECT i.page_key, i.access_class FROM {biz}.page_items i WHERE "
            "i.page_kind = 'record' AND i.section = 'header' ORDER BY 1")
        self.orders = [r[0] for r in rows]
        self.cls = dict(rows)
        recs = self.truth("SELECT handle, access_class, record_id FROM {biz}.records WHERE "
                          "type IN ('Batch', 'Supplier', 'Product') ORDER BY handle")
        self.cites = {r[1]: [] for r in recs}
        for h, c, rid in recs:
            self.cites[c].append((h, rid))
        if not type(self).__dict__.get("_default_opened"):
            # a record every reader of the fixture may read that no test cites or excludes: not the
            # one ``pick(True)`` gives (the second such record)
            first = self.pick(True)
            mine = [h for cls, items in sorted(self.cites.items()) for h, rid in items
                    if h != first and self.readable_by(OPS, h) and self.readable_by(FULL, h)]
            type(self)._default_opened = [mine[0] if mine else first]
        DEFAULT_OPENED[:] = type(self)._default_opened

    def marks(self):
        """The watermarks a runner reads at its start."""
        with psycopg.connect(self.dsn) as c:
            return {"ver": str(c.execute(ps.VER_SQL).fetchone()[0]),
                    "link": str(c.execute(ps.LINK_SQL).fetchone()[0]),
                    "xmin": str(c.execute(ps.XMIN_SQL).fetchone()[0])}

    def load(self, entries, **stamps):
        return ps.load(self.dsn, entries, stamps={"purchase_orders": "t", **self.marks(), **stamps})

    def put(self, entries, **stamps):
        return ps.upsert(self.dsn, [({"purchase_orders": "t", **self.marks(), **stamps}, entries)])

    def deps(self, record_id, section="late"):
        return self.truth("SELECT dep_ids, link_ids, dep_ver, dep_link FROM {biz}.plan_section_items "
                          "WHERE record_id = %s AND section = %s", (record_id, section))[0]

    def moved(self, kind, a, b=None):
        """A later object version of ``a`` (kind 'ver') or a new link row ``a`` -> ``b`` ('link');
        returns what to delete again."""
        with psycopg.connect(self.dsn, autocommit=True) as c:
            if kind == "ver":
                ver = c.execute(ps.VER_SQL).fetchone()[0] + 1
                c.execute("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, digest, "
                          "lineage, markings, recorded_at) VALUES (%s, %s, 'zz://moved', 'd', '[]', "
                          "'[]', '2026-11-23')", (ver, a))
            else:
                c.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                          "to_object, tier, binding_kind, why_not, markings, recorded_at, reason, "
                          "evidence_handles) VALUES ('zz-moved-' || %s, 'ZZLink', %s, %s, "
                          "'verified', 'test', '', '[]', '2026-11-23', 'test', '[]')",
                          (a, a, b))
        self.addCleanup(self.unmove)

    def unmove(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("DELETE FROM ontology.object_versions WHERE l1_handle = 'zz://moved'")
            c.execute("DELETE FROM ontology.link_rows WHERE link_id LIKE 'zz-moved-%'")

    def read(self, record_id, actor, section="late"):
        """The rows the reader gets that are current (nothing they depend on has moved)."""
        return [r for r in self.read_any(record_id, actor, section) if r["current"]]

    def read_any(self, record_id, actor, section="late"):
        out = self.page({"reads": [{"name": "plan_section", "params": {
            "record_id": record_id, "section": section}}]}, actor)
        self.assertNotIn("error", out, out)
        got = out["reads"]["plan_section"]
        return [dict(zip(got["columns"], r)) for r in got["rows"]]

    def readable_by(self, actor, handle):
        out = self.plx_sql(f"SELECT count(*) FROM records WHERE handle = '{handle}'", actor)
        return out["rows"][0]["cells"][0] == "1"

    plx_sql = tps.PageDoor.plx_sql

    def pick(self, may):
        """A cited record (its handle) the OPS reader may read (``may``) or may not."""
        for cls, items in sorted(self.cites.items()):
            for h, rid in items:
                if self.readable_by(OPS, h) == may and self.readable_by(FULL, h):
                    return h
        self.skipTest("fixture has no such record")

    def an_order_ops_reads(self):
        for rid in self.orders:
            if self.page({"reads": [{"name": "record_page", "params": {
                    "record_id": rid, "limit": 5}}]}, OPS)["reads"]["record_page"]["rows"]:
                return rid
        self.skipTest("fixture has no order OPS reads")


class PlanSections(Door):
    def test_a_stored_answer_is_read_only_by_a_reader_holding_every_class_it_cites(self):
        order = self.an_order_ops_reads()
        mine, theirs = self.pick(True), self.pick(False)
        got = self.load([entry(order, [mine]), entry(self.orders[-1], [theirs])])
        self.assertEqual((got["stored"], got["skipped"]), (2, 0))
        row = self.read(order, FULL)
        self.assertEqual(len(row), 1)
        self.assertEqual(json.loads(row[0]["payload_text"])["facts"],
                         [{"text": "fact 0", "citation": mine}])
        self.assertEqual(len(self.read(order, OPS)), 1)          # holds the order and its cite
        self.assertEqual(self.read(order, NOBODY), [])           # no class at all
        # an order OPS holds, answered from a record OPS may not read: no row for OPS
        again = self.load([entry(order, [theirs])])
        self.assertEqual(again["stored"], 1)
        self.assertEqual(len(self.read(order, FULL)), 1)
        self.assertEqual(self.read(order, OPS), [])

    def test_what_cannot_be_decided_or_did_not_run_is_not_stored(self):
        h = self.pick(True)
        order = self.an_order_ops_reads()
        entries = [
            entry(order, [h, "sharepoint://site/unknown@1"]),    # a source it cannot class
            entry(self.orders[1], [h], family="landed_cost"),     # a family not stored yet
            {"record_id": self.orders[2], "family": "late_po"},   # a run that gave no verdict
            {"family": "late_po", "verdict": {"state": "late"}},  # no record
            entry(self.orders[3], [h]), entry(self.orders[3], [h]),   # one answer per record
            entry("PurchaseOrder:portal.app_purchase_order:nope", [h]),   # no page header
            {**entry(self.orders[4], []), "run": {          # the engine said it did not run
                "status": "failed", "reasons": ["result_too_large"]}},
            {**entry(self.orders[5], []), "run": {          # a read failed mid-run
                "status": "failed", "reasons": ["root_not_found", "step_failed"]}}]
        got = self.load(entries)
        # nothing is dropped silently: a run with no complete answer is stored as a typed "no
        # answer" (the page says "not run" and why, and never runs the plan itself); only a line
        # naming no record or no stored family, a second answer and a record with no page go
        self.assertEqual((got["stored"], got["skipped"]), (5, 4))
        self.assertEqual(len(self.read(self.orders[3], FULL)), 1)
        self.assertNotIn("no_answer", self.read(self.orders[3], FULL)[0]["payload_text"])
        # a citation that cannot be classed is a bound (typed "no answer"); a run that failed (no
        # verdict, "did not run", a failed read) is stored as "failed" with its exact reason
        rows = self.read(order, FULL, "late")
        self.assertEqual(len(rows), 1)
        self.assertIn("no_answer", json.loads(rows[0]["payload_text"]))
        for none, why in ((self.orders[1], "the run returned no entries"),
                          *(((self.orders[2], "the run returned no verdict"),)
                            if self.orders[2] != order else ()),   # (else a second line: skipped)
                          (self.orders[4], "the plan's run failed: result_too_large"),
                          (self.orders[5], "the plan's run failed: root_not_found, step_failed")):
            rows = self.read(none, FULL, "late" if none != self.orders[1] else "landed")
            self.assertEqual(len(rows), 1, none)
            payload = json.loads(rows[0]["payload_text"])
            self.assertEqual(payload.get("failed"), why, none)
            self.assertNotIn("no_answer", payload)

    def test_an_order_restricted_after_the_build_hides_its_old_row_at_once_and_a_restored_one_shows_again(self):
        order = self.an_order_ops_reads()
        self.load([entry(order, [])])
        self.assertEqual(len(self.read_any(order, OPS)), 1)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            cls = c.execute(f"SELECT access_class FROM {bv.SCHEMA}.page_items WHERE "
                            "page_kind = 'record' AND page_key = %s AND section = 'header'", (order,)).fetchone()[0]
            other = c.execute(f"SELECT access_class FROM {bv.SCHEMA}.records WHERE access_class "
                              "<> %s AND NOT %s = ANY(ARRAY[access_class]) LIMIT 1", (cls, -1)).fetchone()[0]
            c.execute(f"UPDATE {bv.SCHEMA}.page_items SET access_class = %s WHERE "
                      "page_kind = 'record' AND page_key = %s AND section = 'header'", (other, order))
        # the order's class moved after the row was built: no reader gets the old row
        self.assertEqual(self.read_any(order, OPS), [])
        self.assertEqual(self.read_any(order, FULL), [])
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.page_items SET access_class = %s WHERE "
                      "page_kind = 'record' AND page_key = %s AND section = 'header'", (cls, order))
            c.execute(f"DELETE FROM {bv.SCHEMA}.page_items WHERE page_kind = 'record' AND page_key = %s AND "
                      "section = 'header'", (order,))
        self.assertEqual(self.read_any(order, FULL), [])      # no header any more: nothing shown
        from operating.pg import record_pages
        record_pages.build(self.dsn)    # the fixture's own items back

    def test_a_cited_record_that_moved_on_or_was_tightened_hides_the_row_at_once(self):
        order, h = self.an_order_ops_reads(), self.pick(True)
        self.load([entry(order, [h])])
        self.assertEqual(len(self.read(order, OPS)), 1)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            cls = c.execute(f"SELECT access_class FROM {bv.SCHEMA}.records WHERE handle = %s",
                            (h,)).fetchone()[0]
            other = c.execute(f"SELECT access_class FROM {bv.SCHEMA}.records WHERE handle = %s",
                              (self.pick(False),)).fetchone()[0]
            # tightened: the record now carries a class this reader does not hold
            c.execute(f"UPDATE {bv.SCHEMA}.records SET access_class = %s WHERE handle = %s",
                      (other, h))
        self.assertEqual(self.read(order, OPS), [])
        self.assertEqual(len(self.read(order, FULL)), 1)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.records SET access_class = %s WHERE handle = %s", (cls, h))
            c.execute(f"UPDATE {bv.SCHEMA}.records SET handle = handle || 'x' WHERE handle = %s", (h,))
        self.assertEqual(self.read(order, FULL), [])    # a new version: the cited one is gone
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.records SET handle = %s WHERE handle = %s", (h, h + "x"))
        self.assertEqual(len(self.read(order, OPS)), 1)

    def test_the_rows_record_the_order_views_and_the_watermarks_of_their_runs_start(self):
        marks = self.marks()
        ps.load(self.dsn, [entry(self.orders[0], [])], stamps={
            "purchase_orders": "a", "purchase_order_lines": "b", **marks})
        self.assertEqual(self.read(self.orders[0], FULL)[0]["src_stamp"], f"a|b|{marks['ver']}")
        deps, links, dep_ver, dep_link = self.deps(self.orders[0])
        self.assertEqual((dep_ver, dep_link), (int(marks["ver"]), int(marks["link"])))
        self.assertIn(self.orders[0], deps)            # the order itself is a dependency
        self.assertTrue(set(links) <= set(deps))

    def test_a_run_with_no_watermark_is_never_served(self):
        for stamps in ({"ver": "x", "link": "5"}, {"ver": "5"}, {"link": "5"}, {"ver": "-3", "link": "1"}):
            ps.load(self.dsn, [entry(self.orders[0], [])], stamps=stamps)
            self.assertEqual(self.read(self.orders[0], FULL), [], stamps)

    def test_a_row_whose_dependencies_moved_is_still_read_by_its_readers_and_marked_not_current(self):
        order = self.orders[0]
        self.load([entry(order, [])])
        self.assertEqual([r["current"] for r in self.read_any(order, FULL)], [True])
        deps, _, _, _ = self.deps(order)
        self.moved("ver", sorted(deps)[0])
        got = self.read_any(order, FULL)
        self.assertEqual([r["current"] for r in got], [False])      # old, and says so
        self.assertEqual(self.read(order, FULL), [])
        # the class rules still apply to an old row, and a row with no watermarks is never read
        self.assertEqual(self.read_any(order, NOBODY), [])
        ps.load(self.dsn, [entry(order, [])], stamps={"ver": "x", "link": "5"})
        self.assertEqual(self.read_any(order, FULL), [])

    def test_a_trail_that_lost_a_read_is_not_stored_and_one_resting_on_a_stale_capture_is_not_current(self):
        order = self.orders[0]
        self.assertIsNone(ps.item(trail(order, [], gaps=[{"reason": "tool_error", "what": "x"}])))
        self.assertIsNotNone(ps.item(trail(order, [], gaps=[{"reason": "dates_disagree"}])))
        self.load([trail(order, [], stale_sources=["portal_api"])])
        self.assertEqual([r["current"] for r in self.read_any(order, FULL, "trail")], [False])
        self.load([trail(order, [], stale_sources=[])])
        self.assertEqual([r["current"] for r in self.read_any(order, FULL, "trail")], [True])

    def test_a_row_is_served_until_something_it_depends_on_moves(self):
        order, other = self.orders[0], self.orders[1]
        self.load([entry(order, []), entry(other, [])])
        self.assertEqual((len(self.read(order, FULL)), len(self.read(other, FULL))), (1, 1))
        deps, links, _, _ = self.deps(order)
        stray = "Unrelated:zz:{\"id\":1}"
        # an unrelated object's new version and a link between two unrelated objects: still served
        self.moved("ver", stray)
        self.moved("link", stray, stray + "2")
        self.assertEqual(len(self.read(order, FULL)), 1)
        # a new version of a cited, line or member object (any of its dependencies): not served;
        # a row that does not depend on it stays
        mine = set(deps) - set(self.deps(other)[0])
        self.assertTrue(mine, "the two fixture orders share every dependency")
        self.moved("ver", sorted(mine)[0])
        self.assertEqual(self.read(order, FULL), [])
        self.assertEqual(len(self.read(other, FULL)), 1)

    def test_a_new_link_on_an_existing_object_ends_the_row_and_a_reload_serves_it_again(self):
        order = self.orders[0]
        self.load([entry(order, [])])
        deps, links, _, _ = self.deps(order)
        self.assertTrue(links, "no non-hub dependency")
        self.moved("link", links[0], "Receipt:zz:{\"id\":9}")     # a receipt linked LATER
        self.assertEqual(self.read(order, FULL), [])
        self.load([entry(order, [])])                              # the next run starts after it
        self.assertEqual(len(self.read(order, FULL)), 1)
        # the link towards the object counts too
        self.moved("link", "Receipt:zz:{\"id\":8}", links[0])
        self.assertEqual(self.read(order, FULL), [])

    def test_a_cited_mails_new_version_ends_the_row(self):
        order, mine = self.an_order_ops_reads(), self.pick(True)
        self.load([entry(order, [mine])])
        self.assertEqual(len(self.read(order, FULL)), 1)
        rid = next(rid for items in self.cites.values() for hh, rid in items if hh == mine)
        self.assertIn(rid, self.deps(order)[0])
        self.moved("ver", rid)
        self.assertEqual(self.read(order, FULL), [])

    def test_an_answer_of_any_size_is_stored_whole(self):
        # guardrail 1: no size at which a real answer becomes "no answer": 300 people, 300 landed
        # lines, a 1 MB fact text and a trail of 600 KB come back whole, every handle cited
        order = self.orders[0]
        hs = [f"portal://h/{n}" for n in range(300)]
        got = ps.item(people(order, hs))
        self.assertEqual((len(got[2]["entries"]), len(got[3])), (300, 300))
        lan = ps.item(landed(order, hs))
        self.assertEqual(len(lan[2]["entries"]), 301)
        late = ps.item({**entry(order, hs[:1]), "verdict": {
            "state": "late", "text": "t", "facts": [{"text": "x" * 10000, "citation": hs[0],
                                                     "openable": True}]}})
        self.assertEqual(len(late[2]["facts"][0]["text"]), 10000)
        big = trail(order, hs[:250])
        big["trail"]["steps"][0]["title"] = "y" * 600000
        self.assertEqual(len(ps.item(big)[3]), 250)
        deep = {"handle": "portal://h/deep"}
        for _ in range(40):
            deep = {"child": deep}
        self.assertIn("portal://h/deep", ps.item(trail(order, [], extra=deep))[3])
        for fam, line in (("people", people(order, hs)), ("landed", landed(order, hs)),
                          ("trail", big)):
            self.assertEqual(ps.outcome(line), ("answer", ""), fam)

    def test_the_verdicts_words_never_decide_a_run_failed_only_its_structured_status_does(self):
        order = self.orders[0]
        words = {"state": "not_established", "facts": [],
                 "text": "Not established: no such record, step failed"}
        self.assertEqual(ps.outcome({**entry(order, []), "verdict": words}), ("answer", ""))
        self.assertEqual(ps.outcome({**entry(order, []), "verdict": words,
                                     "run": {"status": "failed", "reasons": ["step_failed"]}}),
                         ("failed", "the plan's run failed: step_failed"))
        # a line that states no status (an older runner) fails closed
        old = {k: v for k, v in entry(order, []).items() if k != "run"}
        self.assertEqual(ps.outcome(old), ("failed", "the line states no run status"))
        self.assertEqual(ps.outcome({**entry(order, []), "run": {"status": "maybe"}})[0], "failed")
        # the run's status from the plan's own structure
        ok = {"investigation": {"answer": [], "answerability": {"state": "partial",
                                                                  "reasons": ["no_confirmation"]}}}
        self.assertEqual(ps.run_status(ok), {"status": "ok", "reasons": []})
        for bad, want in (
                ({"investigation": {"error": {"code": "tool_timeout"}}}, ["tool_timeout"]),
                ({"investigation": {"answer": [], "answerability": {"reasons": [
                    "root_not_found", "step_failed", "no_evidence_found"]}}},
                 ["root_not_found", "step_failed"]),
                ({"investigation": {"answer": [], "gaps": [{"reason": "engine_failed"}]}},
                 ["engine_failed"]),
                ({"investigation": {"answer": "x"}}, ["no_answer_list"]),
                ({}, ["no_investigation"]), (None, ["no_investigation"])):
            self.assertEqual(ps.run_status(bad), {"status": "failed", "reasons": want})

    def test_a_hub_objects_new_link_is_seen_through_its_mark_and_never_passed_unmarked_or_stale(self):
        order = self.orders[0]
        with mock.patch.object(ps, "HUB_LINKS", -1):          # every object counts as a hub
            self.load([entry(order, [])])                     # (load runs a marks round)
        deps, links, _, _ = self.deps(order)
        self.assertEqual(links, [])
        hubs = self.truth("SELECT hub_ids FROM {biz}.plan_section_items")[0][0]
        self.assertEqual(sorted(hubs), sorted(deps))
        self.assertEqual(len(self.read(order, FULL)), 1)
        # a new link on a hub: seen once the marks round has read it
        self.moved("link", deps[0], "Receipt:zz:{\"id\":7}")
        ps.mark_links(self.dsn)
        self.assertEqual(self.read(order, FULL), [])
        # reloaded after it: served again (the run's watermark is past the mark)
        with mock.patch.object(ps, "HUB_LINKS", -1):
            self.load([entry(order, [])])
        self.assertEqual(len(self.read(order, FULL)), 1)
        # a link on a hub numbered BELOW the row's watermark but written by a later transaction (a
        # late commit): the mark's newest number does not move, its transaction horizon does
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, "
                      "tier, binding_kind, why_not, markings, recorded_at, reason, evidence_handles, "
                      "row_no) VALUES ('zz-moved-late-hub', 'ZZLink', %s, 'X:late', 'verified', "
                      "'test', '', '[]', '2026-11-23', 'test', '[]', -9)", (deps[0],))
        ps.mark_links(self.dsn)
        self.assertEqual(self.read(order, FULL), [])
        with mock.patch.object(ps, "HUB_LINKS", -1):
            self.load([entry(order, [])])
        self.assertEqual(len(self.read(order, FULL)), 1)
        # marks not renewed for MARKS_FRESH_S: hub rows are not served (a stopped puller)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.{ps.MARKS_STATE} SET at = now() - interval '5 minutes'")
        self.assertEqual(self.read(order, FULL), [])
        ps.mark_links(self.dsn)
        self.assertEqual(len(self.read(order, FULL)), 1)
        # a hub with no mark at all is not passed
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"DELETE FROM {bv.SCHEMA}.{ps.MARKS} WHERE object_id = %s", (deps[0],))
        self.assertEqual(self.read(order, FULL), [])
        ps.mark_links(self.dsn)                               # tracked again
        self.assertEqual(len(self.read(order, FULL)), 1)
        # a mark that predates the marks' coverage cannot decide a row older than it
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.{ps.MARKS_STATE} SET from_row = from_row + 1000000")
        self.assertEqual(self.read(order, FULL), [])

    def test_an_older_marks_table_without_the_horizon_column_is_upgraded(self):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS zz_marks CASCADE")
            c.execute("CREATE SCHEMA zz_marks")
            c.execute(f"CREATE TABLE zz_marks.{ps.MARKS} (object_id text PRIMARY KEY, "
                      "newest bigint NOT NULL)")
            ps.ensure_marks(c, "zz_marks")
            have = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = "
                "'zz_marks' AND table_name = %s", (ps.MARKS,))}
            c.execute("DROP SCHEMA zz_marks CASCADE")
        self.assertIn("newest_xmin", have)

    def test_a_version_written_by_a_transaction_that_committed_after_the_watermark_counts_as_newer(self):
        order = self.orders[0]
        self.load([entry(order, [])])
        dep = self.deps(order)[0][0]
        # its number is below the runner's watermark (handed out earlier) but it was written by a
        # transaction that began after the runner's horizon: a late commit
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, digest, "
                      "lineage, markings, recorded_at) VALUES (-7, %s, 'zz://moved', 'd', '[]', "
                      "'[]', '2026-11-23')", (dep,))
        self.addCleanup(self.unmove)
        self.assertEqual(self.read(order, FULL), [])
        # and a link row numbered below the watermark, written late, ends a row the same way
        self.load([entry(order, [])])
        self.assertEqual(len(self.read(order, FULL)), 1)
        links = self.deps(order)[1]
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, "
                      "tier, binding_kind, why_not, markings, recorded_at, reason, evidence_handles, "
                      "row_no) VALUES ('zz-moved-late', 'ZZLink', %s, 'X:y', 'verified', 'test', '', "
                      "'[]', '2026-11-23', 'test', '[]', -9)", (links[0],))
        self.assertEqual(self.read(order, FULL), [])

    def test_the_people_answer_is_stored_as_the_plans_entries_and_served_by_the_same_classes(self):
        order, mine, theirs = self.an_order_ops_reads(), self.pick(True), self.pick(False)
        got = self.load([people(order, [mine, mine, mine]), people(self.orders[1], [theirs]),
                         {"record_id": self.orders[2], "family": "people_on_record",
                          "entries": "failed"},
                         people(self.orders[3], [])])
        self.assertEqual((got["stored"], got["skipped"]), (3, 1))
        payload = json.loads(self.read(order, FULL, "people")[0]["payload_text"])
        self.assertEqual(len(payload["entries"]), 3)
        self.assertEqual(payload["entries"][0]["steps"], ["roles: approver, sender"])
        self.assertNotIn("name", payload["entries"][0])            # only what the page reads
        self.assertEqual(len(self.read(order, OPS, "people")), 1)
        self.assertEqual(self.read(order, NOBODY, "people"), [])
        self.assertEqual(len(self.read(self.orders[1], FULL, "people")), 1)
        self.assertEqual(self.read(self.orders[1], OPS, "people"), [])    # cites what OPS may not read
        empty = json.loads(self.read(self.orders[3], FULL, "people")[0]["payload_text"])
        self.assertEqual(empty["entries"], [])                       # nobody named: said, not guessed
        self.assertEqual(self.read(order, FULL, "late"), [])         # a section of its own

    def test_a_person_found_through_evidence_the_reader_may_not_read_is_not_served_to_that_reader(self):
        # who the plan finds depends on everything it read, not only on the entries naming people:
        # the stored row depends on every handle the run cited (staff reader found nobody live)
        order, mine, theirs = self.an_order_ops_reads(), self.pick(True), self.pick(False)
        got = self.load([people(order, [mine], read=[mine, theirs]),
                         {**people(self.orders[1], [mine]), "read": None}])    # no read set: no row
        self.assertEqual((got["stored"], got["skipped"]), (2, 0))
        self.assertEqual(len(self.read(order, FULL, "people")), 1)
        self.assertEqual(self.read(order, OPS, "people"), [])
        said = self.read(self.orders[1], FULL, "people")        # no read set: stored as FAILED
        self.assertEqual(len(said), 1)
        self.assertEqual(json.loads(said[0]["payload_text"])["failed"],
                         "the run returned no entries or no read set")

    def test_every_handle_a_run_opened_is_a_dependency_of_its_row_not_only_the_ones_it_cited(self):
        order, mine, theirs = self.an_order_ops_reads(), self.pick(True), self.pick(False)
        a, b, c = self.orders[1], self.orders[2], self.orders[3]
        got = self.load([entry(order, [mine], opened=[mine, theirs]),
                         landed(a, [mine], opened=[mine, theirs]),
                         trail(b, [mine], opened=[mine, theirs]),
                         people(c, [mine], opened=[mine, theirs])])
        self.assertEqual(got["stored"], 4)
        for oid, section in ((order, "late"), (a, "landed"), (b, "trail"), (c, "people")):
            self.assertEqual(len(self.read(oid, FULL, section)), 1, section)
            self.assertEqual(self.read(oid, OPS, section), [], section)   # OPS may not open what it opened

    def test_a_line_that_states_no_opened_set_is_a_typed_failure_in_every_family(self):
        order, mine = self.an_order_ops_reads(), self.pick(True)
        lines = [entry(order, [mine]), landed(self.orders[1], [mine]), trail(self.orders[2], [mine]),
                 people(self.orders[3], [mine])]
        for ln in lines:
            ln.pop("opened")
            self.assertEqual(ps.outcome(ln), ("failed", "the line states no opened set (every handle the run read)"),
                             ln["family"])
        bad = entry(order, [mine])
        bad["opened"] = [None]
        self.assertEqual(ps.outcome(bad)[0], "failed")

    def comm_tables(self):
        """Rows in the fixture's Teams, SharePoint and meeting views (handle and objects only)."""
        made = {"teams_messages": ["teams-chat://t/1", "teams-chat://t/2"],
                "sharepoint_files": ["sharepoint://s/f@1"], "meetings": ["calendar-event://c/1"]}
        with psycopg.connect(self.dsn, autocommit=True) as c:
            for table, handles in made.items():
                for h in handles:
                    c.execute(f"INSERT INTO {bv.SCHEMA}.{table} (handle, object_ids) VALUES (%s, %s)",
                              (h, [f"obj:{h}"]))
        self.addCleanup(self.drop_comm_rows, made)
        return made

    def drop_comm_rows(self, made):
        with psycopg.connect(self.dsn, autocommit=True) as c:
            for table, handles in made.items():
                c.execute(f"DELETE FROM {bv.SCHEMA}.{table} WHERE handle = ANY(%s)", (handles,))

    def test_an_opened_teams_sharepoint_or_meeting_handle_is_classed_by_its_view_and_the_answer_is_served(self):
        mine = self.pick(True)
        made = self.comm_tables()
        kinds = [("record only", []), ("teams", made["teams_messages"][:1]),
                 ("sharepoint", made["sharepoint_files"]), ("meeting", made["meetings"]),
                 ("all three", [h for hs in made.values() for h in hs]),
                 ("a kind no view holds", ["zz-unknown://x/1"])]
        orders = self.orders[:len(kinds)]
        if len(orders) < len(kinds):
            self.skipTest("fixture has too few orders")

        def served(comm_views):
            with mock.patch.object(ps, "COMM_VIEWS", comm_views):
                self.load([entry(oid, [mine], opened=[mine, *extra]) for oid, (_, extra) in zip(orders, kinds)])
            return [("no_answer" not in json.loads(r[0]["payload_text"])) if (
                r := self.read_any(oid, FULL)) else None for oid in orders]
        before = served(("emails", "attachments"))
        after = served(ps.COMM_VIEWS)
        print(f"served of {len(kinds)} lines opening Teams/SharePoint/meeting handles: "
              f"before {sum(bool(x) for x in before)}, after {sum(bool(x) for x in after)}")
        self.assertEqual(before, [True, False, False, False, False, False])    # the old classes: no_answer
        self.assertEqual(after, [True, True, True, True, True, False])         # an unknown kind still fails closed
        row = self.read(orders[4], FULL)[0]
        self.assertEqual(sorted(h for h in row["comm_handles"] if not h.startswith("zz")),
                         sorted(h for hs in made.values() for h in hs))
        self.assertEqual(self.read(orders[4], NOBODY), [])

    def test_a_line_opening_records_by_object_id_beside_api_handles_is_staged_and_served(self):
        """Live shape: ``_visible_versions`` notes OBJECT ids (``Type:source:{"id":..}``) while the
        cited sources are ``api://`` handles; every opened thing is classed, none dropped."""
        order, mine = self.an_order_ops_reads(), self.pick(True)
        theirs = self.pick(False)
        ids = dict(self.truth("SELECT handle, record_id FROM {biz}.records WHERE handle = ANY(%s)",
                              ([mine, theirs],)))
        self.assertTrue(all(":" in i and not i.startswith("api://") for i in ids.values()), ids)
        lines = [entry(order, [mine], opened=[mine, ids[mine]]),                 # handle + object id
                 landed(self.orders[1], [mine], opened=[ids[mine], ids[theirs]]),  # object ids only
                 trail(self.orders[2], [mine], opened=[mine, ids[theirs]]),
                 people(self.orders[3], [mine], opened=[ids[mine], theirs])]
        got = self.load(lines)
        self.assertEqual(got["stored"], 4)
        for oid, section in ((order, "late"), (self.orders[1], "landed"), (self.orders[2], "trail"),
                             (self.orders[3], "people")):
            rows = self.read_any(oid, FULL, section)
            self.assertEqual(len(rows), 1, section)
            self.assertNotIn("no_answer", json.loads(rows[0]["payload_text"]), section)
        # the row's class check holds the opened records' real handles, never an object id
        row = self.read(order, FULL)[0]
        self.assertEqual(row["cites"], [mine])
        self.assertIn(ids[theirs], self.deps(self.orders[1], "landed")[0])

    def test_a_row_that_opened_a_record_by_object_id_is_hidden_from_a_reader_who_may_not_open_it(self):
        order, mine, theirs = self.an_order_ops_reads(), self.pick(True), self.pick(False)
        ids = dict(self.truth("SELECT handle, record_id FROM {biz}.records WHERE handle = ANY(%s)",
                              ([mine, theirs],)))
        self.load([entry(order, [mine], opened=[ids[mine]]),
                   landed(self.orders[1], [mine], opened=[ids[mine]]),
                   trail(self.orders[2], [mine], opened=[ids[theirs]]),
                   people(self.orders[3], [mine], opened=[ids[mine], ids[theirs]])])
        # OPS reads the order; only the opens of what OPS may open are served to it
        self.assertEqual(len(self.read(order, OPS)), 1)
        self.assertEqual(len(self.read(self.orders[1], OPS, "landed")), 1)
        for oid, section in ((self.orders[2], "trail"), (self.orders[3], "people")):
            self.assertEqual(self.read(oid, OPS, section), [], section)
            self.assertEqual(len(self.read(oid, FULL, section)), 1, section)
        row = self.read(self.orders[3], FULL, "people")[0]
        self.assertEqual(sorted(row["cites"]), sorted([mine, theirs]))

    def test_an_opened_object_id_no_view_knows_still_fails_closed(self):
        order, mine = self.an_order_ops_reads(), self.pick(True)
        self.load([entry(order, [mine], opened=[mine, 'Ghost:portal.nowhere:{"id":404}'])])
        rows = self.read_any(order, FULL)
        self.assertIn("no_answer", json.loads(rows[0]["payload_text"]))

    def test_a_line_that_answered_but_states_an_empty_opened_set_is_a_typed_failure(self):
        order, mine = self.an_order_ops_reads(), self.pick(True)
        for ln in (entry(order, [mine], opened=[]), landed(self.orders[1], [mine], opened=[]),
                   trail(self.orders[2], [mine], opened=[]), people(self.orders[3], [mine], opened=[]),
                   entry(order, [], opened=[])):       # a run that cites nothing read at least its record
            self.assertEqual(ps.outcome(ln), ("failed", "the line states no opened set (every handle the run read)"),
                             ln["family"])
        self.assertEqual(ps.outcome(entry(order, [], opened=[mine]))[0], "answer")

    def test_the_landed_cost_answer_is_served_by_the_classes_of_the_records_its_figures_cite(self):
        order, mine, theirs = self.an_order_ops_reads(), self.pick(True), self.pick(False)
        got = self.load([landed(order, [mine, mine]), landed(self.orders[1], [theirs]),
                         landed(self.orders[2], ["sharepoint://site/unknown@1"]),    # cannot be classed
                         {"record_id": self.orders[3], "family": "landed_cost",
                          "landed": {"entries": "failed"}}])
        # the unclassable and the failed runs are stored as "no answer" too; the one skipped is a
        # second line for an order already answered (the order OPS reads is one of the fixture's)
        self.assertEqual((got["stored"], got["skipped"]), (3, 1))
        payload = json.loads(self.read(order, FULL, "landed")[0]["payload_text"])
        self.assertEqual(payload["answerability"], "answerable")
        self.assertEqual(len(payload["entries"]), 3)
        self.assertEqual(len(self.read(order, OPS, "landed")), 1)
        self.assertEqual(self.read(order, NOBODY, "landed"), [])
        self.assertEqual(len(self.read(self.orders[1], FULL, "landed")), 1)
        self.assertEqual(self.read(self.orders[1], OPS, "landed"), [])     # cites what OPS may not read
        self.assertEqual(self.read(order, FULL, "late"), [])                # a section of its own
        self.assertEqual(self.read(order, FULL, "people"), [])

    def test_the_trail_is_stored_raw_with_every_handle_it_names_cited_and_served_by_class(self):
        order, mine, theirs = self.an_order_ops_reads(), self.pick(True), self.pick(False)
        got = self.load([
            trail(order, [mine], eta={"handle": mine, "state": "x", "text": "t"}),
            trail(self.orders[1], [theirs]),
            trail(self.orders[2], ["sharepoint://site/unknown@1"]),         # cannot be classed
            trail(self.orders[3], [], state=None),                             # nothing found
            {**trail(self.orders[4], [mine]), "trail": {"steps": "x"}},
            trail(self.orders[5], [], gaps=[{"reason": "call_failed"}])])
        # the four with no complete trail are stored as a typed "no answer" (one has no page header)
        self.assertEqual((got["stored"], got["skipped"]), (5, 1))
        kept = [json.loads(r["payload_text"]) for o in self.orders[2:6]
                for r in self.read(o, FULL, "trail")]
        said = [k for k in kept if isinstance(k.get("no_answer"), str)]
        failed = [k["failed"] for k in kept if isinstance(k.get("failed"), str)]
        # a trail that found nothing is "no answer"; a trail with no steps list and one that could
        # not state lateness are FAILED, with the exact reason
        self.assertEqual(len(said), 1, kept)
        self.assertEqual(sorted(failed), ["the run returned no trail",
                                          "the trail's read failed: call_failed"])
        # a trail whose own reads failed or were cut is FAILED, with the gap's name (never "no answer")
        for gap in ("call_failed", "tool_error", "trail_cut", "read_failed"):
            self.assertEqual(ps.outcome(trail(order, [], gaps=[{"reason": gap}])),
                             ("failed", "the trail's read failed: " + gap))
        # a fact about the record is no failure: the call succeeded and states no lateness entry
        # (stored as a typed "no answer" with that reason); a promise chain the records do not
        # complete is part of the answer, shown as the trail states it
        self.assertEqual(ps.outcome(trail(order, [], state=None, gaps=[{"reason": "no_lateness_entry"}])),
                         ("no_answer", "the trail states: no_lateness_entry"))
        self.assertEqual(ps.outcome(trail(order, [], gaps=[{"reason": "chain_incomplete"}])),
                         ("answer", ""))
        self.assertEqual(ps.outcome(trail(order, [], gaps=[{"reason": "chain_incomplete"},
                                                          {"reason": "read_failed"}])),
                         ("failed", "the trail's read failed: read_failed"))
        self.assertEqual(ps.outcome(trail(order, [], state=None)),
                         ("no_answer", "the plan completed and found no answer the table keeps"))
        self.assertTrue(all("raw" not in k for k in said))
        payload = json.loads(self.read(order, FULL, "trail")[0]["payload_text"])
        self.assertEqual(payload["raw"]["days_late"], 3)
        self.assertNotIn("label", payload["raw"])
        self.assertEqual(len(self.read(order, OPS, "trail")), 1)
        self.assertEqual(self.read(order, NOBODY, "trail"), [])
        self.assertEqual(self.read(self.orders[1], OPS, "trail"), [])         # cites what OPS may not read
        self.assertEqual(len(self.read(self.orders[1], FULL, "trail")), 1)
        self.assertEqual(self.read(order, FULL, "late"), [])

    def test_a_not_established_answer_is_stored_without_its_facts(self):
        h = self.pick(True)
        got = self.load([entry(self.orders[0], [h], state="not_established")])
        self.assertEqual(got["stored"], 1)
        payload = json.loads(self.read(self.orders[0], FULL)[0]["payload_text"])
        self.assertEqual((payload["state"], payload["facts"]), ("not_established", []))

    def test_rows_are_neither_lost_nor_repeated_at_1_10_and_60(self):
        h = self.pick(True)
        for n in (1, 10, 60):
            ids = (self.orders * 3)[:n] if n > len(self.orders) else self.orders[:n]
            got = self.load([entry(r, [h]) for r in dict.fromkeys(ids)])
            want = len(dict.fromkeys(ids))
            self.assertEqual(got["stored"], want)
            for r in dict.fromkeys(ids):
                self.assertEqual(len(self.read(r, FULL)), 1, r)
            with psycopg.connect(self.dsn) as c:
                self.assertEqual(c.execute(
                    f"SELECT count(*), count(DISTINCT handle) FROM {bv.SCHEMA}.plan_section_items"
                ).fetchone(), (want, want))
                self.assertEqual(c.execute(
                    f"SELECT count(*) FROM {bv.SCHEMA}.lineage WHERE view = 'plan_section_items'"
                ).fetchone()[0], want)

    def test_an_upsert_replaces_only_the_rows_it_names_and_leaves_every_other_served(self):
        a, b, c = self.orders[0], self.orders[1], self.orders[2]
        self.load([entry(a, [], state="late"), entry(b, [], state="late")])
        self.moved("ver", a)        # a's row is now stale (read live)
        self.assertEqual(self.read(a, FULL), [])
        got = self.put([entry(a, [], state="not_late"), entry(c, [], state="late")])
        self.assertEqual((got["placed"], got["replaced"], got["older"]), (2, 1, 0))
        self.assertEqual(json.loads(self.read(a, FULL)[0]["payload_text"])["state"], "not_late")
        self.assertEqual(json.loads(self.read(b, FULL)[0]["payload_text"])["state"], "late")
        self.assertEqual(len(self.read(c, FULL)), 1)
        with psycopg.connect(self.dsn) as cn:
            self.assertEqual(cn.execute(f"SELECT count(*), count(DISTINCT handle) FROM "
                                        f"{bv.SCHEMA}.plan_section_items").fetchone(), (3, 3))
            self.assertEqual(cn.execute(f"SELECT count(*) FROM {bv.SCHEMA}.lineage WHERE view = "
                                        "'plan_section_items'").fetchone()[0], 3)

    def test_a_late_file_never_replaces_a_row_computed_from_a_newer_state(self):
        a = self.orders[0]
        self.load([entry(a, [], state="late")])
        old = {"ver": str(int(self.marks()["ver"]) - 5), "link": self.marks()["link"]}
        got = self.put([entry(a, [], state="not_late")], **old)
        self.assertEqual((got["placed"], got["older"]), (0, 1))
        self.assertEqual(json.loads(self.read(a, FULL)[0]["payload_text"])["state"], "late")

    def test_an_upserted_row_keeps_every_class_rule_and_the_exclusion(self):
        order, mine, theirs = self.an_order_ops_reads(), self.pick(True), self.pick(False)
        self.load([entry(self.orders[-1], [])])
        self.put([entry(order, [theirs])])
        self.assertEqual(len(self.read(order, FULL)), 1)
        self.assertEqual(self.read(order, OPS), [])         # cites what OPS may not read
        self.assertEqual(self.read(order, NOBODY), [])
        self.put([entry(order, [mine, "sharepoint://site/unknown@1"])])   # undecidable: not put
        self.assertEqual(len(self.read(order, FULL)), 1)    # the stored answer stands
        self.put([entry(order, [mine])])
        self.assertEqual(len(self.read(order, OPS)), 1)

    def test_an_upsert_with_no_stamps_or_an_unbuilt_table_fails_closed(self):
        a = self.orders[0]
        self.load([entry(a, [], state="late")])
        got = ps.upsert(self.dsn, [({"ver": "x"}, [entry(a, [], state="not_late")])])
        self.assertEqual(got["placed"], 0)          # watermark -1: older than every stored row
        self.assertEqual(json.loads(self.read(a, FULL)[0]["payload_text"])["state"], "late")
        with self.assertRaises(RuntimeError):
            ps.upsert(self.dsn, [], schema="no_such_schema")

    def test_upserts_lose_and_repeat_no_row_at_1_10_and_60(self):
        self.load([entry(r, []) for r in self.orders])
        before = len(self.orders)
        for n in (1, 10, 60):
            ids = list(dict.fromkeys((self.orders * 60)[:n])) if n > len(self.orders) else self.orders[:n]
            got = self.put([entry(r, [], state="not_late") for r in ids])
            self.assertEqual(got["placed"], len(ids))
            with psycopg.connect(self.dsn) as c:
                self.assertEqual(c.execute(f"SELECT count(*), count(DISTINCT handle) FROM "
                                           f"{bv.SCHEMA}.plan_section_items").fetchone(),
                                 (before, before))
                self.assertEqual(c.execute(f"SELECT count(*) FROM {bv.SCHEMA}.lineage WHERE view = "
                                           "'plan_section_items'").fetchone()[0], before)

    def test_a_door_read_never_waits_on_a_refreshs_exclusive_lock(self):
        import time
        order = self.an_order_ops_reads()
        self.load([entry(order, [])])
        for table in ("page_items", "plan_section_items"):
            with psycopg.connect(self.dsn, autocommit=True) as holder:
                holder.execute("BEGIN")
                holder.execute(f"LOCK TABLE {bv.SCHEMA}.{table} IN ACCESS EXCLUSIVE MODE")
                try:
                    began = time.monotonic()
                    out = self.page({"reads": [{"name": "plan_section", "params": {
                        "record_id": order, "section": "late"}}, {"name": "record_page", "params": {
                        "record_id": order, "limit": 5}}]}, FULL)
                    waited = time.monotonic() - began
                finally:
                    holder.execute("ROLLBACK")
            self.assertEqual(out["error"]["code"], "not_ready", table)
            self.assertLess(waited, 1.5, table)      # LOCK_MS, not the old 2 s (nor minutes)
        self.assertEqual(len(self.read(order, FULL)), 1)     # the lock gone: served again

    def test_a_read_with_no_live_path_keeps_its_wait_for_a_lock_that_ends(self):
        import threading
        import time
        order = self.an_order_ops_reads()
        with psycopg.connect(self.dsn, autocommit=True) as holder:
            holder.execute("BEGIN")
            holder.execute(f"LOCK TABLE {bv.SCHEMA}.purchase_orders IN ACCESS EXCLUSIVE MODE")
            threading.Timer(0.6, lambda: holder.execute("ROLLBACK")).start()
            began = time.monotonic()
            out = self.page({"reads": [{"name": "purchase_order", "params": {"handle": "x"}}]}, FULL)
            waited = time.monotonic() - began
        self.assertNotIn("error", out)            # waited out the 0.6 s swap (a 100 ms limit would refuse)
        self.assertGreater(waited, 0.4)

    def test_the_ai_reads_no_such_view_and_an_unknown_read_fails_closed(self):
        self.load([entry(self.orders[0], [self.pick(True)])])
        with psycopg.connect(self.dsn) as c:
            self.assertIsNone(c.execute("SELECT to_regclass(%s)", (
                f"{bv.SCHEMA}_sql.plan_section_items",)).fetchone()[0])
            self.assertIsNotNone(c.execute("SELECT to_regclass(%s)", (
                f"{bv.SCHEMA}_page.plan_section_items",)).fetchone()[0])
        for params in ({"record_id": "x"}, {"record_id": "x", "section": "late", "more": "1"}):
            out = self.page({"reads": [{"name": "plan_section", "params": params}]}, FULL)
            self.assertEqual(out["error"]["code"], "invalid_request")


class PlanSectionExclusion(Door):
    """Its own database (the exclusion set persists): a cited record of a person the exclusion
    names makes the row the owner's alone, and an answer citing nobody is untouched."""

    def test_a_cited_excluded_persons_record_makes_the_row_the_owners_alone(self):
        from operating.pg import door
        h = self.pick(True)
        rid = next(rid for items in self.cites.values() for hh, rid in items if hh == h)
        self.load([entry(self.orders[0], [h]), entry(self.orders[1], [])])
        self.assertTrue(self.read(self.orders[0], FULL))
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('x://excluded-cited', %s, 'participant')", (rid,))
            c.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, "
                      "objects, handles, covered_ver) VALUES ('t2', 'now', 1, 1, 1)")
            door.exclude(c, bv.SCHEMA)
        # an old row is read too (read_any): the exclusion hides it whatever its currency
        self.assertEqual(self.read_any(self.orders[0], FULL), [])
        self.assertEqual(self.read_any(self.orders[0], OPS), [])
        self.assertTrue(self.read_any(self.orders[1], FULL))      # an answer citing nobody stays

    def test_an_upserted_row_whose_order_has_an_excluded_object_is_the_owners_alone_at_once(self):
        from operating.pg import door
        self.load([entry(self.orders[0], []), entry(self.orders[2], [])])
        unrelated = set(self.deps(self.orders[2])[0])
        # an object only the first order's row depends on (both rows open the same default record)
        gone = next(d for d in self.deps(self.orders[0])[0] if d not in unrelated)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('x://excluded-dep', %s, 'participant')", (gone,))
            c.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, "
                      "objects, handles, covered_ver) VALUES ('t3', 'now', 1, 1, 1) ON CONFLICT (one) "
                      "DO UPDATE SET exclusion = 't3'")
            door.exclude(c, bv.SCHEMA)
        self.put([entry(self.orders[0], []), entry(self.orders[2], [])])
        self.assertEqual(self.read_any(self.orders[0], FULL), [])      # decided in the same transaction
        self.assertEqual(self.read_any(self.orders[0], OPS), [])
        self.assertTrue(self.read_any(self.orders[2], FULL))           # an answer without it stays



class PlanSectionOldRowExclusion(Door):
    """Its own fixture database (the exclusion it adds is never undone), so it always runs."""

    def test_a_person_excluded_after_the_build_is_hidden_on_an_old_row_before_the_resident_renews_it(self):
        from operating.pg import door
        order = self.an_order_ops_reads()
        self.load([entry(order, [])])
        deps = self.deps(order)[0]
        self.moved("ver", deps[0])          # the row is old now (its dependencies moved)
        self.assertEqual([r["current"] for r in self.read_any(order, OPS)], [False])
        self.assertEqual(len(self.read_any(order, FULL)), 1)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('x://excluded-after', %s, 'participant')", (deps[0],))
            c.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, "
                      "objects, handles, covered_ver) VALUES ('t4', 'now', 1, 1, 1) ON CONFLICT (one) "
                      "DO UPDATE SET exclusion = 't4'")
            door.exclude(c, bv.SCHEMA)

        # no renewal has happened: the old row is gone for the non-owner readers
        self.assertEqual(self.read_any(order, OPS), [])
        self.assertEqual(self.read_any(order, NOBODY), [])


class Resident(Door):
    """What the resident runner (``operator_api.plan_resident``) asks the database for: the rows
    that need an answer now, each exactly once, in the order that serves readers best."""

    def setUp(self):
        super().setUp()
        self.load([])                      # the table exists, empty

    def fill(self, open_):
        self.load([entry(r, []) for r in open_] + [people(r, []) for r in open_]
                  + [landed(r, []) for r in open_] + [trail(r, []) for r in open_])

    def ask(self, memo=None, shard=(0, 1), now=None):
        from operator_api import plan_resident as pr_
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            return pr_.discover(pg, shard, memo or {}, bv.SCHEMA, now)

    def lonely(self, open_):
        """(an open order, one of its dependencies no other row depends on)."""
        held = {r: set(self.deps(r)[0]) for r in open_}
        for r, deps in held.items():
            mine = deps - set().union(*(d for o, d in held.items() if o != r))
            if mine:
                return r, sorted(mine)[0]
        self.skipTest("every open order shares its dependencies")

    def open_orders(self):
        from operator_api import plan_batch
        with psycopg.connect(self.dsn) as c:
            return [r[0] for r in c.execute(plan_batch.OPEN_SQL.format(s=bv.SCHEMA))]

    def test_every_open_order_is_missing_until_it_has_a_row_and_then_only_what_moved_is_asked(self):
        open_ = self.open_orders()
        self.assertTrue(open_, "the fixture has no open order")
        want = sorted(open_)
        self.assertEqual([r for r, _ in self.ask()], want)
        self.assertEqual({tuple(s) for _, s in self.ask()}, {("landed", "late", "people", "trail")})
        self.fill(open_)
        self.assertEqual(self.ask(), [])                      # current rows are left alone
        victim, dep = self.lonely(open_)
        self.moved("ver", dep)
        self.assertEqual(self.ask(), [(victim, ["landed", "late", "people", "trail"])])

    def test_a_failed_row_keeps_its_back_off_in_the_row_and_a_no_answer_is_never_asked_again(self):
        open_ = self.open_orders()
        self.fill(open_)
        order = open_[0]
        got = self.load([{**entry(order, []), "failed": "the plan raised RuntimeError",
                          "attempts": 3, "run_at": "2026-11-23T12:00:00Z"},
                         trail(open_[-1], [], state=None)])      # completed, found nothing
        self.assertEqual(got["stored"], 2 if open_[-1] != order else 1)

        def stored(rid=order, sec="late"):
            with psycopg.connect(self.dsn, autocommit=True) as c:
                return c.execute(f"SELECT payload FROM {bv.SCHEMA}.plan_section_items WHERE "
                                 "record_id = %s AND section = %s", (rid, sec)).fetchone()[0]
        row = stored()
        # the pace is IN the row: 3 attempts, doubling from ten minutes
        self.assertEqual((row["failed"], row["attempts"], row["next_try_at"]),
                         ("the plan raised RuntimeError", 3, "2026-11-23T12:40:00+00:00"))
        self.assertEqual(ps.backoff_seconds(1), 600)
        self.assertEqual(ps.backoff_seconds(3), 2400)
        self.assertEqual(ps.backoff_seconds(40), ps.RETRY_MAX_S)
        # the due time (2026-11-23) has passed: asked again, with the attempts so far
        tried = {}
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            from operator_api import plan_resident as pr_
            got_ = dict(pr_.discover(pg, (0, 1), {}, bv.SCHEMA, attempts_out=tried))
        self.assertIn("late", got_.get(order, []))
        self.assertEqual(tried[(order, "late")], 3)
        if open_[-1] != order:                                # a stored no_answer is left alone
            self.assertNotIn("trail", got_.get(open_[-1], []))
        # not yet due (the next try is in the future): a restart of the process does not ask early,
        # the memo of this process holds nothing
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.plan_section_items SET payload = jsonb_set(payload, "
                      "'{next_try_at}', to_jsonb((now() + interval '1 hour')::text)) WHERE "
                      "record_id = %s AND section = 'late'", (order,))
        self.assertNotIn("late", dict(self.ask()).get(order, []))
        # the failure gets older than its back-off: asked again; moved dependencies ask it too
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.plan_section_items SET payload = jsonb_set(payload, "
                      "'{next_try_at}', to_jsonb((now() - interval '1 second')::text)) WHERE "
                      "record_id = %s AND section = 'late'", (order,))
        self.assertIn("late", dict(self.ask()).get(order, []))

    def test_a_failed_row_of_a_closed_order_is_asked_again_once_due_and_not_before(self):
        open_ = set(self.open_orders())
        closed = [o for o in self.orders if o not in open_]
        if not closed:
            self.skipTest("fixture has no closed order")
        order = closed[0]
        self.assertNotIn(order, [r for r, _ in self.ask()])      # a closed order is not in the cycle
        self.load([{**entry(order, []), "failed": "the plan raised RuntimeError", "attempts": 1,
                    "run_at": "2026-11-23T12:00:00Z"}])
        self.assertEqual(dict(self.ask()).get(order), ["late"])
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.plan_section_items SET payload = jsonb_set(payload, "
                      "'{next_try_at}', to_jsonb((now() + interval '1 hour')::text)) WHERE "
                      "record_id = %s", (order,))
        self.assertNotIn(order, [r for r, _ in self.ask()])

    def test_a_failed_line_written_by_the_loop_carries_its_attempts(self):
        import argparse
        import tempfile
        from operator_api import plan_batch, plan_resident as pr_
        open_ = self.open_orders()
        self.fill(open_)
        victim = self.lonely(open_)
        self.moved("ver", victim[1])

        def fake(view, rid, family):
            return {"record_id": rid, "family": family, "failed": "the plan raised KeyError"}

        def stop(_):
            raise SystemExit
        with tempfile.TemporaryDirectory() as d, mock.patch.object(plan_batch, "answer", fake), \
                mock.patch.object(pr_.time, "sleep", stop):
            args = argparse.Namespace(out=d, queue=None, poll=0.01, max_records=1, max_seconds=60,
                                      require_standby=False, lease_seconds=3600)
            pr_.loop(None, args, self.dsn, (0, 1), "w0")
            import os
            with open(os.path.join(d, os.listdir(d)[0])) as fh:
                lines = [json.loads(x) for x in fh.read().splitlines()][1:]
        self.assertEqual({x["attempts"] for x in lines}, {1})
        self.load(lines)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            rows = c.execute(f"SELECT payload FROM {bv.SCHEMA}.plan_section_items WHERE record_id = %s",
                             (victim[0],)).fetchall()
        failed = [r[0] for r in rows if "failed" in r[0]]
        self.assertTrue(failed and all(r["attempts"] == 1 and r["next_try_at"] for r in failed))

    def test_a_row_from_another_day_is_asked_after_the_rows_whose_dependencies_moved(self):
        open_ = self.open_orders()
        if len(open_) < 2:
            self.skipTest("fixture has one open order")
        self.fill(open_)
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute(f"UPDATE {bv.SCHEMA}.plan_section_items SET built_at = '2020-01-01T12:00:00+00:00' "
                      "WHERE record_id = %s", (open_[0],))
        other = open_[1]
        self.moved("ver", self.deps(other)[0][0])
        got = [r for r, _ in self.ask()]
        self.assertLess(got.index(other), got.index(open_[0]))   # moved first, then the old day

    def test_what_this_process_answered_is_not_asked_again_until_something_moved_past_it(self):
        open_ = self.open_orders()
        self.fill(open_)
        victim, dep = self.lonely(open_)
        self.moved("ver", dep)
        marks = self.marks()
        memo = {(victim, "late"): (100.0, int(marks["ver"]), int(marks["link"]), int(marks["xmin"])),
                (victim, "people"): (100.0, int(marks["ver"]), int(marks["link"]), int(marks["xmin"])),
                (victim, "trail"): (100.0, int(marks["ver"]), int(marks["link"]), int(marks["xmin"])),
                (victim, "landed"): (100.0, int(marks["ver"]), int(marks["link"]), int(marks["xmin"]))}
        # the standby's copy of the row still shows the old watermark: the put has not replayed
        self.assertEqual(self.ask(memo), [])
        self.moved("ver", dep)                               # a newer change than the answer saw
        self.assertEqual(self.ask(memo), [(victim, ["landed", "late", "people", "trail"])])

    def test_a_record_whose_plans_gave_nothing_waits_before_it_is_tried_again(self):
        from operator_api import plan_resident as pr_
        open_ = self.open_orders()
        failed = {(open_[0], s): (-1000.0, 0, 0) for s in ("late", "people", "landed", "trail")}
        self.assertNotIn(open_[0], [r for r, _ in self.ask(failed, now=1000.0 + 5)])
        self.assertIn(open_[0], [r for r, _ in self.ask(failed, now=1000.0 + pr_.RETRY_AFTER + 1)])

    def test_shards_split_the_work_with_no_record_lost_or_repeated(self):
        open_ = self.open_orders()
        for n in (1, 3, 10, 60):
            seen = [r for i in range(n) for r, _ in self.ask(shard=(i, n))]
            self.assertEqual(sorted(seen), sorted(open_), n)
            self.assertEqual(len(seen), len(set(seen)), n)

    def test_the_runner_refuses_a_database_that_is_not_a_standby_and_files_appear_whole(self):
        import argparse
        import os
        import tempfile
        from operator_api import plan_resident as pr_
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(out=d, queue=None, poll=0.01, max_records=1, max_seconds=5)
            self.assertEqual(pr_.loop(None, args, self.dsn, (0, 1), "w0"), 3)
            self.assertEqual(os.listdir(d), [])
            path = pr_.write_atomic(d, "w0-1-1", [{"stamps": {}}, {"record_id": "x"}])
            self.assertEqual(os.listdir(d), ["w0-1-1.jsonl"])
            self.assertEqual(len(open(path).read().splitlines()), 2)
            with open(os.path.join(d, "a.ids"), "w") as fh:
                fh.write("one\n\ntwo\n")
            self.assertEqual(pr_.forced(d), ["one", "two"])
            self.assertEqual(pr_.forced(d), [])               # taken once

    def test_a_change_of_the_people_exclusion_ends_the_process_for_a_restart_with_the_new_list(self):
        from operator_api import plan_resident as pr_
        with psycopg.connect(self.dsn, autocommit=True) as c:
            before = pr_.exclusion_stamp(c)
            c.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, objects, "
                      "handles, covered_ver) VALUES ('t-resident-a', 'now', 1, 1, 1) ON CONFLICT "
                      "(one) DO UPDATE SET exclusion = 't-resident-a'")
            self.assertEqual(pr_.exclusion_stamp(c), "t-resident-a")
            self.assertNotEqual(before, "t-resident-a")

    def test_a_table_locked_by_the_box_or_a_recovery_conflict_is_waited_out_not_a_crash(self):
        import argparse
        import tempfile
        from unittest import mock
        from operator_api import plan_resident as pr_
        calls = []

        def flaky(pg, shard, memo, schema="biz", now=None, why_out=None, attempts_out=None):
            calls.append(1)
            if len(calls) < 3:
                raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")
            if len(calls) == 3:
                raise psycopg.errors.SerializationFailure("canceling statement due to conflict with recovery")
            return []
        sleeps = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pr_, "discover", flaky), \
                mock.patch.object(pr_.time, "sleep", sleeps.append):
            args = argparse.Namespace(out=d, queue=None, poll=0.5, max_records=50, max_seconds=60,
                                      require_standby=False, lease_seconds=3600)
            ticks = iter(range(100))
            with mock.patch.object(pr_.time, "monotonic", lambda: next(ticks) * 10.0):
                self.assertEqual(pr_.loop(None, args, self.dsn, (0, 1), "w0"), 0)
        self.assertGreaterEqual(len(calls), 4)           # it went on after each error
        self.assertTrue(sleeps and all(s >= 1.0 for s in sleeps[:3]))

    def test_a_process_ends_when_a_newer_one_took_its_shard_or_nobody_fetches_its_answers(self):
        import os
        import tempfile
        from operating.pg import plan_sections_pull as pp
        from operator_api import plan_resident as pr_
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as queue:
            old = pr_.take_shard(queue, "w0")
            began = 1000.0
            ask = lambda nonce, now: pr_.still_wanted(out, queue, "w0", nonce, began, 300.0, now)
            self.assertIsNone(ask(old, 1100.0))                    # inside the grace of its start
            self.assertIn("nobody", ask(old, 1000.0 + 301))        # nobody ever fetched
            pp.DirTransport(out).names()                           # the puller's round touches the lease
            now = os.path.getmtime(os.path.join(out, pr_.LEASE))
            self.assertIsNone(pr_.still_wanted(out, queue, "w0", old, began, 300.0, now + 100))
            self.assertIn("nobody", pr_.still_wanted(out, queue, "w0", old, began, 300.0, now + 301))
            new = pr_.take_shard(queue, "w0")                      # the unit started it again
            self.assertNotEqual(old, new)
            self.assertIn("newer", pr_.still_wanted(out, queue, "w0", old, began, 300.0, now + 1))
            self.assertIsNone(pr_.still_wanted(out, queue, "w0", new, began, 300.0, now + 1))
            self.assertIsNone(pr_.still_wanted(out, queue, "w1", old, began, 300.0, now + 1))   # another shard

    def test_the_loop_answers_what_is_asked_and_ends_when_the_exclusion_changes(self):
        import argparse
        import tempfile
        from unittest import mock
        from operator_api import plan_batch, plan_resident as pr_
        open_ = self.open_orders()
        self.fill(open_)
        victim = self.lonely(open_)
        self.moved("ver", victim[1])
        asked = []

        def fake(view, rid, family):
            asked.append((rid, family))
            if family == "late_po":
                return {"record_id": rid, "family": family, "run": OK_RUN, "opened": [rid], "verdict": {
                    "state": "late", "text": "t", "facts": []}}
            return {"record_id": rid, "family": family, "run": OK_RUN, "opened": [rid],
                    "entries": "failed"}   # not an answer

        def change(_):
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute("INSERT INTO ontology.people_excluded_state (exclusion, built_at, objects, "
                          "handles, covered_ver) VALUES ('t-resident-b', 'now', 1, 1, 1) ON CONFLICT "
                          "(one) DO UPDATE SET exclusion = 't-resident-b'")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(plan_batch, "answer", fake), \
                mock.patch.object(pr_.time, "sleep", change):
            args = argparse.Namespace(out=d, queue=None, poll=0.01, max_records=50, max_seconds=60,
                                      require_standby=False, lease_seconds=3600)
            self.assertEqual(pr_.loop(None, args, self.dsn, (0, 1), "w0"), 0)
            import os
            files = os.listdir(d)
            with open(os.path.join(d, files[0])) as fh:
                lines = [json.loads(x) for x in fh.read().splitlines()]
            # every family's line is written, as the batch path writes it: the answer and the
            # three runs that failed (typed, with a reason; the stored row says "failed")
            self.assertEqual(len(lines), 5, lines)
            self.assertEqual(sorted(ps.outcome(x)[0] for x in lines[1:]),
                             ["answer", "failed", "failed", "failed"])
        self.assertEqual({r for r, _ in asked}, {victim[0]})     # only what moved was asked
        self.assertEqual(sorted(f for _, f in asked), ["landed_cost", "late_po", "people_on_record", "po_trail"])
        self.assertEqual(len(files), 1)                          # one file for the one record



class Pull(Door):
    """The box's side of the resident runner: files from the Spark folder put in place."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.load([])
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def drop(self, name, entries, stamps=None):
        import os
        lines = [{"stamps": {**self.marks(), **(stamps or {})}}, *entries]
        with open(os.path.join(self.folder.name, name), "w") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in lines))

    def pull(self):
        from operating.pg import plan_sections_pull as pp
        return pp.pull_once(self.dsn, pp.DirTransport(self.folder.name))

    def left(self):
        import os
        return sorted(n for n in os.listdir(self.folder.name) if n.endswith(".jsonl"))

    def test_files_are_put_in_place_and_taken_off_only_after(self):
        for n, r in enumerate(self.orders[:3]):
            self.drop(f"w0-1-{n}.jsonl", [entry(r, [], state="late"), people(r, [])])
        got = self.pull()
        self.assertEqual((got["files"], got["placed"], got["bad"]), (3, 6, 0))
        self.assertEqual(self.left(), [])
        for r in self.orders[:3]:
            self.assertEqual(len(self.read(r, FULL)), 1)
            self.assertEqual(len(self.read(r, FULL, "people")), 1)

    def test_a_stale_records_latency_is_found_to_put_plus_the_standbys_lag(self):
        import time
        from operating.pg import plan_sections_pull as pp
        total = {"moved_s": []}
        pp._latency(total, {"seen_at": 100.0, "replay_lag_s": 2.5, "why": {"late": "moved"}}, now=110.0)
        pp._latency(total, {"seen_at": 100.0, "why": {"late": "missing"}}, now=110.0)   # not a stale row
        pp._latency(total, {"why": {"late": "moved"}}, now=110.0)                      # no time given
        pp._latency(total, None, now=110.0)
        self.assertEqual(total["moved_s"], [12.5])
        a = self.orders[0]
        self.drop("w0-9-1.jsonl", [entry(a, [])])
        got = self.pull()
        self.assertEqual((got["files"], got["moved_s"]), (1, []))

    def test_a_record_answered_in_two_files_keeps_the_newer_states_answer_once(self):
        a = self.orders[0]
        marks = self.marks()
        newer = {"ver": str(int(marks["ver"]) + 1), "link": marks["link"]}
        self.drop("w0-2-1.jsonl", [entry(a, [], state="not_late")], stamps=newer)
        self.drop("w0-1-1.jsonl", [entry(a, [], state="late")])
        got = self.pull()
        self.assertEqual((got["files"], got["bad"]), (2, 0))
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(c.execute(f"SELECT count(*), count(DISTINCT handle) FROM "
                                       f"{bv.SCHEMA}.plan_section_items").fetchone(), (1, 1))
        # the newer state's answer stands (served once the version it saw is the newest)
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(c.execute(f"SELECT payload->>'state' FROM {bv.SCHEMA}.plan_section_items"
                                       ).fetchone()[0], "not_late")

    def test_a_broken_file_is_set_aside_and_never_holds_the_others_back(self):
        import os
        from unittest import mock
        self.drop("w0-1-1.jsonl", [entry(self.orders[0], [])])
        with open(os.path.join(self.folder.name, "w0-1-2.jsonl"), "w") as fh:
            fh.write("{not json")
        with open(os.path.join(self.folder.name, "w0-1-3.jsonl"), "w") as fh:
            fh.write(json.dumps({"no": "stamps"}) + "\n")
        self.drop("w0-1-4.jsonl", [entry(self.orders[1], [])])
        real = ps.upsert

        def boom(dsn, runs, schema=None):
            if any(e.get("record_id") == self.orders[1] for _, es in runs for e in es) and len(runs) == 1:
                raise RuntimeError("this one alone fails")
            if len(runs) > 1:
                raise RuntimeError("a batch fails")
            return real(dsn, runs, schema)
        with mock.patch.object(ps, "upsert", boom):
            got = self.pull()
        self.assertEqual((got["files"], got["bad"]), (1, 3))
        self.assertEqual(self.left(), [])
        self.assertEqual(sorted(os.listdir(os.path.join(self.folder.name, "bad"))),
                         ["w0-1-2.jsonl", "w0-1-3.jsonl", "w0-1-4.jsonl"])
        self.assertEqual(len(self.read(self.orders[0], FULL)), 1)
        self.assertEqual(self.read(self.orders[1], FULL), [])

    def test_a_table_locked_by_a_rebuild_leaves_every_file_for_the_next_round(self):
        import os
        from unittest import mock
        self.drop("w0-1-1.jsonl", [entry(self.orders[0], [])])
        self.drop("w0-1-2.jsonl", [entry(self.orders[1], [])])
        with mock.patch.object(ps, "upsert", side_effect=psycopg.errors.LockNotAvailable("busy")):
            got = self.pull()
        self.assertEqual((got["files"], got["bad"], got.get("waited")), (0, 0, 2))
        self.assertEqual(self.left(), ["w0-1-1.jsonl", "w0-1-2.jsonl"])
        self.assertFalse(os.path.exists(os.path.join(self.folder.name, "bad")))
        self.assertEqual(self.pull()["files"], 2)                   # the next round puts them

    def test_a_real_lock_held_by_another_session_ends_the_wait_and_keeps_the_files(self):
        from unittest import mock
        self.drop("w0-1-1.jsonl", [entry(self.orders[0], [])])
        with mock.patch.object(ps, "LOCK_WAIT_S", 0.3), \
                psycopg.connect(self.dsn, autocommit=True) as holder:
            holder.execute("BEGIN")
            holder.execute(f"LOCK TABLE {bv.SCHEMA}.page_items IN ACCESS EXCLUSIVE MODE")
            try:
                got = self.pull()
            finally:
                holder.execute("ROLLBACK")
        self.assertEqual((got["files"], got["bad"], got.get("waited")), (0, 0, 1))
        self.assertEqual(self.left(), ["w0-1-1.jsonl"])

    def test_only_one_puller_runs_at_a_time(self):
        from operating.pg import plan_sections_pull as pp
        first = pp.singleton(self.dsn)
        self.assertIsNotNone(first)
        self.assertIsNone(pp.singleton(self.dsn))          # a second one is refused
        first.close()
        again = pp.singleton(self.dsn)
        self.assertIsNotNone(again)                         # the lock goes with the process
        again.close()

    def test_a_puller_that_cannot_renew_the_marks_fails_for_the_host_alarm(self):
        from unittest import mock
        from operating.pg import plan_sections_pull as pp
        ticks = iter(range(0, 10000, 30))
        with mock.patch.object(ps, "mark_links", side_effect=RuntimeError("db away")), \
                mock.patch.object(pp.time, "monotonic", lambda: next(ticks)), \
                mock.patch.object(pp.time, "sleep"), \
                mock.patch.object(pp, "pull_once", return_value={"files": 0}):
            self.assertEqual(pp.main(["--dsn", self.dsn, "--host", "s7d3d"]), 1)

    def test_only_plain_file_names_are_listed_and_the_ssh_commands_quote_them(self):
        import os
        from operating.pg import plan_sections_pull as pp
        for name in ("a.jsonl", "b.txt", "c d.jsonl", "x;rm -rf.jsonl", ".hidden.tmp"):
            open(os.path.join(self.folder.name, name), "w").close()
        self.assertEqual(pp.DirTransport(self.folder.name).names(), ["a.jsonl"])
        sent = []
        tr = pp.SshTransport("s7d3d", "plx-plan/out", "/dev/null", run=lambda s: sent.append(s) or "")
        tr.drop(["a.jsonl", "x;rm -rf.jsonl", "$(id).jsonl"])
        tr.read(["../../etc/passwd", "b.jsonl"])
        self.assertIn("rm -f a.jsonl", sent[0])
        self.assertNotIn("rm -rf", sent[0])
        self.assertNotIn("passwd", sent[1])
        for host, folder in (("a;b", "x"), ("h", "x y"), ("h", "$(id)"), ("h", "x;y")):
            with self.assertRaises(ValueError):
                pp.SshTransport(host, folder)


if __name__ == "__main__":
    unittest.main()
