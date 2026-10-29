"""The upstream drift check compares pinned file hashes with a checkout's HEAD.

Evidence label: ``isolated-fixture`` (temporary local git repos only, no network).
"""

from __future__ import annotations

import contextlib
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import unittest.mock

from scripts import check_upstream_drift as drift

REPO = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True, text=True).stdout.strip()


class DriftCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.up = self.tmp / "upstream"
        self.up.mkdir()
        _git(self.up, "init", "-q")
        (self.up / "a.ts").write_text("one\ntwo\n")
        (self.up / "b.ts").write_text("stable\n")
        (self.up / "c.ts").write_text("going\n")
        _git(self.up, "add", ".")
        _git(self.up, "commit", "-q", "-m", "c1")
        self.c1 = _git(self.up, "rev-parse", "HEAD")

    def pin(self, path: str, text: str, module: str = "operating/m.py", **extra) -> dict:
        return {"repo": "up", "path": path, "commit": self.c1, "status": "resolved",
                "sha256": sha256(text.encode()).hexdigest(), "workbench_module": module,
                "workbench_modules": [module], "contract": f"mirrors {path}", **extra}

    def pins(self) -> dict:
        return {"repos": {"up": {"default_checkout": str(self.up)}},
                "pins": [self.pin("a.ts", "one\ntwo\n", "operating/a_mirror.py"),
                         self.pin("b.ts", "stable\n"), self.pin("c.ts", "going\n")]}

    def run_main(self, pins: dict, *extra: str) -> tuple[int, str]:
        pin_file = self.tmp / "pins.json"
        pin_file.write_text(json.dumps(pins))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = drift.main(["--pins", str(pin_file), *extra])
        return code, out.getvalue()

    def test_no_drift_exits_zero(self) -> None:
        code, out = self.run_main(self.pins())
        self.assertEqual(code, 0)
        self.assertIn("3 unchanged, 0 drifted", out)

    def test_changed_file_is_reported_with_stat_and_modules(self) -> None:
        (self.up / "a.ts").write_text("one\ntwo\nthree\n")
        _git(self.up, "commit", "-qam", "c2")
        code, out = self.run_main(self.pins())
        self.assertEqual(code, 1)
        self.assertIn("DRIFT changed up:a.ts", out)
        self.assertIn("1 insertion(+)", out)
        self.assertIn("operating/a_mirror.py", out)
        self.assertIn("mirrors a.ts", out)
        self.assertNotIn("up:b.ts", out)

    def test_committed_revert_to_pinned_content_is_not_drift(self) -> None:
        (self.up / "a.ts").write_text("other\n")
        _git(self.up, "commit", "-qam", "c2")
        (self.up / "a.ts").write_text("one\ntwo\n")
        _git(self.up, "commit", "-qam", "c3")
        self.assertEqual(self.run_main(self.pins())[0], 0)

    def test_uncommitted_edit_is_ignored_head_only(self) -> None:
        (self.up / "a.ts").write_text("edited, not committed\n")
        self.assertEqual(self.run_main(self.pins())[0], 0)

    def test_deleted_file_is_drift(self) -> None:
        _git(self.up, "rm", "-q", "c.ts")
        _git(self.up, "commit", "-qm", "c2")
        code, out = self.run_main(self.pins())
        self.assertEqual(code, 1)
        self.assertIn("DRIFT deleted up:c.ts", out)
        self.assertIn("file absent at HEAD", out)

    def test_missing_checkout_exits_two_and_is_listed(self) -> None:
        pins = self.pins()
        pins["repos"]["up"]["default_checkout"] = str(self.tmp / "nope")
        code, out = self.run_main(pins)
        self.assertEqual(code, 2)
        self.assertIn("SKIPPED up", out)
        self.assertIn("1 checkout(s) missing", out)

    def test_not_a_git_dir_counts_as_missing(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        pins = self.pins()
        pins["repos"]["up"]["default_checkout"] = str(plain)
        self.assertEqual(self.run_main(pins)[0], 2)

    def test_drift_in_one_repo_wins_over_missing_other_checkout(self) -> None:
        (self.up / "a.ts").write_text("changed\n")
        _git(self.up, "commit", "-qam", "c2")
        pins = self.pins()
        pins["pins"].append({**self.pin("z.ts", "z\n"), "repo": "gone"})
        pins["repos"]["gone"] = {"default_checkout": str(self.tmp / "nope")}
        code, out = self.run_main(pins)
        self.assertEqual(code, 1)
        self.assertIn("SKIPPED gone", out)

    def test_checkout_override_beats_pin_file_default(self) -> None:
        pins = self.pins()
        pins["repos"]["up"]["default_checkout"] = str(self.tmp / "nope")
        code, _ = self.run_main(pins, "--checkout", f"up={self.up}")
        self.assertEqual(code, 0)

    def test_env_checkout_is_used(self) -> None:
        pins = self.pins()
        pins["repos"]["up"]["default_checkout"] = str(self.tmp / "nope")
        with unittest.mock.patch.dict("os.environ", {"PLX_DRIFT_CHECKOUT_UP": str(self.up)}):
            self.assertEqual(self.run_main(pins)[0], 0)

    def test_unresolved_pins_are_listed_not_compared(self) -> None:
        pins = self.pins()
        pins["pins"].append({"repo": "up", "path": "x.ts", "commit": "deadbeef", "sha256": None,
                             "status": "unresolved", "reason": "commit not in checkout"})
        code, out = self.run_main(pins)
        self.assertEqual(code, 0)
        self.assertIn("1 unresolved", out)

    def test_json_report_lists_drift(self) -> None:
        (self.up / "b.ts").write_text("moved\n")
        _git(self.up, "commit", "-qam", "c2")
        code, out = self.run_main(self.pins(), "--json")
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual([d["path"] for d in report["drift"]], ["b.ts"])

    def test_check_is_read_only(self) -> None:
        (self.up / "a.ts").write_text("changed\n")
        _git(self.up, "commit", "-qam", "c2")
        before = _git(self.up, "status", "--porcelain=v1"), _git(self.up, "rev-parse", "HEAD")
        self.run_main(self.pins())
        self.assertEqual(before, (_git(self.up, "status", "--porcelain=v1"), _git(self.up, "rev-parse", "HEAD")))

    def test_align_moves_only_a_checkout_behind_its_repo_pin(self):
        # A session may attach an upstream checkout behind the pin: every pinned file then
        # reads as drift that is only the checkout's age (drift-1010-mc, 3 doc pins).
        (self.up / "a.ts").write_text("one\ntwo\nthree\n")
        _git(self.up, "commit", "-qam", "c2")
        c2 = _git(self.up, "rev-parse", "HEAD")
        pins = self.pins()
        pins["pins"][0] = {**self.pin("a.ts", "one\ntwo\nthree\n"), "commit": c2}
        pins["repo_pins"] = [{"repo": "up", "commit": c2}]
        _git(self.up, "checkout", "-q", self.c1)  # behind the pin
        self.assertEqual(self.run_main(pins)[0], 1)
        code, out = self.run_main(pins, "--align-lagging")
        self.assertEqual((code, out.split()[:3]), (0, ["ALIGN", "up", "moved_to_pin"]))
        self.assertEqual(_git(self.up, "rev-parse", "HEAD"), c2)
        self.assertEqual(self.run_main(pins)[0], 0)
        self.assertIn("at_pin", self.run_main(pins, "--align-lagging")[1])
        # Ahead of the pin is real upstream drift: left as it is, still reported.
        (self.up / "b.ts").write_text("changed\n")
        _git(self.up, "commit", "-qam", "c3")
        c3 = _git(self.up, "rev-parse", "HEAD")
        self.assertIn("skipped:not_behind_pin", self.run_main(pins, "--align-lagging")[1])
        self.assertEqual((_git(self.up, "rev-parse", "HEAD"), self.run_main(pins)[0]), (c3, 1))
        # Behind, but with local changes, or a pin the checkout lacks: never moved.
        _git(self.up, "checkout", "-q", self.c1)
        (self.up / "c.ts").write_text("local edit\n")
        self.assertIn("skipped:local_changes", self.run_main(pins, "--align-lagging")[1])
        _git(self.up, "checkout", "-q", "--", "c.ts")
        pins["repo_pins"] = [{"repo": "up", "commit": "f" * 40}]
        self.assertIn("skipped:pinned_commit_absent",
                      self.run_main(pins, "--align-lagging")[1])
        self.assertEqual(_git(self.up, "rev-parse", "HEAD"), self.c1)

# (pin path, module entry) -> why that module legitimately lacks the path's basename.
NAMED_EXCEPTIONS = {
    ("docs/projects/plx-sharepoint-site/README.md",
     "devloop/CONTEXT/06-LIVE-SOURCES.md (SharePoint site mapping; named by the drift-watch directive)"):
        "module entry is a prose note, not a file path",
    ("portal/prisma/schema.prisma", "tests/test_case_workspace_http.py"):
        "the test mirrors Prisma models by name, never by file name",
    ("portal/src/lib/project-development/overview-shared.ts", "operating/tests/test_portal_landed_engine.py"):
        "the test exercises landed_cost.py, which cites the file",
    ("portal/src/lib/inventory/line-tank-keys.ts", "operating/tests/test_portal_landed_engine.py"):
        "the test exercises landed_cost.py, which cites the file",
    ("portal/src/lib/costing/rate-masters.ts", "operating/tests/test_portal_landed_engine.py"):
        "the test exercises process_stage_map.json, which cites the file",
    ("portal/src/lib/auth/file-clearance.ts", "operating/file_clearance_check.py"):
        "the check enforces the rule; employee_permissions.py cites the file",
    ("src/lib/permissions/project-acl.ts", "ontology/project_mc.py"):
        "the ontology module applies the rule; integrations/mc_sources.py cites the file",
    ("portal/src/lib/fm-migration/fm-db.ts", "scripts/source_registry.py"):
        "the registry applies the SET search_path rule generically; ontology/source_registry.json names the file",
}
NAMED_EXCEPTIONS.update({
    (f"portal/src/app/api/mrp/{route}/route.ts", "operating/action_catalog.py"):
        "the catalog names the route by its URL, not the file name"
    for route in ("3pl/invoice", "sales-returns", "ap-misc", "ap-payments", "ap-settle",
                  "purchase-orders/[id]/vendor-invoice")})


def stale_module_entries(pins: list[dict], root: Path = REPO) -> list[str]:
    """Pin module entries that are not files or do not mention the pinned path's basename."""
    bad = []
    for pin in pins:
        base = pin["path"].rsplit("/", 1)[-1]
        for module in pin.get("workbench_modules") or [pin["workbench_module"]]:
            if (pin["path"], module) in NAMED_EXCEPTIONS:
                continue
            file = root / module
            if not file.is_file():
                bad.append(f"{pin['path']}: {module} is not a file")
            elif base not in file.read_text(encoding="utf-8", errors="replace"):
                bad.append(f"{pin['path']}: {module} does not mention {base}")
    return bad


class PinFileTests(unittest.TestCase):
    def test_committed_pin_file_is_well_formed(self) -> None:
        pins = json.loads((REPO / "devloop" / "upstream-pins.json").read_text())
        self.assertIn("_usage", pins)
        self.assertTrue(pins["pins"])
        for pin in pins["pins"]:
            for key in ("repo", "path", "commit", "workbench_module", "contract", "status"):
                self.assertIn(key, pin)
            if pin["status"] == "resolved":
                self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
            else:
                self.assertIsNone(pin["sha256"])
                self.assertTrue(pin.get("reason"))
        self.assertTrue(any(p["path"] == "docs/projects/plx-sharepoint-site/README.md"
                            for p in pins["pins"]))

    def test_pin_modules_still_name_their_upstream_path(self) -> None:
        pins = json.loads((REPO / "devloop" / "upstream-pins.json").read_text())["pins"]
        self.assertEqual(stale_module_entries(pins), [])

    def test_a_module_that_lost_the_path_or_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "mirror.py").write_text("# cites other.ts only\n")
            pin = {"path": "portal/src/a/column.ts", "workbench_module": "mirror.py"}
            self.assertEqual(len(stale_module_entries([pin], Path(tmp))), 1)
            self.assertEqual(len(stale_module_entries([{**pin, "workbench_module": "gone.py"}], Path(tmp))), 1)
            (Path(tmp) / "mirror.py").write_text("# portal/src/a/column.ts@abc\n")
            self.assertEqual(stale_module_entries([pin], Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()


class AdoptedReadingTests(unittest.TestCase):
    """A Portal reading re-read at a new pin cites that pin, not an older read."""

    def test_receipt_release_reading_cites_the_pinned_routes(self) -> None:
        import re
        pins = {p["path"]: p["commit"] for p in json.loads(
            (REPO / "devloop/upstream-pins.json").read_text(encoding="utf-8"))["pins"]
            if p.get("repo") == "plx-customer-portal"}
        stage_map = json.loads((REPO / "operating/process_stage_map.json")
                               .read_text(encoding="utf-8"))
        reading = next(item for item in stage_map["portal_code_readings"]["items"]
                       if item["id"] == "qc_release_is_a_regulated_signature")
        cited = re.findall(r"(portal/[\w./\[\]-]+)@([0-9a-f]{7,40})", reading["source"])
        release = {path for path, _ in cited if path.endswith("/release/route.ts")}
        self.assertEqual(release, {"portal/src/app/api/mrp/receiving/raw/[id]/release/route.ts",
                                   "portal/src/app/api/mrp/receiving/pack/[id]/release/route.ts"})
        for path, commit in cited:
            self.assertIn(path, pins, path)
            self.assertTrue(pins[path].startswith(commit), (path, commit, pins[path]))
            self.assertTrue(pins[path].startswith(reading["portal_commit"]), path)
        fact = reading["portal_fact"]
        # Pack QC tests and Form #012 only warn since TASK-3130/TASK-3138; the release
        # refuses only when the signed QC results changed. Raw release still blocks.
        for phrase in ("TASK-3138", "TASK-3130", "QC_CHANGED", "warn only",
                       "no evidence that its QC tests passed", "raw receipt release still"):
            self.assertIn(phrase, fact)
        self.assertNotIn("still answers 409 (incomingQcReleaseBlock", fact)
