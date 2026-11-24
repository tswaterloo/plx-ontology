"""deploy/ops/host/tls_files.py: Caddy's certificate from files, reloaded only when it changed,
never a certificate that is wrong for the host or about to expire."""

from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy" / "ops" / "host"))
import tls_files  # noqa: E402

HOST = "box.example.ts.net"


def make_cert(d: Path, name: str, days: int, tag: str) -> tuple[Path, Path]:
    cert, key = d / f"{tag}.crt", d / f"{tag}.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                    "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key), "-out",
                    str(cert), "-days", str(days), "-subj", f"/CN={name}",
                    "-addext", f"subjectAltName=DNS:{name}"],
                   check=True, capture_output=True)
    return cert, key


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class TlsFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.t = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t)
        self.bin = self.t / "bin"
        self.bin.mkdir()
        self.calls = self.t / "calls"
        # systemctl: records each call
        (self.bin / "systemctl").write_text(f"#!/bin/sh\necho \"$@\" >> {self.calls}\n")
        (self.bin / "systemctl").chmod(0o755)
        self.serve(*make_cert(self.t, HOST, 90, "good"))
        self.addCleanup(os.environ.__setitem__, "PATH", os.environ["PATH"])
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        self.dir, self.snippet = self.t / "caddy" / "tls", self.t / "etc" / "files.caddy"

    def serve(self, cert: Path, key: Path, fail: bool = False) -> None:
        """A fake ``tailscale cert`` writing ``cert``/``key`` (or failing)."""
        body = ("#!/bin/sh\necho 'no cert' >&2\nexit 1\n" if fail else
                f"#!/bin/sh\ncp {cert} \"$3\"\ncp {key} \"$5\"\n")
        (self.bin / "tailscale").write_text(body)
        (self.bin / "tailscale").chmod(0o755)

    def go(self) -> int:
        return tls_files.main(["--host", HOST, "--dir", str(self.dir), "--snippet",
                               str(self.snippet), "--owner", pwd.getpwuid(os.getuid()).pw_name,
                               "--tailscale", str(self.bin / "tailscale")])

    def reloads(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_installs_once_and_reloads_only_on_a_change(self) -> None:
        self.assertEqual(self.go(), 0)
        key = self.dir / "key.pem"
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(self.snippet.read_text().splitlines()[-1],
                         f"tls {self.dir / 'cert.pem'} {key}")
        self.assertEqual(self.reloads(), 1)
        self.assertEqual(self.go(), 0)            # the same certificate: no reload
        self.assertEqual(self.reloads(), 1)
        self.serve(*make_cert(self.t, HOST, 90, "renewed"))
        self.assertEqual(self.go(), 0)            # renewed: installed and reloaded
        self.assertEqual(self.reloads(), 2)
        self.assertEqual((self.dir / "cert.pem").read_bytes(),
                         (self.t / "renewed.crt").read_bytes())

    def test_never_installs_a_wrong_certificate(self) -> None:
        cases = {
            "other host": make_cert(self.t, "other.example.ts.net", 90, "other"),
            "expiring": make_cert(self.t, HOST, 2, "short"),
            "foreign key": ((self.t / "good.crt"), make_cert(self.t, HOST, 90, "k2")[1]),
        }
        for why, (cert, key) in cases.items():
            with self.subTest(why):
                self.serve(cert, key)
                self.assertEqual(self.go(), 1)
                self.assertFalse(self.snippet.exists())
                self.assertFalse((self.dir / "cert.pem").exists())
                self.assertEqual(self.reloads(), 0)

    def test_back_to_tailscaled_when_the_copy_is_about_to_expire(self) -> None:
        self.serve(*make_cert(self.t, HOST, 30, "month"))
        self.assertEqual(self.go(), 0)
        # tailscaled cannot give one now; the installed one still has 30 days: kept
        self.serve(self.t / "good.crt", self.t / "good.key", fail=True)
        self.assertEqual(self.go(), 1)
        self.assertTrue(self.snippet.exists())
        self.assertEqual(self.reloads(), 1)
        # the installed one is good for under the margin: the snippet goes, Caddy reloads
        self.assertEqual(tls_files.main(
            ["--host", HOST, "--dir", str(self.dir), "--snippet", str(self.snippet),
             "--owner", pwd.getpwuid(os.getuid()).pw_name, "--min-days", "45",
             "--tailscale", str(self.bin / "tailscale")]), 1)
        self.assertFalse(self.snippet.exists())
        self.assertEqual(self.reloads(), 2)

    def test_refuses_a_name_that_is_not_a_tailscale_host(self) -> None:
        for bad in ("example.com", "x.ts.net/../../etc", "a b.ts.net"):
            self.assertEqual(tls_files.main(["--host", bad, "--dir", str(self.dir),
                                             "--snippet", str(self.snippet)]), 2)
        self.assertEqual(self.reloads(), 0)


if __name__ == "__main__":
    unittest.main()
