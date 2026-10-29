"""devloop/TRY-IT.md is self-checking: every fixture command block runs keyless, every
live-path block is the command of a live check it cites, and every cited check exists."""
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.request
from pathlib import Path

from devloop.live_verify import check_expectations
from devloop.main_ci import KEYLESS

ROOT = Path(__file__).resolve().parents[1]
TRY_IT = ROOT / "devloop" / "TRY-IT.md"
LIVE_CHECKS = ROOT / "devloop" / "live-checks"
LIVE_BRANCH_RESULTS = ("results/latest.json", "results/daily/latest.json")
CONSOLE_URL_SECONDS = 120
STATE_LINE = 'STATE="$(mktemp -d)"'
NOT_A_CHECK = re.compile(r"^plx\.|\.(json|py|md|sqlite)$")  # MCP tool names, file names


def sections():
    """(heading, body) per `## ` section of TRY-IT.md, the preamble under heading ''."""
    out, heading, body = [], "", []
    for line in TRY_IT.read_text().splitlines():
        if line.startswith("## "):
            out.append((heading, "\n".join(body)))
            heading, body = line, []
        else:
            body.append(line)
    out.append((heading, "\n".join(body)))
    return out


def spec_argvs():
    return [spec_argv(json.loads(p.read_text())) for p in LIVE_CHECKS.glob("*.json")]


def is_live(block):
    """A block is the command of a live check, whatever its section is called."""
    lines = block.splitlines()
    if lines[0] == STATE_LINE:
        lines = lines[1:]
    try:
        return len(lines) == 1 and shlex.split(lines[0]) in spec_argvs()
    except ValueError:
        return False


def live_section():
    """The text of the sections that hold a live-check block."""
    found = [body for _, body in sections() if any(map(is_live, section_blocks(body)))]
    assert found, "TRY-IT.md has no live-check section"
    return "\n".join(found)


def section_blocks(text):
    """Indented command blocks of `text`."""
    out, cur = [], []
    for line in text.splitlines():
        if line.startswith("    "):
            cur.append(line[4:])
        elif cur and not line.strip():
            continue
        elif cur:
            out.append("\n".join(cur))
            cur = []
    if cur:
        out.append("\n".join(cur))
    return out


def blocks(live=False):
    """Command blocks that are (live) or are not (default) the command of a live check;
    a keyless container runs the latter."""
    return [b for _, body in sections() for b in section_blocks(body) if is_live(b) == live]


def keyless_env(**extra):
    env = {k: v for k, v in os.environ.items() if not KEYLESS.match(k)}
    env.update(extra)
    return env


def cited_live_checks(text):
    """Every backticked check-shaped id (dotted `prefix.name` or hyphenated
    `ao-originals`) in `text`, whatever the sentence says, so a misspelt id is missing.
    Not checks: a later ask's id (`later ask `x``), an MCP tool name, a file name."""
    cited = set()
    for m in re.finditer(r"`([a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)+)`", text):
        if NOT_A_CHECK.search(m.group(1)) or re.search(
                r"\blater asks?\s*$", text[max(0, m.start() - 20):m.start()], re.I):
            continue
        cited.add(m.group(1))
    return cited


def missing_live_checks(ids):
    return sorted(i for i in ids if not (LIVE_CHECKS / f"{i}.json").exists())


def live_command(block):
    """The argv of a live-path block: an optional STATE line, then one command."""
    lines = block.splitlines()
    if lines[0] == STATE_LINE:
        lines = lines[1:]
    assert len(lines) == 1, f"a live block is one command: {block[:200]}"
    return shlex.split(lines[0])


def spec_argv(spec):
    return [a.replace("{tmp}", "$STATE") for a in spec["command"]]


def git_show(rev, path):
    r = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT, capture_output=True,
                       text=True)
    return json.loads(r.stdout) if r.returncode == 0 else None


def mcp_failures(block, stdout):
    """Every JSON-RPC request with an id the block pipes in must get a result: no
    `error`, no `isError` tool result, no missing or unparseable response."""
    ids = [r["id"] for r in map(json.loads, re.findall(r"'(\{[^']*\})'", block)) if "id" in r]
    responses = {}
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            return ids, [f"unparseable response line: {line[:200]}"]
        if isinstance(message, dict) and "id" in message:
            responses[message["id"]] = message
    failures = []
    for rid in ids:
        message = responses.get(rid)
        if message is None:
            failures.append(f"id {rid}: no response")
        elif "error" in message or not isinstance(message.get("result"), dict):
            failures.append(f"id {rid}: {json.dumps(message)[:300]}")
        elif message["result"].get("isError"):
            failures.append(f"id {rid}: isError {json.dumps(message['result'])[:300]}")
    return ids, failures


class TryItTest(unittest.TestCase):
    def test_try_it_names_only_flags_the_tool_accepts(self):
        text = TRY_IT.read_text(encoding="utf-8")
        for module in ("operator_ui.tests.case_fixtures", "operating.mcp_server"):
            help_out = subprocess.run([sys.executable, "-m", module, "--help"], cwd=ROOT,
                                      capture_output=True, text=True, timeout=120).stdout
            accepted = set(re.findall(r"--[a-z][a-z-]*", help_out))
            if module.endswith("case_fixtures"):  # flags its entry strips before argparse
                src = (ROOT / "operator_ui/tests/case_fixtures.py").read_text(encoding="utf-8")
                accepted |= set(re.findall(r'"(--[a-z-]+)" in sys\.argv', src))
            for command in re.findall(rf"python3 -m {re.escape(module)} ([^\n|]*)", text):
                for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", command.split(" | ")[0]):
                    self.assertIn(flag, accepted, f"{module} {flag}")

    def test_every_command_runs(self):
        found = blocks()
        mcp_blocks = 0
        with tempfile.TemporaryDirectory() as tmp:
            env = keyless_env(TMPDIR=tmp, PYTHONDONTWRITEBYTECODE="1")
            state = {}
            for block in found:
                if "operator_ui.case_console" in block or "operator_ui.tests.case_fixtures" in block:
                    self._console(block, env, state)
                    continue
                if "$DIR" in block:
                    self.assertIn("DIR", state, "step 1 must run first")
                    env["DIR"] = state["DIR"]
                with self.subTest(block=block[:60]):
                    r = subprocess.run(["sh", "-ec", block], cwd=ROOT, env=env,
                                       capture_output=True, text=True, timeout=300)
                    self.assertEqual(r.returncode, 0, r.stderr[-800:])
                    if "operating.mcp_server" in block:
                        mcp_blocks += 1
                        ids, failures = mcp_failures(block, r.stdout)
                        self.assertEqual(ids, [1, 2, 3, 4])
                        self.assertEqual(failures, [], r.stderr[-800:])
        self.assertEqual(mcp_blocks, 1)

    def test_every_cited_live_check_exists(self):
        cited = cited_live_checks(TRY_IT.read_text())
        self.assertEqual(missing_live_checks(cited), [])
        self.assertTrue(any("." not in i for i in cited), "an undotted id is cited and checked")

    def test_a_bogus_live_check_id_is_caught(self):
        real = min(p.stem for p in LIVE_CHECKS.glob("*.*.json"))
        undotted = min(p.stem for p in LIVE_CHECKS.glob("*.json") if "." not in p.stem)
        for text, bogus in (
                (f"Live runs of live checks `{real}` and `no.such_check`.", "no.such_check"),
                (f"- The host has live-check specs, `no.such_check` and `{real}`; nothing else.",
                 "no.such_check"),
                (f"- Credentials, so `{real}` and\n  `no.such_check` can run live. Done.",
                 "no.such_check"),
                (f"The run `{real}` read `no.such_check` once.", "no.such_check"),
                (f"Live checks `{real}` and `made-up-check` pass, later ask `some-ask` waits.",
                 "made-up-check")):
            with self.subTest(text=text):
                cited = cited_live_checks(text)
                self.assertEqual(cited, {real, bogus})
                self.assertEqual(missing_live_checks(cited), [bogus])
        cited = cited_live_checks(f"It read the files `{undotted}` read.")
        self.assertEqual(cited, {undotted})
        self.assertEqual(missing_live_checks(cited), [])

    def test_each_live_block_is_a_cited_check_and_fails_closed_keyless(self):
        cited = cited_live_checks(live_section())
        specs = {i: json.loads((LIVE_CHECKS / f"{i}.json").read_text())
                 for i in cited if not missing_live_checks([i])}
        found = blocks(live=True)
        self.assertTrue(found)
        with tempfile.TemporaryDirectory() as tmp:
            env = keyless_env(TMPDIR=tmp, PYTHONDONTWRITEBYTECODE="1")
            for block in found:
                with self.subTest(block=block[:80]):
                    argv = live_command(block)
                    matching = [i for i, s in specs.items() if spec_argv(s) == argv]
                    self.assertEqual(len(matching), 1, f"no cited live check runs {argv[:4]}")
                    r = subprocess.run(["sh", "-ec", block], cwd=ROOT, env=env,
                                       capture_output=True, text=True, timeout=180)
                    try:
                        document = json.loads(r.stdout)
                    except ValueError:
                        document = None
                    self.assertNotEqual(
                        check_expectations(specs[matching[0]], r.returncode, document), [],
                        "a keyless run must never meet the live check's expectations")

    def test_the_live_section_cites_passing_verdicts(self):
        # main-ci.yml (CI=true) fetches claude/devloop-live first, so there a missing
        # commit fails; the integrate gate cannot fetch it and skips, as a local run does.
        text = live_section()
        commits = re.findall(r"verifier commit\s+`([0-9a-f]{7,40})`", text)
        self.assertEqual(len(commits), 1, "the live section names one verifier commit")
        reports = [r for r in (git_show(commits[0], path) for path in LIVE_BRANCH_RESULTS) if r]
        if not reports:
            if os.environ.get("CI"):
                self.fail(f"verifier commit {commits[0]} is not fetched "
                          "(git fetch origin claude/devloop-live)")
            self.skipTest("verifier commit not fetched (git fetch origin claude/devloop-live)")
        verdicts = {}
        for report in reversed(reports):
            verdicts.update({c["id"]: c["result"] for c in report["checks"]})
        cited = cited_live_checks(text)
        self.assertTrue(cited)
        self.assertEqual({i: verdicts.get(i) for i in cited}, {i: "pass" for i in cited})

    def test_a_missing_verifier_commit_fails_in_ci_and_skips_locally(self):
        with unittest.mock.patch(f"{__name__}.git_show", return_value=None):
            with unittest.mock.patch.dict(os.environ, {"CI": "1"}):
                with self.assertRaises(self.failureException):
                    self.test_the_live_section_cites_passing_verdicts()
            with unittest.mock.patch.dict(os.environ):
                os.environ.pop("CI", None)
                with self.assertRaises(unittest.SkipTest):
                    self.test_the_live_section_cites_passing_verdicts()

    def test_a_broken_mcp_response_fails(self):
        block = next(b for b in blocks() if "operating.mcp_server" in b)
        good = {"jsonrpc": "2.0", "result": {"content": []}}
        for broken in ({"error": {"code": -32601, "message": "unknown"}},
                       {"result": {"isError": True, "content": []}}, None):
            lines = [json.dumps({**good, "id": i}) for i in (1, 2, 3)]
            if broken is not None:
                lines.append(json.dumps({"jsonrpc": "2.0", "id": 4, **broken}))
            with self.subTest(broken=broken):
                self.assertEqual(len(mcp_failures(block, "\n".join(lines))[1]), 1)
        self.assertEqual(mcp_failures(block, "\n".join(
            json.dumps({**good, "id": i}) for i in (1, 2, 3, 4)))[1], [])

    def _console(self, block, env, state):
        # Start the server on a free port, probe the printed URL once, stop it.
        head, rest = block.split("\n", 1)
        self.assertNotIn("--port", rest)
        rest = rest.replace("python3 -m", "exec python3 -u -m", 1) + " --port 0"
        p = subprocess.Popen(["sh", "-ec", head + "\necho DIR=$DIR\n" + rest], cwd=ROOT,
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [*map(lines.put, p.stdout), lines.put(None)],
                                  daemon=True)
        reader.start()
        try:
            url, seen, deadline = None, [], time.monotonic() + CONSOLE_URL_SECONDS
            while url is None:
                try:
                    line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    self.fail(f"console printed no URL in {CONSOLE_URL_SECONDS}s: {seen[-10:]}")
                if line is None:
                    self.fail(f"console exited without a URL: {seen[-10:]}")
                seen.append(line)
                if line.startswith("DIR="):
                    state["DIR"] = line.strip()[4:]
                m = re.search(r"https?://\S+", line)
                if m:
                    url = m.group(0).rstrip(".,")
            self.assertNotRegex(url, r":8765/")
            with urllib.request.urlopen(url, timeout=30) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=15)
            reader.join(timeout=15)
            p.stdout.close()
        self.assertIn("DIR", state)


if __name__ == "__main__":
    unittest.main()
