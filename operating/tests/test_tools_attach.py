"""An Ask never runs without its plx tools (2026-11-08 re-grade: five hard answers said
"PLX Workbench tools aren't available in this session" and two hit the 180 s limit with
no tool call, while the hosts took longer than Claude Code's MCP start-up check).

The session waits for its own host to answer its initialize before Claude Code starts,
stops at Claude Code's start-up line when the plx server is not connected, starts again
with a fresh host when that can help, and otherwise refuses as busy."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from operating import agent_session as runner

# A host's MCP server for these tests: ``mode`` answer (after ``delay`` seconds), exit
# (ends before it answers) or hang (never answers). It writes its pid to ``pids``.
STUB = textwrap.dedent("""
    import json, os, sys, time
    mode, delay, pids = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    with open(pids, "a") as out:
        out.write(f"{os.getpid()}\\n")
    if mode == "exit":
        sys.exit(3)
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("method") == "initialize":
            if mode == "hang":
                time.sleep(3600)
            time.sleep(delay)
            with open(pids + ".answered", "a") as out:
                out.write(f"{time.time()}\\n")
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stub", "version": "1"}}}), flush=True)
        elif "id" in message:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}),
                  flush=True)
""")

TOOLS = [{"name": "plx.find", "description": "find", "read_only": True,
          "inputSchema": {"type": "object", "properties": {}}}]


def _init(status: str = "connected", tools=("mcp__plx__plx_find",)) -> str:
    return json.dumps({"type": "system", "subtype": "init", "model": "m",
                       "mcp_servers": [{"name": "plx", "status": status}],
                       "tools": list(tools)})


ANSWERED = [json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "a", "name": "mcp__plx__plx_find", "input": {}}]}}),
    json.dumps({"type": "result", "subtype": "success", "result": "FINAL",
                "usage": {"input_tokens": 1, "output_tokens": 1}})]


class _Host:
    def __init__(self, work: Path, mode: str, delay: float = 0.0) -> None:
        self.pids = work / f"pids-{mode}"
        self.script = work / "stub_host.py"
        self.script.write_text(STUB, encoding="utf-8")
        self.mode, self.delay = mode, delay

    def tools(self):
        return [dict(tool) for tool in TOOLS]

    def mcp_server(self):
        return {"command": sys.executable,
                "args": [str(self.script), self.mode, str(self.delay), str(self.pids)]}

    def started(self) -> list[int]:
        return [int(n) for n in self.pids.read_text().split()] if self.pids.is_file() \
            else []

    def answered_at(self) -> list[float]:
        path = Path(str(self.pids) + ".answered")
        return [float(n) for n in path.read_text().split()] if path.is_file() else []


def _session(lines_for):
    """A Claude Code stand-in run as the app runs it (it streams, stops and is fed):
    ``lines_for(n)`` is the n-th start's stream (1-based); each start's time is kept."""
    starts: list[float] = []

    def spawn(argv, cwd, env, timeout, *, feed=None, stop=None):
        starts.append(time.time())
        if feed is not None:
            feed()
        kept = []
        for line in lines_for(len(starts)):
            kept.append(line)
            if stop is not None and stop(line):
                break
        return 0, "\n".join(kept)
    spawn.feeds = spawn.stops = True
    return spawn, starts


def _gone(pids: list[int]) -> bool:
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        # a zombie of this process is ended too
        try:
            with open(f"/proc/{pid}/stat") as stat:
                if stat.read().split(") ", 1)[1].startswith("Z"):
                    continue
        except OSError:
            continue
        return False
    return True


class ToolsAttachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def wait_gone(self, pids):
        ends = time.monotonic() + 10
        while time.monotonic() < ends and not _gone(pids):
            time.sleep(0.05)
        return _gone(pids)

    def test_a_cancelled_ask_ends_its_session_at_the_next_line(self):
        """API-011: the cancel token an abandoned Ask sets stops the session's stream."""
        import threading
        host = _Host(self.work, "answer")
        read: list[int] = []
        lines = [_init("connected"), *ANSWERED]

        def stream(n):
            for line in lines:
                read.append(n)
                yield line
        spawn, _starts = _session(stream)
        agent = runner.ClaudeCodeAgent(spawn=spawn)
        agent.cancel_event = threading.Event()
        agent.cancel_event.set()
        try:
            runner.gated_run(agent, "task", host, "question")
        except Exception:  # noqa: BLE001 - an ended session may be refused; what counts
            pass           # is that it read no further
        self.assertEqual(len(read), 1)

    def test_claude_code_starting_without_the_plx_server_never_answers(self):
        # Claude Code's start-up line says plx failed (its MCP_TIMEOUT passed): the
        # session is stopped there, started once more with a fresh host, then refused.
        host = _Host(self.work, "answer")
        spawn, starts = _session(lambda n: [_init("failed", tools=()), *ANSWERED])
        agent = runner.ClaudeCodeAgent(spawn=spawn)
        with self.assertRaises(runner.ToolsUnattached) as raised:
            runner.gated_run(agent, "task", host, "question")
        self.assertEqual(raised.exception.code, runner.CLIENT_UNATTACHED)
        self.assertEqual(len(starts), runner.ATTACH_ATTEMPTS)
        self.assertEqual(len(host.started()), runner.ATTACH_ATTEMPTS)   # a fresh host each
        self.assertTrue(self.wait_gone(host.started()))                 # none left running

    def test_a_start_up_line_that_does_not_show_plx_connected_fails_closed(self):
        unknown = [json.dumps({"type": "system", "subtype": "init"}),        # no servers
                   _init("pending"),                                         # not yet
                   _init("connected", tools=("mcp__other__x",)),             # no plx tool
                   json.dumps({"type": "system", "subtype": "init",
                               "mcp_servers": "plx", "tools": "mcp__plx__plx_find"})]
        for line in unknown:
            check = runner.AttachCheck()
            self.assertTrue(check(line), line)
            self.assertTrue(check.failed)
        check = runner.AttachCheck()
        self.assertFalse(check("not json"))
        self.assertFalse(check(_init()))
        self.assertFalse(check(ANSWERED[0]))
        self.assertFalse(check.failed)

    def test_a_host_that_ends_before_it_answers_is_started_again_then_refused(self):
        host = _Host(self.work, "exit")
        spawn, starts = _session(lambda n: [_init(), *ANSWERED])
        with self.assertRaises(runner.ToolsUnattached) as raised:
            runner.ClaudeCodeAgent(spawn=spawn).run("task", host)
        self.assertEqual(raised.exception.code, runner.HOST_STOPPED)
        self.assertEqual(starts, [])            # Claude Code never started blind
        self.assertEqual(len(host.started()), runner.ATTACH_ATTEMPTS)

    def test_a_host_still_starting_at_the_bound_is_refused_without_a_blind_start(self):
        host = _Host(self.work, "hang")
        spawn, starts = _session(lambda n: [_init(), *ANSWERED])
        with mock.patch.object(runner, "HOST_READY_SECONDS", 1.0):
            with self.assertRaises(runner.ToolsUnattached) as raised:
                runner.ClaudeCodeAgent(spawn=spawn).run("task", host)
        self.assertEqual(raised.exception.code, runner.HOST_NOT_READY)
        self.assertEqual(starts, [])
        self.assertEqual(len(host.started()), 1)   # a fresh host would start from nothing
        self.assertTrue(self.wait_gone(host.started()))

    def test_claude_code_starts_once_its_slow_host_has_answered(self):
        # the host takes longer than it once had (Claude Code's own check is not spent on
        # it); the session then runs with its tools, its clock starting with Claude Code
        host = _Host(self.work, "answer", delay=1.5)
        spawn, starts = _session(lambda n: [_init(), *ANSWERED])
        agent = runner.ClaudeCodeAgent(spawn=spawn, budget_seconds=60)
        begun = time.time()
        run = agent.run("task", host)
        self.assertEqual(len(starts), 1)
        self.assertGreaterEqual(starts[0], host.answered_at()[0])
        self.assertEqual([e["name"] for e in run.events], ["plx.find"])
        self.assertEqual(run.final_text, "FINAL")
        self.assertLess(run.agent["budget"]["seconds"], time.time() - begun - 1)

    def test_a_retry_after_one_failed_start_answers_with_tools(self):
        host = _Host(self.work, "answer")
        spawn, starts = _session(lambda n: [_init("failed" if n == 1 else "connected"),
                                            *ANSWERED])
        run = runner.ClaudeCodeAgent(spawn=spawn).run("task", host)
        self.assertEqual(len(starts), 2)
        self.assertEqual(run.final_text, "FINAL")
        self.assertEqual(len(run.events), 1)


class AskRefusalTests(unittest.TestCase):
    def test_an_ask_whose_tools_never_attach_is_busy_not_answered(self):
        from operator_api import agent_ask, errors

        class Agent:
            def run(self, prompt, host):
                raise runner.ToolsUnattached(runner.HOST_NOT_READY)

        class Host:
            def close(self):
                pass

        ask = agent_ask.AgentAsk(host_for=lambda session: Host(),
                                 agent_for=lambda on_line: Agent())
        with self.assertRaises(errors.UserFacing) as raised:
            ask.run(object(), "which orders are late?", lambda *a: None)
        self.assertIs(raised.exception, errors.ASK_TOOLS_STARTING)
        self.assertEqual((raised.exception.code, raised.exception.status), ("busy", 503))


if __name__ == "__main__":
    unittest.main()


CLAUDE_STUB = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time
    log = sys.argv[sys.argv.index("--stub-log") + 1]
    with open(log, "a") as out:
        out.write(json.dumps({{"start": os.getpid(), "argv": sys.argv[1:]}}) + "\\n")
    n = 0
    for line in sys.stdin:
        message = json.loads(line)
        n += 1
        content = message["message"]["content"]
        text = [b for b in content if b["type"] == "text"][0]["text"]
        shown = [[b["source"]["media_type"], b["source"]["data"]] for b in content
                 if b["type"] == "image"]
        with open(log, "a") as out:
            out.write(json.dumps({{"turn": n, "text": text, "kinds": [b["type"] for b in content],
                                  "images": shown}}) + "\\n")
        if n == 1:
            print(json.dumps({{"type": "system", "subtype": "init", "model": "m",
                              "mcp_servers": [{{"name": "plx", "status": "connected"}}],
                              "tools": ["mcp__plx__plx_find"]}}), flush=True)
        if "SLOW" in text:
            time.sleep(60)
        print(json.dumps({{"type": "assistant", "message": {{"content": [
            {{"type": "text", "text": "answer " + str(n)}}]}}}}), flush=True)
        print(json.dumps({{"type": "result", "subtype": "success", "result": "answer "
                          + str(n), "usage": {{"input_tokens": 1, "output_tokens": 1}}}}),
              flush=True)
""")


class WarmSessionTests(unittest.TestCase):
    """A follow-up in a conversation is one more user message to the Claude Code process
    and host that answered the turn before (``ClaudeCodeAgent.open_session``)."""

    setUp_base = ToolsAttachTests.setUp
    wait_gone = ToolsAttachTests.wait_gone

    def setUp(self):
        self.setUp_base()
        self.log = self.work / "claude.log"
        self.binary = self.work / "claude"
        self.binary.write_text(CLAUDE_STUB.format(python=sys.executable), encoding="utf-8")
        self.binary.chmod(0o755)

    def entries(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] \
            if self.log.is_file() else []

    def agent(self, **kw):
        return runner.ClaudeCodeAgent(binary=str(self.binary), timeout=kw.pop("timeout", 30),
                                      budget_seconds=20, max_steps=5,
                                      extra_args=["--stub-log", str(self.log)], **kw)

    def test_a_second_turn_reuses_the_process_and_has_its_whole_budget_again(self):
        host = _Host(self.work, "answer")
        session = self.agent().open_session(host)
        lines: list[str] = []
        try:
            first = session.turn("Late orders?", lines.append)
            time.sleep(1.1)
            second = session.turn("And for Unilever?", lines.append)
        finally:
            session.close()
        self.assertEqual((first.final_text, second.final_text), ("answer 1", "answer 2"))
        starts = [e for e in self.entries() if "start" in e]
        self.assertEqual(len(starts), 1)                      # one Claude Code process
        self.assertIn("--input-format", starts[0]["argv"])
        self.assertNotIn("Late orders?", starts[0]["argv"])   # the task goes in on stdin
        self.assertEqual([e["text"] for e in self.entries() if "turn" in e],
                         ["Late orders?", "And for Unilever?"])
        self.assertEqual(len(host.started()), 1)              # one host for both turns
        self.assertEqual(second.agent["host_start"], "kept")
        # the follow-up's clock starts at its own turn, not the session's start
        self.assertGreaterEqual(second.agent["budget"]["seconds"], 0)
        self.assertLess(second.agent["budget"]["seconds"], 1.0)
        self.assertTrue(any('"answer 2"' in line for line in lines))
        self.assertFalse(session.alive)
        self.assertTrue(self.wait_gone([starts[0]["start"], *host.started()]))

    def test_an_attached_image_is_shown_to_the_model_with_its_turn_only(self):
        """``images`` go in the turn's user message as image blocks before its text; a
        follow-up without one carries none; an image the model cannot be shown (another
        media type, data that is not base64) is refused before anything is sent."""
        import base64
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nlabel").decode("ascii")
        host = _Host(self.work, "answer")
        session = self.agent().open_session(host)
        try:
            first = session.turn("What product is on this label?",
                                 images=[{"media_type": "image/png", "data": png}])
            second = session.turn("And its supplier?")
        finally:
            session.close()
        self.assertEqual((first.final_text, second.final_text), ("answer 1", "answer 2"))
        turns = [e for e in self.entries() if "turn" in e]
        self.assertEqual([(t["kinds"], t["images"]) for t in turns],
                         [(["image", "text"], [["image/png", png]]), (["text"], [])])
        for bad in ({"media_type": "application/pdf", "data": png},
                    {"media_type": "image/png", "data": "not base64!"}):
            with self.assertRaises(ValueError):
                runner.image_blocks([bad])
        refused = self.agent().open_session(_Host(self.work, "answer"))
        try:
            with self.assertRaises(ValueError):
                refused.turn("Read it", images=[{"media_type": "text/html", "data": png}])
        finally:
            refused.close()
        self.assertEqual(len([e for e in self.entries() if "turn" in e]), 2)

    def test_a_prestarted_session_is_up_before_its_question_and_its_clock_starts_then(self):
        host = _Host(self.work, "answer")
        session = self.agent().open_session(host, prestart=True)
        try:
            # its host and Claude Code process are up before any question
            deadline = time.monotonic() + 10
            while not any("start" in e for e in self.entries()) \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            starts = [e for e in self.entries() if "start" in e]
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(host.started()), 1)
            self.assertFalse(any("turn" in e for e in self.entries()))
            time.sleep(1.1)
            first = session.turn("Late orders?")
        finally:
            session.close()
        self.assertEqual(first.final_text, "answer 1")
        self.assertEqual(len([e for e in self.entries() if "start" in e]), 1)
        self.assertNotIn("Late orders?", starts[0]["argv"])
        # the wait before the question is not taken from its budget
        self.assertLess(first.agent["budget"]["seconds"], 1.0)
        self.assertFalse(session.alive)
        self.assertTrue(self.wait_gone([starts[0]["start"], *host.started()]))

    def test_a_turn_that_times_out_ends_the_session_and_its_process(self):
        host = _Host(self.work, "answer")
        session = self.agent(timeout=2).open_session(host)
        try:
            self.assertEqual(session.turn("first").final_text, "answer 1")
            ran = session.turn("SLOW one")
            self.assertEqual(ran.usage.get("subtype"), "timeout")
            self.assertFalse(session.alive)
            with self.assertRaises(runner.ToolsUnattached):
                session.turn("third")
        finally:
            session.close()
        starts = [e["start"] for e in self.entries() if "start" in e]
        self.assertEqual(len(starts), 1)
        self.assertTrue(self.wait_gone(starts + host.started()))

    def test_a_cancelled_turn_is_stopped_and_never_continued(self):
        import threading
        host = _Host(self.work, "answer")
        session = self.agent().open_session(host)
        cancel = threading.Event()
        cancel.set()
        try:
            session.turn("first", cancel=cancel)
            self.assertFalse(session.alive)
        finally:
            session.close()
        starts = [e["start"] for e in self.entries() if "start" in e]
        self.assertTrue(self.wait_gone(starts + host.started()))
