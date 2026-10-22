"""The last complete Portal source-mirror copy, kept compressed on a private git ref.

Until an always-on host exists, the credentialed
live-check runner has no state directory that survives between runs, so ``integrations/mirror_capture.py``'s copy is
lost with each container. This module keeps the newest complete copy on the ref
``refs/verify/mirror`` of the private repository and reads it back as the outage
fallback when the configured ``PLX_WORKBENCH_STATE_DIR`` holds none. Business data
is accepted in the private repo (this supersedes "captures never go to Git" for
this one ref only); secrets never. The ref is not a branch, so a default ``git fetch`` in a clone never downloads it;
every read names it.

Ref layout: exactly one parentless commit per published copy (older ones are not
ancestors, so they are squashed away), its tree flat::

    manifest.json                plx-mirror-branch/v1: the store manifest ("copy"),
                                 the parts and their digests, published_at
    mirror.sqlite.xz.000 ...     the copy, xz-compressed, split in PART_BYTES parts

Remote: every fetch and push URL of the remote (``remote get-url --all`` and
``--push --all``, ``insteadOf``/``pushurl`` expanded) is this private repository on
github.com, scheme, host and path matched exactly, or a local path; checked before a
publish, a fetch and a restore.

Write (``MirrorBranch.publish``, the live-check runner's ``publish`` command): only a copy the
store itself verifies as complete (``MirrorCaptureStore.load``: manifest complete,
digest, every scope relation captured) is published; an incomplete one never is. The
manifest, every decoded column value of every table of the copy (a value split across
SQLite overflow pages is whole here) and the raw file bytes (freed pages) are scanned
first: the value of every credential-shaped environment variable and the shapes of
well-known tokens refuse the write (``secret_shaped_value``). The bytes compressed are
hashed again and must be the verified copy (``copy_changed``). The push is a
compare-and-swap (``--force-with-lease`` against the head that was read, hooks
disabled):
a writer that loses the race re-reads the head and retries, and never replaces a copy
captured later than its own. A head whose manifest does not verify is replaced, its
reason returned (``replaced_invalid_head``).

Attestation: ``publish --attest FILE`` writes ``plx-mirror-attestation/v1`` (the ref,
the commit and the copy's sha256) for the copy now on the ref; the live-check runner commits it
as ``results/mirror-attestation.json`` on its results branch.

Read (``MirrorBranch.restore_into``): the head from the remote (``ls-remote``, fetched
by SHA), or, when the remote cannot be read without credentials, the local
``refs/verify/remotes/<remote>/...`` the live-check runner fetched (``fetch`` command). A copy
is installed only when that head is the commit, and its copy the sha256, the
live-check runner's attestation on the fetched results branch names (else ``refused``
``unattested``: a self-consistent commit any pusher planted is never served), and only
for a scope the store holds no copy of, each part and the whole file checked against
the manifest's sha256 and the output capped at the copy's size; the store then
verifies and serves it as its own persisted copy (stale, labelled), so per-employee
trimming applies unchanged. The restored manifest carries the attestation.

Every return value is counts, booleans, enums and commit/digest hex; never a row value,
key or citation.
Evidence: module tests run over a local bare repository (``integrations/tests/
test_mirror_branch_store.py``, injected Workbench transport); the live behaviour is
``live-unverified`` until ``verify/live-checks/mirror.persisted_fallback.json`` records
a result read from the ref.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
from hashlib import sha256
import json
import lzma
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlsplit

from . import mirror_capture as mc
from .source_mirror import MirrorError

REF = "refs/verify/mirror"
REF_SHAPE = re.compile(r"refs/verify/[a-z0-9][a-z0-9-]*")
LIVE_BRANCH = "claude/verify-live"
ATTESTATION_PATH = "results/mirror-attestation.json"
ATTESTATION_SCHEMA = "plx-mirror-attestation/v1"
SCHEMA = "plx-mirror-branch/v1"
PART_BYTES = 48 << 20  # under GitHub's 100 MB per-file limit
PART_PREFIX = "mirror.sqlite.xz."
MAX_COPY_BYTES = 64 << 30  # a restore never writes more, whatever a manifest declares
LZMA_MEMLIMIT = 256 << 20  # preset 6 needs about 9 MB to decompress
CHUNK = 1 << 20
CAS_TRIES = 8
GIT_TIMEOUT_SECONDS = 600
# The remote must be this private repository on github.com (scheme, host and path exact)
# or a local path (the tests' bare repositories).
PRIVATE_HOST = "github.com"
PRIVATE_PATH = re.compile(r"tswaterloo/OntologyProject(\.git)?/?")
SECRET_ENV_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|COOKIE|DSN|CREDENTIAL|SESSION",
                             re.IGNORECASE)
TOKEN_SHAPES = re.compile(
    rb"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|sk-ant-[A-Za-z0-9_-]{20,}"
    rb"|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|plxwb_[A-Za-z0-9]{20,}"
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
_TOKEN_OVERLAP = 128
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RACE = ("stale info", "fetch first", "non-fast-forward", "cannot lock ref")
IDENTITY = {"GIT_AUTHOR_NAME": "PLX live verifier", "GIT_AUTHOR_EMAIL": "noreply@anthropic.com",
            "GIT_COMMITTER_NAME": "PLX live verifier",
            "GIT_COMMITTER_EMAIL": "noreply@anthropic.com"}


class BranchStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def secret_values(environ: Mapping[str, str]) -> list[bytes]:
    """Values of credential-shaped variables long enough to be a credential."""
    return sorted({value.encode() for name, value in environ.items()
                   if SECRET_ENV_NAME.search(name) and isinstance(value, str)
                   and len(value) >= 8}, key=len, reverse=True)


def private_url(url: str) -> bool:
    """``url`` is the private repository on github.com (``https://``, ``ssh://git@`` or
    ``git@github.com:``; scheme, host, port and path exact) or a local path (no host)."""
    scp = re.fullmatch(r"git@github\.com:(.*)", url)
    if scp:
        return PRIVATE_PATH.fullmatch(scp.group(1)) is not None
    parts = urlsplit(url)
    if not parts.scheme:
        return ":" not in url.split("/", 1)[0]
    if parts.scheme == "file":
        return not parts.netloc and Path(parts.path).is_absolute()
    try:
        port = parts.port
    except ValueError:
        return False
    return (port is None and parts.hostname == PRIVATE_HOST and not parts.query
            and not parts.fragment
            and (parts.scheme == "https" or (parts.scheme == "ssh" and parts.username == "git"))
            and PRIVATE_PATH.fullmatch(parts.path[1:]) is not None)


class _SecretScan:
    """Search for secret values and token shapes: a byte stream across block borders
    (``feed``) or whole values (``value``)."""

    def __init__(self, values: Sequence[bytes]) -> None:
        self.values = list(values)
        self.keep = max([len(v) for v in self.values] + [_TOKEN_OVERLAP])
        self.tail = b""
        self.found = 0
        self.pending = bytearray()

    def _count(self, data: bytes) -> int:
        return sum(1 for v in self.values if v in data) + (1 if TOKEN_SHAPES.search(data) else 0)

    def feed(self, block: bytes) -> None:
        window = self.tail + block
        self.found += self._count(window)
        self.tail = window[-self.keep:]

    def value(self, data: bytes | None) -> None:
        """One whole value (None: flush). Values are joined by NUL, which neither a token
        shape nor an environment value holds, so no match spans two values."""
        if data is not None:
            self.pending += data + b"\0"
        if self.pending and (data is None or len(self.pending) >= CHUNK):
            self.found += self._count(bytes(self.pending))
            self.pending.clear()


def _scan_values(path: Path, scan: _SecretScan) -> None:
    """Every decoded column value of every table of the SQLite file ``path``, whole: a
    value SQLite splits across overflow pages is one value here, never two raw pieces."""
    try:
        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)
        try:
            tables = [name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")]
            conn.text_factory = bytes
            for table in ["sqlite_master", *tables]:
                for row in conn.execute('SELECT * FROM "%s"' % table.replace('"', '""')):
                    for cell in row:
                        if cell is not None:
                            scan.value(cell if isinstance(cell, bytes) else str(cell).encode())
            scan.value(None)
        finally:
            conn.close()
    except sqlite3.Error:
        raise BranchStoreError("secret_scan_failed", "the copy's values could not be read") \
            from None


class MirrorBranch:
    """The copy on ``ref`` of ``remote`` in the git repository ``repo``."""

    def __init__(self, repo: str | Path, *, remote: str = "origin", ref: str = REF,
                 live_ref: str | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        if not REF_SHAPE.fullmatch(ref):
            raise BranchStoreError("ref_invalid", "the copy lives under refs/verify/ only")
        self.repo = Path(repo)
        self.remote = remote
        self.ref = ref
        # Fetched here, not under refs/remotes/: ``fetch --prune`` would delete it there.
        self.local_ref = f"refs/verify-remotes/{remote}/{ref[len('refs/verify/'):]}"
        self.live_ref = live_ref or f"refs/remotes/{remote}/{LIVE_BRANCH}"
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- git -----------------------------------------------------------------

    def _git(self, *args: str, input: bytes | None = None, check: bool = True,
             env: Mapping[str, str] | None = None, stdout: Any = subprocess.PIPE
             ) -> subprocess.CompletedProcess:
        merged = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
        # Never run hooks: a push here carries the live-check runner's credentials.
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                                cwd=self.repo, input=input, stdout=stdout,
                                stderr=subprocess.PIPE, env=merged,
                                timeout=GIT_TIMEOUT_SECONDS)
        if check and result.returncode != 0:
            raise BranchStoreError("git_failed", f"git {args[0]}")
        return result

    def _text(self, *args: str, check: bool = True) -> str:
        return self._git(*args, check=check).stdout.decode().strip()

    def _assert_private_remote(self) -> None:
        """Every URL git would fetch from or push to for ``remote`` (``url``,
        ``pushurl``, ``insteadOf``/``pushInsteadOf`` expanded); the remote must be a
        configured one, so no URL escapes that expansion."""
        urls: list[str] = []
        for push in ((), ("--push",)):
            listed = self._git("remote", "get-url", *push, "--all", self.remote, check=False)
            if listed.returncode != 0:
                raise BranchStoreError("remote_not_private_repo", "not a configured remote")
            urls += [line.strip() for line in listed.stdout.decode().splitlines()
                     if line.strip()]
        if not urls or not all(private_url(url) for url in urls):
            raise BranchStoreError("remote_not_private_repo")

    def _remote_head(self) -> str | None:
        listed = self._git("ls-remote", self.remote, self.ref, check=False)
        if listed.returncode != 0:
            raise BranchStoreError("remote_unreadable")
        heads = [line.split("\t")[0] for line in listed.stdout.decode().splitlines()
                 if line.split("\t")[1:] == [self.ref]]
        if not heads:
            return None
        if not _HEX40.fullmatch(heads[0]):
            raise BranchStoreError("remote_unreadable")
        if self._git("cat-file", "-e", f"{heads[0]}^{{commit}}", check=False).returncode != 0:
            self._git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                      self.remote, heads[0])
        return heads[0]

    def head(self) -> tuple[str | None, str]:
        """``(commit, source)``: the remote's head (``remote``), else the local ref the
        live-check runner fetched (``local_ref``); ``(None, "unavailable")`` when neither can be
        read, ``(None, "remote")`` when the remote has no such ref."""
        try:
            return self._remote_head(), "remote"
        except BranchStoreError:
            pass
        found = self._git("rev-parse", "--verify", "--quiet", f"{self.local_ref}^{{commit}}",
                          check=False)
        if found.returncode == 0:
            return found.stdout.decode().strip(), "local_ref"
        return None, "unavailable"

    def manifest(self, commit: str) -> dict[str, Any]:
        shown = self._git("show", f"{commit}:manifest.json", check=False)
        try:
            value = json.loads(shown.stdout) if shown.returncode == 0 else None
        except ValueError:
            value = None
        copy = value.get("copy") if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get("schema") != SCHEMA
                or not isinstance(copy, dict) or copy.get("complete") is not True
                or not isinstance(value.get("parts"), list) or not value["parts"]):
            raise BranchStoreError("branch_manifest_invalid")
        return value

    def _part_blobs(self, commit: str) -> dict[str, str]:
        listed = self._text("ls-tree", commit)
        return {name: line.split()[2] for line in listed.splitlines()
                for name in [line.split("\t", 1)[1]] if name != "manifest.json"}

    def fetch(self) -> dict[str, Any]:
        """Refresh the local copy of the ref (the live-check runner, with credentials, before the
        checks: a check may not be able to read the remote itself). The ref is named:
        no default refspec carries it."""
        self._assert_private_remote()
        result = self._git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                           self.remote, f"+{self.ref}:{self.local_ref}", check=False)
        return {"fetched": result.returncode == 0}

    def attestation(self) -> dict[str, Any] | None:
        """The live-check runner's ``plx-mirror-attestation/v1`` record for this ref on the
        fetched results branch (``results_commit``: the results commit it was
        read from); None when absent or malformed."""
        live = self._git("rev-parse", "--verify", "--quiet", f"{self.live_ref}^{{commit}}",
                         check=False)
        if live.returncode != 0:
            return None
        results_commit = live.stdout.decode().strip()
        shown = self._git("show", f"{results_commit}:{ATTESTATION_PATH}", check=False)
        try:
            record = json.loads(shown.stdout) if shown.returncode == 0 else None
        except ValueError:
            record = None
        if (not isinstance(record, dict) or record.get("schema") != ATTESTATION_SCHEMA
                or record.get("ref") != self.ref
                or not isinstance(record.get("commit"), str)
                or not _HEX40.fullmatch(record["commit"])
                or not isinstance(record.get("copy_sha256"), str)
                or not _HEX64.fullmatch(record["copy_sha256"])):
            return None
        return dict(record, results_commit=results_commit)

    # -- publish ---------------------------------------------------------------

    def newest_copy(self, store: mc.MirrorCaptureStore) -> dict[str, Any] | None:
        """The newest copy in ``store`` that the store itself verifies as complete."""
        best = None
        for path in sorted((store.root / "scopes").glob("*/manifest.json")):
            try:
                found = json.loads(path.read_text(encoding="utf-8"))
                manifest = store.load(found["relations"], tenant=found["tenant"])
            except (OSError, ValueError, KeyError, TypeError, MirrorError):
                continue
            if Path(manifest["path"]).parent != path.parent:
                continue  # verified, but another scope's copy: it is visited on its own
            key = (str(manifest["captures_as_of"]), str(manifest["persisted_at"]))
            if best is None or key > best[0]:
                best = (key, manifest)
        return best[1] if best else None

    def publish(self, store: mc.MirrorCaptureStore, environ: Mapping[str, str]
                ) -> dict[str, Any]:
        """Publish the newest complete copy of ``store`` as the ref's one commit.
        ``written`` false with ``reason`` ``no_complete_copy`` / ``unchanged`` /
        ``remote_newer``; ``commit`` and ``copy_sha256`` when the ref holds this copy
        (written or unchanged); ``replaced_invalid_head`` names why a head was replaced.
        ``BranchStoreError`` for a refusal (``secret_shaped_value``, ``secret_scan_failed``,
        ``copy_changed``, ``remote_not_private_repo``, ``push_refused``, ``cas_exhausted``)."""
        self._assert_private_remote()
        manifest = self.newest_copy(store)
        if manifest is None:
            return {"written": False, "reason": "no_complete_copy", "attempts": 0}
        copy = {k: v for k, v in manifest.items() if k != "path"}
        source = Path(manifest["path"])
        scan = _SecretScan(secret_values(environ))
        scan.feed(json.dumps(copy, sort_keys=True).encode())
        scan.tail = b""
        _scan_values(source, scan)
        with tempfile.TemporaryDirectory(prefix="plx-mirror-branch-") as work:
            parts, digest, size = self._compress(source, Path(work), scan)
            if scan.found:
                raise BranchStoreError("secret_shaped_value",
                                       f"{scan.found} secret-shaped match(es); not written")
            if (digest, size) != (copy["sha256"], copy["bytes"]):
                raise BranchStoreError("copy_changed",
                                       "the bytes compressed are not the verified copy")
            entries = [(part["name"], self._text("hash-object", "-w", "--",
                                                 str(Path(work) / part["name"])))
                       for part in parts]
        blobs = dict(entries)
        body = {"schema": SCHEMA, "copy": copy, "compression": "xz",
                "parts": [{k: p[k] for k in ("name", "sha256", "bytes")} for p in parts],
                "published_at": self.clock().astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        blob = self._git("hash-object", "-w", "--stdin",
                         input=json.dumps(body, indent=1, sort_keys=True).encode()
                         ).stdout.decode().strip()
        entries.append(("manifest.json", blob))
        tree = self._git("mktree", input="".join(
            f"100644 blob {sha}\t{name}\n" for name, sha in sorted(entries)).encode()
            ).stdout.decode().strip()
        message = (f"mirror: complete Portal mirror captured as of {copy['captures_as_of']}"
                   f" ({len(copy['relations'])} relations, {copy['row_count']} rows)")
        commit = self._git("commit-tree", tree, "-m", message,
                           env=self._identity()).stdout.decode().strip()
        mine = (str(copy["captures_as_of"]), str(copy["persisted_at"]))
        held = {"copy_sha256": copy["sha256"], "captures_as_of": copy["captures_as_of"]}
        for attempt in range(1, CAS_TRIES + 1):
            head = self._remote_head()
            invalid = None
            if head is not None:
                try:
                    theirs = self.manifest(head)
                except BranchStoreError as exc:
                    invalid = exc.code  # replaced below, never silently: it is reported
                else:
                    if theirs["copy"] == copy and self._part_blobs(head) == blobs:
                        return dict(held, written=False, reason="unchanged", attempts=attempt,
                                    commit=head)
                    if (str(theirs["copy"].get("captures_as_of")),
                            str(theirs["copy"].get("persisted_at"))) > mine:
                        return {"written": False, "reason": "remote_newer",
                                "attempts": attempt}
            if self._push(commit, head):
                return dict(held, written=True, attempts=attempt, commit=commit,
                            parts=len(parts), bytes=sum(p["bytes"] for p in parts),
                            relation_count=len(copy["relations"]),
                            **({"replaced_invalid_head": invalid} if invalid else {}))
        raise BranchStoreError("cas_exhausted", f"ref kept changing over {CAS_TRIES} tries")

    def attest(self, report: Mapping[str, Any], path: Path) -> bool:
        """Write the attestation record of the copy a ``publish`` left on the ref to
        ``path``; False (nothing written) when the ref does not hold this copy."""
        if not report.get("commit"):
            return False
        mc._write_json(path, {
            "schema": ATTESTATION_SCHEMA, "ref": self.ref, "commit": report["commit"],
            "copy_sha256": report["copy_sha256"], "captures_as_of": report["captures_as_of"],
            "recorded_at": self.clock().astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
        return True

    def _push(self, commit: str, old: str | None) -> bool:
        """Move the ref from ``old`` (None: absent) to ``commit``; False when another
        writer moved it first."""
        result = self._git("push", "--porcelain", self.remote,
                           f"--force-with-lease={self.ref}:{old or ''}",
                           f"{commit}:{self.ref}", check=False)
        if result.returncode == 0:
            return True
        rejected = [line for line in result.stdout.decode().splitlines()
                    if line.startswith("!\t")]
        if rejected and all(re.search(r"\((%s)" % "|".join(_RACE), line)
                            for line in rejected):
            return False
        raise BranchStoreError("push_refused")

    def _identity(self) -> dict[str, str]:
        configured = {f: bool(self._text("config", "--get", f"user.{f.lower()}", check=False))
                      for f in ("NAME", "EMAIL")}
        return {var: value for var, value in IDENTITY.items()
                if not os.environ.get(var) and not configured[var.rsplit("_", 1)[1]]}

    @staticmethod
    def _compress(source: Path, work: Path, scan: _SecretScan
                  ) -> tuple[list[dict[str, Any]], str, int]:
        """The parts, and the sha256 and size of the bytes actually compressed."""
        parts: list[dict[str, Any]] = []
        handle = None
        whole = sha256()
        size = 0

        def write(data: bytes) -> None:
            nonlocal handle
            while data:
                if handle is None or parts[-1]["bytes"] >= PART_BYTES:
                    if handle is not None:
                        handle.close()
                    name = f"{PART_PREFIX}{len(parts):03d}"
                    parts.append({"name": name, "bytes": 0, "digest": sha256()})
                    handle = (work / name).open("wb")
                room = PART_BYTES - parts[-1]["bytes"]
                chunk, data = data[:room], data[room:]
                handle.write(chunk)
                parts[-1]["bytes"] += len(chunk)
                parts[-1]["digest"].update(chunk)

        compressor = lzma.LZMACompressor(preset=6)
        try:
            with source.open("rb") as src:
                for block in iter(lambda: src.read(CHUNK), b""):
                    scan.feed(block)
                    whole.update(block)
                    size += len(block)
                    write(compressor.compress(block))
            write(compressor.flush())
        finally:
            if handle is not None:
                handle.close()
        for part in parts:
            part["sha256"] = part.pop("digest").hexdigest()
        return parts, whole.hexdigest(), size

    # -- restore ---------------------------------------------------------------

    def restore_into(self, store: mc.MirrorCaptureStore, relations: Sequence[str], *,
                     tenant: str = "plx") -> dict[str, Any]:
        """Install the ref's copy into ``store`` when the live-check runner attested it, it covers
        ``relations`` for ``tenant`` and the store holds no copy of its scope.
        ``status``: ``restored``, ``present`` (the store has its own), ``absent`` (no
        ref, or a copy of another scope), ``unavailable`` (neither the remote nor a local
        ref could be read) or ``refused`` with ``reason`` (``remote_not_private_repo``,
        ``unattested``: not the commit and copy the live-check runner's attestation names, or a
        manifest, digest, size or stream that does not verify)."""
        try:
            self._assert_private_remote()
        except BranchStoreError as exc:
            return {"branch_source": "unavailable", "status": "refused", "reason": exc.code}
        commit, source = self.head()
        out: dict[str, Any] = {"branch_source": source}
        if commit is None:
            return dict(out, status="unavailable" if source == "unavailable" else "absent")
        attested = self.attestation()
        if attested is None or attested["commit"] != commit:
            return dict(out, status="refused", reason="unattested")
        try:
            body = self.manifest(commit)
        except BranchStoreError as exc:
            return dict(out, status="refused", reason=exc.code)
        copy = body["copy"]
        if copy.get("sha256") != attested["copy_sha256"]:
            return dict(out, status="refused", reason="unattested")
        scope = copy.get("relations") or []
        if copy.get("tenant") != tenant or not set(relations) <= set(scope):
            return dict(out, status="absent", reason="other_scope")
        folder = store.root / "scopes" / mc.scope_id(scope)
        if (folder / "manifest.json").exists():
            return dict(out, status="present")
        name = Path(str(copy.get("file"))).name
        size = copy.get("bytes")
        if name != copy.get("file") or not re.fullmatch(r"mirror-[0-9a-f]{16}\.sqlite", name) \
                or type(size) is not int or not 0 < size <= MAX_COPY_BYTES:
            return dict(out, status="refused", reason="branch_manifest_invalid")
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".branch-{secrets.token_hex(6)}.tmp"
        try:
            self._decompress(commit, body["parts"], tmp, copy)
            os.replace(tmp, folder / name)
        except BranchStoreError as exc:
            tmp.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                folder.rmdir()  # leave no empty scope behind
            return dict(out, status="refused", reason=exc.code)
        attestation = {k: attested[k] for k in ("schema", "commit", "copy_sha256",
                                                "results_commit")}
        manifest = dict(copy, restored_from={"ref": self.ref, "commit": commit,
                                             "attestation": attestation})
        mc._write_json(folder / "manifest.json", manifest)
        return dict(out, status="restored", captures_as_of=copy["captures_as_of"],
                    relation_count=len(scope))

    def _decompress(self, commit: str, parts: Sequence[Any], target: Path,
                    copy: Mapping[str, Any]) -> None:
        """The parts, decompressed into ``target``; typed refusals: a part digest
        (``branch_part_digest_mismatch``), a stream that is corrupt or continues past its
        end (``branch_copy_corrupt``), ends early (``branch_copy_truncated``) or
        decompresses past the copy's size (``branch_copy_oversized``)."""
        limit = copy["bytes"]
        decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=LZMA_MEMLIMIT)
        whole = sha256()
        size = 0
        with target.open("wb") as out, tempfile.TemporaryDirectory() as work:
            def emit(data: bytes) -> None:
                nonlocal size
                size += len(data)
                if size > limit:
                    raise BranchStoreError("branch_copy_oversized")
                whole.update(data)
                out.write(data)

            for index, part in enumerate(parts):
                name = part.get("name") if isinstance(part, dict) else None
                if name != f"{PART_PREFIX}{index:03d}":
                    raise BranchStoreError("branch_manifest_invalid")
                raw = Path(work) / name
                with raw.open("wb") as handle:
                    shown = self._git("cat-file", "blob", f"{commit}:{name}", check=False,
                                      stdout=handle)
                if shown.returncode != 0 or _file_sha(raw) != part.get("sha256"):
                    raise BranchStoreError("branch_part_digest_mismatch")
                try:
                    with raw.open("rb") as handle:
                        for block in iter(lambda: handle.read(CHUNK), b""):
                            emit(decompressor.decompress(block, max_length=CHUNK))
                            while not decompressor.needs_input and not decompressor.eof:
                                emit(decompressor.decompress(b"", max_length=CHUNK))
                            if decompressor.unused_data:
                                raise BranchStoreError("branch_copy_corrupt")
                except (lzma.LZMAError, EOFError):
                    raise BranchStoreError("branch_copy_corrupt") from None
                raw.unlink()
        if not decompressor.eof:
            raise BranchStoreError("branch_copy_truncated")
        if whole.hexdigest() != copy.get("sha256") or size != limit:
            raise BranchStoreError("branch_copy_digest_mismatch")


def _file_sha(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None, *,
         environ: Mapping[str, str] | None = None) -> int:
    """``publish``: the live-check runner, after the daily checks, with its credentials and its
    ``PLX_WORKBENCH_STATE_DIR``, ``--attest`` into its results-branch checkout;
    ``fetch``: before them. Prints counts, enums and commit/digest hex only."""
    parser = argparse.ArgumentParser(prog="python3 -m integrations.mirror_branch_store",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("publish", "fetch"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--ref", default=REF)
    parser.add_argument("--state-dir", default=None,
                        help="publish: used when PLX_WORKBENCH_STATE_DIR is not set")
    parser.add_argument("--attest", default=None,
                        help=f"publish: write the attestation record here (the live-check runner "
                             f"commits it as {ATTESTATION_PATH} on {LIVE_BRANCH})")
    args = parser.parse_args(argv)
    if args.attest is not None and args.command != "publish":
        parser.error("--attest applies to publish only")
    env = os.environ if environ is None else environ
    try:
        branch = MirrorBranch(args.repo, remote=args.remote, ref=args.ref)
        if args.command == "fetch":
            report = branch.fetch()
        else:
            state = mc.default_state_dir(env) or args.state_dir
            if state is None:
                raise BranchStoreError("state_dir_required")
            report = branch.publish(mc.MirrorCaptureStore(state), env)
            if args.attest is not None:
                report["attested"] = branch.attest(report, Path(args.attest))
    except BranchStoreError as exc:
        print(json.dumps({"error": exc.code, "command": args.command}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


__all__ = ["ATTESTATION_PATH", "ATTESTATION_SCHEMA", "BranchStoreError", "MirrorBranch",
           "PART_BYTES", "REF", "SCHEMA", "main", "private_url", "secret_values"]

if __name__ == "__main__":
    raise SystemExit(main())
