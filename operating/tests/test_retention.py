"""Retention (P3): a dry run removes nothing; a worktree another unit, script or process
names, the current directory of any process, an infrastructure worktree, one with uncommitted
work, one with no commits of its own (never pushed), or one touched in the last day is never
a candidate; a pushed, merged, idle one is. plx-release is resolved from the live release."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from operating import retention as R


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})


class Worktrees(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo, self.wt, self.cfg = root / "repo", root / "wt", root / "cfg"
        self.repo.mkdir(); self.wt.mkdir(); self.cfg.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "f").write_text("x")
        git(self.repo, "add", "f"); git(self.repo, "commit", "-qm", "c")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        for name in ("merged", "named", "dirty", "deploy", "fresh", "nocommit", "recent",
                     "incwd"):
            git(self.repo, "worktree", "add", "-q", "-b", f"lane/{name}", str(self.wt / name))
            if name != "nocommit":   # pushed: its remote branch equals HEAD (merged into main)
                git(self.repo, "update-ref", f"refs/remotes/origin/lane/{name}", "HEAD")
        old = __import__("time").time() - 3 * 86400
        for name in ("merged", "named", "dirty", "deploy", "nocommit", "incwd"):
            gitdir = self.repo / ".git" / "worktrees" / name
            for path in [self.wt / name, *(self.wt / name).iterdir(),
                         *(gitdir / f for f in ("HEAD", "logs/HEAD") if (gitdir / f).exists())]:
                os.utime(path, (old, old), follow_symlinks=False)
        (self.wt / "dirty" / "new").write_text("work")
        self.cwd = {str(self.wt / "incwd")}
        (self.wt / "fresh" / "g").write_text("y")
        git(self.wt / "fresh", "add", "g"); git(self.wt / "fresh", "commit", "-qm", "new")
        (self.cfg / "unit.service").write_text(f"ExecStart=/x {self.wt}/named/run.sh\n")
        for k, v in (("REPO", self.repo), ("WT", self.wt), ("DATA", str(root)),
                     ("LOGS", root / "no-logs"),
                     ("EXCLUDE_SOURCES", (str(self.cfg),))):
            p = mock.patch.object(R, k, v)
            p.start(); self.addCleanup(p.stop)
        pat = mock.patch.object(R, "excluded_paths", side_effect=self._excluded)
        pat.start(); self.addCleanup(pat.stop)

    def _excluded(self):
        got = subprocess.run(["grep", "-rhoE", str(self.wt) + r"/[A-Za-z0-9._-]+",
                              str(self.cfg)], capture_output=True, text=True).stdout.split()
        return set(got) | {str(self.wt / n) for n in R.INFRA} | self.cwd

    def test_only_merged_unnamed_clean_worktrees_are_candidates_and_a_dry_run_keeps_all(self):
        got = {Path(c["path"]).name: c["why"] for c in R.worktree_candidates()}
        self.assertEqual(got, {"merged": "merged"})
        out = R.run(False, lambda line: None)
        self.assertTrue((self.wt / "merged").exists())
        self.assertFalse(out["apply"])

    def test_no_commits_recent_touch_and_a_process_cwd_each_keep_a_worktree(self):
        got = {Path(c["path"]).name for c in R.worktree_candidates()}
        self.assertNotIn("nocommit", got)   # never pushed: no commit of its own, not 'merged'
        self.assertNotIn("recent", got)     # merged but touched within IDLE_HOURS
        self.assertNotIn("incwd", got)      # a process runs in it
        os.utime(self.wt / "recent" / "f", None)

    def test_a_process_cwd_inside_a_worktree_is_found(self):
        with tempfile.TemporaryDirectory() as proc:
            for pid, target in (("101", "/data/scratch/wt/ratchet-fix-1123/sub"),
                                ("102", "/home/x"), ("103", "/data/scratch/wt/other")):
                os.makedirs(f"{proc}/{pid}")
                os.symlink(target, f"{proc}/{pid}/cwd")
            os.makedirs(f"{proc}/104")            # a cwd that cannot be read (not a link)
            open(f"{proc}/104/cwd", "w").close()
            os.makedirs(f"{proc}/self-not-a-pid")
            found, unreadable = R.process_cwds(proc)
        self.assertEqual(found, {"/data/scratch/wt/ratchet-fix-1123", "/data/scratch/wt/other"})
        self.assertEqual(unreadable, 1)

    def test_plx_release_is_the_live_releases_not_a_fixed_path(self):
        with tempfile.TemporaryDirectory() as home:
            tool = Path(home) / ".local/bin/plx-release"
            tool.parent.mkdir(parents=True)
            tool.write_text("#!/bin/sh\n")
            with mock.patch.object(R.shutil, "which", return_value=None), \
                    mock.patch.object(Path, "home", return_value=Path(home)):
                self.assertEqual(R.release_tool(), str(tool))
                tool.unlink()
                with mock.patch.object(R.os.path, "exists", return_value=False):
                    with self.assertRaises(RuntimeError):
                        R.release_tool()


if __name__ == "__main__":
    unittest.main()
