"""The incremental proactive pass (operating/proactive_incremental.py) equals a full run.

SYNTHETIC fixtures only (``test_proactive_engine.seeded``: made-up records, no business
values). Each case builds the store before and after a change, runs the full engine over
the store before (the inbox the pass starts from), then folds the store after both ways:
a full run, and an incremental pass over the changed records only. The open gaps of every
rule the pass judges (ids, wording, follow-up, priority, citations, audience
requirements) must be the same. Evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from operating import proactive_engine as pe
from operating import proactive_incremental as pi
from operating.tests.test_proactive_engine import AS_OF, QC_LINK, World, people, seeded

MOMENT = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)


class Changing(World):
    """A world whose link ids stay unique when links are removed and added."""

    def link(self, link_type: str, a: str, b: str, tier: str = "verified") -> None:
        super().link(link_type, a, b, tier)
        self.links_made = getattr(self, "links_made", 0) + 1
        self.rows["links"][-1]["link_id"] = f"l{self.links_made}"


def world() -> tuple[World, dict]:
    return seeded(Changing())


def set_value(w: World, oid: str, prop: str, value) -> None:
    """The record's value of ``prop`` replaced (added when it had none), as a new version
    of its source record (a new digest)."""
    version = next(v for v in w.rows["object_versions"] if v["object_id"] == oid)
    version["digest"] = version["digest"] + "'"
    rows = [r for r in w.rows["object_values"] if r["object_id"] == oid and r["property"] == prop]
    if rows:
        for row in rows:
            row["value"] = value[0] if isinstance(value, tuple) else value
            row["digest"] = version["digest"]
        return
    quantity = isinstance(value, tuple)
    w.rows["object_values"].append({
        "object_id": oid, "property": prop,
        "value_type": "quantity" if quantity else "date" if prop.endswith("_date")
        or prop == "valid_until" else "text",
        "value": value[0] if quantity else value, "unit": value[1] if quantity else None,
        "quantity_role": (value[2] if len(value) > 2 else "ordered") if quantity else None,
        "l1_handle": version["l1_handle"], "field_path": f"row.{prop}",
        "digest": version["digest"], "lineage": "workbench", "binding_id": "fx",
        "parser": "fixture", "markings": version["markings"], "recorded_at": version["recorded_at"]})


def drop_link(w: World, link_type: str, end: str) -> None:
    w.rows["links"] = [l for l in w.rows["links"] if not (
        l["link_type"] == link_type and end in (l["from_object"], l["to_object"]))]


def drop_object(w: World, oid: str) -> None:
    for table in ("objects", "object_versions", "object_values"):
        w.rows[table] = [r for r in w.rows[table] if r["object_id"] != oid]
    w.rows["links"] = [l for l in w.rows["links"] if oid not in (l["from_object"], l["to_object"])]


def of_type(w: World, object_type: str) -> list[str]:
    return [r["object_id"] for r in w.rows["objects"] if r["object_type"] == object_type]


def open_gaps(inbox: Path, rules) -> dict:
    """Every open gap of these rules with what a reader is shown of it."""
    with sqlite3.connect(inbox) as conn:
        gaps = {}
        for row in conn.execute(
                "SELECT gap_id, kind, detector, object_id, object_type, summary, follow_up, "
                "priority FROM gaps WHERE state = 'open' AND detector IN "
                "(SELECT value FROM json_each(?))", (json.dumps(sorted(rules)),)):
            cites = sorted(conn.execute("SELECT grp, handle, object_id, markings FROM gap_cites "
                                        "WHERE gap_id = ?", (row[0],)).fetchall())
            needs = sorted(r[0] for r in conn.execute(
                "SELECT audience_key FROM gap_requires WHERE gap_id = ?", (row[0],)))
            gaps[row[0]] = (*row[1:], cites, needs)
    return gaps


class Incremental(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # the QC link declared (the live schema declares none: the rule is blocked there)
        patch = mock.patch.object(pe, "declared_qc_links", lambda: (QC_LINK,))
        patch.start()
        self.addCleanup(patch.stop)
        pi.INPUTS.clear()
        self.addCleanup(pi.INPUTS.clear)
        pi._declared()
        self.rules = [r for r in pi.STORE_ONLY if r not in pi.NOT_INCREMENTAL]

    def build(self, w: World, name: str, digest: str | None = None) -> Path:
        path = self.dir / name / "store.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        w.build(path)
        if digest is not None:   # the same served generation, its rows moved by a layer
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE store_meta SET value = ? WHERE key = 'content_digest'",
                             (digest,))
        return path

    def full(self, store: Path, as_of=AS_OF) -> dict:
        return pe.run(store, pe.inbox_path(store), as_of, clock=lambda: MOMENT,
                      people=people())

    def changed(self, before: Path, after: Path) -> set[str]:
        prints = []
        for path in (before, after):
            st = pe._Store(path)
            try:
                ids = [r[0] for r in st.conn.execute("SELECT object_id FROM objects")]
                prints.append(pi.fingerprints(st, ids))
            finally:
                st.close()
        return {oid for oid in set(prints[0]) | set(prints[1])
                if (prints[0].get(oid) or ("absent",))[0] != (prints[1].get(oid) or ("absent",))[0]}

    def compare(self, w: World, change, *, as_of=AS_OF, moves: bool = False) -> dict:
        """Full run over ``w``; ``change(w)``; the store after folded fully and by an
        incremental pass from the first inbox: their open gaps must be equal."""
        before = self.build(w, "before")
        self.full(before)
        with sqlite3.connect(before) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        change(w)
        whole = self.build(w, "whole", digest)
        part = self.build(w, "part", digest)
        for target in (whole, part):
            shutil.copy(pe.inbox_path(before), pe.inbox_path(target))
        moved = self.changed(before, part)
        self.full(whole, as_of)
        summary = pi.run_pass(part, as_of=as_of, clock=lambda: MOMENT, changed=moved,
                              base=before, people=people(), wait=True)
        self.assertEqual(summary["state"], "ran", summary)
        expected = open_gaps(pe.inbox_path(whole), self.rules)
        found = open_gaps(pe.inbox_path(part), self.rules)
        self.assertEqual(sorted(found), sorted(expected), summary)
        self.assertEqual(found, expected, summary)
        if moves:   # the change moved what a full run finds (the case is not vacuous)
            self.assertNotEqual(found, open_gaps(pe.inbox_path(before), self.rules))
        return summary

    # -- one change of each kind

    def test_a_late_order_received_in_full_resolves_as_in_a_full_run(self):
        w, ids = world()

        def change(w):
            line = next(l["from_object"] for l in w.rows["links"]
                        if l["link_type"] == "PurchaseOrderLine.line_of"
                        and l["to_object"] == ids["overdue"])
            set_value(w, line, "received_quantity", ("10", "ea", "received"))
        summary = self.compare(w, change, moves=True)
        self.assertIn("scoped", summary)
        self.assertNotIn("whole", summary)

    def test_a_line_value_reopens_its_closed_order_as_in_a_full_run(self):
        w, _ids = world()
        closed = w.obj("PurchaseOrder", {"status": "B.Order", "expected_date": "2026-10-01"})
        line = w.obj("PurchaseOrderLine", {"ordered_quantity": ("10", "ea"),
                                           "received_quantity": ("10", "ea", "received")})
        w.link("PurchaseOrderLine.line_of", line, closed)
        # only the line's value moves: its order is reached by the declared links alone
        summary = self.compare(w, lambda w: set_value(
            w, line, "received_quantity", ("4", "ea", "received")), moves=True)
        self.assertNotIn("whole", summary)

    def test_a_new_confirmation_and_a_lost_one_move_only_their_orders(self):
        w, ids = world()

        def change(w):
            w.link("SupplierConfirmation.confirms",
                   w.obj("SupplierConfirmation", {"delivery_date": "2026-10-30"}),
                   ids["unconfirmed"])
            drop_link(w, "SupplierConfirmation.confirms", ids["confirmed_late"])
        summary = self.compare(w, change, moves=True)
        self.assertLess(summary["scoped"]["read"], len(w.rows["objects"]))

    def test_a_mailbox_owners_new_mail_reads_none_of_their_other_mail(self):
        """A person linked to many mails by a link type no incremental rule reads (a
        mailbox owner, ~0.5 M mail links live) gets one more alongside an order's change:
        the pass equals a full run, the person still counts as changed (the watchlists are
        told), and neither the neighbours kept nor the scope hold the other mails (11-17:
        holding them grew the pass past its 12 GB cap)."""
        w, ids = world()
        person = w.obj("Person", {"name": "Owner"})
        mails = [w.obj("Email", {"subject": f"m{n}"}) for n in range(60)]
        for mail in mails:
            w.link("Email.addressed_to_person", mail, person)
        self.assertNotIn("Email.addressed_to_person", pi.read_kinds())

        def change(w):
            w.link("Email.addressed_to_person", w.obj("Email", {"subject": "new"}), person)
            line = next(l["from_object"] for l in w.rows["links"]
                        if l["link_type"] == "PurchaseOrderLine.line_of"
                        and l["to_object"] == ids["overdue"])
            set_value(w, line, "received_quantity", ("10", "ea", "received"))
        summary = self.compare(w, change, moves=True)
        self.assertIn("scoped", summary)
        self.assertNotIn("whole", summary)
        self.assertLess(summary["scoped"]["read"], len(mails))
        part = self.dir / "part" / "store.sqlite"
        self.assertIn(person, self.changed(self.dir / "before" / "store.sqlite", part))
        st = pe._Store(part)
        try:
            kept = pi.fingerprints(st, [person, *mails[:1]], pi.read_kinds())
            every = pi.fingerprints(st, [person], None)
        finally:
            st.close()
        self.assertEqual(kept[person][1], [])
        self.assertEqual(kept[mails[0]][1], [])
        self.assertEqual(len(every[person][1]), len(mails) + 1)
        self.assertEqual(every[person][0], kept[person][0])   # one print, any neighbours
        # reads bounded to a few rows (halved, the person paged alone): the same prints
        st = pe._Store(part)
        try:
            ids = [r[0] for r in st.conn.execute("SELECT object_id FROM objects")]
            whole = pi.fingerprints(st, ids)
            with mock.patch.object(pi, "PRINT_ROWS", 4):
                self.assertEqual(pi.fingerprints(st, ids), whole)
        finally:
            st.close()

    def test_a_record_gone_from_its_source_takes_its_gaps_with_it(self):
        w, ids = world()
        self.compare(w, lambda w: drop_object(w, ids["expired"]), moves=True)

    def test_quantities_dates_and_quotes_moved_together(self):
        w, ids = world()

        def change(w):
            set_value(w, ids["conflict_b"], "received_quantity", ("30", "kg", "received"))
            set_value(w, ids["over"], "ordered_quantity", ("200", "kg"))
            po = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-02"})
            w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
                "ordered_quantity": ("5", "ea"),
                "received_quantity": ("0", "ea", "received")}), po)
            set_value(w, of_type(w, "ForwarderQuote")[-1], "valid_until", "2026-10-01")
        self.compare(w, change, moves=True)

    def test_a_moved_population_judges_its_rules_whole(self):
        w, _ids = world()

        def change(w):
            # a dozen orders on one day: that day becomes the Portal's bulk default date,
            # which moves every order dated that day, read or not (a bulk day is a default
            # only when a system computed it from each order's own day, BIZ-007: here the
            # order day + 1)
            for _ in range(12):
                w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-10",
                                        "order_date": "2026-10-09"})
        summary = self.compare(w, change)
        self.assertIn("late_or_unconfirmed_po", summary["whole"]["rules"])

    def test_a_new_day_judges_every_dated_rule_whole(self):
        w, _ids = world()
        summary = self.compare(w, lambda w: None, as_of=AS_OF + timedelta(days=12))
        self.assertIn("late_or_unconfirmed_po", summary["whole"]["rules"])
        self.assertEqual(summary["changed"], 0)

    def test_random_changes_equal_a_full_run(self):
        for seed in range(16):
            with self.subTest(seed=seed):
                self.setUp()
                rng = random.Random(seed)
                w, ids = world()

                def change(w, rng=rng):
                    quantities = [r for r in w.rows["object_values"]
                                  if r["value_type"] == "quantity"]
                    dated = [r for r in w.rows["object_values"] if r["value_type"] == "date"]
                    for _ in range(rng.randint(1, 4)):
                        pick = rng.random()
                        if pick < 0.3 and quantities:
                            row = rng.choice(quantities)
                            set_value(w, row["object_id"], row["property"],
                                      (str(rng.choice((0, 5, 10, 30, 35, 100, 120))),
                                       row["unit"], row["quantity_role"]))
                        elif pick < 0.55 and dated:
                            row = rng.choice(dated)
                            set_value(w, row["object_id"], row["property"],
                                      rng.choice(("2026-09-30", "2026-10-18", "2026-10-20",
                                                  "2026-11-30")))
                        elif pick < 0.75 and w.rows["links"]:
                            link = rng.choice(w.rows["links"])
                            w.rows["links"].remove(link)
                        elif pick < 0.9:
                            target = rng.choice(of_type(w, "PurchaseOrder"))
                            w.link("SupplierConfirmation.confirms", w.obj(
                                "SupplierConfirmation",
                                {"delivery_date": rng.choice(("2026-10-01", "2026-11-15"))}),
                                target, rng.choice(("verified", "lead")))
                        else:
                            drop_object(w, rng.choice(w.rows["objects"])["object_id"])
                self.compare(w, change)


class LayerChanges(unittest.TestCase):
    """The changes read from the served delta layers, fingerprinted: a layer re-owning
    records that did not change (a compaction) judges nothing again."""

    def test_layers_are_taken_in_once_and_unchanged_records_are_skipped(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        pi.INPUTS.clear()
        self.addCleanup(pi.INPUTS.clear)
        w, ids = world()
        store = root / "store.sqlite"
        w.build(store)
        pe.run(store, pe.inbox_path(store), AS_OF, clock=lambda: MOMENT, people=people())
        with sqlite3.connect(store) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        delta = root / "delta"
        delta.mkdir()

        def publish(name: str, owned) -> None:
            layer = delta / name
            with sqlite3.connect(layer) as conn:
                conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
                conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in owned])
            (delta / "CURRENT.json").write_text(json.dumps({
                "format": "plx-delta/v1", "base_digest": digest, "store": str(layer),
                "layers": [{"store": str(layer), "owned": len(owned)}]}))
        publish("delta-1.sqlite", [ids["unconfirmed"], ids["expired"]])
        first = pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                            wait=True, holds_layers=True)
        # nothing kept yet for this generation: every owned record counts as changed
        self.assertEqual((first["state"], first["changed"]), ("ran", 2), first)
        again = pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                            wait=True, holds_layers=True)
        self.assertEqual(again["candidates"], 0)            # the layer was taken in
        publish("delta-2.sqlite", [ids["unconfirmed"], ids["expired"], ids["overdue"]])
        compacted = pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                                wait=True, holds_layers=True)
        self.assertEqual(compacted["candidates"], 3)
        self.assertEqual(compacted["changed"], 1)   # only the one never fingerprinted
        # a pass while another holds the inbox's lock waits for it within its bound: a
        # full pass that ends meanwhile hands it over, one that does not leaves it "busy"
        with pe.pass_lock(pe.inbox_path(store)):
            busy = pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                               holds_layers=True, wait=0.3)
        self.assertEqual(busy["state"], "busy")
        # by default a busy store is left at once, not waited for (CC-P-03)
        with pe.pass_lock(pe.inbox_path(store)):
            started = time.monotonic()
            quick = pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                                holds_layers=True)
        self.assertEqual(quick["state"], "busy")
        self.assertLess(time.monotonic() - started, 2.0)
        held, release = threading.Event(), threading.Event()

        def full_pass() -> None:
            with pe.pass_lock(pe.inbox_path(store)):
                held.set()
                release.wait(10)
        holder = threading.Thread(target=full_pass)
        holder.start()
        self.assertTrue(held.wait(10))
        threading.Timer(0.5, release.set).start()
        handed = pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                             holds_layers=True, wait=30)
        holder.join(10)
        self.assertEqual(handed["state"], "ran", handed)
        # the SQLite generation holds no layer: none is taken in, nothing marked seen
        publish("delta-3.sqlite", [ids["late_with_lead"]])
        blind = pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                            wait=True)
        self.assertEqual((blind["reader"], blind["candidates"]), ("generation_only", 0))

    def test_a_postgres_reader_takes_applied_layers_in_under_a_moving_stamp(self):
        """Postgres says its content digest with its data version (``<digest>@<n>``,
        moving with every applied layer): the pass still knows the generation, takes in
        the layers Postgres applied (marked seen, which the delta's release rule reads),
        waits for one not applied yet, and keeps its fingerprints across stamps. Before
        11-12 it took in no layer at all, holding every big layer on disk."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        pi.INPUTS.clear()
        self.addCleanup(pi.INPUTS.clear)
        w, ids = world()
        store = root / "store.sqlite"
        w.build(store)
        pe.run(store, pe.inbox_path(store), AS_OF, clock=lambda: MOMENT, people=people())
        with sqlite3.connect(store) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        delta = root / "delta"
        delta.mkdir()
        layers: list[dict] = []

        def publish(name: str, owned) -> None:
            layer = delta / name
            with sqlite3.connect(layer) as conn:
                conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
                conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in owned])
            layers.insert(0, {"store": str(layer), "owned": len(owned)})
            (delta / "CURRENT.json").write_text(json.dumps({
                "format": "plx-delta/v1", "base_digest": digest, "store": layers[0]["store"],
                "layers": layers}))
        version = [100]
        applied: set[str] = set()
        real_meta = pe._Store.meta

        def stamped(st, key):
            value = real_meta(st, key)
            return f"{value}@{version[0]}" if key == "content_digest" else value
        for target, new in ((pe._Store, "meta"), (pi, "_applied")):
            patch = mock.patch.object(target, new, stamped if new == "meta"
                                      else (lambda _store: set(applied)))
            patch.start()
            self.addCleanup(patch.stop)
        # Postgres's links: the store's own (it stands for Postgres here; never the
        # production database)
        read_pg = []
        patch = mock.patch.object(pi, "_pg_neighbors", lambda ids, out, types=None:
                                  read_pg.append(len(ids))
                                  or pi._file_neighbors(store, ids, out, types))
        patch.start()
        self.addCleanup(patch.stop)

        def run():
            return pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                               wait=True)
        publish("delta-1.sqlite", [ids["unconfirmed"], ids["expired"]])
        publish("delta-2.sqlite", [ids["overdue"]])
        applied.add("delta-1.sqlite")                     # delta-2 not in Postgres yet
        first = run()
        self.assertEqual((first["reader"], first["changed"]), ("postgres", 2), first)
        self.assertEqual(read_pg, [2])   # the earlier links were asked of Postgres
        # what the delta's release rule reads (``_proactive_taken``): delta-1 in, delta-2 not
        with sqlite3.connect(pi.changes_path(pe.inbox_path(store))) as conn:
            taken = {r[0] for r in conn.execute("SELECT layer FROM layers")}
        self.assertEqual({Path(t).name for t in taken}, {"delta-1.sqlite"})
        # Postgres applies delta-2: its data version moves; the generation is the same
        version[0] = 101
        applied.add("delta-2.sqlite")
        second = run()
        self.assertEqual((second["candidates"], second["changed"]), (1, 1), second)
        version[0] = 102
        third = run()
        self.assertEqual(third["candidates"], 0, third)   # both taken in, nothing new
        with sqlite3.connect(pi.changes_path(pe.inbox_path(store))) as conn:
            taken = {Path(r[0]).name for r in conn.execute("SELECT layer FROM layers")}
            prints = conn.execute("SELECT count(*) FROM prints").fetchone()[0]
        self.assertEqual(taken, {"delta-1.sqlite", "delta-2.sqlite"})
        self.assertEqual(prints, 3)                       # kept across the stamps

    def test_layers_owning_more_than_a_scope_reads_are_judged_whole_and_taken_in(self):
        """A catch-up layer owns more records than any scope may read (``read_cap``):
        the pass fingerprints and plans none of them (that held GBs past the unit's
        memory bound and never finished, retried every few minutes), judges the rules
        whole, tells the watchlists every owned record, and marks the layer taken in."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        pi.INPUTS.clear()
        self.addCleanup(pi.INPUTS.clear)
        w, ids = world()
        store = root / "store.sqlite"
        w.build(store)
        pe.run(store, pe.inbox_path(store), AS_OF, clock=lambda: MOMENT, people=people())
        with sqlite3.connect(store) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        delta = root / "delta"
        delta.mkdir()
        owned = [ids["unconfirmed"], ids["expired"], ids["overdue"]]
        layer = delta / "delta-big.sqlite"
        with sqlite3.connect(layer) as conn:
            conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in owned])
        (delta / "CURRENT.json").write_text(json.dumps({
            "format": "plx-delta/v1", "base_digest": digest, "store": str(layer),
            "layers": [{"store": str(layer), "owned": len(owned)}]}))
        watched = []
        with mock.patch.object(pi, "fingerprints",
                               side_effect=AssertionError("fingerprinted a flood")), \
                mock.patch.object(pi, "_watch", lambda _st, _store, moved, _clock:
                                  watched.append(set(moved)) or {"state": "idle"}):
            flood = pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                                wait=True, holds_layers=True, read_cap=2)
        self.assertEqual((flood["state"], flood["flooded"], flood["candidates"]),
                         ("ran", 3, 0), flood)
        self.assertEqual(watched, [set(owned)])
        judged = set((flood.get("ran") or flood).get("whole", {}).get("rules") or [])
        incremental = {r for r in pi.STORE_ONLY if r in pi.INPUTS
                       and r not in pi.NOT_INCREMENTAL}
        self.assertTrue(incremental)
        self.assertLessEqual(incremental, judged, flood)
        with sqlite3.connect(pi.changes_path(pe.inbox_path(store))) as conn:
            taken = {Path(r[0]).name for r in conn.execute("SELECT layer FROM layers")}
        self.assertEqual(taken, {"delta-big.sqlite"})
        after = pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                            wait=True, holds_layers=True, read_cap=2)
        self.assertEqual((after["candidates"], after.get("flooded")), (0, None), after)

    def test_the_generation_digest_of_a_stamp(self):
        self.assertEqual(pi.generation_digest("sha256:ab@1794486485713"), "sha256:ab")
        self.assertEqual(pi.generation_digest("sha256:ab"), "sha256:ab")
        for nothing in (None, "", "@12", 7):
            self.assertIsNone(pi.generation_digest(nothing))


def _links_in(path: Path) -> dict[str, set[str]]:
    """Every current link of the store file at ``path``, by both ends."""
    out: dict[str, set[str]] = {}
    with sqlite3.connect(path) as conn:
        for a, b in conn.execute("SELECT from_object, to_object FROM link_rows "
                                 "WHERE superseded_at IS NULL"):
            out.setdefault(a, set()).add(b)
            out.setdefault(b, set()).add(a)
    return out


class ScopeReach(unittest.TestCase):
    """plan() walks forward over the LINK types (not the object types of the moved
    records): a Lot changed three hops from a PO still reads the receipt line's identity
    twin and the second Lot (the scoped read set is a superset, never less)."""

    def test_far_change_reads_the_twin_and_the_second_lot(self):
        patch = mock.patch.object(pe, "declared_qc_links", lambda: (QC_LINK,))
        patch.start()
        self.addCleanup(patch.stop)
        pi.INPUTS.clear()
        self.addCleanup(pi.INPUTS.clear)
        pi._declared()
        rules = [r for r in pi.STORE_ONLY if r not in pi.NOT_INCREMENTAL and r in pi.INPUTS]
        w, _ids = world()
        po = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-01"})
        lines = [w.obj("PurchaseOrderLine", {"ordered_quantity": ("10", "ea"),
                                             "received_quantity": ("0", "ea", "received")})
                 for _ in range(2)]
        for line in lines:
            w.link("PurchaseOrderLine.line_of", line, po)
        rls = [w.obj("ReceiptLine", {"received_quantity": ("5", "ea", "received")})
               for _ in range(2)]
        for rl, line in zip(rls, lines):
            w.link("ReceiptLine.receives_line", rl, line)
        lots = [w.obj("Lot", {}) for _ in range(2)]
        for rl, lot in zip(rls, lots):
            w.link("ReceiptLine.received_lot", rl, lot)
        twin = w.obj("ReceiptLine", {"received_quantity": ("5", "ea", "received")})
        w.link("ReceiptLine.same_record_as", rls[1], twin)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.sqlite"
            w.build(path)
            st = pe._Store(path)
            try:
                _subjects, read = pi.plan(st, {lots[0]}, {}, Path(tmp) / "none", rules)
            finally:
                st.close()
        self.assertIn(twin, read)
        self.assertIn(lots[1], read)


class BaseNeighbours(unittest.TestCase):
    """The changed records' earlier links (``_base_neighbors``): read from Postgres when
    the engine reads Postgres, with the SQLite generation file beside it while one is
    served; an error, never an empty answer, when no store can be read (before 11-12 the
    pass read the generation file in every mode and silently judged with no earlier
    neighbours once the file was gone). Postgres here is a throwaway database
    (``pg_testdb``), never the production one."""

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_testdb
        from operating.tests.test_pg_ontology_delta import MIGRATIONS
        import psycopg
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        w, _ids = world()
        (cls.root / "gen").mkdir()
        cls.store = w.build(cls.root / "gen" / "store.sqlite")
        from operating.pg import ontology_load
        import io
        ontology_load.load(cls.store, cls.dsn, workers=1, replace=True, log=io.StringIO())
        cls.links = _links_in(cls.store)
        cls.ids = sorted(cls.links)

    def setUp(self):
        patch = mock.patch.dict("os.environ", {"PLX_PG_DSN": self.dsn})
        patch.start()
        self.addCleanup(patch.stop)

    def pg(self):
        import psycopg
        return psycopg.connect(self.dsn, autocommit=True)

    def test_postgres_alone_when_the_generation_file_is_gone(self):
        self.assertGreater(len(self.ids), 10)
        gone = self.root / "retired" / "store.sqlite"
        for n in (1, 10, len(self.ids)):
            with self.subTest(records=n):
                ids = self.ids[:n]
                found = pi._base_neighbors(gone, ids, postgres=True)
                self.assertEqual(found, {i: self.links[i] for i in ids})

    def test_postgres_and_the_served_generation_file_together(self):
        """A layer applied in Postgres removed a link: Postgres no longer has it, the
        generation file does, and both are read (a superset, judged never less)."""
        with self.pg() as pg:
            a, b = pg.execute("SELECT from_object, to_object FROM ontology.link_rows "
                              "ORDER BY link_id LIMIT 1").fetchone()
            pg.execute("CREATE TABLE ontology.kept AS SELECT * FROM ontology.link_rows")
            pg.execute("DELETE FROM ontology.link_rows WHERE from_object = %s AND "
                       "to_object = %s", (a, b))
            pg.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                       "to_object, tier, binding_kind, why_not, markings, recorded_at, "
                       "reason, evidence_handles) VALUES ('new-link', 'x', %s, 'zz-new', "
                       "'verified', 'fixture', '', '[]', '2026-11-12', '', '[]')", (a,))
        try:
            gone = self.root / "retired" / "store.sqlite"
            only = pi._base_neighbors(gone, [a], postgres=True)
            self.assertNotIn(b, only.get(a, set()))
            self.assertIn("zz-new", only[a])
            both = pi._base_neighbors(self.store, [a], postgres=True)
            self.assertIn(b, both[a])
            self.assertIn("zz-new", both[a])
            self.assertEqual(pi._base_neighbors(self.store, [a]), {a: self.links[a]})
        finally:
            with self.pg() as pg:
                pg.execute("TRUNCATE ontology.link_rows")
                pg.execute("INSERT INTO ontology.link_rows SELECT * FROM ontology.kept")
                pg.execute("DROP TABLE ontology.kept")

    def test_no_readable_store_is_an_error_never_nothing(self):
        gone = self.root / "retired" / "store.sqlite"
        with self.assertRaises(pi.BaseUnreadable):     # SQLite read, file gone
            pi._base_neighbors(gone, self.ids[:1])
        with self.assertRaises(pi.BaseUnreadable):
            pi._base_neighbors(None, self.ids[:1])
        bad = self.root / "bad.sqlite"
        bad.write_bytes(b"not a database" * 100)
        with self.assertRaises(pi.BaseUnreadable):
            pi._base_neighbors(bad, self.ids[:1])
        # Postgres read, unreachable: the file beside it is not taken for Postgres
        with mock.patch.dict("os.environ", {"PLX_PG_DSN": "postgresql://127.0.0.1:1/none"}):
            for base in (gone, self.store):
                with self.subTest(base=base), self.assertRaises(pi.BaseUnreadable):
                    pi._base_neighbors(base, self.ids[:1], postgres=True)
        # Postgres reachable but its links not loaded (a load in progress)
        with self.pg() as pg:
            pg.execute("UPDATE ontology.load_state SET name = 'held:link_rows' "
                       "WHERE name = 'table:link_rows'")
        try:
            with self.assertRaisesRegex(pi.BaseUnreadable, "not_loaded"):
                pi._base_neighbors(self.store, self.ids[:1], postgres=True)
        finally:
            with self.pg() as pg:
                pg.execute("UPDATE ontology.load_state SET name = 'table:link_rows' "
                           "WHERE name = 'held:link_rows'")
        self.assertEqual(pi._base_neighbors(gone, [], postgres=True), {})  # nothing asked

    def test_a_pass_whose_earlier_links_cannot_be_read_fails_and_takes_no_layer_in(self):
        pi.INPUTS.clear()
        self.addCleanup(pi.INPUTS.clear)
        w, ids = world()
        store = self.root / "pass" / "store.sqlite"
        store.parent.mkdir(exist_ok=True)
        w.build(store)
        pe.run(store, pe.inbox_path(store), AS_OF, clock=lambda: MOMENT, people=people())
        with sqlite3.connect(store) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        layer = store.parent / "delta-1.sqlite"
        with sqlite3.connect(layer) as conn:
            conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO delta_owned VALUES (?)", (ids["unconfirmed"],))
        from operating import delta_overlay
        pointer = delta_overlay.pointer_path(store)
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({
            "format": "plx-delta/v1", "base_digest": digest, "store": str(layer),
            "layers": [{"store": str(layer), "owned": 1}]}))
        with mock.patch.object(pi, "_applied", lambda _store: {"delta-1.sqlite"}), \
                mock.patch.dict("os.environ", {"PLX_PG_DSN": "postgresql://127.0.0.1:1/none"}):
            with self.assertRaises(pi.BaseUnreadable):
                pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF, people=people(),
                            wait=True)
        with sqlite3.connect(pi.changes_path(pe.inbox_path(store))) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM layers").fetchone()[0], 0)


class SplitRoundtrip(unittest.TestCase):
    """Split Spark compute + box apply equals the fused incremental pass, writes nothing
    during compute, handles stale/bad folds loudly, and never regresses watermarks."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patch = mock.patch.object(pe, "declared_qc_links", lambda: (QC_LINK,))
        patch.start()
        self.addCleanup(patch.stop)
        pi.INPUTS.clear()
        self.addCleanup(pi.INPUTS.clear)
        pi._declared()
        self.rules = [r for r in pi.STORE_ONLY if r not in pi.NOT_INCREMENTAL]

    def _build(self, w: World, name: str, digest: str | None = None) -> Path:
        path = self.dir / name / "store.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        w.build(path)
        if digest is not None:
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE store_meta SET value = ? WHERE key = 'content_digest'",
                             (digest,))
        return path

    def _compare_split(self, w: World, change, *, as_of=AS_OF, moves: bool = False) -> tuple[dict, Path, Path]:
        before = self._build(w, "before")
        pe.run(before, pe.inbox_path(before), AS_OF, clock=lambda: MOMENT, people=people())
        with sqlite3.connect(before) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        change(w)
        fused = self._build(w, "fused", digest)
        split = self._build(w, "split", digest)
        for target in (fused, split):
            shutil.copy(pe.inbox_path(before), pe.inbox_path(target))
        prints = []
        for path in (before, split):
            st = pe._Store(path)
            try:
                ids = [r[0] for r in st.conn.execute("SELECT object_id FROM objects")]
                prints.append(pi.fingerprints(st, ids))
            finally:
                st.close()
        moved = {oid for oid in set(prints[0]) | set(prints[1])
                 if (prints[0].get(oid) or ("absent",))[0] != (prints[1].get(oid) or ("absent",))[0]}
        fused_summary = pi.run_pass(fused, as_of=as_of, clock=lambda: MOMENT, changed=moved,
                                    base=before, people=people(), wait=True)
        self.assertEqual(fused_summary["state"], "ran", fused_summary)

        export = self.dir / "export"
        split_inbox = pe.inbox_path(split)
        before_bytes = split_inbox.read_bytes()
        compute_summary = pi.compute(split, export, as_of=as_of, clock=lambda: MOMENT,
                                     changed=moved, base=before, people=people())
        self.assertEqual(compute_summary["state"], "ran", compute_summary)
        # compute makes zero box writes
        self.assertEqual(split_inbox.read_bytes(), before_bytes)
        self.assertFalse(pi.changes_path(split_inbox).exists())
        self.assertTrue(list(export.glob("inc-*.json")))

        applied = pi.apply_fold_payloads(split, export, clock=lambda: MOMENT,
                                         people=people(), wait=True)
        self.assertEqual(applied["stale"], 0, applied)
        self.assertEqual(applied["bad"], 0, applied)
        self.assertEqual(applied["left"], 0, applied)
        self.assertGreater(applied["applied"], 0, applied)

        self.assertEqual(open_gaps(pe.inbox_path(split), self.rules),
                         open_gaps(pe.inbox_path(fused), self.rules))
        with sqlite3.connect(pi.changes_path(pe.inbox_path(fused))) as fc, \
                sqlite3.connect(pi.changes_path(split_inbox)) as sc:
            self.assertEqual(
                sorted(fc.execute("SELECT object_id, print, neighbors FROM prints").fetchall()),
                sorted(sc.execute("SELECT object_id, print, neighbors FROM prints").fetchall()))
            self.assertEqual(
                sorted(fc.execute("SELECT key, value FROM meta").fetchall()),
                sorted(sc.execute("SELECT key, value FROM meta").fetchall()))
        if moves:
            self.assertNotEqual(open_gaps(split_inbox, self.rules),
                                open_gaps(pe.inbox_path(before), self.rules))
        return applied, fused, split

    def test_compute_and_apply_equal_fused_pass_across_changes_new_day_and_populations(self):
        # 1. Scoped line change
        w, ids = world()

        def resolve_overdue(w):
            line = next(l["from_object"] for l in w.rows["links"]
                        if l["link_type"] == "PurchaseOrderLine.line_of"
                        and l["to_object"] == ids["overdue"])
            set_value(w, line, "received_quantity", ("10", "ea", "received"))
        self._compare_split(w, resolve_overdue, moves=True)

        # 2. New day: dated rules judged whole
        shutil.rmtree(self.dir)
        self.dir.mkdir()
        w2, _ids2 = world()
        self._compare_split(w2, lambda w: None, as_of=AS_OF + timedelta(days=12))

        # 3. Moved population: scoped run triggers whole rule during compute
        shutil.rmtree(self.dir)
        self.dir.mkdir()
        w3, _ids3 = world()

        def move_pop(w):
            for _ in range(12):
                w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-10",
                                        "order_date": "2026-10-09"})
        self._compare_split(w3, move_pop)

    def test_compute_and_apply_preserve_watchlists_and_write_nothing_on_compute(self):
        from operating import watchlists as wl
        w, ids = world()
        before = self._build(w, "before")
        pe.run(before, pe.inbox_path(before), AS_OF, clock=lambda: MOMENT, people=people())
        with sqlite3.connect(before) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        fused = self._build(w, "fused", digest)
        split = self._build(w, "split", digest)
        for target in (fused, split):
            shutil.copy(pe.inbox_path(before), pe.inbox_path(target))
            ws = wl.WatchStore(wl.watch_path(target), clock=lambda: MOMENT)
            ws.follow("plx:employee:owner", ids["overdue"], "PurchaseOrder", "PO-overdue")
            # baseline the follow on the before state
            st = pe._Store(before)
            try:
                wl.evaluate(st, wl.watch_path(target), (), clock=lambda: MOMENT)
            finally:
                st.close()
        line = next(l["from_object"] for l in w.rows["links"]
                    if l["link_type"] == "PurchaseOrderLine.line_of"
                    and l["to_object"] == ids["overdue"])
        set_value(w, line, "received_quantity", ("10", "ea", "received"))
        for target, name in ((fused, "fused"), (split, "split")):
            self._build(w, name, digest)
        changed = {line}
        pi.run_pass(fused, as_of=AS_OF, clock=lambda: MOMENT, changed=changed,
                    base=before, people=people(), wait=True)
        export = self.dir / "w_export"
        watch_before = wl.watch_path(split).read_bytes()
        pi.compute(split, export, as_of=AS_OF, clock=lambda: MOMENT, changed=changed,
                   base=before, people=people())
        self.assertEqual(wl.watch_path(split).read_bytes(), watch_before)
        pi.apply_fold_payloads(split, export, clock=lambda: MOMENT, people=people(), wait=True)
        self.assertEqual(wl.WatchStore(wl.watch_path(split)).raw_alerts("plx:employee:owner", 50),
                         wl.WatchStore(wl.watch_path(fused)).raw_alerts("plx:employee:owner", 50))

    def test_prepare_context_compute_and_apply_roundtrip(self):
        from operating.host import projection_path
        w, ids = world()
        state = self.dir / "state"
        store = projection_path(state)
        store.parent.mkdir(parents=True)
        w.build(store)
        pe.run(store, pe.inbox_path(store), AS_OF, clock=lambda: MOMENT, people=people())
        with sqlite3.connect(store) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        delta = store.parent / "delta"
        delta.mkdir()
        layer = delta / "delta-1.sqlite"
        with sqlite3.connect(layer) as conn:
            conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO delta_owned VALUES (?)",
                             [(ids["unconfirmed"],), (ids["overdue"],)])
        (delta / "CURRENT.json").write_text(json.dumps({
            "format": "plx-delta/v1", "base_digest": digest, "store": str(layer),
            "layers": [{"store": str(layer), "owned": 2}]}))
        # mutate store in place as a delta layer would
        line = next(l["from_object"] for l in w.rows["links"]
                    if l["link_type"] == "PurchaseOrderLine.line_of"
                    and l["to_object"] == ids["overdue"])
        w.link("SupplierConfirmation.confirms",
               w.obj("SupplierConfirmation", {"delivery_date": "2026-10-09"}),
               ids["unconfirmed"])
        set_value(w, line, "received_quantity", ("10", "ea", "received"))
        base = self.dir / "base.sqlite"
        shutil.copy(store, base)
        w.build(store)
        with sqlite3.connect(store) as conn:
            conn.execute("UPDATE store_meta SET value = ? WHERE key = 'content_digest'", (digest,))

        ship = self.dir / "ship"
        export = self.dir / "export"
        prep = pi.prepare(state, ship, wait=True)
        self.assertEqual(prep["state"], "prepared")
        self.assertTrue((ship / "context.json").is_file())
        self.assertTrue((ship / "layer-owned.json.gz").is_file())

        summary = pi.compute(store, export, as_of=AS_OF, clock=lambda: MOMENT,
                             base=base, people=people(), holds_layers=True, context_dir=ship)
        self.assertEqual((summary["state"], summary["changed"]), ("ran", 2))
        self.assertFalse(pi.changes_path(pe.inbox_path(store)).exists())

        applied = pi.apply_fold_payloads(store, export, clock=lambda: MOMENT,
                                         people=people(), wait=True)
        self.assertEqual((applied["stale"], applied["bad"], applied["left"]), (0, 0, 0))
        with sqlite3.connect(pi.changes_path(pe.inbox_path(store))) as conn:
            self.assertEqual({Path(r[0]).name for r in conn.execute("SELECT layer FROM layers")},
                             {"delta-1.sqlite"})

    def test_stale_and_bad_folds_fail_loudly_and_leave_files(self):
        from operating.host import projection_path
        w, _ids = world()
        state = self.dir / "state"
        store = projection_path(state)
        store.parent.mkdir(parents=True)
        w.build(store)
        inbox = pe.inbox_path(store)
        pe.run(store, inbox, AS_OF, clock=lambda: MOMENT, people=people())

        export = self.dir / "export"
        export.mkdir()
        # empty export dir exits 0
        self.assertEqual(pi.main(["--state-dir", str(state), "--apply-folds", str(export)]), 0)

        # stale + bad standalone folds exit 1 and leave files
        (export / "fold-1.json").write_text(json.dumps({
            "fold_transport": 1, "digest": "sha256:stale", "results": {},
            "gaps": {}, "populations": {}, "scope_rows": [], "seconds": {},
            "not_due": [], "abandoned": [], "code": "c", "started_at": {},
            "run_at": MOMENT.isoformat(), "as_of": AS_OF.isoformat(),
            "record_run": False, "whole_scope": False,
        }))
        (export / "fold-2.json").write_text("{bad json")
        with mock.patch("ontology.window_lock.skip_report", return_value=None):
            rc = pi.main(["--state-dir", str(state), "--apply-folds", str(export)])
        self.assertEqual(rc, 1)
        self.assertTrue((export / "fold-1.json").is_file())
        self.assertTrue((export / "fold-2.json").is_file())

        # an inc payload referencing a missing fold refuses to advance watermarks
        for p in export.iterdir():
            p.unlink()
        digest = pe.served_generation(store, inbox)
        (export / "inc-1.json").write_text(json.dumps({
            "inc_transport": 1, "generation": digest, "stamp": digest,
            "reader": "store_with_layers", "print_format": pi.PRINT_FORMAT,
            "as_of": AS_OF.isoformat(), "moment": MOMENT.isoformat(),
            "prints": {}, "taken": ["delta-x.sqlite"], "waiting_for_postgres": False,
            "watch_moved": [], "audience_keys_held": None, "day_completed": True,
            "feed": None, "fold_files": ["fold-missing.json"], "summary": {"state": "ran"},
        }))
        with mock.patch("ontology.window_lock.skip_report", return_value=None):
            rc = pi.main(["--state-dir", str(state), "--apply-folds", str(export)])
        self.assertEqual(rc, 1)
        self.assertFalse(pi.changes_path(inbox).exists())
        self.assertTrue((export / "inc-1.json").is_file())

    def test_standby_lag_never_regresses_day_or_marks_unapplied_layers(self):
        w, ids = world()
        store = self._build(w, "store")
        inbox = pe.inbox_path(store)
        pe.run(store, inbox, AS_OF, clock=lambda: MOMENT, people=people())
        with sqlite3.connect(store) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        delta = store.parent / "delta"
        delta.mkdir()
        layers: list[dict] = []
        for name, oids in (("delta-1.sqlite", [ids["unconfirmed"]]),
                           ("delta-2.sqlite", [ids["overdue"]])):
            layer = delta / name
            with sqlite3.connect(layer) as conn:
                conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
                conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in oids])
            layers.insert(0, {"store": str(layer), "owned": len(oids)})
        (delta / "CURRENT.json").write_text(json.dumps({
            "format": "plx-delta/v1", "base_digest": digest, "store": layers[0]["store"],
            "layers": layers}))

        # Standby only has delta-1 replayed; delta-2 is NOT in standby load_state
        applied_pg = {"delta-1.sqlite"}
        real_meta = pe._Store.meta
        with mock.patch.object(pe._Store, "meta", lambda st, k: f"{real_meta(st, k)}@100"
                               if k == "content_digest" else real_meta(st, k)), \
                mock.patch.object(pi, "_applied", lambda _s: set(applied_pg)), \
                mock.patch.object(pi, "_pg_neighbors", lambda i, o, target=None, types=None:
                                  pi._file_neighbors(store, i, o, types=types)):
            export = self.dir / "lag_export"
            out = pi.compute(store, export, as_of=AS_OF, clock=lambda: MOMENT, people=people())
            self.assertTrue(out["waiting_for_postgres"])
            pi.apply_fold_payloads(store, export, clock=lambda: MOMENT, people=people(), wait=True)
            with sqlite3.connect(pi.changes_path(inbox)) as conn:
                taken = {Path(r[0]).name for r in conn.execute("SELECT layer FROM layers")}
                self.assertEqual(taken, {"delta-1.sqlite"})
            # Advance day to future date, then apply an older-day payload: day must not regress
            with sqlite3.connect(pi.changes_path(inbox)) as conn:
                pi._set_meta(conn, "day", "2026-10-25")
            applied_pg.add("delta-2.sqlite")
            export2 = self.dir / "lag_export2"
            pi.compute(store, export2, as_of=AS_OF, clock=lambda: MOMENT, people=people())
            pi.apply_fold_payloads(store, export2, clock=lambda: MOMENT, people=people(), wait=True)
            with sqlite3.connect(pi.changes_path(inbox)) as conn:
                self.assertEqual(pi._meta(conn, "day"), "2026-10-25")
                taken = {Path(r[0]).name for r in conn.execute("SELECT layer FROM layers")}
                self.assertEqual(taken, {"delta-1.sqlite", "delta-2.sqlite"})


if __name__ == "__main__":
    unittest.main()
