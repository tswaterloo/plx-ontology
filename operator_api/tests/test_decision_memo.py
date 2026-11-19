"""One reader's record decisions are made once across their concurrent checks."""
import threading
import time
import unittest

from operator_api.decision_memo import Memo


class Counting:
    def __init__(self, pause=0.0, fail_first=False):
        self.calls, self.pause, self.fail_first = [], pause, fail_first
        self.lock = threading.Lock()

    def __call__(self, handles):
        with self.lock:
            self.calls.append(list(handles))
            first = self.fail_first and len(self.calls) == 1
        time.sleep(self.pause)
        if first:
            raise RuntimeError("unreadable")
        return {h: None if not h.startswith("no:") else "refused" for h in handles}


class DecisionMemo(unittest.TestCase):
    def test_concurrent_checks_of_one_reader_decide_each_handle_once(self):
        for n in (1, 10, 60):
            memo, decide = Memo(), Counting(pause=0.2)
            handles = [f"h{i}" for i in range(14)] + ["no:x"]
            outs = []

            def check():
                outs.append(memo.decide(("a", 1), handles, decide))
            threads = [threading.Thread(target=check) for _ in range(n)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(len(outs), n)
            self.assertTrue(all(o == outs[0] and o["no:x"] == "refused" and len(o) == 15 for o in outs))
            self.assertEqual(sorted(h for c in decide.calls for h in c), sorted(handles))   # once each

    def test_another_reader_or_another_state_shares_nothing(self):
        memo, decide = Memo(), Counting()
        memo.decide(("a", 1), ["h"], decide)
        memo.decide(("b", 1), ["h"], decide)
        memo.decide(("a", 2), ["h"], decide)
        self.assertEqual(len(decide.calls), 3)

    def test_a_decision_is_reused_only_for_its_seconds(self):
        now = [0.0]
        memo, decide = Memo(seconds=8.0, clock=lambda: now[0]), Counting()
        memo.decide("k", ["h"], decide)
        now[0] = 7.0
        memo.decide("k", ["h"], decide)
        self.assertEqual(len(decide.calls), 1)
        now[0] = 9.0
        memo.decide("k", ["h"], decide)
        self.assertEqual(len(decide.calls), 2)

    def test_no_key_shares_nothing_and_a_failed_decision_is_decided_by_the_waiter(self):
        memo, decide = Memo(), Counting()
        memo.decide(None, ["h"], decide)
        memo.decide(None, ["h"], decide)
        self.assertEqual(len(decide.calls), 2)
        memo, decide = Memo(), Counting(pause=0.2, fail_first=True)
        results = {}

        def first():
            try:
                memo.decide("k", ["h"], decide)
            except RuntimeError:
                results["first"] = "raised"

        def second():
            time.sleep(0.05)
            results["second"] = memo.decide("k", ["h"], decide)
        a, b = threading.Thread(target=first), threading.Thread(target=second)
        a.start(); b.start(); a.join(); b.join()
        self.assertEqual(results, {"first": "raised", "second": {"h": None}})   # never a shared failure


if __name__ == "__main__":
    unittest.main()
