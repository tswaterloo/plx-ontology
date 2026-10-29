"""Capture breadth: one cycle over the whole capture scope (lane P7).

Owner decisions 5be438765457 and 93047ba19524: the Workbench reads every mailbox
and source in its capture scope and keeps what it captures in its own capture
stores under the Workbench state directory; nothing captured goes to Git, logs or
advisory records, and every reader is re-gated per mailbox and per item.

One cycle (``capture_cycle``) runs, in this order:

* the mail capture (``integrations.capture_mail``) over the mail scope
  (``integrations/mail_capture_scope.json``: every mailbox of the tenant directory
  except the scope's exclusion list, inbox and sent items; ING-06), each folder
  from its stored delta link once its first round reached one, a mailbox new in the
  directory from its first round;
* the SharePoint capture (``integrations.capture_sharepoint``) over its scope
  (``integrations/sharepoint_capture_scope.json``: the QA/QC library, Customer
  Documents, the AGENTS use cases), each root a full walk first and then its
  drive's change feed.

Its completeness is per folder: ``complete_bound`` / ``bound_total`` over every mail
folder and SharePoint root of the scope, with a named reason for each incomplete one
(``folders``). A time budget (``budget_seconds``; the CLI's ``--budget-seconds`` shared
by its cycles) bounds a cycle's wall time. Each source has a declared share of it
(``sharepoint_share``, default ``SHAREPOINT_SHARE``; mail the rest): the mail capture
runs first within its share, so it cannot spend SharePoint's, and the SharePoint capture
gets its share plus whatever mail left unused; what SharePoint leaves unused stays with
the CLI's later cycles, where mail spends its share of it first. Every folder or root
the budget cuts is incomplete with reason ``time_budget`` and resumes next cycle (a mail
folder from its stored cursor, a SharePoint walk from its stored position), so a large
scope ends in a reported partial state, never in the caller's timeout
(``summary.folders_budget_cut``; a later cycle's round that continues or starts what a
budget cut is ``summary.rounds_resumed``, never a rerun). Both captures take their units
in one scheduler's order (``capture_mail.schedule_units``): a unit cut mid-round or
mid-walk first (started earliest first), then the others not complete in the scope's
order, then complete units' deltas (oldest position first), so a scope larger than one
budget completes over several cycles; a complete unit the budget reached first stays complete
(``deferred``, ``summary.deltas_deferred``) and its cycle is not complete. A source that
could not start (a mailbox not opened or ``forbidden``, a site not resolved) is bound
and incomplete, never left out of the denominator.

``locate_sources`` resolves the source ids of an exposed-sources document
(``plx-eval-exposed-sources/v1``, e.g. ``evals/exposed/sources.json``; data, never
code) against the captures, for one reader: a drive item by its (drive, item) key
and pinned hash, an archive member inside a captured archive by its member path and
SHA-256, a mail message by its received time (pinned REST ids are not the
immutable ids the capture keeps; two candidates are ``ambiguous``, never a pick) and
a mail attachment by name and SHA-256 within its located message. Nothing is read
from a source.

CLI (credentialed live check; counts, booleans and enums only, never addresses,
names, paths, ids or content)::

    python3 -m integrations.graph_read capture-breadth --state-dir D \\
        --live-audience examples/capture_breadth_audience.live-check.json --runs 2

Live-unverified until ``devloop/live-checks/capture.breadth.json`` passes.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import io
from itertools import pairwise
import json
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import zipfile

from ontology.types import Actor

from . import capture_mail, capture_sharepoint
from .live_source_guard import LiveSourceAudience

SOURCES_SCHEMA = "plx-eval-exposed-sources/v1"
CAPTURE_PRINCIPAL = "svc:capture"
MAX_MEMBER_BYTES = 20_000_000
RESOLVED = "resolved"
# The share of a cycle's time budget kept for SharePoint when both sources run.
SHAREPOINT_SHARE = 0.5


def _bound_folders(mail_run: Mapping[str, Any] | None, mail_scope: Any,
                   sharepoint_run: Mapping[str, Any] | None,
                   sharepoint_scope: Any) -> list[dict[str, Any]]:
    """One row per scope folder: its source, position, round, completeness, reason,
    ``deferred`` (the time budget reached it before it read anything: a complete one is
    complete as its last round left it) and ``state``: ``complete``, ``in_progress``
    (incomplete; the next run resumes from the stored ``cursor``) or ``failed``
    (incomplete with no cursor: the next run starts over; ``reason`` says why)."""
    rows: list[dict[str, Any]] = []
    if mail_run is not None:
        for box in mail_run["mailboxes"]:
            if box["folders"]:
                rows += [{"source": "mail", "mailbox": box["index"], "folder": f["folder"],
                          "round": f.get("round"), "complete": f["complete"],
                          "deferred": bool(f.get("deferred")), "reason": f["reason"],
                          "cursor": f["cursor"]}
                         for f in box["folders"]]
            else:
                rows += [{"source": "mail", "mailbox": box["index"],
                          "folder": folder if folder in capture_mail.grammar
                          .WELL_KNOWN_MAIL_FOLDERS else "configured_id", "round": None,
                          "complete": False, "reason": box["status"], "cursor": None}
                         for folder in (mail_scope.mailboxes[box["index"]][1]
                                        if box["index"] < len(mail_scope.mailboxes)
                                        else mail_scope.all_mailbox_folders)]
    if sharepoint_run is not None:
        for target, declared in zip(sharepoint_run["targets"], sharepoint_scope.targets):
            if target["roots"]:
                rows += [{"source": "sharepoint", "target": target["index"], "root": n,
                          "round": r["round"], "complete": r["complete"],
                          "deferred": bool(r.get("deferred")), "reason": r["reason"],
                          "cursor": r["cursor"]}
                         for n, r in enumerate(target["roots"])]
            else:
                rows += [{"source": "sharepoint", "target": target["index"], "root": n,
                          "round": None, "complete": False, "reason": target["status"],
                          "cursor": None} for n in range(len(declared.folders))]
    for row in rows:
        row["state"] = ("complete" if row["complete"]
                        else "in_progress" if row["cursor"] else "failed")
    return rows


def capture_cycle(state_dir: str | Path, actor: Actor, *, mail_scope: Any = None,
                  mail_reader: Any = None, sharepoint_scope: Any = None,
                  sharepoint_readers: Callable[[str], Any] | None = None,
                  site_drive: Callable[[str], str] | None = None,
                  directory: Callable[[], tuple[list[str], str]] | None = None,
                  page_size: int = 50, page_budget: int | None = None,
                  max_store_bytes: int | None = None,
                  sleep: Callable[[float], None] = time.sleep,
                  budget_seconds: float | None = None,
                  monotonic: Callable[[], float] = time.monotonic,
                  sharepoint_share: float = SHAREPOINT_SHARE) -> dict[str, Any]:
    """One capture cycle into ``<state_dir>`` (module docstring). Either source may be
    left out (its scope ``None``). ``page_budget`` bounds mail delta pages per folder
    per cycle; a folder it stops is ``incomplete`` (``page_budget``) and resumes from
    its stored cursor next cycle. ``budget_seconds`` bounds the cycle's wall time: mail
    first within its share (``1 - sharepoint_share``), SharePoint with what is left
    (``time_budget``); ``budget`` reports the seconds each source was granted."""
    if budget_seconds is not None and (isinstance(budget_seconds, bool) or not isinstance(
            budget_seconds, (int, float)) or not budget_seconds >= 0):
        raise capture_mail.CaptureError("budget_seconds_invalid")
    if isinstance(sharepoint_share, bool) or not isinstance(sharepoint_share, (int, float)) \
            or not 0 <= sharepoint_share <= 1:
        raise capture_mail.CaptureError("sharepoint_share_invalid")
    deadline = None if budget_seconds is None else monotonic() + budget_seconds

    def left() -> float | None:
        return None if deadline is None else max(0.0, deadline - monotonic())

    mail_run = sharepoint_run = None
    granted: dict[str, float] = {}
    if mail_scope is not None:
        share = left()
        if share is not None and sharepoint_scope is not None:
            share = min(share, budget_seconds * (1 - sharepoint_share))
        if share is not None:
            granted["mail_granted"] = share
        store = capture_mail.MailCaptureStore(state_dir, max_store_bytes=max_store_bytes)
        try:
            mail_run = capture_mail.MailCapture(
                store, mail_reader, actor, page_size=page_size, page_budget=page_budget,
                sleep=sleep, budget_seconds=share, monotonic=monotonic
            ).run(mail_scope, directory=directory)
        finally:
            store.close()
    if sharepoint_scope is not None:
        share = left()   # its share and whatever mail left unused
        if share is not None:
            granted["sharepoint_granted"] = share
        store = capture_sharepoint.SharePointCaptureStore(state_dir,
                                                          max_store_bytes=max_store_bytes)
        try:
            sharepoint_run = capture_sharepoint.SharePointCapture(
                store, sharepoint_readers, actor, site_drive=site_drive,
                max_content_bytes=sharepoint_scope.max_content_bytes,
                budget_seconds=share, monotonic=monotonic).run(sharepoint_scope)
        finally:
            store.close()
    folders = _bound_folders(mail_run, mail_scope, sharepoint_run, sharepoint_scope)
    return {"mail": mail_run, "sharepoint": sharepoint_run, "folders": folders,
            "budget": granted or None,
            "complete": bool(folders) and all(f["complete"] and not f.get("deferred")
                                              for f in folders)
            and (mail_run is None or mail_run["complete"]),
            "complete_bound": sum(1 for f in folders if f["complete"]),
            "bound_total": len(folders),
            "incomplete_reasons": sorted({f["reason"] for f in folders
                                          if not f["complete"] and f["reason"]})}


# ------------------------------------------------------------------ exposed sources

def load_sources(path: str | Path) -> dict[str, dict[str, Any]]:
    """``source_id -> entry`` (with its ``kind``) of a ``plx-eval-exposed-sources/v1``
    document; ValueError on another document."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping) or document.get("schema") != SOURCES_SCHEMA:
        raise ValueError("sources document invalid")
    entries: dict[str, dict[str, Any]] = {}
    for kind in ("drive_items", "archive_members", "mail_messages", "mail_attachments"):
        for entry in document.get(kind) or []:
            if isinstance(entry, Mapping) and isinstance(entry.get("source_id"), str):
                entries[entry["source_id"]] = {**entry, "kind": kind}
    return entries


def cited_source_ids(item_paths: Iterable[str | Path]) -> list[str]:
    """The source ids eval items cite (``oracle.derived_from`` entries
    ``...sources.json#<id>``), in first-cited order."""
    found: list[str] = []
    for path in item_paths:
        item = json.loads(Path(path).read_text(encoding="utf-8"))
        oracle = item.get("oracle") if isinstance(item, Mapping) else None
        for ref in (oracle or {}).get("derived_from") or []:
            if isinstance(ref, str) and "sources.json#" in ref:
                source_id = ref.split("sources.json#", 1)[1]
                if source_id not in found:
                    found.append(source_id)
    return found


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _Locator:
    def __init__(self, entries: Mapping[str, Mapping[str, Any]], mail_store: Any,
                 sharepoint_store: Any, actor: Actor, audience: LiveSourceAudience) -> None:
        self.entries, self.mail, self.sharepoint = entries, mail_store, sharepoint_store
        self.actor, self.audience = actor, audience
        self.done: dict[str, dict[str, Any]] = {}

    def locate(self, source_id: str) -> dict[str, Any]:
        if source_id not in self.done:
            entry = self.entries.get(source_id)
            if entry is None:
                found = {"status": "unknown_source_id"}
            else:
                found = getattr(self, "_" + entry["kind"])(entry)
                found["kind"] = entry["kind"]
            self.done[source_id] = {"source_id": source_id, **found}
        return self.done[source_id]

    def _drive_items(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        store = self.sharepoint
        current = None if store is None else store.current(entry.get("drive_id"),
                                                           entry.get("item_id"))
        found = None if current is None else store.resolve(current["l1_handle"], self.actor,
                                                           self.audience, text=False)
        if found is None:
            return {"status": "not_captured"}
        row = found["row"]
        pinned = entry.get("sha256")
        if isinstance(pinned, str) and row.get("content_sha256") is not None:
            same = row["content_sha256"] == pinned.lower()
        elif isinstance(entry.get("quick_xor_hash"), str):
            same = (row.get("source_hashes") or {}).get("quickXorHash") \
                == entry["quick_xor_hash"]
        else:
            same = None
        if same is False:
            status = "hash_mismatch"
        elif row.get("etag") != entry.get("e_tag"):
            status = "changed_since"
        else:
            status = RESOLVED
        return {"status": status, "l1_handle": found["l1_handle"]}

    def _archive_members(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        archive = self.locate(str(entry.get("archive")))
        if archive["status"] not in (RESOLVED, "changed_since"):
            return {"status": "archive_" + archive["status"]}
        content = self.sharepoint.content(archive["l1_handle"], self.actor, self.audience)
        if content is None:
            return {"status": "archive_content_not_captured"}
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as bundle:
                info = bundle.getinfo(entry.get("member"))
                if info.file_size > MAX_MEMBER_BYTES:
                    return {"status": "member_exceeds_cap", "l1_handle": archive["l1_handle"]}
                member = bundle.read(info)
        except KeyError:
            return {"status": "member_not_found", "l1_handle": archive["l1_handle"]}
        except (zipfile.BadZipFile, ValueError, OSError):
            return {"status": "archive_unreadable", "l1_handle": archive["l1_handle"]}
        same = sha256(member).hexdigest() == str(entry.get("sha256")).lower()
        return {"status": RESOLVED if same else "hash_mismatch",
                "l1_handle": archive["l1_handle"]}

    def _mail_messages(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        moment = _instant(entry.get("received_at"))
        if self.mail is None or moment is None:
            return {"status": "not_captured"}
        handles = self.mail.received_at(self.actor, self.audience, moment)
        if not handles:
            return {"status": "not_captured"}
        if len(handles) > 1:
            return {"status": "ambiguous", "candidates": len(handles)}
        return {"status": RESOLVED, "l1_handle": handles[0]}

    def _mail_attachments(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        message = self.locate(str(entry.get("message")))
        if message["status"] != RESOLVED:
            return {"status": "message_" + message["status"]}
        named = [a for a in self.mail.attachments(message["l1_handle"], self.actor,
                                                  self.audience)
                 if a["name"] == entry.get("name")]
        pinned = str(entry.get("sha256")).lower()
        for attachment in named:
            if attachment["content_sha256"] == pinned:
                return {"status": RESOLVED, "l1_handle": attachment["l1_handle"]}
        if any(a["content_sha256"] for a in named):
            return {"status": "hash_mismatch"}
        return {"status": "bytes_not_captured" if named else "not_captured"}


def locate_sources(entries: Mapping[str, Mapping[str, Any]], source_ids: Sequence[str], *,
                   mail_store: Any, sharepoint_store: Any, actor: Actor,
                   audience: LiveSourceAudience) -> dict[str, Any]:
    """Each source id located in the captures for this reader (module docstring):
    ``{"items": [{"source_id", "kind", "status", "l1_handle"?}], "resolved", "total"}``.
    A source the reader may not see reads ``not_captured``, like one never captured."""
    locator = _Locator(entries, mail_store, sharepoint_store, actor, audience)
    items = [locator.locate(source_id) for source_id in source_ids]
    return {"items": items, "total": len(items),
            "resolved": sum(1 for item in items if item["status"] == RESOLVED)}


# ------------------------------------------------------------------ CLI (counts only)

def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", required=True,
                        help="Workbench state directory (outside every git work tree)")
    parser.add_argument("--mail-scope", default=str(capture_mail.DEFAULT_SCOPE),
                        help="plx-mail-capture-scope/v1 file")
    parser.add_argument("--sharepoint-scope", default=str(capture_sharepoint.DEFAULT_SCOPE),
                        help="plx-sharepoint-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    parser.add_argument("--withheld-principal", default="staff:capture-withheld",
                        help="a principal the audience does not grant; the stores are read "
                             "as it to prove withheld captures leak nothing")
    parser.add_argument("--runs", type=int, default=2,
                        help="capture cycles; cycle 2+ follows stored cursors only")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--mail-page-budget", type=int, default=None,
                        help="mail delta pages per folder per cycle (a folder stopped by it "
                             "is incomplete, reason page_budget, and resumes next cycle)")
    parser.add_argument("--max-store-bytes", type=int, default=None)
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="wall-time bound over all cycles, shared equally by the cycles "
                             "left; a folder or root it cuts is incomplete, reason "
                             "time_budget, and resumes next cycle")
    parser.add_argument("--sharepoint-share", type=float, default=SHAREPOINT_SHARE,
                        help="share of each cycle's budget kept for SharePoint (mail runs "
                             "first within the rest; unused time passes on)")
    parser.add_argument("--sources", default=None,
                        help="plx-eval-exposed-sources/v1 file whose sources are located in "
                             "the captures after the last cycle")
    parser.add_argument("--eval-item", action="append", default=[],
                        help="an eval item whose cited sources are located (repeatable; "
                             "default: every source of --sources)")


def _report_cycle(number: int, cycle: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("source", "mailbox", "folder", "target", "root", "round", "state", "complete",
            "deferred", "reason", "cursor")
    return {"run": number, "complete": cycle["complete"], "budget": cycle["budget"],
            "complete_bound": cycle["complete_bound"], "bound_total": cycle["bound_total"],
            "incomplete_reasons": cycle["incomplete_reasons"],
            "mailbox_status": [m["status"] for m in cycle["mail"]["mailboxes"]],
            "sharepoint_status": [t["status"] for t in cycle["sharepoint"]["targets"]],
            "folders": [{k: f[k] for k in keep if k in f} for f in cycle["folders"]]}


def _later_initial_rounds(cycles: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """(reruns, resumed) over the initial mail rounds of cycle 2 and later. Resumed: the
    round goes on from the page cursor an earlier cycle left (``resumed_from``
    ``next_url``), or the previous cycle's time budget cut the folder before its first
    round. A round restarted after an expired token (``round_restarts``) is neither;
    every other initial round is a rerun."""
    rerun = resumed = 0
    for before, cycle in pairwise(cycles):
        earlier = {(m.get("mailbox_id"), n): f for m in before["mail"]["mailboxes"]
                   for n, f in enumerate(m["folders"])}
        for box in cycle["mail"]["mailboxes"]:
            for n, f in enumerate(box["folders"]):
                if f.get("round") != "initial" or f["counts"]["round_restarts"]:
                    continue
                prior = earlier.get((box.get("mailbox_id"), n))
                if f.get("resumed_from") == "next_url" or (
                        prior is not None and prior["reason"] == capture_mail.TIME_BUDGET
                        and prior["cursor"] is None):
                    resumed += 1
                else:
                    rerun += 1
    return rerun, resumed


def run_cli(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
            sender: Any = None, sleep: Callable[[float], None] = time.sleep,
            monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Run ``--runs`` cycles, then read the stores back as the capture actor and as a
    withheld actor, and locate the exposed sources. Prints counts, booleans and enums."""
    from .graph_read import GraphMailReader, SharePointReader, build_graph, resolve_site_drive
    from .live_source_guard import BoundedBackoff, sharepoint_host_from_env

    if isinstance(args.runs, bool) or not 1 <= args.runs <= 5:
        raise ValueError("runs must be 1..5")
    mail_scope = capture_mail.CaptureScope.from_file(args.mail_scope)
    sharepoint_scope = capture_sharepoint.CaptureScope.from_file(args.sharepoint_scope)
    audience = LiveSourceAudience.from_file(args.live_audience)
    api, transport = build_graph(environ, sender=sender,
                                 retry=BoundedBackoff(sleep=sleep, budget_seconds=60))
    host = sharepoint_host_from_env(environ)
    readers: dict[str, Any] = {}

    def reader_for(drive_id: str) -> Any:
        if drive_id not in readers:
            readers[drive_id] = SharePointReader(
                api, audience, drive_id, transport=transport, read_permissions=True,
                max_acl_reads=capture_sharepoint.MAX_ACL_READS)
        return readers[drive_id]

    def site_drive(name: str) -> str:
        if host is None:
            raise ValueError("site_host_unconfigured")
        return resolve_site_drive(api, host, name)

    actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
    withheld = Actor(audience.tenant, frozenset({args.withheld_principal}), "restricted")
    mail_reader = GraphMailReader(api, audience)
    budget = getattr(args, "budget_seconds", None)
    if budget is not None and not budget > 0:
        raise ValueError("budget_seconds must be positive")
    deadline = None if budget is None else monotonic() + budget
    cycles = []
    for number in range(1, args.runs + 1):
        # Each cycle gets an equal share of what is left, so a later cycle still runs
        # (cycle 2 must follow the stored cursors) when an earlier one was cut.
        share = (None if deadline is None
                 else max(0.0, deadline - monotonic()) / (args.runs - number + 1))
        cycles.append(capture_cycle(
            args.state_dir, actor, mail_scope=mail_scope, mail_reader=mail_reader,
            sharepoint_scope=sharepoint_scope, sharepoint_readers=reader_for,
            site_drive=site_drive, page_size=args.page_size,
            page_budget=args.mail_page_budget, max_store_bytes=args.max_store_bytes,
            directory=lambda: capture_mail.directory_mailboxes(
                api, mail_scope.directory_allows), sleep=sleep, budget_seconds=share,
            monotonic=monotonic, sharepoint_share=getattr(args, "sharepoint_share",
                                                          SHAREPOINT_SHARE)))
    mail_store = capture_mail.MailCaptureStore(args.state_dir)
    sharepoint_store = capture_sharepoint.SharePointCaptureStore(args.state_dir)
    try:
        located = None
        if args.sources:
            entries = load_sources(args.sources)
            ids = cited_source_ids(args.eval_item) if args.eval_item else list(entries)
            found = locate_sources(entries, ids, mail_store=mail_store,
                                   sharepoint_store=sharepoint_store, actor=actor,
                                   audience=audience)
            hidden = locate_sources(entries, ids, mail_store=mail_store,
                                    sharepoint_store=sharepoint_store, actor=withheld,
                                    audience=audience)
            by_status: dict[str, int] = {}
            by_kind: dict[str, dict[str, int]] = {}
            for item in found["items"]:
                by_status[item["status"]] = by_status.get(item["status"], 0) + 1
                kind = by_kind.setdefault(item.get("kind", "unknown"),
                                          {"total": 0, "resolved": 0})
                kind["total"] += 1
                kind["resolved"] += item["status"] == RESOLVED
            located = {"total": found["total"], "resolved": found["resolved"],
                       "by_status": by_status, "by_kind": by_kind,
                       "withheld_actor_resolved": hidden["resolved"]}
        mail_counts = mail_store.counts(actor, audience)
        sharepoint_counts = sharepoint_store.counts(actor, audience)
        leak = {"withheld_actor_mailboxes": len(mail_store.completeness(
                    withheld, audience)["mailboxes"]),
                "withheld_actor_mail_counts": sum(mail_store.counts(withheld,
                                                                    audience).values()),
                "withheld_actor_roots": len(sharepoint_store.completeness(
                    withheld, audience)["roots"]),
                "withheld_actor_sharepoint_counts": sum(sharepoint_store.counts(
                    withheld, audience).values())}
        mismatched = sum(m["mismatched_attachments"] for m in mail_store.completeness(
            actor, audience)["mailboxes"])
    finally:
        mail_store.close()
        sharepoint_store.close()
    last = cycles[-1]
    later = [f for cycle in cycles[1:] for f in cycle["folders"]]
    mail_boxes = last["mail"]["mailboxes"]
    rerun, resumed = _later_initial_rounds(cycles)
    summary = {
        "complete_bound": last["complete_bound"], "bound_total": last["bound_total"],
        "complete": last["complete"],
        # The same ratio per source, so a complete mail side cannot hide an incomplete
        # SharePoint side (or the reverse).
        **{f"{source}_{key}": value for source in ("mail", "sharepoint")
           for key, value in (
               ("complete_bound", sum(1 for f in last["folders"]
                                      if f["source"] == source and f["complete"])),
               ("bound_total", sum(1 for f in last["folders"] if f["source"] == source)))},
        # Folders and roots the time budget cut (partial, resumed next cycle).
        "folders_budget_cut": sum(1 for f in last["folders"] if "time_budget" in str(
            f["reason"] or "").split(",")),
        # Complete folders and roots whose delta the time budget left to a later cycle
        # (still complete as their last round left them).
        "deltas_deferred": sum(1 for f in last["folders"]
                               if f["complete"] and f.get("deferred")),
        "mailboxes_named": len(mail_scope.mailboxes),
        "mailboxes_outside_allowlist": len(mail_scope.outside_allowlist),
        "mail_directory": last["mail"]["directory"],
        "mailboxes_listed": last["mail"]["mailboxes_listed"],
        "mailboxes_excluded": last["mail"]["mailboxes_excluded"],
        "exclusions_configured": len(mail_scope.excluded),
        "mailboxes_opened": sum(1 for m in mail_boxes if m.get("mailbox_id")),
        # Gaps, never dropped: a mailbox Graph refuses (HTTP 403) at open or per folder.
        "mailboxes_forbidden": sum(1 for m in mail_boxes if m["status"] == "forbidden"
                                   or any(f["reason"] == "forbidden" for f in m["folders"])),
        "mail_folders": sum(1 for f in last["folders"] if f["source"] == "mail"),
        "sentitems_folders_read": sum(1 for m in mail_boxes for f in m["folders"]
                                      if f["folder"] == "sentitems"
                                      and f["reason"] != "not_visible"),
        "sharepoint_roots": sum(1 for f in last["folders"] if f["source"] == "sharepoint"),
        "sharepoint_targets_resolved": sum(1 for t in last["sharepoint"]["targets"]
                                           if "drive_id" in t),
        # Cycle 2+ must follow stored cursors: a mail folder that starts an initial round
        # again (other than an expired token, counted in round_restarts) is a defect; one
        # that continues or starts a round a budget cut is resumed, a partial unit.
        "rerun_initial_rounds": rerun,
        "rounds_resumed": resumed,
        "rerun_incremental_roots": sum(1 for f in later if f["source"] == "sharepoint"
                                       and f["round"] == "incremental"),
        "messages_current": mail_counts["messages_current"],
        "attachments_captured": mail_counts["attachments_captured"],
        "attachments_mismatched": mismatched,
        "files_current": sharepoint_counts["files_current"],
        "contents_captured": sharepoint_counts["contents_captured"]}
    report = {
        "check": "capture.breadth",
        "evidence_label": "injected-transport" if transport.sender_injected else "live-read",
        "read_only_guard": "GET-only; token POST only",
        "storage": {"outside_git_tree": True},
        "runs": [_report_cycle(n, c) for n, c in enumerate(cycles, 1)],
        "summary": summary, "exposed_sources": located, "leak_check": leak,
        "requests_sent": transport.requests_sent}
    report["ok"] = bool(
        summary["mail_directory"] in (None, "complete")
        and summary["mailboxes_opened"] >= min(1, summary["mailboxes_named"]
                                               + summary["mailboxes_listed"])
        and summary["sharepoint_targets_resolved"] == len(sharepoint_scope.targets)
        and summary["rerun_initial_rounds"] == 0
        and not any(leak.values())
        and (located is None or located["withheld_actor_resolved"] == 0))
    return report


__all__ = ["CAPTURE_PRINCIPAL", "add_cli_arguments", "capture_cycle", "cited_source_ids",
           "load_sources", "locate_sources", "run_cli"]
