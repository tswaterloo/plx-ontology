import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "backup"))
import spark_restore as sr  # noqa: E402


class Fake:
    def __init__(self, fail_on=None, counts="biz.a|5\nbiz.b|7\n", free="  900G\n"):
        self.calls, self.fail_on, self.counts, self.free = [], fail_on, counts, free

    def __call__(self, argv, **kw):
        script = argv[-1]
        self.calls.append(script)
        if self.fail_on and self.fail_on in script:
            return subprocess.CompletedProcess(argv, 3, "", "secret value")
        out = self.free if "df -BG" in script else self.counts if "psql" in script else ""
        return subprocess.CompletedProcess(argv, 0, out, "")


class SparkRestore(unittest.TestCase):
    def dump(self):
        d = tempfile.NamedTemporaryFile(delete=False)
        d.write(b"x")
        d.close()
        return Path(d.name)

    def test_counts_come_back_and_the_database_is_always_dropped(self):
        f = Fake()
        got = sr.restore_on("sb4ec", self.dump(), {"biz.a": 5, "biz.b": 7, "biz.c": None},
                            "plx_restore_test_1", runner=f)
        self.assertEqual(got, {"biz.a": 5, "biz.b": 7})
        self.assertIn("dropdb --if-exists --force plx_restore_test_1", f.calls[-1])
        self.assertTrue(any("pg_restore --exit-on-error" in c for c in f.calls))
        # its own cluster, never the test runner's (which sits at its connection limit during a
        # test batch): made and started before the database, stopped after it is dropped
        start = next(c for c in f.calls if "createdb" in c)
        self.assertIn("initdb", start)
        self.assertIn("PGPORT=5434", start)
        self.assertNotIn("5433", start)
        self.assertIn("pg_ctl -D $HOME/plx-restore/data -m fast stop", f.calls[-1])

    def test_failed_restore_still_drops_and_never_prints_remote_output(self):
        f = Fake(fail_on="pg_restore")
        with self.assertRaises(sr.SparkRestoreError) as e:
            sr.restore_on("sb4ec", self.dump(), {"biz.a": 5}, "plx_restore_test_1", runner=f)
        self.assertNotIn("secret", str(e.exception))
        self.assertIn("dropdb", f.calls[-1])

    def test_low_disk_refuses_before_anything_is_created(self):
        f = Fake(free="  50G\n")
        with self.assertRaises(sr.SparkRestoreError):
            sr.restore_on("sb4ec", self.dump(), {"biz.a": 5}, "plx_restore_test_1", runner=f)
        self.assertEqual(len(f.calls), 1)

    def test_unexpected_names_are_refused(self):
        for table in ("biz.a; drop", "a"):
            with self.assertRaises(sr.SparkRestoreError):
                sr.restore_on("sb4ec", self.dump(), {table: 1}, "plx_restore_test_1", runner=Fake())
        with self.assertRaises(sr.SparkRestoreError):
            sr.restore_on("sb4ec", self.dump(), {"biz.a": 1}, "x; rm", runner=Fake())

    def test_extensions_the_spark_lacks_are_left_out_of_the_list_only(self):
        toc = ("6; 3079 209622031 EXTENSION - pg_repack \n6339; 0 0 COMMENT - EXTENSION pg_repack \n"
               "3; 3079 16714 EXTENSION - pg_trgm \n10; 1259 5 TABLE biz alerts me\n"
               "2; 3079 1 EXTENSION - plpgsql \n")
        out, left = sr.without_unavailable_extensions(toc, ["pg_trgm"])
        self.assertEqual(left, ["pg_repack"])
        self.assertIn("pg_trgm", out)
        self.assertIn("TABLE biz alerts", out)
        self.assertNotIn("pg_repack", out)
        self.assertIn("plpgsql", out)


if __name__ == "__main__":
    unittest.main()
