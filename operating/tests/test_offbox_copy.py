"""deploy/backup/offbox_copy.py end to end, with each "Spark" a local directory behind a fake
ssh/rsync (the real gpg, rsync and sha256sum run): a copy is green only after the Spark's
sha256 matches, holds ciphertext only under a 0700 directory, is sent once (incremental), a
tampered or missing object is never green and is re-sent, an incomplete or changing source,
a short Spark, an unsafe key or remote directory fail closed, a dead kept Spark fails the
run (never a switch), a data host holding the escrow key is refused, list files are checked
against the manifest's digest, retention keeps N records, and the restore test decrypts what
the Spark holds, falling back to an older record when the newest does not pass."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "backup" / "offbox_copy.py"

FAKE_SSH = r'''#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
for i, a in enumerate(args[:-1]):  # like ssh: an option flag needs a real value
    if a in ("-F", "-o") and args[i + 1].startswith("-"):
        sys.stderr.write('command-line line 0: no argument after keyword "%s"\n' % a); sys.exit(255)
while args and args[0].startswith("-"):
    args = args[2:] if args[0] in ("-F", "-o", "-p", "-i", "-J") else args[1:]
host, cmd = args[0], " ".join(args[1:])
root = os.environ["FAKE_SPARKS"]
if os.path.exists(os.path.join(root, host + ".down")):
    sys.stderr.write("ssh: connect to host %s: timed out\n" % host); sys.exit(255)
home = os.path.join(root, host)
os.makedirs(home, exist_ok=True)
env = dict(os.environ, HOME=home)
sys.exit(subprocess.run(["bash", "-c", cmd], cwd=home, env=env).returncode)
'''

FAKE_RSYNC = r'''#!/usr/bin/env python3
import os, re, subprocess, sys, time
root = os.environ["FAKE_SPARKS"]
out, args, eopt = [], sys.argv[1:], ""
i = 0
while i < len(args):
    a = args[i]
    if a == "-e":
        eopt = args[i + 1]; i += 2; continue
    m = re.match(r"^([A-Za-z0-9_-]+):(.*)$", a)
    if m:
        if os.path.exists(os.path.join(root, m.group(1) + ".down")):
            sys.exit(255)
        a = os.path.join(root, m.group(1), m.group(2))
    out.append(a); i += 1
names = sys.stdin.buffer.read()
fail = os.environ.get("FAKE_FAIL_ONCE")
if fail and os.path.exists(fail):  # one dropped connection
    os.unlink(fail); sys.stderr.write("connection closed\n"); sys.exit(12)
hook = os.environ.get("FAKE_HOOK_SRC")
if hook and os.path.exists(hook):  # something lands on the box while the copy runs
    os.rename(hook, os.environ["FAKE_HOOK_DST"])
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write("ARGS %s\n" % "|".join(a for a in out if a.startswith("--bwlimit")))
        fh.write("START %f %s %s\n" % (time.time(), eopt.replace(" ", "|"),
                                       ",".join(names.decode().split())))
    time.sleep(float(os.environ.get("FAKE_RSYNC_SLEEP", "0")))
meet = os.environ.get("FAKE_MEET")
if meet:  # hold this upload (bounded) until another upload has started beside it
    os.makedirs(meet, exist_ok=True)
    open(os.path.join(meet, str(os.getpid())), "w").close()
    deadline = time.time() + float(os.environ.get("FAKE_MEET_WAIT", "30"))
    while len(os.listdir(meet)) < 2 and time.time() < deadline:
        time.sleep(0.05)
rc = subprocess.run(["rsync"] + out, input=names).returncode
if log:
    with open(log, "a") as fh:
        fh.write("END %f\n" % time.time())
sys.exit(rc)
'''


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(shutil.which("gpg") and shutil.which("rsync") and shutil.which("sha256sum"),
                     "needs gpg, rsync, sha256sum")
class OffboxCopyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plx-offbox-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sparks = self.tmp / "sparks"
        self.sparks.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, text in (("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.backups = self.tmp / "backups" / "daily"
        self.capture = self.tmp / "capture"
        self.secret = b"PLAINTEXT-MARKER-" + os.urandom(8).hex().encode()
        self.make_pg_set("20261115T081500Z")
        self.make_capture(blobs=60)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("PLX_", "AZURE_", "PG", "AWS_"))}
        self.env.update(
            HOME=str(self.tmp / "home"), FAKE_SPARKS=str(self.sparks),
            PLX_OFFBOX_SSH_BIN=str(bin_dir / "ssh"), PLX_OFFBOX_RSYNC_BIN=str(bin_dir / "rsync"),
            PLX_OFFBOX_HOSTS="sparkA sparkB", PLX_OFFBOX_SSH_CONFIG="/dev/null",
            PLX_OFFBOX_KEY=str(self.tmp / "home" / "key"), PLX_OFFBOX_MIN_FREE_GB="0",
            PLX_OFFBOX_STAGE=str(self.tmp / "stage"), PLX_OFFBOX_BATCH_MB="0.05",
            PLX_BACKUP_DIR=str(self.backups.parent), PLX_CAPTURE_BACKUP_DIR=str(self.capture),
            PLX_OFFBOX_STATE=str(self.tmp / "state"), PLX_OFFBOX_STAGGER="0",
            PLX_OFFBOX_ESCROW_HOSTS="sparkE")
        self.assertEqual(self.run_it("init-key")[0], 0)
        # the escrow host holds the key and the secrets passphrase (never a data host)
        self.passphrase = self.tmp / "home" / "secrets.pass"
        self.passphrase.write_bytes(os.urandom(24).hex().encode() + b"\n")
        self.passphrase.chmod(0o600)
        self.escrow = self.sparks / "sparkE" / ".config" / "plx"
        self.escrow.mkdir(parents=True, mode=0o700)
        for src, name in ((Path(self.env["PLX_OFFBOX_KEY"]), "offbox.key"),
                          (self.passphrase, "backup-secrets.pass")):
            shutil.copy(src, self.escrow / name)
            (self.escrow / name).chmod(0o600)

    # -------------------------------------------------------------- fixtures
    def make_pg_set(self, stamp: str, secrets: bool = False) -> None:
        self.backups.mkdir(parents=True, exist_ok=True)
        files = {}
        if secrets:  # like plx_backup.py: gpg --symmetric with the passphrase file
            subprocess.run(["gpg", "--homedir", str(self.tmp / "gnupg-test"), "--batch", "--yes",
                            "--quiet", "--symmetric", "--cipher-algo", "AES256",
                            "--pinentry-mode", "loopback", "--passphrase-file",
                            str(self.passphrase), "-o", str(self.backups / f"secrets-{stamp}.tar.gpg")],
                           input=self.secret + b" secret files", check=True, capture_output=True)
        for name in (f"plx-{stamp}.dump", f"state-{stamp}.tar.gz", f"config-{stamp}.tar.gz"):
            data = self.secret + os.urandom(3000)
            (self.backups / name).write_bytes(data)
            files[name] = {"bytes": len(data), "sha256": _sha(data)}
        (self.backups / f"manifest-{stamp}.json").write_text(json.dumps(
            {"format": "plx-backup/v2", "stamp": stamp, "files": files, "pg_problem": None,
             "state_problems": []}))

    def make_capture(self, blobs: int, complete: bool = True) -> None:
        pieces = {}
        rels = ["db/mail-capture.sqlite.zst", "files.tar.zst"] + [
            f"blobs/mail-{i:02x}.tar.zst" for i in range(blobs)]
        for rel in rels:
            path = self.capture / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            data = self.secret + rel.encode() + os.urandom(500)
            path.write_bytes(data)
            names = f"{rel}/a\n{rel}/b\n".encode()
            pieces[rel] = {"bytes": len(data), "sha256": _sha(data), "listing": _sha(names)}
            lst = self.capture / "lists" / (rel.replace("/", "_") + ".gz")
            lst.parent.mkdir(parents=True, exist_ok=True)
            lst.write_bytes(gzip.compress(names))
        self.write_capture_manifest(pieces, complete)

    def write_capture_manifest(self, pieces: dict, complete: bool = True) -> None:
        (self.capture / "manifest.json").write_text(json.dumps(
            {"format": "plx-capture-backup/v1", "complete": complete, "pieces": pieces,
             "skipped": {}, "completed_at": "2026-11-15T19:00:00+00:00"}))

    def run_it(self, cmd: str, **env: str) -> tuple[int, dict]:
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), cmd], capture_output=True,
                                env=dict(self.env, **env), timeout=300)
        try:
            out = json.loads(result.stdout.decode().strip().splitlines()[-1])
        except (IndexError, ValueError):
            self.fail(f"no JSON from {cmd}: {result.stdout!r} {result.stderr!r}")
        return result.returncode, out

    def remote(self, host: str = "sparkA") -> Path:
        return self.sparks / host / "plx-offbox"

    def objects(self, host: str = "sparkA") -> list[Path]:
        return sorted((self.remote(host) / "objects").glob("*.gpg"))

    # -------------------------------------------------------------- tests
    def test_green_copy_is_private_ciphertext_incremental_and_restores(self) -> None:
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"], out["problems"]), (0, "green", []), out)
        # 3 pg files + manifest, capture manifest, 62 pieces, 62 lists: none lost or doubled
        self.assertEqual(out["files"], 4 + 1 + 62 + 62)
        self.assertEqual(out["files_copied"], out["files"])
        self.assertEqual(out["host"], "sparkA")
        root = self.remote()
        self.assertEqual(len(list((root / "copies").glob("*.json.gpg"))), 1)
        self.assertEqual(len(list((root / "incoming").iterdir())), 0)
        for path in [root, *root.rglob("*")]:
            mode = path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o700 if path.is_dir() else 0o600, path)
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(self.secret, data, path)
                self.assertNotIn(b"mail-capture", data, path)
        self.assertEqual(list((self.tmp / "stage").iterdir()), [])
        # second run: nothing new is sent, every object verified again, still green
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"], out["sent_objects"]), (0, "green", 0), out)
        code, out = self.run_it("restore-test")
        self.assertEqual((code, out["status"], out["problems"]), (0, "green", []), out)
        self.assertGreaterEqual(out["checked"], 6)

    def test_tampered_object_is_never_green_and_is_sent_again(self) -> None:
        self.assertEqual(self.run_it("copy")[0], 0)
        victim = self.objects()[5]
        data = bytearray(victim.read_bytes())
        data[-20] ^= 0xFF  # same size: only a checksum can tell
        victim.write_bytes(bytes(data))
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (3, "incomplete"), out)
        self.assertTrue(any(p.startswith("remote_verify_failed:") for p in out["problems"]), out)
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (0, "green"), out)
        self.assertEqual(out["sent_objects"], 1)
        # a corrupted object makes the restore test fail, not pass (a Postgres-set object: the
        # restore test always checks the whole set, the capture pieces only by sample)
        manifest = json.loads((self.backups / "manifest-20261115T081500Z.json").read_text())
        dump = next(v for k, v in manifest["files"].items() if k.endswith(".dump"))
        victim = self.remote() / "objects" / (self.object_name(dump["sha256"]) + ".gpg")
        self.assertTrue(victim.is_file())
        victim.write_bytes(b"garbage")
        code, out = self.run_it("restore-test")
        self.assertNotEqual(code, 0, out)
        self.assertNotEqual(out["status"], "green", out)

    def test_incomplete_or_changing_source_is_not_green(self) -> None:
        manifest = json.loads((self.capture / "manifest.json").read_text())
        self.write_capture_manifest(manifest["pieces"], complete=False)
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (3, "incomplete"), out)
        self.assertIn("capture_backup_incomplete", out["problems"])
        # a piece already on the Spark under its manifest digest is not read again (the Spark
        # holds what the manifest names); a piece not yet sent is read and must match
        pieces = dict(manifest["pieces"])
        new = self.capture / "blobs" / "mail-new.tar.zst"
        pieces["blobs/mail-new.tar.zst"] = {"bytes": 9, "sha256": _sha(b"as listed")}
        new.write_bytes(b"rewritten under the copy")
        self.write_capture_manifest(pieces, complete=True)
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (3, "incomplete"), out)
        self.assertIn("source_changed:capture/blobs/mail-new.tar.zst", out["problems"])
        self.assertIn("capture/blobs/mail-new.tar.zst", out.get("missing", []) or
                      json.loads((self.tmp / "state" / "records" / f"{out['stamp']}.json")
                                 .read_text())["missing"])
        new.unlink()
        code, out = self.run_it("copy")
        self.assertIn("source_missing:capture/blobs/mail-new.tar.zst", out["problems"])
        self.assertEqual(code, 3)

    def test_short_spark_unsafe_key_or_directory_fail_closed(self) -> None:
        code, out = self.run_it("copy", PLX_OFFBOX_MIN_FREE_GB="1000000000")
        self.assertEqual((code, out["status"]), (1, "failed"), out)
        self.assertIn("spark_space", out["error"])
        self.assertEqual(self.objects(), [])
        os.chmod(self.env["PLX_OFFBOX_KEY"], 0o644)
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (1, "failed"))
        self.assertIn("key_unsafe", out["error"])
        os.chmod(self.env["PLX_OFFBOX_KEY"], 0o600)
        stray = self.remote() / "objects" / "readable.txt"
        stray.write_text("x")
        stray.chmod(0o644)  # e.g. an interrupted transfer's leftover: made private first
        code, out = self.run_it("copy")
        self.assertEqual(code, 0, out)
        self.assertEqual(stray.stat().st_mode & 0o777, 0o600)
        stray.unlink()
        # both fake Sparks share one disk: the first copy took whichever showed more free
        # bytes, and later runs keep that host, so the unsafe entry goes on that one
        host = out["host"]
        link = self.remote(host) / "objects" / "elsewhere"
        link.symlink_to(self.tmp)
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (1, "failed"), out)
        self.assertIn("not_private", out["error"])
        link.unlink()
        shutil.rmtree(self.remote(host))
        (self.tmp / "elsewhere").mkdir()
        self.remote(host).symlink_to(self.tmp / "elsewhere")
        code, out = self.run_it("copy")
        self.assertEqual(code, 1, out)
        self.assertEqual(list((self.tmp / "elsewhere").iterdir()), [])

    def test_dead_kept_spark_fails_and_never_switches(self) -> None:
        (self.sparks / "sparkA.down").write_text("")
        (self.sparks / "sparkB.down").write_text("")
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (1, "failed"))
        self.assertIn("no_spark_reachable", out["error"])
        (self.sparks / "sparkB.down").unlink()
        code, out = self.run_it("copy")  # the first copy: the one that answers
        self.assertEqual((code, out["host"]), (0, "sparkB"), out)
        (self.sparks / "sparkA.down").unlink()
        (self.sparks / "sparkB.down").write_text("")
        # the kept Spark is unreachable: the run fails (alarm) and nothing goes to sparkA
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (1, "failed"), out)
        self.assertTrue(out["error"].startswith("kept_host_unreachable:ssh_failed:sparkB:255"), out)
        self.assertEqual(self.objects("sparkA"), [])
        self.assertFalse((self.remote("sparkA") / "copies").exists())
        state = json.loads((self.tmp / "state" / "state.json").read_text())
        self.assertEqual(state["host"], "sparkB")
        (self.sparks / "sparkB.down").unlink()
        code, out = self.run_it("copy")
        self.assertEqual((code, out["host"], out["sent_objects"]), (0, "sparkB", 0), out)
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), "upload-everything"],
                                capture_output=True, env=self.env)
        self.assertEqual(result.returncode, 2)

    def test_a_data_host_holding_the_escrow_key_is_refused(self) -> None:
        # listed as both a data host and an escrow host: refused before any connection
        code, out = self.run_it("copy", PLX_OFFBOX_ESCROW_HOSTS="sparkX sparkB")
        self.assertEqual((code, out["status"], out["error"]),
                         (1, "failed", "data_host_is_escrow_host:sparkB"), out)
        self.assertFalse(self.sparks.joinpath("sparkA").exists())
        # the escrow key found on the first copy's host: nothing is uploaded anywhere
        escrow = self.sparks / "sparkA" / ".config" / "plx" / "offbox.key"
        escrow.parent.mkdir(parents=True)
        escrow.write_text("x")
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"], out["error"]),
                         (1, "failed", "escrow_key_on_data_host:sparkA"), out)
        self.assertEqual(self.objects("sparkA") + self.objects("sparkB"), [])
        self.assertFalse(self.remote("sparkA").exists())
        escrow.unlink()
        code, out = self.run_it("copy", PLX_OFFBOX_HOSTS="sparkA")
        self.assertEqual((code, out["host"]), (0, "sparkA"), out)
        # the key copied to the other configured host while sparkA is kept: refused too
        other = self.sparks / "sparkB" / ".config" / "plx" / "offbox.key"
        other.parent.mkdir(parents=True)
        other.symlink_to("/nonexistent")  # a dangling link counts as held
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"], out["error"]),
                         (1, "failed", "escrow_key_on_data_host:sparkB"), out)
        other.unlink()
        # the escrow key appears on the kept host itself: copy and restore test refuse
        escrow.write_text("x")
        for cmd in ("copy", "restore-test"):
            code, out = self.run_it(cmd)
            self.assertEqual((code, out["error"]), (1, "escrow_key_on_data_host:sparkA"), out)

    def test_restore_test_opens_the_secrets_archive_with_the_escrowed_passphrase(self) -> None:
        (self.tmp / "gnupg-test").mkdir(mode=0o700)
        self.make_pg_set("20261115T091500Z", secrets=True)
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"], out["files"]), (0, "green", 5 + 1 + 62 + 62), out)
        code, out = self.run_it("restore-test")
        self.assertEqual((code, out["status"], out["problems"]), (0, "green", []), out)
        self.assertTrue(out["secrets_archive_opened"], out)
        self.assertEqual(out["escrow"]["sparkE"]["opened"], "daily/secrets-20261115T091500Z.tar.gpg")
        printed = json.dumps(out).encode()
        self.assertNotIn(self.passphrase.read_bytes().strip(), printed)
        self.assertNotIn(Path(self.env["PLX_OFFBOX_KEY"]).read_bytes().strip(), printed)
        # every way the escrow cannot serve a recovery fails the test (the alarm)
        good = (self.escrow / "backup-secrets.pass").read_bytes()
        cases = [
            (lambda: (self.escrow / "backup-secrets.pass").write_bytes(b"wrong\n"),
             "escrow_secrets_pass_does_not_open_archive:sparkE"),
            (lambda: (self.escrow / "backup-secrets.pass").chmod(0o644),
             "escrow_secrets_pass_unsafe:sparkE"),
            (lambda: (self.escrow / "offbox.key").write_bytes(b"x" * 44),
             "escrow_key_different:sparkE"),
            (lambda: (self.escrow / "offbox.key").unlink(), "escrow_key_missing:sparkE"),
        ]
        key = (self.escrow / "offbox.key").read_bytes()
        for spoil, problem in cases:
            spoil()
            code, out = self.run_it("restore-test")
            self.assertEqual((code, out["status"]), (1, "failed"), out)
            self.assertIn(problem, out["problems"])
            (self.escrow / "backup-secrets.pass").write_bytes(good)
            (self.escrow / "backup-secrets.pass").chmod(0o600)
            (self.escrow / "offbox.key").write_bytes(key)
            (self.escrow / "offbox.key").chmod(0o600)
        (self.sparks / "sparkE.down").write_text("")
        code, out = self.run_it("restore-test")
        self.assertEqual(code, 1, out)
        self.assertTrue(any(p.startswith("escrow_unreachable:") for p in out["problems"]), out)

    def test_list_files_are_checked_against_the_manifest_digest(self) -> None:
        manifest = json.loads((self.capture / "manifest.json").read_text())
        victim = self.capture / "lists" / "blobs_mail-03.tar.zst.gz"
        victim.write_bytes(gzip.compress(b"changed under the copy\n"))
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (3, "incomplete"), out)
        self.assertIn("source_changed:capture/lists/blobs_mail-03.tar.zst.gz", out["problems"])
        victim.write_bytes(b"not gzip")
        code, out = self.run_it("copy")
        self.assertIn("source_unreadable:capture/lists/blobs_mail-03.tar.zst.gz", out["problems"])
        # a list whose piece has no digest is left out (never sent unchecked), still green
        manifest["pieces"]["blobs/mail-03.tar.zst"]["listing"] = None
        self.write_capture_manifest(manifest["pieces"])
        (self.capture / "lists" / "orphan.gz").write_bytes(gzip.compress(b"x\n"))
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"], out["problems"]), (0, "green", []), out)
        self.assertEqual(out["capture"]["lists_unverifiable"], 2)
        self.assertEqual(out["files"], 4 + 1 + 62 + 61)

    def test_stale_partial_uploads_in_incoming_are_removed(self) -> None:
        self.assertEqual(self.run_it("copy")[0], 0)
        incoming = self.remote() / "incoming"
        old, fresh = incoming / ".abc.gpg.Xy12", incoming / "def.gpg"
        for path in (old, fresh):
            path.write_bytes(b"partial")
            path.chmod(0o600)
        aged = __import__("time").time() - 13 * 3600
        os.utime(old, (aged, aged))
        code, out = self.run_it("copy")
        self.assertEqual((code, out["incoming_removed"]), (0, 1), out)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_restore_test_falls_back_to_an_older_record(self) -> None:
        import time
        self.assertEqual(self.run_it("copy")[0], 0)
        first = sorted(p.name for p in (self.remote() / "copies").iterdir())
        time.sleep(1.1)
        manifest = json.loads((self.capture / "manifest.json").read_text())
        self.write_capture_manifest(manifest["pieces"], complete=False)
        self.assertEqual(self.run_it("copy")[0], 3)
        records = sorted(p.name for p in (self.remote() / "copies").iterdir())
        self.assertEqual(len(records), 2)
        # the newest record is incomplete: the older green one is tested and passes
        code, out = self.run_it("restore-test")
        self.assertEqual((code, out["status"], out["passed"], out["newest"]),
                         (0, "green", first[0], records[1]), out)
        self.assertEqual([t["status"] for t in out["tried"]], ["incomplete", "green"])
        # a corrupt newest record: the older one still passes, but the test fails (alarm)
        (self.remote() / "copies" / records[1]).write_bytes(b"garbage")
        code, out = self.run_it("restore-test")
        self.assertEqual((code, out["status"], out["passed"]), (1, "failed", first[0]), out)
        self.assertTrue(out["problems"][0].startswith(records[1] + ":record_unusable:"), out)

    def test_retention_keeps_newest_records_and_drops_unreferenced_objects(self) -> None:
        env = {"PLX_OFFBOX_KEEP": "2"}
        for stamp in ("20261116T081500Z", "20261117T081500Z", "20261118T081500Z"):
            self.assertEqual(self.run_it("copy", **env)[0], 0)
            self.make_pg_set(stamp)  # a newer nightly set each night
            import time
            time.sleep(1.1)  # record stamps are per second
        code, out = self.run_it("copy", **env)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(list((self.remote() / "copies").glob("*.json.gpg"))), 2)
        # objects: 2 kept nights' pg sets (4 each, the manifests differ) + shared capture
        self.assertEqual(len(self.objects()), 2 * 4 + 1 + 62 + 62)
        code, out = self.run_it("restore-test")
        self.assertEqual(code, 0, out)

    def test_a_second_run_while_one_holds_the_lock_fails(self) -> None:
        import fcntl
        state = self.tmp / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        with open(state / "run.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code, out = self.run_it("copy")
        self.assertEqual((code, out["status"], out["error"]), (1, "failed", "another_run_active"))
        self.assertFalse(self.remote().exists())

    def test_time_budget_resumes_without_resending(self) -> None:
        code, out = self.run_it("copy", PLX_OFFBOX_MAX_SECONDS="0.001")
        self.assertEqual((code, out["status"]), (3, "incomplete"), out)
        self.assertTrue(any(p.startswith("time_budget_reached") for p in out["problems"]))
        code, out = self.run_it("copy")
        self.assertEqual((code, out["status"]), (0, "green"), out)
        self.assertEqual(out["files_copied"], out["files"])

    def object_name(self, data_sha: str) -> str:
        import hmac
        key = (self.tmp / "home" / "key").read_bytes().strip()
        return hmac.new(key, b"plx-offbox-object:" + data_sha.encode(), hashlib.sha256).hexdigest()

    def calls(self, log: Path) -> list[tuple[float, float, str, list[str]]]:
        """(start, end, ssh command, names) of each object rsync, in start order."""
        out, starts = [], []
        for line in log.read_text().splitlines():
            parts = line.split(" ")
            if parts[0] == "START":
                starts.append((float(parts[1]), parts[2], parts[3].split(",") if parts[3:] else []))
            elif parts[0] == "END":
                # rsync calls overlap: pair each end with the earliest start still open
                start = starts.pop(0)
                out.append((start[0], float(parts[1]), start[1], start[2]))
        return sorted(out)

    def test_batches_go_out_on_parallel_separate_connections(self) -> None:
        log = self.tmp / "rsync.log"
        # the first upload waits (up to 60 s) for a second one to start: a sender that runs
        # its streams in parallel always overlaps them, however slowly a loaded machine
        # encrypts the next batch (main red 299cb43fac: a fixed 0.4 s upload under load
        # ended before the next batch was ready); a serial sender still shows peak 1
        code, out = self.run_it("copy", FAKE_LOG=str(log), FAKE_RSYNC_SLEEP="0.4",
                                FAKE_MEET=str(self.tmp / "meet"), FAKE_MEET_WAIT="60",
                                PLX_OFFBOX_STREAMS="4", PLX_OFFBOX_BATCH_MB="0.004",
                                PLX_OFFBOX_BWLIMIT_KBPS="20000")
        self.assertEqual((code, out["status"], out["files_copied"]), (0, "green", out["files"]), out)
        calls = [c for c in self.calls(log) if not any(n.endswith(".json.gpg") for n in c[3])]
        self.assertGreater(len(calls), 4)
        # the bandwidth cap is split over the streams: every upload carries its share
        bw = [line for line in log.read_text().splitlines() if line.startswith("ARGS")]
        self.assertEqual(set(bw), {"ARGS --bwlimit=5000"})
        # each stream is its own ssh connection, never the shared control socket (one flow)
        for _, _, ssh, _ in calls:
            self.assertIn("ControlPath=none", ssh)
            self.assertNotIn("ControlMaster=auto", ssh)
        peak = max(sum(1 for s2, e2, _, _ in calls if s2 <= s1 < e2) for s1, _, _, _ in calls)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        # every object sent exactly once across the streams: none lost or duplicated
        sent = [n for c in calls for n in c[3]]
        self.assertEqual(len(sent), len(set(sent)))
        self.assertEqual(len(sent), out["sent_objects"])
        self.assertEqual(len(self.objects()), out["sent_objects"])

    def test_newer_pg_set_landing_mid_run_is_sent_first_and_recorded(self) -> None:
        new = "20261115T234500Z"
        self.make_pg_set(new)
        held = self.tmp / "held-manifest.json"
        (self.backups / f"manifest-{new}.json").rename(held)  # lands at the first upload
        log = self.tmp / "rsync.log"
        code, out = self.run_it("copy", FAKE_LOG=str(log), FAKE_HOOK_SRC=str(held),
                                FAKE_HOOK_DST=str(self.backups / f"manifest-{new}.json"),
                                PLX_OFFBOX_PG_RESCAN_SECONDS="0.001", PLX_OFFBOX_STREAMS="2",
                                PLX_OFFBOX_BATCH_MB="0.004")
        self.assertEqual((code, out["status"], out["problems"]), (0, "green", []), out)
        self.assertEqual((out["pg_set"], out["pg_set_replaced"]), (new, ["20261115T081500Z"]))
        self.assertEqual(out["files"], 4 + 1 + 62 + 62)
        manifest = json.loads((self.backups / f"manifest-{new}.json").read_text())
        new_objs = {self.object_name(v["sha256"]) + ".gpg" for v in manifest["files"].values()}
        calls = [c for c in self.calls(log) if not any(n.endswith(".json.gpg") for n in c[3])]
        order = [n for c in calls for n in c[3]]
        self.assertTrue(new_objs <= set(order))
        # the new set went out ahead of capture pieces still queued when it appeared
        last_pg = max(order.index(n) for n in new_objs)
        blobs = {self.object_name(json.loads((self.capture / "manifest.json").read_text())
                                  ["pieces"][f"blobs/mail-{i:02x}.tar.zst"]["sha256"]) + ".gpg"
                 for i in range(60)}
        self.assertGreater(sum(1 for n in order[last_pg + 1:] if n in blobs), 30)
        code, rout = self.run_it("restore-test")
        self.assertEqual((code, rout["status"], rout["problems"]), (0, "green", []), rout)

    def test_a_dropped_stream_is_retried_and_still_verified(self) -> None:
        marker = self.tmp / "fail-once"
        marker.write_text("x")
        code, out = self.run_it("copy", FAKE_FAIL_ONCE=str(marker))
        self.assertFalse(marker.exists())
        self.assertEqual((code, out["status"], out["files_copied"]), (0, "green", out["files"]), out)
        marker.write_text("x")
        self.make_capture(blobs=61)  # one new piece to send; every stream fails for good
        code, out = self.run_it("copy", FAKE_FAIL_ONCE=str(marker), PLX_OFFBOX_RETRIES="0")
        self.assertEqual((code, out["status"]), (1, "failed"), out)
        self.assertTrue(out["error"].startswith("rsync_failed:sparkA:12"), out)
        self.assertEqual(list((self.tmp / "stage").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
