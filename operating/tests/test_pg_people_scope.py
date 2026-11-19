"""Person links kept right over delta layers applied to Postgres
(``operating.pg.people_scope``, ``operating.pg.ontology_delta``, ``operating.pg.whole_steps``).

Evidence label: ``isolated-fixture`` / ``injected-transport``: made-up mailboxes and
contacts captured from the capture tests' in-process FakeGraph, the real projector, schema,
people directory and people step, loader and layer apply, a scratch Postgres database.

Reproduces the 11-18 loss: a delta layer holds only the outside
contacts who sent mail in it, so its people step links a re-projected mail to none of its
other outside recipients, and the apply replaced the mail's links by the layer's.

* A layer re-projecting a mail keeps its links to every outside recipient (the layer itself
  holds none of them); a new mail to an outside contact is linked as a full build links it,
  rows and markings, whatever the size (1, 10 and 60 mails, judged in chunks).
* The people exclusion fails closed: without it nothing is judged, nothing changes and the
  mark stays; a Person it names gets no link from the run.
* Runs are idempotent: a second run (and a run over the same objects again) writes nothing
  and moves no data version.
* The backfill restores a lost link and drops a dangling one, writes only what differs,
  is idempotent, and its undo puts every row back.
* An unknown read or type is refused.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from operating.tests import pg_testdb
from operating.tests.test_pg_ontology_delta import MIGRATIONS

PERSON = "link_id >= 'person:' AND link_id < 'person;'"
OUT_ONLY_B = ("erin@client-three.example", "dave@client-two.example")


def _messages_base(n: int = 0) -> list[tuple]:
    """(box, id, sender, to, cc) of the base capture: two mailboxes, outside contacts who
    wrote only to mailbox B (erin, dave and ``n`` more), a mail of A's naming them."""
    from integrations.tests.test_capture_mail import BOX_A, BOX_B, UPN_A
    erin, dave = OUT_ONLY_B
    out = [(BOX_A, "MSG-A1", "supplier@outside.example", [UPN_A], []),
           (BOX_A, "MSG-A2", "carol@client-one.example", [UPN_A], []),
           (BOX_A, "MSG-A3", UPN_A, [erin], [dave, erin]),   # erin in To and Cc
           (BOX_B, "MSG-B1", dave, ["finance@example-plx.com"], []),
           (BOX_B, "MSG-B2", erin, ["finance@example-plx.com"], [])]
    out += [(BOX_B, f"MSG-BX{i:03d}", f"contact{i:03d}@far{i:03d}.example",
             ["finance@example-plx.com"], []) for i in range(n)]
    return out


def _capture(state: Path, messages: list[tuple]) -> None:
    from integrations import graph_read
    from integrations.capture_mail import MailCapture, MailCaptureStore
    from integrations.graph_read import GraphMailReader
    from integrations.live_source_guard import BoundedBackoff
    from integrations.tests.test_capture_mail import (CAPTURE, ENV, SCOPE, Clock, FakeGraph,
                                                       capture_audience)
    graph = FakeGraph()
    for box, mid, sender, to, cc in messages:
        graph.put(box, mid, "CK-" + mid, subject="Subject " + mid, body="Body " + mid)
        message = graph.messages[box][mid]
        message["from"] = {"emailAddress": {"address": sender}}
        message["toRecipients"] = [{"emailAddress": {"address": a}} for a in to]
        message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    store = MailCaptureStore(state, clock=Clock())
    api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
        sleep=lambda s: None, jitter=lambda: 0.0))
    run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                      sleep=lambda s: None).run(SCOPE)
    store.close()
    assert run["complete"], run


def _build(path: Path, state: Path, *, owned: list[str] | None = None,
           digest: str | None = None) -> Path:
    from integrations.people_exclusion import PeopleExclusion
    from integrations.tests.test_capture_mail import ENV, SCOPE
    from ontology import schema
    from ontology.people_links import people_links_step
    from ontology.project import MailCaptures, build_store
    from operating.tests.test_case_tools_host import NOW
    with MailCaptures(state, internal_domains=SCOPE.internal_domains,
                      exclusion=PeopleExclusion.from_env(ENV)) as mail:
        build_store(path, schema.load_schema(), {"graph_mail": mail}, clock=lambda: NOW,
                    post_project={"with_people": people_links_step})
    if owned is not None:   # a delta layer: its owned objects, over the base generation
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in owned])
            conn.execute("UPDATE store_meta SET value = ? WHERE key = 'content_digest'",
                         (digest,))
    return path


def _pairs(rows) -> list[tuple]:
    return sorted({(r[0], r[1], r[2], r[3]) for r in rows})


def _sqlite_person_links(path: Path) -> list[tuple]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return sorted(conn.execute(f"SELECT link_type, from_object, to_object, tier, markings "
                                   f"FROM links WHERE {PERSON}"))


def email(mid: str, box: str | None = None) -> str:
    from integrations.tests.test_capture_mail import BOX_A
    return ('Email:graph_mail.messages:{"id":"%s","mailbox":"%s"}' % (mid, box or BOX_A))


def person(address_or_oid: str) -> str:
    from ontology import people_directory as pd
    key = address_or_oid if "@" not in address_or_oid else pd.address_key(address_or_oid)
    return 'Person:graph_mail.people:{"person_key":"%s"}' % key


class PeopleFixture(unittest.TestCase):
    """A scratch Postgres with the migrations, the capture tests' exclusion; helpers."""

    def setUp(self):
        import psycopg
        from integrations.tests.test_capture_mail import ENV
        from operating.pg import ontology_load
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # the run's people exclusion: the capture tests' (no mailbox of the fixture)
        self.enterContext(mock.patch.dict(os.environ, {
            "PLX_EXCLUDED_PEOPLE_OIDS": ENV["PLX_EXCLUDED_PEOPLE_OIDS"],
            "PLX_EXCLUSION_ENV_FILE": ""}))
        self.ontology_load = ontology_load

    def load_base(self, messages: list[tuple]) -> Path:
        state = self.dir / "base-mail"
        _capture(state, messages)
        base = _build(self.dir / "base.sqlite", state)
        self.ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        with sqlite3.connect(base) as conn:
            self.digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                       "'content_digest'").fetchone()[0]
        return base

    def layer(self, name: str, messages: list[tuple], owned: list[str]) -> Path:
        state = self.dir / f"{name}-mail"
        _capture(state, messages)
        return _build(self.dir / f"{name}.sqlite", state, owned=owned, digest=self.digest)

    def full(self, name: str, messages: list[tuple]) -> list[tuple]:
        state = self.dir / f"{name}-full-mail"
        _capture(state, messages)
        return _sqlite_person_links(_build(self.dir / f"{name}-full.sqlite", state))

    def served(self) -> list[tuple]:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return sorted(tuple(r) for r in pg.execute(
                "SELECT link_type, from_object, to_object, tier, markings FROM ontology.links "
                "WHERE superseded_at IS NULL AND link_id COLLATE \"C\" >= 'person:' AND "
                "link_id COLLATE \"C\" < 'person;'"))

    def version(self) -> int:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return pg.execute("SELECT ontology.sq_data_version()").fetchone()[0]

    def sql(self, statement: str, params=()) -> None:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            pg.execute(statement, params)


class PersonLinksOverLayers(PeopleFixture):

    def test_a_layer_reprojecting_a_mail_keeps_its_outside_recipients(self):
        from integrations.tests.test_capture_mail import BOX_A
        from operating.pg import ontology_delta
        base_messages = _messages_base()
        base = self.load_base(base_messages)
        base_links = _sqlite_person_links(base)
        a3 = email("MSG-A3")
        named = {(lt, f, t) for lt, f, t, _tier, _m in base_links if f == a3}
        self.assertEqual(named, {("Email.sent_by_person", a3, person(BOX_A)),
                                 ("Email.addressed_to_person", a3, person(OUT_ONLY_B[0])),
                                 ("Email.addressed_to_person", a3, person(OUT_ONLY_B[1]))})
        self.assertEqual(self.served(), base_links)
        # mailbox A's mail only: the layer re-projects A3 without erin's and dave's Persons
        layer = self.layer("delta-1", [m for m in base_messages if m[0] == BOX_A], [a3])
        in_layer = {(lt, f, t) for lt, f, t, _tier, _m in _sqlite_person_links(layer)
                    if f == a3}
        self.assertEqual(in_layer, {("Email.sent_by_person", a3, person(BOX_A))})

        out = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO())
        self.assertEqual(out["applied_now"], 1, out)
        run = out["person_links"]
        self.assertEqual(run["mode"], "incremental", run)
        self.assertEqual(run["objects"], 1, run)
        # every link kept: the served person links are the full build's of the same mail
        self.assertEqual(_pairs(self.served()), _pairs(base_links))
        # the run's rows are the step's: nothing differs from the base build's but the
        # mail's own version they cite (A3 captured again)
        self.assertEqual(run["links"]["gone"] + run["links"]["added"], 0, run)

    def test_a_new_mail_to_outside_contacts_is_linked_as_a_full_build_links_it(self):
        """1, 10 and 60 new mails, each to an outside contact who wrote only to the other
        mailbox, judged in chunks of 7: the full build's links, rows and markings; no
        mail left out or linked twice; the run's time stays small."""
        from integrations.tests.test_capture_mail import BOX_A, UPN_A
        from operating.pg import ontology_delta, people_scope
        base_messages = _messages_base(60)
        self.load_base(base_messages)
        done: list[tuple] = []
        for n in (1, 10, 60):
            with self.subTest(mails=n):
                new = [(BOX_A, f"MSG-AN{i:03d}", UPN_A, [f"contact{i:03d}@far{i:03d}.example"],
                        ["nobody@unknown.example"]) for i in range(len(done), n)]
                done += new
                messages = base_messages + done
                layer = self.layer(f"delta-n{n}", [m for m in messages if m[0] == BOX_A],
                                   [email(m[1]) for m in new])
                with mock.patch.object(people_scope, "CHUNK", 7), \
                        mock.patch.dict(os.environ, {"PLX_PERSON_LINKS_RUN_MAX": "20"}):
                    t = time.monotonic()
                    out = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO())
                    runs = [out["person_links"]]
                    while runs[-1].get("remaining"):   # a catch-up over several runs
                        runs.append(people_scope.recompute(self.dsn))
                    took = time.monotonic() - t
                    self.assertEqual(people_scope.recompute(self.dsn), {"pending": False})
                self.assertEqual(sum(r["objects"] for r in runs), len(new), runs)
                self.assertEqual(len(runs), -(-len(new) // 20), runs)
                served = self.served()
                self.assertEqual(served, self.full(f"n{n}", messages))
                to = [r for r in served if r[0] == "Email.addressed_to_person"
                      and r[1].startswith("Email:") and "MSG-AN" in r[1]]
                self.assertEqual(len(to), n)   # one per mail: none lost, none twice
                self.assertTrue(all("pii" in json.loads(r[4]) for r in to), to[:1])
                self.assertLess(took, 120, runs)

    def test_the_exclusion_fails_closed_and_an_excluded_person_gets_no_link(self):
        from integrations.tests.test_capture_mail import BOX_A, BOX_B, ENV, UPN_A
        from operating.pg import ontology_delta, people_scope
        base_messages = _messages_base()
        base = self.load_base(base_messages)
        new = [(BOX_A, "MSG-A9", UPN_A, ["finance@example-plx.com", OUT_ONLY_B[0]], [])]
        messages = base_messages + new
        layer = self.layer("delta-x", [m for m in messages if m[0] == BOX_A],
                           [email("MSG-A9")])
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": "not-an-id"}), \
                mock.patch("sys.stderr", err):
            out = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO())
        self.assertEqual(out["applied_now"], 1, out)
        self.assertIn("exclusion_unavailable", out["person_links"]["error"])
        self.assertIn("ONTOLOGY PERSON LINKS FAILED", err.getvalue())
        self.assertEqual(self.served(), _sqlite_person_links(base))   # nothing written
        self.assertTrue(people_scope.pending(self.dsn))               # the mark stayed
        # mailbox B's person excluded: the mail's link to B's Person is never written
        with mock.patch.dict(os.environ, {
                "PLX_EXCLUDED_PEOPLE_OIDS": f"{ENV['PLX_EXCLUDED_PEOPLE_OIDS']},{BOX_B}"}):
            run = people_scope.recompute(self.dsn)
        self.assertGreaterEqual(run["counts"]["excluded_target"], 1, run)
        a9 = {(r[0], r[2]) for r in self.served() if r[1] == email("MSG-A9")}
        self.assertEqual(a9, {("Email.sent_by_person", person(BOX_A)),
                              ("Email.addressed_to_person", person(OUT_ONLY_B[0]))})
        full = {(r[0], r[2]) for r in self.full("x", messages) if r[1] == email("MSG-A9")}
        self.assertIn(("Email.addressed_to_person", person(BOX_B)), full)   # the guard's doing
        self.assertFalse(people_scope.pending(self.dsn))

    def test_runs_are_idempotent_and_move_no_version_when_nothing_changed(self):
        from integrations.tests.test_capture_mail import BOX_A
        from operating.pg import ontology_delta, people_scope
        base_messages = _messages_base()
        self.load_base(base_messages)
        layer = self.layer("delta-i", [m for m in base_messages if m[0] == BOX_A],
                           [email("MSG-A3"), email("MSG-A2")])
        out = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO())
        self.assertEqual(out["person_links"]["mode"], "incremental", out)
        after = self.served()
        before = self.version()
        again = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO())
        self.assertEqual((again["applied_now"], again["person_links"]), (0, {"pending": False}))
        # the same objects judged again from the start: nothing written, no version moved
        self.sql("UPDATE ontology.store_meta SET value = %s WHERE key = %s",
                 (json.dumps({"through": 0}), people_scope.STATE))
        run = people_scope.recompute(self.dsn)
        self.assertEqual({k: run["links"].get(k, 0) for k in ("gone", "changed", "added")},
                         {"gone": 0, "changed": 0, "added": 0}, run)
        self.assertIsNone(run["layer"], run)
        self.assertEqual((self.served(), self.version()), (after, before))

    def test_a_person_a_layer_removed_is_linked_from_nothing_after_the_run(self):
        """A layer replacing a Person (none has so far): every object linked to it is
        judged again over the directory without it, so no link dangles."""
        from operating.pg import people_scope
        base = self.load_base(_messages_base())
        gone = person(OUT_ONLY_B[0])
        self.assertTrue([r for r in _sqlite_person_links(base) if r[2] == gone])
        self.sql("DELETE FROM ontology.object_versions WHERE object_id = %s", (gone,))
        self.sql("DELETE FROM ontology.objects WHERE object_id = %s", (gone,))
        self.sql("INSERT INTO ontology.store_meta (key, value) VALUES (%s, %s)",
                 (people_scope.STATE, json.dumps({"through": 0})))
        self.sql("INSERT INTO ontology.applied_owned (layer, object_id) VALUES "
                 "('delta-p', %s)", (gone,))
        run = people_scope.recompute(self.dsn)
        self.assertEqual(run["persons_touched"], 1, run)
        self.assertGreaterEqual(run["links"]["gone"], 2, run)   # A3's and B2's
        self.assertFalse([r for r in self.served() if r[2] == gone])
        self.assertEqual([r for r in self.served() if r[2] != gone],
                         [r for r in _sqlite_person_links(base) if r[2] != gone])

    def test_the_backfill_restores_lost_links_drops_dangling_ones_and_undoes(self):
        from operating.pg import people_scope
        base = self.load_base(_messages_base())
        base_links = _sqlite_person_links(base)
        a3, lost = email("MSG-A3"), person(OUT_ONLY_B[0])
        self.sql("DELETE FROM ontology.link_rows WHERE from_object = %s AND to_object = %s",
                 (a3, lost))
        self.sql("INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, "
                 "tier, binding_kind, why_not, markings, recorded_at, reason, "
                 "evidence_handles) VALUES ('person:dangling', 'Email.sent_by_person', %s, "
                 "'Person:graph_mail.people:{\"person_key\":\"gone\"}', 'corroborated', "
                 "'mention', '[]', '[\"pii\"]', '2026-11-16T00:00:00Z', '[]', '[]')",
                 (email("MSG-A2"),))
        broken = self.served()
        work = self.dir / "backfill"
        dry = people_scope.backfill(self.dsn, work, apply=False, workers=1)
        # the build's row of a pair To and Cc both name is kept as it cites (no change)
        self.assertEqual((dry["links"]["added"], dry["links"]["gone"],
                          dry["links"].get("changed", 0)), (1, 1, 0), dry)
        self.assertEqual(self.served(), broken)   # a dry run writes nothing
        before = self.version()
        done = people_scope.backfill(self.dsn, work, apply=True, workers=2)
        self.assertEqual((done["links"]["added"], done["links"]["gone"]), (1, 1), done)
        self.assertEqual(self.served(), base_links)
        self.assertGreater(self.version(), before)
        self.assertTrue(list(work.glob("journal-*.jsonl")))
        again = people_scope.backfill(self.dsn, self.dir / "backfill-2", apply=True,
                                      workers=1)
        self.assertEqual({k: again["links"].get(k, 0) for k in ("gone", "changed", "added")},
                         {"gone": 0, "changed": 0, "added": 0}, again)
        self.assertEqual(again["aliases"]["added"] + again["aliases"]["gone"], 0, again)
        people_scope.undo(self.dsn, work)
        self.assertEqual(self.served(), broken)
        with self.assertRaises(people_scope.Refused):
            people_scope.backfill(self.dsn, work, apply=False, types=["Invoice"])
        with self.assertRaises(people_scope.Refused):
            people_scope.backfill(self.dsn, work, apply=True, every=2)

    def break_links(self) -> list[tuple]:
        """A3's link to erin lost, a dangling link from A2: the served links then."""
        a3, lost = email("MSG-A3"), person(OUT_ONLY_B[0])
        self.sql("DELETE FROM ontology.link_rows WHERE from_object = %s AND to_object = %s",
                 (a3, lost))
        self.sql("INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, "
                 "tier, binding_kind, why_not, markings, recorded_at, reason, "
                 "evidence_handles) VALUES ('person:dangling', 'Email.sent_by_person', %s, "
                 "'Person:graph_mail.people:{\"person_key\":\"gone\"}', 'corroborated', "
                 "'mention', '[]', '[\"pii\"]', '2026-11-16T00:00:00Z', '[]', '[]')",
                 (email("MSG-A2"),))
        return self.served()

    def test_a_failed_range_never_breaks_undo_and_undo_refreshes_the_caches(self):
        """Review minors: a range whose write fails after its
        journal entry leaves an entry ``undo`` skips (its transaction aborted); ``undo``
        takes back only committed writes, records its ends as a layer (traverse facts,
        change feed, link slice, mention repeats as a write), refuses (nothing changed)
        when a later write moved a row, and a second undo finds nothing. Statements are
        bounded by default."""
        import psycopg
        from operating.pg import people_scope, whole_steps
        base = self.load_base(_messages_base())
        broken = self.break_links()
        work = self.dir / "backfill"
        real = whole_steps._record
        with mock.patch.object(whole_steps, "_record",
                               side_effect=RuntimeError("stopped mid-range")):
            with self.assertRaises(RuntimeError):
                people_scope.backfill(self.dsn, work, apply=True, workers=1)
        self.assertEqual(self.served(), broken)   # the failed write left nothing
        self.assertTrue(list(work.glob("journal-*.jsonl")))   # but its entry
        with mock.patch.object(whole_steps, "_record", side_effect=real) as recorded:
            done = people_scope.backfill(self.dsn, work, apply=True, workers=1)
        self.assertEqual((done["links"]["added"], done["links"]["gone"]), (1, 1), done)
        self.assertEqual(self.served(), _sqlite_person_links(base))
        self.assertTrue(recorded.called)
        # a later write moved a row the backfill wrote: refused, nothing changed
        self.sql("UPDATE ontology.link_rows SET tier = 'inferred' WHERE from_object = %s "
                 "AND to_object = %s", (email("MSG-A3"), person(OUT_ONLY_B[0])))
        moved = self.served()
        with self.assertRaisesRegex(people_scope.Refused, "undo_conflict"):
            people_scope.undo(self.dsn, work)
        self.assertEqual(self.served(), moved)
        self.sql("UPDATE ontology.link_rows SET tier = 'corroborated' WHERE from_object = %s "
                 "AND to_object = %s", (email("MSG-A3"), person(OUT_ONLY_B[0])))
        before = self.version()
        out = people_scope.undo(self.dsn, work)
        self.assertEqual(self.served(), broken)
        # the failed range's entry and the write's, one journal: only the write taken back
        self.assertEqual((out["deleted"], out["restored"], out["skipped_aborted"]), (1, 1, 1),
                         out)
        self.assertTrue(out["layer"].startswith(people_scope.LAYER_PREFIX + "undo-"), out)
        self.assertGreater(self.version(), before)
        with psycopg.connect(self.dsn) as pg:
            owned = {r[0] for r in pg.execute(
                "SELECT object_id FROM ontology.applied_owned WHERE layer = %s",
                (out["layer"],))}
        self.assertEqual(owned, {email("MSG-A3"), email("MSG-A2")})
        self.assertIn("mention_repeats", out)
        self.assertFalse(list(work.glob("journal-*.jsonl")))
        again = people_scope.undo(self.dsn, work)
        self.assertEqual((again["journals"], again["deleted"], again["restored"]), (0, 0, 0))
        self.assertEqual(self.served(), broken)
        # an entry whose outcome cannot be read (no transaction id): refused, unchanged
        other = self.dir / "unknown"
        other.mkdir()
        (other / "journal-00000.jsonl").write_text(json.dumps(
            {"put_back": [], "written": [["person:dangling"]]}) + "\n")
        with self.assertRaisesRegex(people_scope.Refused, "journal_status_unknown"):
            people_scope.undo(self.dsn, other)
        self.assertEqual(self.served(), broken)
        # statements bounded by default, an override read, junk falls back to the default
        self.assertEqual(people_scope.statement_ms({}), people_scope.STATEMENT_MS)
        self.assertEqual(people_scope.statement_ms(
            {"PLX_PERSON_LINKS_STATEMENT_MS": "5000"}), 5000)
        self.assertEqual(people_scope.statement_ms(
            {"PLX_PERSON_LINKS_STATEMENT_MS": "x"}), people_scope.STATEMENT_MS)
        import inspect
        self.assertEqual(inspect.signature(people_scope.backfill).parameters[
            "statement_timeout_ms"].default, people_scope.STATEMENT_MS)


    def test_undo_takes_back_the_old_releases_journals_or_refuses(self):
        """Review minor 5: the 11-18 live backfill ran on the
        release before transaction ids (journals: no ``xid``, ``written`` its ids). Such an
        entry counts as committed when its range's layer is recorded and its rows still
        show its write; a later person-links write to one of its objects, or rows showing
        a write no layer records, refuse; an entry with no layer and no effect is skipped.
        ``--dry-run`` checks it all and writes nothing."""
        from operating.pg import people_scope
        self.load_base(_messages_base())
        broken = self.break_links()
        work = self.dir / "backfill"
        people_scope.backfill(self.dsn, work, apply=True, workers=1)
        fixed = self.served()
        self.assertNotEqual(fixed, broken)

        def old_format(path: Path, entries: list[dict]) -> None:
            held = path.stat().st_mtime if path.exists() else time.time()
            path.write_text("".join(json.dumps(e) + "\n" for e in entries))
            os.utime(path, (held, held))

        for path in work.glob("journal-*.jsonl"):
            old_format(path, [{"put_back": e["put_back"], "written": [r[0] for r in
                                                                       e["written"]]}
                              for e in map(json.loads, path.read_text().splitlines())])
        dry = people_scope.undo(self.dsn, work, dry_run=True)
        self.assertEqual((dry["legacy"], dry["deleted"], dry["restored"], dry["layer"]),
                         (1, 1, 1, None), dry)
        self.assertEqual(self.served(), fixed)
        self.assertTrue(list(work.glob("journal-*.jsonl")))
        # a later person-links run changed A3's links: refused, nothing changed
        later = f"{people_scope.LAYER_PREFIX}9-{int(time.time() * 1000) + 60_000}"
        self.sql("INSERT INTO ontology.applied_owned (layer, object_id) VALUES (%s, %s)",
                 (later, email("MSG-A3")))
        with self.assertRaisesRegex(people_scope.Refused, "undo_conflict"):
            people_scope.undo(self.dsn, work)
        self.assertEqual(self.served(), fixed)
        self.sql("DELETE FROM ontology.applied_owned WHERE layer = %s", (later,))
        out = people_scope.undo(self.dsn, work)
        self.assertEqual((out["legacy"], out["deleted"], out["restored"]), (1, 1, 1), out)
        self.assertEqual(self.served(), broken)
        self.assertFalse(list(work.glob("journal-*.jsonl")))
        # no layer of its range: no effect -> skipped; its write shown -> refused
        never = self.dir / "never"
        never.mkdir()
        old_format(never / "journal-00077.jsonl", [{"put_back": [],
                                                    "written": ["person:never-written"]}])
        out = people_scope.undo(self.dsn, never)
        self.assertEqual((out["skipped_aborted"], out["deleted"]), (1, 0), out)
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            held = pg.execute("SELECT link_id FROM ontology.links WHERE link_id COLLATE "
                              "\"C\" >= 'person:' AND link_id COLLATE \"C\" < 'person;' "
                              "LIMIT 1").fetchone()[0]
        shown = self.dir / "shown"
        shown.mkdir()
        old_format(shown / "journal-00078.jsonl", [{"put_back": [], "written": [held]}])
        with self.assertRaisesRegex(people_scope.Refused, "journal_status_unknown"):
            people_scope.undo(self.dsn, shown)
        self.assertEqual(self.served(), broken)


class UnknownReadsAreRefused(unittest.TestCase):

    def test_a_cells_read_of_another_shape_is_refused(self):
        from ontology.pg_step_inplace import _local_store
        from ontology.sender_parties import _CELLS
        from operating.pg import people_scope
        with tempfile.TemporaryDirectory() as tmp:
            local = _local_store(Path(tmp) / "x.sqlite")
            try:
                conn = people_scope.ObjectsConnection(local.connection, None)
                for sql in (_CELLS.format(fields="1,2") + " AND v.object_id = 'x'",
                            _CELLS.format(fields="1,2").replace("lf.field IN", "lf.kind IN"),
                            "SELECT value FROM value_cells"):
                    with self.subTest(sql=sql[-40:]):
                        with self.assertRaises(people_scope.Refused):
                            conn.execute(sql)
                with self.assertRaises(people_scope.Refused):
                    conn.execute(_CELLS.format(fields="1"), ("param",))
            finally:
                local.abort()


if __name__ == "__main__":
    unittest.main()
