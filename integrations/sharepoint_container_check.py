"""Live check: captured SharePoint containers are projected member by member.

``verify/live-checks/capture.sharepoint_containers.json``.
One GET-only capture run of a narrow scope (``integrations.capture_sharepoint.run_cli``:
the AGENTS site's use-case folder, ``examples/sharepoint_capture_scope.containers
.live-check.json``) into the check's Workbench state, then the projection of that capture
alone (``ontology.project.SharePointCaptures``, whose ``containerMembers`` relation is
``ontology.project_graph.member_records``) into a temporary store under the same state
directory, deleted after the counts. Then, offline:

* ``member_records``: the ``containerMembers`` records the capture yields;
  ``container_members``: the ``ContainerMember`` objects projected from them (one run,
  so one version per item: any difference is a member lost, ``members_not_projected``);
  ``containers``: the distinct container versions they cite;
* ``members_by_state`` and ``members_unread`` (by reason): every member or part not read
  (``unread``, ``skipped_by_cap``) is counted with its reason, never dropped;
* ``members_typed``: typed documents (``documentFields``) cited by a member handle;
* ``members_resolved``: member handles that resolve offline (container gate, then the
  member's bytes) for the capture audience (one run: one handle per member object, so
  it equals ``container_members`` or a member is ``members_unresolved``);
* ``containers_unopened``: captured files named or typed as a container (zip, ``.msg``,
  ``.eml``) whose bytes were not captured (over the content cap, or unavailable), so
  they yield no member record; each is still a container (counted in ``containers``)
  whose members are one unread part with a typed reason (``members_by_state`` unread,
  ``members_unread`` ``container_exceeds_cap`` or ``container_unavailable``); counted
  over the capture's index, which stops at ``INDEX_LIMIT`` files: then
  ``containers_unopened_truncated`` is true, the count is a lower bound and the check
  fails as ``index_truncated``;
* ``leak_check``: members a no-grant actor and a folder-only actor (folder granted, item
  permissions required, no source identity) pass the tools' item gate for (must be 0).

Convergence (a folder larger than one ``--budget-seconds`` never
completed while every run started from an empty store). Without ``--state-dir`` the
capture store is the host's, kept across runs (``capture_mail.check_state_dir``:
``$PLX_WORKBENCH_STATE_DIR/sharepoint-containers-check``, else under ``$XDG_STATE_HOME``
or ``$HOME/.local/state``; an injected transport its own subdirectory; ``storage``). Its
cursor is the capture's own: a walk the budget cuts keeps its position (the folders left,
the children of the cut one already captured), so the next run resumes there and never
downloads a finished container again (an unchanged version is kept, not re-read). Each
run first spends the budget on the containers whose bytes an earlier run could not read
(``capture_sharepoint.run_cli`` ``retry_first``; ``cursor.containers_retried_first``):
each at most once a run, whatever failed plain files precede it, and in at most half the
budget left, then resumes the walk. ``containers_opened`` (containers whose members were read) and
``members_read`` (members in state ``read``) are recorded after every run in
``<state>/container-check-progress.json`` (counts only); ``progress`` gives the previous
run's counts beside this run's, and a run that is still incomplete yet moved none of
them (nor ``capture.contents_captured``) forward and did not move the cursor either
(``progress.walk_moved``: a held walk resumed and listed more folders, or finished
listing; ``walks_held`` and ``walk_folders_listed`` are recorded too) fails as
``capture_stalled``.
Each captured version is projected alone and verified once: its member counts are kept
in ``<state>/container-check-ledger.json`` (counts and digests only), keyed by a digest
of the version's row, so new bytes, permissions or a removal re-verify it, and voided by
any change of the audience file, the principal or the roots. A run verifies new versions
until ``VERIFY_SECONDS`` after the capture, or with ``--budget-seconds`` until
``VERIFY_SECONDS`` after the budget ends, so the budget the capture left unused is spent
verifying (at least one), and defers the rest
(``projection_deferred``; ``projection.items_verified``/``items_carried``/
``items_deferred``), so a growing kept store never pushes a run past the spec's timeout.
A carried container's leak counts are re-checked every run through its own gate.
``cursor`` says what the next run resumes: ``held`` (a walk position is kept),
``folders_left``, ``items_held`` and ``containers_pending`` (containers whose bytes are
still to be retried).

``ok`` is true only with no ``reasons``; each failure is a named reason (``REASONS``),
so a run without container members fails as ``containers_unopened`` when a container
was captured but not opened, else as ``no_container_members``, never passes empty. The
capture's own offline check (``resolve_check``: every captured version resolves and its
pinned bytes re-hash) and its ``ok`` are read too: a plain file that fails them is
``capture_unresolved`` (pinned bytes that fail the re-hash are counted by code in
``capture_resolve_check.content_bytes_failed``), any other capture failure
``capture_failed``. Prints counts, booleans and enums only: never a name, path,
identifier or text.
Live-unverified until the live check records a result; the tests run it over an injected
transport (``verify/tests/test_live_sharepoint_containers.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Callable, Mapping

from integrations import capture_sharepoint as cs
from integrations.capture_mail import assert_outside_git, check_state_dir
from integrations.captured_text import split_member
from integrations.live_source_guard import LiveSourceAudience
from ontology.mentions import cited_page
from ontology.types import Actor

CHECK = "capture.sharepoint_containers"
MEMBER_TYPE = "ContainerMember"
GAP_STATES = ("unread", "skipped_by_cap")
WITHHELD = "staff:sharepoint-containers-withheld"
FOLDER_ONLY = "staff:sharepoint-containers-folder-only"
# Failure reasons, in the order they are checked.
REASONS = ("capture_incomplete", "capture_stalled", "capture_leak", "capture_unresolved",
           "capture_failed", "projection_failed", "projection_deferred", "index_truncated",
           "containers_unopened", "no_container_members", "members_not_projected",
           "members_unresolved", "no_grant_sees_members", "folder_only_sees_members")
# The capture store's index bound (``SharePointCaptureStore.index``).
INDEX_LIMIT = cs.INDEX_LIMIT
# A captured file is a container by its name or type when its bytes are absent (a fixed
# table, as ``integrations.captured_text`` reads a zip, an Outlook .msg or a mail).
CONTAINER_SUFFIXES = (".zip", ".msg", ".eml")
CONTAINER_TYPES = frozenset({"application/zip", "application/x-zip-compressed",
                             "application/vnd.ms-outlook", "application/x-msg",
                             "message/rfc822"})
# The host state directory of the check, kept across its runs (``check_state_dir``).
CHECK_STATE = "sharepoint-containers-check"
PROGRESS_FILE = "container-check-progress.json"
PROGRESS_SCHEMA = "plx-sharepoint-container-progress/v1"
PROGRESS_COUNTS = ("containers_opened", "members_read", "contents_captured")
WALK_COUNTS = ("walks_held", "walk_folders_listed")   # the cursor's movement
# The member counts of each verified captured version (keyed by a digest of the
# version), so a run projects only versions not verified before.
LEDGER_FILE = "container-check-ledger.json"
LEDGER_SCHEMA = "plx-sharepoint-container-ledger/v1"
ITEM_COUNTS = ("member_records", "container_members", "members_typed",
               "members_resolved", "containers")
# Wall time a run spends verifying new versions after the capture (at least one
# version a run; the rest wait), so the whole run stays inside the spec's timeout.
VERIFY_SECONDS = 40.0
WORK_PREFIX = "plx-sharepoint-containers-"


def _is_container(record: Mapping[str, Any]) -> bool:
    """A file named or typed as a container (zip, Outlook .msg, mail)."""
    name, mime = record.get("name"), record.get("mime_type")
    return ((isinstance(name, str) and name.lower().endswith(CONTAINER_SUFFIXES))
            or (isinstance(mime, str)
                and mime.split(";", 1)[0].strip().lower() in CONTAINER_TYPES))


def _unopened_container(row: Mapping[str, Any]) -> str | None:
    """Why the members of a captured file named or typed a container were not read
    because its bytes were not captured (``container_exceeds_cap``,
    ``container_unavailable``), else None."""
    status = row.get("content_status")
    if status == cs.CAPTURED or not _is_container(row):
        return None
    return "container_exceeds_cap" if status == cs.EXCEEDS_CAP else "container_unavailable"


def _state_dir(args: argparse.Namespace, env: Mapping[str, str], *, injected: bool
               ) -> tuple[Path, bool, str | None]:
    """(the state directory, whether it is the host's kept across runs, which host state
    base): ``--state-dir`` as given, else the host's (``check_state_dir``; an injected
    transport its own subdirectory), outside every git tree, made 0700."""
    if args.state_dir is not None:
        return Path(args.state_dir).expanduser(), False, None
    state, base, kind = check_state_dir(env, CHECK_STATE, injected=injected)
    assert_outside_git(state)
    parts = state.relative_to(base).parts
    for n in range(1, len(parts) + 1):
        base.joinpath(*parts[:n]).mkdir(mode=0o700, parents=True, exist_ok=True)
    return state, True, kind


def _previous_progress(state: Path) -> dict[str, Any] | None:
    """The counts the previous run recorded; None when there are none or the file is not
    exactly what this check writes (an unknown file is never trusted as progress)."""
    try:
        doc = json.loads((state / PROGRESS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != PROGRESS_SCHEMA or not all(
            type(doc.get(key)) is int and doc[key] >= 0 for key in (*PROGRESS_COUNTS, "runs")):
        return None
    if not all(type(doc.get(key, 0)) is int and doc.get(key, 0) >= 0
               for key in WALK_COUNTS):   # absent in records before the cursor counts
        return None
    return doc


def _record_progress(state: Path, counts: Mapping[str, int]) -> None:
    """Counts only, written whole (a crash leaves the previous record)."""
    path = state / PROGRESS_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"schema": PROGRESS_SCHEMA, **counts}, sort_keys=True),
                   encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _cursor(capture: Mapping[str, Any]) -> dict[str, Any]:
    """Where the next run resumes: the walk positions the capture's last run kept."""
    held = [root["walk_position"] for run in capture["runs"][-1:]
            for target in run["targets"] for root in target["roots"]
            if root.get("walk_position")]
    return {"held": bool(held),
            "folders_left": sum(p.get("folders_left", 0) for p in held),
            "items_held": sum(p.get("items_held", 0) for p in held),
            "containers_retried_first": capture.get("retried_first", 0)}


def _walk_cursor(capture: Mapping[str, Any]) -> dict[str, int]:
    """The kept walk positions of the capture's last run: how many, and the folders
    their walks listed so far (cumulative over the runs that resumed them)."""
    held = [root["walk_position"] for run in capture["runs"][-1:]
            for target in run["targets"] for root in target["roots"]
            if root.get("walk_position")]
    return {"walks_held": len(held),
            "walk_folders_listed": sum(int(p.get("folders_listed", 0)) for p in held)}


def _walk_moved(previous: Mapping[str, Any] | None, walk: Mapping[str, int]) -> bool:
    """The cursor moved since the previous run: a walk it held resumed and listed more
    folders, or finished listing. Listing the same folders again (a walk restarted from
    the root, cut at the same place or never cut) is not movement."""
    if previous is None or not previous.get("walks_held", 0):
        return False
    return (walk["walks_held"] < previous["walks_held"]
            or walk["walk_folders_listed"] > previous.get("walk_folders_listed", 0))


def _current_items(store: Any) -> list[tuple[int, str, str]]:
    """(version id, l1 handle, ledger key) of every version the projection reads (the
    latest interval of each handle, ``SharePointCaptures``), in version order. The key
    digests every column but ``last_seen_at``, so new bytes, text, permissions or a
    removal give the version a new key and it is verified again."""
    rows = store.db.execute(
        "SELECT * FROM items WHERE version_id IN (SELECT MAX(version_id) FROM items "
        "GROUP BY l1_handle) ORDER BY version_id").fetchall()
    out = []
    for row in rows:
        fields = {k: row[k] for k in row.keys() if k != "last_seen_at"}
        key = hashlib.sha256(json.dumps(fields, sort_keys=True, default=str)
                             .encode()).hexdigest()
        out.append((row["version_id"], row["l1_handle"], key))
    return out


def _ledger_context(store: Any, args: argparse.Namespace) -> str:
    """What every ledger entry was verified under: the ledger layout, the audience file,
    the principal and the captured roots (the folder-only gate's). Any change voids the
    ledger. An item's own permissions are in its key (``_current_items``)."""
    digest = hashlib.sha256(LEDGER_SCHEMA.encode())
    digest.update(Path(args.live_audience).read_bytes())
    digest.update(json.dumps([str(args.principal), [list(r) for r in store.db.execute(
        "SELECT drive_id, root_path FROM roots ORDER BY drive_id, root_path")]]).encode())
    return digest.hexdigest()


def _load_ledger(state: Path, context: str) -> dict[str, dict[str, Any]]:
    """The verified items' counts, or none when the file is missing, malformed or was
    verified under another context (an unknown file is never trusted)."""
    try:
        doc = json.loads((state / LEDGER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("schema") != LEDGER_SCHEMA \
            or doc.get("context") != context or not isinstance(doc.get("items"), dict):
        return {}
    items = {}
    for key, entry in doc["items"].items():
        if not (isinstance(entry, dict) and all(
                type(entry.get(k)) is int and entry[k] >= 0 for k in ITEM_COUNTS)
                and all(isinstance(entry.get(k), dict) and all(
                    isinstance(r, str) and type(n) is int and n >= 0
                    for r, n in entry[k].items()) for k in ("by_state", "unread"))):
            return {}
        items[key] = entry
    return items


def _save_ledger(state: Path, context: str, items: Mapping[str, Any]) -> None:
    """Counts and enums only (keys are digests), written whole."""
    path = state / LEDGER_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"schema": LEDGER_SCHEMA, "context": context,
                               "items": items}, sort_keys=True), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _verify_item(files: Any, schema: Any, version_id: int, work: Path, store: Any,
                 checks: Mapping[str, Any]) -> dict[str, Any]:
    """Project one captured version alone (``SharePointCaptures.sample``): its
    ``containerMembers`` records by state, the projected members, typed documents cited
    by a member, members resolved offline and members a withheld or folder-only actor
    sees (raises ``ProjectionError``)."""
    from ontology.project import MEMBER_RELATION, build_store

    files.sample = lambda rows: (r for r in rows if r["version_id"] == version_id)
    by_state: dict[str, int] = {}
    unread: dict[str, int] = {}
    records = 0
    for record in files.records([MEMBER_RELATION])[MEMBER_RELATION]:
        records += 1
        member_state = str(record.row.get("state") or "unspecified")
        by_state[member_state] = by_state.get(member_state, 0) + 1
        if member_state in GAP_STATES:
            reason = str(record.row.get("reason") or "unspecified")
            unread[reason] = unread.get(reason, 0) + 1
    projection = work / f"item-{version_id}.sqlite"
    try:
        build_store(projection, schema, {"sharepoint": files})
        members, handles, typed = _projected(projection)
    finally:
        for leftover in work.glob(f"item-{version_id}.sqlite*"):
            leftover.unlink()
    actor, audience = checks["actor"], checks["audience"]
    return {"member_records": records, "by_state": by_state, "unread": unread,
            "container_members": members, "members_typed": typed,
            "containers": len({split_member(h)[0] for h in handles}),
            "members_resolved": sum(1 for h in handles
                                    if store.resolve(h, actor, audience) is not None),
            "no_grant": sum(1 for h in handles if checks["gate"](checks["withheld"], h)),
            "folder_only": sum(1 for h in handles
                               if checks["folder_gate"](checks["folder_actor"], h))}


def _projected(projection: Path) -> tuple[int, list[str], int]:
    """(projected ``ContainerMember`` objects, their member handles, typed documents
    other than members cited by a member handle)."""
    conn = sqlite3.connect(f"file:{projection}?mode=ro", uri=True)
    try:
        members = conn.execute(
            "SELECT o.object_id, v.l1_handle FROM objects o JOIN object_versions v ON "
            "v.object_id = o.object_id WHERE o.object_type = ? ORDER BY v.l1_handle",
            (MEMBER_TYPE,)).fetchall()
        typed = {object_id for object_id, handle in conn.execute(
            "SELECT o.object_id, v.l1_handle FROM objects o JOIN object_versions v ON "
            "v.object_id = o.object_id WHERE o.object_type != ?", (MEMBER_TYPE,))
            if split_member(cited_page(handle)[0]) is not None}
    finally:
        conn.close()
    return (len({object_id for object_id, _ in members}),
            sorted({handle for _, handle in members}), len(typed))


def _verify_deadline(budget: float | None, run_start: float, now: float) -> float:
    """With a budget, verification may also spend what the capture left unused, never
    past ``budget + VERIFY_SECONDS`` from the run's start (the spec's timeout allows it),
    and a capture that overran the budget still gets ``VERIFY_SECONDS``."""
    if budget is None:
        return now + VERIFY_SECONDS
    return max(now, run_start + budget) + VERIFY_SECONDS


def run(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
        sender: Any = None, sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None) -> dict[str, Any]:
    state, host_state, state_base = _state_dir(
        args, os.environ if environ is None else environ, injected=sender is not None)
    carried = cs.store_exists(state)
    clock = time.monotonic if monotonic is None else monotonic
    run_start = clock()
    previous = _previous_progress(state)
    capture = cs.run_cli(argparse.Namespace(
        state_dir=str(state), scope=args.scope, live_audience=args.live_audience,
        principal=args.principal, withheld_principal=WITHHELD,
        folder_only_principal=FOLDER_ONLY, runs=1, max_store_bytes=None,
        budget_seconds=args.budget_seconds,
        state_kept=state_base == "workbench_state_dir"), environ=environ, sender=sender, sleep=sleep,
        retry_first=_is_container, **({} if monotonic is None else {"monotonic": monotonic}))
    # A run killed mid-projection leaves its work directory: never carried.
    for stale in state.glob(WORK_PREFIX + "*"):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix=WORK_PREFIX, dir=state) as work:
        report = _report(args, state, Path(work), capture,
                         deadline=_verify_deadline(args.budget_seconds, run_start, clock()),
                         clock=clock)
    report["storage"] = {"host_state": host_state, "carried": carried,
                         "state_base": state_base}
    report["cursor"] = {**_cursor(capture),
                        "containers_pending": report["members_unread"].get(
                            "container_unavailable", 0)}
    counts = {"containers_opened": report["containers_opened"],
              "members_read": report["members_read"],
              "contents_captured": report["capture"]["contents_captured"]}
    complete = "capture_incomplete" not in report["reasons"]
    walk = _walk_cursor(capture)
    moved = _walk_moved(previous, walk)
    forward = complete or previous is None or moved or any(
        counts[key] > previous[key] for key in PROGRESS_COUNTS)
    report["progress"] = {"carried": carried, "previous": previous is not None,
                          "runs": (previous or {"runs": 0})["runs"] + 1,
                          **{f"{key}_before": None if previous is None else previous[key]
                             for key in PROGRESS_COUNTS},
                          "walk_folders_listed": walk["walk_folders_listed"],
                          "walk_folders_listed_before": None if previous is None
                          else previous.get("walk_folders_listed", 0),
                          "walk_moved": moved, "forward": forward}
    _record_progress(state, {**counts, **walk, "runs": report["progress"]["runs"]})
    if not forward:
        report["reasons"] = [r for r in REASONS
                             if r in report["reasons"] or r == "capture_stalled"]
        report["ok"] = False
    # The capture's typed later_ask scope blocks the check only while it is the whole
    # failure (capture_incomplete, with its deferred projection); any other reason fails.
    report["blocked_upstream"] = (
        capture.get("blocked_upstream", [])
        if set(report["reasons"]) <= {"capture_incomplete", "projection_deferred"} else [])
    return report


def _verify(args: argparse.Namespace, state: Path, work: Path, store: Any,
            report: dict[str, Any], failed: set[str], *, deadline: float,
            clock: Callable[[], float], checks: dict[str, Any]) -> None:
    """The member counts of every current version: carried from the ledger when it
    verified that exact version under the same context, else verified now one version
    at a time until ``deadline`` (at least one a run), the rest deferred to the next run
    (``projection_deferred``). A carried container's leak counts are re-checked every
    run through the container's own gate (members are gated by their container)."""
    from ontology import schema as schema_loader
    from ontology.project import ProjectionError, SharePointCaptures

    audience = checks["audience"]
    roots = [(r["drive_id"], r["root_path"]) for r in store.db.execute(
        "SELECT drive_id, root_path FROM roots ORDER BY drive_id, root_path")]
    # The tools' item gate (offline resolution, the container's gate first).
    checks["gate"] = store.item_gate(audience)
    checks["folder_gate"] = store.item_gate(cs.folder_only_audience(audience, FOLDER_ONLY,
                                                                    roots))
    context = _ledger_context(store, args)
    ledger = _load_ledger(state, context)
    current = _current_items(store)
    kept = {key: ledger[key] for _vid, _h, key in current if key in ledger}
    pending = [(vid, handle, key) for vid, handle, key in current if key not in kept]
    fresh: dict[str, dict[str, Any]] = {}
    projection = report["projection"]
    projection["items_carried"] = len(kept)
    try:
        if pending:
            schema = schema_loader.load_schema()
            with SharePointCaptures(state) as files:
                for vid, _handle, key in pending:
                    if fresh and clock() >= deadline:
                        break
                    fresh[key] = _verify_item(files, schema, vid, work, store, checks)
                    kept[key] = {k: v for k, v in fresh[key].items()
                                 if k not in ("no_grant", "folder_only")}
                    _save_ledger(state, context, kept)
    except ProjectionError as error:
        report["projection_error"] = error.code
        failed.add("projection_failed")
    if not pending:
        _save_ledger(state, context, kept)   # versions no longer current dropped
    projection["items_verified"] = len(fresh)
    projection["items_deferred"] = len(pending) - len(fresh)
    leak = {"no_grant_members_visible": 0, "folder_only_members_visible": 0}
    by_state, unread = report["members_by_state"], report["members_unread"]
    for _vid, handle, key in current:
        entry = kept.get(key)
        if entry is None:
            continue
        for count in ("member_records", "container_members", "members_typed",
                      "members_resolved", "containers"):
            report[count] += entry[count]
        for name, number in entry["by_state"].items():
            by_state[name] = by_state.get(name, 0) + number
        for name, number in entry["unread"].items():
            unread[name] = unread.get(name, 0) + number
        if key in fresh:
            leak["no_grant_members_visible"] += fresh[key]["no_grant"]
            leak["folder_only_members_visible"] += fresh[key]["folder_only"]
        elif entry["container_members"]:
            if checks["gate"](checks["withheld"], handle):
                leak["no_grant_members_visible"] += entry["container_members"]
            if checks["folder_gate"](checks["folder_actor"], handle):
                leak["folder_only_members_visible"] += entry["container_members"]
    report["leak_check"] = leak
    if report["projection_error"] is not None:
        return
    index = store.index(checks["actor"], audience, limit=INDEX_LIMIT)
    unopened = [why for why in (
        _unopened_container((store.resolve(item["l1_handle"], checks["actor"], audience,
                                           text=False) or {"row": {}})["row"])
        for item in index["items"]) if why]
    # Still containers: their members one unread part each, with the reason.
    report["containers_unopened"] = len(unopened)
    report["containers"] += len(unopened)
    if unopened:
        by_state["unread"] = by_state.get("unread", 0) + len(unopened)
        for why in unopened:
            unread[why] = unread.get(why, 0) + 1
    report["containers_unopened_truncated"] = index["truncated"] is not False


def _report(args: argparse.Namespace, state: Path, work: Path,
            capture: Mapping[str, Any], *, deadline: float = float("inf"),
            clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    summary = capture["summary"]
    report: dict[str, Any] = {
        "check": CHECK, "evidence_label": capture["evidence_label"],
        "read_only_guard": capture["read_only_guard"],
        "capture": {key: summary[key] for key in (
            "roots", "roots_complete", "files_current", "contents_captured",
            "content_exceeds_cap", "content_unavailable")},
        # The scope the capture left incomplete, typed (capture_sharepoint.incomplete_scope).
        "incomplete_scope": capture.get("incomplete_scope", []),
        "capture_leak_check": capture["leak_check"],
        "capture_resolve_check": capture["resolve_check"],
        "requests_sent": capture["requests_sent"],
        "containers": 0, "containers_unopened": 0, "containers_unopened_truncated": False,
        "container_members": 0, "member_records": 0, "members_typed": 0,
        "members_by_state": {}, "members_unread": {}, "members_resolved": 0,
        "projection_error": None,
        "leak_check": {"no_grant_members_visible": 0, "folder_only_members_visible": 0}}
    failed = set()
    if not summary["roots"] or summary["roots_complete"] != summary["roots"]:
        failed.add("capture_incomplete")
    if any(capture["leak_check"].values()):
        failed.add("capture_leak")
    resolve_check = capture["resolve_check"]
    if resolve_check["resolved_offline"] != resolve_check["handles_checked"] or \
            resolve_check["content_bytes_verified"] != resolve_check["contents_checked"]:
        failed.add("capture_unresolved")
    if capture["ok"] is not True and not failed:
        failed.add("capture_failed")
    audience = LiveSourceAudience.from_file(args.live_audience)
    actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
    withheld = Actor(audience.tenant, frozenset({WITHHELD}), "restricted")
    folder_actor = Actor(audience.tenant, frozenset({FOLDER_ONLY}), "restricted")
    report["projection"] = {"items_verified": 0, "items_carried": 0, "items_deferred": 0}
    try:
        store = cs.SharePointCaptureStore.open_read_only(state)
    except cs.CaptureError as exc:
        report["projection_error"] = ("sharepoint_capture_missing" if exc.code == "store_missing"
                                      else exc.code)
        failed.add("projection_failed")
        store = None
    if store is not None:
        try:
            _verify(args, state, work, store, report, failed, deadline=deadline, clock=clock,
                    checks={"actor": actor, "audience": audience, "withheld": withheld,
                            "folder_actor": folder_actor})
        finally:
            store.close()
    if report["projection_error"] is None:
        if report["containers_unopened_truncated"]:
            failed.add("index_truncated")
        if report["projection"]["items_deferred"]:
            failed.add("projection_deferred")
        if not report["container_members"]:
            # A truncated index proves no folder is free of containers.
            if report["containers_unopened"]:
                failed.add("containers_unopened")
            elif not report["containers_unopened_truncated"]:
                failed.add("no_container_members")
        if report["member_records"] != report["container_members"]:
            failed.add("members_not_projected")
        if report["members_resolved"] != report["container_members"]:
            failed.add("members_unresolved")
    if report["leak_check"]["no_grant_members_visible"]:   # whatever else failed
        failed.add("no_grant_sees_members")
    if report["leak_check"]["folder_only_members_visible"]:
        failed.add("folder_only_sees_members")
    report["containers_opened"] = report["containers"] - report["containers_unopened"]
    report["members_read"] = report["members_by_state"].get("read", 0)
    report["reasons"] = [reason for reason in REASONS if reason in failed]
    report["ok"] = not report["reasons"]
    return report


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None, sleep: Callable[[float], None] | None = None,
         monotonic: Callable[[], float] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.sharepoint_container_check",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", default=None,
                        help="Workbench state directory (outside every git work tree); "
                             "default the host's, kept across runs so each run resumes "
                             "the last")
    parser.add_argument("--scope", required=True, help="plx-sharepoint-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=cs.CAPTURE_PRINCIPAL)
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="wall-time bound of the capture; a root it cuts is incomplete")
    args = parser.parse_args(argv)
    report = run(args, environ=environ, sender=sender, sleep=sleep, monotonic=monotonic)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["ok"] or report["blocked_upstream"] else 1


if __name__ == "__main__":
    sys.exit(main())
