"""Sender-party links kept right over delta layers applied to Postgres
(``operating.pg.sender_scope``, ``operating.pg.ontology_delta``).

Evidence label: ``isolated-fixture``: the sender step's own synthetic Portal parties and
messages (``ontology.tests.test_sender_parties``), the real projector, schema and step, the
loader, a scratch Postgres database.

Reproduces a gap of the delta layers: one judges a new message's sender only against the
parties its closure holds, and a party holding the sender's domain is none of them; served
mail from a party's domain was linked 10-20% a day after the layers began (55-60% before).

* The messages a layer replaced are judged against the whole served directory: the served
  links are the full build's, rows and markings (1, 10 and 60 messages, in chunks); a pair a
  person rejected stays unlinked; a link the whole directory does not make is dropped.
* Runs are idempotent: a second run has nothing pending, a re-judge writes nothing and
  moves no data version; a dry run writes nothing.
* A party record a layer replaced: the messages linked to it are judged again.
* The first run only sets the mark; an object of no message type is left alone.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from operating.tests import pg_testdb
from operating.tests.test_pg_ontology_delta import MIGRATIONS

SENDPARTY = "link_id COLLATE \"C\" >= 'sendparty:' AND link_id COLLATE \"C\" < 'sendparty;'"


def _store(path: Path, extra: int = 0) -> Path:
    """The sender step's fixture built in full (its links: what every run must serve), with
    ``extra`` more messages from Acme's domain."""
    from ontology import identifiers, schema
    from ontology import sender_parties as sp
    from ontology.project import build_store
    from ontology.tests import test_sender_parties as tsp
    mail = {"messages": list(tsp.MAIL["messages"]) + [
        tsp.message(100 + i, f"rep{i}@acme-poly.example") for i in range(extra)]}
    build_store(path, schema.load_schema(),
                {"portal_api": tsp.Source("portal_api", tsp.PORTAL),
                 "graph_mail": tsp.Source("graph_mail", mail)},
                normalizer=identifiers.normalize,
                clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
                post_project={"mentions": tsp.name_orders, "reviews": tsp.reject,
                              "sender_parties": sp.sender_parties_step})
    return path


def _full_links(path: Path) -> list[tuple]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return sorted(conn.execute(
            "SELECT link_id, link_type, from_object, to_object, tier, markings FROM links "
            "WHERE link_id >= 'sendparty:' AND link_id < 'sendparty;'"))


class SenderFixture(unittest.TestCase):

    def setUp(self):
        import psycopg
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def load(self, extra: int = 0) -> list[tuple]:
        from operating.pg import ontology_load
        store = _store(self.dir / f"store-{extra}.sqlite", extra)
        ontology_load.load(store, self.dsn, workers=1, replace=True, log=io.StringIO())
        return _full_links(store)

    def served(self) -> list[tuple]:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return sorted(tuple(r) for r in pg.execute(
                "SELECT link_id, link_type, from_object, to_object, tier, markings FROM "
                f"ontology.links WHERE superseded_at IS NULL AND {SENDPARTY}"))

    def sql(self, statement: str, params=()) -> None:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            pg.execute(statement, params)

    def version(self) -> int:
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return pg.execute("SELECT ontology.sq_data_version()").fetchone()[0]

    def replaced(self, name: str, objects: list[str]) -> None:
        """A delta layer's record of the objects it replaced (``applied_owned``)."""
        import psycopg
        from operating.pg import ontology_migrate
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            ontology_migrate.log_owned(cur, name, sorted(objects))

    def lose(self, where: str = "TRUE") -> None:
        """The layers' judgement: the sender links of the messages they hold dropped."""
        self.sql(f"DELETE FROM ontology.link_rows WHERE {SENDPARTY} AND tier <> "
                 f"'contradicted' AND {where}")


class SenderLinksOverLayers(SenderFixture):

    def test_messages_a_layer_replaced_are_linked_as_a_full_build_links_them(self):
        """1, 10 and 60 messages (chunks of 7, at most 20 a run): the full build's rows,
        none lost or twice, the rejected pair never linked."""
        from operating.pg import sender_scope
        from ontology.tests import test_sender_parties as tsp
        full = self.load(extra=60)
        self.assertTrue(full)
        self.assertEqual(sender_scope.recompute(self.dsn)["pending"], False)   # the mark
        self.assertEqual(self.served(), full)
        extra = [tsp.email(100 + i) for i in range(60)]
        done = 0
        for n in (1, 10, 60):
            with self.subTest(messages=n):
                part = extra[done:n] + ([tsp.email(i) for i in range(1, 12)] if n == 60 else [])
                done = n
                self.sql(f"DELETE FROM ontology.link_rows WHERE {SENDPARTY} AND tier <> "
                         "'contradicted' AND from_object = ANY(%s)", (part,))
                self.assertNotEqual(self.served(), full)
                self.replaced(f"delta:n{n}", part)
                runs = []
                with mock.patch.object(sender_scope, "CHUNK", 7), \
                        mock.patch.dict("os.environ", {"PLX_SENDER_LINKS_RUN_MAX": "20"}):
                    runs.append(sender_scope.recompute(self.dsn))
                    while runs[-1].get("remaining"):
                        runs.append(sender_scope.recompute(self.dsn))
                    self.assertEqual(sender_scope.recompute(self.dsn), {"pending": False})
                self.assertEqual(sum(r["objects"] for r in runs), len(part), runs)
                self.assertEqual(self.served(), full)
        served = self.served()
        self.assertFalse([r for r in served if r[2] == tsp.email(7)], served)
        self.assertEqual(len([r for r in served if r[2] in extra]), 60)

    def test_a_link_the_whole_directory_does_not_make_is_dropped(self):
        """A layer that saw only one of the two parties holding a domain linked the
        message to it; the run judges it against both and drops the link."""
        from operating.pg import sender_scope
        from ontology.tests import test_sender_parties as tsp
        full = self.load()
        sender_scope.recompute(self.dsn)
        bogus = "sendparty:layer-saw-one-party"
        self.sql("INSERT INTO ontology.link_rows SELECT (jsonb_populate_record(NULL::"
                 "ontology.link_rows, to_jsonb(l) || jsonb_build_object('link_id', %s::text, "
                 "'from_object', %s::text))).* FROM ontology.link_rows l WHERE link_id = %s",
                 (bogus, tsp.email(5), full[0][0]))
        self.replaced("delta:bogus", [tsp.email(5)])
        run = sender_scope.recompute(self.dsn)
        self.assertEqual(run["links"]["gone"], 1, run)
        self.assertEqual(self.served(), full)

    def test_runs_are_idempotent_and_a_dry_run_writes_nothing(self):
        from operating.pg import sender_scope
        from ontology.tests import test_sender_parties as tsp
        full = self.load()
        sender_scope.recompute(self.dsn)
        messages = [tsp.email(i) for i in range(1, 12)]
        before = self.version()
        again = sender_scope.rejudge(self.dsn, messages)
        self.assertEqual({k: v for k, v in again["links"].items() if v}, {"kept": len(full)},
                         again)
        self.assertEqual(again["layers"], 0)
        self.assertEqual(self.version(), before)
        self.lose()
        dry = sender_scope.rejudge(self.dsn, messages, dry_run=True)
        self.assertEqual(dry["links"]["added"], len(full), dry)
        self.assertEqual(self.served(), [])
        journal = self.dir / "journal.jsonl"
        wet = sender_scope.rejudge(self.dsn, messages, journal=journal)
        self.assertEqual(wet["links"]["added"], len(full), wet)
        self.assertEqual(self.served(), full)
        self.assertTrue(journal.read_text().strip())

    def test_a_replaced_party_rejudges_the_messages_linked_to_it(self):
        """A layer replacing a party record judged its messages against its own closure;
        the run judges every message still linked to that party again."""
        from operating.pg import sender_scope
        from ontology.tests import test_sender_parties as tsp
        full = self.load()
        sender_scope.recompute(self.dsn)
        to_acme = sorted({r[2] for r in full if r[3] == tsp.ACME})
        self.assertEqual(to_acme, [tsp.email(1)])
        # the layer's judgement of message 1 changed its link (another tier) under Acme
        self.sql(f"UPDATE ontology.link_rows SET tier = 'corroborated' WHERE {SENDPARTY} AND "
                 "from_object = %s", (tsp.email(1),))
        self.assertNotEqual(self.served(), full)
        self.replaced("delta:acme", [tsp.ACME])
        run = sender_scope.recompute(self.dsn)
        self.assertEqual((run["parties_touched"], run["objects"]), (1, 1), run)
        self.assertEqual(run["links"]["changed"], 1, run)
        self.assertEqual(self.served(), full)

    def test_an_object_of_no_message_type_is_left_alone(self):
        from operating.pg import sender_scope
        from ontology.tests import test_sender_parties as tsp
        full = self.load()
        got = sender_scope.rejudge(self.dsn, [tsp.NOVA, "PurchaseOrder:x:{}"])
        self.assertEqual(got["objects"], 0, got)
        self.assertEqual(self.served(), full)


class EmailsNameTheirParties(SenderFixture):
    """The ``emails`` table's ``customers`` / ``suppliers``: a sender-party link carries
    the ``pii`` of the party's stored address; it is followed when the sender's domain (in
    the row) spells the party's name, never for a stored address on a shared mail
    provider (it would tell whose address it is)."""

    def test_a_pii_sender_link_names_the_party_only_when_the_domain_spells_it(self):
        from ontology.tests import test_sender_parties as tsp
        full = self.load()
        marks = {(r[2], r[3]): (r[4], json.loads(r[5])) for r in full}
        # the fixture's kinds: a pii corroborated link on a corporate domain (Zeta), the
        # same on a shared provider (Birch's rep on gmail), pii leads, an unmarked lead
        self.assertEqual(marks[(tsp.email(6), tsp.ZETA[0])], ("corroborated", ["pii"]))
        self.assertEqual(marks[(tsp.email(2), tsp.BIRCH)][0], "corroborated")
        self.assertIn("pii", marks[(tsp.email(2), tsp.BIRCH)][1])
        self.assertEqual(marks[(tsp.email(8), tsp.ZETA[0])], ("lead", ["pii"]))
        handle, rows = self.emails()
        zeta = sorted({handle[z] for z in tsp.ZETA})
        self.assertEqual(rows[tsp.email(6)][0], [handle[tsp.ZETA[0]]])   # zeta.example
        self.assertEqual(rows[tsp.email(8)][0], zeta)                    # a pii lead
        self.assertEqual(rows[tsp.email(2)], ([], []))                   # gmail: never
        self.assertEqual(rows[tsp.email(1)][1], [handle[tsp.ACME]])      # unmarked lead
        self.assertEqual(rows[tsp.email(3)], ([], []))
        self.assertEqual(rows[tsp.email(7)], ([], []))                   # rejected

    def test_a_domain_that_is_only_a_piece_of_a_name_word_names_nobody(self):
        # zeta.example vs "Zetamax Hotels": "zeta" is in the name, but no word of it
        # (as info.com is in "Information"): the corroborated pii link is not followed
        from ontology.tests import test_sender_parties as tsp
        self.load()
        self.sql("UPDATE ontology.value_cells c SET value = 'Zetamax Hotels Ltd' FROM "
                 "ontology.object_versions v JOIN ontology.value_sources s ON s.ver = v.ver "
                 "AND s.superseded_at IS NULL WHERE v.object_id = %s AND "
                 "v.superseded_at IS NULL AND c.src = s.src AND c.field IN (SELECT field "
                 "FROM ontology.value_fields WHERE property = 'name')", (tsp.ZETA[0],))
        _handle, rows = self.emails()
        self.assertEqual(rows[tsp.email(6)], ([], []))
        self.assertEqual(rows[tsp.email(2)], ([], []))

    def emails(self) -> tuple[dict, dict]:
        """The customers/suppliers/emails tables built: party object -> its handle, and
        message -> (customers, suppliers)."""
        import psycopg
        from operating.pg import business_views as bv
        from operating.pg.migrate import MIGRATIONS_DIR
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for name in ("0027_item_audiences.sql", "0040_people_excluded_objects.sql",
                         "0041_business_views.sql"):
                pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        with mock.patch.dict("os.environ", {"PLX_BIZ_MIN_FREE_GB": "0"}):
            bv.build(self.dsn, ["customers", "suppliers", "emails"])
        with psycopg.connect(self.dsn) as pg:
            handle = dict(pg.execute(
                "SELECT unnest(object_ids), handle FROM biz.lineage WHERE view IN "
                "('customers', 'suppliers')").fetchall())
            rows = {r[0]: (sorted(r[1]), sorted(r[2])) for r in pg.execute(
                "SELECT object_ids[1], customers, suppliers FROM biz.emails").fetchall()}
        return handle, rows


class DeltaCycleRunsIt(SenderFixture):

    def test_the_cycle_reports_the_run_and_an_error_keeps_the_mark(self):
        from operating.pg import ontology_delta, sender_scope
        self.load()
        with mock.patch.object(sender_scope, "recompute", side_effect=RuntimeError("boom")):
            out = ontology_delta.recompute_sender_links(self.dsn, log=io.StringIO())
        self.assertEqual(out, {"error": "RuntimeError: boom"})
        from operating import ops_alarms_delta
        checks = ops_alarms_delta.delta_step_checks([{"postgres": {"sender_links": out}}])
        self.assertEqual([(c["name"], c["value"]) for c in checks
                          if c["name"].endswith("sender_links")],
                         [("delta_step_failed:sender_links", 1)])


if __name__ == "__main__":
    unittest.main()
