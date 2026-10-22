"""The last complete Portal source-mirror capture, kept outside Git and served stale
while the live Portal Workbench API is unavailable.

A run that builds its mirror from the Portal API fails as a
whole (``MirrorError``) when the API answers non-2xx or cannot be reached (since
2026-10-03 03:33Z ``GET /v1/health`` answers HTTP 500 ``internal``). This module keeps
the last complete capture of a declared relation scope and serves it, labelled stale,
when a refresh fails that way.

Where (captures never go to Git or logs):
the Workbench's credentialed capture store, ``<state dir>/mirror-capture`` beside the
mail capture's ``<state dir>/mail-capture`` (``integrations/capture_mail.py``). The
state directory is checked outside every git work tree by
``capture_mail.assert_outside_git`` before anything is created. Without an explicit
directory, ``default_state_dir`` reads an explicitly set ``$PLX_WORKBENCH_STATE_DIR``
and nothing else (no home-directory default).

Layout::

    <root>/history.json                               plx-mirror-capture-history/v1
    <root>/work/mirror.sqlite                         resumable working mirror
    <root>/scopes/<scope id>/manifest.json            plx-mirror-capture/v1
    <root>/scopes/<scope id>/mirror-<sha16>.sqlite    the persisted copy

Complete means every relation of the scope was captured by the sync that is persisted
(``outcome: captured``). Only then is the mirror copied (SQLite online backup, one
read snapshot), pruned to the scope (other relations, staged pages and unfinished
traversals removed) and published: the copy is renamed into place first and the
manifest, which names its sha256, is replaced last. A copy that is not complete is
never written, and one that does not verify (manifest not complete, digest mismatch,
a scope relation without a capture) is refused on load (``persisted_incomplete``); no
copy at all is ``missing_mirror``.

Serving: ``SourceMirror`` itself fills one relation it never held from the newest
verified copy holding it when a read (``rows``/``resolve``) cannot fill it live
because the source is out, marked stale per relation and read live again later
(every product path that reads through the case runtime's mirror gets it: the MCP
host's ``plx.find``, the case tools, the case scripts). Opt-in: the store is injected
(``SourceMirror(persisted_store=...)``); the case runtime injects the one an
explicitly set ``PLX_WORKBENCH_STATE_DIR`` names, and a mirror without a client never
reads one. Rows from a copy answer questions, labelled; they never satisfy an action
precondition (``operating/capability_guards.current_reader``).
``sync_or_persisted``, for a path that
syncs a declared scope first: a health check, then the sync. When the source fails
(non-2xx, unreachable, throttled past the policy) before any scope relation is
captured, the newest verified copy whose scope covers the request is restored to a
separate file and opened offline. Its mirror marks every answer stale with the
machine-readable ``source_freshness`` and the label ``stale_label`` builds, never
live (``integrations/source_mirror.py`` ``PERSISTED_FALLBACK_META``). A sync that
captured some relations and not others is served live and incomplete as before, never
mixed with a persisted copy. Audiences (per-employee trimming) apply to the copy
exactly as to a fresh mirror: the copy is the sync principal's capture, and every read
is checked against the caller's audience.

``history.json`` holds counts, scope ids and timestamps only (runs noted, copies
published and for which scopes, when):
the evidence that the store survived earlier runs and whether it ever held a complete
copy, so a caller can tell "this store never captured a complete sync" (nothing to
serve yet) from "a copy was published here and the fallback still failed".

No capture is an atomic upstream snapshot and absence never proves a deletion; the
manifest says so. ``captures_as_of`` is the oldest completed refresh among the scope
relations (the copy is only as fresh as its stalest relation).

Evidence: module tests use the injected Workbench transport (``injected-transport``);
the live behaviour is ``live-unverified`` until ``verify/live-checks/
mirror.persisted_fallback.json`` records a result.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from .capture_mail import assert_outside_git
from .source_mirror import PERSISTED_FALLBACK_META, MirrorError, SourceMirror

STORE_DIR = "mirror-capture"
MANIFEST_SCHEMA = "plx-mirror-capture/v1"
HISTORY_SCHEMA = "plx-mirror-capture-history/v1"
STATE_ENV = "PLX_WORKBENCH_STATE_DIR"
LABEL = "source-mirror captures as of {as_of}, live source unavailable ({status}, {code})"
# Mirror failures that mean the live source did not answer (non-2xx, unreachable,
# throttled past the policy), as opposed to a refusal or a defect of one relation.
LIVE_FAILURES = frozenset({"source_unavailable", "source_throttled", "statement_timeout",
                           "page_server_error"})
# Tables holding one relation's capture; everything outside the scope is pruned.
_RELATION_TABLES = ("relation_state", "row_current", "row_version", "identifier_index",
                    "refresh_failure", "relation_lineage", "capture", "absence_scan",
                    "sync_progress", "stage_row", "row_removal", "relation_soft_delete")


def default_state_dir(env: Mapping[str, str]) -> Path | None:
    """The Workbench state directory an explicitly set ``PLX_WORKBENCH_STATE_DIR``
    names (never in Git); ``None`` when it is not set: no default, so a test or an
    evaluation host never reads a host store by accident."""
    return Path(env[STATE_ENV]) if env.get(STATE_ENV) else None


def store_from_env(env: Mapping[str, str]) -> "MirrorCaptureStore | None":
    state = default_state_dir(env)
    return MirrorCaptureStore(state) if state is not None else None


def is_outage(code: str | None, status: int | None) -> bool:
    """The live source did not answer: a live failure that is unreachable (no HTTP
    status), 5xx or 429. 401/403/404 are refusals (a revoked key is not an outage)."""
    return code in LIVE_FAILURES and (status is None or status >= 500 or status == 429)


def stale_label(captures_as_of: str, http_status: int | None, error_code: str) -> str:
    """The one human label of a persisted copy served instead of the live source."""
    return LABEL.format(as_of=captures_as_of,
                        status=http_status if http_status is not None else "no_http_status",
                        code=error_code)


def scope_id(relations: Iterable[str]) -> str:
    return sha256("\n".join(sorted(set(relations))).encode()).hexdigest()[:16]


def _file_sha(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _remove_sqlite(path: Path) -> None:
    for name in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        name.unlink(missing_ok=True)


class MirrorCaptureStore:
    """``<state_dir>/mirror-capture``: the working mirror and the persisted copies."""

    def __init__(self, state_dir: str | Path, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.root = assert_outside_git(state_dir) / STORE_DIR
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def work_path(self) -> Path:
        return self.root / "work" / "mirror.sqlite"

    def _scope_dir(self, relations: Iterable[str]) -> Path:
        return self.root / "scopes" / scope_id(relations)

    # -- history (counts and timestamps only) --------------------------------

    @property
    def history_path(self) -> Path:
        return self.root / "history.json"

    def history(self) -> dict[str, Any]:
        """``runs`` noted and copies ``persisted`` in this store so far, the
        ``persisted_scopes`` (scope ids) ever published, and the times of the first run
        and the last publication; zeros when there is none (a new store, or a state
        directory that did not survive the last run)."""
        try:
            value = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = None
        if not isinstance(value, dict) or value.get("schema") != HISTORY_SCHEMA:
            value = {}
        counts = {key: value[key] if isinstance(value.get(key), int) else 0
                  for key in ("runs", "persisted")}
        scopes = value.get("persisted_scopes")
        scopes = sorted({x for x in scopes if isinstance(x, str)}) \
            if isinstance(scopes, list) else []
        return {"schema": HISTORY_SCHEMA, **counts, "persisted_scopes": scopes,
                "first_run_at": value.get("first_run_at"),
                "last_persisted_at": value.get("last_persisted_at")}

    def _update_history(self, **changes: Any) -> dict[str, Any]:
        history = self.history()
        history.update(changes)
        self.root.mkdir(parents=True, exist_ok=True)
        _write_json(self.history_path, history)
        return history

    def note_run(self) -> dict[str, Any]:
        """Count one run against this store; returns the history as it was before."""
        before = self.history()
        self._update_history(runs=before["runs"] + 1,
                             first_run_at=before["first_run_at"] or self._now())
        return before

    def _now(self) -> str:
        return self.clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # -- persist -------------------------------------------------------------

    def persist(self, mirror_path: str | Path, relations: Sequence[str],
                sync_report: Mapping[str, Any]) -> dict[str, Any]:
        """Publish the mirror at ``mirror_path`` as the copy of ``relations`` when
        ``sync_report`` (``SourceMirror.sync``) captured every one of them; otherwise
        nothing is written and the reason is returned. Counts only."""
        scope = sorted(set(relations))
        captured = {run["relation"] for run in sync_report.get("runs", ())
                    if run.get("outcome") == "captured"}
        missing = [name for name in scope if name not in captured]
        if not scope or missing:
            return {"persisted": False, "reason": "incomplete", "missing_count": len(missing),
                    "relation_count": len(scope)}
        folder = self._scope_dir(scope)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".mirror-{secrets.token_hex(6)}.tmp"
        try:
            meta = self._snapshot(Path(mirror_path), tmp, scope)
            digest = _file_sha(tmp)
            final = folder / f"mirror-{digest[:16]}.sqlite"
            os.replace(tmp, final)
        except MirrorError:
            return {"persisted": False, "reason": "incomplete", "missing_count": None,
                    "relation_count": len(scope)}
        finally:
            _remove_sqlite(tmp)
        manifest = {"schema": MANIFEST_SCHEMA, "complete": True, "relations": scope,
                    "captures_as_of": meta["captures_as_of"],
                    "persisted_at": self._now(),
                    "tenant": meta["tenant"], "mirror_generation": meta["generation"],
                    "source_digest": meta["source_digest"],
                    "file": final.name, "sha256": digest, "bytes": final.stat().st_size,
                    "row_count": meta["row_count"],
                    "atomic_upstream_snapshot": False, "deletions_proven": False}
        _write_json(folder / "manifest.json", manifest)
        for old in folder.glob("mirror-*.sqlite"):
            if old.name != final.name:
                _remove_sqlite(old)
        history = self.history()
        self._update_history(persisted=history["persisted"] + 1,
                             persisted_scopes=sorted({*history["persisted_scopes"],
                                                      folder.name}),
                             last_persisted_at=manifest["persisted_at"])
        return {"persisted": True, "relation_count": len(scope),
                "captures_as_of": manifest["captures_as_of"], "row_count": meta["row_count"]}

    @staticmethod
    def _snapshot(source: Path, target: Path, scope: Sequence[str]) -> dict[str, Any]:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(str(target), isolation_level=None)
        try:
            src.backup(dst)
            marks = ",".join("?" for _ in scope)
            dst.execute("BEGIN IMMEDIATE")
            for table in _RELATION_TABLES:
                dst.execute(f"DELETE FROM {table} WHERE relation NOT IN ({marks})", scope)
            # Unfinished traversals and their staged pages were never served.
            dst.execute("DELETE FROM stage_row WHERE relation IS NOT NULL")
            dst.execute("DELETE FROM sync_progress WHERE relation IS NOT NULL")
            dst.execute("DELETE FROM absence_seen WHERE scan_id NOT IN "
                        "(SELECT scan_id FROM absence_scan)")
            dst.execute("DELETE FROM capture_state WHERE capture_id NOT IN "
                        "(SELECT capture_id FROM capture)")
            dst.execute("DELETE FROM mirror_meta WHERE name = ?", (PERSISTED_FALLBACK_META,))
            dst.execute("COMMIT")
            meta = _verify(dst, scope)
            dst.execute("VACUUM")
            return meta
        finally:
            src.close()
            dst.close()

    # -- load and restore ----------------------------------------------------

    def load(self, relations: Sequence[str], *, tenant: str,
             source_digest: str | None = None) -> dict[str, Any]:
        """The manifest of the newest verified complete copy covering
        ``relations`` for ``tenant`` (and, when given, filled from the upstream whose
        ``source_digest`` the mirror pins). ``MirrorError`` ``missing_mirror`` when there is no copy at
        all, ``persisted_incomplete`` when copies exist but none is complete for the
        request and verifies."""
        wanted = set(relations)
        found: list[dict[str, Any]] = []
        refused = 0
        for path in sorted((self.root / "scopes").glob("*/manifest.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                refused += 1
                continue
            if (not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA
                    or manifest.get("complete") is not True
                    or manifest.get("tenant") != tenant
                    or (source_digest is not None
                        and manifest.get("source_digest") != source_digest)
                    or not wanted <= set(manifest.get("relations") or ())
                    or Path(str(manifest.get("file"))).name != manifest.get("file")):
                refused += 1
                continue
            found.append(dict(manifest, path=str(path.parent / str(manifest.get("file")))))
        any_copy = refused or found or any((self.root / "scopes").glob("*/mirror-*.sqlite"))
        for manifest in sorted(found, key=lambda m: str(m.get("captures_as_of")),
                               reverse=True):
            path = Path(manifest["path"])
            if not path.is_file() or _file_sha(path) != manifest.get("sha256"):
                continue
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                _verify(conn, manifest["relations"])
            except MirrorError:
                continue
            finally:
                conn.close()
            return manifest
        if any_copy:
            raise MirrorError("persisted_incomplete",
                              "no persisted source-mirror capture is complete for the "
                              "requested relations; it is never served as complete")
        raise MirrorError("missing_mirror", "no persisted source-mirror capture exists")

    def restore(self, manifest: Mapping[str, Any], dest: str | Path, *,
                http_status: int | None, error_code: str) -> dict[str, Any]:
        """Copy the persisted mirror to ``dest`` (outside Git) and mark it as served
        instead of the unavailable live source. Returns its ``source_freshness``."""
        dest = Path(dest)
        assert_outside_git(dest.parent)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _remove_sqlite(dest)
        tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
        shutil.copyfile(manifest["path"], tmp)
        freshness = {"state": "stale", "live": False, "served_from": "persisted_capture",
                     "captures_as_of": manifest["captures_as_of"],
                     "http_status": http_status, "error_code": error_code,
                     "complete": True, "relation_count": len(manifest["relations"]),
                     "label": stale_label(manifest["captures_as_of"], http_status,
                                          error_code)}
        conn = sqlite3.connect(str(tmp), isolation_level=None)
        try:
            conn.execute("INSERT OR REPLACE INTO mirror_meta VALUES (?, ?)",
                         (PERSISTED_FALLBACK_META, json.dumps(freshness, sort_keys=True)))
        finally:
            conn.close()
        os.replace(tmp, dest)
        return freshness


def _verify(conn: sqlite3.Connection, scope: Sequence[str]) -> dict[str, Any]:
    """Generation, tenant, row count and ``captures_as_of`` of a copy holding a
    completed capture of every scope relation; ``persisted_incomplete`` otherwise."""
    conn.row_factory = sqlite3.Row
    states = {row["relation"]: row for row in conn.execute(
        "SELECT relation, last_refresh_at, row_count FROM relation_state")}
    unfinished = conn.execute("SELECT COUNT(*) FROM sync_progress").fetchone()[0]
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'persisted_relation'"
                    ).fetchone() is not None:
        # Rows filled from an earlier copy are never re-published as a capture.
        unfinished += conn.execute("SELECT COUNT(*) FROM persisted_relation").fetchone()[0]
    meta = {row["name"]: row["value"] for row in conn.execute(
        "SELECT name, value FROM mirror_meta")}
    if not scope or any(name not in states for name in scope) or unfinished \
            or set(states) - set(scope) or PERSISTED_FALLBACK_META in meta:
        raise MirrorError("persisted_incomplete",
                          "the persisted copy does not hold a completed capture of "
                          "exactly its scope")
    return {"generation": meta.get("generation"), "tenant": meta.get("tenant"),
            "source_digest": meta.get("source_digest"),
            "captures_as_of": min(states[name]["last_refresh_at"] for name in scope),
            "row_count": sum(int(states[name]["row_count"]) for name in scope)}


def _run_failure(run: Mapping[str, Any]) -> tuple[int | None, str] | None:
    """``(http_status, error_code)`` of a failed sync run when the live source did
    not answer (the service's own code when it sent one)."""
    code = str(run.get("error") or "").split(":", 1)[0]
    if run.get("outcome") != "failed" or not is_outage(code, run.get("http_status")):
        return None
    return run.get("http_status"), run.get("upstream_error_code") or code


def sync_or_persisted(store: MirrorCaptureStore, client: Any, audience_map: Any,
                      actor: Any, *, relations: Sequence[str], tenant: str = "plx",
                      mirror_path: str | Path | None = None,
                      fallback_path: str | Path | None = None,
                      budget_seconds: float | None = None, workers: int = 1,
                      **options: Any) -> tuple[SourceMirror, dict[str, Any]]:
    """Sync ``relations`` live into ``mirror_path`` (default: the store's working
    mirror) and persist the copy when complete; when the live source does not answer,
    the persisted copy restored to ``fallback_path`` instead (offline, stale).

    Returns the open mirror to read and a counts-only report: ``served_from``
    (``live`` or ``persisted_capture``), ``complete``, ``persisted`` and, when served
    from the copy, ``source_freshness``. ``MirrorError`` ``missing_mirror`` /
    ``persisted_incomplete`` when the source failed and no usable copy exists; any
    other mirror refusal propagates unchanged."""
    scope = sorted(set(relations))
    mirror_path = Path(mirror_path) if mirror_path is not None else store.work_path
    # This helper decides itself when the copy is served; the mirror never switches.
    options = dict(options, persisted_store=None)
    mirror = SourceMirror(mirror_path, client, audience_map, tenant=tenant, **options)
    failure: tuple[int | None, str] | None = None
    try:
        mirror.check_health()
        report = mirror.sync(actor, relations=scope, budget_seconds=budget_seconds,
                             workers=workers)
    except MirrorError as exc:
        if not is_outage(exc.code, exc.status):
            mirror.close()
            raise
        failure, report = (exc.status, exc.upstream_code or exc.code), None
    except BaseException:
        mirror.close()
        raise
    if report is not None:
        runs = report["runs"]
        captured = [run for run in runs if run.get("outcome") == "captured"]
        failed = [run for run in runs if run.get("outcome") != "captured"]
        failures = [found for found in map(_run_failure, failed) if found is not None]
        # The copy answers only when nothing was captured and every failure is an
        # outage (a refusal such as 401/403 is reported, never papered over).
        if captured or not failures or len(failures) < len(failed):
            done = {run["relation"] for run in captured}
            complete = bool(scope) and set(scope) <= done
            persisted = (store.persist(mirror_path, scope, report) if complete else
                         {"persisted": False, "reason": "incomplete",
                          "missing_count": len(set(scope) - done),
                          "relation_count": len(scope)})
            return mirror, {"served_from": "live", "complete": complete,
                            "persisted": persisted, "relation_count": len(scope),
                            "captured_count": len(done)}
        failure = failures[0]
    mirror.close()
    manifest = store.load(scope, tenant=tenant, source_digest=mirror._source_digest)
    target = (Path(fallback_path) if fallback_path is not None else
              mirror_path.with_name(mirror_path.stem + ".persisted.sqlite"))
    freshness = store.restore(manifest, target, http_status=failure[0], error_code=failure[1])
    served = SourceMirror(target, None, audience_map, tenant=tenant, **options)
    return served, {"served_from": "persisted_capture", "complete": True,
                    "persisted": {"persisted": False, "reason": "live_source_unavailable"},
                    "relation_count": len(scope), "captured_count": 0,
                    "source_freshness": freshness}


__all__ = ["HISTORY_SCHEMA", "LABEL", "LIVE_FAILURES", "is_outage", "store_from_env", "MANIFEST_SCHEMA", "MirrorCaptureStore", "STATE_ENV",
           "STORE_DIR", "default_state_dir", "scope_id", "stale_label", "sync_or_persisted"]
