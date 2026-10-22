"""verify/live_verify.py runs only fake commands here; real specs are validated, never run."""

from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from verify import live_verify as lv
from scripts import live_read_probes as probes


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()

FAKE_MODULE = "scripts.fake_probe"
SECRET = "s3cr3t-Value-that-must-never-appear-0123"
FAKE_SOURCE = textwrap.dedent('''
    import json, os, pathlib, sys, time
    mode = sys.argv[1]
    if len(sys.argv) > 2:
        pathlib.Path(sys.argv[2]).write_text("ran")
    if mode == "ok":
        print(json.dumps({"token": "ok", "count": 3, "nested": {"n": 2, "flag": True},
                          "items": [{"name": "Acme Customer Ltd", "absent": []},
                                    {"name": "Other", "absent": []}],
                          "status": "current", "label": "Customer PO 12345 for Jane"}))
    elif mode == "leak":
        key = os.environ.get("FAKE_API_KEY", "")
        print(json.dumps({"token": "ok", "status": key,
                          "message": "Authorization: Bearer abcdefghijklmnop " + key,
                          "other": "plxwb_ABCDEFGHIJKLMNOPQRSTUV client_secret=hunter2hunter2",
                          "mail": "someone@example.com",
                          "blob": "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"}))
        print(json.dumps({"error": "SourceUnavailable", "message": "denied for " + key}),
              file=sys.stderr)
        sys.exit(1)
    elif mode == "env":
        pathlib.Path(sys.argv[2]).write_text(json.dumps(sorted(os.environ)))
        print(json.dumps({"token": "ok"}))
    elif mode == "bc":
        print(json.dumps({"token": "ok", "bc": bool(os.environ.get("BC_CLIENT_SECRET"))}))
    elif mode == "sleep":
        time.sleep(30)
    elif mode == "fail":
        print(json.dumps({"error": "SourceUnavailable", "message": "HTTP 503"}), file=sys.stderr)
        sys.exit(1)
    elif mode == "degraded":
        print(json.dumps({"token": "ok", "degraded": True, "available_relation_count": 89,
                          "blocked_upstream": [
                              {"part": name, "source": "portal-workbench-api",
                               "cause": "relation_unavailable",
                               "reason": "148 approved columns are missing or not granted"}
                              for name in ("sage.oe_order_detail", "sage.oe_order_header")]}))
    elif mode == "notready":
        print(json.dumps({"error": "WorkbenchNotReady", "probe": "portal-health",
                          "reason": "health: recovery is not true"}), file=sys.stderr)
        sys.exit(1)
    elif mode == "echo":
        print(os.environ["FAKE_DOC"])
        sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
    elif mode == "text":
        print("not json")
    elif mode == "trace":
        print(json.dumps({"token": "ok"}))
        raise ValueError("customer Acme Cosmetics Inc row 4471 value 99.12")
    elif mode in ("custom", "stdlib"):  # the traceback's innermost frames: fake_errors
        from scripts import fake_errors
        print(json.dumps({"token": "ok"}))
        (fake_errors.resolve if mode == "custom" else fake_errors.parse)("{Acme 4471")
    elif mode == "garbage":
        print("customer Acme Cosmetics Inc owes 99.12", file=sys.stderr)
        sys.exit(3)
    elif mode == "keys":
        print(json.dumps({"token": "ok", "by_customer": {"Acme Cosmetics Inc": 3, "acme": 1},
                          "checks": {"chain_ok": True, "rows": 4},
                          "nested": {"inner": {"x_ok": True}}}))
    elif mode == "share":  # argv: marker, a session path, a log [, crash]
        target, log = pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4])
        with log.open("a") as handle:
            handle.write(json.dumps({"dir": str(target.parent), "seen": target.exists(),
                                     "mode": target.parent.stat().st_mode & 0o777,
                                     "since": os.environ.get("PLX_CAPTURE_FRESH_SINCE")})
                         + "\\n")
        time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
        target.write_text("captured")
        if sys.argv[5:] == ["crash"]:
            sys.exit(1)
        print(json.dumps({"token": "ok"}))
    elif mode == "argv":
        pathlib.Path(sys.argv[2]).write_text(json.dumps(sys.argv[3:]))
        print(json.dumps({"token": "ok"}))
    elif mode == "artifact":  # argv: the declared path; writes FAKE_ARTIFACT's bytes there
        out, source = pathlib.Path(sys.argv[2]), pathlib.Path(os.environ["FAKE_ARTIFACT"])
        out.unlink()
        if os.environ.get("FAKE_LINK"):
            out.symlink_to(source)
        else:
            out.write_bytes(source.read_bytes())
        (out.parent / "other.xml").write_bytes(source.read_bytes())  # never declared
        print(json.dumps({"token": "ok"}))
        sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
    elif mode == "big":
        print(json.dumps({"token": "ok", **{f"field_{i}": {"a": i, "b": [1, 2, 3]}
                                             for i in range(400)}}))
''')
# A custom exception class without an Error suffix whose message spans lines, raised from a
# stdlib error (a chained traceback), and a stdlib error raised inside the stdlib.
FAKE_ERRORS = textwrap.dedent('''
    import json


    class RowUnresolvable(Exception):
        pass


    def resolve(text):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise RowUnresolvable("customer Acme Cosmetics Inc\\nrow 4471 value 99.12") from exc


    def parse(text):
        return json.JSONDecoder().decode(text)
''')


def spec(spec_id: str = "fake.ok", *args: str, **extra) -> dict:
    expect = extra.pop("expect", {"exit_code": 0, "json_fields": ["token"]})
    return {"id": spec_id, "description": "fake probe for tests",
            "command": ["python3", "-m", FAKE_MODULE, *(args or ("ok",))],
            "expect": expect, **extra}


@contextlib.contextmanager
def every_sqlite_connection_closed(case: unittest.TestCase):
    """Every sqlite3 connection opened inside is closed when it exits: one left to the GC
    warns (ResourceWarning, Python 3.13+) into whichever later test holds sys.stderr."""
    import sqlite3
    real, opened = sqlite3.connect, []

    def connect(*args, **kwargs):
        opened.append((args[:1], real(*args, **kwargs)))
        return opened[-1][1]

    with mock.patch("sqlite3.connect", connect):
        yield
    left = []
    for where, conn in opened:
        try:
            conn.total_changes  # raises only once the connection is closed
        except sqlite3.ProgrammingError:
            continue
        left.append(where)
        conn.close()
    case.assertEqual(left, [], "sqlite3 connections left open")


class FakeRoot:
    """A temp repo root with a fake scripts.fake_probe module and a specs dir."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "__init__.py").write_text("")
        (self.root / "scripts" / "fake_probe.py").write_text(FAKE_SOURCE)
        (self.root / "scripts" / "fake_errors.py").write_text(FAKE_ERRORS)
        self.specs = self.root / "verify" / "live-checks"
        self.specs.mkdir(parents=True)
        self.env = {"PATH": os.environ.get("PATH", ""), "FAKE_API_KEY": SECRET}

    def write(self, value: dict) -> None:
        (self.specs / f"{value['id']}.json").write_text(json.dumps(value))

    def run(self, value: dict, **kwargs) -> dict:
        return lv.run_spec(value, root=self.root, environ=kwargs.pop("environ", self.env),
                           allowed_modules={FAKE_MODULE}, **kwargs)

    def main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = lv.main(list(argv), root=self.root, environ=self.env,
                           allowed_modules={FAKE_MODULE})
        return code, out.getvalue()

    def close(self) -> None:
        self._tmp.cleanup()


class LiveVerifyRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeRoot()
        self.addCleanup(self.fake.close)

    def test_report_matches_the_live_verifier_schema(self) -> None:
        self.fake.write(spec("fake.ok", record_enums=["status"],
                             expect={"exit_code": 0, "json_fields": ["token", "count"],
                                     "json_equals": {"token": "ok", "nested.flag": True},
                                     "json_min": {"count": 1},
                                     "json_empty": ["items[*].absent"],
                                     "json_absent": ["items[*].error"]}))
        self.fake.write(spec("fake.skip", requires_env=["NOT_SET_ANYWHERE"]))
        self.fake.write(spec("fake.fail", "fail"))
        self.fake.write({**spec("fake.refused"), "command": ["bash", "-c", "true"]})
        output = self.fake.root / "out" / "latest.json"
        code, text = self.fake.main("--output", str(output), "--main-sha", "a" * 40,
                                    "--previous-main-sha", "b" * 40)
        self.assertEqual(code, 1)  # one check failed
        report = json.loads(output.read_text())
        self.assertEqual(json.loads(text), report)
        self.assertEqual((report["exit_code"], report["stop_reason"]), (1, "finished"))
        # Top-level keys of results/latest.json in the published results.
        for key in ("schema", "run", "main_sha", "previous_main_sha", "generated_at", "mode",
                    "live_verify_py_on_main", "live_checks_dir_on_main", "summary", "checks"):
            self.assertIn(key, report)
        self.assertEqual(report["schema"], "plx-live-verify/v1")
        self.assertEqual(report["main_sha"], "a" * 40)
        self.assertEqual(report["previous_main_sha"], "b" * 40)
        self.assertRegex(report["run"], r"^live-\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(report["run"], "live-" + report["generated_at"])
        self.assertIsInstance(report["live_verify_py_on_main"], bool)
        self.assertIsInstance(report["live_checks_dir_on_main"], bool)
        self.assertEqual(report["summary"], {"pass": 1, "fail": 1, "error": 1, "blocked": 0,
                                             "skipped": 1})
        self.assertEqual(sum(report["summary"].values()), len(report["checks"]))
        by_id = {c["id"]: c for c in report["checks"]}
        for check in report["checks"]:
            self.assertIn(check["result"], {"pass", "fail", "error", "blocked", "skipped"})
            self.assertIsInstance(check["detail"], dict)
            self.assertEqual(check["spec"], f"verify/live-checks/{check['id']}.json")
        ok = by_id["fake.ok"]
        self.assertEqual(ok["result"], "pass")
        self.assertEqual(ok["exit_code"], 0)
        self.assertEqual(ok["command"], "python3 -m scripts.fake_probe ok")
        self.assertEqual(ok["detail"]["fields"]["token"], "<str len=2>")
        self.assertEqual(ok["detail"]["fields"]["count"], 3)
        self.assertEqual(ok["detail"]["fields"]["items[*].absent"], [[], []])
        self.assertEqual(by_id["fake.fail"]["detail"]["failures"],
                         ["exit_code 1 != 0", "stdout is not a JSON object"])
        self.assertEqual(by_id["fake.fail"]["detail"]["stderr"],
                         {"error": "SourceUnavailable", "message": "<str len=8>"})
        self.assertEqual(by_id["fake.refused"]["result"], "error")
        self.assertIn("refused", by_id["fake.refused"]["detail"])

    def test_detail_is_counts_only_strings_are_reduced_unless_enum(self) -> None:
        result = self.fake.run(spec("fake.ok", record=["token", "status", "label", "items",
                                                       "nested"],
                                    record_enums=["status"]))
        fields = result["detail"]["fields"]
        self.assertEqual(fields["status"], "current")  # enum allowed
        self.assertEqual(fields["token"], "<str len=2>")
        self.assertEqual(fields["label"], "<str len=26>")  # free text never copied
        self.assertEqual(fields["items"], {"count": 2})  # rows never copied
        self.assertEqual(fields["nested"], {"keys": 2})  # keys need record_keys
        serialized = json.dumps(result)
        self.assertNotIn("Acme", serialized)
        self.assertNotIn("Jane", serialized)
        self.assertNotIn("12345", serialized)

    def test_a_stop_envelope_is_recorded_whatever_the_spec_declares(self) -> None:
        # The daily run recorded ontology.projection (exit 1) with fields {}: the probe's
        # {"error", "code"} line named no declared path, so the cause was lost.
        expect = {"exit_code": 0, "json_fields": ["objects"], "json_min": {"objects": 1}}
        stop = self.fake.run(spec("fake.stop", "echo", expect=expect), environ={
            **self.fake.env, "FAKE_EXIT": "1",
            "FAKE_DOC": json.dumps({"probe": "p", "error": "X", "code": "budget"})})
        self.assertEqual(stop["result"], "fail")
        self.assertEqual(stop["detail"]["fields"], {"error": "X", "code": "budget"})
        self.assertIn("exit_code 1 != 0", stop["detail"]["failures"])
        # Long, free-text or nested envelope values are typed, never copied raw.
        odd = self.fake.run(spec("fake.odd", "echo", expect=expect), environ={
            **self.fake.env, "FAKE_EXIT": "4", "FAKE_DOC": json.dumps({
                "status": "budget_exhausted", "elapsed_seconds": 12.5,
                "phase": "Customer PO 12345 for Jane", "stage": "x" * 65,
                "error": {"message": "Acme Cosmetics Inc", "rows": [1, 2]},
                "code": ["Acme", "Jane"], "label": "Acme"})})
        self.assertEqual(odd["detail"]["fields"], {
            "status": "budget_exhausted", "elapsed_seconds": 12.5,
            "phase": "<str len=26>", "stage": "<str len=65>",
            "error": {"keys": 2}, "code": {"count": 2}})
        for value in ("Acme", "Jane", "12345"):
            self.assertNotIn(value, json.dumps(odd))
        # A projection probe's SQLite stop line keeps its store and disk byte counts (a
        # low-disk failure shows in the verdict); text in them is dropped.
        disk = {"store_dir": {"free_bytes": 0, "total_bytes": 4096},
                "sqlite_temp_dir": {"free_bytes": 1024, "total_bytes": 8192}}
        full = self.fake.run(spec("fake.full", "echo", expect=expect), environ={
            **self.fake.env, "FAKE_EXIT": "1", "FAKE_DOC": json.dumps({
                "probe": "p", "error": "OperationalError", "code": "SQLITE_FULL",
                "phase": "project_bc", "store": "projection", "disk": disk})})
        self.assertEqual(full["detail"]["fields"], {
            "error": "OperationalError", "code": "SQLITE_FULL", "phase": "project_bc",
            "store": "projection", "disk": disk})
        texty = self.fake.run(spec("fake.texty", "echo", expect=expect), environ={
            **self.fake.env, "FAKE_EXIT": "1", "FAKE_DOC": json.dumps({
                "error": "OperationalError", "store": "/srv/Acme Cosmetics/store.sqlite",
                "disk": {"store_dir": {"free_bytes": "Jane 12345", "total_bytes": 4096,
                                       "path": "/srv/Acme"},
                         "sqlite_temp_dir": {"free_bytes": True, "total_bytes": 1.5},
                         "Acme": {"free_bytes": 1}}})})
        self.assertEqual(texty["detail"]["fields"], {
            "error": "OperationalError", "disk": {"store_dir": {"total_bytes": 4096}}})
        for value in ("Acme", "Jane", "12345", "/srv"):
            self.assertNotIn(value, json.dumps(texty))
        # A detail over the cap still keeps the envelope, store and disk included.
        capped = lv._cap({"unverified": ["missing x" * 50] * 20, "failures": ["exit"],
                          "fields": {"error": "X", "code": "budget", "objects": 3,
                                     "store": "projection", "disk": disk}})
        self.assertEqual(capped["fields"], {"error": "X", "code": "budget",
                                            "store": "projection", "disk": disk})

    def test_secrets_and_token_shapes_are_scrubbed_everywhere(self) -> None:
        result = self.fake.run(spec(
            "fake.leak", "leak", requires_env=["FAKE_API_KEY"],
            record=["status", "message", "other", "mail", "blob"],
            record_enums=["status", "message", "other", "mail", "blob"],
            expect={"exit_code": 0, "json_fields": ["token"]}))
        serialized = json.dumps(result)
        self.assertEqual(result["result"], "fail")
        for leaked in (SECRET, "abcdefghijklmnop", "plxwb_ABCDEF", "hunter2",
                       "someone@example.com", "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"):
            self.assertNotIn(leaked, serialized)
        self.assertEqual(result["detail"]["stderr"]["error"], "SourceUnavailable")
        self.assertRegex(result["detail"]["stderr"]["message"], r"^<str len=\d+>$")

    def test_scrub_keeps_shas_out_of_secret_patterns_but_not_field_names(self) -> None:
        self.assertEqual(lv.scrub("orders_with_probable_po_after_normalization"),
                         "orders_with_probable_po_after_normalization")
        self.assertEqual(lv.scrub("token=abc123def456"), "token=<redacted>")
        self.assertEqual(lv.scrub('"client_secret": "zzzzzzzz"'), '"client_secret": "<redacted>"')
        self.assertEqual(lv.scrub("eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"), "<redacted-jwt>")
        self.assertEqual(len(lv.scrub("x" * 1000)), lv.MAX_STRING_CHARS + len("...<truncated>"))
        self.assertEqual(lv.secret_values({"BC_CLIENT_SECRET": "abcdefgh", "PATH": "/usr/bin",
                                           "MC_MCP_API_KEY": "short"}), ["abcdefgh"])

    def test_refuses_commands_outside_the_allowlist_and_never_runs_them(self) -> None:
        marker = self.fake.root / "ran.txt"
        refused = [
            spec("fake.notlisted", "ok", str(marker)),  # shape ok, not allowlisted
            {**spec(), "command": ["bash", "-c", "echo hi"]},
            {**spec(), "command": ["python3", "-c", "print(1)"]},
            {**spec(), "command": ["python3", "-m", "os", "ok"]},
            {**spec(), "command": ["python3", "-m", "verify.import_graph", "x"]},
            {**spec(), "command": "python3 -m scripts.fake_probe ok"},
            {**spec(), "command": ["python3", "-m", FAKE_MODULE, "{home}/x"]},
        ]
        for value in refused:
            with self.subTest(command=value["command"]):
                with self.assertRaises(lv.SpecError):
                    lv.validate_spec(value, allowed_modules={FAKE_MODULE} - (
                        {FAKE_MODULE} if value["id"] == "fake.notlisted" else set()))
        result = lv.run_spec(refused[0], root=self.fake.root, environ=self.fake.env,
                             allowed_modules=set())
        self.assertEqual(result["result"], "error")
        self.assertIn("not in the live-verify allowlist", result["detail"]["refused"])
        self.assertFalse(marker.exists())
        # The default allowlist never admits a module outside the three packages, except
        # the one verify module: the data-shape profiler (profile.* specs only).
        for module in lv.ALLOWED_MODULES - {lv.PROFILE_MODULE}:
            self.assertRegex(module, r"^(integrations|operating|scripts)\.")
        self.assertEqual(lv.PROFILE_MODULE, "verify.data_profile")
        self.assertEqual([m for m in lv.ALLOWED_MODULES if m.startswith("verify.")],
                         [lv.PROFILE_MODULE])

    def test_failure_never_publishes_stderr_text_only_the_exception_class(self) -> None:
        def line_of(source: str, text: str) -> int:
            return next(n for n, line in enumerate(source.splitlines(), 1) if text in line)

        result = self.fake.run(spec("fake.trace", "trace"))
        self.assertEqual(result["result"], "fail")
        self.assertEqual(result["detail"]["stderr"]["error_class"], "ValueError")
        self.assertEqual(result["detail"]["stderr"]["frame"],
                         f"scripts/fake_probe.py:{line_of(FAKE_SOURCE, 'raise ValueError')}")
        self.assertGreater(result["detail"]["stderr"]["lines"], 1)
        serialized = json.dumps(result)
        for value in ("Acme", "4471", "99.12", "Traceback"):
            self.assertNotIn(value, serialized)
        # One rule for every traceback: the class token after the last frame whatever it
        # is named and however many lines its message has, and the innermost frame in the
        # repository (never a stdlib one), never the message.
        for mode, error_class, raised in (
                ("custom", "scripts.fake_errors.RowUnresolvable", "raise RowUnresolvable"),
                ("stdlib", "json.decoder.JSONDecodeError", "JSONDecoder().decode")):
            with self.subTest(mode=mode):
                failed = self.fake.run(spec(f"fake.{mode}", mode))
                stderr = failed["detail"]["stderr"]
                self.assertEqual((stderr["error_class"], stderr["frame"]), (
                    error_class, f"scripts/fake_errors.py:{line_of(FAKE_ERRORS, raised)}"))
                self.assertEqual(set(stderr), {"error_class", "frame", "lines"})
                for value in ("Acme", "4471", "99.12", "Traceback", "Expecting"):
                    self.assertNotIn(value, json.dumps(failed))
        # Only the exception line's own class token: a bare word another thread printed
        # beside it is never the class, and two bare words name none.
        frames = ("Traceback (most recent call last):\n"
                  '  File "/usr/lib/python3/x.py", line 3, in <module>\n    main()\n')
        for tail, wanted in (("Acme\nValueError: Acme 4471", {"error_class": "ValueError"}),
                             ("progress\nKeyError", {"error_class": "KeyError"}),
                             ("KeyError\nAcme", {"error": "stderr_unparsed"})):
            with self.subTest(tail=tail):
                self.assertEqual({k: v for k, v in lv._stderr_error(frames + tail).items()
                                  if k != "lines"}, wanted)
        garbage = self.fake.run(spec("fake.garbage", "garbage"))
        self.assertEqual(garbage["detail"]["stderr"], {"error": "stderr_unparsed", "lines": 1})
        self.assertNotIn("Acme", json.dumps(garbage))
        self.assertEqual(lv._stderr_error('{"error": "Acme Cosmetics Inc 99.12"}'),
                         {"error": "error_unparsed"})
        self.assertEqual(
            lv._stderr_error('{"error": "SourceHttpError", "http_status": 401, '
                             '"error_code": "invalid_key"}'),
            {"error": "SourceHttpError", "http_status": 401, "error_code": "invalid_key"})
        for bad in ("10**30", "-1", "4500123456", "true", '"401"', "99", "600"):
            bad = "1" + "0" * 30 if bad == "10**30" else bad
            self.assertEqual(
                lv._stderr_error('{"error": "SourceHttpError", "http_status": %s, '
                                 '"error_code": "po-4500123456"}' % bad),
                {"error": "SourceHttpError"}, bad)
        self.assertEqual(
            lv._stderr_error('{"error": "SourceHttpError", "http_status": "401", '
                             '"error_code": "x y"}'), {"error": "SourceHttpError"})

    def test_mapping_keys_are_counted_unless_the_path_allows_keys(self) -> None:
        value = spec("fake.keys", "keys", record=["by_customer", "checks", "nested"],
                     record_keys=["checks", "nested"])
        fields = self.fake.run(value)["detail"]["fields"]
        self.assertEqual(fields["by_customer"], {"keys": 2})
        self.assertEqual(fields["checks"], {"chain_ok": True, "rows": 4})
        self.assertEqual(fields["nested"], {"inner": {"x_ok": True}})
        allowed = self.fake.run({**value, "record_keys": ["by_customer"]})["detail"]["fields"]
        # Even an allowed path never shows keys that are not enum-shaped.
        self.assertEqual(allowed["by_customer"], {"keys": 2})
        self.assertEqual(allowed["checks"], {"keys": 2})
        self.assertNotIn("Acme", json.dumps(allowed))

    def test_disabled_spec_may_name_a_module_outside_the_allowlist_but_never_runs(self) -> None:
        marker = self.fake.root / "ran.txt"
        value = spec("fake.off", "ok", str(marker), disabled="awaiting scope decision")
        self.assertEqual(lv.validate_spec(value, allowed_modules=set()), [])
        self.assertEqual(lv.run_spec(value, root=self.fake.root, environ=self.fake.env,
                                     allowed_modules=set())["result"], "skipped")
        enabled = {k: v for k, v in value.items() if k != "disabled"}
        with self.assertRaises(lv.SpecError):
            lv.validate_spec(enabled, allowed_modules=set())
        self.assertEqual(lv.run_spec(enabled, root=self.fake.root, environ=self.fake.env,
                                     allowed_modules=set())["result"], "error")
        self.assertFalse(marker.exists())

    def test_requires_env_any_runs_when_one_is_set(self) -> None:
        value = spec("fake.any", requires_env_any=["FIRST_KEY", "SECOND_KEY"])
        skipped = self.fake.run(value)
        self.assertEqual(skipped["result"], "skipped")
        self.assertEqual(skipped["detail"], {"missing_env_any": ["FIRST_KEY", "SECOND_KEY"]})
        ran = self.fake.run(value, environ={**self.fake.env, "SECOND_KEY": "x" * 8})
        self.assertEqual(ran["result"], "pass")

    def test_a_spec_sees_only_the_credentials_it_declares(self) -> None:
        seen = self.fake.root / "env.json"
        environ = {**self.fake.env, "SECOND_KEY": "x" * 8, "BC_CLIENT_SECRET": "y" * 8,
                   "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic z", "PLAIN_SETTING": "1"}
        result = self.fake.run(spec("fake.env", "env", str(seen), requires_env=["FAKE_API_KEY"],
                                    requires_env_any=["FIRST_KEY", "SECOND_KEY"]),
                               environ=environ)
        self.assertEqual(result["result"], "pass")
        names = set(json.loads(seen.read_text()))
        self.assertLessEqual({"FAKE_API_KEY", "SECOND_KEY", "PLAIN_SETTING", "PATH"}, names)
        self.assertFalse(names & {"BC_CLIENT_SECRET", "GIT_CONFIG_VALUE_0"})

    def test_reading_a_set_but_undeclared_credential_is_an_error_naming_it(self) -> None:
        # Never a silent run without it (e.g. an eval falling back to Portal-only data).
        environ = {**self.fake.env, "BC_CLIENT_SECRET": "z" * 8}
        result = self.fake.run(spec("fake.bc", "bc"), environ=environ)
        self.assertEqual((result["result"], result["detail"]),
                         ("error", {"undeclared_env": "BC_CLIENT_SECRET"}))
        declared = self.fake.run(spec("fake.bc", "bc", optional_env=["BC_CLIENT_SECRET"],
                                      record=["bc"]), environ=environ)
        self.assertEqual((declared["result"], declared["detail"]["fields"]["bc"]),
                         ("pass", True))

    def test_a_run_refuses_unless_head_is_origin_main(self) -> None:
        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(self.fake.root), *args], check=True,
                           capture_output=True)
        origin = self.fake.root.parent / f"{self.fake.root.name}-origin.git"
        self.addCleanup(shutil.rmtree, origin, True)
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
            "--allow-empty", "-m", "reviewed")
        git("remote", "add", "origin", str(origin))
        git("push", "-q", "origin", "main")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
            "--allow-empty", "-m", "unreviewed")
        marker = self.fake.root / "ran.txt"
        self.fake.write(spec("fake.ok", "ok", str(marker)))

        def run() -> int:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                return lv.main(["--main-sha", "a" * 40],
                               root=self.fake.root,
                               environ=self.fake.env, allowed_modules={FAKE_MODULE},
                               reviewed_main_only=True)
        self.assertEqual(run(), 2)
        self.assertFalse(marker.exists())
        git("push", "-q", "origin", "main")
        spec_file = self.fake.specs / "fake.ok.json"
        git("add", str(spec_file))
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "spec")
        git("push", "-q", "origin", "main")
        spec_file.write_text(spec_file.read_text() + " ")  # a local edit is not reviewed main
        self.assertEqual(run(), 2)
        self.assertFalse(marker.exists())
        git("checkout", "--", str(spec_file))
        self.assertEqual(run(), 0)
        self.assertTrue(marker.exists())

    def test_unknown_expect_key_is_refused_not_silently_ignored(self) -> None:
        with self.assertRaises(lv.SpecError):
            lv.validate_spec(spec(expect={"exit_code": 0, "json_fields": [],
                                          "json_regex": {"a": "b"}}),
                             allowed_modules={FAKE_MODULE})
        warnings = lv.validate_spec({**spec(), "owner": "someone"},
                                    allowed_modules={FAKE_MODULE})
        self.assertEqual(warnings, ["unknown spec key 'owner' ignored"])

    def test_timeout_kills_the_command_and_fails(self) -> None:
        started = time.monotonic()
        result = self.fake.run(spec("fake.sleep", "sleep", timeout_seconds=1))
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result["result"], "fail")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["detail"], {"timeout_seconds": 1, "timed_out": True,
                                            "failures": ["timeout"]})

    def test_missing_env_skips_with_names_never_values(self) -> None:
        marker = self.fake.root / "ran.txt"
        value = spec("fake.env", "ok", str(marker),
                     requires_env=["FAKE_API_KEY", "MISSING_ONE", "MISSING_TWO"])
        result = self.fake.run(value)
        self.assertEqual(result["result"], "skipped")
        self.assertEqual(result["detail"], {"missing_env": ["MISSING_ONE", "MISSING_TWO"]})
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertFalse(marker.exists())
        empty = self.fake.run(value, environ={**self.fake.env, "MISSING_ONE": "",
                                              "MISSING_TWO": "x"})
        self.assertEqual(empty["detail"], {"missing_env": ["MISSING_ONE"]})

    def test_disabled_spec_is_skipped_with_its_reason(self) -> None:
        result = self.fake.run(spec("fake.off", disabled="calls tools/call"))
        self.assertEqual(result["result"], "skipped")
        self.assertEqual(result["detail"], {"disabled": "calls tools/call"})

    def test_expectations_fail_with_named_paths(self) -> None:
        result = self.fake.run(spec(expect={
            "exit_code": 0, "json_fields": ["token", "nope"],
            "json_equals": {"token": "no", "nested.flag": 1},
            "json_min": {"count": 10}, "json_empty": ["items[*].name"],
            "json_absent": ["items[*].absent"]}))
        self.assertEqual(result["result"], "fail")
        self.assertEqual(result["detail"]["failures"], [
            "missing nope", "token != expected", "nested.flag != expected", "count < 10",
            "items[*].name not empty", "items[*].absent present"])
        text = self.fake.run(spec("fake.text", "text"))
        self.assertEqual(text["result"], "fail")
        self.assertEqual(text["detail"]["failures"], ["stdout is not a JSON object"])
        self.assertIs(text["detail"]["stdout_json"], False)

    def test_detail_is_capped(self) -> None:
        result = self.fake.run(spec("fake.big", "big", record=[f"field_{i}"
                                                               for i in range(400)]))
        self.assertEqual(result["result"], "pass")
        self.assertLessEqual(len(json.dumps(result["detail"])), lv.MAX_DETAIL_CHARS)
        self.assertEqual(result["detail"]["fields_truncated"],
                         {"paths": 400 - len(result["detail"]["fields"])})
        self.assertNotIn("field_399", result["detail"]["fields"])
        # run_spec hands the spec to the cap: the last field, once asserted, stays recorded
        asserted = self.fake.run(spec("fake.big", "big", record=[f"field_{i}"
                                                                 for i in range(400)],
                                      expect={"exit_code": 0, "json_fields": ["token"],
                                              "json_equals": {"field_399.a": 399}}))
        self.assertEqual(asserted["result"], "pass")
        self.assertLessEqual(len(json.dumps(asserted["detail"])), lv.MAX_DETAIL_CHARS)
        self.assertEqual(asserted["detail"]["fields"]["field_399"], {"keys": 2})
        self.assertEqual(asserted["detail"]["fields_truncated"],
                         {"paths": 400 - len(asserted["detail"]["fields"])})

    def test_eval_blocks_are_lifted_and_cannot_override_the_verifiers_keys(self) -> None:
        forged = {"pass": "3/4", "result": "pass", "spec": "x", "exit_code": 0,
                  "failures": [], "by_kind": {"receipt": {"pass": "1/1"}}}
        checks = [
            {"id": "eval.sample", "result": "fail", "spec": "verify/live-checks/eval.sample.json",
             "exit_code": 3, "detail": {"fields": {"eval": forged},
                                        "failures": ["exit_code:3"]}},
            {"id": "case.other", "result": "pass", "detail": {"fields": {"eval": {"pass": "1/1"}}}},
            {"id": "eval.no_block", "result": "pass", "detail": {"fields": {"count": 1}}},
        ]
        report = lv.build_report([], checks, root=self.fake.root, main_sha="abc",
                                 previous_main_sha=None)
        self.assertEqual(set(report["eval"]), {"eval.sample"})
        lifted = report["eval"]["eval.sample"]
        self.assertEqual(lifted["result"], "fail")
        self.assertEqual(lifted["exit_code"], 3)
        self.assertEqual(lifted["spec"], "verify/live-checks/eval.sample.json")
        self.assertEqual(lifted["failures"], ["exit_code:3"])
        self.assertEqual(lifted["pass"], "3/4")
        self.assertEqual(lifted["by_kind"], {"receipt": {"pass": "1/1"}})
        none = lv.build_report([], checks[1:], root=self.fake.root, main_sha="abc",
                               previous_main_sha=None)
        self.assertNotIn("eval", none)

    def test_dry_run_validates_without_executing_and_only_selects(self) -> None:
        marker = self.fake.root / "ran.txt"
        self.fake.write(spec("fake.a", "ok", str(marker)))
        self.fake.write(spec("fake.b", "ok", str(marker)))
        code, text = self.fake.main("--dry-run")
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["summary"], {"specs": 2, "valid": 2, "invalid": 0})
        self.assertFalse(marker.exists())
        code, text = self.fake.main("--dry-run", "--only", "fake.b")
        self.assertEqual([s["id"] for s in json.loads(text)["specs"]], ["fake.b"])
        code, _ = self.fake.main("--only", "fake.nope")
        self.assertEqual(code, 2)
        (self.fake.specs / "fake.c.json").write_text(json.dumps(spec("fake.other")))
        code, text = self.fake.main("--dry-run")
        self.assertEqual(code, 1)
        bad = [s for s in json.loads(text)["specs"] if not s["valid"]]
        self.assertEqual([s["id"] for s in bad], ["fake.c"])
        self.assertIn("does not match id", bad[0]["error"])
        code, text = self.fake.main("--only", "fake.a")
        self.assertEqual(code, 0)
        self.assertTrue(marker.exists())
        self.assertEqual([c["id"] for c in json.loads(text)["checks"]], ["fake.a"])


    def test_daily_specs_leave_the_hourly_run_and_run_on_their_own_cadence(self) -> None:
        hourly_marker = self.fake.root / "hourly.txt"
        daily_marker = self.fake.root / "daily.txt"
        self.fake.write(spec("fake.hourly", "ok", str(hourly_marker)))
        self.fake.write(spec("fake.daily", "ok", str(daily_marker), cadence="daily",
                             timeout_seconds=7200))
        code, text = self.fake.main()  # default: the hourly run
        report = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual([c["id"] for c in report["checks"]], ["fake.hourly"])
        self.assertEqual(report["cadence"], "hourly")
        self.assertEqual(report["not_run"], {"daily": ["fake.daily"]})
        self.assertTrue(hourly_marker.exists())
        self.assertFalse(daily_marker.exists())
        hourly_marker.unlink()
        code, text = self.fake.main("--cadence", "daily")
        report = json.loads(text)
        self.assertEqual([c["id"] for c in report["checks"]], ["fake.daily"])
        self.assertEqual((report["cadence"], report["not_run"]),
                         ("daily", {"hourly": ["fake.hourly"]}))
        self.assertTrue(daily_marker.exists())
        self.assertFalse(hourly_marker.exists())
        code, text = self.fake.main("--cadence", "all")
        self.assertEqual(sorted(c["id"] for c in json.loads(text)["checks"]),
                         ["fake.daily", "fake.hourly"])
        self.assertNotIn("not_run", json.loads(text))
        # --only runs the named id whatever its cadence, and refuses a cadence with it.
        code, text = self.fake.main("--only", "fake.daily")
        self.assertEqual(([c["id"] for c in json.loads(text)["checks"]],
                          json.loads(text)["cadence"]), (["fake.daily"], "only"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.fake.main("--only", "fake.daily", "--cadence", "daily")
        code, text = self.fake.main("--dry-run")
        cadences = {s["id"]: s["cadence"] for s in json.loads(text)["specs"]}
        self.assertEqual(cadences, {"fake.daily": "daily", "fake.hourly": "hourly"})

    def _three_daily(self) -> list[str]:
        ids = ["fake.a", "fake.b", "fake.c"]
        for i in ids:
            self.fake.write(spec(i, cadence="daily", timeout_seconds=60))
        return ids

    def test_killed_run_keeps_every_finished_spec_in_a_valid_partial_report(self) -> None:
        ids = self._three_daily()
        for kill_at in (1, 3):
            with self.subTest(kill_at=kill_at):
                out = self.fake.root / f"report-{kill_at}.json"
                real = lv.run_spec
                calls = []

                def kill(*args, **kwargs):
                    calls.append(1)
                    if len(calls) == kill_at:
                        raise KeyboardInterrupt  # the runtime killed the command
                    return real(*args, **kwargs)

                with mock.patch.object(lv, "run_spec", kill), \
                        self.assertRaises(KeyboardInterrupt):
                    self.fake.main("--cadence", "daily", "--output", str(out))
                report = json.loads(out.read_text())
                self.assertEqual([c["id"] for c in report["checks"]], ids[:kill_at - 1])
                self.assertIs(report["complete"], False)
                self.assertEqual(report["pending"], ids[kill_at - 1:])
                self.assertEqual((report["stop_reason"], report["exit_code"]),
                                 ("interrupted", 130))
                self.assertFalse(out.with_name(out.name + ".tmp").exists())

    def test_finished_run_is_complete_and_only_runs_the_named_spec(self) -> None:
        self._three_daily()
        out = self.fake.root / "report.json"
        self.fake.main("--cadence", "daily", "--output", str(out))
        report = json.loads(out.read_text())
        self.assertIs(report["complete"], True)
        self.assertNotIn("pending", report)
        code, text = self.fake.main("--only", "fake.b")
        self.assertEqual([c["id"] for c in json.loads(text)["checks"]], ["fake.b"])

    def test_partial_runs_merge_into_one_complete_daily_report(self) -> None:
        self._three_daily()
        target = self.fake.root / "daily.json"
        # Every clock read is 1 s after the last (a loaded machine), and the run has a main
        # SHA, so each check has a known origin: the set is as old as its oldest check.
        start = datetime.now(timezone.utc).replace(microsecond=0)
        reads = iter(range(10 ** 6))
        sha = ("--main-sha", "1" * 40)
        with mock.patch.object(lv, "_now", lambda: start + timedelta(seconds=next(reads))):
            self.fake.main("--only", "fake.a", "--merge-into", str(target), *sha)
            first = json.loads(target.read_text())
            self.assertEqual((first["complete"], first["pending"], first["cadence"]),
                             (False, ["fake.b", "fake.c"], "daily"))
            self.fake.main("--only", "fake.b", "--only", "fake.c",
                           "--merge-into", str(target), *sha)
        merged = json.loads(target.read_text())
        self.assertEqual([c["id"] for c in merged["checks"]], ["fake.a", "fake.b", "fake.c"])
        self.assertIs(merged["complete"], True)
        self.assertNotIn("pending", merged)
        self.assertEqual(merged["summary"]["pass"], 3)
        self.assertLess(merged["started_at"], first["checks"][0]["checked_at"])
        status = lv.daily_status(target, start + timedelta(seconds=next(reads)))
        self.assertEqual(status["daily_stale"], False)
        self.assertEqual(status["daily_last_run_at"], first["checks"][0]["checked_at"])
        # Re-running one spec replaces its check instead of duplicating it.
        self.fake.main("--only", "fake.b", "--merge-into", str(target), *sha)
        again = json.loads(target.read_text())
        self.assertEqual(len(again["checks"]), 3)
        self.assertIs(again["complete"], True)

    def test_stalled_spec_times_out_and_the_run_continues(self) -> None:
        self.fake.write(spec("fake.a_stall", "sleep", timeout_seconds=1))
        self.fake.write(spec("fake.b_after", "ok"))
        started = time.monotonic()
        code, text = self.fake.main()
        self.assertLess(time.monotonic() - started, 20)
        by_id = {c["id"]: c for c in json.loads(text)["checks"]}
        self.assertEqual(by_id["fake.a_stall"]["result"], "fail")
        self.assertTrue(by_id["fake.a_stall"]["detail"]["timed_out"])
        self.assertEqual(by_id["fake.b_after"]["result"], "pass")
        self.assertEqual(code, 1)

    def test_a_runner_exception_becomes_an_error_check_and_the_run_continues(self) -> None:
        self.fake.write(spec("fake.a"))
        self.fake.write(spec("fake.b"))
        real = lv.run_spec

        def flaky(value, **kwargs):
            if value["id"] == "fake.a":
                raise RuntimeError("boom with row 4471")
            return real(value, **kwargs)
        with mock.patch.object(lv, "run_spec", flaky):
            _, text = self.fake.main()
        by_id = {c["id"]: c for c in json.loads(text)["checks"]}
        self.assertEqual(by_id["fake.a"]["detail"], {"runner_error": "RuntimeError"})
        self.assertEqual(by_id["fake.b"]["result"], "pass")

    def _upstream_specs(self, upstream_mode: str) -> Path:
        marker = self.fake.root / "dependent-ran.txt"
        # Written first alphabetically after the dependents: the upstream must still run first.
        self.fake.write(spec("fake.dependent", "ok", str(marker), requires=["zz.upstream"],
                             source="portal-workbench-api"))
        self.fake.write(spec("zz.upstream", upstream_mode, source="portal-workbench-api"))
        return marker

    def test_required_spec_runs_first(self) -> None:
        self._upstream_specs("ok")
        _, text = self.fake.main()
        report = json.loads(text)
        self.assertEqual([c["id"] for c in report["checks"]], ["zz.upstream", "fake.dependent"])
        self.assertEqual([c["result"] for c in report["checks"]], ["pass", "pass"])

    def test_requires_cycle_is_a_spec_error(self) -> None:
        self.fake.write(spec("fake.a", requires=["fake.b"]))
        self.fake.write(spec("fake.b", requires=["fake.a"]))
        self.fake.write(spec("fake.c"))
        by_id = {e["id"]: e for e in lv.load_specs(self.fake.specs,
                                                   allowed_modules={FAKE_MODULE})}
        for name in ("fake.a", "fake.b"):
            self.assertIn("requires cycle", by_id[name]["error"])
        self.assertIsNone(by_id["fake.c"]["error"])

    def test_report_names_the_failing_check_ids(self) -> None:
        self._upstream_specs("fail")
        _, text = self.fake.main()
        report = json.loads(text)
        self.assertEqual(report["failing"], ["zz.upstream"])
        merged = lv.merge_reports(None, report, ["zz.upstream", "fake.dependent"])
        self.assertEqual(merged["failing"], ["zz.upstream"])

    def test_failed_upstream_blocks_dependents_with_the_recorded_reason(self) -> None:
        marker = self._upstream_specs("fail")
        code, text = self.fake.main()
        by_id = {c["id"]: c for c in json.loads(text)["checks"]}
        self.assertEqual(by_id["zz.upstream"]["result"], "fail")
        blocked = by_id["fake.dependent"]
        self.assertEqual(blocked["result"], "blocked")
        detail = dict(blocked["detail"])
        self.assertRegex(detail.pop("blocked_since"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(detail, {
            "reason": "upstream portal-workbench-api SourceUnavailable",
            "upstream": "zz.upstream", "cause": "upstream_outage", "blocked_hours": 0.0,
            "red": False})
        self.assertFalse(marker.exists())
        self.assertEqual(code, 1)

    def test_degraded_upstream_runs_dependents_and_blocks_only_those_needing_a_gap(self):
        marks = {name: self.fake.root / f"{name}.ran" for name in ("any", "needs", "other")}
        self.fake.write(spec("zz.upstream", "degraded", source="portal-workbench-api",
                             expect={"exit_code": 0, "json_fields": ["token"],
                                     "json_equals": {"degraded": False}},
                             expect_blocked={"exit_code": 0, "json_fields": ["token"],
                                             "json_min": {"available_relation_count": 1}}))
        self.fake.write(spec("fake.any", "ok", str(marks["any"]), requires=["zz.upstream"]))
        self.fake.write(spec("fake.needs", "ok", str(marks["needs"]), requires=["zz.upstream"],
                             requires_relations=["public.app_sales_order",
                                                 "sage.oe_order_header"]))
        self.fake.write(spec("fake.other", "ok", str(marks["other"]), requires=["zz.upstream"],
                             requires_relations=["public.app_sales_order"]))
        _, text = self.fake.main()
        report = json.loads(text)
        by_id = {c["id"]: c for c in report["checks"]}
        upstream = by_id["zz.upstream"]["detail"]["blocked"]
        self.assertEqual((by_id["zz.upstream"]["result"], upstream["cause"],
                          upstream["measured"]), ("blocked", "relation_unavailable", True))
        self.assertEqual(upstream["parts"][1], {
            "part": "sage.oe_order_header", "source": "portal-workbench-api",
            "cause": "relation_unavailable",
            "reason": "148 approved columns are missing or not granted"})
        self.assertEqual((by_id["fake.any"]["result"], by_id["fake.other"]["result"]),
                         ("pass", "pass"))
        needs = by_id["fake.needs"]
        self.assertEqual((needs["result"], needs["detail"]["cause"],
                          needs["detail"]["relations"], needs["detail"]["upstream"]),
                         ("blocked", "relation_unavailable", ["sage.oe_order_header"],
                          "zz.upstream"))
        self.assertEqual([marks[n].exists() for n in ("any", "needs", "other")],
                         [True, False, True])
        self.assertEqual(report["tiers"]["access"]["status"], "partial")
        self.assertEqual(report["tiers"]["access"]["blocked_ids"],
                         {"fake.needs": "relation_unavailable",
                          "zz.upstream": "relation_unavailable"})

    def test_upstream_that_fails_closed_records_its_reason_and_blocks_dependents(self):
        marker = self.fake.root / "dep.ran"
        self.fake.write(spec("zz.upstream", "notready", source="portal-workbench-api"))
        self.fake.write(spec("fake.dependent", "ok", str(marker), requires=["zz.upstream"],
                             requires_relations=["public.app_sales_order"]))
        _, text = self.fake.main()
        by_id = {c["id"]: c for c in json.loads(text)["checks"]}
        self.assertEqual((by_id["zz.upstream"]["result"],
                          by_id["zz.upstream"]["detail"]["stderr"]),
                         ("fail", {"error": "WorkbenchNotReady",
                                   "reason": "health: recovery is not true"}))
        self.assertEqual((by_id["fake.dependent"]["result"],
                          by_id["fake.dependent"]["detail"]["cause"]),
                         ("blocked", "upstream_outage"))
        self.assertFalse(marker.exists())

    def test_upstream_result_in_the_merge_report_blocks_a_lone_dependent(self) -> None:
        marker = self._upstream_specs("fail")
        target = self.fake.root / "daily.json"
        self.fake.main("--only", "zz.upstream", "--merge-into", str(target))
        _, text = self.fake.main("--only", "fake.dependent", "--merge-into", str(target))
        self.assertEqual(json.loads(text)["checks"][-1]["result"], "blocked")
        self.assertFalse(marker.exists())

    def test_requires_is_validated(self) -> None:
        with self.assertRaises(lv.SpecError):
            lv.validate_spec(spec(requires=["fake.ok"]), allowed_modules={FAKE_MODULE})
        with self.assertRaises(lv.SpecError):
            lv.validate_spec(spec(requires="portal.health_catalog"),
                             allowed_modules={FAKE_MODULE})
        self.assertEqual(lv.validate_spec(spec(requires=["portal.health_catalog"]),
                                          allowed_modules={FAKE_MODULE}), [])
        for bad in ({"requires_relations": ["sage.oe_order_header"]},
                    {"requires": ["portal.health_catalog"], "requires_relations": ["oe"]}):
            with self.assertRaises(lv.SpecError):
                lv.validate_spec(spec(**bad), allowed_modules={FAKE_MODULE})

    SHARED = "/capture.sqlite"

    @staticmethod
    def _audience(reader="service", env=("FAKE_API_KEY",)) -> dict:
        return {"sources": ["fake-source"], "env": list(env), "reader": reader}

    def _session_spec(self, spec_id: str, log: Path, *, reader="service", crash=False,
                      **extra) -> None:
        args = ["share", str(self.fake.root / f"{spec_id}.txt"),
                "{session_state}" + self.SHARED, str(log)]
        args += ["crash"] if crash else [] if reader == "service" else reader
        self.fake.write(spec(spec_id, *args, source="fake-source",
                             optional_env=["FAKE_API_KEY"],
                             session_audience={self.SHARED: self._audience(reader)}, **extra))

    def _session_specs(self, *, crash: bool = False, **extra) -> Path:
        """``z.producer`` and its consumer ``a.consumer`` (declared first: requires orders
        the producer first) name one session path under one capture audience;
        ``c.stranger`` names it under the same audience without requiring the producer,
        ``d.other`` requires it but names the path under another audience (its reader)."""
        log = self.fake.root / "session.log"
        self._session_spec("z.producer", log, crash=crash, **extra)
        self._session_spec("a.consumer", log, requires=["z.producer"], **extra)
        self._session_spec("c.stranger", log, **extra)
        self._session_spec("d.other", log, reader=["other-audience.json"],
                           requires=["z.producer"], **extra)
        return log

    @staticmethod
    def _seen(log: Path) -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()]

    def test_a_session_store_is_shared_by_capture_audience_never_by_check_name(self) -> None:
        log = self._session_specs()
        stale = "2000-01-01T00:00:00.000000Z"  # an inherited value never reaches a check
        self.fake.env[lv.CAPTURE_FRESH_SINCE_ENV] = stale
        started = datetime.now(timezone.utc)
        _, text = self.fake.main()
        checks = {c["id"]: c for c in json.loads(text)["checks"]}
        seen = dict(zip(checks, self._seen(log)))
        # Every check of the session gets its start, so a consumer reuses only what this
        # session captured (integrations/bc_read.py reads the same variable).
        from integrations import bc_read
        self.assertEqual(lv.CAPTURE_FRESH_SINCE_ENV, bc_read.FRESH_SINCE_ENV)
        since = {s["since"] for s in seen.values()}
        self.assertEqual(len(since), 1)
        self.assertGreaterEqual(datetime.strptime(since.pop(), "%Y-%m-%dT%H:%M:%S.%fZ")
                                .replace(tzinfo=timezone.utc), started)
        alone = self.fake.root / "alone.log"  # no session directory: never inherited
        self.fake.run(spec("fake.ok", "share", str(self.fake.root / "alone.txt"),
                           "{session_state}" + self.SHARED, str(alone), source="fake-source",
                           optional_env=["FAKE_API_KEY"],
                           session_audience={self.SHARED: self._audience()}))
        self.assertIsNone(self._seen(alone)[0]["since"])
        self.assertEqual(list(checks), ["z.producer", "a.consumer", "d.other", "c.stranger"])
        self.assertEqual({i: s["seen"] for i, s in seen.items()},
                         {"z.producer": False, "a.consumer": True, "d.other": False,
                          "c.stranger": True})
        # One store per audience: the stranger shares the producer's (same audience, no
        # requires); d.other requires the producer but reads as another audience: cold.
        self.assertEqual({seen[i]["dir"] for i in ("z.producer", "a.consumer", "c.stranger")},
                         {seen["z.producer"]["dir"]})
        self.assertNotEqual(seen["d.other"]["dir"], seen["z.producer"]["dir"])
        self.assertEqual({s["mode"] for s in seen.values()}, {0o700})
        session = Path(seen["z.producer"]["dir"]).parent
        self.assertEqual(Path(seen["d.other"]["dir"]).parent, session)
        self.assertFalse(session.exists())  # removed when the session ended
        for repo in (self.fake.root, lv.ROOT):
            self.assertFalse(session.resolve().is_relative_to(repo.resolve()))
        producer = checks["z.producer"]
        run = {"id": "z.producer", "checked_at": producer["checked_at"],
               "main_sha": producer["main_sha"]}
        shared = {"paths": {self.SHARED: "shared"}, "shared_store": True,
                  "producer_verdict": {self.SHARED: "pass"},
                  "producer_run": {self.SHARED: run}}
        self.assertEqual({i: c["detail"]["session_state"] for i, c in checks.items()},
                         {"z.producer": {"paths": {self.SHARED: "produced"}},
                          "a.consumer": shared, "c.stranger": shared,
                          "d.other": {"paths": {self.SHARED: "cold_audience"}}})
        self.assertEqual({c["result"] for c in checks.values()}, {"pass"})
        # Fails closed: the directory would be inside the repository, or a bad path.
        with mock.patch.object(tempfile, "tempdir", str(self.fake.root)):
            _, text = self.fake.main("--only", "z.producer")
        self.assertIn("inside the repository",
                      json.loads(text)["checks"][0]["detail"]["refused"])
        self.assertEqual(list(self.fake.root.glob("live-verify-session-*")), [])
        audience = {"session_audience": {"/a": self._audience()}, "source": "fake-source",
                    "optional_env": ["FAKE_API_KEY"]}
        for bad in ("{session_state}", "{session_state}/../up", "x{session_state}/y",
                    "{session_state}/a{tmp}", "--db={session_state}/a/{session_state}/b"):
            with self.assertRaises(lv.SpecError, msg=bad):
                lv.validate_spec(spec("fake.ok", "ok", bad, **audience),
                                 allowed_modules={FAKE_MODULE})
        self.assertEqual(lv.session_paths(spec("fake.ok", "ok", "--db={session_state}/a/b.db")),
                         ["/a/b.db"])
        # An undeclared or inconsistent capture audience refuses the spec, never runs it.
        good = spec("fake.ok", "ok", "--db={session_state}/a", "x.json", **audience)
        lv.validate_spec(good, allowed_modules={FAKE_MODULE})
        for name, bad in (
                ("undeclared", {"session_audience": {}}),
                ("another path", {"session_audience": {"/b": self._audience()}}),
                ("source", {"source": "other-source"}),
                ("credential", {"optional_env": []}),
                ("reader", {"session_audience": {"/a": self._audience(["y.json"])}}),
                ("empty reader", {"session_audience": {"/a": self._audience([])}}),
                ("key", {"session_audience": {"/a": {**self._audience(), "id": "z"}}})):
            with self.assertRaises(lv.SpecError, msg=name):
                lv.validate_spec({**good, **bad}, allowed_modules={FAKE_MODULE})
        # A spec refused for it is reported as an error beside its producer's run.
        self.fake.write({**spec("b.broken", "share", "x", "{session_state}" + self.SHARED, "y"),
                         "requires": ["z.producer"]})
        _, text = self.fake.main("--only", "z.producer", "--only", "b.broken")
        checks = {c["id"]: c for c in json.loads(text)["checks"]}
        self.assertEqual((checks["z.producer"]["result"], checks["b.broken"]["result"]),
                         ("pass", "error"))
        self.assertIn("session_audience", checks["b.broken"]["detail"]["refused"])

    def test_a_consumer_of_a_failed_or_absent_producer_captures_cold(self) -> None:
        log = self._session_specs(crash=True)
        _, text = self.fake.main()
        checks = {c["id"]: c for c in json.loads(text)["checks"]}
        seen = dict(zip([i for i in checks if i != "d.other"], self._seen(log)))
        self.assertEqual((checks["z.producer"]["result"], checks["a.consumer"]["result"]),
                         ("fail", "pass"))  # a session producer never blocks its consumer
        self.assertEqual(checks["d.other"]["result"], "blocked")  # a requires that is not
        self.assertFalse(seen["a.consumer"]["seen"])
        self.assertNotEqual(seen["z.producer"]["dir"], seen["a.consumer"]["dir"])
        # The consumer names the failed producer's own verdict and run beside shared_store.
        self.assertEqual(checks["a.consumer"]["detail"]["session_state"], {
            "paths": {self.SHARED: "cold_failed"}, "shared_store": False,
            "producer_verdict": {self.SHARED: "fail"},
            "producer_run": {self.SHARED: {"id": "z.producer",
                                           "checked_at": checks["z.producer"]["checked_at"],
                                           "main_sha": checks["z.producer"]["main_sha"]}}})
        self.assertEqual(checks["c.stranger"]["detail"]["session_state"],
                         {"paths": {self.SHARED: "cold_failed"}})
        _, text = self.fake.main("--only", "a.consumer")  # the producer is not in this one
        self.assertEqual(json.loads(text)["checks"][0]["detail"]["session_state"],
                         {"paths": {self.SHARED: "cold_not_run"}, "shared_store": False})

    def test_resume_daily_runs_a_pending_consumers_producer_in_its_session_once(self) -> None:
        log = self._session_specs(cadence="daily", timeout_seconds=60)
        for other in ("c.stranger", "d.other"):
            (self.fake.specs / f"{other}.json").unlink()
        daily = self.fake.root / "results" / "daily"

        def resume(name: str, budget: str) -> dict:
            self.fake.main("--resume-daily", str(daily / name), "--budget-seconds", budget)
            return json.loads((daily / name).read_text())
        first = resume("one.json", "30")  # the producer only: its consumer does not fit
        self.assertEqual(first["pending"], ["a.consumer"])
        second = resume("one.json", "100")  # the producer runs again, then its consumer
        self.assertEqual([c["detail"]["session_state"].get("shared_store")
                          for c in second["checks"]], [None, True])
        self.assertEqual([e["seen"] for e in self._seen(log)], [False, False, True])
        self.assertIs(second["complete"], True)
        # The re-run keeps the run it replaced; the consumer cites the run it used.
        producer, consumer = second["checks"]
        before = first["checks"][0]
        self.assertEqual(producer["earlier_runs"],
                         [{k: before[k] for k in lv.EARLIER_KEYS if k in before}])
        self.assertEqual(consumer["detail"]["session_state"]["producer_run"][self.SHARED],
                         {k: producer[k] for k in ("id", "checked_at", "main_sha")})
        self.assertNotIn("earlier_runs", consumer)
        # A producer slower than its last run: the consumer still fits on its measured
        # duration, so it runs after it in the same session; the producer runs once.
        log.unlink()
        resume("two.json", "1")  # the producer only: the first spec always runs
        self.fake.env["FAKE_SLEEP"] = "1"
        third = resume("two.json", "61")
        self.assertEqual([e["seen"] for e in self._seen(log)], [False, False, True])
        self.assertIs(third["complete"], True)
        # One that leaves no room at all: the consumer waits once and then captures cold,
        # so the producer never re-runs for a consumer that then does not run.
        log.unlink()
        del self.fake.env["FAKE_SLEEP"]
        resume("three.json", "1")
        self.fake.env["FAKE_SLEEP"] = "5"
        fourth = resume("three.json", "6")  # its 1.5 x ~1 s does not fit after a 5 s producer
        self.assertEqual(fourth["deferred_budget"]["a.consumer"]["after_producers"], True)
        del self.fake.env["FAKE_SLEEP"]
        fifth = resume("three.json", "61")
        self.assertEqual(fifth["checks"][-1]["detail"]["session_state"]["paths"],
                         {self.SHARED: "cold_not_run"})
        self.assertEqual(len(self._seen(log)), 3)  # producer, producer, consumer cold
        self.assertIs(fifth["complete"], True)

    def test_resume_daily_runs_a_consumers_producer_chain_deepest_first(self) -> None:
        """A consumer whose session producer has a session producer of its own (the
        mirror -> session projection -> consumer chain) gets both re-run in its command,
        deepest first, so it shares instead of capturing cold."""
        log = self.fake.root / "session.log"
        self._session_spec("z.base", log, cadence="daily", timeout_seconds=20)
        self._session_spec("y.mid", log, requires=["z.base"], cadence="daily",
                           timeout_seconds=20)
        self._session_spec("a.consumer", log, requires=["y.mid"], cadence="daily",
                           timeout_seconds=100)
        daily = self.fake.root / "results" / "daily"

        def resume(budget: str) -> dict:
            self.fake.main("--resume-daily", str(daily / "set.json"), "--budget-seconds",
                           budget)
            return json.loads((daily / "set.json").read_text())
        first = resume("50")   # the chain runs; its consumer does not fit
        self.assertEqual(first["pending"], ["a.consumer"])
        second = resume("200")
        ran = [c["id"] for c in second["checks"]]
        self.assertEqual(ran, ["z.base", "y.mid", "a.consumer"])
        self.assertEqual([c.get("earlier_runs") is not None for c in second["checks"]],
                         [True, True, False])
        consumer = second["checks"][-1]["detail"]["session_state"]
        self.assertEqual((consumer["paths"], consumer["shared_store"]),
                         ({self.SHARED: "shared"}, True))
        # It cites the store's writer, base (mid shared it, so it wrote nothing): the run
        # itself, never a stamp that matches only when base and mid end in one second.
        self.assertEqual(consumer["producer_run"][self.SHARED],
                         {k: second["checks"][0][k] for k in ("id", "checked_at", "main_sha")})
        # second command: base fresh, mid and the consumer read what base wrote
        self.assertEqual([e["seen"] for e in self._seen(log)][2:], [False, True, True])

    def test_previous_report_is_kept_when_no_daily_file_is_present(self) -> None:
        argv = self.fake.root / "argv.json"
        self.fake.write(spec("fake.counts", "argv", str(argv), lv.DAILY_REPORT_ARG,
                             cadence="daily"))
        daily = self.fake.root / "results" / "daily"
        daily.mkdir(parents=True)
        absent = daily / "latest.json"

        def complete_set(hours_ago: float) -> None:
            at = lv._utc(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
            (daily / "old.json").write_text(json.dumps({
                "schema": lv.SCHEMA, "cadence": "daily", "complete": True, "generated_at": at,
                "checks": [{"id": "fake.counts", "result": "pass", "main_sha": "a" * 40,
                            "checked_at": at}]}))
        complete_set(47)
        self.fake.main("--resume-daily", str(daily / "new.json"), "--daily-report", str(absent))
        self.assertEqual(json.loads(argv.read_text()),
                         [f"--previous-report={daily / 'old.json'}"])
        self.assertNotIn("previous_daily_gap", json.loads((daily / "new.json").read_text()))
        # A set older than 48 h is never a probe's previous run: dropped, a recorded gap.
        (daily / "latest.json").unlink()  # the set just completed: the newest otherwise
        complete_set(49)
        self.fake.main("--only", "fake.counts", "--merge-into", str(daily / "new.json"))
        self.assertEqual(json.loads(argv.read_text()), [])
        gap = json.loads((daily / "new.json").read_text())["previous_daily_gap"]
        self.assertEqual((gap["gap"], gap["report"], gap["max_age_hours"]),
                         ("daily_report_stale", "old.json", 48))
        self.assertGreater(gap["age_hours"], 48)
        (daily / "old.json").unlink()  # no other complete set: dropped
        self.fake.main("--only", "fake.counts", "--merge-into", str(daily / "new.json"))
        self.assertEqual(json.loads(argv.read_text()), [])
        self.assertNotIn("previous_daily_gap", json.loads((daily / "new.json").read_text()))

    def test_a_blocked_or_timed_out_run_never_replaces_the_last_measured_previous_report(
            self) -> None:
        """mirror.relation_counts keeps its rotation only in its previous report's fields: a
        run that recorded none (blocked, timed out) passes the last one that did instead;
        a spec without carry_previous_measured (a consumer of the counter's fields) never
        gets an older report."""
        argv, consumer_argv = self.fake.root / "argv.json", self.fake.root / "consumer.json"
        self.fake.write(spec("fake.counts", "argv", str(argv), lv.DAILY_REPORT_ARG,
                             cadence="daily", carry_previous_measured=True))
        self.fake.write(spec("fake.consumer", "argv", str(consumer_argv), lv.DAILY_REPORT_ARG,
                             cadence="daily"))
        for bad in (spec("fake.x", "argv", "x", lv.DAILY_REPORT_ARG, cadence="daily",
                         carry_previous_measured=1),
                    spec("fake.x", "argv", "x", cadence="daily", carry_previous_measured=True)):
            with self.assertRaises(lv.SpecError):
                lv.validate_spec(bad, allowed_modules={FAKE_MODULE})
        daily = self.fake.root / "results" / "daily"
        daily.mkdir(parents=True)

        def daily_set(name: str, hours_ago: float, check: dict) -> None:
            at = lv._utc(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
            (daily / name).write_text(json.dumps({
                "schema": lv.SCHEMA, "cadence": "daily", "complete": True, "generated_at": at,
                "checks": [{"id": name, "main_sha": "a" * 40, "checked_at": at, **check}
                           for name in ("fake.counts", "fake.consumer")]}))
        measured = {"result": "fail", "exit_code": 1,
                    "detail": {"fields": {"relations[*].rotation_rank": [0]}}}
        daily_set("older.json", 70, measured)
        daily_set("measured.json", 50, measured)
        for empty in ({"result": "fail", "exit_code": None,
                       "detail": {"timed_out": True, "failures": ["timeout"]}},
                      {"result": "blocked", "detail": {"reason": "upstream portal 503"}}):
            daily_set("latest.json", 26, empty)
            self.fake.main("--only", "fake.counts", "--merge-into", str(daily / "new.json"),
                           "--daily-report", str(daily / "latest.json"))
            self.assertEqual(json.loads(argv.read_text()),
                             [f"--previous-report={daily / 'measured.json'}"])
            check = json.loads((daily / "new.json").read_text())["checks"][0]
            self.assertEqual(check["detail"]["previous_report_carried"],
                             json.loads((daily / "measured.json").read_text())
                             ["checks"][0]["checked_at"])
            self.fake.main("--only", "fake.consumer", "--merge-into", str(daily / "new.json"),
                           "--daily-report", str(daily / "latest.json"))
            self.assertEqual(json.loads(consumer_argv.read_text()),
                             [f"--previous-report={daily / 'latest.json'}"])
        # A measured run in the daily report itself is passed as before; one past 96 h is
        # never carried (the empty report stays, as the probe then starts afresh).
        daily_set("latest.json", 26, measured)
        self.fake.main("--only", "fake.counts", "--merge-into", str(daily / "new.json"),
                       "--daily-report", str(daily / "latest.json"))
        self.assertEqual(json.loads(argv.read_text()),
                         [f"--previous-report={daily / 'latest.json'}"])
        (daily / "new.json").unlink()
        daily_set("latest.json", 26, {"result": "blocked", "detail": {}})
        daily_set("measured.json", 97, measured)
        (daily / "older.json").unlink()
        self.fake.main("--only", "fake.counts", "--merge-into", str(daily / "new.json"),
                       "--daily-report", str(daily / "latest.json"))
        self.assertEqual(json.loads(argv.read_text()),
                         [f"--previous-report={daily / 'latest.json'}"])
        self.assertNotIn("previous_report_carried",
                         json.loads((daily / "new.json").read_text())["checks"][0]["detail"])

    def test_a_run_sweeps_the_session_directories_killed_runs_left_behind(self) -> None:
        base = Path(tempfile.mkdtemp(dir=self.fake.root.parent))
        self.addCleanup(shutil.rmtree, base, True)
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        made = {}
        for name, pid, mode in (("dead", gone.pid, 0o700), ("live", os.getpid(), 0o700),
                                ("open", gone.pid, 0o755)):
            made[name] = base / f"{lv.SESSION_PREFIX}{pid}-{name}"
            made[name].mkdir(mode=mode)
            os.chmod(made[name], mode)
            (made[name] / "capture.sqlite").write_text("x")
        made["legacy"] = base / f"{lv.SESSION_PREFIX}abc123"  # not a pid-named one
        made["legacy"].mkdir(mode=0o700)
        target = base / "elsewhere"
        target.mkdir(mode=0o700)
        made["link"] = base / f"{lv.SESSION_PREFIX}{gone.pid}-link"
        made["link"].symlink_to(target)
        self._session_spec("z.producer", self.fake.root / "session.log")
        with mock.patch.object(tempfile, "tempdir", str(base)):
            _, text = self.fake.main("--only", "z.producer")
        self.assertEqual(json.loads(text)["session_dirs_swept"], 1)
        self.assertEqual({name for name, path in made.items() if os.path.lexists(path)},
                         {"live", "open", "legacy", "link"})
        self.assertTrue(target.is_dir())
        # This run's own directory went with it; nothing else left behind.
        self.assertEqual(sorted(p.name for p in base.iterdir()),
                         sorted([*(made[n].name for n in ("live", "open", "legacy", "link")),
                                 "elsewhere"]))

    def test_resume_daily_writes_the_file_even_when_partial(self) -> None:
        self._three_daily()
        target = self.fake.root / "results" / "daily" / "sha.json"
        # Budget fits only the first daily spec (each declares 60 s): the rest stay pending.
        code, _ = self.fake.main("--resume-daily", str(target), "--budget-seconds", "1")
        partial = json.loads(target.read_text())
        self.assertEqual(code, 0)
        self.assertIs(partial["complete"], False)
        self.assertEqual(len(partial["checks"]), 1)
        self.assertEqual(len(partial["pending"]), 2)
        self.fake.main("--resume-daily", str(target))
        done = json.loads(target.read_text())
        self.assertIs(done["complete"], True)
        self.assertEqual(sorted(c["id"] for c in done["checks"]),
                         ["fake.a", "fake.b", "fake.c"])

    def test_resume_daily_runs_only_the_pending_ids_under_its_own_sha_and_into_latest(self):
        self._three_daily()
        daily = self.fake.root / "results" / "daily"
        ran: list[str] = []
        real = lv.run_spec

        def record(value, **kwargs):
            ran.append(value["id"])
            return real(value, **kwargs)

        def resume(sha: str, *budget: str) -> dict:
            ran.clear()
            # The scheduler names the file at its session's start: the run writes its own SHA.
            with mock.patch.object(lv, "run_spec", record):
                self.fake.main("--resume-daily", str(daily / f"{'1' * 40}.json"),
                               "--main-sha", sha * 40, *budget)
            report = json.loads((daily / f"{sha * 40}.json").read_text())
            self.assertEqual(report["main_sha"], sha * 40)
            # latest.json is every write of the set, a partial one included.
            self.assertEqual(json.loads((daily / "latest.json").read_text()), report)
            return report

        first = resume("1", "--budget-seconds", "1")
        self.assertEqual((first["pending"], first["stop_reason"], first["exit_code"]),
                         (["fake.b", "fake.c"], "budget", 0))
        status = lv.daily_status(daily / "latest.json", datetime.now(timezone.utc))
        self.assertEqual((status["daily_pending"], status["daily_completed_at"],
                          status["daily_stale"]), (["fake.b", "fake.c"], None, True))
        # A daily spec landed after the set started has no result in it: pending in it too.
        self.fake.write(spec("fake.d", cadence="daily", timeout_seconds=60))
        kept = (daily / f"{'1' * 40}.json").read_text()
        second = resume("2", "--budget-seconds", "1")
        self.assertEqual((ran, second["pending"]), (["fake.b"], ["fake.c", "fake.d"]))
        self.assertEqual(second["started_at"], first["started_at"])  # one set, not per SHA
        self.assertEqual((daily / f"{'1' * 40}.json").read_text(), kept)
        third = resume("3")
        self.assertEqual(ran, ["fake.c", "fake.d"])  # never a check the set already holds
        self.assertIs(third["complete"], True)
        self.assertEqual({c["id"]: c["main_sha"][0] for c in third["checks"]},
                         {"fake.a": "1", "fake.b": "2", "fake.c": "3", "fake.d": "3"})
        # Carried checks keep their own origin (sha and moment) across every later resume.
        origin = lambda report, i: next((c["main_sha"], c["checked_at"])
                                        for c in report["checks"] if c["id"] == i)
        self.assertEqual((origin(third, "fake.a"), origin(third, "fake.b")),
                         (origin(first, "fake.a"), origin(second, "fake.b")))
        # After a complete set a new one starts, carrying every check until it runs again,
        # with every daily spec pending.
        fourth = resume("4", "--budget-seconds", "1")
        self.assertEqual(json.loads((daily / f"{'3' * 40}.json").read_text()), third)
        self.assertEqual((fourth["complete"], len(ran)), (False, 1))
        self.assertEqual(sorted(c["id"] for c in fourth["checks"]),
                         ["fake.a", "fake.b", "fake.c", "fake.d"])
        self.assertEqual(sorted(set(fourth["pending"]) | set(ran)),
                         ["fake.a", "fake.b", "fake.c", "fake.d"])
        carried = fourth["pending"][0]
        self.assertEqual(origin(fourth, carried), origin(third, carried))
        # A set whose pending ids cannot be read is never taken as done: all run again.
        (daily / f"{'4' * 40}.json").write_text(json.dumps({**fourth, "pending": "fake.a"}))
        fifth = resume("5", "--budget-seconds", "1")
        self.assertEqual((len(fifth["pending"]), fifth["started_at"] >= fourth["started_at"]),
                         (3, True))  # four specs, one ran

    def test_a_check_keeps_its_own_checked_at_when_a_later_check_of_the_run_finishes(self):
        self._three_daily()
        target = self.fake.root / "results" / "daily" / "sha.json"
        seen, real = {}, lv.run_spec

        def slow(value, **kwargs):
            if value["id"] != "fake.a":  # what the file holds before this spec, then 1.1 s
                seen[value["id"]] = {c["id"]: c["checked_at"]
                                     for c in json.loads(target.read_text())["checks"]}
                time.sleep(1.1)
            return real(value, **kwargs)
        with mock.patch.object(lv, "run_spec", slow):
            self.fake.main("--resume-daily", str(target))
        final = {c["id"]: c["checked_at"] for c in json.loads(target.read_text())["checks"]}
        self.assertEqual((seen["fake.b"]["fake.a"], seen["fake.c"]["fake.b"]),
                         (final["fake.a"], final["fake.b"]))
        self.assertLess(final["fake.a"], final["fake.b"])

    def test_a_carried_check_without_origin_stays_origin_unknown_and_marks_only_itself(self):
        self._three_daily()
        daily = self.fake.root / "results" / "daily"
        daily.mkdir(parents=True)
        legacy = daily / f"{'0' * 40}.json"  # a set from before checks carried their origin
        made = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        legacy.write_text(json.dumps({
            "schema": lv.SCHEMA, "cadence": "daily", "main_sha": "0" * 40, "generated_at": made,
            "started_at": made, "complete": False, "pending": ["fake.b", "fake.c"],
            "checks": [{"id": "fake.a", "result": "pass", "detail": {},
                        "spec": "verify/live-checks/fake.a.json"}]}))
        kept = legacy.read_text()
        for sha in ("1", "2"):  # two resumes, each at a new main SHA
            self.fake.main("--resume-daily", str(daily / f"{sha * 40}.json"),
                           "--main-sha", sha * 40, "--budget-seconds", "1")
            carried = json.loads((daily / "latest.json").read_text())["checks"][0]
            self.assertEqual((carried["id"], carried.get("origin"),
                              "main_sha" in carried, "checked_at" in carried),
                             ("fake.a", "unknown", False, False))
        self.assertIs(json.loads((daily / "latest.json").read_text())["complete"], True)
        self.assertEqual(legacy.read_text(), kept)
        # Reported, aged from its set's start, but it marks only itself: no spec requires it.
        status = lv.daily_status(daily / "latest.json", datetime.now(timezone.utc))
        self.assertEqual((status["daily_stale"], status["daily_origin_unknown"],
                          status["daily_oldest_check_at"]), (False, ["fake.a"], made))
        status = lv.daily_status(daily / "latest.json", datetime.now(timezone.utc),
                                 required={"fake.a"})
        self.assertEqual((status["daily_stale"], status["daily_required_origin_unknown"]),
                         (True, ["fake.a"]))
        # The blocked-age history never takes the report's own stamp as the check's.
        history = json.loads((daily.parent / lv.HISTORY_NAME).read_text())["checks"]
        self.assertIsNone((history.get("fake.a") or {}).get("last_pass"))

    def test_a_daily_spec_lacking_its_env_is_skipped_never_deferred_for_budget(self) -> None:
        for i in ("fake.a", "fake.b"):
            self.fake.write(spec(i, cadence="daily", timeout_seconds=60))
        self.fake.write(spec("fake.c", cadence="daily", timeout_seconds=7000,
                             requires_env=["FAKE_MISSING_VAR"]))
        results = self.fake.root / "results"
        results.mkdir()
        (results / lv.HISTORY_NAME).write_text(json.dumps({"schema": lv.HISTORY_SCHEMA, "checks": {
            "fake.a": {"last_pass": "2026-10-07T00:00:00Z", "not_passing": []},
            "fake.c": {"last_pass": "2026-10-01T00:00:00Z", "not_passing": []}}}))
        target = results / "daily" / "sha.json"
        for _ in range(5):
            self.fake.main("--resume-daily", str(target), "--budget-seconds", "1")
            report = json.loads(target.read_text())
            self.assertNotIn("fake.c", report.get("deferred_budget", {}))
            if report["complete"]:
                break
        got = {c["id"]: c for c in report["checks"]}["fake.c"]
        self.assertEqual((got["result"], got["detail"]), ("skipped",
                                                          {"missing_env": ["FAKE_MISSING_VAR"]}))
        self.assertIs(report["complete"], True)

    def test_resume_daily_runs_never_run_specs_first_and_counts_budget_deferrals(self) -> None:
        self._three_daily()
        results = self.fake.root / "results"
        results.mkdir()
        # Results inside DAILY_STALE_HOURS: one over it would run first (its own test).
        def hours_ago(hours: int) -> str:
            return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        (results / lv.HISTORY_NAME).write_text(json.dumps({"schema": lv.HISTORY_SCHEMA, "checks": {
            "fake.a": {"last_pass": hours_ago(5), "not_passing": []},
            "fake.c": {"last_pass": hours_ago(10), "not_passing": []}}}))
        target = results / "daily" / "sha.json"
        runs = []
        for _ in range(3):  # each run fits only its first spec (60 s timeouts, 1 s budget)
            self.fake.main("--resume-daily", str(target), "--budget-seconds", "1")
            report = json.loads(target.read_text())
            runs.append((report["checks"][-1]["id"],
                         {k: v["count"] for k, v in report.get("deferred_budget", {}).items()}))
            if report.get("deferred_budget"):
                self.assertRegex(next(iter(report["deferred_budget"].values()))["reason"],
                                 r"^timeout 60 s over the \d+ s budget left$")
        self.assertEqual(runs, [("fake.b", {"fake.a": 1, "fake.c": 1}),  # never run first
                                ("fake.c", {"fake.a": 2}),  # then the oldest result
                                ("fake.a", {})])
        self.assertIs(report["complete"], True)

    def test_resume_daily_fits_a_spec_on_its_measured_duration_never_past_the_budget(self):
        self.fake.write(spec("fake.m", cadence="daily", timeout_seconds=60, metric=True))
        for i in ("fake.fast", "fake.slow", "fake.cut"):
            self.fake.write(spec(i, cadence="daily", timeout_seconds=3600))
        target = self.fake.root / "results" / "daily" / "sha.json"
        self.fake.main("--resume-daily", str(target))
        done = json.loads(target.read_text())
        self.assertIs(done["complete"], True)
        # Measured: 0.2 s, 2000 s, and 0.2 s cut off by its (bounded) timeout.
        took = {"fake.fast": (0.2, False), "fake.slow": (2000, False), "fake.cut": (0.2, True)}
        for check in done["checks"]:
            if check["id"] in took:
                check["duration_s"] = took[check["id"]][0]
                if took[check["id"]][1]:
                    check.setdefault("detail", {})["timed_out"] = True
        target.write_text(json.dumps(done))
        self.fake.write(spec("fake.new", cadence="daily", timeout_seconds=3600))  # unmeasured
        ran, real = {}, lv.run_spec

        def record(value, **kwargs):
            ran[value["id"]] = value.get("timeout_seconds")
            return real(value, **kwargs)
        with mock.patch.object(lv, "run_spec", record):
            self.fake.main("--resume-daily", str(target), "--budget-seconds", "1000")
        report = json.loads(target.read_text())
        # The 0.2 s spec runs in the 1000 s left, killed at that budget, never its 3600 s.
        self.assertEqual(set(ran), {"fake.m", "fake.fast"})
        self.assertTrue(900 < ran["fake.fast"] <= 1000)
        self.assertEqual(sorted(report["pending"]), ["fake.cut", "fake.new", "fake.slow"])
        reasons = {k: v["reason"] for k, v in report["deferred_budget"].items()}
        self.assertRegex(reasons.pop("fake.slow"),
                         r"^1\.5 x last duration 3000 s over the \d+ s budget left$")
        for left in reasons.values():  # never measured, or only a lower bound: its timeout
            self.assertRegex(left, r"^timeout 3600 s over the \d+ s budget left$")
        self.assertEqual(set(reasons), {"fake.cut", "fake.new"})

    def test_resume_daily_keeps_a_spec_killed_at_its_bounded_timeout_pending(self):
        self.fake.write(spec("fake.m", cadence="daily", timeout_seconds=60, metric=True))
        self.fake.write(spec("fake.cut", cadence="daily", timeout_seconds=3600))
        target = self.fake.root / "results" / "daily" / "sha.json"
        self.fake.main("--resume-daily", str(target))  # measured: well under a second
        self.fake.write(spec("fake.cut", "sleep", cadence="daily", timeout_seconds=3600))
        ran, real = [], lv.run_spec

        def record(value, **kwargs):
            ran.append((value["id"], value.get("timeout_seconds")))
            return real(value, **kwargs)
        with mock.patch.object(lv, "run_spec", record):
            started = time.monotonic()
            self.fake.main("--resume-daily", str(target), "--budget-seconds", "3")
            self.assertLess(time.monotonic() - started, 20)  # killed, never its 30 s sleep
            report = json.loads(target.read_text())
            # Fits on its measured duration, runs killed at the budget left: no verdict.
            self.assertEqual(ran[0][0], "fake.m")
            self.assertEqual(ran[1][0], "fake.cut")
            self.assertLessEqual(ran[1][1], 3)
            self.assertEqual(report["pending"], ["fake.cut"])
            held = report["deferred_budget"]["fake.cut"]
            self.assertEqual(held["reason"],
                             f"bounded run killed at {ran[1][1]} s; needs its full timeout")
            self.assertIs(held["full_timeout"], True)
            cut = [c for c in report["checks"] if c["id"] == "fake.cut"]
            self.assertFalse(any((c.get("detail") or {}).get("timed_out") for c in cut))
            # The next command runs it on its full timeout, never bounded again.
            self.fake.write(spec("fake.cut", cadence="daily", timeout_seconds=3600))
            self.fake.main("--resume-daily", str(target), "--budget-seconds", "3")
        report = json.loads(target.read_text())
        self.assertEqual(ran[2:], [("fake.cut", 3600)])
        self.assertIs(report["complete"], True)
        self.assertNotIn("deferred_budget", report)

    def test_a_daily_budget_measures_answers_first_and_a_twice_deferred_spec_next(self):
        # Every spec takes 90 s of the command's clock; a 300 s budget fits half the set.
        self.fake.write(spec("case.m", cadence="daily", timeout_seconds=100, metric=True))
        self.fake.write(spec("eval.x", cadence="daily", timeout_seconds=100,
                             requires=["capture.feed"]))
        for i, timeout in (("capture.a", 100), ("capture.b", 100), ("capture.c", 250),
                           ("capture.feed", 100)):
            self.fake.write(spec(i, cadence="daily", timeout_seconds=timeout))
        target = self.fake.root / "results" / "daily" / "sha.json"
        spent, ran, real = [0.0], [], lv.run_spec

        def ninety(value, **kwargs):
            ran[-1].append(value["id"])
            spent[0] += 90
            return real(value, **kwargs)
        clock = mock.Mock(monotonic=lambda: time.monotonic() + spent[0])

        def command(*new: str) -> dict:
            for i in new:  # a spec landed since the last command joins the open set
                self.fake.write(spec(i, cadence="daily", timeout_seconds=100))
            ran.append([])
            with mock.patch.object(lv, "run_spec", ninety), mock.patch.object(lv, "time", clock):
                self.fake.main("--resume-daily", str(target), "--budget-seconds", "300")
            return json.loads(target.read_text())
        # The metric, then the answer with the capture it requires (its producer) first; the
        # slow captures wait, whatever their declared order.
        first = command()
        self.assertEqual(ran[0], ["case.m", "capture.feed", "eval.x"])
        self.assertEqual(first["pending"], ["capture.a", "capture.b", "capture.c"])
        # Deferred once, the cheapest captures go next; an answer landed meanwhile has no
        # result in the set, so it is pending with them and measured in it.
        second = command("eval.new")
        self.assertEqual(ran[1], ["capture.a", "capture.b", "eval.new"])
        self.assertEqual(second["pending"], ["capture.c"])
        self.assertEqual(second["deferred_budget"]["capture.c"]["count"], 2)
        # Deferred in two runs in a row: it starts the next run (the answer landed meanwhile
        # after it), which completes the set.
        third = command("eval.late")
        self.assertEqual(ran[2], ["capture.c", "eval.late"])
        self.assertIs(third["complete"], True)
        # The set lists each run: what it ran and what it deferred, with why.
        self.assertEqual([r["ran"] for r in third["runs"]], ran)
        self.assertEqual([sorted(r["deferred"]) for r in third["runs"]],
                         [["capture.a", "capture.b", "capture.c"], ["capture.c"], []])
        self.assertRegex(third["runs"][0]["deferred"]["capture.c"],
                         r"^timeout 250 s over the (29|30) s budget left$")
        self.assertEqual([r["stop_reason"] for r in third["runs"]],
                         ["budget", "budget", "finished"])
        # The next set has every spec pending again, the metric first.
        fourth = command()
        self.assertEqual(ran[3][0], "case.m")
        self.assertLessEqual({"eval.new", "eval.late"}, set(ran[3]) | set(fourth["pending"]))

    def test_a_starved_consumer_pulls_its_producer_ahead_of_higher_value_specs(self):
        def entry(i, **extra):
            return {"id": i, "spec": spec(i, cadence="daily", **extra), "error": None}
        entries = [entry("eval.answer"), entry("capture.src"),
                   entry("ontology.view", requires=["capture.src"])]
        order = lv.order_entries(entries, {}, {"ontology.view": {"count": 2, "reason": "x"}})
        self.assertEqual([e["id"] for e in order], ["capture.src", "ontology.view", "eval.answer"])
        # A malformed carried count never starves the queue: value order stands.
        for held in ({"count": "9"}, {"count": None}, "9", None):
            order = lv.order_entries(entries, {}, {"ontology.view": held})
            self.assertEqual([e["id"] for e in order],
                             ["eval.answer", "capture.src", "ontology.view"])
        # A malformed run log is dropped, never fatal; the run's entry starts a new one.
        merged = lv.merge_reports({"runs": 5, "checks": []}, {"checks": []}, [],
                                  run={"id": "r1", "ran": [], "deferred": {}})
        self.assertEqual(merged["runs"], [{"id": "r1", "ran": [], "deferred": {}}])

    def test_a_carried_head_never_runs_past_the_command_limit_after_its_upstream(self):
        self.fake.write(spec("up.h", timeout_seconds=60))  # hourly, uncarried: runs first
        self.fake.write(spec("fake.z", cadence="daily", timeout_seconds=7000, requires=["up.h"]))
        daily = self.fake.root / "results" / "daily"
        daily.mkdir(parents=True)
        made = lv._utc(datetime.now(timezone.utc))
        (daily / f"{'0' * 40}.json").write_text(json.dumps({
            "schema": lv.SCHEMA, "cadence": "daily", "main_sha": "0" * 40, "generated_at": made,
            "started_at": made, "complete": False, "pending": ["fake.z"], "checks": [],
            "deferred_budget": {"fake.z": {"count": 1, "reason": "timeout 7000 s over"}}}))
        spent, ran, real = [0.0], [], lv.run_spec

        def slow_upstream(value, **kwargs):  # up.h takes 4200 s of the command's clock
            ran.append(value["id"])
            check = real(value, **kwargs)
            spent[0] += 4200 if value["id"] == "up.h" else 0
            return check
        clock = mock.Mock(monotonic=lambda: time.monotonic() + spent[0])
        with mock.patch.object(lv, "run_spec", slow_upstream), mock.patch.object(lv, "time", clock):
            self.fake.main("--resume-daily", str(daily / f"{'1' * 40}.json"),
                           "--main-sha", "1" * 40)
        report = json.loads((daily / "latest.json").read_text())
        # 4200 s + its 7000 s timeout would pass the 7200 s limit: it waits, never started.
        self.assertEqual((ran, report.get("pending")), (["up.h"], ["fake.z"]))
        self.assertEqual(report["deferred_budget"]["fake.z"]["count"], 2)
        # The next command starts with it (its upstream's pass is stored): it runs.
        with mock.patch.object(lv, "run_spec", slow_upstream):
            self.fake.main("--resume-daily", str(daily / f"{'1' * 40}.json"),
                           "--main-sha", "1" * 40)
        self.assertEqual(ran, ["up.h", "fake.z"])
        self.assertIs(json.loads((daily / "latest.json").read_text())["complete"], True)

    def test_the_command_limit_counts_the_startup_and_the_kill_grace(self):
        self.fake.write(spec("up.h", timeout_seconds=60))  # hourly, uncarried: runs first
        self.fake.write(spec("fake.z", cadence="daily", timeout_seconds=7155, requires=["up.h"]))
        daily = self.fake.root / "results" / "daily"
        real_load, real_run = lv.load_specs, lv.run_spec

        def attempt(startup: float) -> list[str]:
            """A fresh set with fake.z carried as a deferral; the command's startup (before
            the specs are read) takes ``startup`` seconds of its clock."""
            shutil.rmtree(daily, ignore_errors=True)
            daily.mkdir(parents=True)
            made = lv._utc(datetime.now(timezone.utc))
            (daily / f"{'0' * 40}.json").write_text(json.dumps({
                "schema": lv.SCHEMA, "cadence": "daily", "main_sha": "0" * 40,
                "generated_at": made, "started_at": made, "complete": False,
                "pending": ["fake.z"], "checks": [],
                "deferred_budget": {"fake.z": {"count": 2, "reason": "timeout 7155 s over"}}}))
            spent, ran = [0.0], []

            def slow_start(*args, **kwargs):
                spent[0] += startup
                return real_load(*args, **kwargs)

            def record(value, **kwargs):
                ran.append(value["id"])
                return real_run(value, **kwargs)
            clock = mock.Mock(monotonic=lambda: time.monotonic() + spent[0])
            with mock.patch.object(lv, "load_specs", slow_start), \
                    mock.patch.object(lv, "run_spec", record), mock.patch.object(lv, "time", clock):
                self.fake.main("--resume-daily", str(daily / f"{'1' * 40}.json"),
                               "--main-sha", "1" * 40)
            return ran
        # 40 s of startup, its 7155 s timeout and the 10 s kill grace pass the 7200 s limit
        # (without the grace, or counted from the end of the startup, it would fit): it waits.
        self.assertEqual(attempt(40), ["up.h"])
        self.assertEqual(json.loads((daily / "latest.json").read_text())["pending"], ["fake.z"])
        # 30 s of startup leaves room for all three: it runs as the carried head.
        self.assertEqual(attempt(30), ["up.h", "fake.z"])

    def test_a_plain_merge_rerun_keeps_its_check_pending_until_it_ends(self):
        self.fake.write(spec("fake.d", cadence="daily", timeout_seconds=60))
        target = self.fake.root / "daily.json"
        self.fake.main("--only", "fake.d", "--merge-into", str(target), "--main-sha", "0" * 40)
        first = json.loads(target.read_text())
        self.assertIs(first["complete"], True)
        during = []

        def interrupted(value, **kwargs):
            during.append(json.loads(target.read_text()))  # the file while the check runs
            raise KeyboardInterrupt
        with mock.patch.object(lv, "run_spec", interrupted), \
                self.assertRaises(KeyboardInterrupt):
            self.fake.main("--only", "fake.d", "--merge-into", str(target),
                           "--main-sha", "1" * 40)
        after = json.loads(target.read_text())
        for doc in (*during, after):
            self.assertEqual((doc["complete"], doc.get("pending")), (False, ["fake.d"]))
        self.assertEqual((len(during), after["stop_reason"]), (1, "interrupted"))
        # The earlier result stays, with its own origin, never this run's.
        self.assertEqual([(c["main_sha"], c["checked_at"]) for c in after["checks"]],
                         [("0" * 40, first["checks"][0]["checked_at"])])
        self.fake.main("--only", "fake.d", "--merge-into", str(target), "--main-sha", "1" * 40)
        done = json.loads(target.read_text())
        self.assertEqual((done["complete"], done["checks"][0]["main_sha"]), (True, "1" * 40))

    def test_latest_reads_complete_right_after_the_sets_last_check(self):
        # The metric spec starts the command; the hourly upstream left (failed earlier) does
        # not fit the budget after it and waits, its dependent is blocked by the stored fail.
        self.fake.write(spec("fake.m", cadence="daily", timeout_seconds=60, metric=True))
        self.fake.write(spec("up.h", timeout_seconds=600))
        self.fake.write(spec("fake.d", cadence="daily", timeout_seconds=60, requires=["up.h"]))
        daily = self.fake.root / "results" / "daily"
        daily.mkdir(parents=True)
        made = lv._utc(datetime.now(timezone.utc))
        (daily / f"{'0' * 40}.json").write_text(json.dumps({
            "schema": lv.SCHEMA, "cadence": "daily", "main_sha": "0" * 40, "generated_at": made,
            "started_at": made, "complete": False, "pending": ["fake.d", "fake.m"],
            "checks": [{"id": "up.h", "result": "fail", "main_sha": "0" * 40,
                        "checked_at": made, "detail": {}}]}))
        writes, real = [], lv.write_latest

        def publish(path, text):
            writes.append(json.loads(text))
            return real(path, text)
        with mock.patch.object(lv, "write_latest", publish):
            self.fake.main("--resume-daily", str(daily / f"{'1' * 40}.json"),
                           "--main-sha", "1" * 40, "--budget-seconds", "30")
        after_last = next(w for w in writes if {"fake.d", "fake.m"} <= {
            c["id"] for c in w["checks"] if c.get("main_sha") == "1" * 40})
        self.assertEqual((after_last["stop_reason"], after_last["complete"]), ("running", True))
        self.assertNotIn("pending", after_last)
        final = json.loads((daily / "latest.json").read_text())
        self.assertEqual((final["complete"], final["stop_reason"]), (True, "budget"))
        self.assertEqual({c["id"]: c["result"] for c in final["checks"]},
                         {"fake.m": "pass", "fake.d": "blocked", "up.h": "fail"})

    def test_a_new_set_drops_the_checks_of_specs_no_longer_daily(self):
        self._three_daily()
        self.fake.write(spec("up.h"))  # hourly, required by a daily spec: carried
        self.fake.write(spec("fake.a", cadence="daily", timeout_seconds=60, requires=["up.h"]))
        daily = self.fake.root / "results" / "daily"
        first = daily / f"{'1' * 40}.json"
        self.fake.main("--resume-daily", str(first), "--main-sha", "1" * 40)
        done = json.loads(first.read_text())
        old = lv._utc(datetime.now(timezone.utc) - timedelta(days=3))
        for check in done["checks"]:
            if check["id"] == "fake.c":
                check["checked_at"] = old
        first.write_text(json.dumps(done))
        (self.fake.specs / "fake.c.json").unlink()  # deleted (or renamed, or now hourly)
        for sha, budget in (("2", ("--budget-seconds", "1")), ("3", ())):
            self.fake.main("--resume-daily", str(first), "--main-sha", sha * 40, *budget)
            report = json.loads((daily / f"{sha * 40}.json").read_text())
            self.assertEqual(sorted(c["id"] for c in report["checks"]),
                             ["fake.a", "fake.b", "up.h"])
        self.assertIs(report["complete"], True)
        status = lv.daily_status(daily / "latest.json", datetime.now(timezone.utc))
        self.assertIs(status["daily_stale"], False)

    def test_a_new_set_runs_the_metric_first_and_a_resume_a_twice_deferred_spec_first(self):
        self.fake.write(spec("fake.a", cadence="daily", timeout_seconds=60))
        self.fake.write(spec("fake.m", cadence="daily", timeout_seconds=60, metric=True))
        self.fake.write(spec("fake.z", cadence="daily", timeout_seconds=7000))
        target = self.fake.root / "results" / "daily" / "sha.json"
        firsts = []
        for _ in range(3):  # each run fits only its first spec
            self.fake.main("--resume-daily", str(target), "--budget-seconds", "1")
            report = json.loads(target.read_text())
            firsts.append(report["checks"][-1]["id"])
        # fake.z never fits what a run has left after another spec: deferred in two runs
        # in a row, it starts the next one.
        self.assertEqual(firsts, ["fake.m", "fake.a", "fake.z"])
        self.assertIs(report["complete"], True)
        with self.assertRaises(lv.SpecError):  # only true marks a metric spec
            lv.validate_spec({**spec("fake.x"), "metric": "yes"}, allowed_modules={FAKE_MODULE})

    def test_a_second_daily_attempt_on_this_host_refuses_while_the_first_runs(self) -> None:
        self._three_daily()
        origin = self.fake.root / "live.git"
        git(self.fake.root, "init", "--bare", "-q", str(origin))
        clones = [self.fake.root / name for name in ("first", "second")]
        for clone in clones:  # two checkouts of the results branch in one session
            git(self.fake.root, "clone", "-q", str(origin), str(clone))
        daily = [clone / "results" / "daily" for clone in clones]
        real, seen = lv.run_spec, []

        def second_attempt(value, **kwargs):
            if not seen:  # while the first attempt runs its first spec
                seen.append(io.StringIO())
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(seen[0]):
                    seen.append(lv.main(["--resume-daily", str(daily[1] / f"{'2' * 40}.json"),
                                         "--main-sha", "2" * 40, "--session", "me"],
                                        root=self.fake.root, environ=self.fake.env,
                                        allowed_modules={FAKE_MODULE}))
            return real(value, **kwargs)
        with mock.patch.object(lv, "run_spec", second_attempt):
            code, _ = self.fake.main("--resume-daily", str(daily[0] / f"{'1' * 40}.json"),
                                     "--main-sha", "1" * 40, "--session", "me")
        refusal = json.loads(seen[0].getvalue())
        # The scheduler waits for that attempt's pid.
        self.assertEqual((code, seen[1], refusal["error"]), (0, 76, "daily_running"))
        self.assertIn("session me", refusal["reason"])
        # The refused attempt read and wrote nothing: no set, no run marker.
        self.assertFalse(daily[1].parent.exists())
        self.assertIs(json.loads((daily[0] / "latest.json").read_text())["complete"], True)
        # Released with the first: the next attempt runs.
        code, _ = self.fake.main("--resume-daily", str(daily[1] / f"{'2' * 40}.json"),
                                 "--main-sha", "2" * 40, "--session", "me")
        self.assertEqual(code, 0)

    def test_one_run_at_a_time_through_the_running_marker(self) -> None:
        ran = self.fake.root / "ran.txt"
        self.fake.write(spec("fake.ok", "ok", str(ran)))
        results = self.fake.root / "results"
        results.mkdir()
        marker, out = results / "running.json", results / "out.json"
        now = datetime.now(timezone.utc)

        def ago(hours: float) -> str:
            return (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        other = {"session": "other", "sha": "f" * 40, "phase": "daily"}
        for name, held, code in (
                ("live", {**other, "started_at": ago(5), "updated_at": ago(1.5)}, 75),
                ("stale", {**other, "started_at": ago(5), "updated_at": ago(2.5)}, 0),
                ("legacy live", {**other, "started_at": ago(1.5)}, 75),
                ("legacy stale", {**other, "started_at": ago(2.5)}, 0),
                ("unreadable", None, 75),
                ("own session", {**other, "session": "me", "started_at": ago(1),
                                 "updated_at": ago(1)}, 0)):
            with self.subTest(name):
                marker.write_text("{" if held is None else json.dumps(held))
                ran.unlink(missing_ok=True)
                got, _ = self.fake.main("--output", str(out), "--session", "me")
                self.assertEqual((got, ran.exists()), (code, code == 0))
                if code:
                    self.assertEqual(marker.read_text(), "{" if held is None else json.dumps(held))
                elif held["session"] == "me":  # kept for the session's next command, refreshed
                    kept = json.loads(marker.read_text())
                    self.assertEqual({**kept, "updated_at": held["updated_at"]}, held)
                    self.assertGreater(kept["updated_at"], held["updated_at"])
                else:
                    self.assertFalse(marker.exists())
        marker.unlink()
        seen = []

        def heartbeat_then_terminated(value, **kwargs):
            seen.append(json.loads(marker.read_text()))
            time.sleep(1.2)
            seen.append(json.loads(marker.read_text()))
            os.kill(os.getpid(), signal.SIGTERM)  # the runtime's timeout
            time.sleep(5)
        with mock.patch.object(lv, "HEARTBEAT_SECONDS", 0.05), \
                mock.patch.object(lv, "run_spec", heartbeat_then_terminated), \
                self.assertRaises(SystemExit):
            self.fake.main("--output", str(out), "--session", "me", "--main-sha", "a" * 40)
        self.assertEqual({k: seen[0][k] for k in ("session", "sha", "phase")},
                         {"session": "me", "sha": "a" * 40, "phase": "scheduled"})
        self.assertGreater(seen[1]["updated_at"], seen[0]["updated_at"])
        self.assertFalse(marker.exists())  # removed on termination
        report = json.loads(out.read_text())
        self.assertEqual((report["stop_reason"], report["exit_code"], report["pending"]),
                         ("terminated", 128 + signal.SIGTERM, ["fake.ok"]))

    def test_runs_in_one_session_keep_each_others_marker_and_one_takes_a_free_lock(self):
        results = self.fake.root / "results"
        results.mkdir()
        marker = results / "running.json"
        sha = lambda: json.loads(marker.read_text())["sha"][0]
        with mock.patch.object(lv, "HEARTBEAT_SECONDS", 0.02):
            for a_exits_first in (True, False):
                with self.subTest(a_exits_first=a_exits_first):
                    a = lv.RunLock(marker, "me", "a" * 40, "daily")
                    b = lv.RunLock(marker, "me", "b" * 40, "daily")
                    self.assertEqual((a.acquire(), b.acquire()), (None, None))
                    time.sleep(0.2)  # A's heartbeat never writes over B's marker
                    self.assertEqual(sha(), "b")
                    if a_exits_first:
                        a.release()
                        self.assertEqual(sha(), "b")  # B still runs under its marker
                        b.release()
                    else:
                        b.release()
                        self.assertEqual(sha(), "a")  # A's marker is back while A runs
                        a.release()
                        self.assertFalse(marker.exists())
                    marker.unlink(missing_ok=True)
        # Sessions racing for a free marker: reading it and writing it is one step.
        locks = [lv.RunLock(marker, f"s{i}", "c" * 40, "scheduled") for i in range(6)]
        gate, real_utc, got = threading.Barrier(len(locks)), lv._utc, {}

        def slow_utc(moment):
            time.sleep(0.05)
            return real_utc(moment)

        def take(lock):
            gate.wait()
            got[lock.session] = lock.acquire()
        with mock.patch.object(lv, "_utc", slow_utc):
            threads = [threading.Thread(target=take, args=(lock,)) for lock in locks]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        winners = [lock for lock in locks if got[lock.session] is None]
        for lock in winners:
            lock.release()
        self.assertEqual(len(winners), 1)
        self.assertFalse(marker.exists())

    def test_resume_daily_runs_a_failed_upstream_first_and_blocks_dependents(self) -> None:
        marker = self.fake.root / "dep.txt"
        self.fake.write(spec("up.health", "fail", source="portal-workbench-api"))
        self.fake.write(spec("fake.d", "ok", str(marker), cadence="daily",
                             requires=["up.health"], source="portal-workbench-api"))
        self.fake.write(spec("fake.off", cadence="daily", disabled="paused"))
        target = self.fake.root / f"{'a' * 40}.json"
        self.fake.main("--resume-daily", str(target), "--main-sha", "a" * 40)
        report = json.loads(target.read_text())
        self.assertEqual([(c["id"], c["result"]) for c in report["checks"]],
                         [("up.health", "fail"), ("fake.d", "blocked"), ("fake.off", "skipped")])
        self.assertIs(report["complete"], True)  # every daily spec has a result
        self.assertFalse(marker.exists())
        # Every check carries its origin, a blocked or skipped one too, so the set is fresh.
        self.assertTrue(all(lv.FULL_SHA.fullmatch(c["main_sha"]) and lv._stamp(c["checked_at"])
                            for c in report["checks"]), report["checks"])
        status = lv.daily_status(target, datetime.now(timezone.utc), required={"up.health"})
        self.assertEqual((status["daily_stale"], "daily_origin_unknown" in status),
                         (False, False))

    def test_resume_daily_file_exists_before_the_first_spec_finishes(self) -> None:
        self.fake.write(spec("fake.slow", "sleep", cadence="daily", timeout_seconds=2))
        target = self.fake.root / "daily.json"
        seen = []
        real = lv.run_spec

        def peek(value, **kwargs):
            seen.append(json.loads(target.read_text()))
            return real(value, **kwargs)
        with mock.patch.object(lv, "run_spec", peek):
            self.fake.main("--resume-daily", str(target))
        self.assertEqual((seen[0]["complete"], seen[0]["pending"]), (False, ["fake.slow"]))

    def test_script_run_finds_the_repo_root_when_a_spec_writes_stderr(self) -> None:
        # A copy of the runner in a repo whose HEAD is its origin main (reviewed main).
        specs = self.fake.root / "specs"
        specs.mkdir()
        (specs / "bad.probe.json").write_text(json.dumps(
            spec("bad.probe", "no-such-probe", expect={"exit_code": 0, "json_fields": ["x"]}) |
            {"command": ["python3", "-m", "scripts.live_read_probes", "no-such-probe"]}))
        script = self.fake.root / "verify" / "live_verify.py"
        for name in ("live_verify.py", "data_profile.py"):  # its lazy repo import
            shutil.copy(Path(lv.__file__).with_name(name), script.with_name(name))
        shutil.copy(Path(probes.__file__), self.fake.root / "scripts" / "live_read_probes.py")
        for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                     ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit",
                      "-qm", "main"), ("remote", "add", "origin", ".")):
            subprocess.run(["git", "-C", str(self.fake.root), *args], check=True,
                           capture_output=True)
        env = {"PATH": os.environ.get("PATH", "")}
        done = subprocess.run([sys.executable, str(script), "--specs-dir", str(specs)],
                              cwd=str(self.fake.root), env=env, capture_output=True,
                              text=True, timeout=120)
        self.assertNotIn("ModuleNotFoundError", done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual(report["checks"][0]["result"], "fail")
        self.assertIn("stderr", report["checks"][0]["detail"])

    def test_hourly_report_flags_a_missing_or_stale_daily_run(self) -> None:
        self.fake.write(spec("fake.hourly"))
        daily = self.fake.root / "results" / "daily" / "latest.json"
        code, text = self.fake.main("--daily-report", str(daily))
        report = json.loads(text)
        self.assertEqual((report["daily_last_run_at"], report["daily_age_hours"],
                          report["daily_stale"]), (None, None, True))
        now = datetime.now(timezone.utc)
        at = lambda hours: (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        daily.parent.mkdir(parents=True, exist_ok=True)
        # Freshness is the set's oldest check, not the moment the set completed.
        for ages, stale in (((3, 1), False), ((27, 1), True), ((72, 0), True)):
            daily.write_text(json.dumps({
                "schema": lv.SCHEMA, "cadence": "daily", "complete": True,
                "generated_at": at(0), "checks": [
                    {"id": f"fake.{i}", "result": "pass", "main_sha": "a" * 40,
                     "checked_at": at(hours)} for i, hours in enumerate(ages)]}))
            report = json.loads(self.fake.main("--daily-report", str(daily))[1])
            self.assertEqual(report["daily_last_run_at"], at(max(ages)))
            self.assertAlmostEqual(report["daily_age_hours"], max(ages), delta=0.1)
            self.assertIs(report["daily_stale"], stale)
        # The set's oldest check and the file's completion are two named fields; a check
        # without an origin marks only itself unless another spec requires it.
        self.fake.write(spec("fake.up"))
        self.fake.write(spec("fake.down", requires=["fake.up"]))
        for missing, stale in (("fake.other", False), ("fake.up", True)):
            daily.write_text(json.dumps({
                "schema": lv.SCHEMA, "cadence": "daily", "complete": True,
                "started_at": at(25), "generated_at": at(2), "checks": [
                    {"id": "fake.0", "result": "pass", "main_sha": "a" * 40,
                     "checked_at": at(20)}, {"id": missing, "result": "skipped"}]}))
            report = json.loads(self.fake.main("--daily-report", str(daily))[1])
            self.assertEqual((report["daily_oldest_check_at"], report["daily_last_run_at"],
                              report["daily_completed_at"], report["daily_origin_unknown"],
                              report["daily_stale"]), (at(25), at(25), at(2), [missing], stale))
        # An hourly report in the daily slot does not count as a daily run.
        daily.write_text(json.dumps({"schema": lv.SCHEMA, "cadence": "hourly",
                                     "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}))
        self.assertTrue(json.loads(self.fake.main("--daily-report", str(daily))[1])
                        ["daily_stale"])
        self.assertNotIn("daily_stale", json.loads(self.fake.main()[1]))

    def test_cadence_is_validated_and_sets_the_timeout_ceiling(self) -> None:
        self.assertEqual(lv.validate_spec(spec(cadence="daily", timeout_seconds=7200),
                                          allowed_modules={FAKE_MODULE}), [])
        for bad, message in ((spec(timeout_seconds=7200), "for a hourly spec"),
                             (spec(cadence="daily", timeout_seconds=7201), "for a daily spec"),
                             (spec(cadence="weekly", timeout_seconds=7201), "for a weekly spec"),
                             (spec(cadence="monthly"), "cadence must be one of")):
            with self.subTest(bad=bad.get("cadence")), \
                    self.assertRaisesRegex(lv.SpecError, message):
                lv.validate_spec(bad, allowed_modules={FAKE_MODULE})

    def test_a_broken_spec_is_reported_in_the_hourly_run_whatever_it_declares(self) -> None:
        (self.fake.specs / "fake.broken.json").write_text(
            json.dumps({**spec("fake.broken"), "cadence": "daily", "command": ["bash"]}))
        code, text = self.fake.main()
        report = json.loads(text)
        self.assertEqual(code, 1)
        self.assertEqual([(c["id"], c["result"]) for c in report["checks"]],
                         [("fake.broken", "error")])


def schema_csdl(extra: str = "") -> bytes:
    """The BC $metadata fixture as a schema-only CSDL document, ``extra`` in its Schema."""
    raw = (lv.ROOT / "integrations" / "tests" / "fixtures" / "bc_api_v2_metadata.xml"
           ).read_text(encoding="utf-8")
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)  # the fixture's provenance comment
    head, tail = raw.rsplit("</Schema>", 1)
    return (head + extra + "</Schema>" + tail).encode("utf-8")


class SchemaArtifactTests(unittest.TestCase):
    """A spec's declared schema-only {tmp} file, published to results/artifacts per digest."""

    ARTIFACT = [{"path": "{tmp}/metadata.xml", "kind": "bc_metadata_csdl"}]

    def setUp(self) -> None:
        self.fake = FakeRoot()
        self.addCleanup(self.fake.close)
        self.source = self.fake.root / "source.xml"
        self.fake.env = {**self.fake.env, "FAKE_ARTIFACT": str(self.source)}
        self.results = self.fake.root / "results"
        self.results.mkdir()

    def test_a_declared_schema_artifact_is_published_once_per_digest(self) -> None:
        self.fake.write(spec("fake.art", "artifact", "{tmp}/metadata.xml",
                             artifacts=self.ARTIFACT))
        first = schema_csdl()
        second = schema_csdl('<EnumType Name="newEnum"><Member Name="a" Value="0"/></EnumType>')
        rows = []
        for n, (content, exit_code) in enumerate(((first, "0"), (first, "0"), (second, "1"))):
            self.source.write_bytes(content)
            self.fake.env["FAKE_EXIT"] = exit_code  # a failing run publishes too
            self.fake.main("--only", "fake.art", "--output", str(self.results / f"r{n}.json"),
                           "--main-sha", str(n) * 40)
            check = json.loads((self.results / f"r{n}.json").read_text())["checks"][0]
            self.assertEqual(check["result"], "pass" if exit_code == "0" else "fail")
            rows.append(check["artifacts"])
        digests = [hashlib.sha256(c).hexdigest() for c in (first, second)]
        self.assertEqual([(r[0]["status"], r[0]["sha256"]) for r in rows],
                         [("published", digests[0]), ("unchanged", digests[0]),
                          ("published", digests[1])])
        folder = self.results / "artifacts" / "fake.art"
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         sorted([f"{d}.xml" for d in digests] + ["index.json"]))
        for digest, content in zip(digests, (first, second)):
            self.assertEqual((folder / f"{digest}.xml").read_bytes(), content)  # byte for byte
        index = json.loads((folder / "index.json").read_text())
        self.assertEqual([(c["sha256"], c["main_sha"], c["file"]) for c in index["captures"]],
                         [(digests[0], "0" * 40, f"artifacts/fake.art/{digests[0]}.xml"),
                          (digests[1], "2" * 40, f"artifacts/fake.art/{digests[1]}.xml")])
        for capture in index["captures"]:
            self.assertRegex(capture["captured_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        # The undeclared file the command wrote beside it is never published.
        self.assertEqual([p for p in self.results.rglob("*") if p.name == "other.xml"], [])
        self.assertNotIn(hashlib.sha256(b"ran").hexdigest(),
                         [p.stem for p in folder.iterdir()])

    def test_a_file_with_record_values_or_outside_tmp_is_refused_and_not_published(self):
        record = "PO-0042 Acme Cosmetics Inc"
        value = spec("fake.art", "artifact", "{tmp}/metadata.xml", artifacts=self.ARTIFACT)
        cases = {
            "foreign_element": (schema_csdl(  # an entity instance in the data namespaces
                '<m:properties xmlns:m="http://docs.oasis-open.org/odata/ns/metadata" '
                'xmlns:d="http://docs.oasis-open.org/odata/ns/data">'
                f'<d:number>{record}</d:number></m:properties>'), {}),
            "text_content": (schema_csdl(
                f'<EntityType Name="x"><Property Name="n" Type="Edm.String">{record}'
                '</Property></EntityType>'), {}),
            "not_xml": (json.dumps({"value": [{"number": record}]}).encode(), {}),
            "secret_value": (schema_csdl(f'<Term Name="t" Type="Edm.String" '
                                         f'DefaultValue="{SECRET}"/>'), {}),
            "not_regular_file": (schema_csdl(), {"FAKE_LINK": "1"}),  # a symlink out of {tmp}
        }
        for reason, (content, env) in cases.items():
            with self.subTest(reason):
                self.source.write_bytes(content)
                check = self.fake.run(value, environ={**self.fake.env, **env},
                                      artifacts_dir=self.results / "artifacts",
                                      main_sha="a" * 40)
                self.assertEqual([(r["status"], r.get("reason")) for r in check["artifacts"]],
                                 [("refused", reason)])
                self.assertFalse((self.results / "artifacts").exists())
                self.assertNotIn("PO-0042", json.dumps(check))
                self.assertNotIn(SECRET, json.dumps(check))

    def test_artifact_declarations_fail_closed(self) -> None:
        good = spec("fake.art", "artifact", "{tmp}/metadata.xml", artifacts=self.ARTIFACT)
        self.assertEqual(lv.validate_spec(good, allowed_modules={FAKE_MODULE}), [])
        for bad in ([{"path": "{tmp}/metadata.xml", "kind": "records_page"}],
                    [{"path": "{tmp}/metadata.xml", "kind": ["bc_metadata_csdl"]}],
                    [{"path": "/etc/passwd.xml", "kind": "bc_metadata_csdl"}],
                    [{"path": "{tmp}/../metadata.xml", "kind": "bc_metadata_csdl"}],
                    [{"path": "{tmp}/metadata.json", "kind": "bc_metadata_csdl"}],
                    [{"path": "{tmp}/other.xml", "kind": "bc_metadata_csdl"}],  # not an arg
                    [{"path": "{tmp}/metadata.xml", "kind": "bc_metadata_csdl", "x": 1}],
                    []):
            with self.subTest(bad):
                with self.assertRaises(lv.SpecError):
                    lv.validate_spec({**good, "artifacts": bad}, allowed_modules={FAKE_MODULE})


class SessionCaptureTests(unittest.TestCase):
    """The decided session sharing on the real specs, and what a shared store saves
    Injected transports and temp dirs only; nothing live."""

    def test_real_specs_share_a_session_capture_only_under_one_capture_audience(self) -> None:
        specs = lv.valid_specs(lv.load_specs(lv.ROOT / lv.SPECS_DIR_NAME))

        def users(path: str) -> dict[str, str]:
            return {i: lv.session_key(s, path) for i, s in specs.items()
                    if path in lv.session_paths(s)}
        # The BC service audience (integrations.bc_read.capture_probe's own): one store.
        bc = users("/bc-capture.sqlite")
        self.assertEqual(set(bc), {"bc.capture_full", "eval.order_margin",
                                   "ontology.projection_bc"})
        self.assertEqual(len(set(bc.values())), 1)
        mirror = users("/mirror.sqlite")
        self.assertEqual(set(mirror), {"mirror.full_coverage", "ontology.projection",
                                       "ontology.traverse", "workspace.object_view",
                                       "ontology.mentions", "ontology.text_find",
                                       "eval.dev_baseline", "eval.heldout_product",
                                       "ontology.session_projection", "session.replay",
                                       "process.dependencies", "ontology.identity_scoring",
                                       "process.po_lateness", "process.impact_chain"})
        self.assertEqual(len(set(mirror.values())), 1)
        # The session projection: one store, its producer before
        # every consumer, which it never blocks.
        consumers = {"ontology.projection", "ontology.traverse", "workspace.object_view",
                     "ontology.mentions", "ontology.text_find", "eval.dev_baseline",
                     "eval.heldout_product", "process.dependencies", "process.po_lateness",
                     "process.impact_chain",
                     "session.replay", "eval.unfamiliar_sample", "ontology.identity_scoring",
                     "ontology.money_units"}
        projection = users("/projection")
        self.assertEqual(set(projection), consumers | {"ontology.session_projection"})
        self.assertEqual(len(set(projection.values())), 1)
        entries = [{"id": i, "spec": s, "error": None} for i, s in specs.items()]
        hourly = [e["id"] for e in lv.order_entries(entries)]
        daily = [e["id"] for e in lv.order_entries(
            [e for e in entries if lv.spec_cadence(e["spec"]) == "daily"
             or e["id"] == "portal.health_catalog"], {}, {})]
        for order in (hourly, daily):
            self.assertLess(order.index("mirror.full_coverage"),
                            order.index("ontology.session_projection"))
            for consumer in consumers:
                self.assertLess(order.index("ontology.session_projection"),
                                order.index(consumer))
        # A new daily set measures answers first: the metric and the other answer specs,
        # after only the producers they require, before every other spec.
        answers = {i for i in daily if specs[i].get("metric") or specs[i].get("answer_source")
                   or lv.spec_tier(specs[i]) in lv.ANSWER_TIERS}
        self.assertTrue({"eval.unfamiliar_sample", "case.unfamiliar_order", "eval.heldout",
                         "eval.dev_baseline"} <= answers)
        head = daily[:max(daily.index(i) for i in answers) + 1]
        self.assertEqual(set(head) - answers, {"portal.health_catalog", "mirror.full_coverage",
                                               "ontology.session_projection", "bc.capture_full"})
        captures = [i for i in daily if lv.spec_tier(specs[i]) == "capture"
                    and i not in head]
        self.assertLess(daily.index("schema.portal_queryable"), min(map(daily.index, captures)))
        for consumer in consumers:
            self.assertIn("ontology.session_projection",
                          lv.session_producers(specs[consumer], specs)["/projection"])
            self.assertNotIn("ontology.session_projection",
                             lv.blocking_requires(specs[consumer], specs))
        # The session producer captures the mail consumers' mail once in their store.
        self.assertEqual(set(users("/ontology.sqlite")),
                         {"ontology.session_projection", "workspace.object_view",
                          "ontology.mentions", "ontology.text_find", "session.replay"})
        self.assertEqual(len(set(users("/ontology.sqlite").values())), 1)
        # traverse captures BC as the served host's principal under its own audience: never
        # a session BC store, so it captures cold.
        self.assertIn("--bc-capture", specs["ontology.traverse"]["command"])
        self.assertEqual(lv.session_producers(specs["ontology.projection_bc"], specs),
                         {"/bc-capture.sqlite": ["bc.capture_full"]})
        self.assertEqual(lv.session_producers(specs["eval.dev_baseline"], specs),
                         {"/mirror.sqlite": ["mirror.full_coverage",
                                             "ontology.session_projection"],
                          "/projection": ["ontology.session_projection"]})
        self.assertEqual(lv.blocking_requires(specs["eval.dev_baseline"], specs), [])
        # The held-out product score shares the same session stores; only the frozen set's
        # producer (eval.heldout) blocks it.
        self.assertEqual(lv.blocking_requires(specs["eval.heldout_product"], specs),
                         ["eval.heldout"])
        # Another reader of one path is another store: a narrower live audience never
        # tops up (or reads) the service capture.
        other = copy.deepcopy(specs["ontology.projection_bc"])
        other["command"].append("examples/projection_probe_audience.live-check.json")
        other["session_audience"]["/bc-capture.sqlite"]["reader"] = [other["command"][-1]]
        lv.validate_spec(other)
        self.assertNotEqual(lv.session_key(other, "/bc-capture.sqlite"), bc["bc.capture_full"])
        session = lv.SessionState(lv.ROOT)
        filled = {"id": "bc.capture_full", "result": "fail", "exit_code": 0,
                  "detail": {"fields": {"units_total": 1}}}
        session.record(specs["bc.capture_full"], {"/bc-capture.sqlite": "produced"}, filled)
        self.assertEqual(session.plan(specs["ontology.projection_bc"], specs),
                         {"/bc-capture.sqlite": "shared"})
        self.assertEqual(session.plan({**other, "requires": ["bc.capture_full"]}, specs),
                         {"/bc-capture.sqlite": "cold_audience"})

    def test_a_mirror_top_up_reads_less_than_a_cold_capture(self) -> None:
        from integrations.tests import test_source_mirror as tm
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts.mirror_measure import ordered_full_coverage

        class Counting(tm.FakeWorkbench):
            rows = 0

            def _rows(self, name, query):
                page = super()._rows(name, query)
                self.rows += len(page["rows"])
                return page
        with tempfile.TemporaryDirectory() as tmp:
            fake = Counting()
            client = WorkbenchReadApiClient(tm.BASE, tm.KEY, fetch=fake)
            db = os.path.join(tmp, "mirror.sqlite")
            cold = ordered_full_coverage(client, db, resolve_sample=0)
            read_cold = fake.rows
            topped = ordered_full_coverage(client, db, resolve_sample=0)  # the consumer's
            read_top_up = fake.rows - read_cold
        self.assertGreater(read_cold, 0)
        self.assertLess(read_top_up, read_cold)
        self.assertEqual(topped["rows_current_total"], cold["rows_current_total"])
        self.assertEqual(topped["completeness"]["complete_bound_relation_count"],
                         cold["completeness"]["complete_bound_relation_count"])

    def test_dev_baseline_tops_up_the_session_mirror_and_copies_it_once_per_run(self) -> None:
        from ontology.project import MirrorCaptures
        from scripts import eval_dev_baseline as edb
        from scripts.mirror_measure import ordered_full_coverage

        def held(mirror: Path) -> set[str]:
            with MirrorCaptures(mirror) as captures:
                return set(captures.describe()["relations"])
        with every_sqlite_connection_closed(self), tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            served = sorted(edb.fixture_client().relations())
            mine = served[: len(served) // 2]  # the run's own; the session store holds all

            def build(name: str, **shared):
                return edb.build_product(edb.fixture_client(), work / name, mine,
                                         budget_seconds=None, clock=edb.fixture_clock,
                                         **shared)
            cold = build("cold")
            session = work / "session" / "mirror.sqlite"
            ordered_full_coverage(edb.fixture_client(), session, resolve_sample=0)  # producer
            shared = build("shared", mirror_db=session)
            # The run read the session's store (it holds every relation, a cold run only its
            # own) and projects only its own relations: the product is the cold one's.
            self.assertEqual(held(cold.mirror_path), set(mine))
            self.assertEqual(held(shared.mirror_path), set(served))
            self.assertEqual((shared.info["objects"], shared.info["relations_complete"]),
                             (cold.info["objects"], len(mine)))
            # One copy per run, taken after its sync: its items link it, never copy it,
            # and never open the session's own file.
            self.assertFalse(os.path.samefile(shared.mirror_path, session))
            hosts = [shared.host(["eval:dev-baseline"]) for _ in range(2)]
            try:
                for host in hosts:
                    linked = Path(host.case_host.state_dir) / shared.mirror_path.relative_to(
                        shared.state_dir)
                    self.assertTrue(linked.is_symlink())
                    self.assertTrue(os.path.samefile(linked, shared.mirror_path))
            finally:
                for host in hosts:
                    host.close()


class SessionProjectionTests(unittest.TestCase):
    """``ontology.session_projection`` and its consumers' own argv in one verifier session
    (session stores planned, expanded and recorded as ``main`` does) over the fixture world:
    Portal and BC transports injected, nothing live."""

    def test_one_build_per_session_and_cold_without_a_producer(self) -> None:
        from verify.tests import test_live_wiring as wiring
        from integrations import workbench_read_api
        from ontology import project
        from scripts import ontology_projection_probe as projection
        from scripts import ontology_traverse_probe as traverse
        from scripts import process_dependencies_probe as dependencies
        from scripts.mirror_measure import ordered_full_coverage

        specs = lv.valid_specs(lv.load_specs(lv.ROOT / lv.SPECS_DIR_NAME))
        fake, books = wiring.fixture_bc()
        builds: list[object] = []
        real = project.Projector.project_into

        def counted(projector, builder):
            builds.append(projector)
            return real(projector, builder)

        def run(session, spec_id, main, tmp):
            spec_doc = specs[spec_id]
            plan = session.plan(spec_doc, specs)
            argv = lv._expand(spec_doc["command"][3:], f"{tmp}/{spec_id}", None,
                              session.bases(spec_doc, plan))
            code, document = wiring.run_main(
                main, argv, environ={**wiring.PORTAL_ENV, **wiring.BC_ENV,
                                     lv.CAPTURE_FRESH_SINCE_ENV: session.started_at},
                bc_sender=fake, bc_books=books)
            check = {"id": spec_id, "result": "pass" if code == 0 else "fail",
                     "exit_code": code, "detail": {"fields": document}}
            session.record(spec_doc, plan, check)
            return code, document, session.detail(spec_doc, plan, specs)

        def holds(spec_id, code, document, may_miss):
            failures = lv.check_expectations(specs[spec_id], code, document)
            self.assertLessEqual(set(failures), may_miss, (spec_id, failures))

        labels = {"bc_capture.evidence_label != expected"}
        cwd = os.getcwd()
        with every_sqlite_connection_closed(self), tempfile.TemporaryDirectory() as tmp, \
                wiring.fixture_bound(), wiring.fixture_portal(), \
                mock.patch.object(project.Projector, "project_into", counted):
            os.chdir(lv.ROOT)   # the specs name their audience files relative to the repo
            session, idle = lv.SessionState(lv.ROOT), lv.SessionState(lv.ROOT)
            try:
                mirror = specs["mirror.full_coverage"]
                plan = session.plan(mirror, specs)
                ordered_full_coverage(workbench_read_api.WorkbenchReadApiClient(
                    wiring.FIXTURE_URL, wiring.PORTAL_ENV["PLX_WORKBENCH_API_KEY"]),
                    session.bases(mirror, plan)["/mirror.sqlite"] + "/mirror.sqlite",
                    resolve_sample=0)
                session.record(mirror, plan, {"id": "mirror.full_coverage", "result": "pass",
                                              "exit_code": 0,
                                              "detail": {"fields": {"relation_count": 1}}})
                code, produced, _ = run(session, "ontology.session_projection",
                                        projection.main, tmp)
                holds("ontology.session_projection", code, produced,
                      {"objects < 1000", "values < 10000"} | labels)
                self.assertEqual(produced["sources"], ["bc", "portal_api"])
                self.assertEqual(len(builds), 1)
                consumers = {}
                for spec_id, main in (("process.dependencies", dependencies.main),
                                      ("ontology.traverse", traverse.main)):
                    consumers[spec_id] = run(session, spec_id, main, tmp)
                # Neither consumer projected: counted builds = 1 in the session.
                self.assertEqual(len(builds), 1)
                for spec_id, (code, document, detail) in consumers.items():
                    self.assertEqual(document["session_projection"],
                                     {"mode": "reused", "units_rebuilt": 0}, spec_id)
                    self.assertEqual(detail["paths"]["/projection"], "shared")
                    self.assertEqual(detail["producer_verdict"]["/projection"], "pass")
                    self.assertEqual(document["sources"], ["bc", "portal_api"])
                holds("process.dependencies", *consumers["process.dependencies"][:2],
                      {"roots_open < 5"} | labels)
                traversed = consumers["ontology.traverse"][1]
                holds("ontology.traverse", 0, traversed,
                      {f"{path} < {floor}" for path, floor in
                       specs["ontology.traverse"]["expect"]["json_min"].items()} | labels)
                self.assertGreater(traversed["citations"]["by_scheme"]["bc"], 0)
                # A session whose producer found no session mirror commits nothing: its
                # consumer projects cold (and still never is blocked by it).
                code, failed, _ = run(idle, "ontology.session_projection",
                                      projection.main, tmp + "/idle")
                self.assertEqual((code, failed["error"]), (1, "session_mirror_missing"))
                code, cold, detail = run(idle, "process.dependencies", dependencies.main,
                                         tmp + "/idle")
                self.assertEqual((code, cold["session_projection"]), (0, {"mode": "cold"}))
                self.assertEqual(detail["paths"]["/projection"], "cold_failed")
                self.assertEqual(len(builds), 2)
                self.assertEqual(lv.blocking_requires(specs["process.dependencies"], specs),
                                 [])
            finally:
                os.chdir(cwd)
                session.close()
                idle.close()


def git_repo(root: Path) -> dict[str, str]:
    """A repo whose origin (a bare repo) has main m1 -> m2 -> m3, plus ``off``, a commit
    beside main. Its tracking ref origin/main is left stale at m1: a reader that does not
    fetch first sees the old main."""
    def git(*args: str, env: dict | None = None) -> str:
        return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                              capture_output=True, env=env).stdout.strip()
    git("init", "-q", "-b", "main")
    shas = {}
    for name in ("m1", "m2", "m3"):
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
            "--allow-empty", "-m", name)
        shas[name] = git("rev-parse", "HEAD")
    origin = root / ".git" / "fixture-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")
    git("update-ref", "refs/remotes/origin/main", shas["m1"])
    tree = git("rev-parse", "HEAD^{tree}")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    shas["off"] = git("commit-tree", tree, "-p", shas["m1"], "-m", "off main", env=env)
    return shas


def hourly(sha: str, at: str, checks: list | None = None, **over) -> dict:
    return {"schema": lv.SCHEMA, "run": f"live-{at}", "main_sha": sha, "generated_at": at,
            "cadence": "hourly", "complete": True, "stop_reason": "finished", "exit_code": 0,
            "checks": checks or [], **over}


class LatestAdvanceTests(unittest.TestCase):
    """results/latest.json advances to the newest complete hourly report on main only."""

    def setUp(self) -> None:
        self.fake = FakeRoot()
        self.addCleanup(self.fake.close)
        self.sha = git_repo(self.fake.root)
        self.results = self.fake.root / "results"
        self.results.mkdir()
        self.latest = self.results / "latest.json"

    def put(self, name: str, report: dict) -> None:
        (self.results / f"{name}.json").write_text(json.dumps(report, indent=1) + "\n")

    def held(self) -> dict:
        return json.loads(self.latest.read_text())

    def test_only_a_complete_report_on_main_advances_and_never_backwards(self) -> None:
        frozen = hourly(self.sha["m1"], "2026-10-07T07:51:00Z")
        self.latest.write_text(json.dumps(frozen))
        # Newer, but partial (killed, or never finished) or off main: none of them advances.
        self.put(self.sha["m3"], hourly(self.sha["m3"], "2026-10-07T12:51:00Z",
                                        complete=False, pending=["x"],
                                        stop_reason="terminated", exit_code=143))
        self.put("running", hourly(self.sha["m3"], "2026-10-07T12:55:00Z",
                                   stop_reason="running", exit_code=None))
        self.put(self.sha["off"], hourly(self.sha["off"], "2026-10-07T13:51:00Z"))
        self.put("one-spec", hourly(self.sha["m3"], "2026-10-07T13:00:00Z", cadence="only"))
        self.put("unknown", hourly("f" * 40, "2026-10-07T13:30:00Z"))  # not a commit here
        self.assertIsNone(lv.advance_latest(self.results, self.fake.root))
        self.assertEqual(self.held(), frozen)
        # A complete report on an ancestor of main (main moved during the run) advances.
        self.put(self.sha["m2"], hourly(self.sha["m2"], "2026-10-07T11:51:00Z"))
        self.assertEqual(lv.advance_latest(self.results, self.fake.root).name,
                         f"{self.sha['m2']}.json")
        self.assertEqual(self.held(), json.loads(
            (self.results / f"{self.sha['m2']}.json").read_text()))
        self.assertEqual((self.held()["main_sha"], self.held()["generated_at"]),
                         (self.sha["m2"], "2026-10-07T11:51:00Z"))
        # An older complete report on main never replaces the newer one latest.json holds.
        self.put(self.sha["m1"], hourly(self.sha["m1"], "2026-10-07T09:51:00Z"))
        self.assertIsNone(lv.advance_latest(self.results, self.fake.root))
        self.assertEqual(self.held()["main_sha"], self.sha["m2"])
        # A ref that does not resolve fails closed.
        self.put(self.sha["m3"], hourly(self.sha["m3"], "2026-10-07T14:51:00Z"))
        self.assertIsNone(lv.advance_latest(self.results, self.fake.root, "origin/gone"))
        self.assertEqual(self.held()["main_sha"], self.sha["m2"])

    def test_remote_refs_are_fetched_before_they_are_read(self) -> None:
        """A stale origin/main (m1)
        is fetched before live_verify reads it."""
        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(self.fake.root), *args], check=True,
                                  text=True, capture_output=True).stdout.strip()
        self.assertEqual(git("rev-parse", "origin/main"), self.sha["m1"])
        self.put(self.sha["m3"], hourly(self.sha["m3"], "2026-10-07T12:51:00Z"))
        self.assertEqual(lv.advance_latest(self.results, self.fake.root).name,
                         f"{self.sha['m3']}.json")
        self.assertEqual(git("rev-parse", "origin/main"), self.sha["m3"])
        # --resume-daily runs only under the scheduler's pushed claim of the set: origin's
        # results/running.json naming this session, never a stale local copy of it.
        self.fake.write(spec("fake.day", cadence="daily"))
        daily = self.results / "daily" / f"{self.sha['m3']}.json"

        def live_commit(marker: object) -> str:
            def out(*args: str, data: str) -> str:
                return subprocess.run(["git", "-C", str(self.fake.root), *args], input=data,
                                      check=True, text=True, capture_output=True).stdout.strip()
            blob = out("hash-object", "-w", "--stdin", data=json.dumps(marker))
            inner = out("mktree", data=f"100644 blob {blob}\trunning.json\n")
            tree = out("mktree", data=f"040000 tree {inner}\tresults\n")
            return git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                       "commit-tree", tree, "-m", "claim")

        def resume() -> tuple[int, str]:
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = lv.main(["--resume-daily", str(daily), "--main-sha", self.sha["m3"],
                                "--session", "me"], root=self.fake.root,
                               environ=self.fake.env, allowed_modules={FAKE_MODULE},
                               reviewed_main_only=True)
            return code, err.getvalue()
        live = "refs/heads/verify-live"
        git("update-ref", "refs/remotes/origin/verify-live",
            live_commit({"session": "me"}))  # local only: origin has no claim
        for marker, refusal in ((None, (lv.UNCLAIMED_EXIT, "marker_unpushed")),
                                ({"session": "other", "phase": "daily"},
                                 (lv.LOCKED_EXIT, "run_locked")),
                                (["me"], (lv.UNCLAIMED_EXIT, "marker_unpushed"))):
            if marker is not None:
                git("push", "-q", "-f", "origin", f"{live_commit(marker)}:{live}")
            code, err = resume()
            self.assertEqual((code, json.loads(err)["error"]), refusal)
            self.assertFalse(daily.exists())  # nothing read, run or written
        git("push", "-q", "-f", "origin", f"{live_commit({'session': 'me'})}:{live}")
        self.assertEqual(resume()[0], 0)
        self.assertIs(json.loads(daily.read_text())["complete"], True)

    def test_a_finished_run_advances_latest_and_an_off_main_run_does_not(self) -> None:
        self.fake.write(spec("fake.ok"))
        for name, advanced in (("m3", True), ("off", False)):
            with self.subTest(name):
                out = self.results / f"{self.sha[name]}.json"
                self.fake.main("--output", str(out), "--main-sha", self.sha[name])
                run = json.loads(out.read_text())
                self.assertEqual((run["complete"], run["stop_reason"]), (True, "finished"))
                self.assertEqual(self.held()["main_sha"], self.sha["m3"])
                self.assertEqual(self.held() == run, advanced)

    def test_a_failed_latest_write_leaves_the_finished_report_finished(self) -> None:
        self.fake.write(spec("fake.ok"))
        self.fake.write(spec("fake.day", cadence="daily"))
        daily = self.results / "daily"
        for name, latest, argv in (
                ("hourly", self.latest, ("--output", str(self.results / "run.json"))),
                ("daily", daily / "latest.json",
                 ("--resume-daily", str(daily / f"{self.sha['m3']}.json")))):
            with self.subTest(name):
                latest.mkdir(parents=True)  # latest.json cannot be replaced: an OSError
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    code = lv.main([*argv, "--main-sha", self.sha["m3"]], root=self.fake.root,
                                   environ=self.fake.env, allowed_modules={FAKE_MODULE})
                run = json.loads(Path(argv[1]).read_text())
                self.assertEqual((code, run["complete"], run["stop_reason"], run["exit_code"]),
                                 (0, True, "finished", 0))
                self.assertEqual(json.loads(err.getvalue().splitlines()[-1]),
                                 {"warning": "latest_write_failed", "path": "latest.json",
                                  "error": "IsADirectoryError"})
                self.assertEqual(list(latest.parent.glob("*.tmp")), [])


class ProbeCausesHeadlineTests(unittest.TestCase):
    """The probe's own ``causes`` lead a failed check, beside its blocked parts."""

    def test_refused_key_is_named_in_the_fail_headline_and_in_blocked(self) -> None:
        fake = FakeRoot()
        self.addCleanup(fake.close)
        real = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mc.brain_probe.json").read_text())
        probe = {k: v for k, v in real.items() if k not in ("requires_env", "optional_env")}
        probe["command"] = ["python3", "-m", FAKE_MODULE, "echo"]
        step = {"call": "brain agent/status", "ok": False, "error": "auth_refused",
                "http_status": 401}
        refused = {"ok": False, "requests_sent": 4, "knowledge": {"served_by": "mc"},
                   "steps": [{"call": "mc_self_check", "ok": True}, step],
                   "blocked_upstream": [],
                   "causes": [{"part": "brain:agent/status", "cause": "credential_refused"}]}
        outage = {**refused, "knowledge": {"served_by": None, "fallback": "failed"},
                  "steps": [{"call": "mc_search_knowledge", "ok": False,
                             "error": "upstream_error", "http_status": 200},
                            {**step, "call": "brain agent/search", "http_status": 403}],
                  "blocked_upstream": [{"part": "mc_search_knowledge",
                                        "source": "mission-control-mcp", "http_status": 200,
                                        "error_code": "upstream_error",
                                        "cause": "upstream_outage"}],
                  "causes": [{"part": "mc_search_knowledge", "cause": "upstream_outage"},
                             {"part": "brain:agent/search", "cause": "credential_refused"},
                             {"part": "x", "cause": "Bearer abc def"}]}  # not enum-safe
        for name, doc, headline, blocked in (
                ("refused only", refused, "causes credential_refused", None),
                ("refused beside an outage", outage, "causes upstream_outage, credential_refused",
                 ["upstream_outage", "credential_refused"])):
            with self.subTest(name):
                check = lv.run_spec(probe, root=fake.root,
                                    environ={**fake.env, "FAKE_DOC": json.dumps(doc)},
                                    allowed_modules={FAKE_MODULE})
                self.assertEqual(check["result"], "fail", check)
                self.assertEqual(check["detail"]["failures"][0], headline)
                self.assertEqual((check["detail"].get("blocked") or {}).get("causes"), blocked)
                if blocked:
                    self.assertEqual(lv.upstream_cause(check), "upstream_outage")


class BlockedCauseTests(unittest.TestCase):
    """``blocked_when`` names its cause (``blocked_cause``, default ``upstream_outage``):
    case.packaging_oracle run whole through ``run_spec`` over its producer's own report."""

    PO, LINE = "public.app_purchase_order", "public.app_purchase_order_line"

    def setUp(self) -> None:
        self.fake = FakeRoot()
        self.addCleanup(self.fake.close)
        real = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "case.packaging_oracle.json")
                          .read_text())
        self.spec = {**real, "command": ["python3", "-m", FAKE_MODULE, "echo"]}

    def produce(self, po_number: str, case_lines: int) -> tuple[int, str]:
        """``scripts.eval_packaging_oracle_check.main``: a case rooted on ``root`` citing
        ``case_lines`` lines, the oracle reading one served purchase order with 4 lines."""
        from scripts import eval_packaging_oracle_check as oracle
        tables = {self.PO: [{"id": "root", "po_number": po_number, "custom_fields": {}}],
                  self.LINE: [{"id": f"l{i}", "purchase_order_id": "root"} for i in range(4)]}
        case = {"root": {"relation": self.PO, "id": "root", "selected_by": "plx.find"},
                "lines": case_lines, "checks": {"po_cited": True}, "blocked_upstream": []}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = oracle.main(["--state-dir", "s", "--mirror-audience", "a", "--principal",
                                "staff:operations", "--po", "PO5447"],
                               environ={}, case_runner=lambda args, env: (0, case),
                               read_relation=lambda relation, columns: {
                                   "rows": [dict(r) for r in tables.get(relation, [])]})
        return code, out.getvalue()

    def run_spec(self, value: dict, code: int, document: str) -> dict:
        return self.fake.run(value, environ={
            **self.fake.env, "PLX_WORKBENCH_API_URL": "https://portal.invalid",
            "PLX_WORKBENCH_API_KEY": SECRET, "FAKE_DOC": document, "FAKE_EXIT": str(code)})

    def test_the_packaging_oracle_spec_reaches_a_verdict_and_names_a_source_gap(self) -> None:
        # The spelling plx.find normalises (PO#5447) is a verdict: pass.
        check = self.run_spec(self.spec, *self.produce("PO#5447", 4))
        self.assertEqual(check["result"], "pass", check)
        # A gap alone: blocked with cause source_gap, never a pass.
        gap = self.produce("BC-9", 4)
        check = self.run_spec(self.spec, *gap)
        self.assertEqual((check["result"], check["detail"]["cause"], check["detail"]["reason"]),
                         ("blocked", "source_gap",
                          "upstream portal-workbench-api requested_po_not_in_portal"))
        self.assertNotIn("failures", check["detail"])
        self.assertEqual(lv.tier_summary([check])["deterministic_tools"]["blocked_ids"],
                         {"case.packaging_oracle": "source_gap"})
        # A gap beside lines that differ: the gap never masks the mismatch.
        check = self.run_spec(self.spec, *self.produce("BC-9", 3))
        self.assertEqual(check["result"], "fail", check)
        self.assertNotIn("cause", check["detail"])
        # A spec naming no cause keeps upstream_outage.
        plain = {k: v for k, v in self.spec.items() if k != "blocked_cause"}
        check = self.run_spec(plain, *gap)
        self.assertEqual((check["result"], check["detail"]["cause"]),
                         ("blocked", "upstream_outage"))
        # blocked_causes names the cause of the blocked strings it lists (a value only a
        # human can supply: later_ask); any other blocked string keeps blocked_cause.
        asked = {**self.spec, "blocked_causes": {"requested_po_not_in_portal": "later_ask"}}
        lv.validate_spec(asked, allowed_modules={FAKE_MODULE})
        check = self.run_spec(asked, *gap)
        self.assertEqual((check["result"], check["detail"]["cause"]), ("blocked", "later_ask"))
        other = {**self.spec, "blocked_causes": {"app_registration_absent": "later_ask"}}
        check = self.run_spec(other, *gap)
        self.assertEqual((check["result"], check["detail"]["cause"]), ("blocked", "source_gap"))

    def test_an_unknown_or_unanchored_cause_is_refused(self) -> None:
        for value in ({**self.spec, "blocked_cause": "outage"},
                      {**self.spec, "blocked_cause": ["source_gap"]},
                      {k: v for k, v in self.spec.items() if k != "blocked_when"},
                      {**self.spec, "blocked_causes": {"x": "outage"}},
                      {**self.spec, "blocked_causes": {"": "later_ask"}},
                      {**self.spec, "blocked_causes": {}},
                      {**self.spec, "blocked_causes": ["later_ask"]}):
            with self.subTest(value.get("blocked_cause"), causes=value.get("blocked_causes")), \
                    self.assertRaises(lv.SpecError):
                lv.validate_spec(value, allowed_modules={FAKE_MODULE})


class RepositorySpecsTests(unittest.TestCase):
    """The real specs: valid, allowlisted, credential-gated. Never executed here."""

    def test_every_spec_timeout_fits_the_runtime_command_limit(self) -> None:
        for path in sorted((lv.ROOT / lv.SPECS_DIR_NAME).glob("*.json")):
            timeout = json.loads(path.read_text(encoding="utf-8"))["timeout_seconds"]
            self.assertLess(timeout, 7200, path.name)

    def test_every_real_spec_is_valid(self) -> None:
        entries = lv.load_specs(lv.ROOT / lv.SPECS_DIR_NAME)
        self.assertGreaterEqual(len(entries), 11)
        for entry in entries:
            with self.subTest(spec=entry["id"]):
                self.assertIsNone(entry["error"])
                self.assertEqual(entry["warnings"], [])
                spec_value = entry["spec"]
                if not spec_value.get("disabled"):
                    self.assertIn(spec_value["command"][2], lv.ALLOWED_MODULES)
                # An offline spec over host state (session.employee_recorded) has no
                # credential to gate on: it names a missing state directory itself
                # (``blocked_when``) rather than reading skipped.
                self.assertTrue(spec_value.get("requires_env")
                                or spec_value.get("requires_env_any")
                                or spec_value.get("disabled")
                                or (spec_value.get("blocked_when")
                                    and spec_value.get("optional_env")))
        ids = {e["id"] for e in entries}
        by_id = {e["id"]: e["spec"] for e in entries}
        # The MC read-tool probe runs live; its tool
        # allowlist is read-only (integrations/tests/test_mc_brain_probe_scope.py).
        self.assertIn("integrations.mc_brain_probe", lv.ALLOWED_MODULES)
        self.assertNotIn("disabled", by_id["mc.brain_probe"])
        # The full mirror sync and the keyset counter are too long for the hourly run.
        self.assertEqual(lv.spec_cadence(by_id["mirror.full_coverage"]), "daily")
        # The keyset counter, the breadth and QC/QA captures and the Teams capture run weekly
        # because their daily cost left a daily cycle over 24 h.
        for weekly in ("mirror.relation_counts", "capture.breadth", "capture.qcqa_library",
                       "graph.teams_capture"):
            self.assertEqual(lv.spec_cadence(by_id[weekly]), "weekly")
        for expected in ("graph.adapter_probe", "graph.adapter_probe_content",
                         "bc.adapter_probe", "bc.case_entities", "mc.initialize_tools_list",
                         "vmc.knowledge_search", "portal.health_catalog",
                         "case.customer_po_coverage", "case.workspace_live",
                         "case.mail_evidence"):
            self.assertIn(expected, ids)

    def test_every_record_enums_path_is_a_recorded_path(self) -> None:
        # An enum path no recorded path is (or lies below) never copies a string.
        for path in sorted((lv.ROOT / lv.SPECS_DIR_NAME).glob("*.json")):
            spec = json.loads(path.read_text(encoding="utf-8"))
            recorded = lv.record_paths(spec)
            with self.subTest(spec=path.name):
                self.assertEqual([enum for enum in spec.get("record_enums", ())
                                  if not any(lv.under(p, [enum]) for p in recorded)], [])

    def test_keyless_run_skips_everything_without_starting_a_process(self) -> None:
        out = io.StringIO()
        real_popen = lv.subprocess.Popen

        def only_git(argv, *args, **kwargs):  # git metadata reads are fine; specs are not
            if argv[0] != "git":
                raise AssertionError("must not execute a spec: " + " ".join(argv[:3]))
            return real_popen(argv, *args, **kwargs)

        with mock.patch.object(lv.subprocess, "Popen", side_effect=only_git), \
                contextlib.redirect_stdout(out):
            code = lv.main(["--main-sha", "c" * 40], environ={"PATH": "/usr/bin"})
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["summary"]["pass"] + report["summary"]["fail"], 0)
        # Keyless, a spec is skipped, or (``blocked_without_env``, an owner-supplied
        # credential it waits on) blocked credentials_absent naming that credential; either
        # way no spec process started (the Popen patch above).
        waiting = {}
        for path in sorted((lv.ROOT / lv.SPECS_DIR_NAME).glob("*.json")):
            spec = json.loads(path.read_text(encoding="utf-8"))
            if spec.get("blocked_without_env") and not lv.unmet_env(spec, {"PATH": "/usr/bin"}):
                waiting[spec["id"]] = spec["blocked_without_env"]
        for check in report["checks"]:
            with self.subTest(check=check["id"]):
                if check["id"] in waiting:
                    self.assertEqual(check["result"], "blocked")
                    self.assertEqual(check["detail"]["cause"], "later_ask")
                    self.assertTrue(check["detail"]["reason"].endswith(" credentials_absent"))
                    self.assertEqual(check["detail"]["missing_settings"], waiting[check["id"]])
                else:
                    self.assertEqual(check["result"], "skipped")

    def test_relation_counts_state_survives_the_detail_cap(self) -> None:
        """The two-run rule reads unstable_relations and the per-relation streaks back
        from the capped detail: a spec grown to many relations must still keep them."""
        spec_doc = json.loads((Path(lv.__file__).resolve().parent / "live-checks" /
                               "mirror.relation_counts.json").read_text(encoding="utf-8"))
        state = ("unstable_relations", "relations[*].relation",
                 "relations[*].classification", "relations[*].defect_streak",
                 "relations[*].stable_streak", "relations[*].unstable")
        relation = {path.split(".", 1)[1]: ("x" * 60 if "hash" in path or "note" in path
                                           else 123456789)
                    for path in spec_doc["record"] if path.startswith("relations[*].")}
        relation.update(relation="public.some_relation", classification="mirror_bound",
                        defect_streak=1, stable_streak=0, unstable=False)
        document = {"unstable_relations": ["public.some_relation"],
                    "relations": [dict(relation) for _ in range(12)]}
        for path in spec_doc["record"]:
            if "[*]" not in path and "." not in path:
                document.setdefault(path, "v" * 120)
        detail = lv._cap({"fields": lv._recorded(spec_doc, document)})
        self.assertTrue(detail.get("fields_truncated"), "fixture must overflow the cap")
        for path in state:
            self.assertIn(path, detail["fields"], path)
        self.assertLessEqual(len(json.dumps(detail, sort_keys=True)), lv.MAX_DETAIL_CHARS)

    def test_the_cap_keeps_asserted_paths_after_large_unasserted_fields(self) -> None:
        """host.cycles passed at 39eeb70 asserting case_resume.claim_visible, yet its
        over-cap record dropped every case_resume path: the asserted fields stay first."""
        spec_doc = json.loads((Path(lv.__file__).resolve().parent / "live-checks" /
                               "host.cycles.json").read_text(encoding="utf-8"))
        counts = {f"messages_in_folder_{i:02}": 123456789 for i in range(50)}
        for cycles in (1, 10, 60):
            with self.subTest(cycles=cycles):
                document = {
                    "status": "ok", "checks": {"cycles_run": 2, "sources_configured": 4},
                    "cycles": [{"state": "complete", "sources": {"bc": {"counts": counts},
                                                                 "mail": {"counts": counts}}}
                               for _ in range(cycles)],
                    "interrupt": {"signal": "SIGKILL", "state": "killed_running"},
                    "case_resume": {"claim_visible": True, "withheld_visible": 0}}
                fields = lv._recorded(spec_doc, document)
                self.assertLess(list(fields).index("cycles[*].sources.mail.counts"),
                                list(fields).index("case_resume"))
                detail = {"complete": True, "fields": fields}
                self.assertGreater(len(json.dumps(detail, sort_keys=True)), lv.MAX_DETAIL_CHARS)
                capped = lv._cap(detail, spec_doc)
                self.assertLessEqual(len(json.dumps(capped, sort_keys=True)),
                                     lv.MAX_DETAIL_CHARS)
                kept = capped["fields"]
                self.assertEqual(kept["case_resume"],
                                 {"claim_visible": True, "withheld_visible": 0})
                self.assertEqual((kept["checks"]["cycles_run"], kept["interrupt.signal"]),
                                 (2, "SIGKILL"))
                self.assertEqual(kept["cycles[*].state"], ["complete"] * min(cycles, 50))
                self.assertEqual(kept.get("cycles[*].state.count"),
                                 cycles if cycles > 50 else None)
                self.assertNotIn("cycles[*].sources.mail.counts", kept)  # unasserted
                # every kept value is as recorded; the count is of the dropped paths only
                self.assertEqual(kept, {k: fields[k] for k in kept})
                self.assertEqual(capped["fields_truncated"],
                                 {"paths": len(fields) - len(kept)})
                self.assertTrue(capped["complete"])
        # a path only expect_blocked asserts stays too
        blocked_only = {"expect": {"exit_code": 0, "json_fields": []},
                        "expect_blocked": {"exit_code": 0, "json_fields": [],
                                           "json_min": {"late.measured": 1}}}
        fields = {**{f"wide_{i}": "x" * 200 for i in range(40)}, "late.measured": 3}
        capped = lv._cap({"fields": fields}, blocked_only)
        self.assertEqual(capped["fields"]["late.measured"], 3)
        self.assertEqual(capped["fields_truncated"],
                         {"paths": len(fields) - len(capped["fields"])})
        self.assertGreater(capped["fields_truncated"]["paths"], 0)

    def test_the_envelope_fallback_keeps_asserted_scalars(self) -> None:
        spec_doc = {"expect": {"exit_code": 0, "json_fields": ["done"],
                               "json_equals": {"case_resume.claim_visible": True},
                               "json_max": {"leaks": 0}}}
        fields = {"status": "ok", "other": 7, "rows": [1, 2, 3], "done": "<str len=4>",
                  "case_resume.claim_visible": True, "leaks": 0}
        # the non-field parts alone are over the cap: only the envelope fallback fits
        capped = lv._cap({"unverified": ["missing x" * 50] * 20, "failures": ["exit"],
                          "fields": fields}, spec_doc)
        self.assertLessEqual(len(json.dumps(capped, sort_keys=True)), lv.MAX_DETAIL_CHARS)
        self.assertNotIn("unverified", capped)
        self.assertEqual(capped["fields"], {"status": "ok", "done": "<str len=4>",
                                            "case_resume.claim_visible": True, "leaks": 0})
        self.assertEqual(capped["fields_truncated"], {"paths": 2})


    def test_bc_case_entities_spec_flags_the_known_select_gap(self) -> None:
        value = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "bc.case_entities.json").read_text())
        document = {"requests_sent": 54, "case_entities": [{"company_index": 0, "entities": [
            {"entity_set": "purchaseInvoices", "records_returned": 1,
             "select_allowlist_absent": [], "filter_fields_absent": [],
             "lines": {"records_returned": 3, "select_allowlist_absent": ["directUnitCost"]}}]}]}
        self.assertEqual(lv.check_expectations(value, 0, document), [
            "case_entities[*].entities[*].lines.select_allowlist_absent not empty"])
        document["case_entities"][0]["entities"][0]["lines"]["select_allowlist_absent"] = []
        self.assertEqual(lv.check_expectations(value, 0, document), [])
        fields = lv._recorded(value, document)
        self.assertEqual(fields["case_entities[*].entities[*].entity_set"], ["purchaseInvoices"])


class LiveReadProbesTests(unittest.TestCase):
    """scripts/live_read_probes.py against injected clients (no network)."""

    def test_portal_health_reports_counts_only(self) -> None:
        client = mock.Mock()
        client.health.return_value = {"ok": True, "recovery": True, "replayLagSeconds": 2,
                                      "unavailable": [], "missingColumns": [],
                                      "generatedAt": "2026-10-01T00:00:00Z"}
        client.relations.return_value = {
            "public.app_sales_order": {"key": ["id"], "since": {"column": "u"}},
            "sage.oe_order_header": {"key": ["orduniq"]}, "silver.x": {"key": ["id"]}}
        report = probes.portal_health({}, client=client)
        self.assertEqual(report["relation_count"], 3)
        self.assertEqual(report["relations_by_schema"], {"public": 1, "sage": 1, "silver": 1})
        self.assertEqual(report["relations_with_since"], 1)
        self.assertTrue(report["health_ok"] and report["recovery"])
        self.assertEqual((report["degraded"], report["available_relation_count"],
                          report["blocked_upstream"]), (False, 3, []))

    def test_portal_health_degraded_exits_0_and_names_the_gaps(self) -> None:
        from integrations.tests.test_workbench_read_api import (KEY, SAGE_GAPS, DegradedApi,
                                                                gap_reason)
        from integrations.workbench_read_api import WorkbenchReadApiClient
        fake = DegradedApi()
        client = WorkbenchReadApiClient("https://api.example.test", KEY, fetch=fake)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(probes.main(["portal-health"],
                                         clients={"portal-health": client}), 0)
        report = json.loads(out.getvalue())
        reasons = {name: gap_reason(count) for name, count in SAGE_GAPS.items()}
        self.assertEqual({key: report[key] for key in (
            "health_ok", "degraded", "unavailable_relations", "unavailable_count",
            "missing_columns_count", "relation_count", "available_relation_count")},
            {"health_ok": False, "degraded": True, "unavailable_relations": reasons,
             "unavailable_count": 4, "missing_columns_count": 4, "relation_count": 93,
             "available_relation_count": 89})
        value = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "portal.health_catalog.json")
                           .read_text())
        self.assertIn("degraded != expected", lv.check_expectations(value, 0, report))
        self.assertEqual(lv.check_expectations(value, 0, report, value["expect_blocked"]), [])
        self.assertEqual({p["part"]: (p["cause"], p["reason"]) for p in lv.blocked_parts(report)},
                         {name: ("relation_unavailable", reason)
                          for name, reason in reasons.items()})
        fake.sage_served = True
        fake.health_body.update(unavailable=[], missingColumns=[], loginExpiringSoon=True,
                                loginExpiresAt="2026-10-11T00:00:00.000Z",
                                loginExpiresInDays=4, keysExpiringSoon=[
                                    {"id": "wb-key-vince", "expiresAt": "2026-10-12",
                                     "expiresInDays": 5}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(probes.main(["portal-health"],
                                         clients={"portal-health": client}), 0)
        warned = json.loads(out.getvalue())
        self.assertNotIn("wb-key-vince", out.getvalue())
        self.assertEqual((warned["health_ok"], warned["degraded"], warned["blocked_upstream"]),
                         (False, False, []))
        self.assertEqual(lv.check_expectations(value, 0, warned), [])  # a warning, never a fail
        self.assertEqual(lv._recorded(value, warned)["warnings"], {
            "login_expiring_soon": True, "login_expires_at": "2026-10-11T00:00:00Z",
            "login_expires_in_days": 4, "keys_expiring_soon_count": 1})
        fake.health_body["recovery"] = False
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(probes.main(["portal-health"],
                                         clients={"portal-health": client}), 1)
        self.assertEqual(json.loads(err.getvalue()),
                         {"error": "WorkbenchNotReady", "probe": "portal-health",
                          "reason": "health: recovery is not true"})

    def test_main_reports_http_status_and_code_only(self) -> None:
        import contextlib
        import io
        from integrations.http import SourceHttpError
        from integrations.source_mirror import MirrorError
        for exc, want in (
                (SourceHttpError("https://x.test?k=s", status=401, error_code="invalid_key"),
                 {"error": "SourceHttpError", "probe": "portal-health", "http_status": 401,
                  "error_code": "invalid_key"}),
                (SourceHttpError("m", status=403, error_code="x y<script>"),
                 {"error": "SourceHttpError", "probe": "portal-health", "http_status": 403}),
                (RuntimeError("boom"), {"error": "RuntimeError", "probe": "portal-health"})):
            client = mock.Mock()
            client.health.side_effect = exc
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(probes.main(["portal-health"], clients={"portal-health": client}), 1)
            self.assertEqual(json.loads(err.getvalue()), want)
        try:
            try:
                raise SourceHttpError("m", status=502)
            except SourceHttpError as cause:
                raise MirrorError("wrapped") from cause
        except MirrorError as wrapped:
            client.health.side_effect = wrapped
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            probes.main(["portal-health"], clients={"portal-health": client})
        self.assertEqual(json.loads(err.getvalue()),
                         {"error": "MirrorError", "probe": "portal-health", "http_status": 502})

    def test_main_names_an_mc_refusal_by_its_upstream_code(self) -> None:
        # mc.initialize_tools_list printed a bare UpstreamError while
        # mc.brain_probe saw 401 invalid_api_key. The real MC client and hub transport run
        # against an injected sender; the planted key and URL never print.
        from integrations.live_source_guard import Secret
        from integrations.mc import MissionControlMcpClient
        from integrations.plx_hub_guard import HubTransport, McConfig, UpstreamError
        key = "mc-test-key-0123456789"

        def mc_client(status, code):
            def sender(method, url, headers, body, timeout, max_bytes):
                payload = {"error": {"code": code, "message": f"{key} at https://mc.plxcustomer.io/x"}}
                return status, {"content-type": "application/json"}, json.dumps(payload).encode()
            return MissionControlMcpClient(
                McConfig("https://mc.plxcustomer.io", Secret(key), "cos@petrasoap.com"),
                hub=HubTransport(sender, environ={}, sleep=lambda _s: None, jitter=lambda: 1.0))

        free_text_tool = mock.Mock()
        free_text_tool._rpc.side_effect = UpstreamError("m", code="acme-customer", status=200,
                                                        layer="tool")
        unreachable = mock.Mock()
        unreachable._rpc.side_effect = UpstreamError("m", code="upstream_unreachable",
                                                     layer="transport")
        for client, published in (
                (mc_client(401, "invalid_api_key"),
                 {"http_status": 401, "error_code": "invalid_api_key"}),
                (mc_client(503, "mcp_key_config_invalid"),
                 {"http_status": 503, "error_code": "mcp_key_config_invalid"}),
                (mc_client(503, "schema_behind"), {"http_status": 503, "error_code": "schema_behind"}),
                # outside the closed vocabularies: the key itself, an MC tool's free text
                (mc_client(500, key), {"http_status": 500, "error_code": "other"}),
                (free_text_tool, {"http_status": 200, "error_code": "other"}),
                (unreachable, {"error_code": "upstream_unreachable"})):
            with self.subTest(published=published):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(probes.main(["mc-tools"], environ={},
                                                 clients={"mc-tools": client}), 1)
                stderr = err.getvalue()
                self.assertEqual(json.loads(stderr),
                                 {"error": "UpstreamError", "probe": "mc-tools", **published})
                # what the verifier publishes from that stderr
                self.assertEqual(lv._stderr_error(stderr), {"error": "UpstreamError", **published})
                self.assertNotIn(key, stderr)
                self.assertNotIn("plxcustomer", stderr)

    def test_vmc_search_reports_no_hit_text(self) -> None:
        client = mock.Mock()
        client.search.return_value = {
            "data": {"query": probes.VMC_PROBE_QUERY, "source": "all", "total": 7,
                     "partial": False, "results": [{"id": "n1", "title": "Secret plan",
                                                    "snippet": "private"}]},
            "meta": {}, "trust": {"label": "untrusted_lead"}}
        report = probes.vmc_search({}, client=client)
        client.search.assert_called_once_with(probes.VMC_PROBE_QUERY, limit=3)
        self.assertEqual(report["result_count"], 1)
        self.assertEqual(report["result_keys"], ["id", "snippet", "title"])
        self.assertNotIn("Secret plan", json.dumps(report))
        self.assertEqual((report["degraded"], report["blocked_upstream"]), ([], []))
        # The real spec's verdict over the probe's own output: a component VMC flags
        # degraded blocks the check naming it (a flag not plainly false, here free text,
        # counts); a partial answer with no flag still fails; a full answer passes.
        fake = FakeRoot()
        self.addCleanup(fake.close)
        real = json.loads((lv.ROOT / "verify/live-checks/vmc.knowledge_search.json")
                          .read_text())
        spec = {k: v for k, v in real.items() if k != "requires_env_any"}
        spec["command"] = ["python3", "-m", FAKE_MODULE, "echo"]
        full = client.search.return_value["data"]
        partial = {"total": 0, "partial": True, "results": [], "documentsDegraded": False}
        for name, data, result, parts in (
                ("degraded", {**partial, "semanticDegraded": True}, "blocked", ["semantic"]),
                ("two degraded", {**partial, "semanticDegraded": True,
                                  "memoryDegraded": "index offline at host-7"},
                 "blocked", ["memory", "semantic"]),
                ("unflagged partial", partial, "fail", None),
                ("full", full, "pass", None)):
            with self.subTest(name):
                client.search.return_value = {"data": data, "meta": {},
                                              "trust": {"label": "untrusted_lead"}}
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(probes.main(["vmc-search"],
                                                 clients={"vmc-search": client}), 0)
                check = lv.run_spec(spec, root=fake.root,
                                    environ={**fake.env, "FAKE_DOC": out.getvalue()},
                                    allowed_modules={FAKE_MODULE})
                self.assertEqual(check["result"], result, check)
                self.assertNotIn("host-7", json.dumps(check))
                blocked = check["detail"].get("blocked")
                if parts is None:
                    self.assertIsNone(blocked)
                else:
                    self.assertEqual([(p["part"], p["source"], p["error_code"])
                                      for p in blocked["parts"]],
                                     [(c, "vmc", "upstream_degraded") for c in parts])
                    self.assertEqual(check["detail"]["fields"]["degraded"], parts)
                    self.assertIn("partial != expected", " ".join(
                        check["detail"]["unverified"]))
                if result == "fail":
                    self.assertIn("partial != expected", " ".join(
                        check["detail"]["failures"]))

    def test_mc_tools_uses_initialize_and_tools_list_only(self) -> None:
        from ontology.generator.mcp import PIN_PATH
        client = mock.Mock()
        client._rpc.return_value = {"serverInfo": {"name": "PLX-MC"},
                                    "protocolVersion": "2025-06-18"}
        pinned = json.loads(PIN_PATH.read_text())["servers"]["plx-mc"]["tools"]
        stdio = json.loads((lv.ROOT / "integrations/mc_tool_policy.json").read_text())["stdio_only"]
        listed = [{"name": name, "inputSchema": {
            "type": "object", "properties": {k: {} for k in tool["input"]},
            "required": [k for k, p in tool["input"].items() if p["required"]]}}
            for name, tool in sorted(pinned.items()) if name not in stdio]
        client.list_tool_specs.return_value = listed
        report = probes.mc_tools({}, client=client)
        self.assertEqual(client._rpc.call_args.args[0], "initialize")
        client.call_tool.assert_not_called()
        self.assertEqual((report["tool_count"], report["schema_matched"]), (39, 39))  # HTTP tools at PLX_MC 5bf29d2 (e857f8c added mc_upload_evidence; 5bf29d2 adds no tool)
        self.assertEqual(report["server_name"], "PLX-MC")
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mc.initialize_tools_list.json")
                          .read_text())
        self.assertEqual(lv.check_expectations(spec, 0, report), [])
        # Upstream drift against the pin fails the check by tool name: a new input, a
        # dropped required, an output schema the pin lacks, a tool the pin never saw.
        drifted = copy.deepcopy(listed)
        by_name = {t["name"]: t for t in drifted}
        by_name["mc_create_task"]["inputSchema"]["properties"]["dueDate"] = {}
        by_name["mc_get_task"]["inputSchema"]["required"] = []
        by_name["mc_list_buckets"]["outputSchema"] = {"type": "object"}
        drifted = [t for t in drifted if t["name"] != "mc_self_check"] + [
            {"name": "mc_brand_new", "inputSchema": {"type": "object"}}]
        client.list_tool_specs.return_value = drifted
        report = probes.mc_tools({}, client=client)
        self.assertEqual(report["schema_mismatches"],
                         ["mc_create_task", "mc_get_task", "mc_list_buckets"])
        self.assertEqual((report["schema_unpinned"], report["schema_missing"],
                          report["output_schema_tools"]),
                         (["mc_brand_new"], ["mc_self_check"], ["mc_list_buckets"]))
        failed = lv.check_expectations(spec, 0, report)
        self.assertTrue(any("schema_mismatches" in f for f in failed), failed)
        self.assertTrue(any("schema_unpinned" in f for f in failed), failed)

    def _mc_tools_config(self, environ, default):
        """Run mc_tools with the MC client class replaced; return the McConfig it got."""
        captured = {}

        class Client:
            def __init__(self, config, *, hub):
                captured["config"] = config

            def _rpc(self, method, params):
                return {"serverInfo": {"name": "PLX-MC"}, "protocolVersion": "2025-06-18"}

            def list_tool_specs(self):
                return [{"name": "mc_get_task", "inputSchema": {
                    "type": "object", "properties": {"id": {}}, "required": ["id"]}}]
        with mock.patch("integrations.mc.MissionControlMcpClient", Client):
            report = probes.mc_tools(environ, operator_email_default=default)
        return captured["config"], report

    def test_mc_tools_operator_email_env_wins_over_the_spec_default(self) -> None:
        env = {"MC_MCP_API_KEY": "test-only-key", "MC_OPERATOR_EMAIL": "ops@example.com"}
        config, report = self._mc_tools_config(env, "cos@petrasoap.com")
        self.assertEqual(config.operator_email, "ops@example.com")
        self.assertEqual(report["operator_email_source"], "env")
        config, report = self._mc_tools_config({"MC_MCP_API_KEY": "test-only-key"},
                                                "cos@petrasoap.com")
        self.assertEqual(config.operator_email, "cos@petrasoap.com")
        self.assertEqual(report["operator_email_source"], "spec_default")
        self.assertNotIn("petrasoap", json.dumps(report))  # the value is never reported
        with self.assertRaises(Exception) as raised:
            self._mc_tools_config({"MC_MCP_API_KEY": "test-only-key"}, None)
        self.assertIn("MC_OPERATOR_EMAIL", str(raised.exception))

    def test_mc_spec_runs_without_operator_email_env_and_carries_the_default(self) -> None:
        spec = json.loads((lv.ROOT / lv.SPECS_DIR_NAME / "mc.initialize_tools_list.json")
                          .read_text())
        self.assertEqual(lv.validate_spec(spec), [])
        self.assertEqual(spec["requires_env"], ["MC_MCP_API_KEY"])
        args = spec["command"]
        self.assertEqual(args[args.index("--operator-email-default") + 1], "cos@petrasoap.com")
        self.assertIn("operator_email_source", spec["record_enums"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            probes.main(["portal-health", "--operator-email-default", "x@example.com"],
                        environ={})

    def test_mc_initialize_body_passes_the_read_only_guard(self) -> None:
        from integrations.plx_hub_guard import check_mcp_body
        self.assertEqual(check_mcp_body({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": "2025-06-18"}}),
                         "initialize")

    def test_main_prints_only_the_error_class_on_failure(self) -> None:
        client = mock.Mock()
        client.health.side_effect = RuntimeError("https://host/v1/health?key=plxwb_abc")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = probes.main(["portal-health"], environ={},
                               clients={"portal-health": client})
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err.getvalue()),
                         {"error": "RuntimeError", "probe": "portal-health"})
        with contextlib.redirect_stderr(io.StringIO()) as missing, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probes.main(["portal-health"], environ={}), 1)
        self.assertIn("LookupError", missing.getvalue())


if __name__ == "__main__":
    unittest.main()
