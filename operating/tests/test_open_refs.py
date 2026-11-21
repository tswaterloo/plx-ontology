"""Short refs for plx.open (``operating.open_refs``): a long mail, Teams, meeting or file
handle shown by plx.search or plx.sql gets a ref; plx.open given a ref opens exactly the
full handle this session showed, through the same path; a ref it did not show opens
nothing; the full handles stay in the results; refs leave the output boundary bare."""

from __future__ import annotations

import unittest

from operating import ontology_tools_search as s
from operating import open_refs

MAIL = ("graph-mail://f2f6e5e3-a318-4c3a-9628-b5ac706db794/AAkALgAAAAAAHYQDEapmEc2byACqAC-"
        "EWg0AUIVzZEFick6EuV244W1U4AAGRxRMHgAA@CQAAABYAAABQhXNkQWJyToS5XbjhbVTgAAZFKOLI")
CHAT = "teams-chat://19:abfa0842-c525-4292-b7d2-c402d18a65c2@unq.gbl.spaces/1731000000000"
RECORD = "bc://production/fbd2ba74-f148-f111-b476-7ced8db443c4/customers/825e9558-8f69"


def _sql(*handles):
    return {"result": "sql://0123456789abcdef0123", "columns": ["subject", "handle"],
            "rows": [{"cite": f"sql://0123456789abcdef0123#row={n}",
                      "cells": [{"untrusted_text": "Hi"}, h]} for n, h in enumerate(handles)]}


class RefsTest(unittest.TestCase):
    def test_a_long_document_handle_gets_a_ref_that_resolves_to_it(self):
        refs = open_refs.Refs()
        for handle in (MAIL, CHAT):
            ref = refs.ref(handle)
            self.assertTrue(open_refs.is_ref(ref), ref)
            self.assertTrue(ref.startswith(handle.split("://")[0] + "://r."))
            self.assertLess(len(ref), 40)
            self.assertEqual(refs.resolve(ref), handle)
            self.assertEqual(refs.ref(handle), ref)   # deterministic
        # a record, a short handle, no handle: written as they are
        for other in (RECORD, "graph-mail://a/b", None, 7, "plain words"):
            self.assertIsNone(refs.ref(other), other)
        # anything not a ref passes through; a ref not shown resolves to nothing
        self.assertEqual(refs.resolve(MAIL), MAIL)
        self.assertIsNone(refs.resolve(open_refs.short(MAIL + "x")))
        self.assertIsNone(open_refs.Refs().resolve(open_refs.short(MAIL)))

    def test_a_ref_naming_another_handle_already_is_not_given_and_old_refs_go(self):
        refs = open_refs.Refs(most=2)
        ref = refs.ref(MAIL)
        clash = MAIL + "-other"
        real = open_refs.short
        try:
            open_refs.short = lambda handle: ref
            self.assertIsNone(refs.ref(clash))
        finally:
            open_refs.short = real
        self.assertEqual(refs.resolve(ref), MAIL)
        refs.ref(CHAT)
        refs.ref(MAIL + "2")
        self.assertIsNone(refs.resolve(ref), "the oldest ref is dropped past the bound")

    def test_results_keep_their_handles_and_gain_refs(self):
        refs = open_refs.Refs()
        got = refs.sql(_sql(MAIL, RECORD, None))
        self.assertEqual([r["cells"][1] for r in got["rows"]], [MAIL, RECORD, None])
        self.assertEqual([r.get("ref") for r in got["rows"]],
                         [open_refs.short(MAIL), None, None])
        # no handle column, an error: unchanged
        self.assertEqual(refs.sql({"columns": ["n"], "rows": [{"cite": "c", "cells": [MAIL]}]}),
                         {"columns": ["n"], "rows": [{"cite": "c", "cells": [MAIL]}]})
        self.assertEqual(refs.sql({"error": {"code": "x"}}), {"error": {"code": "x"}})
        hits = refs.search({"documents": [{"citation": CHAT, "kind": "teams_message"},
                                          {"citation": RECORD, "kind": "record"}]})
        self.assertEqual([h.get("ref") for h in hits["documents"]],
                         [open_refs.short(CHAT), None])
        self.assertEqual(hits["documents"][0]["citation"], CHAT)

    def test_refs_leave_the_output_boundary_bare(self):
        from operating.mcp_server_results import _firewall
        refs = open_refs.Refs()
        rows = _firewall(refs.sql(_sql(MAIL)), "plx.sql")["rows"]
        self.assertEqual(rows[0]["ref"], open_refs.short(MAIL))
        hit = {"citation": MAIL, "kind": "email", "title": None, "date": None,
               "snippet": None}
        out = _firewall(refs.search({"documents": [hit], "searched": {"sources": ["x"]},
                                     "gaps": [], "completeness": {}}), "plx.search")
        self.assertEqual(out["documents"][0]["ref"], open_refs.short(MAIL))


class GateTest(unittest.TestCase):
    """The session's gate learns the refs its relayed results show, so a plx.open of one
    reaches any of the session's hosts as the full handle (``agent_session.serve_gate``)."""

    def test_the_gate_learns_shown_refs_and_expands_only_those(self):
        import json
        refs = open_refs.Refs()
        shown = refs.sql(_sql(MAIL, RECORD))
        relayed = {"content": [{"type": "text", "text": json.dumps(shown)}],
                   "structuredContent": shown}
        learnt: dict = {}
        open_refs.learn(json.dumps(relayed), learnt)
        self.assertEqual(learnt, {open_refs.short(MAIL): MAIL})
        args = {"citation": open_refs.short(MAIL), "text_offset": 5}
        self.assertEqual(open_refs.expand(args, learnt), {"citation": MAIL, "text_offset": 5})
        # a full handle, a ref never shown, a handle whose ref was never shown: unchanged
        for other in ({"citation": MAIL}, {"citation": open_refs.short(CHAT)}, {}, None,
                      {"citations": [MAIL, open_refs.short(CHAT)]}):
            self.assertIs(open_refs.expand(other, learnt), other)
        # several citations: each shown ref expands, the rest stay as sent (in order)
        many = {"citations": [open_refs.short(CHAT), open_refs.short(MAIL), MAIL],
                "near": "date"}
        self.assertEqual(open_refs.expand(many, learnt),
                         {"citations": [open_refs.short(CHAT), MAIL, MAIL], "near": "date"})
        quiet: dict = {}
        open_refs.learn(json.dumps(_sql(MAIL, CHAT)), quiet)
        self.assertEqual(quiet, {})


# A stand-in host: plx.sql answers a row with MAIL and its ref; plx.open answers the
# citation it was sent.
FAKE_HOST = r'''
import json, sys
SHOWN = json.loads(sys.argv[1])
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    result = {}
    if message.get("method") == "tools/call":
        params = message["params"]
        body = SHOWN if params["name"] == "plx.sql" else {"sent": params["arguments"]}
        result = {"content": [{"type": "text", "text": json.dumps(body)}],
                  "structuredContent": body}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
                     + "\n")
    sys.stdout.flush()
'''


class ServedGateTest(unittest.TestCase):
    def test_a_plx_open_of_a_shown_ref_reaches_the_host_as_its_full_handle(self):
        import json
        import os
        import sys
        import tempfile
        import threading
        from pathlib import Path
        from operating import agent_session
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        (tmp / "host.py").write_text(FAKE_HOST, encoding="utf-8")
        shown = open_refs.Refs().sql(_sql(MAIL))
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        client_w, client_r = os.fdopen(w_in, "w"), os.fdopen(r_out, "r")
        thread = threading.Thread(target=agent_session.serve_gate, args=(
            ["plx.sql", "plx.open"], [sys.executable, str(tmp / "host.py"),
                                      json.dumps(shown)], tmp / "refused"),
            kwargs={"stdin": os.fdopen(r_in, "r"), "stdout": os.fdopen(w_out, "w"),
                    "env": dict(os.environ)}, daemon=True)
        thread.start()

        def ask(ident, name, arguments):
            client_w.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}})
                           + "\n")
            client_w.flush()
            answer = json.loads(client_r.readline())
            self.assertEqual(answer["id"], ident)
            return json.loads(answer["result"]["content"][0]["text"])
        ref = open_refs.short(MAIL)
        unshown = open_refs.short(CHAT)
        self.assertEqual(ask(1, "plx.open", {"citation": ref}), {"sent": {"citation": ref}})
        self.assertEqual(ask(2, "plx.sql", {"sql": "x"})["rows"][0]["ref"], ref)
        self.assertEqual(ask(3, "plx.open", {"citation": ref}), {"sent": {"citation": MAIL}})
        self.assertEqual(ask(4, "plx.open", {"citation": unshown}),
                         {"sent": {"citation": unshown}})
        client_w.close()
        thread.join(30)
        client_r.close()


class HostTest(unittest.TestCase):
    """The host's plx.sql, plx.search and plx.open (``mcp_server_calls.CallsMixin``)."""

    def host(self):
        from operating.mcp_server_calls import CallsMixin
        calls = []

        class Host(CallsMixin):
            def _generic_call(self, name, wanted):
                calls.append((name, dict(wanted)))
                if name == "plx.sql":
                    return _sql(MAIL)
                if name == s.COMMS_SEARCH:
                    return {"documents": [{"citation": CHAT, "kind": "teams_message",
                                           "title": None, "date": None, "snippet": None}],
                            "searched": {"sources": ["teams"]}, "gaps": [],
                            "completeness": {"complete": True}}
                if name == s.COMMS_OPEN:
                    return {"citation": wanted["citation"], "kind": "email",
                            "text": {"untrusted_text": "Order 4411 ships"}}
                return {"documents": [], "completeness": {}}
        return Host(), calls

    def test_open_by_a_shown_ref_reads_the_full_handle_and_an_unshown_one_nothing(self):
        host, calls = self.host()
        row = host._call("plx.sql", {"sql": "SELECT subject, handle FROM emails"})["rows"][0]
        hit = host._call(s.TOOL_NAME, {"query": "x", "sources": ["teams"]})["documents"][0]
        for ref, full in ((row["ref"], MAIL), (hit["ref"], CHAT)):
            calls.clear()
            by_ref = host._call(s.OPEN_NAME, {"citation": ref})
            self.assertEqual(calls, [(s.COMMS_OPEN, {"citation": full,
                                                     "max_chars": s.OPEN_MAX_CHARS})])
            self.assertEqual(by_ref, host._call(s.OPEN_NAME, {"citation": full}))
        calls.clear()
        other, _ = self.host()   # another session's ref
        got = other._call(s.OPEN_NAME, {"citation": row["ref"]})
        self.assertEqual(got["error"]["code"], "not_found")
        got = host._call(s.OPEN_NAME, {"citation": open_refs.short(MAIL + "x")})
        self.assertEqual(got["error"]["code"], "not_found")
        self.assertEqual(calls, [], "an unshown ref reads nothing")
        # several at once through the host: each shown ref opens its full handle, another
        # session's ref opens nothing and says so in its own place
        calls.clear()
        got = host._call(s.OPEN_NAME, {"citations": [row["ref"], hit["ref"],
                                                     open_refs.short(MAIL + "x")],
                                       "near": "ships"})
        self.assertEqual([c[1]["citation"] for c in calls], [MAIL, CHAT])
        self.assertEqual([i["citation"] for i in got["opened"]],
                         [MAIL, CHAT, open_refs.short(MAIL + "x")])
        self.assertEqual(got["opened"][2]["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
