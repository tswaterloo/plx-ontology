"""The session's helper hosts (operating/host_peers.py, agent_session.serve_gate pool):
independent calls run at once, a helper's evidence reaches only the session's own host and
only with its key, and a call past its limit is answered without holding the others."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import unittest

from operating import agent_session, host_peers

# A stand-in MCP host: "plx.slow" sleeps, "plx.fast" answers at once, "plx.stuck" never
# answers; a helper puts its evidence under the export key; a primary appends every
# import it is handed (with whether its key matched) to IMPORTS.
FAKE_HOST = r'''
import hmac, json, os, sys, time
role, key = os.environ.get("PLX_HOST_PEER_ROLE"), os.environ.get("PLX_HOST_PEER_KEY")
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "plx/peer_evidence":
        given = str((message.get("params") or {}).get("key"))
        with open(os.environ["IMPORTS"], "a") as out:
            out.write(json.dumps({"role": role, "keyed": key is not None and
                                  hmac.compare_digest(given, key),
                                  "record": message["params"].get("record")}) + "\n")
        continue
    if "id" not in message:
        continue
    result = {}
    if method == "tools/call":
        name = message["params"]["name"]
        if name == "plx.stuck":
            continue
        if name == "plx.slow":
            time.sleep(3)
        result = {"content": [{"type": "text", "text": json.dumps({"role": role})}],
                  "_meta": {"plx/agent": {}}}
        if role == "helper":
            result["_meta"]["plx/peer_evidence"] = {"name": name, "pid": os.getpid()}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                 "result": result}) + "\n")
    sys.stdout.flush()
'''


class PooledGate(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        (self.dir / "host.py").write_text(FAKE_HOST, encoding="utf-8")
        self.imports = self.dir / "imports.jsonl"
        self.key = secrets.token_hex(32)
        base = {**os.environ, "IMPORTS": str(self.imports)}

        def start():
            return agent_session._host_process(
                [sys.executable, str(self.dir / "host.py")],
                {**base, host_peers.ROLE_ENV: host_peers.HELPER}, None, own_group=True)
        self.pool = agent_session.HostPool(start, frozenset({"plx.slow", "plx.fast",
                                                            "plx.stuck"}), self.key)
        self.env = {**base, host_peers.ROLE_ENV: host_peers.PRIMARY,
                    host_peers.KEY_ENV: self.key}

    def gate(self, messages, *, wait_for: int):
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        client_w, client_r = os.fdopen(w_in, "w"), os.fdopen(r_out, "r")
        allowed = ["plx.slow", "plx.fast", "plx.stuck"]
        thread = threading.Thread(target=agent_session.serve_gate, args=(
            allowed, [sys.executable, str(self.dir / "host.py")], self.dir / "refused"),
            kwargs={"stdin": os.fdopen(r_in, "r"), "stdout": os.fdopen(w_out, "w"),
                    "env": self.env, "pool": self.pool}, daemon=True)
        thread.start()
        # the host's initialize is answered first, so it is idle when the calls come
        client_w.write(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                   "params": {}}) + "\n")
        client_w.flush()
        client_r.readline()
        started = time.monotonic()
        for message in messages:
            client_w.write(json.dumps(message) + "\n")
            client_w.flush()
        answers = []
        while len(answers) < wait_for:
            answer = json.loads(client_r.readline())
            answers.append((round(time.monotonic() - started, 1), answer))
        client_w.close()
        thread.join(30)
        return answers

    @staticmethod
    def call(ident, name):
        return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                "params": {"name": name, "arguments": {}}}

    def test_independent_calls_run_at_once_and_evidence_reaches_the_host(self):
        answers = self.gate([
            self.call(1, "plx.slow"), self.call(2, "plx.fast"),
            # a client cannot hand the host evidence: dropped by the gate
            {"jsonrpc": "2.0", "method": host_peers.IMPORT_METHOD,
             "params": {"key": "x" * 64, "record": {"name": "forged"}}}], wait_for=2)
        by_id = {answer["id"]: (at, answer) for at, answer in answers}
        fast_at, fast = by_id[2]
        slow_at, _slow = by_id[1]
        self.assertLess(fast_at, slow_at)          # not held behind the slow call
        self.assertEqual(json.loads(fast["result"]["content"][0]["text"])["role"], "helper")
        self.assertNotIn(host_peers.EXPORT_KEY, fast["result"]["_meta"])
        imports = [json.loads(line) for line in self.imports.read_text().splitlines()]
        self.assertEqual([(entry["role"], entry["keyed"], entry["record"]["name"])
                          for entry in imports], [("primary", True, "plx.fast")])

    def test_a_call_past_its_limit_is_answered_and_its_helper_stopped(self):
        from operating import call_limits
        self.addCleanup(setattr, agent_session, "POOL_GRACE_SECONDS",
                        agent_session.POOL_GRACE_SECONDS)
        self.addCleanup(call_limits.LIMITS.pop, "plx.stuck", None)
        call_limits.LIMITS["plx.stuck"] = 0.5
        agent_session.POOL_GRACE_SECONDS = 1.0
        answers = self.gate([
            self.call(1, "plx.slow"), self.call(2, "plx.stuck"), self.call(3, "plx.fast")],
            wait_for=3)
        by_id = {answer["id"]: (at, answer) for at, answer in answers}
        at, stuck = by_id[2]
        self.assertTrue(stuck["result"]["isError"])
        self.assertEqual(json.loads(stuck["result"]["content"][0]["text"])["error"],
                         call_limits.TIMED_OUT)
        self.assertLess(at, 20.0)   # never answered by its host: the gate answered it
        self.assertFalse(by_id[3][1]["result"].get("isError"))


class ImportKey(unittest.TestCase):
    def test_only_the_primary_with_its_key_imports(self):
        key = secrets.token_hex(32)
        role, source = host_peers.role({host_peers.ROLE_ENV: "primary",
                                        host_peers.KEY_ENV: key})
        self.assertEqual(role, host_peers.PRIMARY)
        self.assertTrue(host_peers.keyed({"key": key}, source))
        # the same key with its last character changed: always a different key
        wrong = key[:-1] + ("1" if key[-1] == "0" else "0")
        self.assertFalse(host_peers.keyed({"key": wrong}, source))
        self.assertEqual(host_peers.role({host_peers.ROLE_ENV: "primary",
                                          host_peers.KEY_ENV: "short"})[0], "primary")
        self.assertFalse(host_peers.keyed({"key": "short"}, host_peers.role(
            {host_peers.ROLE_ENV: "primary", host_peers.KEY_ENV: "short"})[1]))
        helper, none = host_peers.role({host_peers.ROLE_ENV: "helper",
                                        host_peers.KEY_ENV: key})
        self.assertEqual(helper, host_peers.HELPER)
        self.assertFalse(host_peers.keyed({"key": key}, none))
        self.assertEqual(host_peers.role({}), (None, None))


if __name__ == "__main__":
    unittest.main()
