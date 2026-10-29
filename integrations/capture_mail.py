"""Versioned mail capture (L1): Graph delta paging into Workbench state.

The Workbench may
read every mailbox in its capture scope and keep everything it captures --
message metadata, text bodies, attachment metadata and attachment bytes -- in
its own capture store under the configured Workbench state directory. Nothing
captured goes to Git, logs or source records. Who may *see* a captured
mailbox is decided again on every read, per reader (``LiveSourceAudience``
over the mailbox identity set recorded at capture plus the derived owner ACL):
a mailbox withheld from an actor yields no rows, no counts and no completeness
entry, exactly like a mailbox that was never captured.

Reads (all GET, through ``integrations.graph_read.GraphMailReader`` and so the
one ``live_source_guard`` client):

* ``message_delta`` per configured folder: ``$select`` projection, a
  ``receivedDateTime ge`` window fixed at the start of the first round
  (default 90 days), no page cap. After every page the next cursor is stored,
  so an interrupted run resumes mid-round; the round's ``@odata.deltaLink``
  is stored and the next run reads only the changes since it. A folder is
  ``complete`` only when its latest round reached a deltaLink (and, with
  ``history`` all, its history below).
* Every folder (scope ``folders`` ``"all"``): the mailbox's folder tree is listed when
  a run opens it (``GraphMailReader.list_folders``, ids and item counts, kept for
  ``FOLDER_RELIST_SECONDS``); each folder is its own unit with its own cursors, an
  empty one that holds no captured mail is skipped until a listing counts mail in it,
  and a listing that fails leaves the mailbox incomplete
  (``folder_listing_incomplete``).
* History (scope ``history`` ``"all"``): once a folder's windowed round reached its
  deltaLink, the mail older than the window is read newest first
  (``GraphMailReader.message_pages``: ``receivedDateTime lt`` the window's start,
  ``$orderby receivedDateTime desc``), resuming by its own stored cursor
  (``backfill_next_url``) until it reaches the end (``backfill_status`` complete).
  That older mail has no delta: a later change to it is not followed.
* For a new or changed message version: the message with its text body
  (expected in the same folder), its attachment list (paged through
  ``@odata.nextLink`` up to a hard cap, ``MAX_ATTACHMENTS_LISTED``) and the
  bytes of every file attachment up to Graph's 20 MB limit, pinned by SHA-256
  and stored content-addressed. A listing cut at the cap is recorded on the
  version (``attachments_status: truncated``): later runs list it again even
  when its changeKey is unchanged, and its folder, mailbox and run are not
  complete while any current version holds that state. A listing still cut at
  the hard cap after two re-lists becomes ``truncated_capped``: it is no longer
  re-listed (no recurring repair cost) but still keeps the folder incomplete.
* Attachment bytes are verified against Graph's metadata by size only (Graph
  publishes no hash for mail attachments): the decoded bytes must stay within the
  reported size, and the attachment read must report the same size as the listing
  did. The SHA-256 the bytes are pinned by is a store-integrity check (the stored
  blob must still hash to it), not a comparison with the source. A failed check is
  a recorded outcome, never silent and never a crash: the attachment is kept as
  metadata with ``content_status`` ``mismatch:sha256`` or ``mismatch:size``,
  retried by later runs, and its folder is ``incomplete`` (reason
  ``attachment_bytes_mismatch``) while a current version holds one.
* Attachment text (``text_status``, ``text_sha256``): captured bytes are read once at
  capture through the one text path of captured files (``integrations.captured_text``,
  as for SharePoint files): a scanned PDF's pages are OCR-read locally when this host
  has an engine and the OCR text is pinned by SHA-256, so every reader (mentions, typed
  fields, ``resolve``) cites that one reading. With ``defer_ocr`` (the host's and the
  CLI's :data:`PRODUCTION`) a scan is stored at once with its text layer as
  ``ocr_pending`` and OCR-read beside the capture (``_OcrQueue``), never holding it. Without an engine a scan is
  ``ocr_unavailable``; a later round on a host with one re-reads the stored bytes
  (bounded per round, no source read), as for attachments captured before text was.
  A re-read is a new immutable version of the attachment's text
  (``attachment_text_versions``: ``fill_text`` appends, never rewrites): the earlier
  reading and its citation stay (``resolve(..., text_version=n)``), the newest is the
  current one, and OCR text stays review-grade (``extracted_ocr``). A failure from
  before OCR tool exits were typed by tool is read again once.
* Throttling: each GET has the guard's bounded retry (``BoundedBackoff``,
  ``Retry-After`` up to its cap); a page that still answers 429/50x waits
  ``throttle_waits`` and resumes from the stored cursor; after the last wait
  the folder stops ``incomplete`` with reason ``throttled`` and the next run
  resumes. An expired delta token (HTTP 410) restarts the round.

Versions (R8, ``as_known_at``): one row per captured message version, keyed by
its L1 handle -- the same ``graph-mail://<mailbox>/<message>@<changeKey>``
citation the GET-only reader mints and ``operating/claim_checks.py`` accepts
(field paths ``row.<field>``). ``recorded_at`` is when this store first
captured the version, ``superseded_at`` when a later version or a delta
``@removed`` replaced it; superseded versions are kept. A version listed
again after it stopped being current (a removal, or A -> B -> A changeKeys)
opens a new interval row, so ``as_known_at`` intervals never overlap. A full
(initial) round -- the first one, a restart after HTTP 410 or a window change
-- reconciles at its deltaLink: a current version inside the window that the
round did not list is superseded with reason ``absent_after_resync``. A body
or attachment that failed non-transiently is retried at the end of later
rounds (``degraded_*`` in completeness). Attachments are cited
``graph-mail://...@<changeKey>#attachment=<id>&sha256=<hex>``
(``graph_evidence.mail_attachment_citation``). Captured handles resolve
offline (``resolve``) for a reader who may see the mailbox.

Which mailboxes. ``all_mailboxes`` (ING-06): every
mailbox of the tenant directory -- each member user with a mail address, listed
through the guarded, paged ``/users`` GET at the start of every run -- except the
scope's explicit ``exclude`` list (addresses or object ids, each with its reason;
no mail of an excluded mailbox is requested, also when the listing names it by
another of its addresses). Each mailbox keeps its own per-folder cursor, so a
mailbox new in the directory starts its first round while the others resume from
their delta links.
Named mailboxes (``mailboxes``, ``allowlisted_mailboxes``) stay bound to the one
canonical allowlist, ``operating/case_mail_scope.json``
(``operating.case_mail_scope.MailScope``): a named mailbox outside it, or one still
``pending_confirmation``, is set aside when the scope is read
(``outside_allowlist``, counted) and never requested. Capturing a mailbox never
lets anyone see it: every reader is re-gated per mailbox when viewing.

Scheduling (``schedule_units``, one rule shared with the SharePoint capture; with
``pages_per_turn`` the run takes the folders in that order in turns of a few pages each,
``MailCapture._turns``, so every folder's newest pages come before any second turn). A run
captures first the folders with a stored frontier (a round cut mid-way), the one started
earliest first, then other incomplete folders in the scope's order, then complete
folders (each reading only its delta) the one brought current longest ago first, then
folders with a terminal reason (``forbidden``, ``not_visible``). A folder the time
budget reaches before reading anything is deferred: its cursors and ``last_run_at`` are
kept, a complete folder stays complete and a terminal reason stays. So a folder larger
than one budget resumes first every run and completes, the others follow in turn, and a
scope larger than one budget completes over several runs instead of re-spending every
budget on the same leading folders.

Completeness per run: ``complete_bound`` / ``bound_total`` over every scope folder
of every target mailbox, plus ``incomplete_reasons``. A mailbox that could not be
opened, or whose folders Graph refuses (HTTP 403, e.g. an Exchange application
access policy: reason ``forbidden``), is bound and incomplete with its reason --
a gap, never dropped -- and the run goes on with the next mailbox. A truncated
directory listing keeps the run incomplete (``directory_truncated:<reason>``).
``MailCaptureStore.mailbox_coverage`` tells a reader which mailbox would hold a
cited message the capture does not hold, and why (``mailbox_not_captured``,
``mailbox_capture_incomplete``).

Storage: ``<state-dir>/mail-capture/`` (SQLite + ``blobs/sha256/``), private
modes; the state directory must lie outside every git work tree (checked
before anything is created). Bodies and attachment bytes are content-addressed
blobs (one copy per sha256, shared across changeKeys); an optional
``max_store_bytes`` bound stops capture with reason ``store_full``. The store is
compact enough to capture every mailbox continuously:

* Bodies and attachment texts are stored zlib-compressed (``<sha256>.z``).
* An attachment original over ``DROP_ORIGINALS_ABOVE`` (256 KiB) is dropped once nothing
  needs its bytes (``MailCaptureStore.settle_original``): its sha256, size, name and MIME
  type stay on its row, and every reading a reader makes of it (each captured name and
  type of those bytes, without and with each pinned OCR text) is made from the bytes and
  kept in its ``.dropped`` form (``integrations.captured_text``), so resolve, the claim
  gate, the mention documents and the typed fields read the same text, pages and status
  as before. Its quickXorHash is kept too. Kept are: small originals, containers (zip,
  ``.msg``, ``.eml``: their members are cited from the bytes), bytes that are their own
  text (compressed), and originals a later reading needs (never read, OCR pending or
  without an engine, an untyped OCR failure) until that reading is stored. The bytes
  are re-read from Graph on demand (``refetch_original``: one GET, checked against the
  pin, never stored).
* The originals kept for a pending reading are bounded (``max_pending_ocr_bytes``,
  default 1 GiB): past it, a new PDF attachment is not downloaded
  (``content_status`` ``deferred:ocr_backlog``; its folder ``incomplete`` with reason
  ``attachments_deferred``) and a later round reads it once OCR has drained the backlog.
* A scope's ``ocr`` ``deferred`` (or ``compact --defer-ocr``) drops the bytes of scans
  waiting for OCR too: their text status becomes ``ocr_deferred`` (a new text version;
  the text layer stays read and indexed) and every answer on one carries the named gap
  ``ocr_deferred`` (``ocr_deferred_gap``; documentFields: ``not_stated_in_read_pages``),
  never an empty or absent reading. ``python3 -m integrations.capture_mail ocr-deferred
  --state-dir D --live-audience F --budget-seconds N`` re-fetches each one
  (``refetch_original``), OCR-reads it, stores the reading and drops the bytes again.
* ``python3 -m integrations.capture_mail compact --state-dir D`` compacts a store
  captured before (``MailCaptureStore.compact``): prune, compress, then ``VACUUM INTO`` a
  new file swapped in atomically when the free disk covers it.

CLI (credentialed live check; prints counts, booleans and completeness only,
never addresses, subjects, bodies, names or ids)::

    python3 -m integrations.graph_read mail-capture --state-dir D \\
        --scope integrations/mail_capture_scope.json \\
        --live-audience examples/mail_capture_audience.live-check.json --runs 2
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from contextlib import contextmanager
import contextvars
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import zlib
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology.mentions import cited_page, is_paged_file
from ontology.mentions import sender_class as mentions_sender_class
from ontology.types import Actor
from operating.case_mail_scope import MailScope

from . import live_source_grammar as grammar
from .captured_text import (COMPRESSED_SUFFIX, DROPPED_SUFFIX, OCR_DEFERRED, OCR_PENDING,
                            OCR_UNAVAILABLE,
                            TEXT_OCR, TEXT_UNREAD, UNTYPED_OCR_FAILURES, blob_path,
                            capture_text, captured_text, file_type, inflate, member_view,
                            ocr_digest, read_dropped, reading_key, readings_of,
                            split_member, write_dropped)
from .capture_store import (SourceKind, register_source_kind, sqlite_bytes,
                            checkpoint, wal_journal)
from .graph_evidence import (FILE_ATTACHMENT_TYPE, MAX_ATTACHMENT_BYTES,
                             is_mail_attachment_citation, mail_attachment_citation)
from .graph_read import MAX_ATTACHMENTS_LISTED
from .people_exclusion import (ExclusionUnavailable, PeopleExcluded, PeopleExclusion,
                               is_object_id)
from .live_source_guard import (LiveSourceAudience, NotVisible, SourceUnavailable,
                                is_nextlink_refusal, unavailable_detail)
from .plx_hub_guard import GRAPH_BLOCKS, upstream_blocks

SCOPE_SCHEMA = "plx-mail-capture-scope/v1"
ALL_FOLDERS = "all"   # a scope's folders: every folder of the mailbox
HISTORIES = ("window", "all")
OCR_MODES = ("inline", "deferred")   # a scope's ``ocr`` (CaptureScope)
STORE_DIR = "mail-capture"
DB_FILE = "capture.db"
LINEAGE = "mail"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCOPE = REPO_ROOT / "integrations" / "mail_capture_scope.json"
# Bodies are kept whole up to what one guarded GET may return (4 MB of JSON).
MAX_BODY_CHARS = 3_500_000
ENVELOPE_CHARS = 20_000  # operating/ontology_tool_contracts.json untrusted_text maxLength
DEFAULT_THROTTLE_WAITS = (30.0, 60.0, 120.0)
_TRANSIENT = frozenset({"http_429", "http_502", "http_503", "http_504", "timeout",
                        "transport_failure", "conn_reset", "remote_closed", "urlerror",
                        "download_incomplete"})
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_UPN = re.compile(r"^[a-z0-9._%+'-]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")
_DOMAIN = re.compile(r"^[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _mail_path(parts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parts, (str, bytes)) or len(parts) != 2 \
            or not all(isinstance(p, str) for p in parts):
        raise ValueError("mail_path_invalid")
    mailbox, message = parts[0].lower(), parts[1]
    if not grammar.TYPES["user_id"].valid(mailbox) or not grammar.TYPES["mail_id"].valid(message):
        raise ValueError("mail_path_invalid")
    return mailbox, message


# The mail source kind in the capture registry (integrations/capture_store.py): ``graph-mail://<mailbox object
# id>/<message id>``, audience unit ``mail:<mailbox>`` (the C0 audience_key).
# Mail keeps its own versioned store (``MailCaptureStore``: changeKey-tailed
# handles, blobs, delta cursors); readers are re-gated per mailbox on the identity
# set and owner ACL recorded at capture, never on this resource name alone.
MAIL_KIND = register_source_kind(SourceKind(
    name="graph_mail", scheme="graph-mail", path_parts=2, canonical_path=_mail_path,
    unit=lambda path: f"mail:{path[0]}", resources=lambda path: (f"mail:{path[0]}",),
    key=lambda path: {"mailbox": path[0], "id": path[1]}))


def store_exists(state_dir: str | Path) -> bool:
    """Whether a mail capture store exists under ``state_dir`` (no file is created)."""
    return (Path(state_dir).expanduser() / STORE_DIR / DB_FILE).is_file()


class CaptureError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    """Fixed-width UTC text, so recorded/superseded times compare as strings."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _normal_time(value: Any) -> str:
    if not isinstance(value, str) or _ISO_Z.fullmatch(value) is None:
        raise CaptureError("as_known_at_invalid")
    head, _, frac = value[:-1].partition(".")
    return f"{head}.{(frac or '').ljust(6, '0')}Z"


WORKBENCH_STATE_ENV = "PLX_WORKBENCH_STATE_DIR"   # the persistent host's state directory
CHECK_STATE = "mail-capture-check"   # its leaf below the host state base (the live check's)


def check_state_dir(env: Mapping[str, str], check: str, *, injected: bool
                    ) -> tuple[Path, Path, str]:
    """(the host state directory of a live capture check kept across its runs, the base
    it lies under, which base): ``$PLX_WORKBENCH_STATE_DIR/<check>`` (``
    workbench_state_dir``, the credentialed host's persistent store, kept between daily
    runs), else ``$XDG_STATE_HOME/plx-workbench/<check>`` (``xdg_state_home``), else the
    same under ``$HOME/.local/state`` (``home``); then ``live``, or
    ``injected-transport`` so a fixture run never shares the live store. A set variable
    that is not an absolute path, or none set: refused (``ValueError``), never a store
    in a place nobody chose. Never in Git: the caller checks it
    (:func:`assert_outside_git`)."""
    leaf = "injected-transport" if injected else "live"
    workbench = env.get(WORKBENCH_STATE_ENV)
    if workbench:
        if not Path(workbench).is_absolute():
            raise ValueError(f"state_dir_invalid: {WORKBENCH_STATE_ENV} is not absolute")
        return Path(workbench) / check / leaf, Path(workbench), "workbench_state_dir"
    base, kind = env.get("XDG_STATE_HOME"), "xdg_state_home"
    if not base and env.get("HOME"):
        base, kind = str(Path(env["HOME"]) / ".local" / "state"), "home"
    if not base or not Path(base).is_absolute():
        raise ValueError(f"state_dir_unset: give --state-dir or set {WORKBENCH_STATE_ENV}, "
                         "XDG_STATE_HOME or HOME")
    return Path(base) / "plx-workbench" / check / leaf, Path(base), kind


def assert_outside_git(path: str | Path) -> Path:
    """The resolved state directory; ``CaptureError`` when it, or any parent,
    is a git work tree (or lies inside this repository). Checked before any
    directory is created, so captured mail can never land in a checkout."""
    if not isinstance(path, (str, Path)) or not str(path):
        raise CaptureError("state_dir_required")
    resolved = Path(path).expanduser().resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise CaptureError("state_dir_inside_git_tree")
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            raise CaptureError("state_dir_inside_git_tree")
    return resolved


# ------------------------------------------------------------------ scope

class CaptureScope:
    """Which mailboxes and folders are captured, and how far back.

    ``plx-mail-capture-scope/v1``: ``mailboxes`` (``{address, folders}``),
    ``allowlisted_mailboxes`` (``{"folders": [...]}``: every mailbox of the
    allowlist, explicit ``mailboxes`` entries keeping their own folders),
    ``window_days`` (first-round window), ``internal_domains`` (sender class
    of the untrusted-text envelope; default: the allowlist's) and
    ``all_mailboxes`` (``{"enabled": bool, "folders": [...], "exclude":
    [{"address", "reason"}]}``: every tenant directory user with a mail address
    except the excluded ones, listed through the guarded ``/users`` GET at run
    time; the allowlist does not narrow it). ``allowlist`` (default: the built-in
    ``MailScope``; ``MailScope.unrestricted()`` only for isolated tests over
    synthetic mailboxes): a named mailbox outside it is set aside
    (``outside_allowlist``) and never captured. Configuration, never a grant:
    the capture actor still needs the live-source audience.

    ``folders`` may be ``"all"`` (:data:`ALL_FOLDERS`): every folder of the mailbox, listed
    when the run opens it (``GraphMailReader.list_folders``). ``history`` ``"all"``: after a
    folder's windowed delta round, the mail older than its window is read too, newest
    first, resuming by its own cursor (``"window"``, the default: the window only)."""

    def __init__(self, mailboxes: Sequence[Mapping[str, Any]], *, window_days: int = 90,
                 internal_domains: Iterable[str] | None = None, all_mailboxes: bool = False,
                 all_mailbox_folders: Sequence[str] | str = ("inbox",),
                 allowlisted_folders: Sequence[str] | None = None,
                 allowlist: MailScope | None = None, exclude: Iterable[str] = (),
                 history: str = "window", ocr: str = "inline") -> None:
        if history not in HISTORIES:
            raise CaptureError("scope_history_invalid")
        if ocr not in OCR_MODES:
            raise CaptureError("scope_ocr_invalid")
        self.history = history
        # ``deferred``: a scan's OCR is put off and its bytes dropped (``ocr_deferred``,
        # read later by ``ocr-deferred``); ``inline``: kept until OCR has read it.
        self.ocr = ocr
        self.allowlist = MailScope.builtin() if allowlist is None else allowlist
        if not isinstance(self.allowlist, MailScope):
            raise CaptureError("scope_allowlist_invalid")
        if isinstance(window_days, bool) or not isinstance(window_days, int) \
                or not 1 <= window_days <= 3650:
            raise CaptureError("scope_window_invalid")
        self.window_days = window_days
        entries: list[tuple[str, tuple[str, ...]]] = []
        outside: list[str] = []
        seen: set[str] = set()
        for entry in mailboxes:
            if not isinstance(entry, Mapping) or set(entry) - {"address", "folders", "note"}:
                raise CaptureError("scope_mailbox_invalid")
            address = entry.get("address")
            if not isinstance(address, str) or not (
                    _UPN.fullmatch(address) or grammar.TYPES["user_id"].valid(address)):
                raise CaptureError("scope_mailbox_invalid")
            if address in seen:
                raise CaptureError("scope_mailbox_duplicate")
            seen.add(address)
            folders = self._folders(entry.get("folders", ["inbox"]))
            if self.allowlist.allows([address]):
                entries.append((address, folders))
            else:
                outside.append(address)
        if allowlisted_folders is not None:
            if self.allowlist.is_unrestricted:
                raise CaptureError("scope_allowlist_unrestricted")
            folders = self._folders(allowlisted_folders)
            entries += [(address, folders) for address in sorted(self.allowlist.addresses)
                        if address not in seen]
        self.mailboxes = tuple(entries)
        self.outside_allowlist = tuple(outside)
        domains = list(self.allowlist.internal_domains if internal_domains is None
                       else internal_domains)
        if any(not isinstance(d, str) or _DOMAIN.fullmatch(d) is None for d in domains):
            raise CaptureError("scope_domain_invalid")
        self.internal_domains = frozenset(domains)
        if not isinstance(all_mailboxes, bool):
            raise CaptureError("scope_all_mailboxes_invalid")
        self.all_mailboxes = all_mailboxes
        self.all_mailbox_folders = self._folders(all_mailbox_folders)
        excluded = set()
        for address in exclude:
            if not isinstance(address, str) or not (
                    _UPN.fullmatch(address.lower()) or grammar.TYPES["user_id"].valid(address)):
                raise CaptureError("scope_exclude_invalid")
            excluded.add(address.lower())
        self.excluded = frozenset(excluded)
        if not self.mailboxes and not self.all_mailboxes and not self.outside_allowlist:
            raise CaptureError("scope_empty")

    def excludes(self, identities: Iterable[str]) -> bool:
        """Whether any of a mailbox's identities is on the exclusion list."""
        return any(str(i).lower() in self.excluded for i in identities)

    def directory_allows(self, identities: Sequence[str]) -> bool:
        """The ``directory_mailboxes`` filter: every listed mailbox not excluded."""
        return not self.excludes(identities)

    @staticmethod
    def _folders(folders: Any) -> tuple[str, ...]:
        if folders == ALL_FOLDERS:
            return (ALL_FOLDERS,)
        if isinstance(folders, (str, bytes)) or not isinstance(folders, Sequence) \
                or not folders or len(folders) > 50:
            raise CaptureError("scope_folders_invalid")
        values = []
        for folder in folders:
            if not grammar.TYPES["mail_folder"].valid(folder) or folder in values:
                raise CaptureError("scope_folders_invalid")
            values.append(folder.lower() if folder.lower()
                          in grammar.WELL_KNOWN_MAIL_FOLDERS else folder)
        return tuple(values)

    @classmethod
    def from_json(cls, document: Any, *, allowlist: MailScope | None = None
                  ) -> "CaptureScope":
        allowed = {"schema", "note", "window_days", "internal_domains", "mailboxes",
                   "all_mailboxes", "allowlisted_mailboxes", "history", "ocr"}
        if (not isinstance(document, dict) or document.get("schema") != SCOPE_SCHEMA
                or set(document) - allowed or not isinstance(document.get("mailboxes", []),
                                                             list)):
            raise CaptureError("scope_document_invalid")
        every = document.get("all_mailboxes", {"enabled": False})
        if not isinstance(every, dict) or set(every) - {"enabled", "folders", "exclude",
                                                         "note"}:
            raise CaptureError("scope_all_mailboxes_invalid")
        exclude = every.get("exclude", [])
        if not isinstance(exclude, list) or any(
                not isinstance(e, dict) or set(e) != {"address", "reason"}
                or not isinstance(e["reason"], str) or not e["reason"].strip()
                for e in exclude):
            raise CaptureError("scope_exclude_invalid")
        listed = document.get("allowlisted_mailboxes")
        if listed is not None and (not isinstance(listed, dict)
                                   or set(listed) - {"folders", "note"}):
            raise CaptureError("scope_allowlisted_mailboxes_invalid")
        return cls(document.get("mailboxes", []), window_days=document.get("window_days", 90),
                   internal_domains=document.get("internal_domains"),
                   all_mailboxes=every.get("enabled", False),
                   all_mailbox_folders=every.get("folders", ["inbox"]),
                   allowlisted_folders=None if listed is None
                   else listed.get("folders", ["inbox", "sentitems"]), allowlist=allowlist,
                   exclude=[e["address"] for e in exclude],
                   history=document.get("history", "window"),
                   ocr=document.get("ocr", "inline"))

    @classmethod
    def from_file(cls, path: str | Path, *, allowlist: MailScope | None = None
                  ) -> "CaptureScope":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CaptureError("scope_unreadable") from None
        return cls.from_json(document, allowlist=allowlist)


# ------------------------------------------------------------------ store

STORE_SCHEMA_VERSION = "3"
_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mailboxes (
    mailbox_id TEXT PRIMARY KEY,
    upn TEXT NOT NULL,
    identities TEXT NOT NULL,
    resources TEXT NOT NULL,
    source_acl TEXT NOT NULL,
    first_captured_at TEXT NOT NULL,
    identity_checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    mailbox_id TEXT NOT NULL REFERENCES mailboxes(mailbox_id),
    folder_key TEXT NOT NULL,
    folder_id TEXT,
    window_days INTEGER,
    received_since TEXT,
    next_url TEXT,
    delta_url TEXT,
    round_kind TEXT,
    round_started_at TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    last_complete_at TEXT,
    last_run_at TEXT,
    rounds_complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mailbox_id, folder_key)
);
-- One row per validity interval of a message version. A version that comes
-- back after a removal gets a new row (same l1_handle, new interval), so
-- as_known_at intervals never overlap and a removal gap stays a gap.
CREATE TABLE IF NOT EXISTS messages (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    l1_handle TEXT NOT NULL,
    mailbox_id TEXT NOT NULL REFERENCES mailboxes(mailbox_id),
    message_id TEXT NOT NULL,
    change_key TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    record TEXT NOT NULL CHECK (json_valid(record)),
    digest TEXT NOT NULL,
    body_status TEXT NOT NULL,
    body_sha256 TEXT REFERENCES blobs(sha256),
    attachments_status TEXT NOT NULL DEFAULT 'complete',
    attachment_list_attempts INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    superseded_at TEXT,
    removed_reason TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_id ON messages(mailbox_id, message_id);
CREATE INDEX IF NOT EXISTS messages_by_handle ON messages(l1_handle);
CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    message_handle TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    l1_handle TEXT UNIQUE,
    mailbox_id TEXT NOT NULL,
    metadata TEXT NOT NULL CHECK (json_valid(metadata)),
    content_sha256 TEXT,
    content_status TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    text_status TEXT,
    text_sha256 TEXT REFERENCES blobs(sha256),
    PRIMARY KEY (message_handle, attachment_id)
);
-- Every reading of an attachment's text, append-only: a re-extraction (OCR arrived, a
-- failed OCR fixed) is a new row, never an update, so a citation to an earlier reading
-- keeps resolving to that reading. attachments.text_status/text_sha256 is the pointer
-- to the newest.
CREATE TABLE IF NOT EXISTS attachment_text_versions (
    l1_handle TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    text_status TEXT NOT NULL,
    text_sha256 TEXT REFERENCES blobs(sha256),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (l1_handle, version)
);
CREATE TRIGGER IF NOT EXISTS attachment_text_versions_no_update
BEFORE UPDATE ON attachment_text_versions
BEGIN SELECT RAISE(ABORT, 'attachment_text_versions_immutable'); END;
CREATE TRIGGER IF NOT EXISTS attachment_text_versions_no_delete
BEFORE DELETE ON attachment_text_versions
BEGIN SELECT RAISE(ABORT, 'attachment_text_versions_immutable'); END;
CREATE TABLE IF NOT EXISTS message_events (
    l1_handle TEXT NOT NULL,
    event TEXT NOT NULL,
    at TEXT NOT NULL
);
-- The directory department of a listed mailbox (JSON of the listing's value), recorded
-- before each directory-wide run so the deny list decides on it at read.
CREATE TABLE IF NOT EXISTS mailbox_directory (
    mailbox_id TEXT PRIMARY KEY,
    department TEXT,
    listed_at TEXT NOT NULL
);
"""
# Every folder of a mailbox (``folders`` all): ids and Graph's item count of the latest
# listing (``GraphMailReader.list_folders``), re-listed after FOLDER_RELIST_SECONDS.
_FOLDER_LISTING_DDL = """CREATE TABLE IF NOT EXISTS mailbox_folders (
    mailbox_id TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    total INTEGER,
    listed_at TEXT NOT NULL,
    PRIMARY KEY (mailbox_id, folder_id))"""
# The one exception to append-only attachment text: an exclusion purge
# (``MailCaptureStore.purge_withheld``) drops the trigger, deletes and re-creates it in one
# transaction.
_TEXT_NO_DELETE = ("CREATE TRIGGER attachment_text_versions_no_delete BEFORE DELETE ON "
                   "attachment_text_versions BEGIN SELECT RAISE(ABORT, "
                   "'attachment_text_versions_immutable'); END")
PURGE_COUNTS = ("mailboxes", "messages", "versions", "attachments", "blobs")
# A mailbox row with the directory listing that named it (``record_directory``), if any.
_MAILBOX_ROWS = ("SELECT m.*, d.department AS department, d.listed_at AS listed_at FROM "
                 "mailboxes m LEFT JOIN mailbox_directory d ON d.mailbox_id=m.mailbox_id "
                 "ORDER BY m.mailbox_id")
_ENV_GUARD: dict[str, Any] = {}
_DENY: dict[str, Any] = {}


def _env_people() -> PeopleExclusion:
    """The people guard of this process's environment now (``PeopleExclusion.from_env``),
    read again whenever the variables, the named file or the committed config change."""
    from . import people_exclusion as pe

    def stat(path: Any) -> tuple[int, int] | None:
        try:
            found = Path(path).expanduser().stat()
        except (OSError, TypeError, ValueError):
            return None
        return found.st_mtime_ns, found.st_size

    named = os.environ.get(pe.ENV_FILE)
    key = (os.environ.get(pe.ENV_IDS), named, stat(named) if named else None,
           stat(pe.CONFIG))
    if _ENV_GUARD.get("key") != key:
        _ENV_GUARD.update(key=key, guard=PeopleExclusion.from_env(os.environ))
    return _ENV_GUARD["guard"]


def _default_deny() -> Any:
    """The shipped HR / payroll deny list (``mail_directory_scope.DEFAULT_SCOPE``), read
    again when the file changes; None when it is unusable (every mailbox is withheld)."""
    from .mail_directory_scope import DEFAULT_SCOPE, DirectoryScope, DirectoryScopeError
    try:
        found = DEFAULT_SCOPE.stat()
        key = (found.st_mtime_ns, found.st_size)
    except OSError:
        return None
    if _DENY.get("key") != key:
        try:
            deny = DirectoryScope.from_file(DEFAULT_SCOPE).deny
        except (DirectoryScopeError, CaptureError):
            deny = None
        _DENY.update(key=key, deny=deny)
    return _DENY["deny"]


def _usable(guard: Any) -> bool:
    return isinstance(guard, PeopleExclusion) and not guard.unavailable
# Body statuses that a later run retries (a non-transient read failure).
_DEGRADED_BODY = "unavailable:%"
# messages.attachments_status: 'complete' (the listing reached its end, or the
# message has none) or 'truncated' (the listing stopped at a cap: some
# attachments of this version were never listed). A truncated version is
# degraded: a later run lists it again even when its changeKey is unchanged, and
# its folder and mailbox are not complete while it is current.
ATTACHMENTS_TRUNCATED = "truncated"
# A listing still cut at the hard cap (MAX_ATTACHMENTS_LISTED) after
# CAPPED_AFTER_ATTEMPTS re-lists: no longer re-listed (it stops consuming the repair
# budget), but still counted as truncated, so its folder and mailbox stay incomplete.
ATTACHMENTS_TRUNCATED_CAPPED = "truncated_capped"
_TRUNCATED_STATES = (ATTACHMENTS_TRUNCATED, ATTACHMENTS_TRUNCATED_CAPPED)
CAPPED_AFTER_ATTEMPTS = 2
REPAIRS_PER_ROUND = 100
TIME_BUDGET = "time_budget"   # a folder left when the run's time budget was spent
_ATTACHMENT_COLUMNS = ("(message_handle, attachment_id, l1_handle, mailbox_id, metadata, "
                       "content_sha256, content_status, recorded_at, text_status, "
                       "text_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)")
# Compact storage (module docstring, *Storage*): an attachment original over this many
# bytes is dropped once every reading of it is kept (its ``.dropped`` form), unless a
# later reading still needs it (OCR pending, never read) or it is a container whose
# members are cited. Originals kept for a pending reading are bounded by
# ``MAX_PENDING_OCR_BYTES``: past it, new PDF attachments are not downloaded
# (``content_status`` ``deferred:ocr_backlog``) until the OCR backlog drains.
DROP_ORIGINALS_ABOVE = 256 * 1024
MAX_PENDING_OCR_BYTES = 1 << 30
ATTACHMENT_DEFERRED = "deferred:ocr_backlog"
_COMPRESSED_KINDS = ("body", "text")
# attachments.content_status of bytes that failed verification (module docstring).
MISMATCH_SHA256 = "mismatch:sha256"
MISMATCH_SIZE = "mismatch:size"
_MISMATCH = "mismatch:%"


# One tool call's mailbox visibility (``MailCaptureStore._visible``): None outside a
# scope, so every read decides afresh; a scope decides once per store file, reader,
# audience and exclusion, and forgets when it ends.
_VISIBILITY: contextvars.ContextVar[dict[Any, Any] | None] = contextvars.ContextVar(
    "plx_mail_visibility", default=None)


@contextmanager
def visibility_scope() -> Iterator[None]:
    """One tool call's mailbox visibility, decided once and reused by every mail read
    of the call (a per-handle gate re-read otherwise re-decides every mailbox for each
    message). A revocation or exclusion recorded during the call applies from the next
    call. Nested scopes share the outer one."""
    if _VISIBILITY.get() is not None:
        yield
        return
    token = _VISIBILITY.set({})
    try:
        yield
    finally:
        _VISIBILITY.reset(token)


class MailCaptureStore:
    """The capture store under ``<state_dir>/mail-capture`` (see module docstring).

    ``max_store_bytes`` bounds the store (SQLite file plus blobs): a write that
    would pass it raises ``CaptureError("store_full")`` and the capture stops
    the folder ``incomplete`` with that reason. ``None`` = unbounded."""

    def __init__(self, state_dir: str | Path, *, clock: Callable[[], datetime] = _now,
                 max_store_bytes: int | None = None, people: PeopleExclusion | None = None,
                 deny: Any = None) -> None:
        if max_store_bytes is not None and (isinstance(max_store_bytes, bool)
                                            or not isinstance(max_store_bytes, int)
                                            or max_store_bytes < 1):
            raise CaptureError("max_store_bytes_invalid")
        state = assert_outside_git(state_dir)
        self.root = state / STORE_DIR
        self.blobs = self.root / "blobs" / "sha256"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self.root, self.root / "blobs", self.blobs):  # ours only
            os.chmod(directory, 0o700)
        self.path = self.root / DB_FILE
        self._clock = clock
        self.max_store_bytes = max_store_bytes
        # The exclusion every read applies (``withheld_mailboxes``): this people guard,
        # else the process environment's; this deny list, else the shipped one.
        self.people = people
        self.deny = deny
        self.db = sqlite3.connect(self.path, timeout=30)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        wal_journal(self.db)
        self.db.execute("PRAGMA foreign_keys = ON")
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master "
                                                "WHERE type='table'")}
        if "messages" in tables and "meta" not in tables:
            self.db.close()
            raise CaptureError("store_schema_outdated")
        self.db.executescript(_DDL)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                            (STORE_SCHEMA_VERSION,))
        version = self.db.execute("SELECT value FROM meta WHERE key='schema_version'"
                                  ).fetchone()[0]
        if version == "2":
            version = self._migrate_from_2()
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "attachment_list_attempts" not in columns:  # an early schema-3 file
            with self.db:
                self.db.execute("ALTER TABLE messages ADD COLUMN attachment_list_attempts "
                                "INTEGER NOT NULL DEFAULT 0")
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(attachments)")}
        if "text_status" not in columns:   # captured before attachment text: read later
            with self.db:
                self.db.execute("ALTER TABLE attachments ADD COLUMN text_status TEXT")
                self.db.execute("ALTER TABLE attachments ADD COLUMN text_sha256 TEXT "
                                "REFERENCES blobs(sha256)")
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(folders)")}
        with self.db:
            # The history older than a round's window, read newest first after the round
            # (``history`` all): its listing cursor, the fixed ``before`` bound the cursor
            # was minted for, and ``complete`` once it reached its end.
            for name in ("backfill_next_url", "backfill_before", "backfill_status"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE folders ADD COLUMN {name} TEXT")
            self.db.execute(_FOLDER_LISTING_DDL)
            # Compact storage: the size of a blob's stored form (NULL: its bytes, as
            # before) and when its original was dropped (``settle_original``).
            have = {r[1] for r in self.db.execute("PRAGMA table_info(blobs)")}
            for name, kind in (("stored_bytes", "INTEGER"), ("dropped_at", "TEXT")):
                if name not in have:
                    self.db.execute(f"ALTER TABLE blobs ADD COLUMN {name} {kind}")
            for name, on in (("attachments_by_content", "attachments(content_sha256)"),
                             ("attachments_by_text", "attachments(text_sha256)"),
                             ("messages_by_body", "messages(body_sha256)")):
                self.db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {on}")
        if version != STORE_SCHEMA_VERSION:
            self.db.close()
            raise CaptureError("store_schema_outdated")

    # Columns a store of the current schema holds once ``__init__`` brought it up to date.
    _CURRENT_COLUMNS = {"messages": {"attachment_list_attempts"},
                        "attachments": {"text_status", "text_sha256"},
                        "folders": {"backfill_next_url", "backfill_before", "backfill_status"},
                        "blobs": {"stored_bytes", "dropped_at"}}

    @classmethod
    def open_read_only(cls, state_dir: str | Path, *, timeout: float = 5.0
                       ) -> "MailCaptureStore":
        """The store opened only to read it (a host serving captured mail): ``mode=ro``,
        no directory, schema, journal or permission change and no write, so a reader
        kept open never holds a capture's write lock. Its people guard and deny list
        are the process's (the constructor's defaults). A missing store, another schema
        version or a store not yet brought up to date by a writer raises
        ``CaptureError``. The connection may serve another thread: its holder
        serialises use."""
        store = cls.__new__(cls)
        store.root = Path(state_dir).expanduser().resolve() / STORE_DIR
        store.blobs = store.root / "blobs" / "sha256"
        store.path = store.root / DB_FILE
        store._clock, store.max_store_bytes = _now, None
        store.people, store.deny = None, None
        if not store.path.is_file():
            raise CaptureError("store_missing")
        store.db = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True,
                                   timeout=timeout, check_same_thread=False)
        store._read_only = True
        store.db.row_factory = sqlite3.Row
        try:
            version = store.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = version is not None and version[0] == STORE_SCHEMA_VERSION and all(
                wanted <= {r[1] for r in store.db.execute(f"PRAGMA table_info({table})")}
                for table, wanted in cls._CURRENT_COLUMNS.items())
        except sqlite3.Error:
            current = False
        if not current:
            store.db.close()
            raise CaptureError("store_schema_outdated")
        return store

    def _migrate_from_2(self) -> str:
        """Schema 2 had no attachment-list state, and its listings stopped at 50
        items without saying so: a version with 50 or more stored attachment rows
        may have been cut, so it is marked ``truncated`` (re-listed by the next
        run); every other version is ``complete``.

        Known limit: a schema-2 listing could also stop at its 5-page cap with fewer
        than 50 items (pages under 10 items). Schema 2 stored no listing state or
        page count, so such a cut cannot be recognised here; those versions stay
        ``complete`` until a new changeKey makes the capture list them again."""
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        with self.db:
            if "attachments_status" not in columns:
                self.db.execute("ALTER TABLE messages ADD COLUMN attachments_status TEXT "
                                "NOT NULL DEFAULT 'complete'")
            self.db.execute(
                "UPDATE messages SET attachments_status=? WHERE (SELECT COUNT(*) FROM "
                "attachments a WHERE a.message_handle=messages.l1_handle) >= 50",
                (ATTACHMENTS_TRUNCATED,))
            self.db.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                            (STORE_SCHEMA_VERSION,))
        return STORE_SCHEMA_VERSION

    def __repr__(self) -> str:
        return "MailCaptureStore()"

    def close(self) -> None:
        """Close the connection; a writer first checkpoints its WAL (no wait:
        ``capture_store.checkpoint``)."""
        if not getattr(self, "_read_only", False):
            checkpoint(self.db)
        self.db.close()

    def now(self) -> str:
        return _stamp(self._clock())

    # ---------------------------------------------------------------- size

    def store_bytes(self) -> int:
        """The SQLite file plus every blob as stored (compressed, or its ``.dropped``
        form)."""
        blobs = self.db.execute("SELECT COALESCE(SUM(COALESCE(stored_bytes, bytes)), 0) "
                                "FROM blobs").fetchone()[0]
        return sqlite_bytes(self.db) + blobs

    def store_full(self) -> bool:
        return self.max_store_bytes is not None and self.store_bytes() >= self.max_store_bytes

    def _reserve(self, size: int) -> None:
        if self.max_store_bytes is not None and \
                self.store_bytes() + size > self.max_store_bytes:
            raise CaptureError("store_full")

    # ---------------------------------------------------------------- writes

    def record_mailbox(self, identity: Mapping[str, Any]) -> None:
        now = self.now()
        with self.db:
            self.db.execute(
                "INSERT INTO mailboxes VALUES (?,?,?,?,?,?,?) ON CONFLICT(mailbox_id) DO UPDATE "
                "SET upn=excluded.upn, identities=excluded.identities, "
                "resources=excluded.resources, source_acl=excluded.source_acl, "
                "identity_checked_at=excluded.identity_checked_at",
                (identity["object_id"], identity["upn"], json.dumps(identity["identities"]),
                 json.dumps(identity["resources"]), json.dumps(identity["source_acl"]),
                 now, now))

    def folder_state(self, mailbox_id: str, folder_key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM folders WHERE mailbox_id=? AND folder_key=?",
                              (mailbox_id, folder_key)).fetchone()
        return dict(row) if row else None

    def save_folder(self, mailbox_id: str, folder_key: str, **fields: Any) -> None:
        current = self.folder_state(mailbox_id, folder_key)
        if current is None:
            with self.db:
                self.db.execute("INSERT INTO folders (mailbox_id, folder_key, status) "
                                "VALUES (?,?,?)", (mailbox_id, folder_key, "new"))
        if fields:
            names = sorted(fields)
            with self.db:
                self.db.execute(
                    "UPDATE folders SET " + ", ".join(f"{n}=?" for n in names)
                    + " WHERE mailbox_id=? AND folder_key=?",
                    (*[fields[n] for n in names], mailbox_id, folder_key))

    def current_version(self, mailbox_id: str, message_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM messages WHERE mailbox_id=? AND message_id=? "
                              "AND superseded_at IS NULL", (mailbox_id, message_id)).fetchone()
        return dict(row) if row else None

    def _latest(self, handle: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM messages WHERE l1_handle=? "
                               "ORDER BY version_id DESC LIMIT 1", (handle,)).fetchone()

    def observe(self, handle: str, folder_id: str) -> str:
        """A version listed again by the source.

        ``seen``: it is the current version (last seen time and folder updated).
        ``reappeared``: it was captured before but is not current (removed, or a
        later changeKey came between, A -> B -> A): a new validity interval row
        is opened from now, copying the captured content (no source read); the
        earlier intervals stay as they were. ``new``: never captured."""
        latest = self._latest(handle)
        if latest is None:
            return "new"
        now = self.now()
        with self.db:
            if latest["superseded_at"] is None:
                self.db.execute("UPDATE messages SET last_seen_at=?, folder_id=? "
                                "WHERE version_id=?", (now, folder_id, latest["version_id"]))
                return "seen"
            self._supersede_current(latest["mailbox_id"], latest["message_id"], now)
            self.db.execute(
                "INSERT INTO messages (l1_handle, mailbox_id, message_id, change_key, "
                "folder_id, record, digest, body_status, body_sha256, attachments_status, "
                "recorded_at, superseded_at, removed_reason, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
                (handle, latest["mailbox_id"], latest["message_id"], latest["change_key"],
                 folder_id, latest["record"], latest["digest"], latest["body_status"],
                 latest["body_sha256"], latest["attachments_status"], now, now))
            self.db.execute("INSERT INTO message_events VALUES (?,?,?)",
                            (handle, "reappeared", now))
        return "reappeared"

    def _supersede_current(self, mailbox_id: str, message_id: str, now: str,
                           reason: str | None = None) -> int:
        rows = self.db.execute("SELECT version_id, l1_handle FROM messages WHERE "
                               "mailbox_id=? AND message_id=? AND superseded_at IS NULL",
                               (mailbox_id, message_id)).fetchall()
        for row in rows:
            self.db.execute("UPDATE messages SET superseded_at=?, removed_reason=? "
                            "WHERE version_id=?", (now, reason, row["version_id"]))
            self.db.execute("INSERT INTO message_events VALUES (?,?,?)",
                            (row["l1_handle"], "removed" if reason else "superseded", now))
        return len(rows)

    def put_blob(self, content: bytes, expected_sha256: str, *, kind: str = "attachment") -> str:
        """Content-addressed bytes (attachments, bodies, texts); one copy per sha256. A
        body or text is stored zlib-compressed (``<sha256>.z``); an attachment as its
        bytes, also when its original was dropped before (it is settled again)."""
        digest = sha256(content).hexdigest()
        if digest != expected_sha256:
            raise CaptureError("attachment_digest_mismatch")
        known = self.db.execute("SELECT 1 FROM blobs WHERE sha256=?", (digest,)).fetchone()
        if known is None or not self._present(digest):
            stored = (zlib.compress(content, 6) if kind in _COMPRESSED_KINDS else content)
            if known is None:
                self._reserve(len(stored))
            self._write_file(blob_path(self.blobs, digest, COMPRESSED_SUFFIX
                                       if kind in _COMPRESSED_KINDS else ""), stored)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO blobs (sha256, kind, bytes, stored_at,"
                                " stored_bytes) VALUES (?,?,?,?,?)",
                                (digest, kind, len(content), self.now(), len(stored)))
                self.db.execute("UPDATE blobs SET stored_bytes=?, dropped_at=NULL WHERE "
                                "sha256=?", (len(stored), digest))
        return digest

    def _present(self, digest: str) -> bool:
        """Whether the blob's bytes are stored (as they are or compressed)."""
        return (blob_path(self.blobs, digest).exists()
                or blob_path(self.blobs, digest, COMPRESSED_SUFFIX).exists())

    @staticmethod
    def _write_file(target: Path, data: bytes) -> None:
        target.parent.mkdir(exist_ok=True, mode=0o700)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, target)

    def blob(self, digest: str) -> bytes:
        """A blob's bytes (stored as they are or compressed), re-hashed. An original the
        store dropped raises ``FileNotFoundError`` like a missing file."""
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise CaptureError("attachment_digest_invalid")
        try:
            content: bytes | None = blob_path(self.blobs, digest).read_bytes()
        except FileNotFoundError:
            content = inflate(blob_path(self.blobs, digest, COMPRESSED_SUFFIX).read_bytes())
        if content is None or sha256(content).hexdigest() != digest:
            raise CaptureError("attachment_digest_mismatch")
        return content

    def body_text(self, row: Mapping[str, Any]) -> str | None:
        digest = row["body_sha256"]
        return None if digest is None else self.blob(digest).decode("utf-8")

    def known_attachment(self, mailbox_id: str, message_id: str, attachment_id: str,
                         metadata: Mapping[str, Any]) -> dict[str, Any] | None:
        """An earlier captured copy of the same attachment (same message, id, size and
        last-modified), so a message whose changeKey moved (read, flagged) does not
        re-download unchanged bytes."""
        rows = self.db.execute(
            "SELECT * FROM attachments WHERE mailbox_id=? AND attachment_id=? AND "
            "content_status='captured' AND message_handle IN (SELECT l1_handle FROM messages "
            "WHERE mailbox_id=? AND message_id=?)",
            (mailbox_id, attachment_id, mailbox_id, message_id))
        for row in rows:
            known = json.loads(row["metadata"])
            if (known.get("size") == metadata.get("size")
                    and known.get("last_modified") == metadata.get("last_modified")):
                return {**dict(row), "metadata": known}
        return None

    def captured_attachments(self, mailbox_id: str, message_id: str) -> list[dict[str, Any]]:
        """The captured attachments of every stored version of one message (what
        :meth:`known_attachment` matches against), for a reader thread that may not
        touch this store."""
        return [{**dict(row), "metadata": json.loads(row["metadata"])}
                for row in self.db.execute(
                    "SELECT * FROM attachments WHERE mailbox_id=? AND content_status='captured' "
                    "AND message_handle IN (SELECT l1_handle FROM messages WHERE mailbox_id=? "
                    "AND message_id=?)", (mailbox_id, mailbox_id, message_id))]

    def folder_listing(self, mailbox_id: str) -> tuple[list[dict[str, Any]], str | None]:
        """(the stored folder listing of a mailbox, when it was listed; None: never)."""
        rows = [dict(r) for r in self.db.execute(
            "SELECT folder_id, total, listed_at FROM mailbox_folders WHERE mailbox_id=? "
            "ORDER BY rowid", (mailbox_id,))]
        return rows, (min(r["listed_at"] for r in rows) if rows else None)

    def save_folder_listing(self, mailbox_id: str, folders: Sequence[Mapping[str, Any]]
                            ) -> None:
        now = self.now()
        with self.db:
            self.db.execute("DELETE FROM mailbox_folders WHERE mailbox_id=?", (mailbox_id,))
            self.db.executemany("INSERT OR IGNORE INTO mailbox_folders VALUES (?,?,?,?)",
                                [(mailbox_id, f["id"], f.get("total"), now) for f in folders])

    def folder_holds_current(self, mailbox_id: str, folder_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM messages WHERE mailbox_id=? AND folder_id=? "
                               "AND superseded_at IS NULL LIMIT 1",
                               (mailbox_id, folder_id)).fetchone() is not None

    def _stored_record(self, record: Mapping[str, Any]) -> tuple[str, str | None]:
        """The record JSON without the body (kept once, content-addressed) and the
        body's sha256; identical bodies across changeKeys share one blob."""
        body = record.get("body_text")
        body_sha = None
        if isinstance(body, str):
            raw = body.encode("utf-8")
            body_sha = self.put_blob(raw, sha256(raw).hexdigest(), kind="body")
        stored = {key: value for key, value in record.items() if key != "body_text"}
        return json.dumps(stored, sort_keys=True), body_sha

    def add_version(self, record: Mapping[str, Any], *, folder_id: str, body_status: str,
                    attachments: Sequence[Mapping[str, Any]],
                    attachments_status: str = "complete") -> int:
        """Insert a new message version, superseding the current one. Returns the
        number of versions superseded (0 or 1). ``attachments_status`` is
        ``truncated`` when the attachment listing stopped at a cap."""
        stored, body_sha = self._stored_record(record)
        self._reserve(len(stored))
        now = self.now()
        with self.db:
            superseded = self._supersede_current(record["mailbox"], record["id"], now)
            self.db.execute(
                "INSERT INTO messages (l1_handle, mailbox_id, message_id, change_key, "
                "folder_id, record, digest, body_status, body_sha256, attachments_status, "
                "recorded_at, superseded_at, removed_reason, last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
                (record["citation"], record["mailbox"], record["id"], record["change_key"],
                 folder_id, stored, record["content_sha256"], body_status, body_sha,
                 attachments_status, now, now))
            self.db.execute("INSERT INTO message_events VALUES (?,?,?)",
                            (record["citation"], "recorded", now))
            for item in attachments:
                self.db.execute("INSERT OR REPLACE INTO attachments " + _ATTACHMENT_COLUMNS,
                                (record["citation"], item["id"], item.get("l1_handle"),
                                 record["mailbox"], json.dumps(item["metadata"], sort_keys=True),
                                 item.get("content_sha256"), item["content_status"], now,
                                 item.get("text_status"), item.get("text_sha256")))
        return superseded

    def fill_body(self, handle: str, record: Mapping[str, Any], body_status: str) -> None:
        """A degraded version's body, read later for the same changeKey (same version):
        every interval row of the handle gets the body; an event records the repair."""
        stored, body_sha = self._stored_record(record)
        now = self.now()
        with self.db:
            self.db.execute("UPDATE messages SET record=?, digest=?, body_status=?, "
                            "body_sha256=? WHERE l1_handle=?",
                            (stored, record["content_sha256"], body_status, body_sha, handle))
            self.db.execute("INSERT INTO message_events VALUES (?,?,?)",
                            (handle, "body_recovered", now))

    def fill_attachment(self, message_handle: str, entry: Mapping[str, Any]) -> None:
        with self.db:
            self.db.execute("UPDATE attachments SET l1_handle=?, metadata=?, content_sha256=?, "
                            "content_status=?, text_status=?, text_sha256=? WHERE "
                            "message_handle=? AND attachment_id=?",
                            (entry.get("l1_handle"), json.dumps(entry["metadata"],
                                                                sort_keys=True),
                             entry.get("content_sha256"), entry["content_status"],
                             entry.get("text_status"), entry.get("text_sha256"),
                             message_handle, entry["id"]))
            self.db.execute("INSERT INTO message_events VALUES (?,?,?)",
                            (message_handle, "attachment_recovered", self.now()))

    def add_attachments(self, message_handle: str, entries: Sequence[Mapping[str, Any]],
                        attachments_status: str) -> int:
        """Attachments of a truncated listing read again for the same version: rows
        not stored yet are added (stored rows are kept as they are) and the
        version's listing state is set, on every interval row of the handle.
        Returns the number of rows added."""
        row = self._latest(message_handle)
        if row is None:
            return 0
        now = self.now()
        added = 0
        with self.db:
            for item in entries:
                added += self.db.execute(
                    "INSERT OR IGNORE INTO attachments " + _ATTACHMENT_COLUMNS,
                    (message_handle, item["id"], item.get("l1_handle"), row["mailbox_id"],
                     json.dumps(item["metadata"], sort_keys=True),
                     item.get("content_sha256"), item["content_status"], now,
                     item.get("text_status"), item.get("text_sha256"))).rowcount
            self.db.execute("UPDATE messages SET attachments_status=?, "
                            "attachment_list_attempts=attachment_list_attempts+1 "
                            "WHERE l1_handle=?", (attachments_status, message_handle))
            if attachments_status not in _TRUNCATED_STATES:
                self.db.execute("INSERT INTO message_events VALUES (?,?,?)",
                                (message_handle, "attachment_list_recovered", now))
        return added

    def text_unread(self, mailbox_id: str | None, folder_id: str | None, limit: int
                    ) -> list[dict[str, Any]]:
        """Captured attachments of current versions in a folder whose text was never
        read (captured before attachment text), and PDFs whose scanned pages were not
        OCR-read (no engine then, or a failure from before the OCR tool exits were typed
        by tool, ``UNTYPED_OCR_FAILURES``): a later round re-reads their stored bytes,
        once, since the new reading is typed by tool and is not among those. ``None`` for
        the mailbox or folder: every one (the run's OCR backlog), newest version first."""
        where = " AND ".join(["1=1"] + (["a.mailbox_id=?"] if mailbox_id is not None else [])
                             + (["m.folder_id=?"] if folder_id is not None else []))
        order = ("m.version_id DESC" if mailbox_id is None or folder_id is None
                 else "m.version_id")
        unread = ",".join("?" * len(TEXT_UNREAD))
        rows = self.db.execute(
            "SELECT a.* FROM attachments a JOIN messages m ON m.l1_handle=a.message_handle "
            f"AND m.superseded_at IS NULL WHERE {where} AND "
            "a.content_status='captured' AND (a.text_status IS NULL OR a.text_status IN "
            f"({unread}) OR (a.text_status IN (?,?) AND NOT EXISTS (SELECT 1 FROM message_events e "
            f"WHERE e.l1_handle=a.l1_handle AND e.event='text_reextracted'))) ORDER BY {order}, "
            "a.attachment_id LIMIT ?",
            (*[v for v in (mailbox_id, folder_id) if v is not None], *TEXT_UNREAD,
             *UNTYPED_OCR_FAILURES, limit)).fetchall()
        return [dict(r) for r in rows if r["text_status"] is None or is_paged_file(
            {"mime_type": (meta := json.loads(r["metadata"])).get("content_type"),
             "name": meta.get("name")})]

    def text_chain(self, l1_handle: str) -> list[dict[str, Any]]:
        """Every reading of one attachment's text, oldest first (``version`` 1, 2, ...):
        what the capture wrote, unfiltered (the capture's own bookkeeping; a reader goes
        through :meth:`text_versions`). An attachment whose text was read when it was
        captured, before any re-extraction, has that one reading as version 1."""
        rows = [dict(r) for r in self.db.execute(
            "SELECT version, text_status, text_sha256, recorded_at FROM "
            "attachment_text_versions WHERE l1_handle=? ORDER BY version", (l1_handle,))]
        if rows:
            return rows
        found = self.db.execute("SELECT text_status, text_sha256, recorded_at FROM "
                                "attachments WHERE l1_handle=?", (l1_handle,)).fetchone()
        if found is None or found["text_status"] is None:
            return []
        return [{"version": 1, **dict(found)}]

    def text_versions(self, l1_handle: Any, actor: Actor, audience: LiveSourceAudience
                      ) -> list[dict[str, Any]]:
        """The readings of an attachment's text a reader may see (:meth:`text_chain`);
        none for an attachment whose mailbox the reader may not see, or never captured
        (indistinguishable)."""
        if not isinstance(l1_handle, str):
            return []
        found = self.db.execute("SELECT message_handle FROM attachments WHERE l1_handle=?",
                                (l1_handle,)).fetchone()
        if found is None or self._message_row(actor, audience,
                                              found["message_handle"]) is None:
            return []
        return self.text_chain(l1_handle)

    def fill_text(self, l1_handle: str, status: str, text_sha256: str | None) -> None:
        """Text of a captured attachment read again from its stored bytes: a new
        immutable version (:meth:`text_chain`; an earlier reading is kept and its
        citation still resolves to it), and the attachment's current pointer moves to
        it. The same reading again (status and text) is no new version. An event
        records the read either way."""
        with self.db:
            chain = self.text_chain(l1_handle)
            now = self.now()
            if chain and (chain[-1]["text_status"], chain[-1]["text_sha256"]) == \
                    (status, text_sha256):   # read again, same reading: no new version
                self.db.execute("INSERT INTO message_events VALUES (?,?,?)",
                                (l1_handle, "text_reextracted", now))
                return
            if chain and not self.db.execute(
                    "SELECT 1 FROM attachment_text_versions WHERE l1_handle=?",
                    (l1_handle,)).fetchone():   # the reading made at capture: version 1
                self.db.execute("INSERT INTO attachment_text_versions VALUES (?,?,?,?,?)",
                                (l1_handle, 1, chain[0]["text_status"],
                                 chain[0]["text_sha256"], chain[0]["recorded_at"]))
            self.db.execute("INSERT INTO attachment_text_versions VALUES (?,?,?,?,?)",
                            (l1_handle, len(chain) + 1, status, text_sha256, now))
            self.db.execute("UPDATE attachments SET text_status=?, text_sha256=? "
                            "WHERE l1_handle=?", (status, text_sha256, l1_handle))
            self.db.execute("INSERT INTO message_events VALUES (?,?,?)",
                            (l1_handle, "text_reextracted", now))

    def put_text(self, text: str | None) -> str | None:
        """The pinned blob of an attachment's captured text (None without text)."""
        if text is None:
            return None
        raw = text.encode("utf-8")
        return self.put_blob(raw, sha256(raw).hexdigest(), kind="text")

    # ---------------------------------------------------------------- compact storage

    @staticmethod
    def _awaits_reading(text_status: Any, metadata: Mapping[str, Any]) -> bool:
        """Whether a later round reads an attachment's text from its stored bytes again
        (:meth:`text_unread`): never read, a scan whose OCR is pending or had no engine,
        or an OCR failure from before failures were typed. Its bytes are kept."""
        if text_status is None or text_status in UNTYPED_OCR_FAILURES:
            return True
        return text_status in TEXT_UNREAD and is_paged_file(
            {"mime_type": metadata.get("content_type"), "name": metadata.get("name")})

    def _pending_rows(self, digest: str | None = None) -> list[sqlite3.Row]:
        """Captured attachments of current versions whose bytes a later reading needs
        (:meth:`_awaits_reading`), with their blob's size; of one blob, or of all."""
        unread = (*TEXT_UNREAD, *UNTYPED_OCR_FAILURES)
        marks = ",".join("?" * len(unread))
        rows = self.db.execute(
            "SELECT a.content_sha256 AS digest, a.l1_handle, a.metadata, a.text_status, "
            "a.text_sha256, b.bytes FROM "
            "attachments a JOIN blobs b ON b.sha256=a.content_sha256 AND b.dropped_at IS NULL "
            "WHERE a.content_status='captured' AND (a.text_status IS NULL OR a.text_status IN "
            f"({marks})) AND EXISTS (SELECT 1 FROM messages m WHERE m.l1_handle="
            "a.message_handle AND m.superseded_at IS NULL)"
            + ("" if digest is None else " AND a.content_sha256=?"),
            (*unread, *(() if digest is None else (digest,)))).fetchall()
        return [r for r in rows if self._awaits_reading(r["text_status"],
                                                        json.loads(r["metadata"]))]

    def pending_ocr_bytes(self) -> int:
        """Bytes of the originals kept only because a later reading needs them (the OCR
        backlog), each blob once."""
        return sum({r["digest"]: r["bytes"] for r in self._pending_rows()}.values())

    def _referenced_as_text(self, digest: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM messages WHERE body_sha256=? UNION ALL SELECT 1 FROM attachments "
            "WHERE text_sha256=? UNION ALL SELECT 1 FROM attachment_text_versions WHERE "
            "text_sha256=? LIMIT 1", (digest, digest, digest)).fetchone() is not None

    def original_dropped(self, digest: Any) -> bool:
        """Whether the store dropped this attachment original (its readings kept)."""
        row = self.db.execute("SELECT dropped_at FROM blobs WHERE sha256=?",
                              (digest,)).fetchone()
        return row is not None and row["dropped_at"] is not None

    def settle_original(self, digest: str, *, content: bytes | None = None,
                        readings: Mapping[str, Any] | None = None,
                        defer_ocr: bool = False) -> str:
        """Drop an attachment original once nothing needs its bytes (module docstring,
        *Storage*): over :data:`DROP_ORIGINALS_ABOVE`, no current attachment awaiting a
        reading of it, not a body or text, not a container, and every reading a reader
        makes of it (each captured name and type of these bytes, without and with each
        pinned OCR text it has) made from the bytes and kept in its ``.dropped`` form.
        ``content``: its bytes when in hand; ``readings``: readings made already (the
        capture's reader thread). The ``.dropped`` form is written before the bytes go,
        so a reader never finds neither. Returns the outcome (``dropped``, ``kept_small``,
        ``kept_pending``, ``kept_text``, ``kept_container``, ``kept_unreadable``,
        ``unreferenced``, ``absent``).

        ``defer_ocr`` (scope ``ocr`` ``deferred``, ``compact --defer-ocr``): a scan whose
        OCR is pending (``TEXT_UNREAD``) is dropped too; its current attachments get a new
        text version ``ocr_deferred`` keeping the text layer (:data:`OCR_DEFERRED`), read
        later by :func:`ocr_deferred_pass`. An original dropped before is settled again
        when its bytes are in hand (``content``, re-fetched and re-hashed): the readings
        of a new text version (its OCR text) are added to its ``.dropped`` form."""
        row = self.db.execute("SELECT bytes, dropped_at FROM blobs WHERE sha256=?",
                              (digest,)).fetchone()
        if row is None or _SHA256.fullmatch(digest) is None:
            return "absent"
        dropped = row["dropped_at"] is not None
        if dropped and (content is None or sha256(content).hexdigest() != digest):
            blob_path(self.blobs, digest).unlink(missing_ok=True)   # an interrupted drop
            return "dropped"
        if not dropped and row["bytes"] <= DROP_ORIGINALS_ABOVE:
            return "kept_small"
        if not dropped and self._referenced_as_text(digest):
            # Bytes that are their own text (a plain-text or CSV file): kept, compressed.
            self._compress(digest)
            return "kept_text"
        captured = self.db.execute(
            "SELECT l1_handle, metadata, text_status, text_sha256 FROM attachments WHERE "
            "content_sha256=? AND content_status='captured'", (digest,)).fetchall()
        if not captured:
            return "unreferenced"
        pending = self._pending_rows(digest)
        if pending and not (defer_ocr and all(r["text_status"] in TEXT_UNREAD
                                              for r in pending)):
            return "kept_pending"
        if content is None:
            try:
                content = self.blob(digest)
            except (CaptureError, OSError):
                return "kept_unreadable"
        from operating.use_case_parts import is_container
        keys: set[tuple[str, str, str | None]] = set()
        for found in captured:
            mime, name = file_type(json.loads(found["metadata"]))
            if is_container(content, name, mime):
                return "kept_container"   # its members are cited from its bytes
            chain = self.text_chain(found["l1_handle"]) if found["l1_handle"] else []
            keys.add((mime, name, None))
            keys.update((mime, name, ocr_digest(v)) for v in
                        [*chain, dict(found)] if ocr_digest(v) is not None)
        kept = read_dropped(self.blobs, digest)
        have = dict(kept["readings"]) if kept is not None else {}
        have.update(readings or {})
        missing = [k for k in sorted(keys, key=lambda k: reading_key(*k))
                   if reading_key(*k) not in have]

        def text(text_digest: str) -> str | None:
            try:
                return self.blob(text_digest).decode("utf-8")
            except (CaptureError, OSError, UnicodeDecodeError):
                return None
        made = readings_of(content, missing, text)
        if made is None:
            return "kept_unreadable"
        have.update(made)
        from .graph_read import quick_xor_hash
        size = write_dropped(self.blobs, digest, len(content), have, quick_xor_hash(content))
        for found in pending:   # its scans' OCR put off: a new text version, layer kept
            self.fill_text(found["l1_handle"], OCR_DEFERRED, found["text_sha256"])
        with self.db:
            self.db.execute("UPDATE blobs SET dropped_at=COALESCE(dropped_at, ?), "
                            "stored_bytes=? WHERE sha256=?", (self.now(), size, digest))
        blob_path(self.blobs, digest).unlink(missing_ok=True)
        return "dropped"

    def _compress(self, digest: str) -> bool:
        """Store a blob kept as its bytes zlib-compressed instead (``<sha256>.z``, written
        before the bytes go); False when it is not stored as its bytes or unreadable."""
        raw = blob_path(self.blobs, digest)
        if not raw.exists():
            return False
        try:
            content = self.blob(digest)
        except (CaptureError, OSError):
            return False
        packed = zlib.compress(content, 6)
        self._write_file(blob_path(self.blobs, digest, COMPRESSED_SUFFIX), packed)
        with self.db:
            self.db.execute("UPDATE blobs SET stored_bytes=? WHERE sha256=?",
                            (len(packed), digest))
        raw.unlink()
        return True

    def ocr_deferred_count(self, mailbox_id: str | None = None) -> int:
        """Attachments of current versions whose scanned pages wait for the
        ``ocr-deferred`` pass (``ocr_deferred``)."""
        return len(self.ocr_deferred_rows(mailbox_id, None))

    def ocr_deferred_rows(self, mailbox_id: str | None, limit: int | None
                          ) -> list[dict[str, Any]]:
        """Current attachments whose OCR was put off with their bytes dropped
        (``ocr_deferred``), newest version first: each one's attachment handle, digest,
        metadata and re-fetch ids."""
        rows = self.db.execute(
            "SELECT a.l1_handle, a.content_sha256, a.metadata, a.mailbox_id, a.attachment_id,"
            " m.message_id FROM attachments a JOIN messages m ON m.l1_handle=a.message_handle "
            "AND m.superseded_at IS NULL WHERE a.content_status='captured' AND "
            "a.text_status=?" + ("" if mailbox_id is None else " AND a.mailbox_id=?")
            + " ORDER BY m.version_id DESC, a.attachment_id"
            + ("" if limit is None else " LIMIT ?"),
            (OCR_DEFERRED, *(() if mailbox_id is None else (mailbox_id,)),
             *(() if limit is None else (limit,)))).fetchall()
        return [dict(r) for r in rows]

    def deferred_attachments(self, mailbox_id: str, folder_id: str | None = None) -> int:
        """Attachments of current versions not downloaded while the OCR backlog was full
        (``deferred:ocr_backlog``), retried by later rounds."""
        sql = ("SELECT COUNT(*) FROM attachments a JOIN messages m ON "
               "m.l1_handle=a.message_handle AND m.superseded_at IS NULL WHERE "
               "a.mailbox_id=? AND a.content_status=?")
        params: list[Any] = [mailbox_id, ATTACHMENT_DEFERRED]
        if folder_id is not None:
            sql += " AND m.folder_id=?"
            params.append(folder_id)
        return self.db.execute(sql, params).fetchone()[0]

    def compact(self, *, vacuum: bool = True, defer_ocr: bool = False,
                free_bytes: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free
                ) -> dict[str, Any]:
        """One-time compaction of a store captured before compact storage (CLI
        ``compact``): bodies and texts stored as they are are compressed; every
        attachment original nothing needs is dropped (:meth:`settle_original`); then the
        SQLite file is rewritten (``VACUUM INTO`` a new file in the same directory,
        checked, swapped in atomically) when the free disk covers the new file, else it
        is left as it is (``vacuum`` ``skipped:<why>``). Counts and bytes only. Never run
        it beside a capture (the CLI holds the host's cycle lock)."""
        before = self._disk_bytes()
        result: dict[str, Any] = {"bytes_before": before, "compressed": 0, "outcomes": {}}
        for digest, kind, size in self.db.execute(
                "SELECT sha256, kind, bytes FROM blobs WHERE dropped_at IS NULL AND "
                "(stored_bytes IS NULL OR stored_bytes=bytes)").fetchall():
            raw = blob_path(self.blobs, digest)
            if not raw.exists() or self.db.execute(
                    "SELECT 1 FROM attachments WHERE content_sha256=? LIMIT 1",
                    (digest,)).fetchone() is not None:
                continue   # an attachment's bytes are settled below, never compressed
            if self._referenced_as_text(digest):
                result["compressed"] += self._compress(digest)
        for (digest,) in self.db.execute(
                "SELECT DISTINCT content_sha256 FROM attachments WHERE content_status="
                "'captured' AND content_sha256 IS NOT NULL ORDER BY content_sha256"
                ).fetchall():
            outcome = self.settle_original(digest, defer_ocr=defer_ocr)
            result["outcomes"][outcome] = result["outcomes"].get(outcome, 0) + 1
        result["pending_ocr_bytes"] = self.pending_ocr_bytes()
        result["vacuum"] = self._vacuum(free_bytes) if vacuum else "skipped:not_asked"
        result["bytes_after"] = self._disk_bytes()
        return result

    def _disk_bytes(self) -> int:
        """Bytes on disk of the store: the SQLite file with its WAL, every blob file."""
        total = 0
        for path in (self.path, self.path.with_name(self.path.name + "-wal")):
            total += path.stat().st_size if path.exists() else 0
        for folder in self.blobs.iterdir():
            if folder.is_dir():
                total += sum(f.stat().st_size for f in folder.iterdir() if f.is_file())
        return total

    def _vacuum(self, free_bytes: Callable[[Path], int]) -> str:
        """Rewrite the SQLite file without its free pages: into a new file beside it,
        checked (integrity and every table's row count), then swapped in by an atomic
        rename. Skipped when the free disk does not cover the new file with a margin, or
        when the WAL cannot be emptied first (another connection reading)."""
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        used = (self.db.execute("PRAGMA page_count").fetchone()[0]
                - self.db.execute("PRAGMA freelist_count").fetchone()[0]) * page_size
        if free_bytes(self.root) < used + used // 10 + 64 * 1024 * 1024:
            return "skipped:disk_space"
        busy = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        if busy:
            return "skipped:wal_busy"
        target = self.path.with_name(f".{self.path.name}.vacuum.tmp")
        target.unlink(missing_ok=True)
        tables = [r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts = {t: self.db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                  for t in tables}
        self.db.execute("VACUUM INTO ?", (str(target),))
        check = sqlite3.connect(target)
        try:
            whole = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and all(
                check.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] == n
                for t, n in counts.items())
        finally:
            check.close()
        if not whole:
            target.unlink(missing_ok=True)
            return "skipped:copy_check_failed"
        os.chmod(target, 0o600)
        self.db.close()
        wal = self.path.with_name(self.path.name + "-wal")
        if wal.exists() and wal.stat().st_size:
            target.unlink(missing_ok=True)   # written to since the checkpoint: keep the file
            self._reopen()
            return "skipped:wal_busy"
        os.replace(target, self.path)
        for suffix in ("-wal", "-shm"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
        self._reopen()
        return "done"

    def _reopen(self) -> None:
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        wal_journal(self.db)
        self.db.execute("PRAGMA foreign_keys = ON")

    def degraded(self, mailbox_id: str, folder_id: str, limit: int) -> list[dict[str, Any]]:
        """Current versions in a folder whose body or an attachment read failed
        non-transiently, or whose attachment listing was truncated; a later run
        retries them (whatever their changeKey)."""
        rows = self.db.execute(
            "SELECT * FROM messages m WHERE mailbox_id=? AND folder_id=? AND "
            "superseded_at IS NULL AND (body_status LIKE ? OR attachments_status=? OR "
            "EXISTS (SELECT 1 FROM attachments a WHERE a.message_handle=m.l1_handle AND "
            "(a.content_status IN ('unavailable', ?) OR a.content_status LIKE ?))) "
            "ORDER BY version_id LIMIT ?",
            (mailbox_id, folder_id, _DEGRADED_BODY, ATTACHMENTS_TRUNCATED,
             ATTACHMENT_DEFERRED, _MISMATCH, limit)).fetchall()
        return [dict(r) for r in rows]

    def mismatched(self, mailbox_id: str, folder_id: str | None = None) -> int:
        """Attachments of current versions (of one folder, or of the mailbox) whose
        bytes failed verification (``mismatch:*``)."""
        sql = ("SELECT COUNT(*) FROM attachments a JOIN messages m ON "
               "m.l1_handle=a.message_handle AND m.superseded_at IS NULL WHERE "
               "a.mailbox_id=? AND a.content_status LIKE ?")
        params: list[Any] = [mailbox_id, _MISMATCH]
        if folder_id is not None:
            sql += " AND m.folder_id=?"
            params.append(folder_id)
        return self.db.execute(sql, params).fetchone()[0]

    def truncated_lists(self, mailbox_id: str, folder_id: str | None = None) -> int:
        """Current versions (of one folder, or of the mailbox) whose attachment
        listing is truncated (``truncated`` or ``truncated_capped``)."""
        sql = ("SELECT COUNT(*) FROM messages WHERE mailbox_id=? AND superseded_at IS NULL "
               "AND attachments_status IN (?, ?)")
        params: list[Any] = [mailbox_id, *_TRUNCATED_STATES]
        if folder_id is not None:
            sql += " AND folder_id=?"
            params.append(folder_id)
        return self.db.execute(sql, params).fetchone()[0]

    def remove(self, mailbox_id: str, message_id: str, folder_id: str, reason: str) -> int:
        """A delta ``@removed`` for this folder: the current version (if it was in
        this folder) is superseded with the removal reason; history is kept."""
        current = self.current_version(mailbox_id, message_id)
        if current is None or current["folder_id"] != folder_id:
            return 0
        with self.db:
            return self._supersede_current(mailbox_id, message_id, self.now(), reason)

    def reconcile_round(self, mailbox_id: str, folder_id: str, round_started_at: str,
                        received_since: str | None) -> int:
        """After a full (initial) round reached its deltaLink: every current version
        of this folder inside the round's window that the round did not list is gone
        from the source (deleted or moved while a token was stale). It is superseded
        with reason ``absent_after_resync``. Messages older than the window, which a
        windowed round never lists, are left alone."""
        rows = self.db.execute(
            "SELECT DISTINCT message_id FROM messages WHERE mailbox_id=? AND folder_id=? AND "
            "superseded_at IS NULL AND last_seen_at < ? AND "
            "json_extract(record, '$.received_at') >= ?",
            (mailbox_id, folder_id, round_started_at, received_since or "")).fetchall()
        now = self.now()
        with self.db:
            for row in rows:
                self._supersede_current(mailbox_id, row["message_id"], now,
                                        "absent_after_resync")
        return len(rows)

    # ---------------------------------------------------------------- exclusion

    def record_directory(self, departments: Mapping[str, Any]) -> None:
        """The directory department (any JSON value) of each mailbox a directory-wide
        listing named, recorded before the run so the deny list decides on it at read."""
        now = self.now()
        with self.db:
            self.db.executemany(
                "INSERT INTO mailbox_directory VALUES (?,?,?) ON CONFLICT(mailbox_id) DO "
                "UPDATE SET department=excluded.department, listed_at=excluded.listed_at",
                [(str(oid).lower(), json.dumps(value), now)
                 for oid, value in departments.items()])

    def _withholds(self, people: Any = None, *, undecided: bool = True
                   ) -> Callable[[Mapping[str, Any]], bool] | None:
        """What the exclusion in effect now withholds (decision
        2026-10-21-mail-exclusion-retroactive): ``check(row)`` over a ``_MAILBOX_ROWS``
        row, true for a mailbox whose recorded object ids (its addresses too once the
        guard is bound) name an excluded person, or that a directory-wide listing named
        and the deny list matches (addresses, department). The guard is ``people``, else
        the store's, else the process environment's; the deny list the store's, else the
        shipped one. None: every mailbox is withheld (a configured guard that cannot
        decide). No list configured at all applies no people check here: the store cannot
        know who is excluded, and the case runtime serves no captured mail without a
        usable guard (``operating.ontology_tools_evidence``). A deny list that cannot
        decide withholds every listed mailbox; ``undecided=False`` (the purge) keeps them
        instead, so an unusable list never deletes."""
        guard = people if people is not None else (
            self.people if self.people is not None else _env_people())
        if not _usable(guard):
            if getattr(guard, "unavailable", None) != "excluded_people_oids_absent":
                return None
            guard = None
        deny: list[Any] = []

        def withheld(row: Mapping[str, Any]) -> bool:
            identities = [row["mailbox_id"], row["upn"], *json.loads(row["identities"])]
            if guard is not None:
                try:
                    if guard.excluded(v for v in identities
                                      if guard.bound or is_object_id(v)):
                        return True
                except ExclusionUnavailable:
                    return undecided
            if row["listed_at"] is None:
                return False
            if not deny:
                deny.append(self.deny if self.deny is not None else _default_deny())
            if deny[0] is None:
                return undecided
            department = row["department"]
            return deny[0].denies({
                "userPrincipalName": row["upn"],
                "proxyAddresses": [f"smtp:{v}" for v in identities
                                   if isinstance(v, str) and "@" in v],
                "department": None if department is None else json.loads(department)})
        return withheld

    def withheld_mailboxes(self, people: Any = None) -> list[str] | None:
        """The captured mailboxes the exclusion withholds now (None: every one)."""
        withholds = self._withholds(people)
        if withholds is None:
            return None
        return [row["mailbox_id"] for row in self.db.execute(_MAILBOX_ROWS) if withholds(row)]

    def held(self, mailbox_ids: Sequence[str]) -> dict[str, Any]:
        """What the store holds of these mailboxes, unfiltered (the capture's own check,
        ``mail_directory_scope.run_cli``): mailbox rows, items (message versions plus
        attachments) and the items' handles, to be resolved as a reader."""
        marks = ",".join("?" * len(mailbox_ids))
        if not mailbox_ids:
            return {"mailboxes": 0, "items": 0, "handles": []}
        boxes = self.db.execute(f"SELECT COUNT(*) FROM mailboxes WHERE mailbox_id IN "
                                f"({marks})", list(mailbox_ids)).fetchone()[0]
        versions = self.db.execute(f"SELECT l1_handle FROM messages WHERE mailbox_id IN "
                                   f"({marks})", list(mailbox_ids)).fetchall()
        attachments = self.db.execute(f"SELECT l1_handle FROM attachments WHERE mailbox_id "
                                      f"IN ({marks})", list(mailbox_ids)).fetchall()
        handles = sorted({h for (h,) in [*versions, *attachments] if h})
        return {"mailboxes": boxes, "items": len(versions) + len(attachments),
                "handles": handles}

    def purge_withheld(self, people: PeopleExclusion) -> dict[str, int]:
        """Delete what the exclusion withholds (a capture run, its guard prepared): each
        withheld mailbox whole, and in every other mailbox each message (every version)
        whose sender or To/Cc recipient is an excluded person (the guard bound), with
        their attachments, attachment text readings and events; then every blob nothing
        references any more, with its file. Nothing is deleted on a guard or deny list
        that cannot decide. Returns the ``PURGE_COUNTS``."""
        counts = dict.fromkeys(PURGE_COUNTS, 0)
        withholds = self._withholds(people, undecided=False) if _usable(people) else None
        if withholds is None:
            return counts
        boxes = [row["mailbox_id"] for row in self.db.execute(_MAILBOX_ROWS)
                 if withholds(row)]
        addresses: list[str] = []
        if people.bound:
            found = self.db.execute(
                "SELECT DISTINCT lower(json_extract(record, '$.from')) FROM messages UNION "
                "SELECT DISTINCT lower(j.value) FROM messages, json_each(messages.record, "
                "'$.to') j UNION SELECT DISTINCT lower(j.value) FROM messages, "
                "json_each(messages.record, '$.cc') j").fetchall()
            addresses = [a for (a,) in found if isinstance(a, str) and people.is_excluded(a)]
        if not boxes and not addresses:
            return counts
        db = self.db
        db.execute("CREATE TEMP TABLE IF NOT EXISTS purge_boxes (id TEXT PRIMARY KEY)")
        db.execute("CREATE TEMP TABLE IF NOT EXISTS purge_addresses (a TEXT PRIMARY KEY)")
        db.execute("CREATE TEMP TABLE IF NOT EXISTS purge_messages (h TEXT PRIMARY KEY)")
        db.execute("CREATE TEMP TABLE IF NOT EXISTS purge_attachments (h TEXT PRIMARY KEY)")
        db.execute("CREATE TEMP TABLE IF NOT EXISTS purge_blobs (d TEXT PRIMARY KEY)")
        try:
            with db:
                for table in ("purge_boxes", "purge_addresses", "purge_messages",
                              "purge_attachments", "purge_blobs"):
                    db.execute(f"DELETE FROM temp.{table}")
                db.executemany("INSERT INTO temp.purge_boxes VALUES (?)", [(b,) for b in boxes])
                db.executemany("INSERT OR IGNORE INTO temp.purge_addresses VALUES (?)",
                               [(a,) for a in addresses])
                named = ("SELECT 1 FROM temp.purge_addresses WHERE a={}")
                pairs = db.execute(
                    "SELECT DISTINCT mailbox_id, message_id FROM messages WHERE mailbox_id IN "
                    "(SELECT id FROM temp.purge_boxes) OR EXISTS ("
                    + named.format("lower(json_extract(record, '$.from'))") + ") OR EXISTS "
                    "(SELECT 1 FROM json_each(messages.record, '$.to') j WHERE EXISTS ("
                    + named.format("lower(j.value)") + ")) OR EXISTS (SELECT 1 FROM "
                    "json_each(messages.record, '$.cc') j WHERE EXISTS ("
                    + named.format("lower(j.value)") + "))").fetchall()
                counts["messages"] = len(pairs)
                for mailbox_id, message_id in pairs:
                    db.execute("INSERT OR IGNORE INTO temp.purge_messages SELECT l1_handle "
                               "FROM messages WHERE mailbox_id=? AND message_id=?",
                               (mailbox_id, message_id))
                inside = ("message_handle IN (SELECT h FROM temp.purge_messages) OR "
                          "mailbox_id IN (SELECT id FROM temp.purge_boxes)")
                db.execute("INSERT OR IGNORE INTO temp.purge_attachments SELECT l1_handle "
                           f"FROM attachments WHERE l1_handle IS NOT NULL AND ({inside})")
                db.execute(
                    "INSERT OR IGNORE INTO temp.purge_blobs SELECT d FROM ("
                    "SELECT body_sha256 AS d FROM messages WHERE l1_handle IN (SELECT h FROM "
                    f"temp.purge_messages) UNION SELECT content_sha256 FROM attachments WHERE "
                    f"{inside} UNION SELECT text_sha256 FROM attachments WHERE {inside} UNION "
                    "SELECT text_sha256 FROM attachment_text_versions WHERE l1_handle IN "
                    "(SELECT h FROM temp.purge_attachments)) WHERE d IS NOT NULL")
                # The one deletion of attachment text history (an exclusion), the trigger
                # re-created inside the same transaction.
                db.execute("DROP TRIGGER attachment_text_versions_no_delete")
                db.execute("DELETE FROM attachment_text_versions WHERE l1_handle IN "
                           "(SELECT h FROM temp.purge_attachments)")
                db.execute(_TEXT_NO_DELETE)
                counts["attachments"] = db.execute(
                    f"DELETE FROM attachments WHERE {inside}").rowcount
                db.execute("DELETE FROM message_events WHERE l1_handle IN (SELECT h FROM "
                           "temp.purge_messages) OR l1_handle IN (SELECT h FROM "
                           "temp.purge_attachments)")
                counts["versions"] = db.execute(
                    "DELETE FROM messages WHERE l1_handle IN (SELECT h FROM "
                    "temp.purge_messages) OR mailbox_id IN (SELECT id FROM "
                    "temp.purge_boxes)").rowcount
                for table in ("folders", "mailbox_directory"):
                    db.execute(f"DELETE FROM {table} WHERE mailbox_id IN (SELECT id FROM "
                               "temp.purge_boxes)")
                counts["mailboxes"] = db.execute(
                    "DELETE FROM mailboxes WHERE mailbox_id IN (SELECT id FROM "
                    "temp.purge_boxes)").rowcount
                db.execute(
                    "DELETE FROM temp.purge_blobs WHERE d IN (SELECT body_sha256 FROM messages "
                    "UNION SELECT content_sha256 FROM attachments UNION SELECT text_sha256 "
                    "FROM attachments UNION SELECT text_sha256 FROM attachment_text_versions)")
                gone = [d for (d,) in db.execute("SELECT d FROM temp.purge_blobs")]
                counts["blobs"] = db.execute(
                    "DELETE FROM blobs WHERE sha256 IN (SELECT d FROM temp.purge_blobs)"
                ).rowcount
            for digest in gone:   # after the commit: a file is never missing for a row
                if _SHA256.fullmatch(digest):
                    for suffix in ("", COMPRESSED_SUFFIX, DROPPED_SUFFIX):
                        blob_path(self.blobs, digest, suffix).unlink(missing_ok=True)
        finally:
            for table in ("purge_boxes", "purge_addresses", "purge_messages",
                          "purge_attachments", "purge_blobs"):
                db.execute(f"DROP TABLE IF EXISTS temp.{table}")
        return counts

    # ---------------------------------------------------------------- reads (per reader)

    def _visible(self, actor: Actor, audience: LiveSourceAudience) -> dict[str, dict]:
        """Mailboxes this reader may see, decided on the identity set and owner ACL
        recorded at capture with the reader's audience, less every mailbox the exclusion
        in effect now withholds (:meth:`_withholds`; grant holders included). Anything
        else is absent. Inside :func:`visibility_scope` (one tool call) the decision is
        made once per store file, reader, audience and exclusion, and reused."""
        if not isinstance(actor, Actor) or not isinstance(audience, LiveSourceAudience):
            return {}
        if not audience.may_read_any(actor, "mail:"):
            return {}
        memo = _VISIBILITY.get()
        if memo is None:
            return self._decide_visible(actor, audience)
        # The objects themselves are held with the decision, so an id in the key is
        # never reused by another object while the scope lasts.
        key = (str(self.path), actor, id(audience), id(self.people), id(self.deny))
        if key not in memo:
            memo[key] = ((audience, self.people, self.deny),
                         self._decide_visible(actor, audience))
        return {mailbox: dict(row) for mailbox, row in memo[key][1].items()}

    def _decide_visible(self, actor: Actor, audience: LiveSourceAudience
                        ) -> dict[str, dict]:
        withholds = self._withholds()
        if withholds is None:
            return {}
        visible = {}
        for row in self.db.execute(_MAILBOX_ROWS):
            if withholds(row):
                continue
            resources = json.loads(row["resources"])
            acl = json.loads(row["source_acl"])
            if audience.visible_all(actor, resources, acl):
                visible[row["mailbox_id"]] = dict(row)
        return visible

    @staticmethod
    def _as_of(as_known_at: str | None) -> tuple[str, tuple]:
        if as_known_at is None:
            return "superseded_at IS NULL", ()
        moment = _normal_time(as_known_at)
        return ("recorded_at <= ? AND (superseded_at IS NULL OR superseded_at > ?)",
                (moment, moment))

    def messages(self, actor: Actor, audience: LiveSourceAudience, *,
                 as_known_at: str | None = None, include_body: bool = False,
                 internal_domains: Iterable[str] = (), limit: int = 100,
                 offset: int = 0) -> dict[str, Any]:
        """Captured message versions this reader may see (current, or as known at a
        time), newest received first. Every source text field is an untrusted-text
        envelope (tool output)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CaptureError("limit_invalid")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise CaptureError("offset_invalid")
        visible = self._visible(actor, audience)
        if not visible:
            return {"items": [], "total": 0, "lineage": LINEAGE}
        clause, params = self._as_of(as_known_at)
        marks = ",".join("?" * len(visible))
        where = f"mailbox_id IN ({marks}) AND {clause}"
        total = self.db.execute(f"SELECT COUNT(*) FROM messages WHERE {where}",
                                (*visible, *params)).fetchone()[0]
        rows = self.db.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY "
            "json_extract(record, '$.received_at') DESC, version_id LIMIT ? OFFSET ?",
            (*visible, *params, limit, offset)).fetchall()
        domains = frozenset(internal_domains)
        return {"items": [self._view(dict(row), include_body, domains) for row in rows],
                "total": total, "lineage": LINEAGE}

    def history(self, actor: Actor, audience: LiveSourceAudience, message_handle: str
                ) -> list[dict[str, Any]]:
        """Every validity interval of every captured version of the message a handle
        names, oldest first (``[]`` when the reader may not see it or it was never
        captured)."""
        row = self._message_row(actor, audience, message_handle)
        if row is None:
            return []
        rows = self.db.execute("SELECT l1_handle, change_key, recorded_at, superseded_at, "
                               "removed_reason, digest FROM messages WHERE mailbox_id=? AND "
                               "message_id=? ORDER BY recorded_at, version_id",
                               (row["mailbox_id"], row["message_id"])).fetchall()
        return [dict(r) for r in rows]

    def _message_row(self, actor: Actor, audience: LiveSourceAudience, handle: Any
                     ) -> dict[str, Any] | None:
        if not isinstance(handle, str):
            return None
        row = self._latest(handle)
        if row is None or row["mailbox_id"] not in self._visible(actor, audience):
            return None
        return dict(row)

    def resolve(self, handle: Any, actor: Actor, audience: LiveSourceAudience, *,
                text: bool = False, text_version: Any = None) -> dict[str, Any] | None:
        """The captured row an L1 handle names, offline, for a reader who may see its
        mailbox; ``None`` when not visible or never captured (indistinguishable).
        Shape for ``claim_checks`` rows: ``{"relation", "l1_handle", "lineage",
        "recorded_at", "superseded_at", "row": {...}}`` (field paths ``row.<name>``).
        An attachment citation may name one page (``&page=<n>``, the projection's
        citation of a PDF attachment's text, ``ontology.project_graph``); ``text=True``
        adds ``row.content_text``, the attachment's text as every reader cites it
        (``integrations.captured_text``: that page's only, and ``None`` for a page the
        text does not have), so a cited span is checkable. A member of a captured
        zip/.msg/.eml attachment (``&member=<path>&member_sha256=<hex>``) resolves for a
        reader who may see the attachment, as that member.
        Machine-only: values are raw (claim_checks compares them); anything shown
        to a model goes through ``messages`` (untrusted-text envelopes).

        An attachment's text can be read again (:meth:`fill_text`): ``text=True`` gives
        the newest reading, with ``row.text_version`` and ``row.text_status``; a citation
        made on an earlier reading passes its ``text_version`` (an int, 1 up) and
        resolves to that reading. A version the attachment does not have, or on a handle
        that is not an attachment's, resolves to None."""
        if not isinstance(handle, str):
            return None
        if text_version is not None and not (
                text and type(text_version) is int and text_version >= 1 and
                is_mail_attachment_citation(cited_page(handle)[0])):
            return None
        base, page = cited_page(handle)
        member = split_member(base)
        if member is not None:
            # A member of a captured zip/.msg/.eml attachment
            # (``integrations.captured_text``): visible with its attachment.
            container, path, digest = member
            view = self.resolve(container, actor, audience)
            if view is None or view["relation"] != "graph-mail.attachment":
                return None
            row = {"container": container, "path": path, "content_sha256": digest}
            if text or page is not None:
                found = member_view(self.blobs, view["row"], path, digest, page)
                if found is None:
                    return None
                row.update(found)
                if not text:
                    row.pop("content_text")
            return {**view, "relation": "graph-mail.containerMember", "l1_handle": handle,
                    "row": row}
        if is_mail_attachment_citation(base):
            found = self.db.execute("SELECT * FROM attachments WHERE l1_handle=?",
                                    (base,)).fetchone()
            if found is None:
                return None
            message = self._message_row(actor, audience, found["message_handle"])
            if message is None:
                return None
            row = {**json.loads(found["metadata"]), "content_sha256": found["content_sha256"]}
            if text:
                chain = self.text_chain(base)
                reading = (chain[-1] if text_version is None else next(
                    (v for v in chain if v["version"] == text_version), None)) \
                    if chain else None
                if text_version is not None and reading is None:
                    return None
                row["text_version"] = None if reading is None else reading["version"]
                row["text_status"] = found["text_status"] if reading is None \
                    else reading["text_status"]
                read = captured_text(self.blobs, {
                    **row, "text_status": row["text_status"],
                    "text_sha256": found["text_sha256"] if reading is None
                    else reading["text_sha256"]})
                row["content_text"] = (None if read is None else read.text if page is None
                                       else read.page(page))
                if page is not None and row["content_text"] is None:
                    return None
                if read is not None and read.skipped_by_cap is not None:
                    row["skipped_by_cap"] = read.skipped_by_cap   # the text's pages only
            if row.get("text_status", found["text_status"]) == OCR_DEFERRED:
                row["gaps"] = [ocr_deferred_gap()]
            return {"relation": "graph-mail.attachment", "l1_handle": handle,
                    "lineage": LINEAGE, "recorded_at": found["recorded_at"],
                    "superseded_at": message["superseded_at"],
                    "message_handle": found["message_handle"], "row": row}
        if page is not None:
            return None   # only an attachment's text is cited by page
        message = self._message_row(actor, audience, handle)
        if message is None:
            return None
        record = json.loads(message["record"])
        row = {key: value for key, value in record.items()
               if key not in ("citation", "source_acl")}
        row["body_text"] = self.body_text(message)
        return {"relation": "graph-mail.message", "l1_handle": handle, "lineage": LINEAGE,
                "recorded_at": message["recorded_at"],
                "superseded_at": message["superseded_at"],
                "removed_reason": message["removed_reason"], "digest": message["digest"],
                "row": row}

    def index(self, actor: Actor, audience: LiveSourceAudience, *, limit: int = 5000,
              mailbox_allowed: Callable[[Sequence[str]], bool] | None = None
              ) -> dict[str, Any]:
        """Current captured messages this reader may see, newest received first, as
        identifiers only (``mailbox``, ``message_id``, ``conversation_id``,
        ``l1_handle``): the mail index of readers that re-read each message for
        themselves (``operating.case_mail``).

        ``mailbox_allowed(identities)``: a further scope (the case mail scope) over
        the identity set recorded at capture; a mailbox outside it is skipped and
        counted. ``complete`` holds only when every visible in-scope mailbox's
        capture is complete (delta rounds, attachment lists) and the index was not
        cut at ``limit``. A withheld mailbox adds nothing to any count."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100_000:
            raise CaptureError("limit_invalid")
        visible = self._visible(actor, audience)
        inside: list[str] = []
        outside = 0
        for mailbox_id, row in visible.items():
            identities = [*json.loads(row["identities"]), row["upn"], mailbox_id]
            if mailbox_allowed is None or mailbox_allowed(identities):
                inside.append(mailbox_id)
            else:
                outside += 1
        result: dict[str, Any] = {"status": "read", "items": [], "truncated": False,
                                  "complete": False, "mailboxes_in_scope": len(inside),
                                  "mailboxes_outside_scope": outside}
        if not inside:
            return result
        marks = ",".join("?" * len(inside))
        rows = self.db.execute(
            f"SELECT l1_handle, mailbox_id, message_id, record FROM messages WHERE "
            f"mailbox_id IN ({marks}) AND superseded_at IS NULL ORDER BY "
            "json_extract(record, '$.received_at') DESC, version_id LIMIT ?",
            (*inside, limit + 1)).fetchall()
        for row in rows[:limit]:
            conversation = json.loads(row["record"]).get("conversation_id")
            result["items"].append({
                "mailbox": row["mailbox_id"], "message_id": row["message_id"],
                "l1_handle": row["l1_handle"],
                "conversation_id": conversation if isinstance(conversation, str) else None})
        result["truncated"] = len(rows) > limit
        boxes = {m["mailbox"]: m for m in self.completeness(actor, audience)["mailboxes"]}
        result["complete"] = (not result["truncated"]
                              and all(boxes.get(m, {}).get("complete") for m in inside))
        return result

    def received_at(self, actor: Actor, audience: LiveSourceAudience, moment: datetime
                    ) -> list[str]:
        """Handles of the current captured messages this reader may see that were
        received at exactly ``moment`` (to the second), oldest version first."""
        visible = self._visible(actor, audience)
        if not visible or not isinstance(moment, datetime) or moment.tzinfo is None:
            return []
        second = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        marks = ",".join("?" * len(visible))
        rows = self.db.execute(
            f"SELECT l1_handle FROM messages WHERE mailbox_id IN ({marks}) AND "
            "superseded_at IS NULL AND substr(json_extract(record, '$.received_at'), 1, 19)"
            " = ? ORDER BY version_id", (*visible, second)).fetchall()
        return [row["l1_handle"] for row in rows]

    def attachments(self, message_handle: Any, actor: Actor, audience: LiveSourceAudience
                    ) -> list[dict[str, Any]]:
        """The captured attachments of a message version this reader may see (``[]``
        otherwise): id, name, bytes, ``content_sha256``, ``content_status`` and the
        attachment handle (raw values, machine-only like :meth:`resolve`)."""
        if self._message_row(actor, audience, message_handle) is None:
            return []
        rows = self.db.execute("SELECT * FROM attachments WHERE message_handle=? ORDER BY "
                               "attachment_id", (message_handle,)).fetchall()
        out = []
        for row in rows:
            metadata = json.loads(row["metadata"])
            out.append({"attachment_id": row["attachment_id"], "name": metadata.get("name"),
                        "bytes": metadata.get("bytes"), "content_sha256": row["content_sha256"],
                        "content_status": row["content_status"], "l1_handle": row["l1_handle"]})
        return out

    def audience_check(self, audience: LiveSourceAudience) -> Callable[[Actor, str], bool]:
        """A ``ontology.store.ReadPolicy`` audience check for the ``mail`` prefix:
        ``check(actor, mailbox_id)`` is true only for a captured mailbox this reader
        may see now (identity set and owner ACL recorded at capture, the reader's
        audience), so projected mail objects are re-gated per reader."""
        return lambda actor, mailbox_id: mailbox_id in self._visible(actor, audience)

    def item_gate(self, audience: LiveSourceAudience) -> Callable[[Actor, str], bool]:
        """An ``operating.ontology_tools`` ``item_gate`` for projected mail: a
        ``graph-mail://`` message or attachment handle is readable only when it
        resolves offline for this reader (mailbox audience and owner ACL)."""
        return lambda actor, handle: self.resolve(handle, actor, audience) is not None

    def rows(self, actor: Actor, audience: LiveSourceAudience) -> Callable[[str], Any]:
        """A ``claim_checks`` ``Rows`` callable bound to one reader."""
        return lambda handle: self.resolve(handle, actor, audience, text=True)

    def _pinned_attachment(self, handle: Any, actor: Actor, audience: LiveSourceAudience
                           ) -> dict[str, Any] | None:
        """The resolved attachment a byte-pinned handle names for this reader, else None."""
        resolved = self.resolve(handle, actor, audience)
        if resolved is None or resolved["relation"] != "graph-mail.attachment":
            return None
        digest = resolved["row"].get("content_sha256")
        if not digest or not handle.endswith("sha256=" + digest):
            return None
        return resolved

    def attachment_content(self, handle: Any, actor: Actor, audience: LiveSourceAudience
                           ) -> bytes | None:
        """Captured attachment bytes (re-hashed against the handle), or ``None`` (also
        when the store dropped the original: :meth:`attachment_reading` reads its text,
        :meth:`refetch_original` its bytes from the source)."""
        resolved = self._pinned_attachment(handle, actor, audience)
        if resolved is None:
            return None
        digest = resolved["row"]["content_sha256"]
        if self.original_dropped(digest):
            return None
        return self.blob(digest)

    def attachment_reading(self, handle: Any, actor: Actor, audience: LiveSourceAudience
                           ) -> Any:
        """The reading (``captured_text.CapturedText``) of a byte-pinned attachment every
        reader cites, from its bytes or, when the original was dropped, from the readings
        it left; None when not visible or unreadable."""
        resolved = self._pinned_attachment(handle, actor, audience)
        if resolved is None:
            return None
        found = self.db.execute("SELECT metadata, text_status, text_sha256 FROM attachments "
                                "WHERE l1_handle=?", (cited_page(handle)[0],)).fetchone()
        if found is None:
            return None
        return captured_text(self.blobs, {
            **json.loads(found["metadata"]), "content_sha256": resolved["row"]["content_sha256"],
            "text_status": found["text_status"], "text_sha256": found["text_sha256"]})

    def attachment_hashes(self, handle: Any, actor: Actor, audience: LiveSourceAudience
                          ) -> dict[str, Any] | None:
        """``sha256``, ``bytes`` and ``quick_xor`` (OneDrive's quickXorHash) of a
        byte-pinned attachment's original, from its bytes or the record its dropped
        original left; None when not visible or neither verifies."""
        from .graph_read import quick_xor_hash
        resolved = self._pinned_attachment(handle, actor, audience)
        if resolved is None:
            return None
        digest = resolved["row"]["content_sha256"]
        if self.original_dropped(digest):
            kept = read_dropped(self.blobs, digest)
            return None if kept is None else {"sha256": digest, "bytes": kept["bytes"],
                                              "quick_xor": kept["quick_xor"]}
        try:
            content = self.blob(digest)
        except (CaptureError, OSError):
            return None
        return {"sha256": digest, "bytes": len(content), "quick_xor": quick_xor_hash(content)}

    def original_source(self, handle: Any, actor: Actor, audience: LiveSourceAudience
                        ) -> dict[str, Any] | None:
        """The re-fetch handle of a byte-pinned attachment for this reader: its mailbox,
        message and attachment ids, digest, size and whether the store keeps its bytes."""
        resolved = self._pinned_attachment(handle, actor, audience)
        if resolved is None:
            return None
        found = self.db.execute(
            "SELECT a.mailbox_id, a.attachment_id, m.message_id FROM attachments a JOIN "
            "messages m ON m.l1_handle=a.message_handle WHERE a.l1_handle=? LIMIT 1",
            (cited_page(handle)[0],)).fetchone()
        if found is None:
            return None
        digest = resolved["row"]["content_sha256"]
        row = self.db.execute("SELECT bytes FROM blobs WHERE sha256=?", (digest,)).fetchone()
        return {"mailbox_id": found["mailbox_id"], "message_id": found["message_id"],
                "attachment_id": found["attachment_id"], "content_sha256": digest,
                "bytes": None if row is None else row["bytes"],
                "stored": not self.original_dropped(digest)}

    def refetch_original(self, handle: Any, actor: Actor, audience: LiveSourceAudience,
                         reader: Any, reader_actor: Actor) -> bytes | None:
        """A dropped (or kept) original's bytes for this reader, read again from Graph by
        one GET through ``reader`` (``GraphMailReader.get_attachment``, as
        ``reader_actor``) and returned only when they hash to the handle's pin; never
        stored. None when not visible, gone at the source or changed there."""
        source = self.original_source(handle, actor, audience)
        if source is None:
            return None
        if source["stored"]:
            return self.attachment_content(handle, actor, audience)
        try:
            read = reader.get_attachment(reader_actor, source["mailbox_id"],
                                         source["message_id"], source["attachment_id"],
                                         max_bytes=MAX_ATTACHMENT_BYTES)
        except (NotVisible, SourceUnavailable):
            return None
        content = read.get("content")
        if not isinstance(content, bytes) or \
                sha256(content).hexdigest() != source["content_sha256"]:
            return None
        return content

    def attachment_verified(self, handle: Any, actor: Actor, audience: LiveSourceAudience
                            ) -> bool:
        """Whether a byte-pinned attachment's original verifies: its bytes hash to the
        pin, or the store dropped it and its ``.dropped`` record is whole."""
        resolved = self._pinned_attachment(handle, actor, audience)
        if resolved is None:
            return False
        digest = resolved["row"]["content_sha256"]
        if self.original_dropped(digest):
            return read_dropped(self.blobs, digest) is not None
        try:
            self.blob(digest)
        except (CaptureError, OSError):
            return False
        return True

    def _mailbox_bytes(self, mailbox_id: str) -> int:
        records = self.db.execute("SELECT COALESCE(SUM(length(record)), 0) FROM messages "
                                  "WHERE mailbox_id=?", (mailbox_id,)).fetchone()[0]
        blobs = self.db.execute(
            "SELECT COALESCE(SUM(COALESCE(stored_bytes, bytes)), 0) FROM blobs WHERE sha256 IN ("
            "SELECT body_sha256 FROM messages WHERE mailbox_id=? UNION "
            "SELECT content_sha256 FROM attachments WHERE mailbox_id=?)",
            (mailbox_id, mailbox_id)).fetchone()[0]
        return records + blobs

    def completeness(self, actor: Actor, audience: LiveSourceAudience) -> dict[str, Any]:
        """Per visible mailbox and folder: round status, window, capture times,
        degraded versions and stored bytes; plus the store bound. A withheld mailbox
        has no entry and adds nothing to any count."""
        visible = self._visible(actor, audience)
        mailboxes = []
        for mailbox_id in visible:
            folders = []
            for row in self.db.execute("SELECT * FROM folders WHERE mailbox_id=? "
                                       "ORDER BY folder_key", (mailbox_id,)):
                folders.append({
                    "folder": row["folder_key"], "complete": row["status"] == "complete",
                    "status": row["status"], "reason": row["reason"],
                    "window_received_since": row["received_since"],
                    "last_complete_at": row["last_complete_at"],
                    "last_run_at": row["last_run_at"],
                    "rounds_complete": row["rounds_complete"]})
            counts = self.db.execute(
                "SELECT COUNT(*) AS versions, SUM(superseded_at IS NULL) AS current, "
                "SUM(superseded_at IS NULL AND body_status LIKE ?) AS degraded "
                "FROM messages WHERE mailbox_id=?", (_DEGRADED_BODY, mailbox_id)).fetchone()
            attachments = self.db.execute(
                "SELECT COUNT(*) FROM attachments a WHERE mailbox_id=? AND "
                "content_status='unavailable' AND EXISTS (SELECT 1 FROM messages m WHERE "
                "m.l1_handle=a.message_handle AND m.superseded_at IS NULL)",
                (mailbox_id,)).fetchone()[0]
            truncated = self.truncated_lists(mailbox_id)
            mailboxes.append({"mailbox": mailbox_id, "folders": folders,
                              "mismatched_attachments": self.mismatched(mailbox_id),
                              "complete": (bool(folders) and all(f["complete"] for f in folders)
                                           and truncated == 0),
                              "versions": counts["versions"],
                              "current_messages": counts["current"] or 0,
                              "degraded_bodies": counts["degraded"] or 0,
                              "degraded_attachments": attachments,
                              "deferred_attachments": self.deferred_attachments(mailbox_id),
                              "ocr_deferred_attachments": self.ocr_deferred_count(mailbox_id),
                              "truncated_attachment_lists": truncated,
                              "stored_bytes": self._mailbox_bytes(mailbox_id)})
        bound = [f for m in mailboxes for f in m["folders"]]
        result: dict[str, Any] = {
            "lineage": LINEAGE, "mailboxes": mailboxes,
            "complete": bool(mailboxes) and all(m["complete"] for m in mailboxes),
            "complete_bound": sum(1 for f in bound if f["complete"]),
            "bound_total": len(bound)}
        if mailboxes:
            result["store_bound"] = {"max_bytes": self.max_store_bytes,
                                     "full": self.store_full()}
        return result

    def mailbox_coverage(self, mailbox: str, actor: Actor, audience: LiveSourceAudience
                         ) -> dict[str, Any]:
        """What this capture holds of one mailbox (the object id a ``graph-mail://``
        handle names), for one reader: ``source`` (``mail:<mailbox>``, the source that
        would hold its messages), ``status`` -- ``captured`` (every folder complete),
        ``incomplete`` (``reasons``: the folders' recorded reasons, e.g. ``forbidden``)
        or ``not_captured`` -- never captured (excluded, not yet reached, not
        openable) or withheld from this reader; the two read the same."""
        key = str(mailbox).lower()
        out: dict[str, Any] = {"source": f"mail:{key}", "status": "not_captured",
                               "reasons": []}
        if key not in self._visible(actor, audience):
            return out
        rows = self.db.execute("SELECT status, reason FROM folders WHERE mailbox_id=?",
                               (key,)).fetchall()
        reasons = sorted({r["reason"] or r["status"] for r in rows if r["status"] != "complete"})
        if self.truncated_lists(key):
            reasons = sorted({*reasons, "attachment_lists_truncated"})
        out.update(status="incomplete" if reasons or not rows else "captured",
                   reasons=reasons)
        return out

    def counts(self, actor: Actor, audience: LiveSourceAudience) -> dict[str, int]:
        visible = list(self._visible(actor, audience))
        if not visible:
            return {"mailboxes": 0, "messages_current": 0, "versions": 0,
                    "attachments_captured": 0}
        marks = ",".join("?" * len(visible))
        row = self.db.execute(
            f"SELECT COUNT(*) AS versions, SUM(superseded_at IS NULL) AS current FROM messages "
            f"WHERE mailbox_id IN ({marks})", visible).fetchone()
        attachments = self.db.execute(
            f"SELECT COUNT(*) FROM attachments WHERE mailbox_id IN ({marks}) "
            "AND content_status='captured'", visible).fetchone()[0]
        return {"mailboxes": len(visible), "messages_current": row["current"] or 0,
                "versions": row["versions"], "attachments_captured": attachments}

    def owner_marking(self, actor: Actor, audience: LiveSourceAudience) -> dict[str, int]:
        """Current message versions this reader may see, split by whether each one
        carries the access marking of its owning mailbox: the record's ``source_acl``
        is a derived ``mailbox_owner`` ACL naming the row's mailbox, and the mailbox
        row (what ``_visible`` re-gates every reader on) carries the same owner ACL.
        Counts only; a withheld mailbox adds nothing."""
        visible = self._visible(actor, audience)
        marked = unmarked = 0
        if not visible:
            return {"with": 0, "without": 0}

        def owner(acl: Any, mailbox_id: str) -> bool:
            return (isinstance(acl, Mapping) and acl.get("kind") == "mailbox_owner"
                    and acl.get("status") == "derived"
                    and isinstance(acl.get("identities"), list)
                    and mailbox_id in acl["identities"])

        marks = ",".join("?" * len(visible))
        for row in self.db.execute(
                f"SELECT mailbox_id, record FROM messages WHERE superseded_at IS NULL "
                f"AND mailbox_id IN ({marks})", list(visible)):
            mailbox_id = row["mailbox_id"]
            record = json.loads(row["record"])
            if (owner(record.get("source_acl"), mailbox_id)
                    and record.get("mailbox") == mailbox_id
                    and owner(json.loads(visible[mailbox_id]["source_acl"]), mailbox_id)):
                marked += 1
            else:
                unmarked += 1
        return {"with": marked, "without": unmarked}

    def _view(self, row: Mapping[str, Any], include_body: bool,
              internal_domains: frozenset[str]) -> dict[str, Any]:
        record = json.loads(row["record"])
        handle = row["l1_handle"]
        sender = record.get("from")
        # Internal only with DMARC pass for the internal domain (the one sender rule).
        sender_class = mentions_sender_class(sender, internal_domains,
                                             record.get("authentication_results"))

        def envelope(text: Any, field: str, citation: str = handle) -> dict[str, Any] | None:
            if not isinstance(text, str):
                return None
            result = {"untrusted_text": text[:ENVELOPE_CHARS],
                      "origin": {"citation": citation, "field_path": f"row.{field}",
                                 "sender_class": sender_class}}
            if len(text) > ENVELOPE_CHARS:
                result["truncated"] = True
            return result

        def envelopes(values: Any, field: str) -> list[dict[str, Any]]:
            return [envelope(v, field) for v in values or [] if isinstance(v, str)]

        attachments = []
        for item in self.db.execute("SELECT * FROM attachments WHERE message_handle=? "
                                    "ORDER BY attachment_id", (handle,)):
            metadata = json.loads(item["metadata"])
            origin = item["l1_handle"] or handle
            attachments.append({
                "l1_handle": item["l1_handle"], "attachment_id": item["attachment_id"],
                "name": envelope(metadata.get("name"), "name", origin),
                "content_type": envelope(metadata.get("content_type"), "content_type", origin),
                "size": metadata.get("size"), "bytes": metadata.get("bytes"),
                "is_inline": metadata.get("is_inline"),
                "content_sha256": item["content_sha256"],
                "content_status": item["content_status"]})
        view = {
            "l1_handle": handle, "lineage": LINEAGE, "mailbox": row["mailbox_id"],
            "message_id": row["message_id"], "change_key": row["change_key"],
            "folder_id": row["folder_id"], "recorded_at": row["recorded_at"],
            "superseded_at": row["superseded_at"], "removed_reason": row["removed_reason"],
            "digest": row["digest"], "received_at": record.get("received_at"),
            "sent_at": record.get("sent_at"), "last_modified": record.get("last_modified"),
            "from": envelope(sender, "from"), "to": envelopes(record.get("to"), "to"),
            "cc": envelopes(record.get("cc"), "cc"),
            "conversation_id": record.get("conversation_id"),
            "internet_message_id": envelope(record.get("internet_message_id"),
                                            "internet_message_id"),
            "has_attachments": record.get("has_attachments"),
            "original_url": envelope(record.get("original_url"), "original_url"),
            "sender_class": sender_class,
            "subject": envelope(record.get("subject"), "subject"),
            "body_preview": envelope(record.get("body_preview"), "body_preview"),
            "body_status": row["body_status"], "body_sha256": row["body_sha256"],
            "attachments": attachments}
        if include_body:
            view["body"] = envelope(self.body_text(row), "body_text")
        return view


# ------------------------------------------------------------------ capture

# The capture scheduler's terminal reasons: a unit refused by the source (HTTP 403, e.g.
# an Exchange application access policy) or not visible to the capture actor stays
# incomplete with that reason until a later run reads it; it is re-probed after every
# other unit, never treated as work the budget left.
TERMINAL_REASONS = frozenset({"forbidden", "not_visible", "source_error:http_403"})


def terminal_reason(reason: str | None) -> bool:
    """Whether a unit's (comma-joined) reason is terminal: every part of it is."""
    parts = set(filter(None, str(reason or "").split(",")))
    return bool(parts) and parts <= TERMINAL_REASONS


# A unit's standing for the scheduler (schedule_units), in the order a run takes them.
FRONTIER, PENDING, COMPLETE, TERMINAL = range(4)


def schedule_units(units: Sequence[Any], standing: Callable[[Any], tuple[int, str]]
                   ) -> list[Any]:
    """The order one budget-bound run captures its units in (mail folders, SharePoint
    roots): one rule for both.
    ``standing(unit)`` is ``(kind, since)``:

    * ``FRONTIER``: a unit with a stored frontier (a cut round or walk), the one started
      earliest first (``since``: when its round or walk started), so a unit larger than
      one budget resumes every run and completes;
    * ``PENDING``: other incomplete units, never started ones among them, in the declared
      order;
    * ``COMPLETE``: complete units, each reading only its delta, the oldest position first
      (``since``: when it was last brought current; empty: unknown, the oldest);
    * ``TERMINAL``: units with a terminal reason (:func:`terminal_reason`).

    Ties keep the declared order."""
    def key(numbered: tuple[int, Any]) -> tuple[int, str, int]:
        kind, since = standing(numbered[1])
        return kind, since if kind in (FRONTIER, COMPLETE) else "", numbered[0]
    return [unit for _n, unit in sorted(enumerate(units), key=key)]


def folder_standing(state: Mapping[str, Any] | None) -> tuple[int, str]:
    """A mail folder's :func:`schedule_units` standing from its stored row."""
    if state is None:
        return PENDING, ""
    if state["status"] == "complete":
        return COMPLETE, state["last_run_at"] or ""
    if terminal_reason(state["reason"]):
        return TERMINAL, ""
    if state["next_url"]:
        return FRONTIER, state["round_started_at"] or ""
    return PENDING, ""


# The cut a kept store resumes is a later_ask while the store is not kept between runs.
PERSISTENT_STATE_ASK = "live-host-persistent-state-dir"


def budget_cut_block(units: Iterable[tuple[str, Iterable[str]]], *, carried: bool,
                     source: str = "microsoft-graph",
                     cuts: frozenset[str] = frozenset({TIME_BUDGET})) -> list[dict[str, Any]]:
    """The live verifier's ``blocked_upstream`` parts of a budget-bound capture's last
    run, one rule for every capture check that resumes across runs (mail folders, the
    SharePoint estate's roots, the QCQA census; decision
    2026-10-21-sharepoint-census-across-runs). ``units``: ``(part, reasons)`` of each
    unit the run left incomplete (comma-joined reasons split), ``carried``: the store
    existed before this invocation (``store_exists``, observed, never inferred from
    where it lies).

    Blocked (cause ``later_ask`` live-host-persistent-state-dir) only when the store
    was not carried and every incomplete unit was cut by the time budget alone: the
    run did all it could in one budget over a store nobody kept, and only a host that
    keeps it lets the next run resume. A carried store resumes by itself (the check
    fails until it has), and any unit incomplete for another reason (an error, a
    refusal, a scope bound, an unknown or empty reason) is a typed fail: no parts.
    Nothing incomplete: no parts."""
    found: dict[str, dict[str, Any]] = {}
    for part, reasons in units:
        named = {x for r in reasons for x in str(r or "").split(",") if x}
        if not named or not named <= cuts:
            return []
        found.setdefault(str(part), {"part": str(part), "source": source,
                                     "cause": "later_ask", "later_ask": PERSISTENT_STATE_ASK})
    return [] if carried else list(found.values())


def mail_cut_units(run: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    """The ``(part, reasons)`` of every unit a :meth:`MailCapture.run` left incomplete,
    for :func:`budget_cut_block`: a folder by mailbox and folder ordinal (a complete one
    the budget deferred counts as cut: it was not brought current), a mailbox that was
    not opened once per scope folder with its status, an incomplete directory listing,
    a people guard that refused the run."""
    if run.get("people_blocked"):
        return [("mail.people", ["people_blocked"])]
    units: list[tuple[str, list[str]]] = []
    if run.get("directory") not in (None, "complete"):
        units.append(("mail.directory", [f"directory_{run['directory']}"]))
    for entry in run.get("mailboxes") or []:
        index = entry["index"]
        if entry.get("folders"):
            for j, folder in enumerate(entry["folders"]):
                if folder["complete"] and folder.get("deferred"):
                    units.append((f"mail.mailbox{index}.folder{j}", [TIME_BUDGET]))
                elif not folder["complete"]:
                    units.append((f"mail.mailbox{index}.folder{j}",
                                  [str(folder.get("reason") or "")]))
        elif entry.get("status") != "complete":
            units += [(f"mail.mailbox{index}.folder{j}", [str(entry.get("status") or "")])
                      for j in range(max(int(entry.get("folders_bound") or 0), 1))]
    if not run.get("complete") and not units:
        units.append(("mail.run", ["incomplete"]))   # incomplete for no unit: never a cut
    return units


def folder_error_reason(exc: BaseException) -> str:
    """The typed reason of a folder that raised: ``folder_error:<cause>``, the cause a
    ``CaptureError``'s fixed code or the exception class name (a short identifier),
    never the message."""
    cause = exc.code if isinstance(exc, CaptureError) else type(exc).__name__
    cause = re.sub(r"[^A-Za-z0-9_]", "_", str(cause))[:40] or "unclassified"
    return f"folder_error:{cause}"


def _transient(detail: str | None) -> bool:
    if not detail:
        return False
    return detail.removeprefix("retried.") in _TRANSIENT


def _failure(exc: SourceUnavailable) -> str:
    """The recorded reason of a mailbox or folder the source refused or failed:
    ``forbidden`` for HTTP 403 (e.g. outside the Exchange application access
    policy), else ``source_error:<detail>``."""
    detail = unavailable_detail(exc) or "unclassified"
    return "forbidden" if detail.removeprefix("retried.") == "http_403" \
        else f"source_error:{detail}"


def graph_block(part: str, op: str, permissions: Mapping[str, str],
                status: Any) -> dict[str, Any] | None:
    """The ``blocked_upstream`` part of a Graph read ``op`` that answered ``status``,
    or None when the one upstream rule (``plx_hub_guard.upstream_blocks``, source
    ``graph``) does not block it. The one shape of a Graph refusal in the mail, Teams
    and SharePoint list captures: ``error_code`` from ``GRAPH_BLOCKS``
    (``missing_permission`` on 403, ``unauthorized`` on 401), on 403 the application
    permission ``op`` needs (``permissions[op]``), cause ``host_misconfiguration``."""
    if not upstream_blocks("http", None, status, None, "graph"):
        return None
    return {"part": part, "source": "microsoft-graph", "http_status": status,
            "error_code": GRAPH_BLOCKS[status], "cause": "host_misconfiguration",
            **({"permission": permissions[op]} if status == 403 else {})}


def blocked_reason(part: Mapping[str, Any]) -> str:
    """A blocked read's reason: ``blocked:<error_code>[:<permission>]``."""
    return ":".join(["blocked", part["error_code"],
                     *([part["permission"]] if "permission" in part else [])])


# The Graph application permission each mail read needs, named when Graph refuses it
# (403). A mailbox is opened by its directory lookup (``graph.user``); a folder is read
# through its mail endpoints (``graph.mail_folder``, then the message delta under the
# same Mail.Read grant). A folder 403 with Mail.Read granted is an Exchange application
# access policy that leaves the mailbox out ("Graph refusals").
_PERMISSION = {"graph.user": "User.Read.All", "graph.mail_folder": "Mail.Read"}
# The part and the Graph call each refused unit stands for.
REFUSED_PARTS = {"mailbox": ("mail.mailbox_lookup", "graph.user"),
                 "folder": ("mail.folder_read", "graph.mail_folder")}


def _blocked(kind: str, exc: BaseException) -> dict[str, Any] | None:
    """The ``graph_block`` part of a mailbox or folder read that failed with ``exc``
    (its HTTP status: a mail read's or the token request's), None when not blocked."""
    part, op = REFUSED_PARTS[kind]
    return graph_block(part, op, _PERMISSION, getattr(exc, "http_status", None))


def refused_parts(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The live verifier's ``blocked_upstream`` parts of a capture run the source
    refused outright: when every mailbox the run bound (the allowlist's refusals aside)
    is refused under the one upstream rule (``graph_block``: HTTP 403 or 401), on its
    directory lookup or on every one of its folders, the run read nothing because the
    host's grant or credential is missing, not because the product failed. One part
    per refused call and status (``mail.mailbox_lookup`` needs User.Read.All,
    ``mail.folder_read`` Mail.Read): the unit's ``blocked`` part, or for a folder the
    budget deferred with its stored ``forbidden`` reason, that 403. A run where any
    bound mailbox or folder was not refused (read, throttled, not visible, budget-cut)
    has no parts: a partial refusal stays a capture gap in ``reasons`` ("Graph refusals")."""
    if run.get("people_blocked"):
        return [run["people_blocked"]]
    bound = [m for m in run.get("mailboxes") or [] if m.get("status") != "outside_allowlist"]
    units: list[tuple[str, Mapping[str, Any]]] = []
    for entry in bound:
        if entry.get("folders"):
            units += [("folder", f) for f in entry["folders"]]
        else:
            units.append(("mailbox", entry))
    parts = []
    for kind, unit in units:
        part, op = REFUSED_PARTS[kind]
        found = unit.get("blocked") or (
            graph_block(part, op, _PERMISSION, 403)
            if kind == "folder" and unit.get("reason") == "forbidden" else None)
        if found is None:
            return []
        parts.append(found)
    unique = {json.dumps(p, sort_keys=True): p for p in parts}.values()
    return sorted(unique, key=lambda p: (p["part"], p["http_status"]))


# Reader threads per capture at most (``MailCapture`` workers / ocr_workers).
MAX_WORKERS = 16
# A folder whose turn read ``pages_per_turn`` pages: the run comes back to it (run()).
TURN = "turn"
# The mailbox folder listing (``folders`` all) is read again after this long.
FOLDER_RELIST_SECONDS = 6 * 3600
# The OCR backlog a run queues at its start (``_OcrQueue``), newest versions first.
OCR_BACKLOG_PER_RUN = 2000
# What the host and the CLI capture with (``MailCapture``): four concurrent reads per
# mailbox (Exchange's per-mailbox limit), two pages per folder per turn, OCR beside the
# capture on up to four threads (each runs one local OCR process at a time).
PRODUCTION: dict[str, Any] = {"workers": 4, "pages_per_turn": 2, "defer_ocr": True,
                              "ocr_workers": max(1, min(4, (os.cpu_count() or 2) // 2)),
                              "page_size": 100}


class _OcrQueue:
    """The deferred OCR of captured attachments (``ocr_pending`` and the other unread
    statuses), read from their stored bytes by ``workers`` threads beside the capture.
    A thread reads only the blob file and runs the extractor; the capture thread alone
    writes each reading (``MailCaptureStore.fill_text``: a new text version) when it
    drains the queue. A reading not started once ``spent()`` is dropped and stays
    pending for the next run; ``close`` cancels the ones not started and waits for the
    running ones."""

    def __init__(self, store: MailCaptureStore, workers: int, spent: Callable[[], bool]
                 ) -> None:
        self.store, self._spent = store, spent
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="mail-ocr")
        self._pending: dict[str, tuple[Future, str]] = {}
        self.counts = {"queued": 0, "read": 0, "texts_ocr": 0, "texts_ocr_unavailable": 0,
                       "left_pending": 0, "failed": 0, "originals_dropped": 0}

    def submit(self, l1_handle: str, digest: str, metadata: Mapping[str, Any]) -> None:
        if l1_handle in self._pending or not isinstance(digest, str):
            return
        self._pending[l1_handle] = (self._pool.submit(self._read, digest, dict(metadata)),
                                    digest)
        self.counts["queued"] += 1

    def _read(self, digest: str, metadata: Mapping[str, Any]) -> tuple[str, str | None] | None:
        if self._spent():
            return None
        try:
            content = self.store.blob(digest)   # the blob file only: no database access
        except (CaptureError, OSError):
            return None   # bytes missing or altered: left as they are, never re-read
        return capture_text(content, metadata)

    def drain(self, *, wait: bool = False) -> int:
        """Store the readings done (each a new text version), then drop each original
        nothing needs any more (``settle_original``); returns the readings stored."""
        stored = 0
        for handle, (future, digest) in list(self._pending.items()):
            if not (wait or future.done()):
                continue
            del self._pending[handle]
            if future.cancelled():
                self.counts["left_pending"] += 1
                continue
            try:
                found = future.result()
            except Exception:   # noqa: BLE001 - an extractor defect leaves the text as it was
                self.counts["failed"] += 1
                continue
            if found is None:
                self.counts["left_pending"] += 1
                continue
            status, text = found
            self.store.fill_text(handle, status, self.store.put_text(text))
            stored += 1
            self.counts["read"] += 1
            self.counts["texts_ocr"] += status == TEXT_OCR
            self.counts["texts_ocr_unavailable"] += status == OCR_UNAVAILABLE
            self.counts["originals_dropped"] += _settle(self.store, digest) == "dropped"
        return stored

    def close(self) -> dict[str, int]:
        for future, _digest in self._pending.values():
            future.cancel()
        self._pool.shutdown(wait=True)
        self.drain(wait=True)
        return dict(self.counts)


def ocr_deferred_gap() -> dict[str, Any]:
    """The named gap of an attachment whose scanned pages were not OCR-read yet
    (``ocr_deferred``): what its text does not state is unread, never absent or empty."""
    return {"kind": "not_ingested", "reason": OCR_DEFERRED,
            "detail": "scanned pages of this attachment were not OCR-read yet (OCR "
                      "deferred, its bytes re-read from the source later); only its text "
                      "layer is read, so what it does not state is unknown, not absent"}


def ocr_deferred_pass(store: MailCaptureStore, reader: Any, actor: Actor,
                      audience: LiveSourceAudience, *, budget_seconds: float,
                      monotonic: Callable[[], float] = time.monotonic,
                      limit: int = OCR_BACKLOG_PER_RUN) -> dict[str, int]:
    """OCR the attachments whose OCR was deferred (``ocr_deferred``), newest first,
    within ``budget_seconds``: each original re-fetched by one Graph GET
    (``MailCaptureStore.refetch_original``: visible to ``actor``, re-hashed against its
    pin, never stored as such), OCR-read here (``capture_text``), the reading stored as a
    new text version (``fill_text``), its readings added to the ``.dropped`` form and the
    bytes dropped again (``settle_original``). An original gone or changed at the source
    stays ``ocr_deferred`` (``refetch_failed``); a reading that still needs OCR (no
    engine) is not stored (``left_deferred``). Counts only."""
    counts = {"selected": 0, "read": 0, "refetch_failed": 0, "left_deferred": 0,
              "texts_ocr": 0, "originals_dropped": 0, "budget_spent": 0}
    deadline = monotonic() + budget_seconds
    for row in store.ocr_deferred_rows(None, limit):
        if monotonic() >= deadline:
            counts["budget_spent"] = 1
            break
        counts["selected"] += 1
        content = store.refetch_original(row["l1_handle"], actor, audience, reader, actor)
        if content is None:
            counts["refetch_failed"] += 1
            continue
        status, text = capture_text(content, json.loads(row["metadata"]))
        if status in TEXT_UNREAD or status == OCR_DEFERRED:
            counts["left_deferred"] += 1
            continue
        store.fill_text(row["l1_handle"], status, store.put_text(text))
        counts["read"] += 1
        counts["texts_ocr"] += status == TEXT_OCR
        counts["originals_dropped"] += _settle(store, row["content_sha256"],
                                               content=content) == "dropped"
    counts["left"] = store.ocr_deferred_count()
    return counts


def _settle(store: MailCaptureStore, digest: str, **given: Any) -> str:
    """``settle_original``; a file that cannot be written keeps the original
    (``kept_unwritable``), never stops the capture."""
    try:
        return store.settle_original(digest, **given)
    except OSError:
        return "kept_unwritable"


def _readings(content: bytes, attachment: Mapping[str, Any], status: str, text: str | None,
              *, defer_ocr: bool = False) -> dict[str, Any] | None:
    """Reader side: the readings of new attachment bytes the store keeps when it drops
    the original (``settle_original``), made here so the store thread does not: none
    for bytes it keeps anyway (small, awaiting a reading, a container)."""
    from operating.use_case_parts import is_container

    mime, name = file_type(attachment)
    if len(content) <= DROP_ORIGINALS_ABOVE or (
            MailCaptureStore._awaits_reading(status, attachment)
            and not (defer_ocr and status in TEXT_UNREAD)) or is_container(content, name, mime):
        return None
    keys = [(mime, name, None)]
    pinned = None if text is None else sha256(text.encode("utf-8")).hexdigest()
    if status == TEXT_OCR and pinned is not None:
        keys.append((mime, name, pinned))
    return readings_of(content, keys, lambda digest: text if digest == pinned else None)


def _ocr_engine() -> bool:
    from .document_understanding import local_ocr_available
    return local_ocr_available()


def _new_counts() -> dict[str, int]:
    return {key: 0 for key in (
        "pages", "messages_seen", "versions_new", "versions_superseded", "unchanged",
        "reappeared", "removed", "absent_after_resync", "vanished", "bodies_captured",
        "bodies_unavailable", "bodies_recovered", "attachments_captured",
        "attachments_reused", "attachments_recovered", "attachment_bytes",
        "attachments_metadata_only", "attachments_deferred", "originals_dropped",
        "attachment_lists_incomplete",
        "attachment_lists_recovered", "attachment_lists_capped", "attachments_mismatched",
        "texts_ocr", "texts_ocr_unavailable", "texts_reextracted",
        "provenance_dropped", "people_dropped", "throttle_waits", "round_restarts",
        "backfill_pages")}


def _count_text(status: str, counts: dict[str, int]) -> None:
    counts["texts_ocr"] += status == TEXT_OCR
    counts["texts_ocr_unavailable"] += status == OCR_UNAVAILABLE


class MailCapture:
    """Runs capture rounds for one scope with one capture actor (see module docstring).

    ``budget_seconds`` bounds the wall time of every run of this object together (0:
    spent from the start): once spent, no further delta page is read; the folder it cut
    ends ``incomplete`` with reason ``time_budget`` and resumes from its stored cursor, so
    a large scope stops in a reported partial state instead of running into a caller's
    timeout. Every folder after it is ``deferred`` (nothing read): incomplete with reason
    ``time_budget``, or still complete (its next run reads only its delta), or still its
    terminal reason. A run captures its folders in the order of :func:`schedule_units`,
    so a budget-bound scope completes over several runs; a mailbox or run is complete only when no folder was deferred.

    Throughput (:data:`PRODUCTION`, what the host and CLI use; the defaults read one
    message at a time):

    * ``pages_per_turn``: a folder reads at most this many pages per turn, then the run
      goes on to the next folder and comes back in a later turn (its cursor stored), so
      one large mailbox never takes a whole budget while the others wait: every folder's
      newest pages arrive in the first turns.
    * ``workers``: the messages of a page are read concurrently (body, attachment
      listing and bytes, text) by that many reader threads, at most Exchange's four
      concurrent requests per mailbox; this thread alone writes the store, in page order.
      Graph throttling (429 ``Retry-After``) holds every thread (``BoundedBackoff``).
    * ``defer_ocr`` and ``ocr_workers``: a scanned PDF is stored with its text layer as
      ``ocr_pending`` and OCR-read from its stored bytes by a pool beside the capture
      (``ocr_workers`` threads), so a 60 s scan never holds the mail behind it. Readings
      not finished when the budget ends stay pending; the next run reads them first."""

    def __init__(self, store: MailCaptureStore, reader: Any, actor: Actor, *,
                 page_size: int = 50, throttle_waits: Sequence[float] = DEFAULT_THROTTLE_WAITS,
                 sleep: Callable[[float], None] = time.sleep,
                 page_budget: int | None = None,
                 max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
                 attachment_list_cap: int = MAX_ATTACHMENTS_LISTED,
                 budget_seconds: float | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 workers: int = 1, pages_per_turn: int | None = None,
                 defer_ocr: bool = False, ocr_workers: int = 0,
                 max_pending_ocr_bytes: int | None = MAX_PENDING_OCR_BYTES) -> None:
        if page_budget is not None and (isinstance(page_budget, bool)
                                        or not isinstance(page_budget, int) or page_budget < 1):
            raise CaptureError("page_budget_invalid")
        if budget_seconds is not None and (isinstance(budget_seconds, bool) or not isinstance(
                budget_seconds, (int, float)) or not budget_seconds >= 0):
            raise CaptureError("budget_seconds_invalid")
        for value, low, code in ((workers, 1, "workers_invalid"),
                                 (ocr_workers, 0, "ocr_workers_invalid")):
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not low <= value <= MAX_WORKERS:
                raise CaptureError(code)
        if pages_per_turn is not None and (isinstance(pages_per_turn, bool) or not isinstance(
                pages_per_turn, int) or pages_per_turn < 1):
            raise CaptureError("pages_per_turn_invalid")
        if max_pending_ocr_bytes is not None and (
                isinstance(max_pending_ocr_bytes, bool)
                or not isinstance(max_pending_ocr_bytes, int) or max_pending_ocr_bytes < 1):
            raise CaptureError("max_pending_ocr_bytes_invalid")
        self.max_pending_ocr_bytes = max_pending_ocr_bytes
        # Whether the OCR backlog is at its bound (read by reader threads, set here).
        self._backlog_full = False
        # The scope's ``ocr`` ``deferred`` (``run`` takes it): scans' bytes dropped.
        self._defer_all = False
        # Originals stored this message, settled once their rows are written.
        self._unsettled: dict[str, tuple[bytes, Mapping[str, Any] | None]] = {}
        self._monotonic = monotonic
        self._deadline = None if budget_seconds is None else monotonic() + budget_seconds
        if isinstance(attachment_list_cap, bool) or not isinstance(attachment_list_cap, int) \
                or not 1 <= attachment_list_cap <= MAX_ATTACHMENTS_LISTED:
            raise CaptureError("attachment_list_cap_invalid")
        self.attachment_list_cap = attachment_list_cap
        self.store, self.reader, self.actor = store, reader, actor
        self.page_size = page_size
        self.throttle_waits = tuple(float(w) for w in throttle_waits)
        self._sleep = sleep
        self.page_budget = page_budget
        self.max_attachment_bytes = max_attachment_bytes
        self.window_days = 90  # ``run`` takes the scope's window
        self.history = "window"  # ``run`` takes the scope's
        self.workers = workers
        self.pages_per_turn = pages_per_turn
        self.defer_ocr = defer_ocr
        self.ocr_workers = ocr_workers
        self._ocr: _OcrQueue | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._pages_read: dict[tuple[str, str], int] = {}   # per folder, this object's runs

    def _spent(self) -> bool:
        return self._deadline is not None and self._monotonic() >= self._deadline

    # ---------------------------------------------------------------- per message
    #
    # Reader side (``_download``, ``_list_attachments``, ``_read_message``, ``_prefetch``):
    # Graph reads and text extraction only, never the store, so a reader thread may run
    # it. Store side (``_commit_*``): this thread only, in page order.

    def _download(self, mailbox_id: str, message: Mapping[str, Any], meta: Mapping[str, Any],
                  known: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """One attachment, reader side: its bytes and text (``content``, ``text``), or the
        captured copy it is (``reused``: the same message, id, size and last-modified in
        ``known``, so a message whose changeKey moved re-downloads nothing), or why not."""
        entry: dict[str, Any] = {"id": meta["id"], "metadata": dict(meta)}
        size = meta.get("size")
        if meta.get("kind") not in (None, FILE_ATTACHMENT_TYPE):
            entry["content_status"] = "not_file_attachment"
            return entry
        if isinstance(size, int) and size > self.max_attachment_bytes + 1_000_000:
            entry["content_status"] = "exceeds_cap"
            return entry
        for row in known:
            if (row["attachment_id"] == meta["id"]
                    and row["metadata"].get("size") == meta.get("size")
                    and row["metadata"].get("last_modified") == meta.get("last_modified")):
                entry.update(content_status="captured", content_sha256=row["content_sha256"],
                             metadata={**row["metadata"]}, text_status=row["text_status"],
                             text_sha256=row["text_sha256"], reused=True)
                return entry
        if self._backlog_full and is_paged_file({"mime_type": meta.get("content_type"),
                                                 "name": meta.get("name")}):
            # A PDF may be a scan whose bytes wait for OCR: not read while the backlog is
            # at its bound; a later round reads it (degraded, its folder incomplete).
            entry["content_status"] = ATTACHMENT_DEFERRED
            return entry
        try:
            read = self.reader.get_attachment(
                self.actor, mailbox_id, message["id"], meta["id"],
                max_bytes=self.max_attachment_bytes, message=message)
        except SourceUnavailable as exc:
            if _transient(unavailable_detail(exc)):
                raise
            text = str(exc)
            entry["content_status"] = (
                "not_file_attachment" if "not a file attachment" in text
                else MISMATCH_SIZE if "exceed its reported size" in text
                else "exceeds_cap" if "byte bound" in text else "unavailable")
            return entry
        listed, reported = meta.get("size"), read["attachment"].get("size")
        if listed is not None and reported is not None and listed != reported:
            entry["content_status"] = MISMATCH_SIZE   # not the attachment listed
            return entry
        status, text = capture_text(read["content"], read["attachment"],
                                    defer_ocr=self.defer_ocr or self._defer_all)
        entry.update(content=read["content"], read=dict(read["attachment"]),
                     text_status=status, text=text,
                     readings=_readings(read["content"], read["attachment"], status, text,
                                        defer_ocr=self._defer_all))
        return entry

    def _commit_attachment(self, message: Mapping[str, Any], entry: dict[str, Any],
                           counts: dict[str, int]) -> dict[str, Any]:
        """One attachment, store side: bytes pinned by SHA-256, its text stored, counted;
        its original settled once its row is written (``_settle_new``)."""
        readings = entry.pop("readings", None)
        if entry.pop("reused", False):
            counts["attachments_reused"] += 1
        elif "content" in entry:
            content, read, text = entry.pop("content"), entry.pop("read"), entry.pop("text")
            try:
                digest = self.store.put_blob(content, read["content_sha256"])
            except CaptureError as exc:
                if exc.code != "attachment_digest_mismatch":
                    raise
                entry.pop("text_status", None)
                entry["content_status"] = MISMATCH_SHA256
                counts["attachments_mismatched"] += 1
                return entry
            entry.update(content_status="captured", content_sha256=digest, metadata=read,
                         text_sha256=self.store.put_text(text))
            self._unsettled[digest] = (content, readings)
            counts["attachments_captured"] += 1
            counts["attachment_bytes"] += len(content)
            _count_text(entry["text_status"], counts)
        else:
            counts["attachments_mismatched"] += entry["content_status"] == MISMATCH_SIZE
            counts["attachments_deferred"] += entry["content_status"] == ATTACHMENT_DEFERRED
            return entry
        entry["l1_handle"] = mail_attachment_citation(message["citation"], entry["id"],
                                                      entry["content_sha256"])
        return entry

    def _fetch_attachment(self, mailbox_id: str, message: Mapping[str, Any],
                          meta: Mapping[str, Any], counts: dict[str, int]) -> dict[str, Any]:
        """One attachment entry, read and stored on this thread: bytes captured and
        pinned, or why not."""
        known = self.store.captured_attachments(mailbox_id, message["id"])
        return self._commit_attachment(message, self._download(mailbox_id, message, meta,
                                                               known), counts)

    def _list_attachments(self, mailbox_id: str, message: Mapping[str, Any],
                          known: Sequence[Mapping[str, Any]], skip: Iterable[str] = ()
                          ) -> tuple[list[dict[str, Any]], str]:
        """Reader side: (each listed attachment ``_download``-ed, listing state:
        ``truncated`` when the listing stopped at a cap). ``skip``: attachment ids
        already stored for this version (not read again)."""
        listing = self.reader.list_attachments(self.actor, mailbox_id, message["id"],
                                               max_items=self.attachment_list_cap)
        status = ("complete" if listing["completeness"].get("complete", False)
                  else ATTACHMENTS_TRUNCATED)
        skipped = frozenset(skip)
        return [self._download(mailbox_id, message, meta, known)
                for meta in listing["items"] if meta["id"] not in skipped], status

    def _commit_attachments(self, message: Mapping[str, Any], entries: Sequence[dict[str, Any]],
                            status: str, counts: dict[str, int]
                            ) -> tuple[list[dict[str, Any]], str]:
        counts["attachment_lists_incomplete"] += status == ATTACHMENTS_TRUNCATED
        captured = []
        for entry in entries:
            entry = self._commit_attachment(message, entry, counts)
            if entry["content_status"] != "captured":
                counts["attachments_metadata_only"] += 1
            captured.append(entry)
        return captured, status

    def _attachments(self, mailbox_id: str, message: Mapping[str, Any],
                     counts: dict[str, int], *, skip: Iterable[str] = ()
                     ) -> tuple[list[dict[str, Any]], str]:
        """(attachment entries, listing state), read and stored on this thread:
        ``truncated`` when the listing stopped at a cap, so the version is retried later.
        ``skip``: attachment ids already stored for this version (not read again)."""
        known = self.store.captured_attachments(mailbox_id, message["id"])
        return self._commit_attachments(
            message, *self._list_attachments(mailbox_id, message, known, skip), counts)

    def _read_message(self, mailbox_id: str, folder_id: str, message_id: str
                      ) -> tuple[dict[str, Any] | None, str]:
        """(record with body, body status); (None, 'vanished') when it is gone."""
        try:
            full = self.reader.get_message(self.actor, mailbox_id, message_id,
                                           include_body=True, max_body_chars=MAX_BODY_CHARS,
                                           expected_folder_id=folder_id)
        except NotVisible:
            return None, "vanished"
        except SourceUnavailable as exc:
            detail = unavailable_detail(exc)
            if _transient(detail):
                raise
            return None, f"unavailable:{detail}"
        return full, "truncated" if full.get("body_truncated") else "captured"

    def _prefetch(self, mailbox_id: str, folder_id: str, item: Mapping[str, Any],
                  known: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Reader side of one listed message new to the store: its body, then its
        attachments (``known``: the captured attachments of this message)."""
        full, body_status = self._read_message(mailbox_id, folder_id, item["id"])
        if body_status == "vanished":
            return {"vanished": True}
        record = full if full is not None else dict(item)
        fetched: dict[str, Any] = {"full": full, "record": record, "body_status": body_status}
        if record.get("has_attachments"):
            try:
                fetched["attachments"] = self._list_attachments(mailbox_id, record, known)
            except NotVisible:
                fetched["attachments_vanished"] = True
        return fetched

    def _commit_message(self, folder_id: str, fetched: Mapping[str, Any],
                        counts: dict[str, int]) -> None:
        if fetched.get("vanished"):
            counts["vanished"] += 1  # moved or deleted since the page; delta reports it
            return
        counts["bodies_unavailable" if fetched["full"] is None else "bodies_captured"] += 1
        record = fetched["record"]
        if self._observed(self.store.observe(record["citation"], folder_id), counts):
            return
        if fetched.get("attachments_vanished"):
            counts["vanished"] += 1  # gone between the message and its attachments
            return
        attachments, listed = (self._commit_attachments(record, *fetched["attachments"], counts)
                               if "attachments" in fetched else ([], "complete"))
        superseded = self.store.add_version(record, folder_id=folder_id,
                                            body_status=fetched["body_status"],
                                            attachments=attachments, attachments_status=listed)
        counts["versions_new"] += 1
        counts["versions_superseded"] += superseded
        self._settle_new(counts)
        self._queue_ocr_of(record["citation"])

    def _settle_new(self, counts: dict[str, int]) -> None:
        """Settle the originals stored since the last call, their rows now written
        (``MailCaptureStore.settle_original``), then re-measure the OCR backlog."""
        unsettled, self._unsettled = self._unsettled, {}
        for digest, (content, readings) in unsettled.items():
            counts["originals_dropped"] += _settle(self.store, digest, content=content,
                                                   readings=readings,
                                                   defer_ocr=self._defer_all) == "dropped"
        if unsettled:
            self._check_backlog()

    def _check_backlog(self) -> None:
        self._backlog_full = (self.max_pending_ocr_bytes is not None and
                              self.store.pending_ocr_bytes() >= self.max_pending_ocr_bytes)

    def _observed(self, outcome: str, counts: dict[str, int]) -> bool:
        if outcome == "new":
            return False
        counts["reappeared" if outcome == "reappeared" else "unchanged"] += 1
        return True

    def _capture_message(self, mailbox_id: str, folder_id: str, item: Mapping[str, Any],
                         counts: dict[str, int]) -> None:
        counts["messages_seen"] += 1
        if self._observed(self.store.observe(item["citation"], folder_id), counts):
            return
        known = self.store.captured_attachments(mailbox_id, item["id"])
        self._commit_message(folder_id, self._prefetch(mailbox_id, folder_id, item, known),
                             counts)

    def _capture_page(self, mailbox_id: str, folder_id: str, items: Sequence[Mapping[str, Any]],
                      counts: dict[str, int], *, may_cut: bool) -> bool:
        """Capture one page's messages in page order; False when the time budget cut it
        after a message (``may_cut``: the page has a stored cursor to resume from). With
        ``workers`` > 1 the messages new to the store are read ahead by the reader pool
        (at most two per worker in flight) and stored here in page order; a read that
        raises cancels the reads not started and waits for the running ones, so no
        request outlives the page."""
        if self.workers == 1:
            for n, item in enumerate(items):
                if n and may_cut and self._spent():
                    return False
                self._capture_message(mailbox_id, folder_id, item, counts)
                self._drain_ocr()
            return True
        if self._pool is None:
            self._pool = ThreadPoolExecutor(self.workers, thread_name_prefix="mail-read")
        ahead: list[tuple[str, Future | None]] = []
        position = 0

        def submit(item: Mapping[str, Any]) -> tuple[str, Future | None]:
            outcome = self.store.observe(item["citation"], folder_id)
            if outcome != "new":
                return outcome, None
            known = self.store.captured_attachments(mailbox_id, item["id"])
            return outcome, self._pool.submit(self._prefetch, mailbox_id, folder_id, item,
                                              known)
        try:
            for n in range(len(items)):
                spent = self._spent()
                if n and may_cut and spent:
                    return False
                while position <= n or (position < len(items) and not spent
                                        and len(ahead) < 2 * self.workers):
                    ahead.append(submit(items[position]))
                    position += 1
                outcome, future = ahead.pop(0)
                counts["messages_seen"] += 1
                if future is None:
                    self._observed(outcome, counts)
                else:
                    self._commit_message(folder_id, future.result(), counts)
                self._drain_ocr()
            return True
        finally:
            running = [f for _o, f in ahead if f is not None and not f.cancel()]
            if running:
                wait_futures(running)

    # ---------------------------------------------------------------- OCR beside the capture

    def _queue_ocr_of(self, message_handle: str) -> None:
        """Queue the deferred OCR of a just-stored version's scanned attachments."""
        if self._ocr is None:
            return
        for row in self.store.db.execute(
                "SELECT l1_handle, content_sha256, metadata FROM attachments WHERE "
                "message_handle=? AND text_status=? AND l1_handle IS NOT NULL",
                (message_handle, OCR_PENDING)).fetchall():
            self._ocr.submit(row["l1_handle"], row["content_sha256"],
                             json.loads(row["metadata"]))

    def _drain_ocr(self) -> None:
        if self._ocr is not None and self._ocr.drain():
            self._check_backlog()

    def _repair_degraded(self, mailbox_id: str, folder_id: str,
                         counts: dict[str, int]) -> None:
        """Retry bodies and attachments a non-transient failure left out, for current
        versions of this folder (bounded per round). A read that now returns another
        changeKey is left to the next delta round, which reports the change."""
        for row in self.store.degraded(mailbox_id, folder_id, REPAIRS_PER_ROUND):
            if self._spent():
                break   # the rest is retried in a later round
            handle = row["l1_handle"]
            message = json.loads(row["record"])
            if row["body_status"].startswith("unavailable:"):
                full, status = self._read_message(mailbox_id, folder_id, row["message_id"])
                if full is None or full["citation"] != handle:
                    continue
                self.store.fill_body(handle, full, status)
                counts["bodies_recovered"] += 1
                message = full
            message = {**message, "citation": handle}
            if row["attachments_status"] == ATTACHMENTS_TRUNCATED:
                stored = [r["attachment_id"] for r in self.store.db.execute(
                    "SELECT attachment_id FROM attachments WHERE message_handle=?", (handle,))]
                try:
                    entries, listed = self._attachments(mailbox_id, message, counts,
                                                        skip=stored)
                except NotVisible:
                    continue
                attempts = row["attachment_list_attempts"] + 1
                if (listed == ATTACHMENTS_TRUNCATED and attempts >= CAPPED_AFTER_ATTEMPTS
                        and self.attachment_list_cap >= MAX_ATTACHMENTS_LISTED):
                    # Still cut at the hard cap: stop re-listing, stay incomplete.
                    listed = ATTACHMENTS_TRUNCATED_CAPPED
                    counts["attachment_lists_capped"] += 1
                self.store.add_attachments(handle, entries, listed)
                self._settle_new(counts)
                counts["attachment_lists_recovered"] += listed not in _TRUNCATED_STATES
            for item in self.store.db.execute(
                    "SELECT * FROM attachments WHERE message_handle=? AND "
                    "(content_status IN ('unavailable', ?) OR content_status LIKE ?)",
                    (handle, ATTACHMENT_DEFERRED, _MISMATCH)).fetchall():
                meta = json.loads(item["metadata"])
                try:
                    entry = self._fetch_attachment(mailbox_id, message, meta, counts)
                except NotVisible:
                    break
                if entry["content_status"] not in ("unavailable", ATTACHMENT_DEFERRED) \
                        and not entry["content_status"].startswith("mismatch:"):
                    self.store.fill_attachment(handle, entry)
                    counts["attachments_recovered"] += entry["content_status"] == "captured"
                self._settle_new(counts)

    def _reextract(self, mailbox_id: str, folder_id: str, counts: dict[str, int]) -> None:
        """Read the text of captured attachments of this folder never read, or whose
        scanned pages were never OCR-read (bounded per round; no source read). Only on
        a host with a local OCR engine (without one the bytes read as they did)."""
        from .document_understanding import local_ocr_available

        if not local_ocr_available():
            return
        if self._ocr is not None:   # read beside the capture, never on this thread
            for row in self.store.text_unread(mailbox_id, folder_id, REPAIRS_PER_ROUND):
                self._ocr.submit(row["l1_handle"], row["content_sha256"],
                                 json.loads(row["metadata"]))
            return
        for row in self.store.text_unread(mailbox_id, folder_id, REPAIRS_PER_ROUND):
            if self._spent():
                break   # the rest is read in a later round
            try:
                content = self.store.blob(row["content_sha256"])
            except (CaptureError, OSError):
                continue   # bytes missing or altered: left as they are, never re-read
            status, text = capture_text(content, json.loads(row["metadata"]))
            self.store.fill_text(row["l1_handle"], status, self.store.put_text(text))
            counts["texts_reextracted"] += 1
            _count_text(status, counts)
            counts["originals_dropped"] += _settle(self.store, row["content_sha256"],
                                                   content=content) == "dropped"

    # ---------------------------------------------------------------- per folder

    def _window(self) -> str:
        return (self.store._clock() - timedelta(days=self.window_days)).astimezone(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def capture_folder(self, mailbox_id: str, folder_key: str, *, turn: int | None = None
                       ) -> dict[str, Any]:
        """One folder's delta round (then, with ``history`` all, its older mail);
        ``cursor`` names what the next run resumes from (``next_url`` mid-round,
        ``delta_url`` after a round, ``None``: starts over). ``turn``: at most that many
        pages this call, then reason ``turn`` with the cursor stored (``run`` comes back)."""
        try:
            result = self._capture_folder(mailbox_id, folder_key, turn)
        except Exception as exc:   # noqa: BLE001 - typed below, never the message
            result = self._folder_failed(mailbox_id, folder_key, exc)
        state = self.store.folder_state(mailbox_id, folder_key) or {}
        result["cursor"] = ("next_url" if state.get("next_url")
                            else "delta_url" if state.get("delta_url") else None)
        return result

    def _folder_failed(self, mailbox_id: str, folder_key: str,
                       exc: Exception) -> dict[str, Any]:
        """A folder whose round raised something the round does not itself type (a
        store or parse error, a bad page): recorded ``incomplete`` with the typed reason
        ``folder_error:<cause>`` (:func:`folder_error_reason`: a fixed code or the
        exception class, never its message, which may carry mail content), the stored
        cursor kept so the next run resumes from it, and the run goes on to the next
        folder: one bad folder never stalls the stream."""
        reason = folder_error_reason(exc)
        try:
            self.store.save_folder(mailbox_id, folder_key, status="incomplete",
                                   reason=reason, last_run_at=self.store.now())
        except Exception:   # noqa: BLE001 - the store itself failing: reported, not raised
            pass
        return {"folder": folder_key if folder_key in grammar.WELL_KNOWN_MAIL_FOLDERS
                else "configured_id", "counts": _new_counts(), "round": None,
                "status": "incomplete", "complete": False, "reason": reason}

    def _capture_folder(self, mailbox_id: str, folder_key: str,
                        turn: int | None = None) -> dict[str, Any]:
        counts = _new_counts()
        store = self.store
        state = store.folder_state(mailbox_id, folder_key)
        restart = (state is None or state["status"] == "new"
                   or state["window_days"] != self.window_days
                   or not (state["next_url"] or state["delta_url"]))
        result = {"folder": folder_key if folder_key in grammar.WELL_KNOWN_MAIL_FOLDERS
                  else "configured_id", "counts": counts}
        if self._spent():
            # Deferred, nothing read: the stored cursors are kept and last_run_at is not
            # touched, so the next run schedules this folder before the ones that spent
            # this run's budget. A complete folder stays complete (its next run reads only
            # its delta) and a terminal reason stays; any other folder is cut.
            result.update(round=None, deferred=True)
            if state is not None and state["status"] == "complete":
                return {**result, "status": "complete", "complete": True, "reason": None}
            if state is not None and terminal_reason(state["reason"]):
                return {**result, "status": "incomplete", "complete": False,
                        "reason": state["reason"]}
            store.save_folder(mailbox_id, folder_key, status="incomplete", reason=TIME_BUDGET)
            return {**result, "status": "incomplete", "complete": False, "reason": TIME_BUDGET}

        def start_round() -> tuple[str | None, str, str]:
            folder_id = self.reader.resolve_folder(self.actor, mailbox_id, folder_key)
            since = self._window()
            store.save_folder(mailbox_id, folder_key, folder_id=folder_id,
                              window_days=self.window_days, received_since=since,
                              next_url=None, delta_url=None, round_kind="initial",
                              round_started_at=store.now(), status="in_progress", reason=None)
            return None, folder_id, "initial"

        try:
            if restart:
                cursor, folder_id, kind = start_round()
            else:
                folder_id = state["folder_id"]
                if state["next_url"]:
                    cursor, kind = state["next_url"], state["round_kind"] or "initial"
                else:
                    cursor, kind = state["delta_url"], "incremental"
                    store.save_folder(mailbox_id, folder_key, round_kind=kind,
                                      round_started_at=store.now(), status="in_progress",
                                      reason=None)
                result["resumed_from"] = "next_url" if state["next_url"] else "delta_url"
        except (NotVisible, SourceUnavailable) as exc:
            reason = _failure(exc) if isinstance(exc, SourceUnavailable) else "not_visible"
            store.save_folder(mailbox_id, folder_key, status="incomplete",
                              reason=reason, last_run_at=store.now())
            result["blocked"] = _blocked("folder", exc)
            return {**result, "status": "incomplete", "complete": False,
                    "reason": reason, "round": None}
        result["round"] = kind
        throttles = 0
        gone_restarts = 0
        pages_this_turn = 0
        unit = (mailbox_id, folder_key)
        while True:
            since = store.folder_state(mailbox_id, folder_key)["received_since"]
            try:
                for page in self.reader.message_delta(
                        self.actor, mailbox_id, folder_id, cursor=cursor,
                        received_since=since, page_size=self.page_size):
                    counts["pages"] += 1
                    pages_this_turn += 1
                    self._pages_read[unit] = self._pages_read.get(unit, 0) + 1
                    counts["provenance_dropped"] += page["provenance_dropped"]
                    # Sent by an excluded person: dropped by the reader before any
                    # body read, never stored or indexed; only the count is kept.
                    counts["people_dropped"] += page.get("people_dropped", 0)
                    # Spent within a page (each message may read a body and attachments):
                    # cut there, the stored cursor still this page's, so the next run
                    # resumes from it (in progress, what this run stored of it counts
                    # unchanged). A round's first page has no stored cursor to resume
                    # from: it is read whole, so the cut keeps its next_url (decision
                    # 2026-10-19-sharepoint-listing-resume: a cut resumes).
                    if not self._capture_page(mailbox_id, folder_id, page["items"], counts,
                                              may_cut=cursor is not None):
                        store.save_folder(mailbox_id, folder_key, status="incomplete",
                                          reason=TIME_BUDGET, last_run_at=store.now())
                        return {**result, "status": "incomplete", "complete": False,
                                "reason": TIME_BUDGET}
                    for gone in page["removed"]:
                        counts["removed"] += store.remove(mailbox_id, gone["id"], folder_id,
                                                          gone["reason"])
                    if page["delta_url"] is not None:
                        done = store.folder_state(mailbox_id, folder_key)
                        if done["round_kind"] == "initial":
                            # A full round lists everything in the window: what it did
                            # not list is gone (deleted/moved while a token was stale).
                            counts["absent_after_resync"] += store.reconcile_round(
                                mailbox_id, folder_id, done["round_started_at"],
                                done["received_since"])
                        self._repair_degraded(mailbox_id, folder_id, counts)
                        self._reextract(mailbox_id, folder_id, counts)
                        if self.history == "all" and done["backfill_status"] != "complete":
                            # The round is current: its deltaLink is kept, then the mail
                            # older than its window is read, newest first (_backfill).
                            store.save_folder(mailbox_id, folder_key, next_url=None,
                                              delta_url=page["delta_url"],
                                              last_complete_at=store.now(),
                                              rounds_complete=done["rounds_complete"] + 1)
                            stopped = self._backfill(mailbox_id, folder_key, folder_id,
                                                     counts, result, pages_this_turn, turn)
                            if stopped is not None:
                                return stopped
                            done = store.folder_state(mailbox_id, folder_key)
                            done["rounds_complete"] -= 1   # counted above
                        if store.truncated_lists(mailbox_id, folder_id):
                            # The round reached its deltaLink, but a current version's
                            # attachment list was cut: not complete until a later run
                            # lists it whole (the deltaLink is kept for that run).
                            store.save_folder(mailbox_id, folder_key, next_url=None,
                                              delta_url=page["delta_url"], status="incomplete",
                                              reason="attachment_lists_truncated",
                                              last_run_at=store.now(),
                                              rounds_complete=done["rounds_complete"] + 1)
                            return {**result, "status": "incomplete", "complete": False,
                                    "reason": "attachment_lists_truncated"}
                        if store.deferred_attachments(mailbox_id, folder_id):
                            # PDFs not read while the OCR backlog was at its bound: read
                            # by a later round; the folder is not complete until then.
                            store.save_folder(mailbox_id, folder_key, next_url=None,
                                              delta_url=page["delta_url"], status="incomplete",
                                              reason="attachments_deferred",
                                              last_run_at=store.now(),
                                              rounds_complete=done["rounds_complete"] + 1)
                            return {**result, "status": "incomplete", "complete": False,
                                    "reason": "attachments_deferred"}
                        if store.mismatched(mailbox_id, folder_id):
                            # Bytes that failed verification are kept as metadata only and
                            # retried; the folder is not complete while one is current.
                            store.save_folder(mailbox_id, folder_key, next_url=None,
                                              delta_url=page["delta_url"], status="incomplete",
                                              reason="attachment_bytes_mismatch",
                                              last_run_at=store.now(),
                                              rounds_complete=done["rounds_complete"] + 1)
                            return {**result, "status": "incomplete", "complete": False,
                                    "reason": "attachment_bytes_mismatch"}
                        store.save_folder(mailbox_id, folder_key, next_url=None,
                                          delta_url=page["delta_url"], status="complete",
                                          reason=None, last_complete_at=store.now(),
                                          last_run_at=store.now(),
                                          rounds_complete=done["rounds_complete"] + 1)
                        return {**result, "status": "complete", "complete": True,
                                "reason": None}
                    cursor = page["next_url"]
                    store.save_folder(mailbox_id, folder_key, next_url=cursor)
                    if self.page_budget is not None and \
                            self._pages_read[unit] >= self.page_budget:
                        store.save_folder(mailbox_id, folder_key, status="incomplete",
                                          reason="page_budget", last_run_at=store.now())
                        return {**result, "status": "incomplete", "complete": False,
                                "reason": "page_budget"}
                    if self._spent():
                        store.save_folder(mailbox_id, folder_key, status="incomplete",
                                          reason=TIME_BUDGET, last_run_at=store.now())
                        return {**result, "status": "incomplete", "complete": False,
                                "reason": TIME_BUDGET}
                    if turn is not None and pages_this_turn >= turn:
                        # This folder's turn is over: the run reads the other folders'
                        # pages, then comes back to this cursor (run()).
                        store.save_folder(mailbox_id, folder_key, status="incomplete",
                                          reason=TURN)
                        return {**result, "status": "incomplete", "complete": False,
                                "reason": TURN}
                raise SourceUnavailable("graph listing invalid: delta stream ended "
                                        "without a deltaLink")
            except CaptureError as exc:
                if exc.code != "store_full":
                    raise
                reason = "store_full"
            except NotVisible:
                reason = "not_visible"
            except SourceUnavailable as exc:
                detail = unavailable_detail(exc) or "unclassified"
                if _transient(detail) and throttles < len(self.throttle_waits):
                    self._sleep(self.throttle_waits[throttles])
                    throttles += 1
                    counts["throttle_waits"] += 1
                    cursor = (store.folder_state(mailbox_id, folder_key)["next_url"]
                              or cursor)
                    continue
                restarted = False
                if detail == "http_410" and gone_restarts == 0:
                    # Expired delta/skip token (syncStateNotFound): a fresh round. The
                    # restart is a source read too: its refusal ends the folder typed
                    # (below), it does not escape the round.
                    gone_restarts += 1
                    counts["round_restarts"] += 1
                    try:
                        cursor, folder_id, kind = start_round()
                        restarted = True
                    except (NotVisible, SourceUnavailable) as again:
                        exc = again
                        detail = (unavailable_detail(again) or "unclassified"
                                  if isinstance(again, SourceUnavailable) else "not_visible")
                if restarted:
                    result["round"] = kind
                    continue
                if not isinstance(exc, SourceUnavailable):
                    reason = "not_visible"
                else:
                    if is_nextlink_refusal(exc):
                        # A refused continuation is never followed or resumed: drop both
                        # cursors, so the next run starts the folder on a fresh round.
                        store.save_folder(mailbox_id, folder_key, next_url=None,
                                          delta_url=None)
                    reason = "throttled" if _transient(detail) else _failure(exc)
                    result["blocked"] = _blocked("folder", exc)
            store.save_folder(mailbox_id, folder_key, status="incomplete", reason=reason,
                              last_run_at=store.now())
            return {**result, "status": "incomplete", "complete": False, "reason": reason}

    def _backfill(self, mailbox_id: str, folder_key: str, folder_id: str,
                  counts: dict[str, int], result: Mapping[str, Any], pages_used: int,
                  turn: int | None) -> dict[str, Any] | None:
        """Read a folder's mail older than its round's window (``history`` all), newest
        first, from its stored listing cursor (``GraphMailReader.message_pages``; the
        ``before`` bound fixed when it started, so the stored cursor stays valid). None
        once it reached its end (``backfill_status`` complete: the folder may complete);
        otherwise the folder's incomplete result: its turn or the page budget is spent
        (``turn``, ``page_budget``), the time budget (``time_budget``), or a typed
        failure (the cursor kept; a refused one dropped so the history starts over). The
        older mail has no delta: a later change to it is not followed."""
        store = self.store
        state = store.folder_state(mailbox_id, folder_key)
        before = state["backfill_before"] or state["received_since"]
        if before is None:
            store.save_folder(mailbox_id, folder_key, backfill_status="complete")
            return None
        store.save_folder(mailbox_id, folder_key, backfill_before=before)
        cursor = state["backfill_next_url"]
        unit = (mailbox_id, folder_key)

        def stop(reason: str) -> dict[str, Any]:
            store.save_folder(mailbox_id, folder_key, status="incomplete", reason=reason,
                              **({} if reason == TURN else {"last_run_at": store.now()}))
            return {**result, "status": "incomplete", "complete": False, "reason": reason}

        def exhausted() -> str | None:
            if self._spent():
                return TIME_BUDGET
            if self.page_budget is not None and \
                    self._pages_read.get(unit, 0) >= self.page_budget:
                return "page_budget"
            if turn is not None and pages_used >= turn:
                return TURN
            return None
        if exhausted() is not None:
            return stop(exhausted())
        try:
            for page in self.reader.message_pages(self.actor, mailbox_id, folder_id,
                                                  before=before, cursor=cursor,
                                                  page_size=min(self.page_size, 100)):
                counts["pages"] += 1
                counts["backfill_pages"] += 1
                pages_used += 1
                self._pages_read[unit] = self._pages_read.get(unit, 0) + 1
                counts["provenance_dropped"] += page["provenance_dropped"]
                counts["people_dropped"] += page.get("people_dropped", 0)
                if not self._capture_page(mailbox_id, folder_id, page["items"], counts,
                                          may_cut=cursor is not None):
                    return stop(TIME_BUDGET)
                cursor = page["next_url"]
                if cursor is None:
                    store.save_folder(mailbox_id, folder_key, backfill_next_url=None,
                                      backfill_status="complete")
                    return None
                store.save_folder(mailbox_id, folder_key, backfill_next_url=cursor)
                if exhausted() is not None:
                    return stop(exhausted())
        except CaptureError as exc:
            if exc.code != "store_full":
                raise
            return stop("store_full")
        except NotVisible:
            return stop("not_visible")
        except SourceUnavailable as exc:
            if is_nextlink_refusal(exc):
                store.save_folder(mailbox_id, folder_key, backfill_next_url=None)
            detail = unavailable_detail(exc) or "unclassified"
            return stop("throttled" if _transient(detail) else _failure(exc))
        raise SourceUnavailable("graph listing invalid: history listing ended without an end")

    # ---------------------------------------------------------------- per run

    def _turns(self, ordered: Sequence[tuple[str, str]]) -> dict[tuple[str, str], Any]:
        """Capture ``ordered`` folders in turns: each reads at most ``pages_per_turn``
        pages, then the next folder takes its turn; the folders with more to read come
        back, in the same order, until none has or the time budget is spent (a folder
        cut by it then ends ``time_budget``). One result per folder: its last turn's,
        with the counts of every turn. Without ``pages_per_turn``, one call each."""
        done: dict[tuple[str, str], Any] = {}
        queue = list(ordered)
        while queue:
            again = []
            for unit in queue:
                result = self.capture_folder(*unit, turn=self.pages_per_turn)
                before = done.get(unit)
                if before is not None:
                    for key, value in before["counts"].items():
                        result["counts"][key] += value
                    result.pop("deferred", None)   # it was read in an earlier turn
                    # How the run started it (its first turn), never how a turn resumed.
                    result["resumed_from"] = before.get("resumed_from")
                    if before.get("round") is not None:
                        result["round"] = before["round"]
                done[unit] = result
                if result.get("reason") == TURN:
                    if self._spent():
                        self.store.save_folder(*unit, status="incomplete", reason=TIME_BUDGET,
                                               last_run_at=self.store.now())
                        result["reason"] = TIME_BUDGET
                    else:
                        again.append(unit)
            queue = again
        return done

    def _every_folder(self, mailbox_id: str) -> tuple[tuple[str, ...], bool]:
        """(the folder keys of every folder of a mailbox, the listing whole): the stored
        listing while younger than FOLDER_RELIST_SECONDS, else a new one
        (``GraphMailReader.list_folders``). A folder already captured under a well-known
        name keeps that key. An empty folder that holds no captured current message is
        left out until a listing counts mail in it. A listing that fails keeps the stored
        one (else inbox and sent items) and the mailbox is not complete."""
        stored, listed_at = self.store.folder_listing(mailbox_id)
        whole = True
        fresh = listed_at is not None and (
            self.store._clock() - datetime.fromisoformat(listed_at.replace("Z", "+00:00"))
        ).total_seconds() < FOLDER_RELIST_SECONDS
        if not fresh:
            try:
                listing = self.reader.list_folders(self.actor, mailbox_id)
                whole = listing["complete"]
                stored = [{"folder_id": f["id"], "total": f["total"]}
                          for f in listing["folders"]]
                if stored or whole:
                    self.store.save_folder_listing(
                        mailbox_id, [{"id": f["folder_id"], "total": f["total"]}
                                     for f in stored])
            except (NotVisible, SourceUnavailable):
                whole = False
        if not stored:
            return ("inbox", "sentitems"), False
        named = {row["folder_id"]: row["folder_key"] for row in self.store.db.execute(
            "SELECT folder_key, folder_id FROM folders WHERE mailbox_id=? AND folder_id "
            "IS NOT NULL", (mailbox_id,)) if row["folder_key"] in grammar.WELL_KNOWN_MAIL_FOLDERS}
        keys = [named.get(f["folder_id"], f["folder_id"]) for f in stored
                if f["total"] != 0 or self.store.folder_holds_current(mailbox_id, f["folder_id"])
                or self.store.folder_state(mailbox_id, named.get(f["folder_id"],
                                                                 f["folder_id"])) is not None]
        # A mailbox with no mail in any folder: its inbox still stands for it.
        return tuple(dict.fromkeys(keys)) or ("inbox",), whole

    def _close_pools(self) -> dict[str, int]:
        """Stop the reader pool and the OCR queue (its readings not started stay pending
        for the next run); the OCR counts."""
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
        ocr, self._ocr = self._ocr, None
        return ocr.close() if ocr is not None else {}

    def _people_blocked(self, scope: CaptureScope,
                        exc: BaseException | None = None) -> dict[str, Any]:
        """The run the people guard refused: nothing opened, listed or read; a typed
        block (codes only) that the live check reports as blocked, never a pass. When
        the guard's directory listing itself failed upstream (a refused token, a 403),
        each named mailbox carries that failure instead, under the same rule as a
        mailbox lookup that failed so: the cause is the source's, not the guard's."""
        cause = exc.__cause__ if exc is not None else None
        if isinstance(cause, SourceUnavailable):
            entries = [{"index": index, "folders": [], "status": _failure(cause),
                        "complete": False, "blocked": _blocked("mailbox", cause)}
                       for index in range(len(scope.mailboxes))]
            return {"mailboxes": entries, "directory": None, "mailboxes_listed": 0,
                    "mailboxes_excluded": 0, "people_excluded": 0, "people_blocked": None,
                    "purged": dict.fromkeys(PURGE_COUNTS, 0), "complete": False, "complete_bound": 0,
                    "bound_total": sum(len(f) for _a, f in scope.mailboxes),
                    "incomplete_reasons": [_failure(cause)]}
        people = getattr(self.reader, "people", None)
        part = (people.block_part("mail.people_exclusion") if people is not None
                else PeopleExclusion(unavailable="excluded_people_oids_absent")
                .block_part("mail.people_exclusion"))
        bound_total = sum(len(folders) for _address, folders in scope.mailboxes)
        return {"mailboxes": [], "directory": None, "mailboxes_listed": 0,
                "mailboxes_excluded": 0, "people_excluded": 0, "people_blocked": part,
                "purged": dict.fromkeys(PURGE_COUNTS, 0), "complete": False, "complete_bound": 0, "bound_total": bound_total,
                "incomplete_reasons": [f"people_exclusion:{part['error_code']}"]}

    def run(self, scope: CaptureScope, *,
            directory: Callable[[], tuple[list[str], str, int]] | None = None
            ) -> dict[str, Any]:
        """One capture run over the scope; per-mailbox and per-folder outcomes. With
        ``all_mailboxes`` the run is complete only when the directory listing was.
        ``directory`` lists the tenant's mailboxes (``directory_mailboxes(api,
        scope.directory_allows)``: the listed ids, the listing status and how many
        listed mailboxes the exclusion list dropped); each is checked against the
        exclusion list again once opened, on every identity the directory resolved."""
        self.window_days = scope.window_days
        self.history = scope.history
        self._pages_read = {}
        named = len(scope.mailboxes)
        # The people guard first: without its ID list (or the directory listing that
        # finds the excluded people's addresses) no mailbox is opened, listed or read.
        try:
            prepare = getattr(self.reader, "prepare_people", None)
            if prepare is not None:
                prepare()
        except ExclusionUnavailable as exc:
            return self._people_blocked(scope, exc)
        # A later exclusion: what the store holds of it is purged before anything is read;
        # reads hid it already.
        purged = dict.fromkeys(PURGE_COUNTS, 0)
        people = getattr(self.reader, "people", None)
        if _usable(people):
            if self.store.people is None:
                self.store.people = people
            purged = self.store.purge_withheld(people)
        targets: list[tuple[str, tuple[str, ...]]] = list(scope.mailboxes)
        directory_status = None
        excluded = 0
        if scope.all_mailboxes:
            listed, directory_status, excluded = (directory() if directory
                                                  else ([], "no_directory", 0))
            addresses = {address for address, _ in targets}
            targets += [(user, scope.all_mailbox_folders) for user in listed
                        if user not in addresses]
        mailboxes = []
        people_excluded = 0
        opened: dict[str, tuple[str, ...]] = {}   # mailbox id -> its scope folders
        with self.reader.directory_cache():
            for index, (address, folders) in enumerate(targets):
                entry: dict[str, Any] = {"index": index, "folders": [],
                                         "folders_bound": len(folders)}
                if self._spent():
                    # The budget is spent: no mailbox is opened (its lookup is a
                    # request); each one left stays bound, cut, for the next run. A
                    # listed one the scope would exclude once opened counts here too:
                    # never fewer bound than there are (gap-86a8a6debd2e).
                    entry.update(status=TIME_BUDGET, complete=False)
                    mailboxes.append(entry)
                    continue
                try:
                    identity = self.reader.open_mailbox(self.actor, address)
                except ExclusionUnavailable as exc:
                    return self._people_blocked(scope, exc)
                except PeopleExcluded:
                    # An excluded person's mailbox: refused before its lookup. A listed
                    # one is out of scope; a named one is a scope error, never read.
                    people_excluded += 1
                    if index < named:
                        entry.update(status="people_excluded", complete=False)
                        mailboxes.append(entry)
                    continue
                except NotVisible:
                    entry.update(status="not_visible", complete=False)
                    mailboxes.append(entry)
                    continue
                except SourceUnavailable as exc:
                    entry.update(status=_failure(exc), complete=False,
                                 blocked=_blocked("mailbox", exc))
                    mailboxes.append(entry)
                    continue
                identities = [*identity["identities"], identity["upn"]]
                if index >= named and scope.excludes(identities):
                    excluded += 1          # out of scope: not bound, never read
                    continue
                if index < named and not scope.allowlist.allows(identities):
                    entry.update(status="outside_allowlist", complete=False)
                    mailboxes.append(entry)
                    continue
                if identity["object_id"] in opened:
                    continue               # named and listed: captured once per run
                opened[identity["object_id"]] = folders
                self.store.record_mailbox(identity)
                entry["mailbox_id"] = identity["object_id"]
                mailboxes.append(entry)
            # A mailbox whose scope is every folder: its folders as listed (cached).
            unlisted: set[str] = set()
            for mailbox_id, folders in list(opened.items()):
                if folders == (ALL_FOLDERS,):
                    opened[mailbox_id], whole = self._every_folder(mailbox_id)
                    if not whole:
                        unlisted.add(mailbox_id)
            # Every folder of every opened mailbox, captured in the scheduler's order
            # (schedule_units) and reported in the scope's.
            units = [(mailbox_id, folder) for mailbox_id, folders in opened.items()
                     for folder in folders]
            self._defer_all = scope.ocr == "deferred"
            self._check_backlog()
            if self.ocr_workers and self._ocr is None and _ocr_engine():
                self._ocr = _OcrQueue(self.store, self.ocr_workers, self._spent)
                for row in self.store.text_unread(None, None, OCR_BACKLOG_PER_RUN):
                    self._ocr.submit(row["l1_handle"], row["content_sha256"],
                                     json.loads(row["metadata"]))
            try:
                done = self._turns(schedule_units(
                    units, lambda unit: folder_standing(self.store.folder_state(*unit))))
            finally:
                ocr = self._close_pools()
        for entry in mailboxes:
            if "mailbox_id" in entry:
                entry["folders"] = [done[(entry["mailbox_id"], folder)]
                                    for folder in opened[entry["mailbox_id"]]]
                # Complete only when every folder is, and was brought current this run.
                entry["complete"] = all(f["complete"] and not f.get("deferred")
                                        for f in entry["folders"]) \
                    and entry["mailbox_id"] not in unlisted
                entry["status"] = "complete" if entry["complete"] else "incomplete"
                if entry["mailbox_id"] in unlisted:
                    entry["folder_listing"] = "incomplete"
        bound = [(f["complete"], f["reason"]) for m in mailboxes for f in m["folders"]]
        bound += [(False, m["status"]) for m in mailboxes if not m["folders"]
                  for _folder in targets[m["index"]][1]]
        return {"mailboxes": mailboxes, "directory": directory_status,
                "mailboxes_listed": len(targets) - named, "mailboxes_excluded": excluded,
                "people_excluded": people_excluded, "people_blocked": None, "purged": purged,
                "complete": (bool(mailboxes) and all(m["complete"] for m in mailboxes)
                             and directory_status in (None, "complete")),
                "complete_bound": sum(1 for done, _r in bound if done),
                "bound_total": len(bound),
                "incomplete_reasons": sorted({r for done, r in bound if not done and r}
                                             | ({f"directory_{directory_status}"}
                                                if directory_status not in (None, "complete")
                                                else set())
                                             | ({"folder_listing_incomplete"} if unlisted
                                                else set())),
                "ocr": ocr}


def directory_members(api: Any, allowed: Callable[[Sequence[str]], bool] | None = None
                      ) -> tuple[list[tuple[str, str]], str, int]:
    """The one directory reader: ``(object id, mail)`` of every member user with a mail
    address, kept only when ``allowed([id, mail])``; guests are skipped. Also returns the
    status (``complete`` or ``truncated:<reason>``) and how many ``allowed`` refused."""
    url = grammar.url("graph.users", {"$select": "id,mail,userType", "$top": "999"})
    users, completeness = api.get_pages(url, max_pages=50, max_items=5000)
    members = {str(u["id"]).lower(): u["mail"].lower() for u in users
               if grammar.TYPES["user_id"].valid(str(u.get("id") or ""))
               and isinstance(u.get("mail"), str) and u["mail"]
               and u.get("userType") != "Guest"}
    found = [(i, mail) for i, mail in sorted(members.items())
             if allowed is None or allowed([i, mail])]
    status = ("complete" if completeness["complete"]
              else f"truncated:{completeness['truncation_reason']}")
    return found, status, len(members) - len(found)


def directory_mailboxes(api: Any, allowed: Callable[[Sequence[str]], bool] | None = None
                        ) -> tuple[list[str], str, int]:
    """Every member user of the tenant directory with a mail address (object ids),
    via the guarded, paged ``/users`` GET (``@odata.nextLink`` within the guard's
    caps), kept only when ``allowed([id, mail])`` (``CaptureScope.directory_allows``:
    not excluded); guests have no mailbox here and are skipped. Returns the ids, the
    status ``complete`` or ``truncated:<reason>`` (a run with a truncated listing is
    never complete) and the number of member mailboxes ``allowed`` refused."""
    found, status, refused = directory_members(api, allowed)
    return [i for i, _mail in found], status, refused


def capture_into(state_dir: str | Path, scope: CaptureScope, reader: Any, actor: Actor, *,
                 allow_address: Callable[[str], bool] | None = None,
                 page_budget: int | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """One capture run into ``<state_dir>/mail-capture`` for a consumer CLI
    (``operating.case_mail``), counts only.

    ``allow_address``: a further allowlist (the case mail scope): configured
    mailboxes outside it are not captured and ``all_mailboxes`` is ignored.
    ``page_budget`` bounds delta pages per folder for a bounded live check; a
    folder stopped by it is ``incomplete`` (reason ``page_budget``) and resumes on
    the next run."""
    kept = [(address, folders) for address, folders in scope.mailboxes
            if allow_address is None or allow_address(address)]
    summary: dict[str, Any] = {"mailboxes_in_scope": len(kept),
                               "mailboxes_outside_scope": len(scope.mailboxes) - len(kept)
                               + len(scope.outside_allowlist),
                               "complete": False, "folders": 0, "folders_complete": 0,
                               "reasons": [], "counts": {k: 0 for k in _FOLDER_COUNTS},
                               "blocked_upstream": []}
    every = scope.all_mailboxes and allow_address is None
    if not kept and not every:
        summary["reasons"] = ["no_mailbox_in_scope"]
        return summary
    narrowed = CaptureScope([{"address": a, "folders": ALL_FOLDERS if f == (ALL_FOLDERS,)
                              else list(f)} for a, f in kept],
                            window_days=scope.window_days,
                            internal_domains=scope.internal_domains, all_mailboxes=every,
                            all_mailbox_folders=scope.all_mailbox_folders,
                            allowlist=scope.allowlist, exclude=scope.excluded,
                            history=scope.history, ocr=scope.ocr)
    store = MailCaptureStore(state_dir)
    try:
        api = getattr(reader, "_api", None)
        run = MailCapture(store, reader, actor, page_budget=page_budget, sleep=sleep).run(
            narrowed, directory=(lambda: directory_mailboxes(api, scope.directory_allows))
            if every else None)
    finally:
        store.close()
    folders = [f for m in run["mailboxes"] for f in m["folders"]]
    reasons = {m["status"] for m in run["mailboxes"] if not m.get("folders")
               and m["status"] != "complete"}
    reasons |= {f["reason"] for f in folders if f["reason"]}
    summary.update(complete=run["complete"], folders=len(folders),
                   folders_complete=sum(1 for f in folders if f["complete"]),
                   reasons=sorted(reasons), complete_bound=run["complete_bound"],
                   bound_total=run["bound_total"], blocked_upstream=refused_parts(run))
    for folder in folders:
        for key in _FOLDER_COUNTS:
            summary["counts"][key] += folder["counts"][key]
    return summary


# ------------------------------------------------------------------ CLI (counts only)

CAPTURE_PRINCIPAL = "svc:mail-capture"


def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default=None,
                        help="Workbench state directory (outside every git work tree); "
                             "default the host's, kept across runs (check_state_dir: "
                             "$PLX_WORKBENCH_STATE_DIR/mail-capture-check)")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="wall time of all runs together; a folder cut by it resumes "
                             "from its stored cursor on the next invocation")
    parser.add_argument("--scope", default=str(DEFAULT_SCOPE),
                        help="plx-mail-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    parser.add_argument("--withheld-principal", default="staff:mail-capture-withheld",
                        help="a principal the audience does not grant; the store is read "
                             "as it to prove a withheld mailbox leaks nothing")
    parser.add_argument("--runs", type=int, default=2,
                        help="capture runs; run 2+ resumes from the stored deltaLink")
    parser.add_argument("--page-size", type=int, default=PRODUCTION["page_size"])
    parser.add_argument("--workers", type=int, default=PRODUCTION["workers"],
                        help="concurrent message reads per page (Exchange allows 4 per "
                             "mailbox)")
    parser.add_argument("--pages-per-turn", type=int, default=PRODUCTION["pages_per_turn"],
                        help="pages a folder reads before the next folder takes its turn")
    parser.add_argument("--ocr-workers", type=int, default=PRODUCTION["ocr_workers"],
                        help="threads OCR-reading scanned attachments beside the capture "
                             "(0: OCR inline)")
    parser.add_argument("--max-store-bytes", type=int, default=None,
                        help="bound on the capture store (SQLite + blobs); a folder that "
                             "would pass it stops incomplete with reason store_full")


_FOLDER_COUNTS = ("pages", "messages_seen", "versions_new", "versions_superseded",
                  "unchanged", "reappeared", "removed", "absent_after_resync", "vanished",
                  "bodies_captured", "bodies_unavailable", "bodies_recovered",
                  "attachments_captured", "attachments_reused", "attachments_recovered",
                  "attachments_metadata_only",
                  "attachment_bytes", "attachment_lists_incomplete",
                  "attachment_lists_recovered", "attachment_lists_capped",
                  "attachments_mismatched", "texts_ocr", "texts_ocr_unavailable",
                  "texts_reextracted", "provenance_dropped", "people_dropped",
                  "throttle_waits", "round_restarts")


def _report_run(number: int, run: Mapping[str, Any]) -> dict[str, Any]:
    mailboxes = []
    for entry in run["mailboxes"]:
        folders = [{"folder": f["folder"], "status": f["status"], "complete": f["complete"],
                    "reason": f["reason"], "round": f.get("round"),
                    "resumed_from": f.get("resumed_from"), "cursor": f.get("cursor"),
                    "counts": {k: f["counts"][k] for k in _FOLDER_COUNTS}}
                   for f in entry["folders"]]
        mailboxes.append({"index": entry["index"], "status": entry["status"],
                          "complete": entry["complete"], "folders": folders})
    return {"run": number, "complete": run["complete"], "directory": run["directory"],
            "complete_bound": run["complete_bound"], "bound_total": run["bound_total"],
            "incomplete_reasons": run["incomplete_reasons"],
            "people_excluded": run.get("people_excluded", 0),
            "purged": dict(run.get("purged") or dict.fromkeys(PURGE_COUNTS, 0)),
            "mailboxes": mailboxes}


def run_cli(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
            sender: Any = None, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Capture ``--runs`` times, then read the store back as the capture actor and
    as a withheld actor. Prints counts, booleans and enums only."""
    from .graph_read import GraphMailReader, build_graph
    from .live_source_guard import BoundedBackoff

    if isinstance(args.runs, bool) or not 1 <= args.runs <= 5:
        raise ValueError("runs must be 1..5")
    scope = CaptureScope.from_file(args.scope)
    audience = LiveSourceAudience.from_file(args.live_audience)
    env = os.environ if environ is None else environ
    state_base = None
    if args.state_dir is None:
        state_dir, base, state_base = check_state_dir(env, CHECK_STATE,
                                                      injected=sender is not None)
        assert_outside_git(state_dir)
        # The directories it makes below the host's state base: 0700.
        for n in range(1, len(state_dir.relative_to(base).parts) + 1):
            base.joinpath(*state_dir.relative_to(base).parts[:n]).mkdir(
                mode=0o700, parents=True, exist_ok=True)
    else:
        state_dir = Path(args.state_dir).expanduser()
    carried = store_exists(state_dir)
    store = MailCaptureStore(state_dir, max_store_bytes=args.max_store_bytes)
    try:
        api, transport = build_graph(environ, sender=sender,
                                     retry=BoundedBackoff(sleep=sleep, budget_seconds=60))
        reader = GraphMailReader(api, audience)
        store.people = reader.people   # every read below applies this run's exclusion
        actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
        withheld = Actor(audience.tenant, frozenset({args.withheld_principal}), "restricted")
        ocr_workers = getattr(args, "ocr_workers", 0)
        capture = MailCapture(store, reader, actor, page_size=args.page_size, sleep=sleep,
                              budget_seconds=getattr(args, "budget_seconds", None),
                              workers=getattr(args, "workers", 1),
                              pages_per_turn=getattr(args, "pages_per_turn", None),
                              defer_ocr=bool(ocr_workers), ocr_workers=ocr_workers)
        runs, refused = [], []
        for number in range(1, args.runs + 1):
            run = capture.run(scope, directory=lambda: directory_mailboxes(
                api, scope.directory_allows))
            runs.append(_report_run(number, run))
            runs[-1]["ocr"] = {k: v for k, v in (run.get("ocr") or {}).items()
                               if isinstance(v, int)}
            refused.append(refused_parts(run))
            last_run = run
        # Offline resolution of what was captured (no source read).
        view = store.messages(actor, audience, limit=1000)
        resolved = sum(1 for item in view["items"]
                       if store.resolve(item["l1_handle"], actor, audience) is not None)
        attachment_handles = [a["l1_handle"] for item in view["items"]
                              for a in item["attachments"] if a["l1_handle"]]
        verified = sum(1 for h in attachment_handles
                       if store.attachment_verified(h, actor, audience))
        hidden_view = store.messages(withheld, audience, limit=1000)
        hidden_counts = store.counts(withheld, audience)
        hidden_completeness = store.completeness(withheld, audience)
        hidden_resolved = sum(1 for item in view["items"]
                              if store.resolve(item["l1_handle"], withheld, audience)
                              is not None)
        counts = store.counts(actor, audience)
        completeness = store.completeness(actor, audience)
        folders = [f for m in completeness["mailboxes"] for f in m["folders"]]
        last = runs[-1]
        resumes = sum(1 for r in runs[1:] for m in r["mailboxes"] for f in m["folders"]
                      if f["resumed_from"] == "delta_url" and f["complete"])
        report = {
            "check": "graph.mail_capture",
            "mailboxes_outside_allowlist": len(scope.outside_allowlist),
            "evidence_label": "injected-transport" if transport.sender_injected
            else "live-read",
            "read_only_guard": "GET-only; token POST only",
            "storage": {"outside_git_tree": True,
                        "under_state_dir": Path(store.root).parent == state_dir.resolve(),
                        # Kept across invocations in the host's state directory (no
                        # --state-dir), and carried: the store existed before this one.
                        # Only workbench_state_dir is kept between the verifier's daily
                        # runs (check_state_dir).
                        "host_state": args.state_dir is None, "carried": carried,
                        "state_base": state_base},
            "runs": runs,
            # Every run refused outright (HTTP 403 or 401 on every bound mailbox): the
            # host's grant or credential is missing, so the check is blocked upstream,
            # never a pass or a fail. Else a last run the budget alone cut over a store
            # that was not carried (budget_cut_block): later_ask
            # live-host-persistent-state-dir, never an untyped fail.
            "blocked_upstream": refused[-1] if all(refused) else budget_cut_block(
                mail_cut_units(last_run), carried=carried),
            "summary": {"mailboxes": len(completeness["mailboxes"]),
                        "mailboxes_complete": sum(1 for m in completeness["mailboxes"]
                                                  if m["complete"]),
                        "folders": len(folders),
                        "folders_complete": sum(1 for f in folders if f["complete"]),
                        "messages_current": counts["messages_current"],
                        "versions": counts["versions"],
                        "attachments_captured": counts["attachments_captured"],
                        "delta_resumes": resumes,
                        "degraded_bodies": sum(m["degraded_bodies"]
                                               for m in completeness["mailboxes"]),
                        "degraded_attachments": sum(m["degraded_attachments"]
                                                    for m in completeness["mailboxes"]),
                        "truncated_attachment_lists": sum(
                            m["truncated_attachment_lists"] for m in completeness["mailboxes"]),
                        "mismatched_attachments": sum(
                            m["mismatched_attachments"] for m in completeness["mailboxes"]),
                        "complete_bound": last["complete_bound"],
                        "bound_total": last["bound_total"],
                        "store_bytes": store.store_bytes(),
                        "store_full": store.store_full()},
            "resolve_check": {"handles_checked": len(view["items"]),
                              "resolved_offline": resolved,
                              "attachments_checked": len(attachment_handles),
                              "attachment_bytes_verified": verified},
            "leak_check": {"withheld_actor_mailboxes": len(hidden_completeness["mailboxes"]),
                           "withheld_actor_rows": len(hidden_view["items"]),
                           "withheld_actor_counts": hidden_view["total"]
                           + sum(hidden_counts.values()),
                           "withheld_actor_resolved": hidden_resolved},
            # The ontology relations (source family graph_mail) this run holds records of:
            # a passing check names them, so the source registry counts them evaluated.
            "relations": {name: n for name, n in (
                ("messages", counts["messages_current"]),
                ("attachments", counts["attachments_captured"])) if n},
            "requests_sent": transport.requests_sent,
            # The Graph reads it made (grammar operations): the endpoints a pass evaluates.
            "operations_read": sorted(transport.operations_read),
        }
        report["ok"] = bool(
            last["complete"] and report["summary"]["folders"]
            and report["summary"]["folders_complete"] == report["summary"]["folders"]
            and resolved == len(view["items"]) and verified == len(attachment_handles)
            and report["leak_check"]["withheld_actor_rows"] == 0
            and report["leak_check"]["withheld_actor_counts"] == 0
            and report["leak_check"]["withheld_actor_mailboxes"] == 0
            and hidden_resolved == 0)
        sound = (resolved == len(view["items"]) and verified == len(attachment_handles)
                 and not any(report["leak_check"].values()))
        if not sound and not all(refused):   # blocked only while the cut is the whole failure
            report["blocked_upstream"] = []
        return report
    finally:
        store.close()


# ------------------------------------------------------------------ compact (one-time)

def compact_cli(argv: Sequence[str] | None = None) -> int:
    """``python3 -m integrations.capture_mail compact --state-dir D``: compact a store
    captured before compact storage (:meth:`MailCaptureStore.compact`), holding the
    host's cycle lock (``<D>/host/cycle.lock``) so no capture runs beside it. Prints
    counts and bytes only."""
    parser = argparse.ArgumentParser(prog="python3 -m integrations.capture_mail compact",
                                     description=compact_cli.__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True,
                        help="Workbench state directory holding mail-capture/")
    parser.add_argument("--no-vacuum", action="store_true",
                        help="prune and compress only; leave the SQLite file as it is")
    parser.add_argument("--defer-ocr", action="store_true",
                        help="also drop the bytes of scans whose OCR is pending (status "
                             "ocr_deferred; read later by the ocr-deferred command)")
    args = parser.parse_args(argv)
    return _locked(args.state_dir, lambda store: store.compact(
        vacuum=not args.no_vacuum, defer_ocr=args.defer_ocr))


def _locked(state_dir: str, work: Callable[[Any], dict[str, Any]], *,
            open_store: Callable[[Path], Any] = lambda state: MailCaptureStore(state),
            exists: Callable[[Path], bool] = lambda state: store_exists(state),
            missing: str = "mail_capture_missing") -> int:
    """``work`` on the store under ``state_dir`` (``open_store``; the mail store, or the
    SharePoint store's ``compact``) while holding the host's cycle lock
    (``<D>/host/cycle.lock``: no capture runs beside it); prints its counts."""
    import fcntl

    state = Path(state_dir).expanduser()
    if not exists(state):
        print(json.dumps({"ok": False, "error": missing}))
        return 2
    lock_dir = state / "host"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(lock_dir / "cycle.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"ok": False, "error": "host_cycle_running"}))
            return 2
        try:
            store = open_store(state)
            try:
                result = work(store)
            finally:
                store.close()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


def ocr_deferred_cli(argv: Sequence[str] | None = None, *,
                     environ: Mapping[str, str] | None = None, sender: Any = None) -> int:
    """``python3 -m integrations.capture_mail ocr-deferred --state-dir D --live-audience F
    --budget-seconds N``: OCR the attachments whose OCR was deferred
    (:func:`ocr_deferred_pass`), re-fetching each original by one guarded Graph GET
    (credentials from the environment, as the capture). Needs a local OCR engine. Holds
    the host's cycle lock. Prints counts only."""
    from .graph_read import GraphMailReader, build_graph
    from .live_source_guard import BoundedBackoff

    parser = argparse.ArgumentParser(prog="python3 -m integrations.capture_mail ocr-deferred",
                                     description=ocr_deferred_cli.__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    parser.add_argument("--budget-seconds", type=float, required=True)
    args = parser.parse_args(argv)
    if not args.budget_seconds > 0:
        print(json.dumps({"ok": False, "error": "budget_seconds_invalid"}))
        return 2
    if not _ocr_engine():
        print(json.dumps({"ok": False, "error": "ocr_engine_unavailable"}))
        return 2
    audience = LiveSourceAudience.from_file(args.live_audience)
    env = os.environ if environ is None else environ
    api, _transport = build_graph(env, sender=sender, retry=BoundedBackoff(budget_seconds=60))
    reader = GraphMailReader(api, audience)
    actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")

    def work(store: MailCaptureStore) -> dict[str, Any]:
        store.people = reader.people   # every read applies this run's exclusion
        return ocr_deferred_pass(store, reader, actor, audience,
                                 budget_seconds=args.budget_seconds)
    return _locked(args.state_dir, work)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["compact"]:
        return compact_cli(argv[1:])
    if argv[:1] == ["ocr-deferred"]:
        return ocr_deferred_cli(argv[1:])
    print("usage: python3 -m integrations.capture_mail compact --state-dir D [--no-vacuum] "
          "[--defer-ocr]\n       python3 -m integrations.capture_mail ocr-deferred "
          "--state-dir D --live-audience F --budget-seconds N"
          "\n(capture runs: python3 -m integrations.graph_read mail-capture ...)")
    return 2


__all__ = ["CAPTURE_PRINCIPAL", "PERSISTENT_STATE_ASK", "CaptureError", "CaptureScope",
           "MAIL_KIND", "MailCapture", "MailCaptureStore", "WORKBENCH_STATE_ENV",
           "add_cli_arguments", "assert_outside_git", "blocked_reason", "budget_cut_block",
           "capture_into", "check_state_dir", "compact_cli", "ocr_deferred_cli",
           "ocr_deferred_gap", "ocr_deferred_pass", "directory_mailboxes", "folder_standing",
           "graph_block", "mail_cut_units", "run_cli", "schedule_units", "store_exists",
           "terminal_reason"]


if __name__ == "__main__":
    raise SystemExit(main())
