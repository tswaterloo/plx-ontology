"""The entity index's connections: a failed setup leaks nothing; a full pool gives up."""
import threading
import unittest

from operating.query_understanding import PgEntityIndex


class Conn:
    closed = False

    def __init__(self, fail=False):
        self.fail = fail

    def execute(self, *args):
        if self.fail:
            raise RuntimeError("setup")

    def close(self):
        self.closed = True


class Connections(unittest.TestCase):
    def test_failed_setup_closes_the_connection(self):
        made = []

        def connect(dsn):
            made.append(Conn(fail=True))
            return made[-1]
        index = PgEntityIndex("x", connect=connect)
        with self.assertRaises(RuntimeError):
            index._locked(lambda c: None)
        self.assertTrue(made[0].closed)

    def test_a_full_pool_times_out_instead_of_waiting_for_ever(self):
        index = PgEntityIndex("x", connect=lambda d: Conn())
        index.SLOT_WAIT_S = 0.05
        hold, release = threading.Event(), threading.Event()

        def busy():
            index._locked(lambda c: (hold.set(), release.wait(5)))
        threads = [threading.Thread(target=busy) for _ in range(index.CONNECTIONS)]
        for t in threads:
            t.start()
        try:
            hold.wait(2)
            while index._slots._value:
                pass
            with self.assertRaises(TimeoutError):
                index._locked(lambda c: None)
        finally:
            release.set()
            for t in threads:
                t.join()
        self.assertEqual(index._locked(lambda c: 7), 7)   # the slots came back


if __name__ == "__main__":
    unittest.main()
