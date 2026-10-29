"""The last complete Portal mirror on a private git ref,
over a local bare repository standing in for the
private remote.

Evidence label: ``injected-transport``. Copies come from the real ``SourceMirror``
behind the real ``WorkbenchReadApiClient`` over the source-mirror tests' SYNTHETIC
``FakeWorkbench``; git is real, against a temporary bare repository (never the
repository's own remote). The verifier's attestation on the live-results branch is a commit
made here (``attest``). The live check reading the ref is tested with the spec's other
tests (``test_source_mirror_persisted.LiveCheckSpecTests``).
"""

from __future__ import annotations

from hashlib import sha256
import io
import contextlib
import json
import sqlite3
import lzma
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from integrations import mirror_branch_store as mbs, mirror_capture as mc
from integrations.http import SourceHttpError
from integrations.source_mirror import MirrorError
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor
from ontology.tests.test_resolve import CATALOG, tables

RELATIONS = sorted(CATALOG)
AUDIENCE = {"public.*": ["staff:sales"]}
SALES = Actor("plx", frozenset({"staff:sales"}), "restricted")
HTTP_500 = SourceHttpError("read-only source GET failed: HTTPError", status=500,
                           error_code="internal")
LABEL = ("source-mirror captures as of 2026-09-29T12:00:00.000000Z, live source unavailable "
         "(500, internal)")
TOKEN = "ghp_" + "Z" * 36


def git(cwd, *args, **kwargs):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          **kwargs).stdout


def commit_tree(cwd, entries):
    """A parentless commit of ``entries`` (``(mode, type, name, bytes or tree sha)``)."""
    lines = []
    for mode, kind, name, data in entries:
        sha = data if kind == "tree" else git(cwd, "hash-object", "-w", "--stdin",
                                               input=data).decode().strip()
        lines.append(f"{mode} {kind} {sha}\t{name}\n")
    tree = git(cwd, "mktree", input="".join(sorted(lines)).encode()).decode().strip()
    return tree, git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree,
                     "-m", "t").decode().strip()


def attest(repo, record, remote="origin"):
    """The verifier's results commit on the live-results branch holding ``record`` at
    ``results/mirror-attestation.json``, as the reader fetched it."""
    inner, _ = commit_tree(repo, [("100644", "blob", "mirror-attestation.json",
                                   json.dumps(record).encode())])
    _, commit = commit_tree(repo, [("040000", "tree", "results", inner)])
    git(repo, "update-ref", f"refs/remotes/{remote}/{mbs.LIVE_BRANCH}", commit)
    return commit


def publish_attested(branch, store, environ=None):
    """``publish`` then ``attest``, the record committed on the reader's live ref."""
    report = branch.publish(store, environ or {})
    with tempfile.TemporaryDirectory() as work:
        record = Path(work) / "mirror-attestation.json"
        if branch.attest(report, record):
            attest(branch.repo, json.loads(record.read_text()), branch.remote)
    return report


class BranchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fake = FakeWorkbench()
        self.fake.catalog = json.loads(json.dumps(CATALOG))
        self.fake.tables = tables()
        self.down = None  # raised by GET /v1/health while set (the live outage shape)
        self.clock = Clock()
        self.store = mc.MirrorCaptureStore(self.root / "state", clock=self.clock)
        self.bare = self.root / "remote.git"
        git(self.root, "init", "--quiet", "--bare", str(self.bare))
        self.repo = self.root / "repo"
        git(self.root, "init", "--quiet", str(self.repo))
        git(self.repo, "remote", "add", "origin", str(self.bare))
        self.branch = mbs.MirrorBranch(self.repo)

    def transport(self, url, headers, *, max_bytes):
        if self.down is not None and urlsplit(url).path == "/v1/health":
            raise self.down
        return self.fake(url, headers, max_bytes=max_bytes)

    def serve(self):
        mirror, report = mc.sync_or_persisted(
            self.store, WorkbenchReadApiClient(BASE, KEY, fetch=self.transport), AUDIENCE,
            SALES, relations=RELATIONS, clock=self.clock, retry_delay_seconds=0)
        self.addCleanup(mirror.close)
        return mirror, report

    def persist_live(self):
        mirror, report = self.serve()
        self.assertTrue(report["persisted"]["persisted"], report)
        mirror.close()

    def heads(self):
        return git(self.bare, "for-each-ref", "--format=%(objectname)",
                   mbs.REF).decode().split()

    def published(self):
        """Every byte the ref stores: the manifest and the decompressed copy."""
        (head,) = self.heads()
        self.assertEqual(git(self.bare, "rev-list", "--count", head).strip(), b"1")
        manifest = json.loads(git(self.bare, "show", f"{head}:manifest.json"))
        data = b"".join(git(self.bare, "cat-file", "blob", f"{head}:{part['name']}")
                        for part in manifest["parts"])
        return manifest, lzma.decompress(data)

    def copy_in(self, name):
        """A complete copy persisted by a full sync into a new store ``name``."""
        self.store = mc.MirrorCaptureStore(self.root / name, clock=self.clock)
        self.persist_live()
        return self.store

    def local_copy(self, store=None):
        """The file a publish of the store's newest copy carries (a pin's scope exported)."""
        return self.local_manifest(store)[1]

    def local_manifest(self, store=None):
        work = self.root / f"export-{len(list(self.root.glob('export-*')))}"
        work.mkdir()
        return mbs.scope_copy((store or self.store).load(RELATIONS, tenant="plx"), work)

    def plant(self, blobs, copy):
        """A self-consistent commit any pusher can put on the ref: part digests and the
        manifest agree with ``blobs``."""
        parts = [{"name": f"{mbs.PART_PREFIX}{i:03d}", "sha256": sha256(b).hexdigest(),
                  "bytes": len(b)} for i, b in enumerate(blobs)]
        manifest = {"schema": mbs.SCHEMA, "copy": copy, "compression": "xz", "parts": parts,
                    "published_at": "2026-09-29T13:00:00.000000Z"}
        _, commit = commit_tree(self.bare, [("100644", "blob", "manifest.json",
                                             json.dumps(manifest).encode())] +
                                [("100644", "blob", p["name"], b) for p, b in zip(parts, blobs)])
        git(self.bare, "update-ref", mbs.REF, commit)
        return commit

    def restore_fresh(self, name):
        fresh = mc.MirrorCaptureStore(self.root / name, clock=self.clock)
        return fresh, self.branch.restore_into(fresh, RELATIONS)


class PublishAndRestoreTests(BranchTestCase):
    def test_a_complete_copy_round_trips_byte_identical_and_answers_a_fresh_store(self):
        self.persist_live()
        original = self.local_copy().read_bytes()
        record = self.root / "live" / "results" / "mirror-attestation.json"
        record.parent.mkdir(parents=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mbs.main(["publish", "--repo", str(self.repo), "--attest", str(record)],
                            environ={mc.STATE_ENV: str(self.root / "state")})
        report = json.loads(out.getvalue())
        self.assertEqual((code, report["written"], report["attested"]), (0, True, True), report)
        manifest, copy = self.published()
        self.assertEqual(copy, original)
        # A non-branch ref: a default fetch (or clone) of the remote never downloads it.
        self.assertEqual(git(self.bare, "for-each-ref", "refs/heads").strip(), b"")
        clone = self.root / "clone"
        git(self.root, "clone", "--quiet", str(self.bare), str(clone))
        git(clone, "fetch", "--quiet", "origin")
        self.assertEqual(git(clone, "for-each-ref", "refs/devloop").strip(), b"")
        attested = attest(self.repo, json.loads(record.read_text()))

        fresh = mc.MirrorCaptureStore(self.root / "fresh", clock=self.clock)
        self.down = HTTP_500
        self.store = fresh
        with self.assertRaises(MirrorError) as caught:
            self.serve()
        self.assertEqual(caught.exception.code, "missing_mirror")
        restored = self.branch.restore_into(fresh, RELATIONS)
        self.assertEqual((restored["status"], restored["branch_source"]),
                         ("restored", "remote"), restored)
        self.assertEqual(self.local_copy(fresh).read_bytes(), original)
        # The restored manifest carries the attestation it was installed under.
        restored_from = fresh.load(RELATIONS, tenant="plx")["restored_from"]
        self.assertEqual(restored_from, {"ref": mbs.REF, "commit": report["commit"],
                                         "attestation": {
                                             "schema": mbs.ATTESTATION_SCHEMA,
                                             "commit": report["commit"],
                                             "copy_sha256": manifest["copy"]["sha256"],
                                             "results_commit": attested}})
        mirror, served = self.serve()
        self.assertEqual((served["served_from"], served["source_freshness"]["label"]),
                         ("persisted_capture", LABEL), served)
        self.assertEqual(self.branch.restore_into(fresh, RELATIONS)["status"], "present")
        # A reader without access to the remote reads the ref the verifier fetched by name.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mbs.main(["fetch", "--repo", str(self.repo)]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"fetched": True})
        git(self.repo, "remote", "set-url", "origin", str(self.root / "unreachable.git"))
        offline, restored = self.restore_fresh("offline")
        self.assertEqual((restored["status"], restored["branch_source"]),
                         ("restored", "local_ref"), restored)
        self.assertEqual(self.local_copy(offline).read_bytes(), original)
        git(self.repo, "remote", "set-url", "origin", str(self.bare))

        # A later complete copy replaces it: still one parentless commit.
        self.down = None
        self.clock.advance(3600)
        self.copy_in("state-2")
        with patch.object(mbs, "PART_BYTES", 997):  # many parts, borders inside blocks
            self.assertGreater(self.branch.publish(self.store, {})["parts"], 3)
        newer, copy = self.published()
        self.assertGreater(newer["copy"]["captures_as_of"], manifest["copy"]["captures_as_of"])
        self.assertEqual(copy, self.local_copy().read_bytes())
        with patch.object(mbs, "PART_BYTES", 997):
            again = self.branch.publish(self.store, {})
        self.assertEqual((again["reason"], again["commit"]), ("unchanged", self.heads()[0]))

    def test_only_the_copy_the_verifier_attested_is_restored(self):
        """Any session that can push can plant a self-consistent commit; it is served
        only when the verifier's own record names its commit and copy digest."""
        self.persist_live()
        report = publish_attested(self.branch, self.store)
        genuine = attest(self.repo, {"schema": mbs.ATTESTATION_SCHEMA, "ref": mbs.REF,
                                     "commit": report["commit"],
                                     "copy_sha256": report["copy_sha256"]})
        self.clock.advance(60)
        self.fake.tables["public.app_sales_order"][0]["status"] = "PLANTED-ROW"
        planted = self.copy_in("planted")
        copy, planted_file = self.local_manifest(planted)
        commit = self.plant([lzma.compress(planted_file.read_bytes())], copy)
        for record in (None,  # the verifier's record names the earlier commit
                       {"commit": commit, "copy_sha256": report["copy_sha256"]},
                       {"commit": commit, "copy_sha256": copy["sha256"], "ref": "refs/x"}):
            if record is not None:
                attest(self.repo, dict({"schema": mbs.ATTESTATION_SCHEMA, "ref": mbs.REF},
                                       **record))
            fresh, restored = self.restore_fresh("fresh")
            self.assertEqual((restored["status"], restored["reason"]),
                             ("refused", "unattested"), (record, restored))
            self.assertEqual(list((fresh.root / "scopes").glob("*/*")), [])
        git(self.repo, "update-ref", "-d", f"refs/remotes/origin/{mbs.LIVE_BRANCH}")
        self.assertEqual(self.restore_fresh("none")[1]["reason"], "unattested")
        self.assertIsNotNone(genuine)

    def test_a_compressed_copy_that_is_truncated_oversized_or_continued_fails_typed(self):
        self.persist_live()
        copy, exported = self.local_manifest()
        original = exported.read_bytes()
        whole = lzma.compress(original)
        for blobs, reason in (([whole[:-64]], "branch_copy_truncated"),
                              ([lzma.compress(original + b"\0" * (3 << 20))],
                               "branch_copy_oversized"),
                              ([whole, lzma.compress(b"more")], "branch_copy_corrupt")):
            commit = self.plant(blobs, copy)  # attested: the decoder is the last guard
            attest(self.repo, {"schema": mbs.ATTESTATION_SCHEMA, "ref": mbs.REF,
                               "commit": commit, "copy_sha256": copy["sha256"]})
            fresh, restored = self.restore_fresh(f"fresh-{reason}")
            self.assertEqual((restored["status"], restored["reason"]), ("refused", reason))
            self.assertEqual(list((fresh.root / "scopes").glob("*")), [])

    def test_an_incomplete_unverified_or_changing_copy_is_never_written(self):
        self.assertEqual(self.branch.publish(self.store, {})["reason"], "no_complete_copy")
        self.persist_live()
        scan = mbs._scan_values

        def changed_after_the_scan(path, found):
            scan(path, found)
            data = bytearray(path.read_bytes())
            data[-1] ^= 0xFF
            path.write_bytes(bytes(data))
        with patch.object(mbs, "_scan_values", changed_after_the_scan):
            with self.assertRaises(mbs.BranchStoreError) as caught:
                self.branch.publish(self.store, {})
        self.assertEqual(caught.exception.code, "copy_changed")
        self.assertEqual(self.heads(), [])
        # A pin whose mirror no longer holds a completed capture of its scope never publishes.
        pinned = Path(self.store.load(RELATIONS, tenant="plx")["path"])
        conn = sqlite3.connect(pinned)
        conn.execute("DELETE FROM relation_state WHERE relation = ?", (RELATIONS[0],))
        conn.commit()
        conn.close()
        self.assertEqual(self.branch.publish(self.store, {})["reason"], "no_complete_copy")
        self.assertEqual(self.heads(), [])

    def test_a_head_whose_manifest_is_invalid_is_replaced_with_its_reason(self):
        _, junk = commit_tree(self.bare, [("100644", "blob", "manifest.json", b"{}")])
        git(self.bare, "update-ref", mbs.REF, junk)
        self.persist_live()
        report = self.branch.publish(self.store, {})
        self.assertEqual((report["written"], report["replaced_invalid_head"]),
                         (True, "branch_manifest_invalid"), report)
        self.assertEqual(self.published()[1], self.local_copy().read_bytes())

    def test_a_concurrent_writer_loses_the_cas_and_retries_never_replacing_a_later_copy(self):
        other = self.copy_in("other")  # the rival writer's copy, captured first
        self.clock.advance(600)
        self.copy_in("mine")
        rival = mbs.MirrorBranch(self.repo)
        push = self.branch._push
        calls = []

        def racing(commit, old):
            if not calls:  # the rival publishes between our read of the head and our push
                self.assertTrue(rival.publish(other, {})["written"])
            calls.append(old)
            return push(commit, old)

        with patch.object(self.branch, "_push", racing):
            report = self.branch.publish(self.store, {})
        self.assertEqual((report["written"], report["attempts"]), (True, 2), report)
        self.assertIsNone(calls[0])
        self.assertEqual(self.published()[1], self.local_copy().read_bytes())
        # The rival's older copy never replaces the later one.
        self.assertEqual(rival.publish(other, {})["reason"], "remote_newer")
        self.assertEqual(self.published()[1], self.local_copy().read_bytes())

    def test_no_secret_shaped_value_is_stored(self):
        environ = {"PLX_WORKBENCH_API_KEY": KEY, "GH_TOKEN": "gho_" + "q" * 36,
                   "PATH": "/usr/bin"}
        self.persist_live()
        self.assertTrue(self.branch.publish(self.store, environ)["written"])
        manifest, copy = self.published()
        stored = copy + json.dumps(manifest).encode() + git(
            self.bare, "log", "--format=%B", mbs.REF)
        for value in mbs.secret_values(environ):
            self.assertNotIn(value, stored)

        # A copy holding a credential's value, or a token shape, is refused whole.
        head = self.heads()
        self.clock.advance(60)
        self.fake.tables["public.app_sales_order"][0]["status"] = TOKEN
        self.copy_in("token")
        with self.assertRaises(mbs.BranchStoreError) as caught:
            self.branch.publish(self.store, {})
        self.assertEqual(caught.exception.code, "secret_shaped_value")
        self.fake.tables["public.app_sales_order"][0]["status"] = "CONFIRMED-SYNX9"
        self.clock.advance(60)
        self.copy_in("credential")
        with self.assertRaises(mbs.BranchStoreError) as caught:
            self.branch.publish(self.store, {"PORTAL_SECRET": "CONFIRMED-SYNX9"})
        self.assertEqual(caught.exception.code, "secret_shaped_value")

        # A token inside one long value that SQLite splits across pages: the raw file
        # never holds it whole, the decoded value does.
        long_value = "".join(f"{i:05d}" for i in range(1200))
        self.fake.tables["public.app_sales_order"][0]["status"] = long_value
        self.clock.advance(60)
        raw = self.local_copy(self.copy_in("probe")).read_bytes()
        at = raw.index(long_value[:40].encode())
        split = next(i for i in range(len(long_value)) if raw[at + i] != ord(long_value[i]))
        self.assertGreater(split, 4 << 9)  # past the first page's local payload
        self.fake.tables["public.app_sales_order"][0]["status"] = (
            long_value[:split - 20] + TOKEN + long_value[split + 20:])
        self.clock.advance(60)
        raw = self.local_copy(self.copy_in("straddling")).read_bytes()
        self.assertIsNone(mbs.TOKEN_SHAPES.search(raw))  # the token straddles a page border
        with self.assertRaises(mbs.BranchStoreError) as caught:
            self.branch.publish(self.store, {})
        self.assertEqual(caught.exception.code, "secret_shaped_value")
        self.assertEqual(self.heads(), head)

    def test_business_data_goes_only_to_the_private_repository(self):
        for url in ("https://github.com/tswaterloo/OntologyProject",
                    "https://x-access-token:t@github.com/tswaterloo/OntologyProject.git",
                    "git@github.com:tswaterloo/OntologyProject.git",
                    "ssh://git@github.com/tswaterloo/OntologyProject", str(self.bare)):
            self.assertTrue(mbs.private_url(url), url)
        for url in ("https://evil.example/tswaterloo/OntologyProject",
                    "https://github.com.evil.example/tswaterloo/OntologyProject",
                    "https://github.com@evil.example/tswaterloo/OntologyProject",
                    "https://github.com:8443/tswaterloo/OntologyProject",
                    "http://github.com/tswaterloo/OntologyProject",
                    "https://github.com/someone/tswaterloo/OntologyProject",
                    "git@evil.example:tswaterloo/OntologyProject",
                    "evil.example:tswaterloo/OntologyProject",
                    "file://evil.example/tswaterloo/OntologyProject", "ext::sh -c x"):
            self.assertFalse(mbs.private_url(url), url)
        self.persist_live()
        publish_attested(self.branch, self.store)
        head = self.heads()
        git(self.repo, "remote", "add", "lookalike",
            "https://evil.example/tswaterloo/OntologyProject")
        # Fetch from the private bare, push elsewhere: pushurl, or pushInsteadOf.
        git(self.repo, "config", "remote.origin.pushurl",
            "https://evil.example/tswaterloo/OntologyProject")
        for remote in ("lookalike", "origin"):
            branch = mbs.MirrorBranch(self.repo, remote=remote)
            for call in (lambda: branch.publish(self.store, {}), branch.fetch):
                with self.assertRaises(mbs.BranchStoreError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "remote_not_private_repo", remote)
            self.assertEqual(self.restore_fresh(f"fresh-{remote}")[1]["reason"],
                             "remote_not_private_repo")
        git(self.repo, "config", "--unset", "remote.origin.pushurl")
        git(self.repo, "config", f"url.https://evil.example/{self.bare}.pushInsteadOf",
            str(self.bare))
        with self.assertRaises(mbs.BranchStoreError) as caught:
            self.branch.publish(self.store, {})
        self.assertEqual(caught.exception.code, "remote_not_private_repo")
        self.assertEqual(self.heads(), head)
