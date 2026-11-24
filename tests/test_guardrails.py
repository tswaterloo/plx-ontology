"""The owner's guardrails as ratchets (tools/guardrails.py): a file's count of a rule may go down, never up.

``RatchetTests`` scan the real tree against ``tools/guardrails-baseline.json``; a new silent except,
silent cut, word-list meaning, duplicate definition or unexpiring pause fails with the exact line and
the rule. ``RuleTests`` prove, on a scratch tree, what each rule flags and what it leaves alone, so
the ratchet has few false positives. Lower the baseline after a fix: ``python3 tools/guardrails.py --lower``.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import guardrails as g  # noqa: E402
from operating.limits import cut, cut_list  # noqa: E402


class RatchetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.findings = g.scan()
        cls.base = g.load_baseline()

    def test_no_file_is_above_its_baseline(self):
        over = g.over_baseline(self.findings, self.base)
        self.assertEqual([], over, "\n" + g.explain(self.findings, over, _base_ref()))

    def test_baseline_never_raised_against_main(self):
        ref = _base_ref()
        shown = subprocess.run(["git", "show", f"{ref}:tools/guardrails-baseline.json"], cwd=ROOT,
                               capture_output=True, text=True)
        if shown.returncode:
            self.skipTest(f"no {ref}:tools/guardrails-baseline.json to compare against")
        before = json.loads(shown.stdout)["rules"]
        raised = [(r, p, n, before.get(r, {}).get(p, 0)) for r, files in self.base.items()
                  for p, n in files.items() if n > before.get(r, {}).get(p, 0)]
        self.assertEqual([], raised, "baseline counts may only go down (rule, file, now, on main)")

    def test_every_rule_has_a_baseline_entry_and_a_statement(self):
        self.assertEqual(set(g.RULES), set(self.base))


def _base_ref() -> str:
    ok = subprocess.run(["git", "rev-parse", "--verify", "-q", "origin/main"], cwd=ROOT, capture_output=True)
    return "origin/main" if ok.returncode == 0 else "HEAD"


class RuleTests(unittest.TestCase):
    def tree(self, files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for rel, text in files.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text))
        return d

    def rules(self, files: dict[str, str]) -> list[tuple[str, str, int]]:
        return [(f.rule, f.path, f.line) for f in g.scan(self.tree(files))]

    def test_silent_except_flagged_and_logged_or_narrow_ones_pass(self):
        got = self.rules({"operating/a.py": """
            import logging
            def a():
                try: x()
                except Exception: return None
            def b():
                try: x()
                except: pass
            def c():
                try: x()
                except (ValueError, Exception): return []
            def ok_logs():
                try: x()
                except Exception:
                    logging.warning("x failed")
                    return None
            def ok_raise():
                try: x()
                except Exception:
                    raise
            def ok_narrow():
                try: x()
                except KeyError: return None
            def ok_status():
                try: x()
                except Exception as e: return {"status": "failed", "error": str(e)}
            def ok_pragma():
                try: x()
                except Exception:  # guard-ok: best-effort cleanup, nothing to surface
                    pass
            """})
        self.assertEqual([("silent-except", "operating/a.py", n) for n in (5, 8, 11)], got)

    def test_pragma_needs_a_reason(self):
        got = self.rules({"operating/a.py": """
            def a():
                try: x()
                except Exception:  # guard-ok:
                    pass
            """})
        self.assertEqual([("silent-except", "operating/a.py", 4)], got)

    def test_silent_cuts_python_sql_and_ts(self):
        got = self.rules({
            "operating/a.py": """
                def f(text, rows, sha, when):
                    a = text[:200]
                    b = rows[:20]
                    c = sha[:12]
                    d = when[:10]
                    e = text[:2]
                    g = rows[2:9]
                    q = "SELECT * FROM t LIMIT 50"
                    return a, b, c, d, e, g, q
                def paged(cur):
                    return cur.execute("SELECT * FROM t LIMIT 50 OFFSET 0"), "total"
                def lookup(cur):
                    return cur.execute("SELECT * FROM t LIMIT 1")
                """,
            "operating/limits.py": "def cut(t, n):\n    return t[:100]\n",
            "app/src/lib/x.ts": "const a = s.slice(0, 80);\nconst b = s.slice(1);\nconst c = 'line-clamp-3';\n"
                                "// s.slice(0, 80) in a comment\nconst d = s.slice(0, 2);\n",
            "app/src/lib/x.test.ts": "const a = s.slice(0, 80);\n",
            "app/src/lib/cut.ts": "const a = s.slice(0, 80);\n",
        })
        self.assertEqual([("silent-cut", "app/src/lib/x.ts", 1), ("silent-cut", "app/src/lib/x.ts", 3),
                          ("silent-cut", "operating/a.py", 3), ("silent-cut", "operating/a.py", 4),
                          ("silent-cut", "operating/a.py", 9)], got)

    def test_shape_meaning_lists_and_word_regexes(self):
        got = self.rules({"operating/a.py": """
            import re
            BULK_WORDS = ("sale", "offer")
            NOREPLY_LOCALS = {"noreply", "donotreply"}
            _STATUS_PATTERNS = (re.compile("x"),)
            REPORT = re.compile(r"invoice|statement|report", re.I)
            OK = re.compile(r"jp|png")
            ID = re.compile(r"[A-Z]{2}\\d+")
            MAX_ITEMS = 5
            """})
        self.assertEqual([("shape-meaning", "operating/a.py", n) for n in (3, 4, 5, 6)], got)

    def test_duplicate_definitions_across_related_modules(self):
        got = self.rules({
            "operating/ask_a.py": "MAX_STEP_CHARS = 4000\nSETTLED = ('done', 'paid', 'closed')\nSAME_FIGURE_MAX = 5\nBATCH = 1\nPAGE_SIZE_MAX = 7\n",
            "operating/ask_b.py": "MAX_STEP_CHARS = 2000\nSETTLED = ('done', 'paid', 'void')\nSAME_FIGURE_MAX = 5\nBATCH = 2\nPAGE_SIZE_MAX = 9\n",
            "operating/zzz.py": "PAGE_SIZE_MAX = 11\nDISK_FLOOR_GB = 1\n",
            "operator_api/c.py": "from operating.ask_a import SETTLED\nALL_DONE_STATES = frozenset({'done', 'paid', 'closed'})\n",
            "operator_api/e.py": "DISK_FLOOR_GB = 30\n",
            "operator_api/d.py": "OTHER_DONE_STATES = frozenset({'done', 'paid', 'closed'})\n",
        })
        names = {(p, ln) for r, p, ln in got}
        self.assertIn(("operating/ask_a.py", 1), names)      # MAX_STEP_CHARS differs between siblings
        self.assertIn(("operating/ask_b.py", 2), names)      # SETTLED differs between siblings
        self.assertIn(("operator_api/c.py", 2), names)       # the same members as ask_a's SETTLED, and it imports ask_a
        self.assertIn(("operator_api/d.py", 1), names)       # a copy of the same status members, related or not
        self.assertNotIn(("operating/ask_a.py", 3), names)   # same value everywhere
        self.assertNotIn(("operating/ask_a.py", 4), names)   # not a limit, floor or status name
        self.assertNotIn(("operating/zzz.py", 1), names)     # same limit name, a module nobody relates to ask_*
        self.assertIn(("operating/zzz.py", 2), names)        # a disk floor with two values is one figure anywhere
        self.assertIn(("operating/ask_a.py", 5), names)      # a limit that differs between siblings

    def test_pauses_need_an_expiry(self):
        got = self.rules({
            "deploy/systemd/host/x.service.d/off.conf": "[Service]\nEnvironment=PLX_THING_OFF=1\n",
            "deploy/systemd/host/y.service.d/off.conf": "# expires: 2026-12-01 owner Pat\n[Service]\nEnvironment=PLX_THING_OFF=1\n",
            "deploy/systemd/host/z.timer.d/off.conf": "# resume: after the migration\n[Service]\nExecStart=/bin/true\n",
            "deploy/systemd/host/w.service.d/hold.conf": "[Service]\nExecStart=/bin/true\n",
            "deploy/ops/ingest-move.hold": "held\n",
            "deploy/systemd/host/v.service.d/ok.conf": "[Service]\nEnvironment=NEXT_TELEMETRY_DISABLED=1\nEnvironment=PLX_X=1\n",
        })
        self.assertEqual([("pause-expiry", "deploy/ops/ingest-move.hold", 1),
                          ("pause-expiry", "deploy/systemd/host/w.service.d/hold.conf", 2),
                          ("pause-expiry", "deploy/systemd/host/x.service.d/off.conf", 2)], got)

    def test_over_baseline_names_the_line_and_the_rule(self):
        findings = g.scan(self.tree({"operating/a.py": "def f(t):\n    return t[:200]\n"}))
        over = g.over_baseline(findings, {"silent-cut": {}})
        self.assertEqual([("silent-cut", "operating/a.py", 1, 0)], over)
        self.assertEqual([], g.over_baseline(findings, {"silent-cut": {"operating/a.py": 1}}))
        self.assertIn("operating/a.py:2", g.explain(findings, over, "HEAD"))


class CutHelperTests(unittest.TestCase):
    def test_cut_says_what_was_left_out(self):
        c = cut("x" * 9120, 4000, say="model window")
        self.assertEqual((4000, True, 5120), (len(c.text), c.cut, c.left_out))
        self.assertIn("4,000 of 9,120 characters shown (model window); 5,120 more not shown", c.note)

    def test_nothing_cut_says_nothing(self):
        c = cut("short", 100)
        self.assertEqual(("short", False, ""), (c.text, c.cut, c.note))

    def test_cut_list_n_1_10_60(self):
        for n in (1, 10, 60):
            c = cut_list(list(range(n)), 10, say="page size", noun="orders")
            self.assertEqual(min(n, 10), len(c.items))
            self.assertEqual(n > 10, c.cut)
            self.assertEqual(n, c.total)
            self.assertEqual(max(0, n - 10), c.left_out)
            self.assertEqual(c.cut, bool(c.note))


if __name__ == "__main__":
    unittest.main()
