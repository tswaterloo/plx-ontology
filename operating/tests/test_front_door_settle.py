"""A front-door turn that times out keeps its warm session (operating/front_door.py):
the session reads its late reply on its own thread and answers the next question with
its own reply; no cold session starts in its place. Evidence label: ``isolated-fixture``
(a stand-in session process, made-up questions)."""

from __future__ import annotations

import sys
import time
import unittest

from operating import front_door as fd

# A stand-in for the headless session: the rules are answered at once, a question
# naming "slow" after a second, any other at once; each reply restates the question.
SCRIPT = r'''
import json, sys, time
for line in sys.stdin:
    text = json.loads(line)["message"]["content"]
    if text.startswith("{"):
        question = json.loads(text)["question"]
        if "slow" in question:
            time.sleep(1.0)
        reply = json.dumps({"rewrite": question, "route": "lookup", "status": "Reading"})
    else:
        reply = json.dumps({"ready": True})
    print(json.dumps({"type": "result", "result": reply}), flush=True)
'''


class SettleTests(unittest.TestCase):
    def test_a_timed_out_turn_keeps_the_warm_session(self):
        spawned = []

        def spawn(argv, env):
            spawned.append(fd._Session([sys.executable, "-c", SCRIPT], env))
            return spawned[-1]
        front = fd.FrontDoor(spawn=spawn, pool=1, seconds=0.3)
        self.addCleanup(lambda: [session.close() for session in spawned])
        front.warm()
        deadline = time.monotonic() + 20
        while front._free.qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(front.read("which slow orders are late"))
        reading = front.read("which purchase orders are late")
        self.assertIsNotNone(reading)
        self.assertEqual(reading.rewrite, "which purchase orders are late")
        self.assertEqual(len(spawned), 1)   # the same warm session, no cold start


if __name__ == "__main__":
    unittest.main()
