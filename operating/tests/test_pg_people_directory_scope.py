"""Outside contacts first seen after the load get their served Person
(``operating.pg.people_directory_scope``, wired in ``operating.pg.ontology_delta``).

Evidence label: ``isolated-fixture`` / ``injected-transport``: made-up mailboxes and
contacts captured from the capture tests' in-process FakeGraph, the real projector, schema,
people directory, people step, loader and layer apply, a scratch Postgres database.

Reproduces the 11-18 gap (open decision option 1): no delta layer
owns a Person, so a contact whose first mail came after the load had none served and no
mail linked to it.

* The step's Person equals the full build's for the same capture: object, version
  (citation, digest, markings, times), values, audience over every mailbox it wrote to
  (not only the layer's), and the served person links then equal the full build's,
  including mail the load already held naming the contact before it ever wrote.
* Excluded people never get a Person: a contact who wrote only to an excluded person's
  mailbox, an alias of an excluded person's address; the exclusion or the mail scope
  unavailable writes nothing and keeps the mark.
* Idempotent (a second run, a run over the same mail again: nothing written, no version
  moved), bounded (1, 10 and 60 new contacts drained over runs of at most 7 mails), and a
  run failing after its Persons were served judges their mail on the next run.

* widened (it wrote to another mailbox), narrowed (its mail in a mailbox removed) and
  removed (its last mail removed): the Person's rows and every person link equal the full
  build's after each, and the layer owns only the contacts that changed;
* an excluded person's mailbox never stays in or enters a refreshed audience (a contact
  who wrote to it and another loses it, one who wrote only to it loses its Person), the
  exclusion unavailable changes nothing; settings come from the delta's ``environ``;
* 1, 10 and 60 served contacts refreshed over runs of at most 7, rows as the full build's;
* a run stopped right after its layer committed judges the older mail naming its new
  contact on the next run (the state named it first); the naming cap counts only source
  objects (type filter before the limit).
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest import mock

from operating.tests.test_pg_people_scope import (OUT_ONLY_B, PeopleFixture, _capture,
                                                  _messages_base, _sqlite_person_links,
                                                  email, person)

NEW = "newco@new-client.example"


def _objects(rows, oid):
    return [r for r in rows if r[0] == oid]


class NewOutsidePersons(PeopleFixture):

    def setUp(self):
        super().setUp()
        from integrations.tests.test_capture_mail import SCOPE
        from operating.pg import people_directory_scope as pds
        self.pds = pds
        # the delta's mail scope file: the capture tests' internal domains
        self.enterContext(mock.patch.object(pds, "internal_domains",
                                            lambda environ: tuple(SCOPE.internal_domains)))

    # the base, then the layer of a cycle, over the capture as it is now
    def cycle(self, name: str, base_messages: list, new: list, *, box_only: str | None = None):
        messages = base_messages + new
        state = self.dir / f"{name}-now"
        _capture(state, messages)
        in_layer = [m for m in messages if box_only is None or m[0] == box_only]
        layer = self.layer(name, in_layer, [email(m[1], m[0]) for m in new
                                            if box_only is None or m[0] == box_only])
        return messages, state, layer

    def full_store(self, name: str, messages: list) -> Path:
        """The full build of ``messages`` under the exclusion in the environment now."""
        from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
        from integrations.tests.test_capture_mail import SCOPE
        from ontology import schema
        from ontology.people_links import people_links_step
        from ontology.project import MailCaptures, build_store
        from operating.tests.test_case_tools_host import NOW
        state = self.dir / f"{name}-full-mail"
        _capture(state, messages)
        path = self.dir / f"{name}-full.sqlite"
        guard = PeopleExclusion.from_env(environ_with_exclusion(os.environ))
        with MailCaptures(state, internal_domains=SCOPE.internal_domains,
                          exclusion=guard) as mail:
            build_store(path, schema.load_schema(), {"graph_mail": mail}, clock=lambda: NOW,
                        post_project={"with_people": people_links_step})
        return path

    def person_rows_pg(self, oid: str) -> dict:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            obj = pg.execute("SELECT object_type, binding_id, native_key, audience_key, "
                             "first_recorded_at, missing_from_source_since FROM "
                             "ontology.objects WHERE object_id = %s", (oid,)).fetchall()
            ver = pg.execute("SELECT l1_handle, digest, lineage, markings, recorded_at, "
                             "superseded_at FROM ontology.object_versions WHERE "
                             "object_id = %s", (oid,)).fetchall()
            values = pg.execute("SELECT property, value, markings FROM ontology.object_values "
                                "WHERE object_id = %s", (oid,)).fetchall()
        return {"objects": [tuple(map(_text, r)) for r in obj],
                "versions": [tuple(map(_text, r)) for r in ver],
                "values": sorted(tuple(map(_text, r)) for r in values)}

    @staticmethod
    def person_rows_sqlite(path: Path, oid: str) -> dict:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            obj = conn.execute("SELECT object_type, binding_id, native_key, audience_key, "
                               "first_recorded_at, missing_from_source_since FROM objects "
                               "WHERE object_id = ?", (oid,)).fetchall()
            ver = conn.execute("SELECT l1_handle, digest, lineage, markings, recorded_at, "
                               "superseded_at FROM object_versions WHERE object_id = ?",
                               (oid,)).fetchall()
            values = conn.execute("SELECT property, value, markings FROM object_values "
                                  "WHERE object_id = ?", (oid,)).fetchall()
        return {"objects": [tuple(map(_text, r)) for r in obj],
                "versions": [tuple(map(_text, r)) for r in ver],
                "values": sorted(tuple(map(_text, r)) for r in values)}

    def test_a_new_contact_gets_the_full_builds_person_and_every_link_to_it(self):
        """A contact who never wrote before the load (mailbox A wrote to it then) writes
        twice to B and once to A. Each layer holds one mailbox's mail; the Person's
        audience is both mailboxes, cited by B's (most of its mail), as the full build
        makes it."""
        from integrations.tests.test_capture_mail import BOX_A, BOX_B, UPN_A
        from ontology import people_directory as pd
        from operating.pg import ontology_delta, people_directory_scope as pds
        base_messages = _messages_base() + [(BOX_A, "MSG-A0", UPN_A, [NEW], [])]
        self.load_base(base_messages)
        oid = person(NEW)
        self.assertEqual(self.person_rows_pg(oid)["objects"], [])   # never wrote: none
        new = [(BOX_B, "MSG-BN1", NEW, ["finance@example-plx.com"], []),
               (BOX_B, "MSG-BN2", NEW, ["finance@example-plx.com"], [OUT_ONLY_B[0]]),
               (BOX_A, "MSG-AN1", NEW, [UPN_A], [])]
        # a layer per mailbox, each projected from its own mailbox's mail only
        messages, state, layer = self.cycle("d1", base_messages, new, box_only=BOX_A)
        _m, _s, layer_b = self.cycle("d1b", base_messages, new, box_only=BOX_B)
        out = ontology_delta.apply_listed([layer_b, layer], self.dsn, log=io.StringIO(),
                                          state_dir=state)
        run = out["people_directory"]
        self.assertEqual((run["persons"], run["multi_mailbox"]), (1, 1), run)
        full = self.full_store("d1", messages)
        self.assertEqual(self.person_rows_pg(oid), self.person_rows_sqlite(full, oid))
        rows = self.person_rows_pg(oid)
        self.assertEqual(rows["versions"][0][0], pd.person_handle(BOX_B, pd.address_key(NEW)))
        self.assertTrue(rows["objects"][0][3].startswith(pd.COMPOSITE), rows)   # A and B
        self.assertIn("pii", rows["versions"][0][3])
        # every person link the full build makes, the mail the load held included
        served = self.served()
        self.assertEqual(served, _sqlite_person_links(full))
        self.assertIn(("Email.addressed_to_person", email("MSG-A0"), oid),
                      {(r[0], r[1], r[2]) for r in served})
        # the census over the capture equals the directory's own for that address
        capture = pds.Capture(state)
        try:
            whole = pd.sender_counts(capture.path, [b.mailbox_id for b in capture.boxes])
            self.assertEqual(capture.census([NEW]),
                             {k: v for k, v in whole.items() if k[0] == NEW and v})
        finally:
            capture.close()

    def test_excluded_people_never_get_a_person_and_failures_change_nothing(self):
        from integrations.tests.test_capture_mail import BOX_A, BOX_B, ENV, UPN_A
        from operating.pg import ontology_delta, people_directory_scope as pds, people_scope
        base_messages = _messages_base()
        self.load_base(base_messages)
        only_b = "onlyb@excluded-only.example"
        alias = "finance+x@example-plx.com"   # the excluded mailbox's own, plus-tagged
        new = [(BOX_B, "MSG-BX1", only_b, ["finance@example-plx.com"], []),
               (BOX_A, "MSG-AX1", alias, [UPN_A], []),
               (BOX_A, "MSG-AX2", NEW, [UPN_A], [])]
        messages, state, layer = self.cycle("x1", base_messages, new)
        before = self.served()
        err = io.StringIO()
        # the exclusion unavailable: refused, nothing written, the mark stays
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": "not-an-id"}), \
                mock.patch("sys.stderr", err):
            out = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO(),
                                              state_dir=state)
        self.assertIn("exclusion_unavailable", out["people_directory"]["error"])
        self.assertIn("ONTOLOGY PEOPLE DIRECTORY FAILED", err.getvalue())
        self.assertEqual(self.person_rows_pg(person(NEW))["objects"], [])
        self.assertEqual(self.served(), before)
        # the mail scope unreadable: refused too
        with mock.patch.object(pds, "internal_domains",
                               side_effect=pds.Refused("mail_scope_unreadable")):
            with self.assertRaisesRegex(pds.Refused, "mail_scope_unreadable"):
                pds.recompute(self.dsn, state)
        # mailbox B's person excluded: who wrote only to B gets no Person, nor does an
        # alias of B's address; the full build under the same exclusion agrees
        excluded = {"PLX_EXCLUDED_PEOPLE_OIDS": f"{ENV['PLX_EXCLUDED_PEOPLE_OIDS']},{BOX_B}"}
        with mock.patch.dict(os.environ, excluded):
            run = pds.recompute(self.dsn, state)
            people_scope.recompute(self.dsn)
            full = self.full_store("x1", messages)
        self.assertEqual(run["persons"], 1, run)
        for gone in (only_b, alias):
            self.assertEqual(self.person_rows_pg(person(gone))["objects"], [])
            self.assertEqual(self.person_rows_sqlite(full, person(gone))["objects"], [])
        self.assertEqual(self.person_rows_pg(person(NEW)),
                         self.person_rows_sqlite(full, person(NEW)))

    def test_idempotent_bounded_and_a_failed_judging_is_taken_up_next_run(self):
        """1, 10 and 60 new contacts, runs of at most 7 mails: every one gets its Person
        once, links as the full build's; a second run and a run over the same mail again
        write nothing; a run failing after its Persons were served judges their mail on
        the next run."""
        from integrations.tests.test_capture_mail import BOX_A, UPN_A
        from operating.pg import ontology_delta, people_directory_scope as pds, people_scope
        base_messages = _messages_base()
        self.load_base(base_messages)
        done: list[tuple] = []
        for n in (1, 10, 60):
            with self.subTest(contacts=n):
                new = [(BOX_A, f"MSG-NC{i:03d}", f"c{i:03d}@new{i:03d}.example", [UPN_A], [])
                       for i in range(len(done), n)]
                done += new
                messages, state, layer = self.cycle(f"n{n}", base_messages, done)
                t = time.monotonic()
                with mock.patch.dict(os.environ, {"PLX_PEOPLE_DIRECTORY_RUN_MAX": "7"}):
                    out = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO(),
                                                      state_dir=state)
                    runs = [out["people_directory"]]
                    while runs[-1].get("remaining"):
                        runs.append(pds.recompute(self.dsn, state))
                    self.assertEqual(pds.recompute(self.dsn, state), {"pending": False})
                took = time.monotonic() - t
                people_scope.recompute(self.dsn)
                self.assertTrue(all(r["mails"] <= 7 for r in runs), runs)
                self.assertEqual(sum(r["persons"] for r in runs), len(new), runs)
                full = self.full_store(f"n{n}", messages)
                self.assertEqual(self.served(), _sqlite_person_links(full))
                for m in done:
                    self.assertEqual(self.person_rows_pg(person(m[2])),
                                     self.person_rows_sqlite(full, person(m[2])))
                self.assertLess(took, 180, runs)
        # the same mail judged again from the start: nothing written, no version moved
        self.sql("UPDATE ontology.store_meta SET value = %s WHERE key = %s",
                 (json.dumps({"through": 0}), pds.STATE))
        before, served = self.version(), self.served()
        again = pds.recompute(self.dsn, state)
        self.assertEqual((again["persons"], again.get("layer")), (0, None), again)
        self.assertEqual((self.version(), self.served()), (before, served))
        # the judging fails after the Persons were served: the next run judges their mail
        late = "late@late-client.example"
        first = [(BOX_A, "MSG-LATE0", UPN_A, [late], [])]       # A wrote to it first
        then = [(BOX_A, "MSG-LATE1", late, [UPN_A], [])]
        known = base_messages + done
        early = self.layer("late-early", known + first, [email("MSG-LATE0")])
        ontology_delta.apply_listed([early], self.dsn, log=io.StringIO())
        messages, state, layer = self.cycle("late", known + first, then)
        ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO())   # no directory
        with mock.patch.object(people_scope, "rejudge", side_effect=RuntimeError("stopped")):
            with self.assertRaises(RuntimeError):
                pds.recompute(self.dsn, state)
        self.assertEqual(len(self.person_rows_pg(person(late))["objects"]), 1)
        run = pds.recompute(self.dsn, state)
        self.assertEqual(run["naming_left_over"], 1, run)
        people_scope.recompute(self.dsn)
        named = {(r[0], r[1], r[2]) for r in self.served()}
        self.assertIn(("Email.addressed_to_person", email("MSG-LATE0"), person(late)), named)
        self.assertIn(("Email.sent_by_person", email("MSG-LATE1"), person(late)), named)
        self.assertEqual(self.served(), _sqlite_person_links(self.full_store("late",
                                                                             messages)))

    # ------------------------------------------------------------ audience refresh

    def removed(self, name: str, messages: list, gone: list[tuple]) -> tuple:
        """The capture without ``gone`` (box, id) and a layer removing their mail."""
        keep = [m for m in messages if (m[0], m[1]) not in set(gone)]
        state = self.dir / f"{name}-now"
        _capture(state, keep)
        return keep, state, self.layer(name, keep, [email(i, b) for b, i in gone])

    def owned_by(self, layer: str) -> set:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return {r[0] for r in pg.execute("SELECT object_id FROM ontology.applied_owned "
                                             "WHERE layer = %s", (layer,))}

    def test_a_served_contacts_audience_follows_its_mail_as_a_full_build_does(self):
        """erin (served, wrote only to B) writes twice to A: widened to A and B, cited by
        A; her mail in B removed: narrowed to A; her last mail removed: no Person, no link
        to her (A3 names her). After each, her rows and every person link are the full
        build's of the capture then, and the layer owns her alone."""
        from integrations.tests.test_capture_mail import BOX_A, BOX_B, UPN_A
        from ontology import people_directory as pd
        from operating.pg import ontology_delta
        erin = OUT_ONLY_B[0]
        base_messages = _messages_base()
        self.load_base(base_messages)
        oid = person(erin)
        self.assertEqual(self.person_rows_pg(oid)["objects"][0][3], f"mail:{BOX_B}")
        dave = self.person_rows_pg(person(OUT_ONLY_B[1]))
        wrote = [(BOX_A, "MSG-EA1", erin, [UPN_A], []), (BOX_A, "MSG-EA2", erin, [UPN_A], [])]
        messages, state, layer = self.cycle("w", base_messages, wrote, box_only=BOX_A)
        run = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO(),
                                          state_dir=state)["people_directory"]
        self.assertEqual((run["persons"], run["refreshed"], run["removed"],
                          run["counts"].get("widened")), (0, 1, 0, 1), run)
        self.assertEqual(self.owned_by(run["layer"]), {oid})
        full = self.full_store("w", messages)
        rows = self.person_rows_pg(oid)
        self.assertEqual(rows, self.person_rows_sqlite(full, oid))
        self.assertTrue(rows["objects"][0][3].startswith(pd.COMPOSITE), rows)
        self.assertEqual(rows["versions"][0][0], pd.person_handle(BOX_A, pd.address_key(erin)))
        self.assertEqual(self.served(), _sqlite_person_links(full))
        self.assertEqual(self.person_rows_pg(person(OUT_ONLY_B[1])), dave)   # untouched
        # narrowed: her mail in B removed (the capture holds it no more: her served link
        # from it names her)
        messages, state, layer = self.removed("n", messages, [(BOX_B, "MSG-B2")])
        run = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO(),
                                          state_dir=state)["people_directory"]
        self.assertEqual((run["refreshed"], run["counts"].get("narrowed")), (1, 1), run)
        self.assertEqual(self.owned_by(run["layer"]), {oid})
        full = self.full_store("n", messages)
        self.assertEqual(self.person_rows_pg(oid), self.person_rows_sqlite(full, oid))
        self.assertEqual(self.person_rows_pg(oid)["objects"][0][3], f"mail:{BOX_A}")
        self.assertEqual(self.served(), _sqlite_person_links(full))
        # removed: her last mail gone, the full build holds no Person of her
        messages, state, layer = self.removed("r", messages, [(BOX_A, "MSG-EA1"),
                                                              (BOX_A, "MSG-EA2")])
        run = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO(),
                                          state_dir=state)["people_directory"]
        self.assertEqual((run["refreshed"], run["removed"]), (0, 1), run)
        full = self.full_store("r", messages)
        self.assertEqual(self.person_rows_pg(oid), self.person_rows_sqlite(full, oid))
        self.assertEqual(self.person_rows_pg(oid)["objects"], [])
        self.assertEqual(self.served(), _sqlite_person_links(full))
        self.assertFalse([r for r in self.served() if r[2] == oid])
        # nothing left to do: a run judges nothing again, writes nothing
        before = self.version()
        self.assertEqual(self.pds.recompute(self.dsn, state), {"pending": False})
        self.assertEqual(self.version(), before)

    def test_an_excluded_persons_mailbox_leaves_every_refreshed_audience(self):
        """Mailbox B's person becomes excluded. xena (wrote to A once, B twice: cited by B)
        writes to A again: her audience is A alone, cited by A, never B; erin (wrote only
        to B) writes to B again: her Person goes. Both as the full build under the same
        exclusion. The exclusion unavailable (in the delta's own settings, the process's
        fine) changes nothing."""
        from integrations.tests.test_capture_mail import BOX_A, BOX_B, ENV, UPN_A, UPN_B
        from ontology import people_directory as pd
        from operating.pg import ontology_delta
        xena, erin = "xena@partner-x.example", OUT_ONLY_B[0]
        base_messages = _messages_base() + [
            (BOX_A, "MSG-XA1", xena, [UPN_A], []), (BOX_B, "MSG-XB1", xena, [UPN_B], []),
            (BOX_B, "MSG-XB2", xena, [UPN_B], [])]
        self.load_base(base_messages)
        before = {p: self.person_rows_pg(person(p)) for p in (xena, erin)}
        self.assertEqual(before[xena]["versions"][0][0],
                         pd.person_handle(BOX_B, pd.address_key(xena)))
        new = [(BOX_A, "MSG-XA2", xena, [UPN_A], []), (BOX_B, "MSG-EB3", erin, [UPN_B], [])]
        messages, state, layer = self.cycle("e", base_messages, new)
        excluded = {"PLX_EXCLUDED_PEOPLE_OIDS": f"{ENV['PLX_EXCLUDED_PEOPLE_OIDS']},{BOX_B}"}
        # the delta's own settings without a usable exclusion: refused, nothing changed
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            out = ontology_delta.apply_listed(
                [layer], self.dsn, log=io.StringIO(), state_dir=state,
                environ=dict(os.environ, PLX_EXCLUDED_PEOPLE_OIDS="not-an-id"))
        self.assertIn("exclusion_unavailable", out["people_directory"]["error"])
        self.assertIn("exclusion_unavailable", out["person_links"]["error"])
        self.assertEqual({p: self.person_rows_pg(person(p)) for p in (xena, erin)}, before)
        run = self.pds.recompute(self.dsn, state, environ=dict(os.environ, **excluded))
        self.assertEqual((run["refreshed"], run["removed"]), (1, 1), run)
        with mock.patch.dict(os.environ, excluded):
            full = self.full_store("e", messages)
        for p in (xena, erin):
            self.assertEqual(self.person_rows_pg(person(p)),
                             self.person_rows_sqlite(full, person(p)))
        rows = self.person_rows_pg(person(xena))
        self.assertEqual(rows["objects"][0][3], f"mail:{BOX_A}")
        self.assertEqual(rows["versions"][0][0], pd.person_handle(BOX_A, pd.address_key(xena)))
        self.assertEqual(self.person_rows_pg(person(erin))["objects"], [])
        from operating.pg import people_scope
        people_scope.recompute(self.dsn, environ=dict(os.environ, **excluded))
        self.assertFalse([r for r in self.served() if r[2] == person(erin)])

    def test_a_contact_holding_a_row_the_layer_would_not_write_is_held_back(self):
        """A served contact some link of no whole-store step names (none on live 11-18):
        re-owning it would delete that link, so it is held back, counted, unchanged."""
        import psycopg
        from integrations.tests.test_capture_mail import BOX_A, UPN_A
        from operating.pg import ontology_delta
        erin = OUT_ONLY_B[0]
        base_messages = _messages_base()
        self.load_base(base_messages)
        self.sql("INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, "
                 "tier, binding_kind, why_not, markings, recorded_at, reason, "
                 "evidence_handles) VALUES ('owner:held', 'MCApproval.requested_by_person', "
                 "%s, %s, 'corroborated', 'mention', '[]', '[\"pii\"]', "
                 "'2026-11-16T00:00:00Z', '[]', '[]')", (email("MSG-A1"), person(erin)))
        rows = self.person_rows_pg(person(erin))
        wrote = [(BOX_A, "MSG-EA1", erin, [UPN_A], [])]
        messages, state, layer = self.cycle("h", base_messages, wrote)
        run = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO(),
                                          state_dir=state)["people_directory"]
        self.assertEqual((run["refreshed"], run["counts"].get("refresh_held_foreign")),
                         (0, 1), run)
        self.assertEqual(self.person_rows_pg(person(erin)), rows)
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute("SELECT count(*) FROM ontology.link_rows WHERE "
                                        "link_id = 'owner:held'").fetchone()[0], 1)

    def test_refreshes_are_bounded_and_lose_no_contact(self):
        """1, 10 and 60 served contacts (each wrote only to B) write to A, at most 7
        refreshed a run (the delta's settings): every one widened once, as the full
        build's."""
        from integrations.tests.test_capture_mail import BOX_A, UPN_A
        from operating.pg import ontology_delta
        base_messages = _messages_base(60)
        self.load_base(base_messages)
        settings = dict(os.environ, PLX_PEOPLE_DIRECTORY_REFRESH_MAX="7")
        done: list[tuple] = []
        for n in (1, 10, 60):
            with self.subTest(contacts=n):
                new = [(BOX_A, f"MSG-RA{i:03d}", f"contact{i:03d}@far{i:03d}.example",
                        [UPN_A], []) for i in range(len(done), n)]
                done += new
                messages, state, layer = self.cycle(f"r{n}", base_messages, done)
                t = time.monotonic()
                out = ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO(),
                                                  state_dir=state, environ=settings)
                runs = [out["people_directory"]]
                while runs[-1].get("remaining"):
                    runs.append(self.pds.recompute(self.dsn, state, environ=settings))
                took = time.monotonic() - t
                from operating.pg import people_scope
                people_scope.recompute(self.dsn)
                self.assertTrue(all(r.get("refreshed", 0) + r.get("removed", 0) <= 7
                                    for r in runs), runs)
                self.assertEqual(sum(r.get("refreshed", 0) for r in runs), len(new), runs)
                self.assertEqual(sum(r["counts"].get("widened", 0) for r in runs), len(new))
                full = self.full_store(f"r{n}", messages)
                for m in done:
                    self.assertEqual(self.person_rows_pg(person(m[2])),
                                     self.person_rows_sqlite(full, person(m[2])))
                self.assertEqual(self.served(), _sqlite_person_links(full))
                self.assertLess(took, 180, runs)

    def test_a_run_stopped_after_its_layer_committed_judges_the_older_mail_next(self):
        """Review minor (6891d625ff): the layer applied, then the run stopped before it
        recorded its new contact. The state named it before the apply, so the next run
        judges the mail A sent it before it ever wrote."""
        from integrations.tests.test_capture_mail import BOX_A, UPN_A
        from operating.pg import ontology_delta, people_scope
        late = "late@late-client.example"
        base_messages = _messages_base() + [(BOX_A, "MSG-LATE0", UPN_A, [late], [])]
        self.load_base(base_messages)
        then = [(BOX_A, "MSG-LATE1", late, [UPN_A], [])]
        messages, state, layer = self.cycle("late", base_messages, then)
        ontology_delta.apply_listed([layer], self.dsn, log=io.StringIO())   # no directory
        real = ontology_delta.apply_layer

        def stopped(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("stopped after the layer committed")
        with mock.patch.object(ontology_delta, "apply_layer", side_effect=stopped):
            with self.assertRaises(RuntimeError):
                self.pds.recompute(self.dsn, state)
        self.assertEqual(len(self.person_rows_pg(person(late))["objects"]), 1)
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            held = json.loads(pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                                         "%s", (self.pds.STATE,)).fetchone()[0])
        self.assertEqual(held["naming"], [person(late)])
        run = self.pds.recompute(self.dsn, state)
        self.assertEqual((run["naming_left_over"], run["persons"]), (1, 0), run)
        people_scope.recompute(self.dsn)
        named = {(r[0], r[1], r[2]) for r in self.served()}
        self.assertIn(("Email.addressed_to_person", email("MSG-LATE0"), person(late)), named)
        self.assertEqual(self.served(), _sqlite_person_links(self.full_store("late",
                                                                             messages)))

    def test_the_naming_cap_counts_only_source_objects(self):
        """Review minor (6891d625ff): the source types are filtered before the limit, so
        objects of other types naming an address never hide one past the cap."""
        from collections import Counter
        import psycopg
        from integrations.tests.test_capture_mail import BOX_A, UPN_A
        from operating.pg import people_scope
        erin = OUT_ONLY_B[0]
        self.load_base(_messages_base() + [(BOX_A, f"MSG-N{i}", UPN_A, [erin], [])
                                           for i in range(3)])
        # A3 (naming erin) made an object of a type the person step never reads
        self.sql("UPDATE ontology.object_versions SET object_id = 'Note:x:1' WHERE "
                 "object_id = %s", (email("MSG-A3"),))
        ctx = people_scope.context()
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            whole = self.pds.naming(cur, ctx, [erin], Counter())
            for cap in (len(whole) - 1, len(whole)):
                counts = Counter()
                with mock.patch.object(self.pds, "NAMING_MAX", cap):
                    got = self.pds.naming(cur, ctx, [erin], counts)
                self.assertEqual(len(got), cap)
                self.assertEqual(counts["naming_capped"], int(cap < len(whole)))
        self.assertEqual(len(whole), 4)   # MSG-N0..2 and B2 (from her), never the Note
        self.assertTrue(all(o.startswith("Email:") for o in whole), whole)



def _text(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return str(value)


if __name__ == "__main__":
    unittest.main()
