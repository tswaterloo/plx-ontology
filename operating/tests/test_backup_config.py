"""OPS-003: the plain config archive holds an allowlist, never a file the name screen missed."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "plx_backup_cfg", Path(__file__).resolve().parents[2] / "deploy" / "backup" / "plx_backup.py")
plx_backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plx_backup)

MARK = "audit-only-marker"


class PlainArchive(unittest.TestCase):
    def build(self, files: dict[str, str]):
        tmp = Path(tempfile.mkdtemp())
        root = tmp / "cfg"
        root.mkdir()
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        target = tmp / "config.tar.gz"
        out = plx_backup._config_tar([str(root)], target, tmp / "secrets.tar.gpg", None)
        with tarfile.open(target) as tar:
            names = {Path(m.name).name for m in tar.getmembers() if m.isfile()}
            body = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
        return out, names, body

    def test_unfamiliar_secret_spellings_never_enter_the_plain_archive(self):
        files = {
            "settings.json": json.dumps({"clientSecret": MARK, "accessToken": MARK}),
            "nested.json": json.dumps({"a": {"b": {"Credentials": {"x-api-key": MARK}}}}),
            "headers.json": json.dumps({"headers": {"Authorization": f"Bearer {MARK}12345"}}),
            "multi.json": '{\n "note": "x",\n "private":\n "' + MARK + '"\n}',
            "app.conf": f"password {MARK}\n",
            "odd.yaml": f"anything: {MARK}\n",
            "plx-ok.service": "[Service]\nExecStart=/bin/true\n",
            "Caddyfile": ":80 {\n respond \"hi\"\n}\n",
        }
        out, names, body = self.build(files)
        self.assertNotIn(MARK.encode(), body)
        self.assertEqual(names, {"plx-ok.service", "Caddyfile"})
        self.assertEqual({Path(f["path"]).name for f in out["secret_files"]},
                         {"settings.json", "nested.json", "headers.json", "multi.json",
                          "app.conf", "odd.yaml"})
        self.assertEqual(out["secrets"], "omitted")

    def test_a_unit_that_assigns_a_secret_is_held_back_too(self):
        _, names, body = self.build({
            "plx-a.service": f"[Service]\nEnvironment=\"clientSecret={MARK}\"\n",
            "plx-b.service": f"[Service]\nEnvironment=ACCESS_TOKEN={MARK}\n",
            "plx-c.timer": "[Timer]\nOnCalendar=daily\n"})
        self.assertEqual(names, {"plx-c.timer"})
        self.assertNotIn(MARK.encode(), body)

    def test_dropins_and_named_policy_files_stay_plain_but_are_still_screened(self):
        out, names, body = self.build({
            "plx-api.service.d/zz-mem.conf": "[Service]\nMemoryMax=8G\n",
            "plx-api.service.d/zz-secret.conf": f"[Service]\nEnvironment=API_TOKEN={MARK}\n",
            "workbench-host/host.json": '{"tenant": "plx"}',
            "workbench-host/vince_audience.json": '{"a": 1}',
            "workbench-host/employee_policy.json": f'{{"client_secret": "{MARK}"}}',
            "other/random.conf": "x=1\n",
            "other/random.json": "{}"})
        self.assertEqual(names, {"zz-mem.conf", "host.json", "vince_audience.json"})
        self.assertNotIn(MARK.encode(), body)
        self.assertEqual(sorted(Path(p).name for p in out["restore_files_missing"]),
                         ["employee_policy.json", "zz-secret.conf"])

    def test_with_a_passphrase_nothing_is_reported_missing(self):
        # the encrypted file set holds what the plain archive does not
        self.assertEqual(plx_backup._restore_needed(Path("a/plx-x.service.d/zz.conf")), True)
        self.assertEqual(plx_backup._restore_needed(Path("a/other/zz.conf")), False)

    def test_a_password_free_dsn_stays_plain_and_one_with_a_password_is_held_back(self):
        out, names, body = self.build({
            "plx-a.service": "[Service]\nEnvironment=PLX_PG_DSN=postgresql:///plx\n",
            "plx-b.service": "[Service]\nEnvironment=PLX_PG_DSN=postgresql://svc@db/plx\n",
            "plx-c.service": f"[Service]\nEnvironment=PLX_PG_DSN=postgresql://svc:{MARK}@db/plx\n",
            "plx-d.service": f"[Service]\nEnvironment=PLX_PG_DSN=host=db dbname=plx password={MARK}\n",
            "plx-e.service": f"[Service]\nEnvironment=PLX_PG_DSN=postgresql://db/plx?password={MARK}\n",
            "plx-f.service": f"[Service]\nEnvironment=PLX_PG_DSN=postgresql:///plx API_TOKEN={MARK}\n"})
        self.assertEqual(names, {"plx-a.service", "plx-b.service"})
        self.assertNotIn(MARK.encode(), body)
        self.assertEqual(out["restore_files_missing"],
                         sorted(str(Path(p)) for p in out["restore_files_missing"]))
        self.assertEqual(len(out["restore_files_missing"]), 4)


class RootStagedSecrets(unittest.TestCase):
    """The root pre-step keeps the 0600 root /etc/plx files closed: they reach the set only
    as an archive encrypted by root, listed by path."""

    def stage(self, age=0, listed="/etc/plx/live-session.env\n"):
        import os
        d = Path(tempfile.mkdtemp())
        (d / "root-secrets.tar.gpg").write_bytes(b"gpg")
        (d / "root-secrets.list").write_text(listed)
        if age:
            old = __import__("time").time() - age
            for f in d.iterdir():
                os.utime(f, (old, old))
        return d

    def test_fresh_own_archive_is_read_with_its_list(self):
        d = self.stage()
        arc, paths = plx_backup._root_staged(d)
        self.assertEqual((arc.name, paths), ("root-secrets.tar.gpg", {"/etc/plx/live-session.env"}))

    def test_stale_or_absent_archive_is_ignored(self):
        self.assertEqual(plx_backup._root_staged(self.stage(age=3 * 3600)), (None, set()))
        self.assertEqual(plx_backup._root_staged(Path(tempfile.mkdtemp())), (None, set()))

    def test_staged_unreadable_files_are_not_missing_and_ride_in_the_secrets_archive(self):
        import os
        from unittest import mock
        tmp = Path(tempfile.mkdtemp())
        cfg = tmp / "cfg"
        cfg.mkdir()
        (cfg / "plx-a.service").write_text("[Service]\nMemoryMax=1G\n")
        (cfg / "live-session.env").write_text("x\n")
        d = self.stage(listed=f"{cfg / 'live-session.env'}\n")
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"enc")
        with mock.patch.dict(os.environ, {"PLX_BACKUP_ROOT_SECRETS_DIR": str(d)}), \
                mock.patch.object(plx_backup, "_config_files",
                                  return_value=[cfg / "plx-a.service", cfg / "live-session.env"]), \
                mock.patch.object(plx_backup.os, "access", side_effect=lambda p, m: "live-session" not in str(p)), \
                mock.patch.object(plx_backup.subprocess, "run", fake_run):
            out = plx_backup._config_tar(["x"], tmp / "c.tar.gz", tmp / "s.gpg", "/pass")
        self.assertEqual(out["restore_files_missing"], [])
        self.assertEqual(out["root_staged"], [str(cfg / "live-session.env")])
        self.assertEqual(out["secrets"], "encrypted")
        self.assertEqual(len(calls), 1)

    def test_unstaged_unreadable_file_still_fails_the_run_without_secret_files(self):
        import os
        from unittest import mock
        tmp = Path(tempfile.mkdtemp())
        (tmp / "mint-readers.env").write_text("x\n")
        with mock.patch.dict(os.environ, {"PLX_BACKUP_ROOT_SECRETS_DIR": str(tmp / "none")}), \
                mock.patch.object(plx_backup, "_config_files", return_value=[tmp / "mint-readers.env"]), \
                mock.patch.object(plx_backup.os, "access", return_value=False):
            out = plx_backup._config_tar(["x"], tmp / "c.tar.gz", tmp / "s.gpg", None)
        self.assertEqual(out["restore_files_missing"], [str(tmp / "mint-readers.env")])

    def test_the_unit_runs_the_root_step_with_a_runtime_dir_and_only_named_files(self):
        root = Path(__file__).resolve().parents[2] / "deploy" / "backup"
        unit = (root / "plx-backup.service").read_text()
        self.assertIn("ExecStartPre=+@PLX_CURRENT@/deploy/backup/root_secrets.sh", unit)
        self.assertIn("RuntimeDirectory=plx-backup", unit)
        script = (root / "root_secrets.sh").read_text()
        self.assertIn("/etc/plx/live-session.env /etc/plx/mint-readers.env /etc/plx/api-model.env /etc/plx/backup-root.conf", script)
        self.assertNotIn("/etc/plx/*", script)
        self.assertNotIn("chmod", script)
        self.assertIn("/etc/plx/backup-root.conf", script)
        self.assertNotIn("PLX_BACKUP", script)        # the backup user's env is never read
        self.assertIn("mv -T", script)
        self.assertIn("| gpg", script)                 # tar piped into gpg, no plaintext file
        self.assertNotIn("in.tar", script)

    def test_passphrase_and_readable_secrets_but_no_staging_is_still_missing(self):
        import os
        from unittest import mock
        tmp = Path(tempfile.mkdtemp())
        cfg = tmp / "cfg"
        cfg.mkdir()
        (cfg / "other.env").write_text("TOKEN=x\n")             # a readable secret file
        (cfg / "live-session.env").write_text("x\n")            # unreadable, never staged
        def fake_run(cmd, **kw):
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"enc")
        with mock.patch.dict(os.environ, {"PLX_BACKUP_ROOT_SECRETS_DIR": str(tmp / "none")}), \
                mock.patch.object(plx_backup, "_config_files",
                                  return_value=[cfg / "other.env", cfg / "live-session.env"]), \
                mock.patch.object(plx_backup.os, "access", side_effect=lambda p, m: "live-session" not in str(p)), \
                mock.patch.object(plx_backup.subprocess, "run", fake_run):
            out = plx_backup._config_tar(["x"], tmp / "c.tar.gz", tmp / "s.gpg", "/pass")
        self.assertEqual(out["restore_files_missing"], [str(cfg / "live-session.env")])
        self.assertEqual(out["root_staged"], [])

    def test_stale_staging_with_a_passphrase_is_also_missing(self):
        import os
        from unittest import mock
        tmp = Path(tempfile.mkdtemp())
        (tmp / "live-session.env").write_text("x\n")
        d = self.stage(age=3 * 3600, listed=f"{tmp / 'live-session.env'}\n")
        with mock.patch.dict(os.environ, {"PLX_BACKUP_ROOT_SECRETS_DIR": str(d)}), \
                mock.patch.object(plx_backup, "_config_files", return_value=[tmp / "live-session.env"]), \
                mock.patch.object(plx_backup.os, "access", return_value=False):
            out = plx_backup._config_tar(["x"], tmp / "c.tar.gz", tmp / "s.gpg", "/pass")
        self.assertEqual(out["restore_files_missing"], [str(tmp / "live-session.env")])

    def test_restore_test_decrypts_the_nested_archive_and_checks_the_files(self):
        import io, os
        from unittest import mock
        def tar_of(members):
            b = io.BytesIO()
            with tarfile.open(fileobj=b, mode="w") as t:
                for name, data in members.items():
                    i = tarfile.TarInfo(name); i.size = len(data); t.addfile(i, io.BytesIO(data))
            return b.getvalue()
        inner_ok = tar_of({"etc/plx/live-session.env": b"x", "etc/plx/mint-readers.env": b""})
        outer = tar_of({"root-secrets.tar.gpg": b"NESTED"})
        def fake_run(cmd, input=None, **kw):
            return mock.Mock(stdout=outer if input is None else inner_ok)
        daily = Path(tempfile.mkdtemp())
        pw = daily / "pw"; pw.write_text("p")
        man = {"config": {"root_staged": ["/etc/plx/live-session.env", "/etc/plx/mint-readers.env"]}}
        with mock.patch.dict(os.environ, {"PLX_BACKUP_SECRETS_PASSPHRASE_FILE": str(pw)}), \
                mock.patch.object(plx_backup.subprocess, "run", fake_run):
            got = plx_backup._check_root_secrets(daily, "S", man)
        self.assertEqual(got, ["root_secrets_missing:/etc/plx/mint-readers.env"])   # empty file
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("PLX_BACKUP_SECRETS_PASSPHRASE_FILE", None)
            self.assertEqual(plx_backup._check_root_secrets(daily, "S", man), ["root_secrets_no_passphrase"])
        self.assertEqual(plx_backup._check_root_secrets(daily, "S", {"config": {"root_staged": []}}), [])


class RestoreTestRecord(unittest.TestCase):
    """The restore test leaves its result in the backup directory, so the alarms age it
    from the backups themselves (operating.ops_alarms.backup_checks)."""

    def test_the_result_is_recorded_pass_or_fail(self):
        import datetime as dt
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"PLX_BACKUP_DIR": tmp}):
                now = dt.datetime(2030, 1, 2, 9, 30, tzinfo=dt.timezone.utc)
                plx_backup.record_restore_test(0, {"stamp": "20300102T081500Z",
                                                   "tables": 12, "problems": []}, now)
                held = json.loads((Path(tmp) / "restore-test.json").read_text())
                self.assertEqual((held["at"], held["ok"], held["tables"], held["problems"]),
                                 ("2030-01-02T09:30:00Z", True, 12, 0))
                plx_backup.record_restore_test(1, {"error": "no_complete_backup_set"}, now)
                held = json.loads((Path(tmp) / "restore-test.json").read_text())
                self.assertEqual((held["ok"], held["error"]),
                                 (False, "no_complete_backup_set"))

class DumpSessionOptions(unittest.TestCase):
    def test_dump_env_overrides_timeouts_for_its_own_session_and_keeps_dsn_options(self):
        env = plx_backup._dump_env("postgresql:///plx?options=-c%20work_mem%3D64MB")
        opts = env["PGOPTIONS"]
        self.assertIn("idle_in_transaction_session_timeout=0", opts)
        self.assertIn("statement_timeout=0", opts)
        self.assertIn("work_mem=64MB", opts)

    def test_dump_env_ignores_an_inherited_pgoptions(self):
        with mock.patch.dict("os.environ", {"PGOPTIONS": "-c statement_timeout=5"}):
            opts = plx_backup._dump_env("postgresql:///plx")["PGOPTIONS"]
        self.assertNotIn("statement_timeout=5", opts)
        self.assertIn("statement_timeout=0", opts)

    def test_unit_is_not_idle_io_class(self):
        unit = (Path(plx_backup.__file__).parent / "plx-backup.service").read_text()
        self.assertNotIn("IOSchedulingClass=idle", unit)
        self.assertIn("IOSchedulingClass=best-effort", unit)


if __name__ == "__main__":
    unittest.main()
