"""Readers' doors decided ahead (operator_api.doors, SERVE decision Q1): a page read takes
the door from memory and reads on its own thread; a door is never shared between two
readers, never served past its age or across a stamp move, and a failed decision is never
served."""
import json
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from operator_api import doors as d


def reader(principals, clearance="internal", view_as=False, access=None, mode="dev"):
    return SimpleNamespace(mode=mode, session=SimpleNamespace(
        tenant="plx", principals=frozenset(principals), clearance=clearance, access=access,
        service=None, view_as=view_as))


class Worker:
    def __init__(self, fail=()):
        self.fail, self.ran = set(fail), []

    def run(self, who, job, timeout=None):
        key = ",".join(sorted(who.session.principals))
        self.ran.append(key)
        if key in self.fail:
            raise RuntimeError("policy refused")
        return SimpleNamespace(grant=SimpleNamespace(digest=key)), "dsn", 7, {"plx.reader_self": key}


class DoorsAhead(unittest.TestCase):
    def doors(self, worker):
        got = d.Doors(SimpleNamespace(access_worker=worker))
        got._stamp = 7
        return got

    def test_unknown_reader_is_read_the_old_way_and_decided_next(self):
        w = Worker()
        doors = self.doors(w)
        a = reader({"a"})
        with self.assertLogs("operator_api.serving_miss") as miss:
            self.assertIsNone(doors.get(a))
        self.assertIn("door_not_ahead", miss.output[0])
        with mock.patch.object(doors, "_read_stamp"):
            self.assertEqual(doors.run_once(), 1)
        door, dsn, comms = doors.get(a)
        self.assertEqual((door.grant.digest, dsn), ("a", "dsn"))
        self.assertEqual(comms, {"plx.reader_self": "a"})   # the mail settings decided with the door

    def test_never_shared_between_readers_at_1_10_60(self):
        for n in (1, 10, 60):
            w = Worker()
            doors = self.doors(w)
            readers = [reader({f"p{i}"}) for i in range(n)]
            for r in readers:
                self.assertTrue(doors.decide(r))
            self.assertEqual(len(w.ran), n)          # one decision each, none skipped
            for i, r in enumerate(readers):
                self.assertEqual(doors.get(r)[0].grant.digest, f"p{i}")
        # same principals, any other part of the session differs: another door
        keys = {d.reader_key(r) for r in (reader({"a"}), reader({"a"}, clearance="secret"),
                                          reader({"a"}, view_as=True),
                                          reader({"a"}, access="full"),
                                          reader({"a"}, mode="entra"))}
        self.assertEqual(len(keys), 5)
        self.assertIsNone(d.reader_key(SimpleNamespace(session=SimpleNamespace(principals=()))))

    def test_aged_or_moved_stamp_is_never_served(self):
        doors = self.doors(Worker())
        a = reader({"a"})
        doors.decide(a)
        self.assertIsNotNone(doors.get(a))
        doors._stamp = 8                          # the door moved: decided again first
        self.assertIsNone(doors.get(a))
        doors._stamp = 7
        doors._held[d.reader_key(a)].decided_at = time.monotonic() - d.MAX_AGE_SECONDS - 1
        self.assertIsNone(doors.get(a))
        self.assertIn(a, doors._due())

    def test_a_failed_decision_is_dropped_never_served(self):
        doors = self.doors(Worker(fail={"a"}))
        a = reader({"a"})
        doors.want(a)
        with self.assertLogs("operator_api.doors", "WARNING"):
            self.assertFalse(doors.decide(a))
        self.assertNotIn(d.reader_key(a), doors._held)
        self.assertIsNone(doors.get(a))

    def test_a_reader_not_served_for_hours_is_no_longer_kept(self):
        doors = self.doors(Worker())
        a = reader({"a"})
        doors.decide(a)
        doors._held[d.reader_key(a)].served_at = time.monotonic() - d.RECENT_SECONDS - 1
        self.assertEqual(doors._due(), [])
        self.assertNotIn(d.reader_key(a), doors._held)

    def test_a_stamp_move_redecides_every_held_door_in_one_pass_and_ready_waits(self):
        w = Worker()
        doors = self.doors(w)
        readers = [reader({f"p{i}"}) for i in range(10)]
        for r in readers:
            doors.want(r)
        self.assertFalse(doors.ready())
        with mock.patch.object(doors, "_read_stamp"):
            self.assertEqual(doors.run_once(), 10)    # the start's pass: every reader
        self.assertTrue(doors.ready())
        doors._stamp = 8                              # the door moved (a refresh, an exclusion)
        self.assertTrue(all(doors.get(r) is None for r in readers))
        with mock.patch.object(doors, "_read_stamp"):
            self.assertEqual(doors.run_once(), 10)    # one pass (LOOP_SECONDS = 2 s): all again
        self.assertEqual(len(w.ran), 20)
        self.assertLessEqual(d.LOOP_SECONDS * 2, 4)

    def test_a_page_read_with_a_door_ahead_never_touches_the_worker(self):
        from operator_api import served
        door = SimpleNamespace(grant=SimpleNamespace(digest="a"))
        worker = SimpleNamespace(run=mock.Mock(side_effect=AssertionError("worker used")))
        backend = SimpleNamespace(access_worker=worker, reads=worker,
                                  doors=SimpleNamespace(get=lambda r: (door, "dsn"),
                                                        decide_now=worker.run))
        seen = {}

        def scoped(pg, got, work, seconds, **kw):
            seen.update(grant=got.grant, seconds=seconds, person=kw.get("person"))
            return ('{"header": [{"v": {"po": 1}, "withheld": [], "col": null}]}',
                    datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc), None, 1), None
        pool = mock.MagicMock()
        with mock.patch("operating.ontology_tools_sql.scoped_read", scoped), \
                mock.patch("operating.page_reads.pool_for", return_value=pool):
            body, headers = served.read(backend, reader({"a"}), "record", "PO:1")
        self.assertIs(seen["grant"], door.grant)
        self.assertEqual((seen["person"], headers["X-PLX-Src"]), (True, "items"))
        self.assertEqual(json.loads(body)["sections"]["header"][0]["v"], {"po": 1})
        self.assertEqual(json.loads(body)["status"], "ok")
        pool.connection.assert_called_once_with()
        worker.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
