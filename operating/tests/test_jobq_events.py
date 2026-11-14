"""The ordered event queue (operating.jobq.events) on a scratch Postgres database: per-entity
order under racing consumers, priority across entities, at-least-once delivery with fenced
acks and the dead letter, partitions, deduplication; and the autopilot cycle through it
(SYNTHETIC autopilot fixtures; evidence label: isolated-fixture)."""

from __future__ import annotations

import random
import shutil
import threading
import time
import unittest

from operating import autopilot as ap
from operating.jobq import events as ev
from operating.tests.test_autopilot import NOW, Fixture
from datetime import timedelta

try:
    from operating.pg import scratch_db
    import psycopg  # noqa: F401
    scratch_db.use_test_cluster()
    _NAME, _DSN = scratch_db.create_db()
except Exception:  # noqa: BLE001 - no local Postgres: skipped
    _NAME = _DSN = None


def tearDownModule() -> None:
    if _NAME:
        scratch_db.drop_db(_NAME)


def event(group: str, n: int, priority: int = 20, kind: str = "record.changed") -> dict:
    return {"event_id": f"{group}#{n}", "event_type": kind, "group_key": group,
            "priority": priority, "body": {"n": n}}


@unittest.skipIf(_DSN is None, "no scratch Postgres")
class EventQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.q = self.evq()
        self.q.conn.execute("DROP SCHEMA IF EXISTS jobq CASCADE")   # its tables and migration record
        self.q.init()

    def tearDown(self) -> None:
        self.q.close()

    def evq(self, **kw) -> ev.EventQueue:
        return ev.EventQueue(_DSN, topic=kw.pop("topic", "t"), **kw)

    def drain(self, consumers: int, *, partitions=None, delay=0.0):
        """Run `consumers` threads until nothing is left; (handled log, overlaps)."""
        log: list[tuple[str, int, float, float]] = []
        lock = threading.Lock()

        def consume(k: int) -> None:
            q = self.evq()
            empty = 0
            while empty < 5:
                got = q.receive(f"c{k}", partitions=partitions[k] if partitions else None)
                if not got:
                    empty += 1
                    time.sleep(0.02)
                    continue
                empty = 0
                msg = got[0]
                start = time.monotonic()
                time.sleep(delay * random.random())
                end = time.monotonic()
                with lock:
                    log.append((msg.group_key, msg.body["n"], start, end))
                q.ack(msg)
            q.close()

        threads = [threading.Thread(target=consume, args=(k,)) for k in range(consumers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return log

    def test_racing_consumers_keep_each_entitys_order_at_every_size(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.q.conn.execute("TRUNCATE jobq.event_queue")
                groups = [f"po:{n}-{g}" for g in range(max(1, n // 5))]
                published = [event(random.choice(groups), i) for i in range(n)]
                self.assertEqual(self.q.publish(published), n)
                log = self.drain(6, delay=0.01)
                self.assertEqual(sorted((g, k) for g, k, _s, _e in log),
                                 sorted((e["group_key"], e["body"]["n"]) for e in published))
                for g in groups:
                    mine = [entry for entry in log if entry[0] == g]
                    mine.sort(key=lambda entry: entry[2])
                    self.assertEqual([k for _g, k, _s, _e in mine],
                                     sorted(k for _g, k, _s, _e in mine))      # in order
                    for a, b in zip(mine, mine[1:]):
                        self.assertLessEqual(a[3], b[2] + 0.005)          # never two at once

    def test_priority_across_entities_never_reorders_one_entity(self):
        self.q.publish([event("po:1", 1, 40), event("po:2", 1, 40), event("po:1", 2, 40)])
        self.q.publish([event("approval:9", 1, 10, "approval.requested")])
        self.q.publish([event("thread:x", 1, 40), event("thread:x", 2, 10, "message.received")])
        order = []
        while True:
            got = self.q.receive("c")
            if not got:
                break
            order.append(got[0].event_id)
            self.q.ack(got[0])
        # the approval first; the thread's urgent second message lifts its group, but its
        # routine first message is still handled before it
        self.assertEqual(order[:3], ["approval:9#1", "thread:x#1", "thread:x#2"])
        self.assertLess(order.index("po:1#1"), order.index("po:1#2"))
        self.assertEqual(len(order), 6)

    def test_lapsed_lease_redelivers_fences_the_old_consumer_then_dead_letters(self):
        q = self.evq(lease_s=0.2, max_receives=2)
        q.publish([event("task:1", 1), event("task:1", 2)])
        first = q.receive("a")[0]
        self.assertEqual(q.receive("b"), [])           # the group waits for its head
        time.sleep(0.3)
        again = q.receive("b")[0]
        self.assertEqual((again.event_id, again.receives, again.redelivered), ("task:1#1", 2, True))
        with self.assertRaises(ev.Stale):
            q.ack(first)                              # the lapsed consumer changes nothing
        time.sleep(0.3)                               # lapses again: out of receives
        nxt = q.receive("c")[0]
        self.assertEqual(nxt.event_id, "task:1#2")    # the dead head no longer blocks
        q.ack(nxt)
        dead = q.conn.execute("SELECT state, last_error FROM jobq.event_queue WHERE event_id ="
                              " 'task:1#1'").fetchone()
        self.assertEqual((dead["state"], dead["last_error"]), ("dead", "lease lapsed"))
        q.close()

    def test_nack_backs_off_and_holds_its_entity_others_go_on(self):
        self.q.publish([event("po:1", 1), event("po:1", 2), event("po:2", 1)])
        head = self.q.receive("c")[0]
        self.assertEqual(self.q.nack(head, "store busy", delay_s=60), "ready")
        got = self.q.receive("c", limit=5)
        self.assertEqual([m.event_id for m in got], ["po:2#1"])
        self.assertEqual(self.q.nack(got[0], "bad", dead=True), "dead")

    def test_publish_is_once_per_event_and_refuses_what_it_cannot_order(self):
        self.assertEqual(self.q.publish([event("po:1", 1), event("po:1", 1)]), 1)
        self.assertEqual(self.q.publish([event("po:1", 1)]), 0)
        for bad in ({**event("po:1", 2), "group_key": ""}, {**event("po:1", 3), "event_id": None},
                    {**event("po:1", 4), "priority": "high"}, {**event("po:1", 5), "priority": True}):
            with self.assertRaises(ValueError):
                self.q.publish([bad])
        seqs = [r["seq"] for r in self.q.conn.execute(
            "SELECT seq FROM jobq.event_queue ORDER BY id")]
        self.assertEqual(seqs, [1])

    def test_partitioned_consumers_split_the_entities_and_cover_them_all(self):
        groups = [f"g{i}" for i in range(40)]
        self.q.publish([event(g, k) for k in range(3) for g in groups])
        parts = [list(range(0, 8)), list(range(8, 16))]
        log = self.drain(2, partitions=parts)
        self.assertEqual(len(log), 120)
        for g in groups:
            self.assertEqual([k for gg, k, _s, _e in sorted(log, key=lambda x: x[2]) if gg == g],
                             [0, 1, 2])
        owners = {}
        for g in groups:
            owners.setdefault(self.q.partition_of(g) < 8, set()).add(g)
        self.assertEqual(sum(len(v) for v in owners.values()), 40)

    def test_deferrals_spend_no_delivery_and_a_pending_event_is_revived(self):
        q = self.evq(max_receives=2)
        q.publish([event("po:1", 1)])
        for _ in range(10):
            msg = q.receive("c")[0]
            self.assertEqual(q.nack(msg, "rate_limited", delay_s=0, spend=False), "ready")
        msg = q.receive("c")[0]
        self.assertEqual(msg.receives, 1)
        self.assertEqual(q.nack(msg, "x", dead=True), "dead")
        self.assertEqual(q.publish([event("po:1", 1)]), 0)              # no revive: stays dead
        self.assertEqual(q.publish([event("po:1", 1)], revive=True), 1)
        again = q.receive("c")[0]
        self.assertEqual((again.event_id, again.receives), ("po:1#1", 1))
        q.close()

    def test_a_receive_stays_fast_at_5000_waiting_events_without_statistics(self):
        self.q.conn.execute(
            "INSERT INTO jobq.event_queue (topic, event_id, event_type, group_key, seq, part,"
            " priority, body, max_receives) SELECT 't', 'e' || i, 'record.changed',"
            " 'g' || (i % 1500), i, (i % 1500) % 16, 20 + (i % 3) * 10, '{}', 5"
            " FROM generate_series(1, 6000) AS i")
        started = time.monotonic()
        got = self.q.receive("c", limit=10)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(got), 10)
        self.assertEqual({m.priority for m in got}, {20})
        self.assertEqual(len({m.group_key for m in got}), 10)

    def test_handled_marks_make_a_redelivered_effect_happen_once(self):
        self.assertTrue(self.q.mark_handled("prepare", "e1"))
        self.assertFalse(self.q.mark_handled("prepare", "e1"))
        self.assertTrue(self.q.handled("prepare", "e1"))
        self.assertFalse(self.q.handled("notify", "e1"))


@unittest.skipIf(_DSN is None, "no scratch Postgres")
class AutopilotThroughTheEventQueue(Fixture):
    def setUp(self) -> None:
        super().setUp()
        q = ev.EventQueue(_DSN, topic=ap.EVQ_TOPIC)
        q.conn.execute("DROP SCHEMA IF EXISTS jobq CASCADE")   # its tables and migration record
        q.init()
        self.evq = q
        self.addCleanup(q.close)

    def states(self) -> dict:
        return {r["state"]: r["n"] for r in self.evq.conn.execute(
            "SELECT state, count(*) AS n FROM jobq.event_queue GROUP BY state")}

    def test_rate_limited_events_never_die_however_often_deferred(self):
        # Six redeliveries inside the hour, then a free hour
        burst = [self.message(100 + i, f"Could you send the COA for lot {i}?",
                              subject=f"COA {i}") for i in range(35)]
        self.env = {ap.EVQ_ENV: "1", ap.EVQ_DSN_ENV: _DSN}
        self.cycle(burst, event_queue=True)
        for k in range(6):
            self.evq.conn.execute("UPDATE jobq.event_queue SET visible_after = now()"
                                  " WHERE state = 'ready'")
            self.cycle(burst, now=NOW + timedelta(minutes=5 * (k + 1)), event_queue=True)
        self.assertEqual(self.states(), {"done": 30, "ready": 5})
        self.assertEqual({r["receives"] for r in self.evq.conn.execute(
            "SELECT receives FROM jobq.event_queue WHERE state = 'ready'")}, {0})
        self.evq.conn.execute("UPDATE jobq.event_queue SET visible_after = now()")
        later = self.cycle(burst, now=NOW + timedelta(hours=2), event_queue=True)
        self.assertEqual(later["prepared"], {"customer_request": 5})
        self.assertEqual(len(self.queue().items()), 35)

    def test_an_event_still_pending_locally_is_revived_from_the_dead_letter(self):
        msg = self.message(1, "Could you send the COA for lot 7?", subject="COA")
        self.env = {ap.EVQ_ENV: "1", ap.EVQ_DSN_ENV: _DSN}
        self.cycle([msg], now=NOW, event_queue=True)
        # as if a consumer had crashed past max_receives while the local queue still waits
        q = self.queue()
        with q.conn:
            q.conn.execute("UPDATE events SET state = 'new'")
        self.evq.conn.execute("UPDATE jobq.event_queue SET state = 'dead'")
        q.conn.execute("DELETE FROM items")
        q.conn.commit()
        out = self.cycle([msg], now=NOW + timedelta(hours=1), event_queue=True)
        self.assertEqual(out["prepared"], {"customer_request": 1})
        self.assertEqual(self.states(), {"done": 1})

    def test_each_autopilot_queue_has_its_own_topic(self):
        # Two queues with the same five events
        msgs = [self.message(200 + i, f"Could you send the COA for lot {i}?", subject=f"COA {i}")
                for i in range(5)]
        self.env = {ap.EVQ_ENV: "1", ap.EVQ_DSN_ENV: _DSN}
        a = self.cycle(msgs, event_queue=True)
        shutil.move(str(self.dir / "q"), str(self.dir / "qA"))
        b = self.cycle(msgs, event_queue=True)
        self.assertEqual(a["prepared"], {"customer_request": 5})
        self.assertEqual(b["prepared"], {"customer_request": 5})
        topics = {r["topic"]: r["n"] for r in self.evq.conn.execute(
            "SELECT topic, count(*) AS n FROM jobq.event_queue WHERE state = 'done' GROUP BY topic")}
        self.assertEqual(sorted(topics.values()), [5, 5])
        self.assertTrue(all(t.startswith("autopilot:") for t in topics))

    def test_a_replay_never_touches_the_event_queue(self):
        msg = self.message(1, "Could you send the COA for lot 7?", subject="COA")
        self.env = {ap.EVQ_ENV: "1", ap.EVQ_DSN_ENV: _DSN}
        out = self.cycle([msg])                    # no event_queue=True: not the live follow
        self.assertEqual(out["prepared"], {"customer_request": 1})
        self.assertEqual(self.states(), {})

    def test_an_event_type_without_a_priority_is_failed_locally_and_others_go_on(self):
        msg = self.message(1, "Could you send the COA for lot 7?", subject="COA")
        q = self.queue()
        with q.conn:
            q.add_event({"event_id": "odd-1", "event_type": "made.up", "object_id": "x",
                         "object_type": "Thing", "at": "2026-10-18T10:00:00Z"}, "s")
        self.env = {ap.EVQ_ENV: "1", ap.EVQ_DSN_ENV: _DSN}
        out = self.cycle([msg], event_queue=True)
        self.assertEqual(out["prepared"], {"customer_request": 1})
        self.assertEqual(out["skipped"].get("unknown_event_type"), 1)
        row = q.conn.execute("SELECT state, reason FROM events WHERE event_id = 'odd-1'").fetchone()
        self.assertEqual(tuple(row), ("failed", "unknown_event_type"))

    def test_every_event_type_has_a_priority_and_an_entity(self):
        for kind in ap.EVENT_TYPES:
            self.assertIsInstance(ap.event_priority({"event_type": kind}), int)
        with self.assertRaises(ValueError):
            ap.event_priority({"event_type": "made.up"})
        self.assertLess(ap.event_priority({"event_type": "message.received", "side": "customer"}),
                        ap.event_priority({"event_type": "record.changed"}))
        self.assertEqual(ap.event_group({"event_id": "e", "conversation_id": "c1",
                                         "object_type": "Email", "object_id": "o"}), "thread:c1")
        self.assertEqual(ap.event_group({"event_id": "e", "object_type": "PurchaseOrder",
                                         "object_id": "po-1"}), "PurchaseOrder:po-1")

    def test_the_cycle_prepares_the_same_work_with_deferred_events_held_back(self):
        burst = [self.message(100 + i, f"Could you send the COA for lot {i}?",
                              subject=f"COA {i}") for i in range(35)]
        self.env = {ap.EVQ_ENV: "1", ap.EVQ_DSN_ENV: _DSN}
        out = self.cycle(burst, event_queue=True)
        self.assertEqual(out["prepared"], {"customer_request": 30})   # 30 an hour, as without it
        self.assertEqual(self.states(), {"done": 30, "ready": 5})     # the deferred wait
        held = self.evq.conn.execute("SELECT min(visible_after - now()) AS d FROM jobq.event_queue"
                                     " WHERE state = 'ready'").fetchone()["d"]
        self.assertGreater(held.total_seconds(), 200)
        again = self.cycle(burst, event_queue=True)
        self.assertEqual((again["events_new"], again["prepared"]), (0, {}))
        self.assertEqual(len(self.queue().items()), 30)

    def test_an_unreachable_event_queue_blocks_the_cycle_and_loses_nothing(self):
        msg = self.message(1, "Could you send the COA for lot 7?", subject="COA")
        self.env = {ap.EVQ_ENV: "1", ap.EVQ_DSN_ENV: "host=/nonexistent dbname=x connect_timeout=1"}
        out = self.cycle([msg], event_queue=True)
        self.assertEqual(out["status"], "blocked")
        self.assertTrue(out["reason"].startswith("event_queue_unavailable"))
        self.assertEqual(out["prepared"], {})
        self.env = {ap.EVQ_ENV: "1", ap.EVQ_DSN_ENV: _DSN}
        later = self.cycle([msg], event_queue=True)
        self.assertEqual(later["prepared"], {"customer_request": 1})


if __name__ == "__main__":
    unittest.main()
