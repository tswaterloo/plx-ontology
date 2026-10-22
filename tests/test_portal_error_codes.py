"""The Portal error-code vocabulary is generated from the Portal's own code at the pin
(``scripts/portal_error_codes.py``): no free text, test sample or file outside
``portal/src`` enters it, and ``--check`` fails when the committed list is not that
generation (edited by hand, or made at another commit: the pin moved)."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import portal_error_codes as pec

PORTAL = "plx-customer-portal"
ROUTE = '''import { NextResponse } from "next/server";
import { FG_FROZEN, GIT_ZONE_CODE } from "@/lib/freeze";
export type SoErrorCode =
  | "ORDER_NOT_EDITABLE"
  | "SHIP_TO_MISMATCH";
export async function PATCH() {
  if (bad) return NextResponse.json({ error: "Invalid PO for jane_doe", code: "VALIDATION" });
  if (err.code === 'CONFLICT') return NextResponse.json({ error: { code: "NOT_FOUND" } });
  const where = "bc-tenant.example.invalid";
  const label = "PO_5501";
  if (frozen) return NextResponse.json({ error: "frozen", code: FG_FROZEN });
  const zone = await db.zone.findFirst({ where: { code: GIT_ZONE_CODE } });
  if (other) return NextResponse.json({ error: "x", code: NOT_DECLARED });
  return NextResponse.json({ error: "Sales order not found", code: "not_found" });
}
'''
# A self-named constant is a code; a constant holding a record key is not.
CONSTANTS = 'export const FG_FROZEN = "FG_FROZEN";\nexport const GIT_ZONE_CODE = "GIT";\n'
SAMPLE = 'it("refuses", () => expect(res).toMatchObject({ code: "ACME_CORP" }));\n'


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True, text=True).stdout.strip()


class PortalErrorCodesTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.up = self.tmp / PORTAL
        route = self.up / "portal/src/app/api/so/route.ts"
        route.parent.mkdir(parents=True)
        route.write_text(ROUTE, encoding="utf-8")
        (self.up / "portal/src/app/api/so/route.test.ts").write_text(SAMPLE, encoding="utf-8")
        (self.up / "portal/src/lib").mkdir()
        (self.up / "portal/src/lib/freeze.ts").write_text(CONSTANTS, encoding="utf-8")
        (self.up / "scripts").mkdir()
        (self.up / "scripts/seed.ts").write_text('const x = { code: "OUTSIDE_SRC" };\n')
        _git(self.up, "init", "-q")
        _git(self.up, "add", "-A")
        _git(self.up, "commit", "-qm", "one")
        self.out = self.tmp / "portal_error_codes.json"
        self.pins = self.tmp / "pins.json"
        self.pin(_git(self.up, "rev-parse", "HEAD"))

    def pin(self, commit: str) -> None:
        self.pins.write_text(json.dumps({"repo_pins": [
            {"repo": PORTAL, "commit": commit, "status": "resolved"}]}), encoding="utf-8")

    def run_cli(self, *args: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return pec.main([*args, "--checkout", str(self.up), "--pins", str(self.pins),
                             "--out", str(self.out)])

    def edit(self, **fields) -> None:
        self.out.write_text(pec.render({**json.loads(self.out.read_text()), **fields}))

    def test_generate_then_check_fails_on_any_difference_from_the_pin(self):
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(json.loads(self.out.read_text())["codes"],
                         ["CONFLICT", "FG_FROZEN", "NOT_FOUND", "ORDER_NOT_EDITABLE",
                          "SHIP_TO_MISMATCH", "VALIDATION"])
        # no free text, test sample, file outside src, record-key constant or undeclared name
        self.assertEqual(self.run_cli("--check"), 0)
        committed = self.out.read_text()
        self.edit(codes=sorted(json.loads(committed)["codes"] + ["PO_5501"]))
        self.assertEqual(self.run_cli("--check"), 1)   # edited by hand
        self.out.write_text(committed)
        route = self.up / "portal/src/app/api/so/route.ts"
        route.write_text(ROUTE.replace('code: "VALIDATION"', 'code: "INVALID_INPUT"'))
        _git(self.up, "commit", "-qam", "two")
        self.assertEqual(self.run_cli("--check"), 0)   # still the pinned commit's
        self.pin(_git(self.up, "rev-parse", "HEAD"))
        self.assertEqual(self.run_cli("--check"), 1)   # the pin moved: regenerate
        self.assertEqual(self.run_cli(), 0)
        self.assertIn("INVALID_INPUT", json.loads(self.out.read_text())["codes"])
        self.assertEqual(self.run_cli("--check"), 0)
        self.pin("0" * 40)   # the checkout lacks the pinned commit: not checked
        self.edit(commit="0" * 40)
        self.assertEqual(self.run_cli("--check"), 2)

    def test_the_committed_vocabulary_is_current_at_the_repo_pin(self):
        code, message = pec.check()
        if code == 2:
            self.skipTest(message)
        self.assertEqual(code, 0, message)


if __name__ == "__main__":
    unittest.main()
