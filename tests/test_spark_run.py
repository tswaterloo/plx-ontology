"""The Spark launcher's boundaries (deploy/spark): what of the box's state may go to a Spark,
what of the caller's environment goes with a run, and what spark_run.sh refuses to ship,
checked without a Spark (every refusal happens before any connection)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "spark"))
import spark_run_remote as remote  # noqa: E402
import spark_run_spec as spec  # noqa: E402
import spark_state_sync as sync  # noqa: E402


def _db(path: Path, wal: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    if wal:
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (a)")
    c.commit()
    c.close()


class StatePlan(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.root)]))

    def put(self, rel: str, text: str = "x") -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_only_allowed_stores_go_never_sessions_keys_heldout_or_blobs(self):
        _db(self.root / "mail-capture" / "capture.db", wal=True)
        _db(self.root / "session-replay.sqlite", wal=False)
        _db(self.root / "projection" / "delta" / "delta-20261119T010508869306.sqlite", False)
        for rel in ("app-sessions.json", "console-ticket-audit.jsonl", "eval-heldout/items.json",
                    "ts-petra/ssh_config", "ts-petra/id_ed25519", "ops-alarms/status.json",
                    "jobq/q.json", "view-as/audit.jsonl", "host/workbench.env",
                    "host/api-token.json", "mail-capture/blobs/ab/abcd",
                    "mail-capture/capture.db-wal", "mail-capture/capture.db.new",
                    "app-data/known-readers.json", "projection/delta/work-20261119T0552/x",
                    "sharepoint-capture/.lookup.sqlite.lock", "autopilot/queue.json",
                    "answer-bank/bank.json", "host/logs/a.log", "case-runtime/cases.sqlite"):
            self.put(rel)
        got = {r["rel"]: r["kind"] for r in sync.plan(self.root)}
        self.assertEqual(got, {"mail-capture/capture.db": "wal",
                               "session-replay.sqlite": "rollback",
                               "autopilot/queue.json": "file",
                               "answer-bank/bank.json": "file",
                               "case-runtime/cases.sqlite": "file"})
        with_blobs = {r["rel"] for r in sync.plan(self.root, blobs=True)}
        self.assertEqual(with_blobs - set(got), {"mail-capture/blobs/ab/abcd"})
        self.assertEqual({r["rel"] for r in sync.plan(self.root, only=("mail-capture",))},
                         {"mail-capture/capture.db"})

    def test_text_blobs_go_alone_only_the_text_a_store_still_holds(self):
        """HARD20 h07 (11-21): a Spark run had no text of any captured file (its blob folder
        is never copied), so plx.open said "no text was extracted" for files the box reads.
        --text-blobs adds the blobs of kind text a store still holds, never a file body, a
        dropped blob, a name that is not a digest or one missing on disk."""
        db = self.root / "sharepoint-capture" / "capture.db"
        _db(db, wal=True)
        shas = {k: (f"{i:x}" * 64)[:64] for i, k in enumerate(
            ("text", "content", "dropped", "missing", "unknown"), start=1)}
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, kind TEXT, bytes INTEGER, "
                  "stored_at TEXT, dropped_at TEXT, stored_bytes INTEGER)")
        for k, sha in shas.items():
            c.execute("INSERT INTO blobs VALUES (?, ?, 1, 'x', ?, 1)",
                      (sha, "text" if k in ("text", "dropped", "missing") else "content",
                       "2026-11-16" if k == "dropped" else None))
        c.execute("INSERT INTO blobs VALUES ('../../etc/passwd', 'text', 1, 'x', NULL, 1)")
        c.commit()
        c.close()
        for k in ("text", "content", "dropped"):
            self.put(f"sharepoint-capture/blobs/sha256/{shas[k][:2]}/{shas[k]}")
        # a mail capture: its attachment text is a loose compressed file (a symlinked blob folder)
        mail = self.root / "mail-capture"
        _db(mail / "capture.db", wal=True)
        c = sqlite3.connect(mail / "capture.db")
        c.execute("CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, kind TEXT, bytes INTEGER, "
                  "stored_at TEXT, dropped_at TEXT, stored_bytes INTEGER)")
        mtext, mbody = "e" * 64, "f" * 64
        c.execute("INSERT INTO blobs VALUES (?, 'text', 1, 'x', NULL, 1)", (mtext,))
        c.execute("INSERT INTO blobs VALUES (?, 'body', 1, 'x', NULL, 1)", (mbody,))
        c.commit()
        c.close()
        self.put(f"mail-capture/blobs.nvme/sha256/ee/{mtext}.z")
        (mail / "blobs").symlink_to("blobs.nvme")
        base = {r["rel"] for r in sync.plan(self.root)}
        got = {r["rel"]: r["kind"] for r in sync.plan(self.root, text_blobs=True)}
        extra = set(got) - base
        self.assertEqual(extra, {f"sharepoint-capture/blobs/sha256/{shas['text'][:2]}/"
                                 f"{shas['text']}", f"mail-capture/blobs/sha256/ee/{mtext}.z"})
        self.assertEqual({got[r] for r in extra}, {"file"})
        self.assertEqual(base, {r["rel"] for r in sync.plan(self.root)})   # off by default

    def test_a_folder_outside_the_list_never_goes_whatever_it_holds(self):
        _db(self.root / "spark" / "x.sqlite", wal=True)
        self.put("unknown-new-dir/data.json")
        self.put("windows/holds/x")
        self.assertEqual(sync.plan(self.root), [])

    def test_a_bad_destination_refuses(self):
        for dest in ("../x", "/home/x", "a b", "x;rm"):
            with self.assertRaises(SystemExit):
                sync.main(["--host", "s7d3d", "--dest", dest, "--dry-run",
                           "--state", str(self.root)])


class RunEnvironment(unittest.TestCase):
    ENV = {"PLX_ONTOLOGY_PG": "only", "PLX_WORKBENCH_SESSION_SECRET": "s3",
           "PLX_EVAL_READER": '{"email": "x"}', "PLX_API_KEY": "k", "PLX_PG_DSN": "dsn",
           "PLX_AGENT_SESSION_TICKET": "t", "PLX_SPARK_SSH_CONFIG": "/c", "HOME": "/h",
           "AZURE_CLIENT_SECRET": "a", "TZ": "America/New_York", "OTHER": "o",
           "SPEC_SHA": "a" * 40, "SPEC_TREE": "/t", "SPEC_CWD": "/t", "SPEC_STATE": "copy",
           "SPEC_NAME": "n"}

    def test_secrets_go_only_by_name_and_nothing_outside_plx(self):
        self.assertEqual(spec.passed_env(self.ENV, []),
                         {"PLX_ONTOLOGY_PG": "only", "PLX_EVAL_READER": '{"email": "x"}',
                          "TZ": "America/New_York"})
        got = spec.passed_env(self.ENV, ["PLX_WORKBENCH_SESSION_SECRET", "ABSENT"])
        self.assertEqual(got["PLX_WORKBENCH_SESSION_SECRET"], "s3")
        self.assertNotIn("ABSENT", got)
        self.assertNotIn("PLX_API_KEY", got)

    def test_a_bad_name_or_no_command_refuses(self):
        for named in (["A B"], ["X=1"], ["$(id)"]):
            with self.assertRaises(ValueError):
                spec.passed_env(self.ENV, named)
        with self.assertRaises(ValueError):
            spec.build(["0", "0", "0"], self.ENV)
        got = spec.build(["1", "/data/scratch/x", "1", "/data/scratch/o", "0", "python3", "-c",
                          "1"], self.ENV)
        self.assertEqual((got["ship"], got["out"], got["cmd"], got["state"]),
                         (["/data/scratch/x"], ["/data/scratch/o"], ["python3", "-c", "1"],
                          "copy"))


    def test_the_people_exclusion_goes_with_every_run_or_nothing(self):
        # the Spark's state copy has no exclusion: the box's list travels in the spec
        oid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        cmd = ["0", "0", "0", "python3", "-c", "1"]
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / "proactive.env"
            env_file.write_text(f"PLX_EXCLUDED_PEOPLE_OIDS={oid}\n")
            env_file.chmod(0o600)
            env = {**self.ENV, "PLX_EXCLUSION_ENV_FILE": str(env_file)}
            got = spec.build(cmd, env)
            self.assertEqual(got["env"]["PLX_EXCLUDED_PEOPLE_OIDS"], oid)
            # a box path to a list file is replaced by the ids it names
            listed = Path(d) / "ids.json"
            from integrations.people_exclusion import SCHEMA
            listed.write_text(json.dumps({"schema": SCHEMA, "object_ids": [oid]}))
            got = spec.build(cmd, {**self.ENV, "PLX_EXCLUDED_PEOPLE_FILE": str(listed)})
            self.assertEqual(got["env"].get("PLX_EXCLUDED_PEOPLE_OIDS"), oid)
            self.assertNotIn("PLX_EXCLUDED_PEOPLE_FILE", got["env"])
            # an invalid, absent or unreadable list passes nothing: the run fails closed
            for bad in ({"PLX_EXCLUDED_PEOPLE_OIDS": "not-an-id"},
                        {"PLX_EXCLUDED_PEOPLE_OIDS": ""},
                        {"PLX_EXCLUDED_PEOPLE_FILE": str(Path(d) / "missing.json")},
                        {"PLX_EXCLUSION_ENV_FILE": ""}):
                got = spec.build(cmd, {**self.ENV, **bad})
                self.assertNotIn("PLX_EXCLUDED_PEOPLE_OIDS", got["env"], bad)
                self.assertNotIn("PLX_EXCLUDED_PEOPLE_FILE", got["env"], bad)

class Metering(unittest.TestCase):
    def test_the_sparks_exporter_settings_pass_with_the_run_name_never_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "otel.env"
            self.assertEqual(remote.telemetry_env("x", path), {})
            path.write_text("# metering\nCLAUDE_CODE_ENABLE_TELEMETRY=1\n"
                            "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318\n"
                            "OTEL_LOG_USER_PROMPTS=1\nOTEL_EXPORTER_OTLP_HEADERS=a=b\n"
                            "PLX_WORKBENCH_API_KEY=k\n"
                            "OTEL_RESOURCE_ATTRIBUTES=plx.source=spark-run,plx.host=s7d3d\n")
            self.assertEqual(remote.telemetry_env("bench a", path), {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
                "OTEL_RESOURCE_ATTRIBUTES": "plx.source=spark-run,plx.host=s7d3d,plx.run=bench_a"})
            path.write_text("CLAUDE_CODE_ENABLE_TELEMETRY=0\nOTEL_LOGS_EXPORTER=otlp\n")
            self.assertEqual(remote.telemetry_env("x", path), {})


class Sandbox(unittest.TestCase):
    def test_every_sudo_on_the_spark_is_the_shim_whatever_path_the_job_sets(self):
        """A bench whose command sets its own PATH and calls the Workbench's
        ``sudo -n -u plx-workbench ... claude`` wrapper must reach the shim: the real sudo
        cannot run in the sandbox, so each Claude session exited 1 before its first turn."""
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(home)]))
        fake = home / "fs"
        for real in ("/usr/bin/sudo", "/usr/local/sbin/sudo"):
            (fake / real.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
            (fake / real.lstrip("/")).write_text("real")
        (home / "plx-state-copy").mkdir()
        saved = {k: getattr(remote, k) for k in ("HOME", "RUN", "TESTS", "AI", "STATE_COPY",
                                                  "SUDO_PATHS")}
        self.addCleanup(lambda: [setattr(remote, k, v) for k, v in saved.items()])
        remote.HOME, remote.RUN, remote.TESTS = home, home / "plx-run", home / "plx-tests"
        remote.AI, remote.STATE_COPY = home / "plx-ai", home / "plx-state-copy"
        remote.SUDO_PATHS = tuple(str(fake) + p for p in saved["SUDO_PATHS"])
        for p in ("/data/scratch/in", "/data/scratch/out"):
            (home / "plx-run" / "files" / p.lstrip("/")).mkdir(parents=True)
        binds = remote.mounts({"state": "copy", "ship": ["/data/scratch/in"],
                               "out": ["/data/scratch/out"], "name": "t"}, home / "wt")
        # only the out folder, the run's own /tmp, the Claude homes and the state copy (the
        # Spark's replica) are writable; never the checkout or a shipped path
        self.assertEqual(sorted(dst for _, dst, ro in binds if not ro),
                         ["/data/plx-state", "/data/scratch/out", "/tmp",
                          "/var/lib/plx-autopilot/claude-home", "/var/lib/plx-workbench"])
        tmp = next(src for src, dst, _ in binds if dst == "/tmp")
        self.assertTrue(tmp.startswith(str(home / "plx-run" / "tmp" / "t-")))
        # the Spark's own folders go in read-only
        args = remote.bwrap_args([(str(home), "/nonexistent-x/y", False)], root=str(fake))
        self.assertIn(["--ro-bind", str(fake / "usr"), str(fake / "usr")],
                      [args[i:i + 3] for i in range(len(args))])
        self.assertNotIn("--bind", args[:-4])
        shim = str(home / "plx-ai" / "shim" / "sudo")
        over = {dst for src, dst, ro in binds if src == shim and ro}
        self.assertEqual(over, {str(fake) + "/usr/bin/sudo", str(fake) + "/usr/local/sbin/sudo"})
        # the shim drops sudo's options and runs the command as the sandbox's own user
        got = subprocess.run(["sh", shim, "-n", "-u", "plx-workbench", "-g", "plx-workbench",
                              "env", "-i", "HOME=/x", "sh", "-c", "echo $HOME; id -u"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(got.stdout.split(), ["/x", str(os.getuid())])


class ShipRefusals(unittest.TestCase):
    def test_state_secrets_and_code_are_never_shipped(self):
        env = {k: v for k, v in os.environ.items() if not spec.SECRET.search(k)}
        env["PLX_SPARK_SSH_CONFIG"] = "/nonexistent"
        home = "/home/" + "operator"   # a fixture user's home: any /home/<user> path is refused
        for path in ("/data/plx-state/app-sessions.json", "/data/plx-state", "/etc/plx",
                     f"{home}/.ssh", f"{home}/.claude/.credentials.json",
                     f"{home}/.config/plx/workbench-session.env", "/root/x",
                     "/data/OntologyProject/operating", "/var/lib/plx-workbench",
                     "/data/scratch/../plx-state/x"):
            got = subprocess.run(["bash", str(ROOT / "deploy/spark/spark_run.sh"), "--ship",
                                  path, "--", "true"], cwd=ROOT, env=env,
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(got.returncode, 2, (path, got.stderr))
            self.assertIn("refused", got.stderr, path)
        got = subprocess.run(["bash", str(ROOT / "deploy/spark/spark_run.sh"), "--state",
                              "all", "--", "true"], cwd=ROOT, env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(got.returncode, 2)


class LoginPreflight(unittest.TestCase):
    """A Spark whose Claude login is missing or expired stops the run at its start (exit 95)
    with the reason, instead of every Claude call answering "Not logged in"."""

    def run_with(self, expires_in, skip=False):
        import json
        import time
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(home)]))
        for d in ("plx-workbench", "autopilot"):
            cred = home / "plx-ai/home" / d / ".claude/.credentials.json"
            cred.parent.mkdir(parents=True)
            if expires_in is not None:
                cred.write_text(json.dumps({"claudeAiOauth": {
                    "accessToken": "x", "expiresAt": (time.time() + expires_in) * 1000}}))
        # a stand-in ssh that runs the remote python here (HOME = the fake Spark home) and
        # fails everything else (the push after the check)
        bin_ = home / "bin"
        bin_.mkdir()
        (bin_ / "ssh").write_text(
            '#!/bin/sh\nfor a in "$@"; do case "$a" in true) exit 0;; python3) exec python3 -;; esac; '
            'done\nexit 1\n')
        (bin_ / "ssh").chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not spec.SECRET.search(k)}
        env.update(PATH=f"{bin_}:{env['PATH']}", HOME=str(home), PLX_SPARK_SSH_CONFIG="/dev/null")
        if skip:
            env["SPARK_SKIP_LOGIN_CHECK"] = "1"
        return subprocess.run(["bash", str(ROOT / "deploy/spark/spark_run.sh"), "--", "true"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)

    def test_missing_or_expired_login_stops_the_run_a_valid_one_goes_on(self):
        for left in (None, -50, 30):
            got = self.run_with(left)
            self.assertEqual(got.returncode, 95, (left, got.stderr))
            self.assertIn("Claude login", got.stderr)
        self.assertEqual(self.run_with(3600).returncode, 97)   # past the check: the push fails
        self.assertEqual(self.run_with(None, skip=True).returncode, 97)


class StandbyChoice(unittest.TestCase):
    """``--pg standby`` reads the Spark's hot standby only when its guard passes; else, or
    when the guard fails, the box, as every run without it does."""

    def setUp(self):
        saved = remote.AI
        self.addCleanup(setattr, remote, "AI", saved)
        remote.AI = Path("/home/x/plx-ai")

    def test_the_box_unless_asked_and_the_guard_passes(self):
        box = Path("/home/x/plx-ai/run")
        self.assertEqual(remote.database({"state": "copy"}), (box, {}))
        self.assertEqual(remote.database({"pg": "box"}, lambda: (True, "")), (box, {}))
        self.assertEqual(remote.database({"pg": "standby"}, lambda: (False, "lag")), (box, {}))

        def broken():
            raise OSError("no psql")
        self.assertEqual(remote.database({"pg": "standby"}, broken), (box, {}))
        pg, env = remote.database({"pg": "standby"}, lambda: (True, "caught up"))
        self.assertEqual(pg, Path("/home/x/plx-ai/standby-run"))
        self.assertEqual(env, {"PLX_PG_KEYSET_DSN": "host=/run/plx-primary dbname=plx user=plx_eval"})

    def test_the_guard_reads_the_box_on_lag_a_lost_slot_or_a_promoted_standby(self):
        import standby_guard as guard
        box = ["0/3000000", "reserved"]
        self.assertTrue(guard.choose(["t", "0/3000000", "900"], box, 600)[0])   # caught up
        self.assertTrue(guard.choose(["t", "0/2000000", "30"], box, 600)[0])
        self.assertFalse(guard.choose(["t", "0/2000000", "900"], box, 600)[0])
        self.assertFalse(guard.choose(["t", "0/3000000", "1"], ["0/3000000", "lost"], 600)[0])
        self.assertFalse(guard.choose(["t", "0/3000000", "1"], ["0/3000000", ""], 600)[0])
        self.assertFalse(guard.choose(["f", "0/3000000", "1"], box, 600)[0])
        self.assertEqual(guard.pick(psql="/nonexistent/psql")[0], False)


if __name__ == "__main__":
    unittest.main()
