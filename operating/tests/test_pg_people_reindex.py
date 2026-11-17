"""The queued people-directory ``Person`` objects (``ontology.reproject``) re-indexed from
their served values (``operating.pg.people_reindex``): a person the current exclusion
clears leaves the queue and is served exactly as before (same audience, markings and
values; no text row written), an excluded, undecided or unreadable one stays queued and
untouched, and the run refuses on digests or an unavailable guard."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from operating.tests import pg_testdb

MIGRATIONS = sorted(p for p in (Path(__file__).resolve().parents[1] / "pg" / "migrations")
                    .glob("*.sql") if p.name >= "0006")
S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
EXCLUDED = "99999999-9999-9999-9999-999999999999"
MAILBOXES = [S1, S2, EXCLUDED]
TRAIN = "4e29586947120ceb-1794348919509"
FIELDS = {"row.object_id": 1, "row.upn": 2, "row.addresses": 3, "row.kind": 4}
TABLES = ("objects", "object_versions", "value_sources", "value_cells", "value_fields",
          "value_kinds", "text_fields", "text_chunks", "link_rows", "store_meta",
          "identifiers")
STEPS = "steps:bf06102ee41fcc0000"


def composite(ids):
    from ontology.people_directory import audience_key
    return audience_key(ids, MAILBOXES)


def outside(address):
    from ontology.people_directory import address_key, person_handle
    return person_handle("people", address_key(address))


def staff(oid):
    from ontology.people_directory import person_handle
    return person_handle(oid, oid)


def guard(ids=(EXCLUDED,), unavailable=None):
    from integrations.people_exclusion import PeopleExclusion
    from operating.chunking.permissions import Exclusion
    people = PeopleExclusion(unavailable=unavailable) if unavailable else PeopleExclusion(ids)
    return Exclusion(people, directory={"ann@plx.example": S1, "bob@plx.example": S2,
                                        "eve@plx.example": EXCLUDED},
                     internal_domains=["plx.example"])


def ok(_dsn):
    return {"ok": True}


class PeopleReindexTests(unittest.TestCase):

    def setUp(self):
        import psycopg
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
            for path, field in FIELDS.items():
                pg.execute("INSERT INTO ontology.value_fields VALUES (%s, %s, %s)",
                           (field, path[len("row."):], path))
            pg.execute("INSERT INTO ontology.value_kinds (kind, value_type, parser, "
                       "markings) VALUES (1, 'text', 'trim', '[]')")
            pg.execute("INSERT INTO ontology.store_meta VALUES ('schema_digest', "
                       "'sha256:0ec1c5e7c8d4'), ('steps_digest', %s)", (STEPS,))
        self.work = Path(tempfile.mkdtemp(prefix="people-reindex-"))
        self.n = 0

    def person(self, handle, audience, *, upn=None, oid=None, addresses=(), raw=None,
               object_type="Person", binding="graph_mail.people", migration=TRAIN):
        import psycopg
        self.n += 1
        object_id = f"p{self.n:05d}"
        values = {"row.kind": "staff" if oid else "outside", "row.object_id": oid,
                  "row.upn": upn,
                  "row.addresses": raw if raw is not None else json.dumps(list(addresses))}
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                       "native_key, audience_key, first_recorded_at) VALUES "
                       "(%s, %s, %s, %s, %s, '2026-11-10T00:00:00Z')",
                       (object_id, object_type, binding, object_id, audience))
            pg.execute("INSERT INTO ontology.object_versions VALUES (%s, %s, %s, 'd', 'l', "
                       "'[\"pii\"]', '2026-11-10T00:00:00Z', NULL)",
                       (self.n, object_id, handle))
            pg.execute("INSERT INTO ontology.value_sources VALUES (%s, %s, %s, NULL, NULL, "
                       "'2026-11-10T00:00:00Z', NULL)", (self.n, self.n, binding))
            for path, value in values.items():
                if value is not None:
                    pg.execute("INSERT INTO ontology.value_cells (src, field, kind, value) "
                               "VALUES (%s, %s, 1, %s)", (self.n, FIELDS[path], value))
            pg.execute("INSERT INTO ontology.reproject (object_id, migration) VALUES "
                       "(%s, %s)", (object_id, migration))
        return object_id

    def queued(self):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return {r[0] for r in pg.execute("SELECT object_id FROM ontology.reproject")}

    def served(self):
        """Every served table but the queue, as a digest per table (what a reader sees)."""
        import psycopg
        out = {}
        with psycopg.connect(self.dsn) as pg:
            for table in TABLES:
                rows = pg.execute(f"SELECT t::text FROM ontology.{table} t").fetchall()
                out[table] = hashlib.sha256("\n".join(sorted(r[0] for r in rows))
                                            .encode()).hexdigest()
        return out

    def run_(self, apply=True, exclusion=None, workers=1, **kw):
        from operating.pg import people_reindex
        kw.setdefault("check", ok)
        return people_reindex.run(self.dsn, self.work, exclusion=exclusion or guard(),
                                  mailboxes=MAILBOXES, apply=apply, workers=workers, **kw)

    def test_an_excluded_or_undecided_person_stays_queued_and_a_cleared_one_is_served_as_before(self):
        clear = {
            self.person(staff(S1), f"mail:{S1}", oid=S1, upn="ann@plx.example",
                        addresses=["ann@plx.example"]),
            self.person(outside("vendor@acme.example"), composite([S1, S2]),
                        addresses=["vendor@acme.example"])}
        stays = {
            # the audience unit is an excluded person's mailbox
            self.person(outside("a@acme.example"), f"mail:{EXCLUDED}",
                        addresses=["a@acme.example"]),
            # a composite naming an excluded person's mailbox
            self.person(outside("b@acme.example"), composite([S1, EXCLUDED]),
                        addresses=["b@acme.example"]),
            # an excluded staff person (its handle's id), on a cleared unit
            self.person(staff(EXCLUDED), f"mail:{S2}", oid=EXCLUDED),
            # an excluded person's address in the directory, on a cleared unit
            self.person(outside("eve@plx.example"), f"mail:{S1}",
                        addresses=["eve@plx.example"]),
            # an address at our own domain no mailbox holds: undecided
            self.person(outside("who@plx.example"), f"mail:{S1}",
                        addresses=["who@plx.example"]),
            # unreadable addresses
            self.person(outside("c@acme.example"), f"mail:{S1}", raw="not json"),
            # no identity at all
            self.person(outside("d@acme.example"), f"mail:{S1}"),
            # a composite that no longer decodes against the mailboxes now
            self.person(outside("e@acme.example"),
                        composite([S1, S2]).rsplit(".", 1)[0] + ".0000000000000000",
                        addresses=["e@acme.example"]),
            # an unknown audience family
            self.person(outside("f@acme.example"), f"teams:chat:{S1}",
                        addresses=["f@acme.example"]),
            # no person handle
            self.person("graph-mail://people/message/x", f"mail:{S1}",
                        addresses=["g@acme.example"])}
        other = self.person("graph-mail://x/msg/1", f"mail:{S1}", object_type="SupplierInvoice",
                            binding="graph_mail.supplier_invoices")
        before = self.served()

        dry = self.run_(apply=False)
        self.assertEqual(self.queued(), clear | stays | {other})
        self.assertEqual((dry["queued"], dry["would_clear"], dry["cleared"]), (12, 2, 0))

        report = self.run_()
        self.assertEqual(self.queued(), stays | {other})
        self.assertEqual(report["cleared"], 2)
        self.assertEqual(report["unit_excluded_or_undecided"], 4)
        self.assertEqual(report["identity_excluded_or_undecided"], 3)
        self.assertEqual((report["identity_unreadable"], report["no_identity"],
                          report["not_person_handle"]), (1, 1, 1))
        self.assertEqual(report["text_rows_written"], 0)
        # nothing a reader sees moved: same audiences, markings, values; no text row
        self.assertEqual(self.served(), before)
        self.assertNotIn("p0", json.dumps(report))   # counts only, never who

        logged = sorted(json.loads(line)["object_id"] for path in self.work.glob("undo-*")
                        for line in path.read_text().splitlines())
        self.assertEqual(logged, sorted(clear))
        from operating.pg import people_reindex
        self.assertEqual(people_reindex.undo(self.dsn, self.work)["restored"], 2)
        self.assertEqual(self.queued(), clear | stays | {other})

    def test_a_cleared_person_is_found_under_the_same_audience_and_its_queue_entry_clears(self):
        import psycopg
        pid = self.person(outside("vendor@acme.example"), composite([S1, S2]),
                          addresses=["vendor@acme.example"])
        read = ("SELECT o.audience_key, v.markings, c.value FROM ontology.objects o "
                "JOIN ontology.object_versions v ON v.object_id = o.object_id "
                "JOIN ontology.value_sources s ON s.ver = v.ver "
                "JOIN ontology.value_cells c ON c.src = s.src WHERE c.field = 3 "
                "AND c.value LIKE '%%vendor@acme.example%%' AND v.superseded_at IS NULL")
        with psycopg.connect(self.dsn) as pg:
            found = pg.execute(read).fetchall()
        self.run_()
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute(read).fetchall(), found)
            self.assertEqual(found[0][0], composite([S1, S2]))
            self.assertEqual(pg.execute("SELECT count(*) FROM ontology.text_fields")
                             .fetchone()[0], 0)
        self.assertNotIn(pid, self.queued())

    def test_refused_when_the_guard_or_the_digests_do_not_hold(self):
        from operating.pg.people_reindex import Refused
        pid = self.person(staff(S1), f"mail:{S1}", oid=S1, upn="ann@plx.example")
        with self.assertRaisesRegex(Refused, "exclusion_unavailable"):
            self.run_(exclusion=guard(unavailable="excluded_people_oids_absent"))
        with self.assertRaisesRegex(Refused, "exclusion_unavailable"):
            from operating.pg import people_reindex
            people_reindex.run(self.dsn, self.work, exclusion=None, mailboxes=MAILBOXES,
                               apply=True, check=ok)
        with self.assertRaisesRegex(Refused, "served_digests:postgres_holds_no_whole"):
            self.run_(check=None)    # the real deploy check: no whole generation here
        with self.assertRaisesRegex(Refused, "served_steps_digest_not_expected"):
            self.run_(expect_steps="steps:00000000")
        with self.assertRaisesRegex(Refused, "person_index_rows_expected:search.entities"):
            with mock.patch.dict("operating.chunking.entities.ENTITY_TYPES",
                                 {"Person": ("person", ("upn",))}):
                self.run_()
        # the command line: an apply after a dry run of other digests, and one whose
        # exclusion state (the mail capture) cannot be read, refuse before any read
        import contextlib
        import io
        from operating.pg import people_reindex
        other = self.work / "other.json"
        other.write_text(json.dumps({"served": {"steps_digest": "steps:00000000",
                                                "schema_digest": "sha256:0ec1"}}))
        for report, want in ((other, "dry_run_of_other_digests"),
                             (self.work / "absent.json", "dry_run_report_unreadable"),
                             (None, "mail_capture_unreadable")):
            out = io.StringIO()
            argv = ["run", "--dsn", self.dsn, "--state-dir", str(self.work / "no-state"),
                    "--work", str(self.work), "--apply", "--expect-steps", "steps:bf06102e"]
            with contextlib.redirect_stdout(out):
                code = people_reindex.main(argv + (["--after-dry-run", str(report)]
                                                   if report else []))
            self.assertEqual((code, json.loads(out.getvalue())), (1, {"refused": want}))
        self.assertEqual(self.queued(), {pid})
        report = self.run_(expect_steps="steps:bf06102e")
        self.assertEqual((report["cleared"], self.queued()), (1, set()))

    def test_a_person_changed_between_judging_and_clearing_stays_queued(self):
        import psycopg
        from operating.pg import people_reindex
        pid = self.person(staff(S1), f"mail:{S1}", oid=S1, upn="ann@plx.example")
        real = people_reindex.verdict

        def moved(row, *args):
            with psycopg.connect(self.dsn, autocommit=True) as pg:   # a delta cycle lands
                pg.execute("UPDATE ontology.objects SET audience_key = %s WHERE "
                           "object_id = %s", (composite([S1, EXCLUDED]), pid))
            return real(row, *args)

        with mock.patch.object(people_reindex, "verdict", moved):
            report = self.run_()
        self.assertEqual((report["cleared"], report["changed_since_read"]), (0, 1))
        self.assertEqual(self.queued(), {pid})

    def test_every_person_clears_once_at_any_count_across_ranges(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                ids = {self.person(outside(f"v{n}-{i}@acme.example"),
                                   composite([S1, S2]) if i % 2 else f"mail:{S2}",
                                   addresses=[f"v{n}-{i}@acme.example"]) for i in range(n)}
                held = self.person(outside(f"x{n}@acme.example"), f"mail:{EXCLUDED}",
                                   addresses=[f"x{n}@acme.example"])
                report = self.run_(workers=4)
                self.assertEqual(report["ranges"], 4)
                self.assertEqual(report["cleared"], n)
                self.assertTrue(held in self.queued() and not ids & self.queued())
                again = self.run_(workers=4)   # a rerun resumes: nothing left to clear
                self.assertEqual(again["cleared"], 0)


if __name__ == "__main__":
    unittest.main()
