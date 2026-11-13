"""OPS-003: the plain config archive holds an allowlist, never a file the name screen missed."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
