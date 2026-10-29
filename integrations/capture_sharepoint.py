"""Versioned SharePoint capture (L1): drive items of a bounded scope into Workbench state.

Owner decisions 5be438765457 and 93047ba19524, 10-DECISION 2: the Workbench keeps
what it reads from SharePoint in its own credentialed capture store, so a
``sharepoint://`` citation resolves offline to exactly the version it cited, a
changed file keeps its history and the projection (``ontology.project
.SharePointCaptures``) can serve ``SharePointFile`` objects with their mention links.
Nothing captured goes to Git, logs or advisory records. Who may *see* a captured
file is decided again on every read, per reader: the reader's
``LiveSourceAudience`` over the item's path keys (``sharepoint:<drive>:<path>``)
and the item permissions recorded at capture. A file withheld from a reader yields
no row and no count; nothing says it exists.

Scope (``plx-sharepoint-capture-scope/v1``, data, never a grant): targets naming a
drive by id or a site by name (its default library, resolved at run time on the
configured ``SHAREPOINT_SITE_ID`` host), the folders to walk, and the walk bounds
(``max_depth``, ``max_folders``) plus ``max_content_bytes``: 20 MB unless the scope
declares more, at most the guard's one-download bound (``live_source_guard
.MAX_CONTENT_BYTES``, 100 MB; a larger declaration is refused). The capture principal
still needs the live-source audience for every folder and item it reads.

Discovery (``discover``, :class:`DiscoveryRule`; ``integrations/sharepoint_estate_scope.json``).
Instead of (or beside) named targets, a scope may capture the estate: every site the
application may read (``graph_read.SharePointSites``: ``/sites/getAllSites`` paged,
then each site's ``/drives`` paged) on the configured host and on its tenant's OneDrive
host (``<tenant>-my.sharepoint.com``, personal sites; :func:`estate_hosts`, the two
download hosts the guard admits), each document library walked from ``/`` like a named
drive (same store, change feed, item permissions and per-reader gates: a personal
drive's file too is re-gated per reader on its path keys and the item permissions read
with it). On the OneDrive host a drive Graph types ``business`` (a user's OneDrive for
Business) is walked as well as a ``documentLibrary``; on the configured host only a
``documentLibrary`` is. A site on any other host is left out and counted
(``sites_other_host``; ``sites_personal`` counts the OneDrive-host sites and
``libraries_personal_planned`` the libraries planned on them). A personal site whose
libraries SharePoint refuses as locked (HTTP 423, a OneDrive held for a removed user,
readable by nobody) is counted (``sites_locked``), never planned and not a gap; a
locked site on the configured host is a gap like any listing failure. The ``exclude`` list (data) names
sites and libraries left out; a library an explicit target already names is left to
that target. A site listing that
fails or is cut, a site whose libraries cannot be listed and a dropped id are gaps of
the run (``discovery.gaps``, each an incomplete bound); a library the capture actor
may not read is planned and its root ends incomplete with the walk's reason. A
library-wide root takes over a narrower root of the same drive captured before:
items it lists move to it and, once its walk listed everything, the narrower root's
walk state is retired (:meth:`SharePointCaptureStore.retire_nested_roots`).

Library classes (``library_security_classes``, :class:`LibraryClass`). The Portal reads
its files with an app-only token, so a restricted library's own read list restricts
nothing for the Workbench either: the employee gate decides on each file's security
class (``operating.doc_metadata_taxonomy.file_security_class``). A scope records the
class of a library beside its targets (by drive id, or by library name on a site or
on every site), and the class is kept per drive (``drive_classes``) and served with
every captured version (``row.library_security_class``). Before a library is walked
its display name is known (discovery lists it; a named target reads it GET-only,
``SharePointReader.library``): a library whose name says it is restricted
(``taxonomy.known_library_class``: Finance, Business Central, Staff Scratch) and
that the scope does not classify is refused, never walked (``restricted_library_
unclassified``, an incomplete bound), and what was captured of it before is served
to no employee (class UNRESOLVED). A named target whose name cannot be read and that
the scope does not classify by drive id is refused for the run.

Columns (``list_item_columns``). A scope may read the Portal-taxonomy columns
(``"portal_taxonomy"``: exactly ``taxonomy.COLUMNS``, the one column list, never repeated
per scope; a list naming a column outside it is refused) for every new version through the
file's own listItem (``SharePointReader.list_item_fields``, GET only), kept on the version
as ``list_item_fields`` (``sharepoint-listitem-fields/v1``, bound to the version's eTag,
with the ``field_names`` requested). A read that fails is counted
(``list_items_unavailable``); the version is then classified by its library alone. A
version's resolved row carries ``portal_metadata`` (``taxonomy.stated_metadata``: what the
Portal states, cited to the listItem; absent and empty columns typed gaps). Nothing is
ever written to SharePoint. Every read (or failed read) stamps the version with the
reader's column-set version (``list_item_columns_version``: ``column_set_version``, the
reader version and the columns it requests). A current version stamped with another one
(read before a reader change, or before any stamp) is re-read once, even when its
listItem is unchanged: after each walk or follow of its root, as long as the run's time
budget lasts (``list_items_reread``), as a new interval of the same version (the earlier
one keeps what was read then; event ``columns_reread``). This holds for every column, not
one in particular.

Reads (all GET, through ``integrations.graph_read.SharePointReader`` and so the one
``live_source_guard`` client): each folder is listed by item id after its canonical
path passed the audience; item permissions are read for every listed item
(``read_permissions``) and kept on the version; a new or changed version's bytes are
downloaded once (bounded, checked against the item size and the source ``sha256Hash``
or ``quickXorHash`` Graph publishes, pinned by SHA-256, content-addressed blobs) and its
text is extracted by the deterministic ``integrations.document_understanding`` path (no
model) for the mention extractor. Bytes over that extractor's 20 MB bound (a scope that
declares a larger cap) are pinned but never handed to it whole (``text_status``
``exceeds_bound``): a container among them (a zip, ``.msg``, ``.eml``) is opened as a
container wherever it is read (``integrations.captured_text.container_members``), each
member within its own bound.

Text (``text_status``; the one text path of captured files,
``integrations.captured_text``). A PDF's text is kept page by page (pages joined by
``ontology.mentions.PAGE_BREAK``), so a mention cites its page
(``sharepoint://...@<eTag>#page=<n>`` resolves to that page's text). A PDF page
without a text layer (a scan) is OCR-read locally (poppler + tesseract, or the
macOS helper; never a network service): ``extracted_ocr``. OCR text is untrusted
source text; only the deterministic identifier grammar turns it into values, and
readers label it unconfirmed. Without a local OCR engine such a file is
``ocr_unavailable`` (counted, and a gap wherever it is served), never silently
textless; a later walk on a host with OCR re-reads its captured bytes (no source
read), as it does for a scanned PDF captured before OCR existed (``no_text``).

Versions (R8). Keyed by ``(driveId, itemId)``; one row per validity interval of a
version, cited ``sharepoint://<drive>/<item>@<eTag>`` (``graph_read.drive_citation``;
``#sha256=<hex>`` names the pinned bytes and resolves to the same row). A new eTag
supersedes the current version (both kept). A version listed at another path with
the same eTag (a move) opens a new interval at the new path, so it is re-gated on
the keys of where it is now. A walk of a root that listed everything (no cap, no
withheld or dropped child, no folder error) reconciles at its end: a current version
under that root the walk did not see is gone from its folder. Each is read once
more by item id (GET, bounded): still under the root (moved during the walk into a
folder the walk had listed already) is captured where it is now, a move, never
removed (re-gated on the keys and item permissions of where it is now at once: a run
whose budget runs out before that capture leaves the root ``incomplete``,
``time_budget``); elsewhere in the drive is ``moved_out_of_scope``, and its
history is re-gated on the keys and item permissions of where it is now; not
readable (deleted, moved where the capture may not look, or the source failing) is
``absent_after_resync`` and its history is hidden from every reader, so no stale
name or path is served from either. Folders of a scope never nest: a nested root
would split one item's attribution between two walks.

Incremental (``full_walk_hours``; decision 2026-10-12-capture-cursor-convergence). A
full walk records the folders it listed (by item id) and, before it starts, the drive's
change-feed cursor (``SharePointReader.drive_delta``, ``token=latest``; read once per
drive and run, and taken by the walk itself when that read failed). Each root keeps its
own feed position (``root_cursors``: the cursor and when it was read): a walk that ends
sets it to the cursor read before its first run, a follow that applied every entry to
the run's new cursor, and a deferred root or a cut follow keeps it. A later run of a
root whose last full walk was complete (or lacked only file bytes) and ended less than
``full_walk_hours`` ago follows the feed: per drive and run one read, from the oldest
position of its following roots, applied to each root whose walked folder is an entry's
parent. An entry whose eTag is the version captured there (not re-shared) is applied
already and read no further, so a root ahead of the oldest reads nothing twice and a cut
follow resumes cheaply, its root still complete. A new or changed file is read again by
item id (``get_item``: canonical path, audience, item permissions) and captured like a
listed one. A change the feed cannot settle (a deleted or moved-out file, a renamed,
moved, deleted or re-shared walked folder, a new folder under a walked one: a folder moved
in is listed without its descendants, so only a walk sees what it holds; an item the
capture actor may not read) turns that root's run into a full walk, which reconciles. A
feed that fails (HTTP 410 for a stale position, or unreadable) makes every root that
followed it walk (``feed_unavailable``; one deferred is made due, ``expire_walk``);
the round is then ``full`` with the reason recorded, never silently skipped. A legacy
drive cursor seeds the position of every walked root of its drive (read time unknown:
the oldest). The full walk every ``full_walk_hours`` also re-reads every item's
permissions (the feed reports sharing changes only where Graph honours ``Prefer:
deltashowsharingchanges``).

Permission re-reads (``permission_reread_hours``, directive 1cd1c66dd47d). The eTag
shortcut skips ``get_item``, which is also the item's permission re-read, so a
permission-only change the feed lists without the sharing annotation would stay stale.
Each root therefore keeps a permission position too (``root_permissions``): a feed
cursor and when it was read, such that every item permission change the feed listed
before it has been re-read. A walk that ends listing everything sets it to its feed
position; a follow that re-read every entry and applied all of them moves it to the
run's new cursor; a follow that used the shortcut leaves it. While it is younger than
``permission_reread_hours`` the shortcut applies. Once it is older, the drive's feed is
read from it (or from an older position) and the root re-reads every file entry by item
id, the eTag-equal ones too (``permissions_reread``), after the changed ones. A re-read
the time budget cuts resumes: its later runs pass over an entry whose permissions were
read at or after its first run's feed read, and when it ends the permission position is
that first run's cursor (a change after it is replayed by the next re-read). So a grant
revoked without the annotation is stale at most that bound plus the runs the re-read
takes (k + 1 for k budgets of entries, like a cut walk). A root with a feed
position but no permission position (a store from before it) walks
(``permissions_unknown``). A change Graph never lists in the feed waits for the full
walk.

Resumable walks (``walk_positions``). A full walk the run's time budget cuts keeps its
position: the folders it has left by item id (breadth first, the cut one first with its
listing's resume point, the page and position of the first child not read, or with the
ids of the children it already holds, so they are not read again; a folder renamed or
moved within the root meanwhile is listed where it is now, one moved out of the root is
left to the reconcile), the gaps it met so far, when its first run started and the
drive's change-feed cursor read before it. The root's next run resumes there
(``full_walk_reason`` ``resumed``), so a root larger than one budget completes over
several runs; until then it is reported partial (reason ``time_budget``, ``cursor``
``walk_position``, ``walk_position`` counts). Every child is captured as soon as its
permissions are read (never without them). A folder of more children than one listing
holds (``LIST_MAX_ITEMS``) is listed on from that resume point, never cut. The walk that
ends is complete only if no run of it met a gap, and only then reconciles, against its
first run's start (what any of its runs saw is not gone). Its feed position is then the cursor read before its
first run, so its next follow carries what changed meanwhile in the folders it walked
in earlier runs, and its next full walk is due ``full_walk_hours`` after it ended.

Refresh walks. The full walk a complete root (or one lacking only bytes) is due again
after ``full_walk_hours`` (``walk_due``) is a refresh: the root keeps its state while it
runs. A refresh the time budget alone cuts keeps its position like any cut walk
(``refresh`` in it), the root stays complete (``deferred``; ``estate.roots_refresh_cut``)
and stands with the cut walks, by when it started, ahead of the roots no walk has
reached: until it ends the root follows no feed, so a permission revoked or a file
deleted since its last full walk is applied only when the refresh ends, never after a
growing estate's new roots. A refresh that meets any other gap, ends, or was due for
another reason (a stale or unknown feed or permission position) records its outcome like
any walk. Until it ends the root follows no feed (its walked folders are being rebuilt):
what changed since its last full walk waits for the refresh, as it did for a cut walk.
So complete never reads as fresh: every root carries the age of its last full walk that
listed everything (:meth:`SharePointCaptureStore.full_walk_age`: ``last_full_walk_at``,
``age_hours``, ``refresh_open``), on each root of ``completeness`` and on every answer
that cites a version of it (``resolve`` ``root_full_walk``); the CLI counts the planned
roots by age band (``estate.full_walk_age``: ``fresh`` under ``full_walk_hours``,
``due`` under ``STALE_HOURS``, ``stale``, ``never``) and gives the oldest age in hours.

Scheduling (``capture_mail.schedule_units``, the one rule shared with the mail capture).
A run takes first the roots with a cut walk (a cut refresh too), the one started earliest
first (so a root larger than one budget resumes every run and completes), then other
incomplete roots,
never started ones among them, in the scope's order, then complete roots (each following
its drive's change feed) the oldest position first, then roots with a terminal reason
(``not_visible``, ``source_error:http_403``). Under a time budget every walk but a refresh
gets a fair slice of it: the budget left over the walks not reached yet (a complete
root its change feed carries is no walk). The first pass spends at most
``FIRST_WALK_PASS_SHARE`` of the budget left when it begins (only the walk a run cut
that started earliest resumes with at least ``ROOT_SLICE_MIN_SECONDS``, so a root larger
than one budget completes within bounded runs; this resume-with-floor path cannot be
checked live until later_ask live-host-persistent-state-dir lands, since every live run
starts from fresh state, and only unit tests cover it): each walk's slice is its
share of the budget left above that reserve, with no floor (a 20 s floor gave each
first large root of a 224-root estate three times its share and left 78 roots after
them unreached, live 2026-10-18T09:46Z at dabe69ce3), so what a small root leaves goes
to the walks after it and the first pass reaches every root. A walk its slice cuts
keeps its position like any cut walk. A slice cuts its walk only once that slice moved it
on (read a child or ended a folder), so a slice shorter than one listing does not cut the
walk where the last one did, every pass and every run; it cuts inside a listing (the
resumed walk lists the folder again and skips the children it holds), never only between
folders (one listing's files ran 224 s past its slice live, b38701bd1 at
2026-10-18T10:41Z). Once every root had its slice, the budget left
(the reserve, so roots each a little larger than their share are not all cut and none
completed) goes to the walks slices cut, the one with the fewest folders left to list
first (the nearest its end as far as the walk knows), each at least
``ROOT_SLICE_MIN_SECONDS`` (every root was visited by then), pass after pass (at most
``ROOT_PASSES_MAX``), each resuming from its stored
position (one root result: the counts of every slice, ``walk_slices``). So one root larger
than the budget no longer holds every root after it unvisited: a growing estate's
``roots_unvisited`` falls every run while the large root resumes. A refresh is never
sliced (it resumes ahead of the roots no walk has reached, decision 28e628682928). A
root the time budget reaches before
reading anything is deferred (``deferred``): nothing of it is read or written, so a
complete root stays complete with its walked folders and feed position; a terminal
reason stays, and any other root is cut (``time_budget``). A target and a run are
complete only when no root was deferred; the estate report types every root the
budget cut by root class (``estate.roots_budget_cut_by``: ``unreached``, ``start_cut``,
``walk_cut``, ``finish_cut``; :func:`budget_cut_causes`). A transient download failure (``URLError``, a
timeout, a body cut short) is retried once while the budget lasts
(``SharePointReader.get_content`` ``may_retry``); a second failure leaves the file
``unavailable:<detail>``, counted (``contents_unavailable``) and retried by a later run.

Named items first (``named_items``; ``--named-originals``: every item a case original
under ``<state-dir>/case-originals`` names, ``operating.captured_originals
.sharepoint_items``). A cold walk of the estate takes days, and a captured original is
served only through its item's current captured version
(``operating.captured_originals``), so a run first captures the named items, before any
root, within ``NAMED_PASS_SHARE`` of the budget left (:meth:`SharePointCapture
._named_pass`). Only an item of a drive the run planned is read (the scope, discovery's
site and library exclusions, library classes and the people exclusion already decided
which drives those are: an excluded person's OneDrive is never planned, so none of its
items is read); any other is ``out_of_scope``, with no GET. A planned one is read by id
(``get_item``: metadata and item permissions, as the change feed reads a changed file;
children never), and only when its path lies under one of its drive's planned roots is
it recorded, by the walk's own per-file step (:meth:`_capture_file`: the same version
row, item permissions, bytes, text and listItem columns); else ``outside_root``, nothing
written. An item held as current and seen within ``permission_reread_hours`` is not
read again, so the pass is idempotent and a cut pass resumes with the items not held
first. No root status, walk position, walked folder or feed position is touched: the
walk that later lists such an item finds its version captured (``unchanged``). Every
named item ends in one typed outcome (``named_items.outcomes``: ``captured``,
``unchanged``, ``held_fresh``, ``out_of_scope``, ``outside_root``, ``not_visible``,
``not_found``, ``not_file``, ``source_error:<detail>``, ``time_budget``,
``store_full``, ``invalid``, ``not_recorded``, ``over_bound``); the failures by item stay
on the capture (``named_failures``), never printed.

Complete means every listed item reached a terminal outcome. A file whose bytes are
captured is one; so is a file over ``max_content_bytes`` (``exceeds_cap``): its
metadata, size, item permissions and version are captured, its content is skipped on
purpose and recorded as such (counted apart as ``content_exceeds_cap``, labelled
wherever it is served), so it does not leave the root incomplete. A metadata-only capture
(``read_content=False``; ``operating.file_clearance_check``, which reads only metadata,
permissions and eTags) sends no content GET and records each new file's content
``skipped_metadata_only`` (counted ``contents_skipped``): never captured bytes. Its root
ends ``incomplete`` with reason ``content_not_read`` (``completeness``: not
``complete``, ``metadata_complete`` only; ``content_skipped`` per root), is never stamped
walked nor pruned, and a later content capture of the same store reads the skipped bytes
(``degraded``). Incomplete, never
absent: a listing cut at a cap, a child withheld from the capture actor, a folder or
depth bound, a source error, and every current file whose bytes could not be read
(``unavailable:<detail>``, retried by later runs) keep the root ``incomplete`` with the
reason. Such a file is still a captured version (its metadata), never dropped. A root
whose only gap is such bytes (``content_incomplete``, listed to the end) is an incomplete
source: the estate report names it apart from walk gaps (``estate.roots_incomplete_source``,
``estate.contents_unavailable`` by detail; every root reason in ``estate.root_gaps``).

Storage: ``<state-dir>/sharepoint-capture/`` (SQLite + ``blobs/sha256/``), private
modes; the state directory must lie outside every git work tree. The store is bounded
(``max_store_bytes``, default ``MAX_STORE_BYTES``; ``--max-store-bytes``): a blob past it
ends its root ``store_full``. Each root keeps the bytes of its last complete capture
only: once a full walk captured everything, the bytes and text of every version under
it superseded before that walk started are dropped (``pruned``; a version superseded
during it keeps its bytes until the next complete walk,
:meth:`SharePointCaptureStore.prune_superseded`, ``contents_pruned``); the version's
metadata, permissions, digest and interval stay. Compact storage (live 2026-10-27: the
store held 4 GB, mostly originals kept after their text was read), as the mail store's
(``integrations.capture_mail``, Storage): texts are kept zlib-compressed; an original over
``DROP_ORIGINALS_ABOVE`` (256 KiB) is dropped once its text is kept and every reading a
reader makes of it is kept in its ``.dropped`` form with its quickXorHash
(:meth:`SharePointCaptureStore.settle_original`), so resolve, the typed fields and the
mention documents read the same text; its version keeps its SHA-256 pin, item id and
eTag, the refetch handle (:meth:`SharePointCaptureStore.refetch_original`: one guarded
GET, returned only when it hashes to the pin, never stored). Kept: small originals,
containers, bytes that are their own text, and originals a pending OCR still needs.
``python3 -m integrations.capture_sharepoint compact --state-dir D [--defer-ocr]``
compacts a store captured before (holding the host's cycle lock); ``--defer-ocr`` drops
pending scans too (``ocr_deferred``: the text layer is the reading, a named gap), and a
later walk on a host with OCR re-fetches each one, OCR-reads it and drops it again.
Without ``--state-dir``
the CLI keeps its store in the host's state directory (:func:`default_state_dir`:
``$PLX_WORKBENCH_STATE_DIR/sharepoint-capture-check`` when set, else under
``$XDG_STATE_HOME`` or ``$HOME/.local/state``; ``storage.state_base`` says which; the
pattern of ``capture_teams`` and ``eval.heldout``), so the estate check's resumable walks
carry on into its next daily run (``storage.host_state``, ``storage.carried``) where that
directory persists.

Scope not finished (``incomplete_scope``, the CLI over a scope of fixed targets). A root
the last run left incomplete is listed by target id and root ordinal with its typed
reasons, never counted complete (``ok`` stays false). ``resumable``: the time budget cut
it, so the next run carries on from its stored walk position. A fresh ``--state-dir``
(the verifier's ``{tmp}``) is gone when the check ends, so a root larger than one budget
can never finish there: ``--state-name NAME`` keeps the store as
``<host state base>/sharepoint-capture-check-NAME/live`` (``state_base``
``workbench_state_dir`` is kept between the verifier's daily runs), and the cut root
resumes in the next daily run. Kept is observed, never inferred from the base: an
entry is ``persisted`` only when this invocation found the store a previous one left
(``storage.carried``). Where it did not, a run cut by the time budget alone names
``later_ask`` live-host-persistent-state-dir and ``blocked_upstream`` carries one part
per cut root (or discovery gap), the verifier's blocked with the progress shown; a cut
over a carried store, or any other reason, stays a typed fail (one rule for every
capture: ``capture_mail.budget_cut_block``). The count of roots never hides:
``summary.roots_complete`` stays below ``summary.roots``.

Estate progress (``estate.progress``, the CLI over an estate scope). Before its runs and
after them it reads the store: ``roots_complete`` (``summary.roots_complete``) must not
fall (``roots_complete_not_falling``), and ``roots_unvisited``, the roots the last run
planned that no walk has reached (none ended, none holds a position), must fall until
none is left (``roots_unvisited_falling``).

CLI (credentialed live check; prints counts, booleans and enums only, never names,
paths, ids or content)::

    python3 -m integrations.graph_read sharepoint-capture --state-dir D \\
        --scope integrations/sharepoint_capture_scope.json \\
        --live-audience examples/sharepoint_capture_audience.live-check.json --runs 2

With ``--registry ontology/source_registry.json`` an estate run also names every site the
registry's ``sharepoint`` ``site`` entries name, by outcome, with the typed reason of each
one not captured (``registry_sites``; names already committed there), so the source
registry can credit them (:func:`registry_site_outcomes`).
``registry_sites.readback`` adds, per site, counts and a reason code only
(:func:`registry_site_readback`): libraries planned, items listed, items read, items
withheld and, when none was read, ``no_access``, ``empty_site``, ``not_in_scope``,
``not_walked`` or ``source_error``; a site walked whole that holds no file is
``unexercised`` (:func:`credit_readback`), never ``captured``.

Live-unverified until ``devloop/live-checks/capture.sharepoint.json`` passes; the estate
(``--scope integrations/sharepoint_estate_scope.json``) until
``devloop/live-checks/capture.sharepoint_estate.json`` passes.
"""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from ontology.mentions import cited_page, is_paged_file
from ontology.types import Actor

from operating import doc_metadata_taxonomy as taxonomy

from . import live_source_grammar as grammar
from . import people_exclusion
from .capture_mail import (COMPLETE, DROP_ORIGINALS_ABOVE, FRONTIER, PENDING,
                           PERSISTENT_STATE_ASK, TERMINAL, CaptureError, MailCaptureStore,
                           assert_outside_git, budget_cut_block, check_state_dir,
                           schedule_units, terminal_reason)
from .captured_text import (COMPRESSED_SUFFIX, DROPPED_SUFFIX, OCR_DEFERRED,  # noqa: F401
                            OCR_FAILED, OCR_NO_TEXT, OCR_UNAVAILABLE, TEXT_EXTRACTED,
                            TEXT_OCR, TEXT_READ, TEXT_UNREAD, UNTYPED_OCR_FAILURES, blob_path,
                            capture_text, captured_text, file_type, inflate, member_view,
                            ocr_digest, read_blob, read_dropped, reading_key, readings_of,
                            split_member, write_dropped)
from .capture_store import (SourceKind, register_source_kind, sqlite_bytes,
                            checkpoint, wal_journal)
from .live_source_guard import MAX_CONTENT_BYTES as MAX_DOWNLOAD_BYTES
from .live_source_guard import (LiveSourceAudience, NotVisible, SourceNotFound,
                                SourceUnavailable, parse_handle, q, tenant_download_hosts,
                                unavailable_detail)

SCOPE_SCHEMA = "plx-sharepoint-capture-scope/v1"
STORE_DIR = "sharepoint-capture"
DB_FILE = "capture.db"
LINEAGE = "sharepoint"
SCHEME = "sharepoint"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCOPE = REPO_ROOT / "integrations" / "sharepoint_capture_scope.json"
FULL_WALK_HOURS = 24
PERMISSION_REREAD_HOURS = 24   # the default bound on a root's skipped permission re-reads
MAX_CONTENT_BYTES = 20_000_000   # a scope's default content cap: the document extractor's bound
# The store's default bound (SQLite file plus blobs; ``SharePointCaptureStore``, the CLI's
# ``--max-store-bytes``): a blob past it ends its root ``store_full``.
MAX_STORE_BYTES = 50_000_000_000
STALE_HOURS = 7 * 24   # a root's last full walk older than this reads ``stale`` (CLI)
# The largest content cap a scope may declare: the guard's one-download bound.
MAX_DECLARED_CONTENT_BYTES = MAX_DOWNLOAD_BYTES
LIST_PAGE_SIZE = 200
LIST_MAX_PAGES = 50
LIST_MAX_ITEMS = 5000
# SharePointReader bound per listing: every item a listing may hold, so each listed item's
# permissions are read (a lower bound left items of a large folder without them and the
# root incomplete, ``acl_read_bound``); the run's time budget bounds the walk instead,
# asked before every item's permission read (``SharePointReader.list_folder`` ``stop``).
MAX_ACL_READS = LIST_MAX_ITEMS
# The CLI's offline read-back bound (``SharePointCaptureStore.index``): an index cut there
# is ``resolve_check.index_truncated`` and the check is never ok over it.
INDEX_LIMIT = 100_000
REPAIRS_PER_ROOT = 100
# Worker threads that extract the text (OCR included) of a walk's new files while the walk
# reads the next ones (:class:`_Extraction`); 1: inline. Every source read stays on the
# walking thread, one request at a time, through the one guarded client.
EXTRACT_WORKERS = max(1, min(4, (os.cpu_count() or 1) - 1))
# OCR never runs past the run's time budget (live 2026-10-27: a run whose walk was cut
# while three scans were being OCR-read waited for them, past the host's grace, was
# killed, and reported nothing). A file's OCR starts only while at least this much of the
# budget is left (the OCR's own whole-document deadline and a margin); later in the run
# its text layer is recorded at once (``ocr_pending``, bytes pinned) and its scanned pages
# are OCR-read from the captured bytes by a later walk of its root (:meth:`_reextract`).
OCR_RESERVE_SECONDS = 330.0
# The most of the budget left a walk spends first on those pending OCR reads.
REEXTRACT_SHARE = 0.25
# The longest Retry-After a run without a time budget waits once (:meth:`_throttled`);
# a budgeted run waits only what ends before its root's slice and its deadline.
RETRY_AFTER_MAX_SECONDS = 120
FIRST_PASS_SHARE = 0.5   # the most of the budget left a run's retry-first pass spends
# Named items (module docstring, Named items first): the most of the budget left the pass
# spends, the most items one run names, and the outcomes it types per item.
NAMED_PASS_SHARE = 0.5
MAX_NAMED_ITEMS = 20_000
NAMED_OUTCOMES = ("captured", "unchanged", "held_fresh", "out_of_scope", "outside_root",
                  "invalid", "not_visible", "not_found", "not_file", "time_budget",
                  "store_full")
# The most of the walk budget (what is left when the first walk starts) the first pass
# over every root spends (module docstring, Scheduling): the rest resumes the walks its
# slices cut.
FIRST_WALK_PASS_SHARE = 0.5
# A resumed walk's least slice on the run's later passes (every root visited by then).
ROOT_SLICE_MIN_SECONDS = 20.0
# The run's passes over the roots its slices cut (the first pass over every root aside).
ROOT_PASSES_MAX = 16
# The listItem reader's version: raise it when the way a column is read or kept changes,
# so every version read before is re-read once (``column_set_version``).
LIST_ITEM_READER_VERSION = 1
TIME_BUDGET = "time_budget"      # a root left when the run's time budget was spent
EXPIRED_WALK = "0001-01-01T00:00:00.000000Z"   # a last full walk that is due again at once
# A root whose walk listed everything but some current files' bytes the source did not
# serve (``unavailable:<detail>``, retried every run): an incomplete source, not a walk gap.
CONTENT_INCOMPLETE = "content_incomplete"
INCOMPLETE_SOURCE = frozenset({CONTENT_INCOMPLETE})
CAPTURED = "captured"
EXCEEDS_CAP = "exceeds_cap"
# A file a metadata-only capture (``SharePointCapture(read_content=False)``) recorded
# without reading its bytes: terminal for that capture, a gap a content capture fills.
CONTENT_SKIPPED = "skipped_metadata_only"
# A root a metadata-only capture listed to the end whose current files' content it skipped:
# never content-complete (``completeness``), never pruned or stamped walked.
CONTENT_NOT_READ = "content_not_read"
PRUNED = "pruned"   # bytes and text of a version superseded before its root's last full walk
# Removal reasons of a version gone from the scope (reconcile_root).
ABSENT = "absent_after_resync"
MOVED_OUT = "moved_out_of_scope"
_UNAVAILABLE = "unavailable:"
# A site's library listing answered HTTP 423 Locked (``unavailable_detail``).
LOCKED_DETAILS = frozenset({"http_423", "retried.http_423"})
_TARGET_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Fields of a listed drive-item record kept on a version (graph_read.SharePointReader
# ._record); the source ACL and resource keys are kept beside it, never in it.
RECORD_FIELDS = ("drive_id", "id", "etag", "ctag", "path", "name", "size", "mime_type",
                 "source_hashes", "created_at", "last_modified", "last_modified_by",
                 "original_url")


def _drive_path(parts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parts, (str, bytes)) or len(parts) != 2 \
            or not all(isinstance(p, str) for p in parts):
        raise ValueError("drive_path_invalid")
    if not grammar.TYPES["drive_id"].valid(parts[0]) or not grammar.TYPES["item_id"].valid(
            parts[1]):
        raise ValueError("drive_path_invalid")
    return parts[0], parts[1]


# The SharePoint source kind in the capture registry (integrations/capture_store.py):
# ``sharepoint://<drive id>/<item id>``, audience unit ``sharepoint:<drive>`` (the C0
# audience_key). Like mail it keeps its own versioned store; readers are re-gated per
# item on the path keys and item permissions recorded at capture, never on the drive
# resource alone.
SHAREPOINT_KIND = register_source_kind(SourceKind(
    name="sharepoint", scheme=SCHEME, path_parts=2, canonical_path=_drive_path,
    unit=lambda path: f"sharepoint:{path[0]}", resources=lambda path: (f"sharepoint:{path[0]}:/",),
    key=lambda path: {"drive_id": path[0], "id": path[1]}))


# The drive types an estate discovery walks on the OneDrive host (Graph: a user's OneDrive
# for Business drive is "business"; a library added to it is "documentLibrary").
PERSONAL_DRIVE_TYPES = ("business", "documentLibrary")


# The hosts whose sites an estate discovery walks: the configured SharePoint host and its
# tenant's OneDrive (``-my``) host, the two download hosts the guard admits; the guard's one
# derivation (``GuardedTransport.download_hosts`` uses it too), never a second copy.
estate_hosts = tenant_download_hosts


def store_exists(state_dir: str | Path) -> bool:
    """Whether a SharePoint capture store exists under ``state_dir`` (nothing is created)."""
    return (Path(state_dir).expanduser() / STORE_DIR / DB_FILE).is_file()


def version_handle(drive_id: str, item_id: str, etag: str) -> str:
    """``sharepoint://<drive>/<item>@<eTag>`` (``graph_read.drive_citation`` spelling)."""
    return f"{SCHEME}://{q(drive_id)}/{q(item_id)}@{q(etag)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    """Fixed-width UTC text, so recorded/superseded times compare as strings."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _keys(drive_id: str, path: str, is_folder: bool) -> list[str]:
    """Every audience key an item answers to (``SharePointReader.resources``)."""
    keys = [f"sharepoint:{drive_id}:{path}"]
    if is_folder and path != "/":
        keys.append(f"sharepoint:{drive_id}:{path.rstrip('/')}/")
    return keys


def _under(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(root.rstrip("/") + "/")


def _in_root(path: str, root: str) -> bool:
    """Whether a listed path lies under a scope root (SharePoint paths are
    case-insensitive; the scope may spell the root in another case)."""
    return _under(path.casefold(), root.casefold())


def _page_number(value: Any) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,3}", value):
        return int(value)
    return None


# ------------------------------------------------------------------ scope

class CaptureTarget:
    def __init__(self, entry: Mapping[str, Any]) -> None:
        allowed = {"id", "drive_id", "site", "folders", "max_depth", "max_folders", "note"}
        if not isinstance(entry, Mapping) or set(entry) - allowed:
            raise CaptureError("scope_target_invalid")
        self.id = entry.get("id")
        if not isinstance(self.id, str) or _TARGET_ID.fullmatch(self.id) is None:
            raise CaptureError("scope_target_invalid")
        drive, site = entry.get("drive_id"), entry.get("site")
        if (drive is None) == (site is None):
            raise CaptureError("scope_target_drive_or_site")
        if drive is not None and not grammar.TYPES["drive_id"].valid(drive):
            raise CaptureError("scope_drive_invalid")
        if site is not None and not grammar.TYPES["site_name"].valid(site):
            raise CaptureError("scope_site_invalid")
        self.drive_id, self.site = drive, site
        folders = entry.get("folders", ["/"])
        if isinstance(folders, (str, bytes)) or not isinstance(folders, Sequence) \
                or not folders or len(folders) > 50:
            raise CaptureError("scope_folders_invalid")
        roots: list[str] = []
        for folder in folders:
            if not isinstance(folder, str) or not (
                    folder == "/" or grammar.TYPES["drive_path"].valid(folder)):
                raise CaptureError("scope_folders_invalid")
            if folder in roots:
                raise CaptureError("scope_folders_invalid")
            roots.append(folder)
        if any(a != b and _under(b, a) for a in roots for b in roots):
            raise CaptureError("scope_folders_nested")
        self.folders = tuple(roots)
        self.max_depth = self._bound(entry.get("max_depth", 3), 0, 20)
        self.max_folders = self._bound(entry.get("max_folders", 200), 1, 5000)

    @staticmethod
    def _bound(value: Any, low: int, high: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise CaptureError("scope_bound_invalid")
        return value


def _library_name_ok(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 128 and value == value.strip()
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


class DiscoveryRule:
    """The estate discovery of a scope (``discover``): every document library of every
    site the application may read on the configured host, each walked from ``/`` with
    the given bounds, except what ``exclude`` names. An exclusion is data: ``site`` (a
    site URL name: the whole site, its libraries never listed), ``library`` (a library
    name on every site) or both (that library of that site), matched case-insensitively."""

    def __init__(self, entry: Mapping[str, Any]) -> None:
        allowed = {"max_sites", "max_libraries_per_site", "max_depth", "max_folders",
                   "exclude", "note"}
        if not isinstance(entry, Mapping) or set(entry) - allowed:
            raise CaptureError("scope_discover_invalid")
        bound = CaptureTarget._bound
        self.max_sites = bound(entry.get("max_sites", 500), 1, 5000)
        self.max_libraries = bound(entry.get("max_libraries_per_site", 50), 1, 500)
        self.max_depth = bound(entry.get("max_depth", 8), 0, 20)
        self.max_folders = bound(entry.get("max_folders", 5000), 1, 5000)
        rules = entry.get("exclude", [])
        if isinstance(rules, (str, bytes)) or not isinstance(rules, Sequence) or len(rules) > 200:
            raise CaptureError("scope_exclude_invalid")
        self.exclude: list[tuple[str | None, str | None]] = []
        for rule in rules:
            if (not isinstance(rule, Mapping) or set(rule) - {"site", "library", "note"}
                    or not ({"site", "library"} & set(rule))
                    or ("site" in rule and not grammar.TYPES["site_name"].valid(rule["site"]))
                    or ("library" in rule and not _library_name_ok(rule["library"]))):
                raise CaptureError("scope_exclude_invalid")
            self.exclude.append((rule["site"].casefold() if "site" in rule else None,
                                 rule["library"].casefold() if "library" in rule else None))

    def site_excluded(self, name: str | None) -> bool:
        site = (name or "").casefold()
        return any(s == site and lib is None for s, lib in self.exclude)

    def library_excluded(self, site_name: str | None, library: str | None) -> bool:
        site, name = (site_name or "").casefold(), (library or "").casefold()
        return any(lib == name and s in (None, site) for s, lib in self.exclude
                   if lib is not None)


class LibraryClass:
    """One ``library_security_classes`` entry: the security class of the library a
    ``drive_id`` names, or of the library called ``library`` (on ``site``, else on
    every site), matched case-insensitively."""

    def __init__(self, entry: Mapping[str, Any]) -> None:
        if (not isinstance(entry, Mapping)
                or set(entry) - {"drive_id", "site", "library", "class", "note"}
                or ("drive_id" in entry) == ("library" in entry)
                or ("site" in entry and "library" not in entry)
                or entry.get("class") not in taxonomy.SECURITY_CLASSES
                or ("drive_id" in entry and not grammar.TYPES["drive_id"].valid(entry["drive_id"]))
                or ("site" in entry and not grammar.TYPES["site_name"].valid(entry["site"]))
                or ("library" in entry and not _library_name_ok(entry["library"]))):
            raise CaptureError("scope_library_class_invalid")
        self.drive_id = entry.get("drive_id")
        self.site = entry["site"].casefold() if "site" in entry else None
        self.library = entry["library"].casefold() if "library" in entry else None
        self.security_class = entry["class"]


class CaptureScope:
    """Which drives/sites and folders are captured, how deep and how large, plus the
    optional estate discovery (:class:`DiscoveryRule`), the classes of its libraries
    (:class:`LibraryClass`) and the SharePoint columns kept per version. Configuration,
    never a grant: the capture actor still needs the live-source audience."""

    def __init__(self, targets: Sequence[Mapping[str, Any]], *,
                 max_content_bytes: int = MAX_CONTENT_BYTES,
                 full_walk_hours: int = FULL_WALK_HOURS,
                 permission_reread_hours: int = PERMISSION_REREAD_HOURS,
                 census_max_age_hours: int | None = None,
                 discover: Mapping[str, Any] | None = None,
                 library_security_classes: Sequence[Mapping[str, Any]] = (),
                 list_item_columns: Sequence[str] = ()) -> None:
        self.discover = None if discover is None else DiscoveryRule(discover)
        if isinstance(library_security_classes, (str, bytes)) or not isinstance(
                library_security_classes, Sequence) or len(library_security_classes) > 200:
            raise CaptureError("scope_library_class_invalid")
        self.library_classes = tuple(LibraryClass(e) for e in library_security_classes)
        if list_item_columns == taxonomy.CAPTURE_COLUMNS:
            list_item_columns = tuple(column["name"] for column in taxonomy.COLUMNS)
        if (isinstance(list_item_columns, (str, bytes))
                or not isinstance(list_item_columns, Sequence) or len(list_item_columns) > 20
                or not all(grammar.column_name_ok(c) for c in list_item_columns)
                or len(set(list_item_columns)) != len(list_item_columns)):
            raise CaptureError("scope_list_item_columns_invalid")
        if not set(list_item_columns) <= set(taxonomy.COLUMN_BY_NAME):
            # The Portal's column set is consumed, never forked (QA-06).
            raise CaptureError("scope_list_item_column_not_in_taxonomy")
        self.list_item_columns = tuple(list_item_columns)
        if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence) or (
                not targets and self.discover is None):
            raise CaptureError("scope_empty")
        self.targets = tuple(CaptureTarget(t) for t in targets)
        if len({t.id for t in self.targets}) != len(self.targets):
            raise CaptureError("scope_target_duplicate")
        if isinstance(max_content_bytes, bool) or not isinstance(max_content_bytes, int) \
                or not 1 <= max_content_bytes <= MAX_DECLARED_CONTENT_BYTES:
            raise CaptureError("scope_max_content_bytes_invalid")
        self.max_content_bytes = max_content_bytes
        self.full_walk_hours = CaptureTarget._bound(full_walk_hours, 1, 720)
        self.permission_reread_hours = CaptureTarget._bound(permission_reread_hours, 1, 720)
        # How long the host's coverage census may be carried across calls (its own window,
        # for a check that runs less often than the full walk); default full_walk_hours.
        self.census_max_age_hours = (
            self.full_walk_hours if census_max_age_hours is None
            else CaptureTarget._bound(census_max_age_hours, 1, 720))

    def library_class(self, drive_id: str, site: str | None, library: str | None
                      ) -> str | None:
        """The class the scope gives this library: by drive id, else by name on its
        site, else by name on every site; None when it gives none."""
        site_key, name = (site or "").casefold(), (library or "").casefold()
        for match in (lambda e: e.drive_id == drive_id,
                      lambda e: library is not None and e.library == name
                      and e.site is not None and e.site == site_key,
                      lambda e: library is not None and e.library == name and e.site is None):
            found = {e.security_class for e in self.library_classes if match(e)}
            if found:
                # Two classes for one library: nobody may read it.
                return found.pop() if len(found) == 1 else taxonomy.UNRESOLVED_CLASS
        return None

    @classmethod
    def from_json(cls, document: Any) -> "CaptureScope":
        allowed = {"schema", "note", "targets", "max_content_bytes", "full_walk_hours",
                   "permission_reread_hours", "census_max_age_hours", "discover",
                   "library_security_classes", "list_item_columns"}
        if (not isinstance(document, dict) or document.get("schema") != SCOPE_SCHEMA
                or set(document) - allowed or not isinstance(document.get("targets"), list)):
            raise CaptureError("scope_document_invalid")
        return cls(document["targets"],
                   max_content_bytes=document.get("max_content_bytes", MAX_CONTENT_BYTES),
                   full_walk_hours=document.get("full_walk_hours", FULL_WALK_HOURS),
                   permission_reread_hours=document.get("permission_reread_hours",
                                                        PERMISSION_REREAD_HOURS),
                   census_max_age_hours=document.get("census_max_age_hours"),
                   discover=document.get("discover"),
                   library_security_classes=document.get("library_security_classes", []),
                   list_item_columns=document.get("list_item_columns", []))

    @classmethod
    def from_file(cls, path: str | Path) -> "CaptureScope":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CaptureError("scope_unreadable") from None
        return cls.from_json(document)


# ------------------------------------------------------------------ store

STORE_SCHEMA_VERSION = "1"
_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roots (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    round_started_at TEXT,
    last_run_at TEXT,
    last_complete_at TEXT,
    rounds_complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (drive_id, root_path)
);
-- One row per validity interval of a drive-item version. A version that comes back
-- after a gap (removal, a move with the same eTag) gets a new row under the same
-- handle, so as_known_at intervals never overlap.
CREATE TABLE IF NOT EXISTS items (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    l1_handle TEXT NOT NULL,
    drive_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    etag TEXT NOT NULL,
    root_path TEXT NOT NULL,
    path TEXT NOT NULL,
    record TEXT NOT NULL CHECK (json_valid(record)),
    resources TEXT NOT NULL CHECK (json_valid(resources)),
    source_acl TEXT NOT NULL CHECK (json_valid(source_acl)),
    digest TEXT NOT NULL,
    content_status TEXT NOT NULL,
    content_sha256 TEXT REFERENCES blobs(sha256),
    text_status TEXT NOT NULL,
    text_sha256 TEXT REFERENCES blobs(sha256),
    recorded_at TEXT NOT NULL,
    superseded_at TEXT,
    removed_reason TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_by_id ON items(drive_id, item_id);
CREATE INDEX IF NOT EXISTS items_by_handle ON items(l1_handle);
CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_events (
    l1_handle TEXT NOT NULL,
    event TEXT NOT NULL,
    at TEXT NOT NULL
);
-- Legacy: one change-feed cursor per drive. It only seeds root_cursors (every root of
-- the drive that was walked) and is then deleted.
CREATE TABLE IF NOT EXISTS drive_cursors (
    drive_id TEXT PRIMARY KEY,
    delta_url TEXT NOT NULL,
    stored_at TEXT NOT NULL
);
-- Each root's position in its drive's change feed (graph_read.SharePointReader
-- .drive_delta): the root has applied every change before delta_url, read at read_at
-- (NULL: unknown, the earliest). A NULL delta_url is no position (the root walks).
CREATE TABLE IF NOT EXISTS root_cursors (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    delta_url TEXT,
    read_at TEXT,
    PRIMARY KEY (drive_id, root_path)
);
-- Each root's permission position: every item permission change its drive's change
-- feed listed before delta_url (read at read_at; NULL: unknown, the earliest) has been
-- re-read for the root. No row: unknown (the root walks once it follows the feed).
-- reread_url/reread_at: the feed cursor and read time of the first run of a re-read the
-- time budget cut (NULL: none open).
CREATE TABLE IF NOT EXISTS root_permissions (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    delta_url TEXT,
    read_at TEXT,
    reread_url TEXT,
    reread_at TEXT,
    PRIMARY KEY (drive_id, root_path)
);
-- The folders the last full walk of a root listed, by item id, so a change-feed entry
-- is attributed to the root whose walked folder is its parent. Rebuilt by every full
-- walk of the root; root_walks.walked_at is when its last full walk ended.
CREATE TABLE IF NOT EXISTS walked_folders (
    drive_id TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT,
    parent_id TEXT,
    depth INTEGER NOT NULL,
    PRIMARY KEY (drive_id, folder_id)
);
-- The security class the scope gave a library when it was last planned; UNRESOLVED
-- for a restricted library it did not classify (served to no employee).
CREATE TABLE IF NOT EXISTS drive_classes (
    drive_id TEXT PRIMARY KEY,
    security_class TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS root_walks (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    walked_at TEXT NOT NULL,
    PRIMARY KEY (drive_id, root_path)
);
-- The position of a full walk the time budget cut, resumed by the root's next run:
-- started_at is when its first run started, delta_url the drive's change-feed cursor
-- read before it, position the folders left (the cut one first, with the children it
-- already holds) and the walk's gaps so far.
CREATE TABLE IF NOT EXISTS walk_positions (
    drive_id TEXT NOT NULL,
    root_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    delta_url TEXT,
    position TEXT NOT NULL CHECK (json_valid(position)),
    saved_at TEXT NOT NULL,
    PRIMARY KEY (drive_id, root_path)
);
"""
_CONTENT_COLUMNS = ("content_status", "content_sha256", "text_status", "text_sha256")


class SharePointCaptureStore:
    """The capture store under ``<state_dir>/sharepoint-capture`` (module docstring).

    ``max_store_bytes`` bounds the store (SQLite file plus blobs; default
    ``MAX_STORE_BYTES``, None: unbounded): a blob that would pass it raises
    ``CaptureError("store_full")`` and the root stops ``incomplete``. Each root keeps the
    bytes of its last complete capture only (:meth:`prune_superseded`)."""

    def __init__(self, state_dir: str | Path, *, clock: Callable[[], datetime] = _now,
                 max_store_bytes: int | None = MAX_STORE_BYTES) -> None:
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
        self.db = sqlite3.connect(self.path, timeout=30)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        wal_journal(self.db)
        self.db.executescript(_DDL)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                            (STORE_SCHEMA_VERSION,))
        version = self.db.execute("SELECT value FROM meta WHERE key='schema_version'"
                                  ).fetchone()[0]
        if version != STORE_SCHEMA_VERSION:
            self.db.close()
            raise CaptureError("store_schema_outdated")
        # Compact storage (module docstring, Storage): additive columns, so a reader of
        # schema version 1 reads either layout.
        have = {r[1] for r in self.db.execute("PRAGMA table_info(blobs)")}
        with self.db:
            for column in ("dropped_at TEXT", "stored_bytes INTEGER"):
                if column.split()[0] not in have:
                    self.db.execute(f"ALTER TABLE blobs ADD COLUMN {column}")

    def _reopen(self) -> None:
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        wal_journal(self.db)

    @classmethod
    def open_read_only(cls, state_dir: str | Path, *, timeout: float = 5.0
                       ) -> "SharePointCaptureStore":
        """The store opened only to read it (a host serving captured files): nothing is
        created or written; a missing store or another schema version raises. ``timeout``:
        seconds a read waits for a writer's lock (SQLite's busy timeout)."""
        store = cls.__new__(cls)
        store.root = Path(state_dir).expanduser().resolve() / STORE_DIR
        store.blobs = store.root / "blobs" / "sha256"
        store.path = store.root / DB_FILE
        store._clock, store.max_store_bytes = _now, None
        if not store.path.is_file():
            raise CaptureError("store_missing")
        store.db = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True, timeout=timeout)
        store._read_only = True
        store.db.row_factory = sqlite3.Row
        try:
            version = store.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.Error:
            version = None
        if version is None or version[0] != STORE_SCHEMA_VERSION:
            store.db.close()
            raise CaptureError("store_schema_outdated")
        return store

    def __repr__(self) -> str:
        return "SharePointCaptureStore()"

    def close(self) -> None:
        """Close the connection; a writer first checkpoints its WAL (no wait:
        ``capture_store.checkpoint``)."""
        if not getattr(self, "_read_only", False):
            checkpoint(self.db)
        self.db.close()

    def now(self) -> str:
        return _stamp(self._clock())

    # ---------------------------------------------------------------- blobs

    def store_bytes(self) -> int:
        blobs = self.db.execute("SELECT COALESCE(SUM(COALESCE(stored_bytes, bytes)), 0) "
                                "FROM blobs").fetchone()[0]
        return sqlite_bytes(self.db) + blobs

    def store_full(self) -> bool:
        return self.max_store_bytes is not None and self.store_bytes() >= self.max_store_bytes

    def put_blob(self, content: bytes, *, kind: str) -> str:
        """Content-addressed bytes (file content and extracted text); one copy per sha256."""
        digest = sha256(content).hexdigest()
        known = self.db.execute("SELECT 1 FROM blobs WHERE sha256=?", (digest,)).fetchone()
        folder = self.blobs / digest[:2]
        target = folder / digest
        if kind == "text" and known is not None and \
                blob_path(self.blobs, digest, COMPRESSED_SUFFIX).exists():
            return digest   # held compressed
        if known is None or not target.exists():
            if known is None and self.max_store_bytes is not None and \
                    self.store_bytes() + len(content) > self.max_store_bytes:
                raise CaptureError("store_full")
            folder.mkdir(exist_ok=True, mode=0o700)
            temporary = folder / f".{digest}.{os.getpid()}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            os.replace(temporary, target)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO blobs (sha256, kind, bytes, stored_at) "
                                "VALUES (?,?,?,?)", (digest, kind, len(content), self.now()))
                # Bytes held again (a dropped original captured anew): settled again later.
                self.db.execute("UPDATE blobs SET dropped_at=NULL, stored_bytes=NULL WHERE "
                                "sha256=?", (digest,))
        return digest

    def blob(self, digest: Any) -> bytes:
        """A blob's bytes (held as they are, or compressed), re-hashed. An original the
        store dropped raises ``original_dropped`` (its readings are kept:
        :meth:`original_reading`; its bytes are re-read from the source:
        :meth:`refetch_original`); missing raises ``FileNotFoundError``."""
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise CaptureError("blob_digest_invalid")
        raw = blob_path(self.blobs, digest)
        packed = blob_path(self.blobs, digest, COMPRESSED_SUFFIX)
        if not raw.exists() and packed.exists():
            content = inflate(packed.read_bytes())
        elif not raw.exists() and self.original_dropped(digest):
            raise CaptureError("original_dropped")
        else:
            content = raw.read_bytes()
        if content is None or sha256(content).hexdigest() != digest:
            raise CaptureError("blob_digest_mismatch")
        return content

    def original_dropped(self, digest: Any) -> bool:
        """Whether the store dropped this original (no bytes held, a whole ``.dropped``
        form with its readings and quickXorHash kept)."""
        return isinstance(digest, str) and _SHA256.fullmatch(digest) is not None \
            and not blob_path(self.blobs, digest).exists() \
            and not blob_path(self.blobs, digest, COMPRESSED_SUFFIX).exists() \
            and read_dropped(self.blobs, digest) is not None

    # ---------------------------------------------------------------- roots

    def root_state(self, drive_id: str, root_path: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM roots WHERE drive_id=? AND root_path=?",
                              (drive_id, root_path)).fetchone()
        return dict(row) if row else None

    def save_root(self, drive_id: str, root_path: str, **fields: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO roots (drive_id, root_path, status) "
                            "VALUES (?,?,?)", (drive_id, root_path, "new"))
            if fields:
                names = sorted(fields)
                self.db.execute(
                    "UPDATE roots SET " + ", ".join(f"{n}=?" for n in names)
                    + " WHERE drive_id=? AND root_path=?",
                    (*[fields[n] for n in names], drive_id, root_path))

    # ---------------------------------------------------------------- incremental state

    def position(self, drive_id: str, root_path: str) -> dict[str, Any] | None:
        """The root's change-feed position (``delta_url``, ``read_at``: None is unknown,
        the earliest), or None when it has none."""
        row = self.db.execute("SELECT delta_url, read_at FROM root_cursors WHERE "
                              "drive_id=? AND root_path=?", (drive_id, root_path)).fetchone()
        return None if row is None or row[0] is None else {"delta_url": row[0],
                                                           "read_at": row[1]}

    def save_position(self, drive_id: str, root_path: str, delta_url: str | None,
                      read_at: str | None) -> None:
        """Store a root's change-feed position; ``None`` leaves it none (it walks)."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO root_cursors VALUES (?,?,?,?)",
                            (drive_id, root_path, delta_url, read_at))

    def permission_position(self, drive_id: str, root_path: str) -> dict[str, Any] | None:
        """The root's permission position (module docstring, Permission re-reads) with
        its open re-read (``reread_url``, ``reread_at``; None: none), or None when it has
        none."""
        row = self.db.execute("SELECT delta_url, read_at, reread_url, reread_at FROM "
                              "root_permissions WHERE drive_id=? AND root_path=?",
                              (drive_id, root_path)).fetchone()
        return None if row is None or row[0] is None else dict(row)

    def item_permissions(self, *, timeout: float | None = None) -> dict[tuple[str, str], str]:
        """Per captured drive item (drive id, item id), the digest of what decides who may
        read any of its versions (:meth:`_row_visible`): every version's handle, path keys,
        item permissions, removal and supersession, and its library's class. A permission
        re-read that changes nothing for an item leaves its digest; one that changes it (a
        grant, a revocation, a move, a new version, a removal, a reclassification) moves
        it. Read in one transaction (``timeout``: the busy timeout for it, seconds). A store
        that records no permission positions (no ``root_permissions``, a store from before
        permission re-reads) raises ``CaptureError("permissions_unrecorded")``: its item
        permissions are of no known re-read."""
        if timeout is not None:
            self.db.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
        digests: dict[tuple[str, str], Any] = {}
        cursor = self.db.cursor()
        cursor.row_factory = None   # plain tuples: one row per captured version
        cursor.execute("BEGIN")
        try:
            if cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                              "name='root_permissions'").fetchone() is None:
                raise CaptureError("permissions_unrecorded")
            for drive_id, item_id, *version in cursor.execute(
                    "SELECT i.drive_id, i.item_id, i.version_id, i.l1_handle, i.resources, "
                    "i.source_acl, i.removed_reason, i.superseded_at, c.security_class "
                    "FROM items i LEFT JOIN drive_classes c ON c.drive_id = i.drive_id "
                    "ORDER BY i.drive_id, i.item_id, i.version_id"):
                digest = digests.get((drive_id, item_id))
                if digest is None:
                    digest = digests[(drive_id, item_id)] = sha256()
                digest.update(repr(version).encode())   # str, int and None: unambiguous
        finally:
            self.db.rollback()
        return {key: digest.hexdigest() for key, digest in digests.items()}

    @staticmethod
    def item_of(handle: Any) -> tuple[str, str] | None:
        """The drive item (drive id, item id) whose permissions gate a handle as
        :meth:`resolve` reads it (a container member's: its container's), or None when the
        handle names no drive item."""
        if not isinstance(handle, str):
            return None
        member = split_member(cited_page(handle)[0])
        if member is not None:
            return SharePointCaptureStore.item_of(member[0])
        try:
            (drive_id, item_id), _, _ = parse_handle(handle, SCHEME, 2)
        except ValueError:
            return None
        return drive_id, item_id

    def save_permission_position(self, drive_id: str, root_path: str,
                                 delta_url: str | None, read_at: str | None) -> None:
        """Store a root's permission position; an open re-read ends with it."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO root_permissions VALUES (?,?,?,?,NULL,NULL)",
                            (drive_id, root_path, delta_url, read_at))

    def open_reread(self, drive_id: str, root_path: str, delta_url: str, read_at: str
                    ) -> None:
        """Record the first run of a re-read the time budget cut (kept until it ends)."""
        with self.db:
            self.db.execute("UPDATE root_permissions SET reread_url=?, reread_at=? WHERE "
                            "drive_id=? AND root_path=? AND reread_at IS NULL",
                            (delta_url, read_at, drive_id, root_path))

    def seed_positions(self, drive_id: str, roots: Sequence[str]) -> None:
        """A legacy drive cursor becomes the position of each of ``roots`` that was walked
        and has none stored (read time unknown: the earliest), then goes."""
        row = self.db.execute("SELECT delta_url FROM drive_cursors WHERE drive_id=?",
                              (drive_id,)).fetchone()
        if row is None:
            return
        with self.db:
            for root in roots:
                if self.walked_at(drive_id, root) is not None:
                    self.db.execute("INSERT OR IGNORE INTO root_cursors VALUES (?,?,?,NULL)",
                                    (drive_id, root, row[0]))
            self.db.execute("DELETE FROM drive_cursors WHERE drive_id=?", (drive_id,))

    def expire_walk(self, drive_id: str, root_path: str) -> None:
        """Make a complete root's next run a full walk (``walk_due``) with no position:
        its feed position is gone (HTTP 410) or its feed unreadable."""
        self.save_walked(drive_id, root_path, EXPIRED_WALK)
        self.save_position(drive_id, root_path, None, None)
        self.save_permission_position(drive_id, root_path, None, None)

    def save_drive_class(self, drive_id: str, security_class: str | None) -> None:
        """Record (or, with None, forget) the class of a drive's library."""
        with self.db:
            if security_class is None:
                self.db.execute("DELETE FROM drive_classes WHERE drive_id=?", (drive_id,))
            else:
                self.db.execute("INSERT INTO drive_classes VALUES (?,?,?) ON CONFLICT "
                                "(drive_id) DO UPDATE SET security_class=excluded."
                                "security_class, decided_at=excluded.decided_at",
                                (drive_id, security_class, self.now()))

    def drive_class(self, drive_id: str) -> str | None:
        try:
            found = self.db.execute("SELECT security_class FROM drive_classes WHERE "
                                    "drive_id=?", (drive_id,)).fetchone()
        except sqlite3.OperationalError:
            return None   # a store captured before drive classes existed (read-only)
        return None if found is None else found[0]

    def walked_folders(self, drive_id: str, root_path: str) -> dict[str, dict[str, Any]]:
        return {row["folder_id"]: dict(row) for row in self.db.execute(
            "SELECT * FROM walked_folders WHERE drive_id=? AND root_path=?",
            (drive_id, root_path))}

    def forget_folders(self, drive_id: str, root_path: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM walked_folders WHERE drive_id=? AND root_path=?",
                            (drive_id, root_path))

    def note_folder(self, drive_id: str, folder_id: str, root_path: str, path: str,
                    name: str | None, parent_id: str | None, depth: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO walked_folders VALUES (?,?,?,?,?,?,?)",
                            (drive_id, folder_id, root_path, path, name, parent_id, depth))

    def walked_at(self, drive_id: str, root_path: str) -> str | None:
        row = self.db.execute("SELECT walked_at FROM root_walks WHERE drive_id=? AND "
                              "root_path=?", (drive_id, root_path)).fetchone()
        return row[0] if row else None

    def save_walked(self, drive_id: str, root_path: str, walked_at: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO root_walks VALUES (?,?,?)",
                            (drive_id, root_path, walked_at))

    def full_walk_age(self, drive_id: str, root_path: str) -> dict[str, Any]:
        """How old the root's captured state is: when its last full walk that listed
        everything ended (``last_full_walk_at``, None: never), its age in whole hours
        (``age_hours``) and whether a refresh of it is open (``refresh_open``: a cut walk
        holds a position while the root still serves that older walk's state)."""
        walked = self.walked_at(drive_id, root_path)
        age = None if walked is None else int((self._clock() - datetime.strptime(
            walked, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        ).total_seconds() // 3600)
        return {"last_full_walk_at": walked, "age_hours": age,
                "refresh_open": self.walk_position(drive_id, root_path) is not None}

    def walk_position(self, drive_id: str, root_path: str) -> dict[str, Any] | None:
        """The stored position of a root's cut full walk (``walk_positions``), or None."""
        row = self.db.execute("SELECT * FROM walk_positions WHERE drive_id=? AND "
                              "root_path=?", (drive_id, root_path)).fetchone()
        if row is None:
            return None
        return {"started_at": row["started_at"], "delta_url": row["delta_url"],
                **json.loads(row["position"])}

    def save_walk_position(self, drive_id: str, root_path: str, started_at: str,
                           delta_url: str | None, position: Mapping[str, Any] | None) -> None:
        """Store a cut walk's position; ``None`` forgets it (the walk ended)."""
        with self.db:
            if position is None:
                self.db.execute("DELETE FROM walk_positions WHERE drive_id=? AND "
                                "root_path=?", (drive_id, root_path))
            else:
                self.db.execute("INSERT OR REPLACE INTO walk_positions VALUES (?,?,?,?,?,?)",
                                (drive_id, root_path, started_at, delta_url,
                                 _canonical(dict(position)), self.now()))

    # ---------------------------------------------------------------- versions

    def _latest(self, handle: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM items WHERE l1_handle=? "
                               "ORDER BY version_id DESC LIMIT 1", (handle,)).fetchone()

    def current(self, drive_id: str, item_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM items WHERE drive_id=? AND item_id=? AND "
                              "superseded_at IS NULL", (drive_id, item_id)).fetchone()
        return dict(row) if row else None

    def _supersede_current(self, drive_id: str, item_id: str, now: str,
                           reason: str | None = None, event: str | None = None) -> int:
        rows = self.db.execute("SELECT version_id, l1_handle FROM items WHERE drive_id=? AND "
                               "item_id=? AND superseded_at IS NULL",
                               (drive_id, item_id)).fetchall()
        for row in rows:
            self.db.execute("UPDATE items SET superseded_at=?, removed_reason=? "
                            "WHERE version_id=?", (now, reason, row["version_id"]))
            self.db.execute("INSERT INTO item_events VALUES (?,?,?)",
                            (row["l1_handle"], event or ("removed" if reason else "superseded"),
                             now))
        return len(rows)

    @staticmethod
    def _digest(record: Mapping[str, Any], content_sha256: str | None) -> str:
        return sha256(_canonical({"record": record, "content_sha256": content_sha256})
                      .encode("utf-8")).hexdigest()

    def _insert(self, handle: str, record: Mapping[str, Any], root_path: str,
                resources: Sequence[str], acl: Mapping[str, Any],
                content: Mapping[str, Any], now: str) -> None:
        self.db.execute(
            "INSERT INTO items (l1_handle, drive_id, item_id, etag, root_path, path, record, "
            "resources, source_acl, digest, content_status, content_sha256, text_status, "
            "text_sha256, recorded_at, superseded_at, removed_reason, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
            (handle, record["drive_id"], record["id"], record["etag"], root_path,
             record["path"], _canonical(record), _canonical(list(resources)),
             _canonical(dict(acl)), self._digest(record, content.get("content_sha256")),
             content["content_status"], content.get("content_sha256"),
             content["text_status"], content.get("text_sha256"), now, now))

    def observe(self, record: Mapping[str, Any], root_path: str, resources: Sequence[str],
                acl: Mapping[str, Any]) -> str:
        """A version listed again by the source.

        ``seen``: the current version at the same path (last seen time and the item
        permissions read now are updated; permissions change without a new eTag).
        ``moved``: the current version, listed at another path with the same eTag: a new
        interval at the new path (re-gated on its new keys). ``reappeared``: captured
        before but not current (removed, or A -> B -> A eTags): a new interval. Both copy
        the captured content (no source read). ``new``: never captured, or back after
        its bytes were pruned (:meth:`prune_superseded`; its bytes are read again)."""
        handle = version_handle(record["drive_id"], record["id"], record["etag"])
        latest = self._latest(handle)
        if latest is None or (latest["superseded_at"] is not None
                              and latest["content_status"] == PRUNED):
            # Never captured, or back after its bytes were pruned: read from the source.
            return "new"
        now = self.now()
        with self.db:
            if latest["superseded_at"] is None and latest["path"] == record["path"]:
                # A root that now holds the item (a wider root replacing a nested one)
                # takes it over, so its reconcile and byte retries cover it.
                self.db.execute("UPDATE items SET last_seen_at=?, source_acl=?, resources=?, "
                                "root_path=? WHERE version_id=?",
                                (now, _canonical(dict(acl)), _canonical(list(resources)),
                                 root_path, latest["version_id"]))
                return "seen"
            outcome = "moved" if latest["superseded_at"] is None else "reappeared"
            self._supersede_current(record["drive_id"], record["id"], now,
                                    event="moved" if outcome == "moved" else None)
            stored = {**json.loads(latest["record"]), "path": record["path"],
                      "name": record.get("name")}
            self._insert(handle, stored, root_path, resources, acl,
                         {c: latest[c] for c in _CONTENT_COLUMNS}, now)
            self.db.execute("INSERT INTO item_events VALUES (?,?,?)", (handle, outcome, now))
        return outcome

    def add_version(self, record: Mapping[str, Any], root_path: str, resources: Sequence[str],
                    acl: Mapping[str, Any], content: Mapping[str, Any]) -> int:
        """Insert a new version, superseding the current one (returns 0 or 1)."""
        handle = version_handle(record["drive_id"], record["id"], record["etag"])
        now = self.now()
        with self.db:
            superseded = self._supersede_current(record["drive_id"], record["id"], now)
            self._insert(handle, record, root_path, resources, acl, content, now)
            self.db.execute("INSERT INTO item_events VALUES (?,?,?)", (handle, "recorded", now))
        return superseded

    def fill_content(self, handle: str, content: Mapping[str, Any]) -> None:
        """Bytes of a degraded version read later for the same eTag: every interval row
        of the handle gets them; an event records the repair."""
        row = self._latest(handle)
        if row is None:
            return
        record = json.loads(row["record"])
        with self.db:
            self.db.execute(
                "UPDATE items SET content_status=?, content_sha256=?, text_status=?, "
                "text_sha256=?, digest=? WHERE l1_handle=?",
                (content["content_status"], content.get("content_sha256"),
                 content["text_status"], content.get("text_sha256"),
                 self._digest(record, content.get("content_sha256")), handle))
            self.db.execute("INSERT INTO item_events VALUES (?,?,?)",
                            (handle, "content_recovered", self.now()))

    def fill_text(self, handle: str, status: str, text_sha256: str | None) -> None:
        """Text of a captured version extracted again from its captured bytes (OCR now
        available): every interval row of the handle gets it; an event records it."""
        with self.db:
            self.db.execute("UPDATE items SET text_status=?, text_sha256=? WHERE l1_handle=?",
                            (status, text_sha256, handle))
            self.db.execute("INSERT INTO item_events VALUES (?,?,?)",
                            (handle, "text_reextracted", self.now()))

    def text_unread(self, drive_id: str, root_path: str, limit: int) -> list[dict[str, Any]]:
        """Current versions under a root whose bytes are captured but whose scanned pages
        were never OCR-read (no engine then, or captured before OCR existed)."""
        rows = self.db.execute(
            "SELECT * FROM items WHERE drive_id=? AND root_path=? AND superseded_at IS NULL "
            f"AND content_status=? AND text_status IN ({','.join('?' * len(TEXT_UNREAD))}) "
            "ORDER BY version_id LIMIT ?",
            (drive_id, root_path, CAPTURED, *TEXT_UNREAD, limit)).fetchall()
        return [dict(r) for r in rows if is_paged_file(json.loads(r["record"]))]

    def columns_stale(self, drive_id: str, root_path: str, version: str
                      ) -> list[dict[str, Any]]:
        """Current versions under a root whose columns were read by another column-set
        version than ``version`` (or carry none)."""
        rows = self.db.execute(
            "SELECT * FROM items WHERE drive_id=? AND root_path=? AND superseded_at IS NULL "
            "AND json_extract(record, '$.list_item_columns_version') IS NOT ? "
            "ORDER BY version_id", (drive_id, root_path, version)).fetchall()
        return [dict(r) for r in rows]

    def reread_columns(self, row: Mapping[str, Any], record: Mapping[str, Any]) -> None:
        """A current version's columns read again (``record``, the same eTag): a new
        interval of the version from now holding them, the earlier one keeping what was
        read then; content, permissions and place carried over; event ``columns_reread``."""
        if record.get("etag") != row["etag"] or record.get("id") != row["item_id"]:
            raise CaptureError("columns_reread_other_version")
        now = self.now()
        with self.db:
            self.db.execute("UPDATE items SET superseded_at=? WHERE version_id=? AND "
                            "superseded_at IS NULL", (now, row["version_id"]))
            self._insert(row["l1_handle"], record, row["root_path"],
                         json.loads(row["resources"]), json.loads(row["source_acl"]),
                         {c: row[c] for c in _CONTENT_COLUMNS}, now)
            # A listItem read is no listing: the version was last seen when it was.
            self.db.execute("UPDATE items SET last_seen_at=? WHERE l1_handle=? AND "
                            "superseded_at IS NULL", (row["last_seen_at"], row["l1_handle"]))
            self.db.execute("INSERT INTO item_events VALUES (?,?,?)",
                            (row["l1_handle"], "columns_reread", now))

    def degraded(self, drive_id: str, root_path: str, limit: int, *,
                 after: int = 0) -> list[dict[str, Any]]:
        """Current versions under a root whose bytes failed to read or a metadata-only
        capture skipped (read by a later content capture), in version order; ``after``:
        only those past that version id (a keyset page)."""
        rows = self.db.execute(
            "SELECT * FROM items WHERE drive_id=? AND root_path=? AND superseded_at IS NULL "
            "AND (content_status LIKE ? OR content_status=?) AND version_id > ? "
            "ORDER BY version_id LIMIT ?",
            (drive_id, root_path, _UNAVAILABLE + "%", CONTENT_SKIPPED, after,
             limit)).fetchall()
        return [dict(r) for r in rows]

    def content_skipped(self, drive_id: str, root_path: str) -> int:
        """Current versions under a root whose content a metadata-only capture skipped."""
        return self.db.execute(
            "SELECT COUNT(*) FROM items WHERE drive_id=? AND root_path=? AND "
            "superseded_at IS NULL AND content_status=?",
            (drive_id, root_path, CONTENT_SKIPPED)).fetchone()[0]

    def content_incomplete(self, drive_id: str, root_path: str, *,
                           read_content: bool = True) -> int:
        """Current versions under a root without a terminal content outcome: neither
        captured nor recorded over the content cap (their bytes failed to read, or, for a
        capture that reads content, a metadata-only capture skipped them)."""
        return self.db.execute(
            "SELECT COUNT(*) FROM items WHERE drive_id=? AND root_path=? AND "
            "superseded_at IS NULL AND content_status NOT IN (?,?,?)",
            (drive_id, root_path, CAPTURED, EXCEEDS_CAP,
             EXCEEDS_CAP if read_content else CONTENT_SKIPPED)).fetchone()[0]

    def reconcile_root(self, drive_id: str, root_path: str, started_at: str, *,
                       where_now: Callable[[str], Mapping[str, Any] | None] | None = None
                       ) -> tuple[dict[str, int], list[Mapping[str, Any]]]:
        """After a walk that listed everything under a root: every current version of the
        drive under the root's path that the walk did not see is gone from where it was
        (whichever root it was attributed to: roots of one scope never nest, and a
        narrower root a wider one replaced leaves its items here). ``where_now(item_id)``
        (a GET by item id, at most ``REPAIRS_PER_ROOT`` per call) tells where it is now,
        as its drive-item record: a file still under the root (moved during the walk into
        a folder the walk had listed already) is left current, re-gated on the keys and
        item permissions of where it is now (the caller may capture it there only in a
        later run), and returned, for the caller to capture where it is now; elsewhere ->
        ``moved_out_of_scope``, re-gated there; ``None`` or not asked ->
        ``absent_after_resync``, hidden from every reader (:meth:`_row_visible`). Paths
        compare as the walk does (:func:`_in_root`). Returns the versions superseded per
        reason and the records of the files still under the root."""
        rows = [row for row in self.db.execute(
            "SELECT DISTINCT item_id, path FROM items WHERE drive_id=? AND "
            "superseded_at IS NULL AND last_seen_at < ?",
            (drive_id, started_at)).fetchall() if _in_root(row["path"], root_path)]
        located = {}
        if where_now is not None:
            for row in rows[:REPAIRS_PER_ROOT]:   # source reads outside the transaction
                located[row["item_id"]] = where_now(row["item_id"])
        within = {item_id: place for item_id, place in located.items() if place is not None
                  and place.get("kind") == "file" and _in_root(place["path"], root_path)}
        now = self.now()
        gone = {ABSENT: 0, MOVED_OUT: 0}
        with self.db:
            for row in rows:
                place = located.get(row["item_id"])
                if place is None:
                    gone[ABSENT] += self._supersede_current(drive_id, row["item_id"], now,
                                                            ABSENT)
                    continue
                if row["item_id"] in within:
                    self.db.execute(
                        "UPDATE items SET resources=?, source_acl=? WHERE drive_id=? AND "
                        "item_id=? AND superseded_at IS NULL",
                        (_canonical(_keys(drive_id, place["path"], False)),
                         _canonical(dict(place.get("source_acl") or {})), drive_id,
                         row["item_id"]))
                    continue
                gone[MOVED_OUT] += self._supersede_current(drive_id, row["item_id"], now,
                                                           MOVED_OUT, event=MOVED_OUT)
                self.db.execute(
                    "UPDATE items SET resources=?, source_acl=? WHERE version_id = (SELECT "
                    "MAX(version_id) FROM items WHERE drive_id=? AND item_id=?)",
                    (_canonical(_keys(drive_id, place["path"], place.get("kind") == "folder")),
                     _canonical(dict(place.get("source_acl") or {})), drive_id,
                     row["item_id"]))
        return gone, list(within.values())

    def retire_nested_roots(self, drive_id: str, root_path: str) -> int:
        """Forget the walk state of the drive's other roots under ``root_path`` once a
        full walk of it listed everything (a library-wide root replacing folder roots):
        their items were taken over or reconciled by that walk."""
        nested = [r[0] for r in self.db.execute(
            "SELECT root_path FROM roots WHERE drive_id=? AND root_path<>?",
            (drive_id, root_path)) if _under(r[0], root_path)]
        with self.db:
            for path in nested:
                for table in ("roots", "walked_folders", "root_walks", "walk_positions",
                              "root_cursors", "root_permissions"):
                    self.db.execute(f"DELETE FROM {table} WHERE drive_id=? AND root_path=?",
                                    (drive_id, path))
        return len(nested)

    def prune_superseded(self, drive_id: str, root_path: str, before: str) -> int:
        """Keep only the root's last complete capture: called once a full walk that
        started at ``before`` captured everything under the root, every version under it
        superseded before that walk started (so in no capture since the one before) loses
        its bytes and text (``content_status`` and ``text_status`` ``pruned``; a
        ``content_pruned`` event). A version superseded during the walk keeps its bytes
        until the next complete walk, so what a reader cited of the previous capture still
        reads (decided: ``devloop/DECISIONS/2026-10-15-estate-prune-window.md``). Its
        metadata, item permissions, digest and validity interval stay, so its citation still resolves
        (without bytes; a ``#sha256=`` citation of it no longer does). A version whose
        handle is current (moved, same eTag) keeps its bytes; one that comes back later
        is read again (:meth:`observe`); a blob no row names any more is deleted.
        Returns the versions pruned."""
        rows = [row for row in self.db.execute(
            "SELECT version_id, l1_handle, path, content_sha256, text_sha256 FROM items "
            "WHERE drive_id=? AND superseded_at IS NOT NULL AND superseded_at < ? AND "
            "(content_sha256 IS NOT NULL OR text_sha256 IS NOT NULL) AND l1_handle NOT IN "
            "(SELECT l1_handle FROM items WHERE superseded_at IS NULL)",
            (drive_id, before)).fetchall() if _in_root(row["path"], root_path)]
        digests = {d for row in rows for d in (row["content_sha256"], row["text_sha256"])
                   if d}
        now, gone = self.now(), set()
        with self.db:
            for row in rows:
                self.db.execute("UPDATE items SET content_status=?, content_sha256=NULL, "
                                "text_status=?, text_sha256=NULL WHERE version_id=?",
                                (PRUNED, PRUNED, row["version_id"]))
                self.db.execute("INSERT INTO item_events VALUES (?,?,?)",
                                (row["l1_handle"], "content_pruned", now))
            for digest in sorted(digests):
                if self.db.execute("SELECT 1 FROM items WHERE content_sha256=? OR "
                                   "text_sha256=? LIMIT 1", (digest, digest)).fetchone():
                    continue
                self.db.execute("DELETE FROM blobs WHERE sha256=?", (digest,))
                gone.add(digest)
        for digest in gone:
            for suffix in ("", COMPRESSED_SUFFIX, DROPPED_SUFFIX):
                blob_path(self.blobs, digest, suffix).unlink(missing_ok=True)
        return len(rows)

    # ---------------------------------------------------------------- compact storage

    @staticmethod
    def _awaits_reading(text_status: Any, record: Mapping[str, Any]) -> bool:
        """Whether a later walk reads a version's text from its held bytes again
        (:meth:`text_unread`, :meth:`SharePointCapture._reextract`): a scan whose OCR is
        pending or had no engine, or an untyped OCR failure. Its bytes are kept."""
        if text_status is None or text_status in UNTYPED_OCR_FAILURES:
            return True
        return text_status in TEXT_UNREAD and is_paged_file(record)

    def _pending_rows(self, digest: str) -> list[sqlite3.Row]:
        """Current versions holding ``digest`` whose bytes a later reading needs."""
        rows = self.db.execute(
            "SELECT l1_handle, record, text_status, text_sha256 FROM items WHERE "
            "content_sha256=? AND content_status=? AND superseded_at IS NULL",
            (digest, CAPTURED)).fetchall()
        return [r for r in rows if self._awaits_reading(r["text_status"],
                                                        json.loads(r["record"]))]

    def _compress(self, digest: str) -> bool:
        """A blob held as its bytes stored zlib-compressed instead (``<sha256>.z``,
        written before the bytes go); False when not held as its bytes or unreadable."""
        import zlib

        raw = blob_path(self.blobs, digest)
        if not raw.exists():
            return False
        try:
            content = self.blob(digest)
        except (CaptureError, OSError):
            return False
        packed = zlib.compress(content, 6)
        target = blob_path(self.blobs, digest, COMPRESSED_SUFFIX)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(packed)
        os.replace(temporary, target)
        with self.db:
            self.db.execute("UPDATE blobs SET stored_bytes=? WHERE sha256=?",
                            (len(packed), digest))
        raw.unlink()
        return True

    def settle_original(self, digest: str, *, content: bytes | None = None,
                        defer_ocr: bool = False) -> str:
        """Drop a captured original once nothing needs its bytes, as the mail capture
        does (``MailCaptureStore.settle_original``): over ``DROP_ORIGINALS_ABOVE``
        (256 KiB), not a text, not a container (its members are cited from its bytes),
        no current version awaiting a reading of it, and every reading a reader makes of
        it (each captured name and type of these bytes, without and with each pinned OCR
        text) made from the bytes and kept in its ``.dropped`` form with its quickXorHash
        (written before the bytes go). Its versions keep their ``content_sha256`` pin,
        their item ids and eTags: the verified refetch handle
        (:meth:`refetch_original`). ``defer_ocr`` (``compact --defer-ocr``): a scan whose
        OCR is pending is dropped too and its current versions read ``ocr_deferred``
        (text layer kept; a later walk re-fetches and OCR-reads it). Returns the outcome
        (``dropped``, ``kept_small``, ``kept_pending``, ``kept_text``,
        ``kept_container``, ``kept_unreadable``, ``unreferenced``, ``absent``)."""
        row = self.db.execute("SELECT bytes FROM blobs WHERE sha256=?", (digest,)).fetchone()
        if row is None or _SHA256.fullmatch(digest) is None:
            return "absent"
        dropped = self.original_dropped(digest)
        if dropped and (content is None or sha256(content).hexdigest() != digest):
            return "dropped"
        if row["bytes"] <= DROP_ORIGINALS_ABOVE:
            return "kept_small"
        if self.db.execute("SELECT 1 FROM items WHERE text_sha256=? LIMIT 1",
                           (digest,)).fetchone() is not None:
            self._compress(digest)   # bytes that are their own text: kept, compressed
            return "kept_text"
        captured = self.db.execute(
            "SELECT l1_handle, record, text_status, text_sha256 FROM items WHERE "
            "content_sha256=? AND content_status=?", (digest, CAPTURED)).fetchall()
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
            mime, name = file_type(json.loads(found["record"]))
            if is_container(content, name, mime):
                return "kept_container"
            keys.add((mime, name, None))
            if ocr_digest(dict(found)) is not None:
                keys.add((mime, name, ocr_digest(dict(found))))
        kept = read_dropped(self.blobs, digest)
        have = dict(kept["readings"]) if kept is not None else {}
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
        for found in pending:   # OCR put off: the text layer stays the reading
            self.fill_text(found["l1_handle"], OCR_DEFERRED, found["text_sha256"])
        with self.db:
            self.db.execute("UPDATE blobs SET dropped_at=COALESCE(dropped_at, ?), "
                            "stored_bytes=? WHERE sha256=?", (self.now(), size, digest))
        blob_path(self.blobs, digest).unlink(missing_ok=True)
        return "dropped"

    def ocr_deferred_rows(self, drive_id: str, root_path: str, limit: int
                          ) -> list[dict[str, Any]]:
        """Current versions under a root whose OCR was put off with their bytes
        dropped (``ocr_deferred``), oldest first."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM items WHERE drive_id=? AND root_path=? AND superseded_at IS NULL "
            "AND content_status=? AND text_status=? ORDER BY version_id LIMIT ?",
            (drive_id, root_path, CAPTURED, OCR_DEFERRED, limit)).fetchall()]

    def compact(self, *, vacuum: bool = True, defer_ocr: bool = False,
                free_bytes: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free
                ) -> dict[str, Any]:
        """One-time compaction of a store captured before compact storage (CLI
        ``compact``), as ``MailCaptureStore.compact``: every text stored as it is is
        compressed; every original nothing needs is dropped (:meth:`settle_original`);
        then the SQLite file is rewritten (``VACUUM INTO``, checked, swapped in
        atomically) when the free disk covers it. Counts and bytes only. Never beside a
        capture (the CLI holds the host's cycle lock)."""
        result: dict[str, Any] = {"bytes_before": self._disk_bytes(), "compressed": 0,
                                  "outcomes": {}}
        for (digest,) in self.db.execute(
                "SELECT sha256 FROM blobs WHERE kind='text' AND dropped_at IS NULL "
                "ORDER BY sha256").fetchall():
            if self.db.execute("SELECT 1 FROM items WHERE content_sha256=? LIMIT 1",
                               (digest,)).fetchone() is None:
                result["compressed"] += self._compress(digest)
        for (digest,) in self.db.execute(
                "SELECT DISTINCT content_sha256 FROM items WHERE content_status=? AND "
                "content_sha256 IS NOT NULL ORDER BY content_sha256", (CAPTURED,)).fetchall():
            outcome = self.settle_original(digest, defer_ocr=defer_ocr)
            result["outcomes"][outcome] = result["outcomes"].get(outcome, 0) + 1
        result["ocr_deferred"] = self.db.execute(
            "SELECT COUNT(*) FROM items WHERE superseded_at IS NULL AND text_status=?",
            (OCR_DEFERRED,)).fetchone()[0]
        result["vacuum"] = self._vacuum(free_bytes) if vacuum else "skipped:not_asked"
        result["bytes_after"] = self._disk_bytes()
        return result

    # The mail store's own measure and rewrite (same attributes: ``db``, ``path``,
    # ``root``, ``blobs``, ``_reopen``), one copy of each.
    _disk_bytes = MailCaptureStore._disk_bytes
    _vacuum = MailCaptureStore._vacuum

    # ---------------------------------------------------------------- reads (per reader)

    def _row_visible(self, row: Mapping[str, Any], actor: Any, audience: Any) -> bool:
        """The reader may see this version (its path keys and item permissions) and the
        item as it was last captured: a revoked permission or a move into a folder the
        reader may not see hides the item's history too, and an item gone from the scope
        to where the capture could not see it (``absent_after_resync``) is hidden from
        every reader."""
        if not isinstance(actor, Actor) or not isinstance(audience, LiveSourceAudience):
            return False
        if not audience.visible_all(actor, json.loads(row["resources"]),
                                    json.loads(row["source_acl"])):
            return False
        latest = self._item_latest(row)
        if latest["removed_reason"] == ABSENT:
            return False
        return latest["version_id"] == row["version_id"] or audience.visible_all(
            actor, json.loads(latest["resources"]), json.loads(latest["source_acl"]))

    def _item_latest(self, row: Mapping[str, Any]) -> sqlite3.Row:
        """The latest captured version of ``row``'s item (by version id)."""
        return self.db.execute("SELECT version_id, resources, source_acl, removed_reason "
                               "FROM items WHERE drive_id=? AND item_id=? "
                               "ORDER BY version_id DESC LIMIT 1",
                               (row["drive_id"], row["item_id"])).fetchone()

    def _drives(self, actor: Any, audience: Any) -> list[str]:
        """Captured drives where this reader holds any grant (items are checked one by one)."""
        if not isinstance(actor, Actor) or not isinstance(audience, LiveSourceAudience):
            return []
        return [r[0] for r in self.db.execute("SELECT DISTINCT drive_id FROM items "
                                              "ORDER BY drive_id")
                if audience.may_read_any(actor, f"sharepoint:{r[0]}:")]

    def _visible_rows(self, actor: Any, audience: Any, where: str = "superseded_at IS NULL",
                      params: Sequence[Any] = ()) -> Iterator[sqlite3.Row]:
        drives = self._drives(actor, audience)
        if not drives:
            return
        marks = ",".join("?" * len(drives))
        for row in self.db.execute(f"SELECT * FROM items WHERE drive_id IN ({marks}) AND "
                                   f"{where} ORDER BY drive_id, item_id, version_id",
                                   (*drives, *params)):
            if self._row_visible(row, actor, audience):
                yield row

    def resolve(self, handle: Any, actor: Actor, audience: LiveSourceAudience, *,
                text: bool = True) -> dict[str, Any] | None:
        """The captured version a handle names, offline, for a reader who may see the
        item (path keys and item permissions recorded at capture); ``None`` when not
        visible or never captured (indistinguishable). ``#sha256=<hex>`` must name the
        version's pinned bytes; ``page=<n>`` (a paged file) narrows ``content_text`` to
        that page. Shape for ``claim_checks`` rows (field paths ``row.<name>``;
        ``row.content_text`` is the extracted text the mention extractor read), plus the
        last capture that saw the version (``last_seen_at``), the item permissions
        recorded then (``source_acl``) and those of the item's latest captured version
        (``item_source_acl``: a permission revoked since applies to every older
        version too, as :meth:`_row_visible` applies it). ``text=False`` (a visibility
        decision) reads no text blob: ``content_text`` is None and a page is not checked
        against the text. A member of a captured zip/.msg/.eml file
        (``&member=<path>&member_sha256=<hex>``, ``integrations.captured_text``) resolves
        for a reader who may see its container, as that member (``text``: its text, or a
        page's). Machine-only: values are raw."""
        if isinstance(handle, str):
            base, page = cited_page(handle)
            member = split_member(base)
            if member is not None:
                return self._resolve_member(handle, member, page, actor, audience, text)
        try:
            (drive_id, item_id), etag, extra = parse_handle(handle, SCHEME, 2)
        except ValueError:
            return None
        if set(extra) - {"sha256", "page"}:
            return None
        row = self._latest(version_handle(drive_id, item_id, etag))
        if row is None or not self._row_visible(row, actor, audience):
            return None
        if "sha256" in extra and extra["sha256"] != row["content_sha256"]:
            return None
        record = json.loads(row["record"])
        found = None if not text else captured_text(self.blobs, {
            **record, **{key: row[key] for key in _CONTENT_COLUMNS}})
        content_text = None if found is None else found.text
        page = None
        if "page" in extra:
            page = _page_number(extra["page"])
            if page is None or not is_paged_file(record):
                return None
            if text:
                content_text = None if found is None else found.page(page)
                if content_text is None:
                    return None
        # The class the scope gave the library now (a reclassification applies to
        # every captured version).
        security_class = self.drive_class(row["drive_id"])
        library_class = ({} if security_class is None
                         else {"library_security_class": security_class})
        out = {"relation": "sharepoint.driveItems", "l1_handle": row["l1_handle"],
               "lineage": LINEAGE, "recorded_at": row["recorded_at"],
               "superseded_at": row["superseded_at"], "removed_reason": row["removed_reason"],
               "last_seen_at": row["last_seen_at"], "digest": row["digest"],
               "source_acl": json.loads(row["source_acl"]),
               "item_source_acl": json.loads(self._item_latest(row)["source_acl"]),
               # How old the captured state of the version's root is (module docstring,
               # Refresh walks): stale completeness never reads as fresh.
               "root_full_walk": self.full_walk_age(row["drive_id"], row["root_path"]),
               "row": {**record, **library_class,
                       "portal_metadata": taxonomy.stated_metadata(record),
                       "content_status": row["content_status"],
                       "content_sha256": row["content_sha256"],
                       "text_status": row["text_status"], "content_text": content_text}}
        if found is not None and found.skipped_by_cap is not None:
            out["row"]["skipped_by_cap"] = found.skipped_by_cap   # the text's pages only
        if page is not None:
            out["page"] = page
        return out

    def _resolve_member(self, handle: str, member: tuple[str, str, str | None],
                        page: int | None, actor: Actor, audience: LiveSourceAudience,
                        text: bool) -> dict[str, Any] | None:
        """:meth:`resolve` of a container member: the container's gate (its pinned bytes),
        then the member (``text=False``: the gate alone, no blob read)."""
        container, path, digest = member
        view = self.resolve(container, actor, audience, text=False)
        if view is None or view.get("page") is not None or "#sha256=" not in container:
            return None
        row = {"container": container, "path": path, "content_sha256": digest,
               "content_text": None}
        if text:
            found = member_view(self.blobs, view["row"], path, digest, page)
            if found is None:
                return None
            row.update(found)
        out = {**view, "relation": "sharepoint.containerMembers", "l1_handle": handle,
               "row": row}
        if page is not None:
            out["page"] = page
        return out

    def content(self, handle: Any, actor: Actor, audience: LiveSourceAudience) -> bytes | None:
        """Captured bytes of a visible version (re-hashed), or ``None`` (none captured,
        or an original the store dropped: :meth:`original_reading`,
        :meth:`refetch_original`)."""
        resolved = self.resolve(handle, actor, audience, text=False)
        if resolved is None or resolved["row"]["content_sha256"] is None \
                or self.original_dropped(resolved["row"]["content_sha256"]):
            return None
        return self.blob(resolved["row"]["content_sha256"])

    def original_verified(self, handle: Any, actor: Actor, audience: LiveSourceAudience
                          ) -> bool:
        """Whether a visible version's original verifies: its held bytes hash to the pin
        (``blob_digest_mismatch`` raises), or the store dropped it and its ``.dropped``
        record is whole."""
        resolved = self.resolve(handle, actor, audience, text=False)
        if resolved is None or resolved["row"]["content_sha256"] is None:
            return False
        digest = resolved["row"]["content_sha256"]
        if self.original_dropped(digest):
            return True   # original_dropped reads the .dropped record whole
        self.blob(digest)
        return True

    def original_reading(self, handle: Any, actor: Actor, audience: LiveSourceAudience
                         ) -> Any:
        """The reading kept of a visible version's dropped original
        (``captured_text.CapturedText``: the reading its bytes gave), or None (not
        visible, not dropped, or no such reading kept)."""
        resolved = self.resolve(handle, actor, audience, text=False)
        return None if resolved is None else self.kept_reading(resolved["l1_handle"])

    def kept_reading(self, l1_handle: str) -> Any:
        """:meth:`original_reading` for a version a gate already let its reader see."""
        row = self._latest(l1_handle)
        if row is None or not self.original_dropped(row["content_sha256"]):
            return None
        return captured_text(self.blobs, {**json.loads(row["record"]),
                                          **{key: row[key] for key in _CONTENT_COLUMNS}})

    def refetch_original(self, handle: Any, actor: Actor, audience: LiveSourceAudience,
                         reader: Any, reader_actor: Actor) -> bytes | None:
        """A visible version's original bytes: held ones, or for a dropped one one
        guarded GET through ``reader`` (``SharePointReader.get_content`` of its item, as
        ``reader_actor``) returned only when they hash to the version's pin; never stored.
        None when not visible, gone or changed at the source."""
        resolved = self.resolve(handle, actor, audience, text=False)
        if resolved is None or resolved["row"]["content_sha256"] is None:
            return None
        digest = resolved["row"]["content_sha256"]
        if not self.original_dropped(digest):
            return self.blob(digest)
        try:
            read = reader.get_content(reader_actor, resolved["row"]["id"],
                                      max_bytes=MAX_DECLARED_CONTENT_BYTES)
        except (NotVisible, SourceNotFound, SourceUnavailable):
            return None
        content = read.get("content")
        if not isinstance(content, bytes) or sha256(content).hexdigest() != digest:
            return None
        return content

    def history(self, actor: Actor, audience: LiveSourceAudience, handle: Any
                ) -> list[dict[str, Any]]:
        """Every validity interval of every captured version of the item a handle names
        that this reader may see, oldest first (``[]`` when not visible)."""
        found = self.resolve(handle, actor, audience)
        if found is None:
            return []
        row = self._latest(found["l1_handle"])
        out = []
        for version in self.db.execute(
                "SELECT * FROM items WHERE drive_id=? AND item_id=? ORDER BY recorded_at, "
                "version_id", (row["drive_id"], row["item_id"])):
            if self._row_visible(version, actor, audience):
                out.append({key: version[key] for key in (
                    "l1_handle", "etag", "path", "recorded_at", "superseded_at",
                    "removed_reason", "digest", "content_status", "content_sha256")})
        return out

    def index(self, actor: Actor, audience: LiveSourceAudience, *, limit: int = 5000
              ) -> dict[str, Any]:
        """Current captured versions this reader may see, as identifiers only."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100_000:
            raise CaptureError("limit_invalid")
        items, truncated = [], False
        for row in self._visible_rows(actor, audience):
            if len(items) >= limit:
                truncated = True
                break
            items.append({"drive_id": row["drive_id"], "item_id": row["item_id"],
                          "l1_handle": row["l1_handle"],
                          "content_sha256": row["content_sha256"]})
        return {"items": items, "truncated": truncated, "lineage": LINEAGE}

    def audience_check(self, audience: LiveSourceAudience) -> Callable[[Actor, str], bool]:
        """A ``ReadPolicy`` audience check for the ``sharepoint`` prefix: a captured drive
        where this reader holds any grant; each item is re-gated by :meth:`item_gate`."""
        return lambda actor, drive_id: drive_id in self._drives(actor, audience)

    def item_gate(self, audience: LiveSourceAudience) -> Callable[[Actor, str], bool]:
        """An ``operating.ontology_tools`` ``item_gate``: a ``sharepoint://`` handle is
        readable only when it resolves offline for this reader."""
        return lambda actor, handle: self.resolve(handle, actor, audience,
                                                  text=False) is not None

    def rows(self, actor: Actor, audience: LiveSourceAudience) -> Callable[[str], Any]:
        """A ``claim_checks`` ``Rows`` callable bound to one reader."""
        return lambda handle: self.resolve(handle, actor, audience)

    def completeness(self, actor: Actor, audience: LiveSourceAudience) -> dict[str, Any]:
        """Per captured root this reader may see (its folder keys): walk status, capture
        times and the reader's visible files whose bytes are not captured: over the
        content cap (a terminal outcome, counted apart) or unreadable (incomplete). A
        root with a current file withheld from this reader is not complete for it
        (nothing is counted about that file). A root the reader may not see has no
        entry."""
        roots = []
        if isinstance(actor, Actor) and isinstance(audience, LiveSourceAudience):
            for root in self.db.execute("SELECT * FROM roots ORDER BY drive_id, root_path"):
                if not audience.allows_all(actor, _keys(root["drive_id"], root["root_path"],
                                                        True)):
                    continue
                current = self.db.execute(
                    "SELECT * FROM items WHERE drive_id=? AND root_path=? AND "
                    "superseded_at IS NULL", (root["drive_id"], root["root_path"])).fetchall()
                visible = [r for r in current if self._row_visible(r, actor, audience)]
                exceeds = sum(1 for r in visible if r["content_status"] == EXCEEDS_CAP)
                unavailable = sum(1 for r in visible
                                  if r["content_status"].startswith(_UNAVAILABLE))
                skipped = sum(1 for r in visible if r["content_status"] == CONTENT_SKIPPED)
                withheld = len(visible) < len(current)
                roots.append({
                    "drive_id": root["drive_id"], "root": root["root_path"],
                    "status": root["status"], "reason": root["reason"],
                    "last_run_at": root["last_run_at"],
                    "last_complete_at": root["last_complete_at"],
                    "rounds_complete": root["rounds_complete"],
                    **self.full_walk_age(root["drive_id"], root["root_path"]),
                    "current_files": len(visible), "content_exceeds_cap": exceeds,
                    "content_unavailable": unavailable, "content_skipped": skipped,
                    "withheld_for_reader": withheld,
                    # Listed to the end, unread bytes only from a metadata-only capture.
                    "metadata_complete": ((root["status"] == "complete"
                                           or root["reason"] == CONTENT_NOT_READ)
                                          and not unavailable and not withheld),
                    "complete": (root["status"] == "complete" and not unavailable
                                 and not skipped and not withheld)})
        result: dict[str, Any] = {"lineage": LINEAGE, "roots": roots,
                                  "complete": bool(roots) and all(r["complete"] for r in roots)}
        if roots:
            result["store_bound"] = {"max_bytes": self.max_store_bytes,
                                     "full": self.store_full()}
        return result

    def counts(self, actor: Actor, audience: LiveSourceAudience) -> dict[str, int]:
        versions = list(self._visible_rows(actor, audience, "1=1"))
        current = [r for r in versions if r["superseded_at"] is None]
        return {"drives": len({r["drive_id"] for r in current}),
                "files_current": len(current), "versions": len(versions),
                "contents_captured": sum(1 for r in current
                                         if r["content_status"] == CAPTURED),
                "contents_skipped": sum(1 for r in current
                                        if r["content_status"] == CONTENT_SKIPPED),
                "texts_extracted": sum(1 for r in current if r["text_status"] in TEXT_READ),
                "texts_ocr": sum(1 for r in current if r["text_status"] == TEXT_OCR),
                "texts_ocr_unavailable": sum(1 for r in current
                                             if r["text_status"] == OCR_UNAVAILABLE)}


# ------------------------------------------------------------------ capture

def column_set_version(columns: Sequence[str]) -> str:
    """The listItem reader's column-set version for ``columns``: the reader version and
    a digest of the sorted column names. Another value forces one re-read."""
    digest = sha256(_canonical(sorted(columns)).encode("utf-8")).hexdigest()[:16]
    return f"{LIST_ITEM_READER_VERSION}:{digest}"


def _new_counts() -> dict[str, int]:
    return {key: 0 for key in FOLDER_COUNTS}


class _OneDriveOwners:
    """The people guard over one discovery's personal sites: made ready once (its ID
    list, one directory listing) on the first personal site, so a discovery without one
    reads nothing more."""

    def __init__(self, estate: Any) -> None:
        self._estate, self._ready = estate, None

    def unavailable(self, site: Mapping[str, Any]) -> str | None:
        """None to read the site, ``excluded`` for an excluded owner's OneDrive, else
        the typed gap that withholds it (``people_exclusion:<code>``)."""
        guard = people_exclusion.from_api(self._estate)
        if self._ready is None:
            try:
                prepare = getattr(self._estate, "prepare_people", None)
                if prepare is None:
                    raise people_exclusion.ExclusionUnavailable("excluded_people_oids_absent")
                prepare()
                self._ready = ""
            except people_exclusion.ExclusionUnavailable as exc:
                self._ready = "people_exclusion:" + str(exc.args[0] if exc.args else
                                                        "excluded_people_unresolved")
        if self._ready:
            return self._ready
        if not site.get("personal"):
            return "people_exclusion:onedrive_owner_unresolved"
        try:
            guard.refuse_onedrive_path(site["personal"])
        except people_exclusion.PeopleExcluded:
            return "excluded"
        except people_exclusion.ExclusionUnavailable:
            return "people_exclusion:excluded_people_unresolved"
        return None


DISCOVERY_COUNTS = ("sites_seen", "sites_other_host", "sites_personal", "sites_locked",
                    "sites_excluded", "sites_people_excluded", "sites_people_withheld",
                    "sites_listed",
                    "libraries_seen", "libraries_not_document_library", "libraries_excluded",
                    "libraries_covered_by_target", "libraries_planned",
                    "libraries_personal_planned", "libraries_restricted_unclassified")
FOLDER_COUNTS = ("folders_listed", "files_seen", "versions_new", "versions_superseded",
                 "unchanged", "moved", "reappeared", "absent_after_resync",
                 "moved_out_of_scope", "contents_captured", "content_bytes",
                 "contents_exceeds_cap", "contents_unavailable", "contents_recovered",
                 "texts_extracted", "texts_ocr", "texts_ocr_unavailable", "texts_reextracted",
                 "withheld", "provenance_dropped", "acl_reads", "feed_changes",
                 "permissions_reread", "list_items_read", "list_items_unavailable",
                 "contents_pruned", "list_items_reread", "contents_skipped",
                 "originals_dropped", "ocr_deferred_refetch_failed")


def _count_text(status: str, counts: dict[str, int]) -> None:
    if status in TEXT_READ:
        counts["texts_extracted"] += 1
    if status == TEXT_OCR:
        counts["texts_ocr"] += 1
    elif status == OCR_UNAVAILABLE:
        counts["texts_ocr_unavailable"] += 1


class _Extraction:
    """The text extraction of one walk's new files on ``workers`` threads while the walk
    reads the next files (OCR is local CPU work, no source read). At most ``workers``
    files wait; each one's ``finish`` (its text blob and its version row) runs on the
    walking thread, in the order the files were read, when the oldest is harvested or the
    walk drains them (before it saves its position or ends), so no version is recorded,
    and no walk position holds an item, before its text is. ``workers`` 1: inline."""

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.pool = (ThreadPoolExecutor(workers, thread_name_prefix="capture-text")
                     if workers > 1 else None)
        self.pending: deque[tuple[str, Future, Callable[[tuple], None]]] = deque()

    def holds(self, item_id: str) -> bool:
        return any(waiting == item_id for waiting, _future, _finish in self.pending)

    def submit(self, item_id: str, content: bytes, record: Mapping[str, Any],
               finish: Callable[[tuple], None], *, defer_ocr: bool = False) -> None:
        if self.pool is None:
            finish(capture_text(content, record, defer_ocr=defer_ocr))
            return
        while len(self.pending) >= self.workers:
            self._harvest()
        self.pending.append((item_id, self.pool.submit(capture_text, content, record,
                                                       defer_ocr=defer_ocr), finish))

    def _harvest(self) -> None:
        _item, future, finish = self.pending.popleft()
        finish(future.result())

    def drain(self) -> None:
        while self.pending:
            self._harvest()

    def close(self) -> None:
        """Ends the pool; work not harvested (the walk failed) is dropped unrecorded."""
        for _item, future, _finish in self.pending:
            future.cancel()
        self.pending.clear()
        if self.pool is not None:
            self.pool.shutdown(wait=True)


class SharePointCapture:
    """Runs capture walks for one scope with one capture actor (module docstring).

    ``readers(drive_id)`` returns the ``SharePointReader`` of a drive (built with
    ``read_permissions=True``); ``site_drive(name)`` resolves a site's default library
    (``graph_read.resolve_site_drive``); ``estate`` (``graph_read.SharePointSites``)
    enumerates sites and libraries for a scope's ``discover`` rule. ``budget_seconds``
    bounds the wall time of every run of this object together (0: spent from the start):
    once spent, no further folder, item permission, file, change or change feed is read,
    and the root it cut ends ``incomplete`` with reason ``time_budget``, never reconciled,
    so a slow scope stops in a reported partial state instead of running into a caller's
    timeout. It is asked before every item's permission read too, so one large folder
    cannot outrun it; a walk it cuts resumes from its stored position (module docstring,
    Resumable walks). Every root after it is deferred (:meth:`_deferred`); each walk but a
    refresh is held to its fair slice of it (:meth:`_sliced_walk`; module docstring,
    Scheduling). ``read_content`` False: a metadata-only capture (metadata, item
    permissions and eTags as ever, no content GET); each new file's content is recorded
    ``skipped_metadata_only`` (``contents_skipped``), a terminal outcome for it, and it
    retries no bytes and prunes none."""

    def __init__(self, store: SharePointCaptureStore, readers: Callable[[str], Any],
                 actor: Actor, *, site_drive: Callable[[str], str] | None = None,
                 estate: Any = None, max_content_bytes: int = MAX_CONTENT_BYTES,
                 budget_seconds: float | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 extract_workers: int = EXTRACT_WORKERS,
                 read_content: bool = True) -> None:
        if not isinstance(read_content, bool):
            raise CaptureError("read_content_invalid")
        self.read_content = read_content
        if isinstance(max_content_bytes, bool) or not isinstance(max_content_bytes, int) \
                or not 1 <= max_content_bytes <= MAX_DECLARED_CONTENT_BYTES:
            raise CaptureError("max_content_bytes_invalid")
        if budget_seconds is not None and (isinstance(budget_seconds, bool) or not isinstance(
                budget_seconds, (int, float)) or not budget_seconds >= 0):
            raise CaptureError("budget_seconds_invalid")
        self.store, self.readers, self.actor = store, readers, actor
        self.site_drive, self.estate = site_drive, estate
        self.max_content_bytes = max_content_bytes
        self.list_item_columns: tuple[str, ...] = ()
        self.permission_reread_hours = PERMISSION_REREAD_HOURS
        self.planned: list[tuple[str, str]] = []   # the last run's (drive id, root)
        # A caller's priority (:meth:`_retry_first`): degraded versions whose record it
        # selects get their bytes retried before any root is walked; their counts.
        self.retry_first: Callable[[Mapping[str, Any]], bool] | None = None
        self.first_counts = _new_counts()
        # The handles that first pass tried this run: each is retried at most once a run,
        # so a root's own :meth:`_repair` skips them.
        self.first_tried: set[str] = set()
        # A caller's named items, (drive id, item id), each run captures first
        # (:meth:`_named_pass`); the last run's item failures (drive id, item id, outcome),
        # kept on the capture, never in the run's result (which callers print).
        self.named_items: Sequence[tuple[str, str]] = ()
        self.named_failures: list[tuple[str, str, str]] = []
        self._monotonic, self._sleep = monotonic, sleep
        self._deadline = None if budget_seconds is None else monotonic() + budget_seconds
        # The clock as the latest budget check read it (:meth:`_ocr_now` reads no clock of
        # its own: the walk checks the budget before every file).
        self._checked_at: float | None = None
        self._slice: float | None = None   # the walking root's own deadline (:meth:`run`)
        # The walking slice has read a child or ended a folder: until it has, only the run's
        # deadline stops the walk, so every slice moves it on (:meth:`_sliced_walk`).
        self._slice_moved = True
        self._slice_length = 0.0   # the walking slice's own length (bounds a wait in it)
        self.extract_workers = max(1, extract_workers)
        self._extraction = _Extraction(1)   # the walking root's (:meth:`_walk`)
        # The source's own Retry-After waits honoured (:meth:`_throttled`): count, seconds.
        self.retry_after_waits, self.retry_after_seconds = 0, 0.0

    def _spent(self) -> bool:
        now = self._checked_at = self._monotonic()
        return (self._slice is not None and self._slice_moved and now >= self._slice
                ) or self._run_spent(now)

    def _ocr_now(self) -> bool:
        """Whether an OCR read may start: no budget, or ``OCR_RESERVE_SECONDS`` of it left
        (so the read, bounded by its own deadline, ends before the run's)."""
        if self._deadline is None:
            return True
        now = self._checked_at if self._checked_at is not None else self._monotonic()
        return self._deadline - now >= OCR_RESERVE_SECONDS

    def _run_spent(self, now: float | None = None) -> bool:
        """The run's own budget spent (a root's slice aside)."""
        if now is None and self._deadline is not None:
            now = self._checked_at = self._monotonic()
        return self._deadline is not None and now >= self._deadline

    def _throttled(self, call: Callable[[], Any]) -> Any:
        """``call()``; when the source refused it with 429 or 503 and its own Retry-After
        (beyond the client's capped backoff), waited once and called again, but only when
        that wait ends before the walking root's slice (before it can cut the walk: the
        slice's own length from now; :meth:`_wait_left`) and the run's deadline (a run without a budget: at most ``RETRY_AFTER_MAX_SECONDS``). Any other failure, a
        second refusal or a wait the budget cannot hold is raised as it came, so the item
        stays for the resumed walk or the next run."""
        try:
            return call()
        except SourceUnavailable as exc:
            wait = getattr(exc, "retry_after_seconds", None)
            if (getattr(exc, "http_status", None) not in (429, 503)
                    or isinstance(wait, bool) or not isinstance(wait, (int, float))
                    or not 0 <= wait <= self._wait_left()):
                raise
        self._sleep(wait)
        self.retry_after_waits += 1
        self.retry_after_seconds += wait
        return call()

    def _wait_left(self) -> float:
        """Seconds a wait may take: until the nearer of the slice and the deadline. A
        slice that has not moved its walk yet (:meth:`_sliced_walk`) cannot cut it, but a
        wait in it is still held to the slice's own length from now, so one throttled
        root never waits past its share."""
        now = self._monotonic()
        slice_end = (None if self._slice is None else self._slice if self._slice_moved
                     else max(self._slice, now + self._slice_length))
        ends = [end for end in (slice_end, self._deadline) if end is not None]
        if not ends:
            return RETRY_AFTER_MAX_SECONDS
        return min(ends) - now

    def _sliced_walk(self, roots_left: int | None, reader: Any, drive_id: str, root: str,
                     *, reserve: float = 0.0, least: float = 0.0,
                     **walk: Any) -> dict[str, Any]:
        """:meth:`_walk` within the root's fair share of the budget left: the budget left
        above ``reserve`` (all of it once only the reserve is left) over ``roots_left``,
        the roots of the pass not reached yet, at least ``least`` (never past the run's
        deadline; module docstring, Scheduling). ``roots_left`` None (a refresh,
        decision 28e628682928) or a run without a budget: the run's deadline alone.
        ``slice_cut``: the slice, not the run's budget, cut the walk and its position is
        kept, so a later pass may resume it."""
        if roots_left is not None and self._deadline is not None:
            left = max(0.0, self._deadline - self._monotonic())
            share = (left - reserve if left > reserve else left) / max(1, roots_left)
            self._slice_length = max(least, share)
            self._slice = self._monotonic() + self._slice_length
            # The slice cuts its walk only once this slice moved it on (a child read or a
            # folder ended): a slice shorter than one listing and its first child would
            # otherwise cut the walk where the last one did, every pass and every run.
            # It cuts inside a listing (the resumed walk re-lists the folder and skips
            # the children it holds): one listing's files took 224 s live, at b38701bd1
            # 2026-10-18T10:41Z, when the slice cut only between folders.
            self._slice_moved = False
        try:
            result = self._walk(reader, drive_id, root, **walk)
        finally:
            self._slice, self._slice_moved, self._slice_length = None, True, 0.0
        result["slice_cut"] = (roots_left is not None and result.get("position") is not None
                               and not self._run_spent())
        return result

    # ---------------------------------------------------------------- per file

    def _content(self, reader: Any, record: Mapping[str, Any], counts: dict[str, int]
                 ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        """(content columns, the record the bytes belong to)."""
        columns, source, content = self._bytes(reader, record, counts)
        if content is not None:
            self._text(columns, capture_text(content, source, defer_ocr=not self._ocr_now()),
                       counts)
        return columns, source

    def _text(self, columns: dict[str, Any], extracted: tuple[str, str | None],
              counts: dict[str, int]) -> None:
        status, text = extracted
        columns["text_status"] = status
        if text is not None:
            columns["text_sha256"] = self.store.put_blob(text.encode("utf-8"), kind="text")
        _count_text(status, counts)

    def _bytes(self, reader: Any, record: Mapping[str, Any], counts: dict[str, int],
               listed: bool = False) -> tuple[dict[str, Any], Mapping[str, Any], bytes | None]:
        """(content columns but the text's, the record the bytes belong to, the bytes
        captured or None). ``listed``: ``record`` is the child the reader's listing hands
        over right now, whose metadata and permissions that listing just read: they are
        not read again (``get_content(from_listing=True)``)."""
        if not self.read_content:
            counts["contents_skipped"] += 1
            return {"content_status": CONTENT_SKIPPED, "text_status": "not_read"}, record, None
        size = record.get("size")
        if isinstance(size, int) and size > self.max_content_bytes:
            counts["contents_exceeds_cap"] += 1
            return {"content_status": EXCEEDS_CAP, "text_status": "not_read"}, record, None
        try:
            # A transient download failure is retried once while the budget lasts; a
            # throttled read once after the source's own wait, if the budget holds it.
            read = self._throttled(lambda: reader.get_content(
                self.actor, record["id"], max_bytes=self.max_content_bytes,
                may_retry=lambda: not self._run_spent(), from_listing=listed))
        except NotVisible:
            counts["contents_unavailable"] += 1
            return {"content_status": _UNAVAILABLE + "not_visible",
                    "text_status": "not_read"}, record, None
        except SourceUnavailable as exc:
            counts["contents_unavailable"] += 1
            return {"content_status": _UNAVAILABLE + (unavailable_detail(exc) or "unclassified"),
                    "text_status": "not_read"}, record, None
        if read.get("content_status") != "read" or not isinstance(read.get("content"), bytes):
            counts["contents_exceeds_cap"] += 1
            return ({"content_status": EXCEEDS_CAP, "text_status": "not_read"},
                    read["record"], None)
        content = read["content"]
        digest = self.store.put_blob(content, kind="content")
        counts["contents_captured"] += 1
        counts["content_bytes"] += len(content)
        return {"content_status": CAPTURED, "content_sha256": digest}, read["record"], content

    @staticmethod
    def _kept(record: Mapping[str, Any]) -> dict[str, Any]:
        return {key: record.get(key) for key in RECORD_FIELDS}

    def _capture_file(self, reader: Any, drive_id: str, root: str, listed: Mapping[str, Any],
                      counts: dict[str, int]) -> None:
        counts["files_seen"] += 1
        record = self._kept(listed)
        extraction = self._extraction
        if extraction.holds(record["id"]):
            extraction.drain()   # an earlier version of it waits: recorded first
        keys = _keys(drive_id, record["path"], False)
        outcome = self.store.observe(record, root, keys, listed.get("source_acl") or {})
        if outcome != "new":
            counts["unchanged" if outcome == "seen" else outcome] += 1
            return
        columns, source, content = self._bytes(reader, record, counts, listed=True)
        if source is not listed and source.get("etag") != record["etag"]:
            # The file changed between the listing and the content read: the bytes belong
            # to the newer version, which is the one recorded.
            record = self._kept(source)
            keys = _keys(drive_id, record["path"], False)
            listed = source
            if self.store.observe(record, root, keys, source.get("source_acl") or {}) != "new":
                return
        if self.list_item_columns:
            record = self._with_columns(reader, record, counts)
        acl = listed.get("source_acl") or {}

        def finish(extracted: tuple[str, str | None] | None = None) -> None:
            if extracted is not None:
                self._text(columns, extracted, counts)
            counts["versions_superseded"] += self.store.add_version(record, root, keys, acl,
                                                                    columns)
            counts["versions_new"] += 1
            if content is not None and columns.get("content_sha256"):
                # Compact storage: an original over 256 KiB goes once its text is kept.
                outcome = self.store.settle_original(columns["content_sha256"],
                                                     content=content)
                counts["originals_dropped"] += outcome == "dropped"

        if content is None:
            finish()
        else:
            extraction.submit(record["id"], content, source, finish,
                              defer_ocr=not self._ocr_now())

    def _with_columns(self, reader: Any, record: dict[str, Any], counts: dict[str, int]
                      ) -> dict[str, Any]:
        """The record stamped with the reader's column-set version, and with its listItem
        column snapshot when one of this version reads (else the snapshot it held)."""
        record = {**record, "list_item_columns_version":
                  column_set_version(self.list_item_columns)}
        try:
            read = reader.list_item_fields(self.actor, record["id"],
                                           field_names=self.list_item_columns)
        except (NotVisible, SourceNotFound, SourceUnavailable, ValueError):
            read = None
        if read is None or read["snapshot"].get("drive_etag") != record["etag"]:
            counts["list_items_unavailable"] += 1
            return record
        counts["list_items_read"] += 1
        return {**record, "list_item_fields": {**read["snapshot"],
                                               "field_names": list(self.list_item_columns)}}

    def _reextract(self, drive_id: str, root: str, counts: dict[str, int], *,
                   share: float | None = None, reader: Any = None) -> None:
        """OCR the captured bytes of current scanned PDFs whose pages were never read
        (bounded per walk; no source read), on the extraction workers. Only on a host with
        a local OCR engine, and each read only while :meth:`_ocr_now` (it ends before the
        run's budget); ``share``: none starts once that share of the budget left at the
        call is spent (a walk's first step, so its listing still advances)."""
        from .document_understanding import local_ocr_available

        if not local_ocr_available() or not self._ocr_now():
            return
        until = None
        if share is not None and self._deadline is not None:
            now = self._monotonic()
            until = now + share * max(0.0, self._deadline - now)

        def finish(extracted: tuple[str, str | None], handle: str, original: str,
                   content: bytes) -> None:
            status, text = extracted
            digest = None if text is None else self.store.put_blob(text.encode("utf-8"),
                                                                    kind="text")
            self.store.fill_text(handle, status, digest)
            counts["texts_reextracted"] += 1
            _count_text(status, counts)
            self.store.settle_original(original, content=content)

        # Scans whose OCR was put off with their bytes dropped (``ocr_deferred``): each
        # original re-read by one guarded GET and checked against its pin, never stored.
        deferred = [] if reader is None else self.store.ocr_deferred_rows(
            drive_id, root, REPAIRS_PER_ROOT)
        extraction = _Extraction(self.extract_workers)
        try:
            for row in [*self.store.text_unread(drive_id, root, REPAIRS_PER_ROOT),
                        *deferred]:
                now = self._checked_at = self._monotonic()
                if not self._ocr_now() or (until is not None and now >= until):
                    break
                try:
                    if row["text_status"] == OCR_DEFERRED:
                        content = self._refetch(reader, row)
                        if content is None:
                            counts["ocr_deferred_refetch_failed"] += 1
                            continue
                    else:
                        content = self.store.blob(row["content_sha256"])
                except (CaptureError, OSError):
                    continue   # bytes missing or altered: left as they are, never re-read
                extraction.submit(row["item_id"], content, json.loads(row["record"]),
                                  lambda found, handle=row["l1_handle"],
                                  pin=row["content_sha256"], bytes_=content:
                                  finish(found, handle, pin, bytes_))
            extraction.drain()
        finally:
            extraction.close()

    def _refetch(self, reader: Any, row: Mapping[str, Any]) -> bytes | None:
        """A dropped original of ``row`` read again (one guarded GET) when it still hashes
        to the version's pin, else None."""
        try:
            read = self._throttled(lambda: reader.get_content(
                self.actor, row["item_id"], max_bytes=self.max_content_bytes,
                may_retry=lambda: not self._run_spent()))
        except (NotVisible, SourceNotFound, SourceUnavailable):
            return None
        content = read.get("content")
        if not isinstance(content, bytes) or sha256(content).hexdigest() != \
                row["content_sha256"]:
            return None
        return content

    def _reread_columns(self, reader: Any, drive_id: str, root: str,
                        counts: dict[str, int]) -> None:
        """Read again, once, the columns of the current versions under the root stamped
        with another column-set version (module docstring, Columns), while the run's time
        budget lasts; what is left waits for the next run."""
        if not self.list_item_columns:
            return
        version = column_set_version(self.list_item_columns)
        for row in self.store.columns_stale(drive_id, root, version):
            if self._spent():
                return
            record = self._with_columns(reader, json.loads(row["record"]), counts)
            self.store.reread_columns(row, record)
            counts["list_items_reread"] += 1

    def _where_now(self, reader: Any) -> Callable[[str], Any]:
        """Where an item that left its walked folder is now, read by item id as the
        capture actor (GET metadata and permissions only): its drive-item record (path,
        item permissions), or None when it cannot be read (deleted, not visible, source
        failing)."""
        def where(item_id: str) -> Mapping[str, Any] | None:
            try:
                record = reader.get_item(self.actor, item_id)
            except (NotVisible, SourceNotFound, SourceUnavailable, ValueError):
                return None
            if not isinstance(record, Mapping) or not isinstance(record.get("path"), str):
                return None
            return record
        return where

    def _repair(self, reader: Any, drive_id: str, root: str, counts: dict[str, int]) -> None:
        """Retry the bytes of current versions whose read failed (bounded per walk), but
        none the first pass already tried this run (:meth:`_retry_first`)."""
        rows = self.store.degraded(drive_id, root, REPAIRS_PER_ROOT + len(self.first_tried))
        rows = [r for r in rows if r["l1_handle"] not in self.first_tried]
        for row in rows[:REPAIRS_PER_ROOT]:
            if self._spent():
                return   # the rest waits for a later run
            self._retry_bytes(reader, row, json.loads(row["record"]), counts)

    def _retry_bytes(self, reader: Any, row: Mapping[str, Any], record: Mapping[str, Any],
                     counts: dict[str, int]) -> None:
        """One degraded version's bytes read again. A read that answers another eTag is
        left to the next walk, which lists the change. A metadata-only capture reads none."""
        if not self.read_content:
            return
        scratch = _new_counts()
        columns, source = self._content(reader, record, scratch)
        if columns["content_status"].startswith(_UNAVAILABLE):
            return
        if source.get("etag") != row["etag"]:
            return
        self.store.fill_content(row["l1_handle"], columns)
        counts["contents_recovered"] += 1
        for key in ("contents_captured", "content_bytes", "texts_extracted", "texts_ocr",
                    "texts_ocr_unavailable", "contents_exceeds_cap"):
            counts[key] += scratch[key]

    def _named_pass(self, plans: Sequence[tuple]) -> dict[str, Any] | None:
        """Capture every named item (``named_items``) before any root is walked (module
        docstring, Named items first): only an item of a drive this run planned (the
        scope, discovery's exclusions and the people exclusion decided that), read by id
        (``get_item``: metadata and item permissions; children never), and only when
        the drive's planned roots hold its path; then recorded by :meth:`_capture_file`,
        exactly as a listed child. An item held as current and seen within
        ``permission_reread_hours`` is not read again (resumable, idempotent). No root
        status, walk position or feed position is touched. At most ``NAMED_PASS_SHARE``
        of the budget left. Every item ends in one typed outcome (``outcomes``); the
        failures are kept by item in ``named_failures``. None without named items."""
        self.named_failures = []
        if not self.named_items:
            return None
        outcomes = {key: 0 for key in NAMED_OUTCOMES}
        counts = _new_counts()
        drives: dict[str, tuple[Any, list[str]]] = {}
        for _entry, reader, drive_id, root, _target in plans:
            drives.setdefault(drive_id, (reader, []))[1].append(root)
        seen: set[tuple[str, str]] = set()
        wanted: list[tuple[str, str]] = []
        for number, pair in enumerate(self.named_items):
            if number >= MAX_NAMED_ITEMS:
                outcomes["over_bound"] = len(self.named_items) - MAX_NAMED_ITEMS
                break
            if not (isinstance(pair, (tuple, list)) and len(pair) == 2
                    and all(isinstance(p, str) and p for p in pair)):
                outcomes["invalid"] += 1
                continue
            pair = (pair[0], pair[1])
            if pair not in seen:
                seen.add(pair)
                wanted.append(pair)

        def fail(drive_id: str, item_id: str, outcome: str) -> None:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            self.named_failures.append((drive_id, item_id, outcome))

        due: list[tuple[str, str, str]] = []   # (last seen or "", drive id, item id)
        fresh = timedelta(hours=self.permission_reread_hours)
        for drive_id, item_id in wanted:
            if drive_id not in drives:
                fail(drive_id, item_id, "out_of_scope")   # never read
                continue
            held = self.store.current(drive_id, item_id)
            if held is not None and held["removed_reason"] is None:
                if self._age(held["last_seen_at"]) < fresh:
                    outcomes["held_fresh"] += 1
                    continue
                due.append((held["last_seen_at"], drive_id, item_id))
            else:
                due.append(("", drive_id, item_id))
        due.sort()   # never held first, then the least recently seen
        recorded: list[tuple[str, str, int]] = []   # (drive, item, observe counts before)

        def observed() -> int:
            return counts["unchanged"] + counts["moved"] + counts["reappeared"]

        if self._deadline is not None:
            now = self._monotonic()
            self._slice = now + max(0.0, self._deadline - now) * NAMED_PASS_SHARE
        self._extraction = _Extraction(self.extract_workers)
        left = 0
        try:
            for left, (_seen, drive_id, item_id) in enumerate(due):
                if self._spent():
                    for _s, drive, item in due[left:]:
                        fail(drive, item, TIME_BUDGET)
                    break
                reader, roots = drives[drive_id]
                try:
                    record = self._throttled(lambda: reader.get_item(self.actor, item_id))
                except NotVisible:
                    fail(drive_id, item_id, "not_visible")
                    continue
                except SourceNotFound:
                    fail(drive_id, item_id, "not_found")
                    continue
                except SourceUnavailable as exc:
                    fail(drive_id, item_id,
                         "source_error:" + (unavailable_detail(exc) or "unclassified"))
                    continue
                except ValueError:
                    fail(drive_id, item_id, "invalid")
                    continue
                if not isinstance(record, Mapping) or record.get("kind") != "file":
                    fail(drive_id, item_id, "not_file")
                    continue
                path = record.get("path")
                root = next((r for r in roots if isinstance(path, str) and _in_root(path, r)),
                            None)
                if root is None:
                    fail(drive_id, item_id, "outside_root")   # nothing of it is written
                    continue
                before = observed()
                self._capture_file(reader, drive_id, root, record, counts)
                recorded.append((drive_id, item_id, observed() - before))
            else:
                left = len(due)
            self._extraction.drain()
        except CaptureError as exc:
            if exc.code != "store_full":
                raise
            done = {(d, i) for d, i, _o in recorded}
            for _s, drive, item in due[left:]:
                if (drive, item) not in done:
                    fail(drive, item, "store_full")
        finally:
            self._extraction.close()
            self._extraction = _Extraction(1)
            self._slice = None
        for drive_id, item_id, seen_again in recorded:
            if seen_again:
                outcomes["unchanged"] += 1
            elif self.store.current(drive_id, item_id) is not None:
                outcomes["captured"] += 1
            else:
                fail(drive_id, item_id, "not_recorded")
        return {"named": len(self.named_items), "outcomes": outcomes, "counts": counts}

    def _retry_first(self, plans: Sequence[tuple]) -> None:
        """Before any walk, in the run's root order, retry the bytes of every degraded
        current version ``retry_first`` selects (a caller's priority: the container check
        spends it on containers whose bytes a cut earlier run could not read before
        resuming any walk), however many unselected ones precede them. Each is tried at
        most once a run (``first_tried``; the root's own :meth:`_repair` skips it) and the
        pass spends at most half the budget left (``FIRST_PASS_SHARE``), so the walks
        always advance. Counted in ``first_counts``."""
        self.first_tried = set()
        if self._deadline is not None:
            now = self._monotonic()
            self._slice = now + max(0.0, self._deadline - now) * FIRST_PASS_SHARE
        try:
            for _entry, reader, drive_id, root, _target in plans:
                after = 0
                while True:
                    page = self.store.degraded(drive_id, root, REPAIRS_PER_ROOT, after=after)
                    if not page:
                        break
                    after = page[-1]["version_id"]
                    for row in page:
                        if row["l1_handle"] in self.first_tried:
                            continue
                        record = json.loads(row["record"])
                        if not self.retry_first(record):
                            continue
                        if self._spent():
                            return
                        self.first_tried.add(row["l1_handle"])
                        self._retry_bytes(reader, row, record, self.first_counts)
        finally:
            self._slice = None

    # ---------------------------------------------------------------- per root

    def _walk(self, reader: Any, drive_id: str, root: str, *, max_depth: int,
              max_folders: int, why: str = "first_walk", cursor: str | None = None,
              cursor_read_at: str | None = None) -> dict[str, Any]:
        """List every folder under ``root`` (breadth first, bounded) and capture its
        files; every listed folder is recorded by item id for the change feed. A walk the
        time budget cuts keeps its position and the root's next run resumes it (``why``
        ``resumed``; module docstring, Resumable walks); ``cursor`` is the drive's
        change-feed cursor read before a new walk (None: the walk takes one as it starts),
        ``cursor_read_at`` when it was read. A walk of a root whose last full walk was
        complete that is only due again (``walk_due``) is a refresh: the root keeps its
        state while the refresh runs (module docstring, Refresh walks).
        Every folder below the root is listed by its item id, so one renamed or moved
        since it was queued is listed where it is now (outside the root: left). Returns
        the walk state :meth:`_finish` completes (``why``: the reason it is a full walk)."""
        store = self.store
        counts = _new_counts()
        position = store.walk_position(drive_id, root)
        if position is None:
            refresh = why == "walk_due"
            started, reasons, listed = store.now(), set(), 0
            queue: list[dict[str, Any]] = [{"id": None, "path": root, "depth": 0,
                                            "parent": None, "listed": False, "held": []}]
            store.forget_folders(drive_id, root)
            if cursor is None and not self._spent():
                cursor_read_at = store.now()
                cursor = self._fresh_cursor(reader, drive_id)
        else:
            started, cursor, why = position["started_at"], position["delta_url"], "resumed"
            cursor_read_at = position.get("cursor_read_at") or started
            reasons, listed = set(position["reasons"]), position["folders_listed"]
            queue, refresh = position["queue"], bool(position.get("refresh"))
        if refresh:
            store.save_root(drive_id, root, round_started_at=started)
        else:
            store.save_root(drive_id, root, status="in_progress", reason=None,
                            round_started_at=started)
        cut, continued = False, None
        if not self._spent():
            # Scanned pages a cut run left unread (``ocr_pending``) first, within a share.
            self._reextract(drive_id, root, counts, share=REEXTRACT_SHARE, reader=reader)
        self._extraction = _Extraction(self.extract_workers)
        try:
            while queue:
                folder = queue[0]
                if self._spent():
                    cut = True
                    break
                if not folder["listed"] and listed >= max_folders:
                    reasons.add("folder_bound")
                    break
                held: set[str] = set(folder["held"])
                subfolders: list[tuple[str, str]] = []

                def each(item: dict[str, Any], held: set[str] = held,
                         subfolders: list[tuple[str, str]] = subfolders) -> bool | None:
                    # Each child as soon as its permissions are read (never without them);
                    # a folder that left the root since it was queued holds none of it.
                    if not _in_root(item["path"], root):
                        return None
                    if item.get("kind") == "folder":
                        subfolders.append((item["id"], item["path"]))
                    elif item.get("kind") == "file":
                        if self._spent():
                            return False   # unread: the resumed listing starts at it
                        self._capture_file(reader, drive_id, root, item, counts)
                    held.add(item["id"])
                    self._slice_moved = True
                    return None

                try:
                    # A folder whose listing stopped at its caps or a budget cut resumes
                    # at the start of its stored next page (``list_folder`` ``start``);
                    # the children it holds are skipped, so a child deleted before the
                    # resume point between runs shifts no unread child past it.
                    listing = self._throttled(lambda: reader.list_folder(
                        self.actor, folder["path"], page_size=LIST_PAGE_SIZE,
                        max_pages=LIST_MAX_PAGES, max_items=LIST_MAX_ITEMS, stop=self._spent,
                        skip=frozenset(held), each=each, folder_id=folder.get("id"),
                        start=folder.get("next")))
                except NotVisible:
                    reasons.add("not_visible")
                    queue.pop(0)
                    continue
                except SourceUnavailable as exc:
                    reasons.add("source_error:" + (unavailable_detail(exc) or "unclassified"))
                    queue.pop(0)
                    continue
                if not _in_root(listing["path"], root):
                    queue.pop(0)   # moved out of the root since it was queued
                    continue
                if folder is not continued:   # its listing continued is the same folder
                    counts["folders_listed"] += 1
                if not folder["listed"]:
                    folder["listed"] = True
                    listed += 1
                store.note_folder(drive_id, listing["folder_id"], root, listing["path"],
                                  listing["path"].rsplit("/", 1)[-1] or None,
                                  folder["parent"], folder["depth"])
                done = listing["completeness"]
                resume = done.get("next")
                # The budget spent inside the listing (its permission reads or a file):
                # the folder stays first in the position with the children it holds.
                stopped = bool(done.get("budget_cut"))
                if not (resume or done.get("budget_cut") or done.get("complete", False)):
                    reasons.add("listing_truncated")
                if done.get("withheld_count", 0) - done.get("provenance_dropped", 0) > 0:
                    reasons.add("withheld_from_capture")
                if done.get("provenance_dropped", 0):
                    reasons.add("provenance_dropped")
                if done.get("acl_reads_capped"):
                    reasons.add("acl_read_bound")
                counts["withheld"] += done.get("withheld_count", 0)
                counts["provenance_dropped"] += done.get("provenance_dropped", 0)
                counts["acl_reads"] += done.get("acl_reads", 0)
                for folder_id, path in subfolders:
                    if folder["depth"] + 1 <= max_depth:
                        queue.append({"id": folder_id, "path": path,
                                      "depth": folder["depth"] + 1,
                                      "parent": listing["folder_id"], "listed": False,
                                      "held": []})
                    else:
                        reasons.add("depth_bound")
                if resume:
                    # Everything before the resume point is handled: the folder (by its
                    # id) continues there, this run or the next.
                    folder.update(id=listing["folder_id"], held=sorted(held),
                                  next={"url": resume["url"], "offset": 0})
                elif stopped:
                    folder["held"] = sorted(held)
                if stopped:
                    cut = True
                    break
                if resume:
                    continued = folder
                    continue
                queue.pop(0)
                self._slice_moved = True
            # Every file read is recorded before the walk's position or end is.
            self._extraction.drain()
        except CaptureError as exc:
            if exc.code != "store_full":
                raise
            reasons.add("store_full")
            cut = False   # ends the walk, as a full store did before any cut
        finally:
            self._extraction.close()
            self._extraction = _Extraction(1)
        walk = {"reader": reader, "drive_id": drive_id, "root": root, "started": started,
                "reasons": reasons | ({TIME_BUDGET} if cut else set()), "counts": counts,
                "round": "full", "why": why, "position": None, "refresh": refresh}
        if cut and (position is not None or listed or refresh):
            # Partial: the next run resumes here; its gaps so far stay with it, so the
            # walk that ends is complete (and reconciles) only if no run of it had one.
            # A refresh keeps its position even before it listed anything: its walked
            # folders are forgotten, so its root may not follow the feed meanwhile.
            store.save_walk_position(drive_id, root, started, cursor, {
                "queue": queue, "reasons": sorted(reasons), "folders_listed": listed,
                "cursor_read_at": cursor_read_at, "refresh": refresh})
            walk["position"] = {"folders_listed": listed, "folders_left": len(queue),
                                "items_held": len(queue[0]["held"])}
        elif not cut:
            if position is not None:
                store.save_walk_position(drive_id, root, started, None, None)
            walk["walk_cursor"] = cursor   # the run stores it (see :meth:`run`)
            walk["cursor_read_at"] = cursor_read_at or started
        return walk

    def _follow(self, reader: Any, drive_id: str, root: str, changes: Sequence[Mapping], *,
                reread_since: str | None = None) -> dict[str, Any] | None:
        """Apply the drive's change-feed entries to one root whose last full walk was
        complete (module docstring, Incremental). ``reread_since``: its permission
        position is older than the bound, so an entry whose version is captured already is
        read again too, unless its permissions were read at or after this time (the first
        run of the re-read), after the changed entries (module docstring, Permission
        re-reads). ``None`` when an entry needs a full walk instead; nothing is reconciled
        here."""
        store = self.store
        folders = store.walked_folders(drive_id, root)
        files: list[Mapping[str, Any]] = []
        rereads: list[Mapping[str, Any]] = []
        for change in changes:
            known = folders.get(change["id"])
            if known is not None:
                if (change["deleted"] or change["shared_changed"]
                        or (known["parent_id"] is not None
                            and change["parent_id"] != known["parent_id"])
                        or (change["name"] is not None and known["name"] is not None
                            and change["name"] != known["name"])):
                    return None   # a walked folder renamed, moved, deleted or re-shared
                continue
            current = store.current(drive_id, change["id"])
            in_root = current is not None and current["root_path"] == root
            if change["deleted"]:
                if in_root:
                    return None   # a captured file deleted (a deleted entry may lack its
                continue          # file facet): the full walk reconciles it
            if change["kind"] == "folder" and change["parent_id"] in folders:
                return None       # a folder new under a walked one (created or moved in)
            if change["kind"] != "file":
                continue
            if change["parent_id"] in folders:
                parent = folders[change["parent_id"]]["path"]
                if (in_root and change["etag"] is not None
                        and change["etag"] == current["etag"]
                        and not change["shared_changed"]
                        and change["name"] in (None, current["path"].rsplit("/", 1)[1])
                        and current["path"].rsplit("/", 1)[0] == parent.rstrip("/")):
                    if (reread_since is None
                            or current["last_seen_at"] >= reread_since):
                        continue  # applied already: this version is captured where it is
                    rereads.append(change)   # its permissions may have changed unannotated
                    continue
                files.append(change)
            elif in_root:
                return None       # moved out of the walked folders
        counts = _new_counts()
        counts["permissions_reread"] = len(rereads)
        files += rereads
        started = store.now()
        store.save_root(drive_id, root, status="in_progress", reason=None,
                        round_started_at=started)
        reasons: set[str] = set()
        cut = False
        try:
            for change in files:
                if self._spent():
                    # The root stays complete: its position is kept, and the next follow
                    # passes over the entries applied here (their eTags are captured).
                    cut = True
                    break
                try:
                    record = reader.get_item(self.actor, change["id"])
                except (NotVisible, SourceNotFound, SourceUnavailable):
                    return None   # withheld or gone since: the full walk records which
                if record.get("kind") != "file" or not _under(record["path"], root):
                    return None
                counts["feed_changes"] += 1
                self._capture_file(reader, drive_id, root, record, counts)
        except CaptureError as exc:
            if exc.code != "store_full":
                raise
            return {"reader": reader, "drive_id": drive_id, "root": root, "started": started,
                    "reasons": {"store_full"}, "counts": counts, "round": "incremental",
                    "why": None}
        return {"reader": reader, "drive_id": drive_id, "root": root, "started": started,
                "reasons": reasons, "counts": counts, "round": "incremental", "why": None,
                "cut": cut, "reread": reread_since is not None}

    def _will_walk(self, drive_id: str, root: str, feed: Mapping[str, Any],
                   hours: int) -> bool:
        """The root's run is a walk (``capture``'s choice before it follows a change
        feed): a stored walk position, a full walk due, an expired or missing cursor."""
        if self.store.walk_position(drive_id, root) is not None:
            return True
        return (self._full_walk_due(drive_id, root, hours) is not None
                or root in feed["expire"] or feed["changes"] is None
                or self.store.position(drive_id, root) is None)

    def _full_walk_due(self, drive_id: str, root: str, hours: int) -> str | None:
        """Why this root's run must be a full walk, or ``None`` when its drive's change
        feed may carry it (its last full walk complete, or lacking only bytes, and
        younger than ``hours``)."""
        walked = self.store.walked_at(drive_id, root)
        if walked is None:
            return "first_walk"
        state = self.store.root_state(drive_id, root) or {}
        if state.get("status") != "complete" and state.get("reason") != CONTENT_INCOMPLETE:
            return "incomplete_before"
        if self._age(walked) >= timedelta(hours=hours):
            return "walk_due"
        if (self.store.position(drive_id, root) is not None
                and self.store.permission_position(drive_id, root) is None):
            return "permissions_unknown"   # nothing says which entries were re-read
        return None

    def _age(self, stamp: str) -> timedelta:
        return self.store._clock() - datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ"
                                                       ).replace(tzinfo=timezone.utc)

    def _reread_due(self, drive_id: str, root: str) -> bool:
        """The root's permission position is older than ``permission_reread_hours``
        (read time unknown: the oldest), so its next follow re-reads every entry."""
        held = self.store.permission_position(drive_id, root)
        return (held is None or held["read_at"] is None
                or self._age(held["read_at"]) >= timedelta(hours=self.permission_reread_hours))

    def _feed(self, reader: Any, drive_id: str, cursor: str | None
              ) -> tuple[list[dict[str, Any]], str]:
        """(the drive's change entries since ``cursor``, last state per item; the new
        cursor). ``cursor`` None reads only a fresh cursor (``token=latest``)."""
        latest: dict[str, dict[str, Any]] = {}
        delta_url = None
        for page in reader.drive_delta(self.actor, cursor=cursor):
            for item in page["items"]:
                latest.pop(item["id"], None)
                latest[item["id"]] = item
            delta_url = page["delta_url"]
        return list(latest.values()), delta_url

    def _follows(self, drive_id: str, root: str, hours: int) -> dict[str, Any] | None:
        """The root's feed position when its next run may follow the change feed (its
        last full walk complete, or lacking only bytes, and not due; no walk cut)."""
        if (self.store.walk_position(drive_id, root) is not None
                or self._full_walk_due(drive_id, root, hours) is not None):
            return None
        return self.store.position(drive_id, root)

    def _feeds(self, plans: Sequence[tuple], hours: int) -> dict[str, dict[str, Any]]:
        """Per drive of the run, one feed read before any walk, from the oldest position
        of its roots that follow it (module docstring, Incremental): the change entries
        (``None`` when none follows), the new cursor and when it was read (each root that
        applies every entry moves to it; a new walk starts from it), why the feed could
        not carry a root, ``expire``: the following roots whose position the feed
        refused (HTTP 410 or unreadable), and ``reread``: the following roots whose
        permission position is due, read from it (module docstring, Permission
        re-reads)."""
        feeds: dict[str, dict[str, Any]] = {}
        for _entry, reader, drive_id, _root, _target in plans:
            if drive_id in feeds:
                continue
            roots = [p[3] for p in plans if p[2] == drive_id]
            self.store.seed_positions(drive_id, roots)
            following = {r: f for r in roots if (f := self._follows(drive_id, r, hours))}
            reread = [r for r in following if self._reread_due(drive_id, r)]
            feed: dict[str, Any] = {"changes": None, "cursor": None, "read_at": None,
                                    "why": None, "expire": [], "reread": reread}
            feeds[drive_id] = feed
            if self._spent():
                feed["why"] = TIME_BUDGET   # every root of the drive is deferred
                continue
            feed["read_at"] = self.store.now()
            try:
                if following:
                    starts = [*following.values(), *(
                        self.store.permission_position(drive_id, r) for r in reread)]
                    oldest = min(starts, key=lambda f: f["read_at"] or "")
                    feed["changes"], feed["cursor"] = self._feed(reader, drive_id,
                                                                 oldest["delta_url"])
                else:
                    feed["cursor"] = self._feed(reader, drive_id, None)[1]
            except (NotVisible, SourceNotFound, SourceUnavailable):
                feed.update(changes=None, why="feed_unavailable", read_at=self.store.now(),
                            cursor=self._fresh_cursor(reader, drive_id),
                            expire=sorted(following))
        return feeds

    def _fresh_cursor(self, reader: Any, drive_id: str) -> str | None:
        """The drive's current change-feed cursor (``token=latest``), or None."""
        try:
            return self._feed(reader, drive_id, None)[1]
        except (NotVisible, SourceNotFound, SourceUnavailable):
            return None

    def _finish(self, walk: Mapping[str, Any]) -> dict[str, Any]:
        """Reconcile a walk that listed everything, retry unreadable bytes and record the
        root's state. Runs after every root of the run was walked, so a file moved from
        one walked root to another is seen at its new path before anything is called
        absent."""
        store = self.store
        drive_id, root, reasons = walk["drive_id"], walk["root"], set(walk["reasons"])
        counts = walk["counts"]
        try:
            if not reasons and walk["round"] == "full":
                # Everything under the root was listed: what the walk did not see is gone
                # from where it was; a file moved into a folder it had listed is captured
                # where it is now.
                gone, within = store.reconcile_root(drive_id, root, walk["started"],
                                                    where_now=self._where_now(walk["reader"]))
                for reason, number in gone.items():
                    counts[reason] += number
                for record in within:
                    if self._spent():
                        # Left for a later run (re-gated where it is now, its path not
                        # yet): the root is not complete until a walk captures it there.
                        reasons.add(TIME_BUDGET)
                        break
                    self._capture_file(walk["reader"], drive_id, root, record, counts)
            if not self._spent():   # byte retries and OCR re-reads wait for a later run
                self._repair(walk["reader"], drive_id, root, counts)
                self._reextract(drive_id, root, counts, reader=walk["reader"])
                self._reread_columns(walk["reader"], drive_id, root, counts)
        except CaptureError as exc:
            if exc.code != "store_full":
                raise
            reasons.add("store_full")
        state = store.root_state(drive_id, root) or {}
        if walk.get("refresh") and set(walk["reasons"]) == reasons == {TIME_BUDGET}:
            # A refresh the time budget alone cut (module docstring, Refresh walks): the
            # root keeps the state of its last full walk; the cut is reported apart.
            store.save_root(drive_id, root, last_run_at=store.now())
            return {"status": state["status"], "complete": state["status"] == "complete",
                    "reason": state["reason"], "round": walk["round"],
                    "full_walk_reason": walk["why"], "walk_position": walk.get("position"),
                    "counts": counts, "deferred": True, "refresh_cut": True}
        if not reasons and store.content_incomplete(drive_id, root,
                                                    read_content=self.read_content):
            # The walk listed everything, but some current files' bytes could not be
            # read (a file over the cap is a terminal outcome): incomplete, never absent.
            reasons.add(CONTENT_INCOMPLETE)
        if not reasons and store.content_skipped(drive_id, root):
            # A metadata-only capture listed everything but read no bytes: the root is
            # metadata-complete only, never content-complete (and never stamped walked).
            reasons.add(CONTENT_NOT_READ)
        walked = not (reasons - {CONTENT_INCOMPLETE})
        if walked and walk["round"] == "full":
            # Due again full_walk_hours after it ended, however many runs it took.
            store.save_walked(drive_id, root, store.now())
            store.retire_nested_roots(drive_id, root)
        if not reasons and walk["round"] == "full" and self.read_content:
            # The root's last complete capture is this walk's (every current file's bytes
            # read): older versions' bytes go. An incomplete capture prunes nothing.
            counts["contents_pruned"] += store.prune_superseded(drive_id, root,
                                                                walk["started"])
        reason = ",".join(sorted(reasons)) or None
        now = store.now()
        store.save_root(drive_id, root, status="incomplete" if reasons else "complete",
                        reason=reason, last_run_at=now,
                        rounds_complete=state.get("rounds_complete", 0) + walked,
                        **({} if reasons else {"last_complete_at": now}))
        return {"status": "incomplete" if reasons else "complete", "complete": not reasons,
                "reason": reason, "round": walk["round"], "full_walk_reason": walk["why"],
                "walk_position": walk.get("position"), "counts": counts}

    def _standing(self, drive_id: str, root: str) -> tuple[int, str]:
        """The root's :func:`capture_mail.schedule_units` standing: a cut walk (a cut
        refresh of a complete root too) by when it started, a complete root by when its
        feed position was read (unknown: oldest)."""
        walk = self.store.walk_position(drive_id, root)
        if walk is not None:
            # Before never visited roots: a refresh's deletions and permission changes
            # apply before new roots grow the estate (module docstring, Refresh walks).
            return FRONTIER, walk["started_at"]
        state = self.store.root_state(drive_id, root)
        if state is None:
            return PENDING, ""
        if state["status"] == "complete":
            position = self.store.position(drive_id, root)
            return COMPLETE, (position or {}).get("read_at") or ""
        if terminal_reason(state["reason"]):
            return TERMINAL, ""
        return PENDING, ""

    def _deferred(self, drive_id: str, root: str, hours: int) -> dict[str, Any]:
        """A root the time budget reached before reading anything (``deferred``): nothing
        is read, and its state, walked folders, walk position and feed position stay as
        they are (a root never seen before gets its row, cut, so the store counts it), so
        it is scheduled before the roots that spent this run. It stays complete when it
        was (its next run follows its drive's change feed, or walks when due), keeps a
        terminal reason, and is otherwise cut (``time_budget``)."""
        state = self.store.root_state(drive_id, root)
        if state is None:
            self.store.save_root(drive_id, root, status="incomplete", reason=TIME_BUDGET)
            state = {}
        position = self.store.walk_position(drive_id, root)
        refresh = position is not None and bool(position.get("refresh"))
        if (position is None or refresh) and state.get("status") == "complete":
            complete, reason = True, None
        elif position is None and terminal_reason(state.get("reason")):
            complete, reason = False, state["reason"]
        elif refresh:   # a cut refresh keeps the root's state (content_incomplete)
            complete, reason = False, state.get("reason")
        else:
            complete, reason = False, TIME_BUDGET
        follows = self._follows(drive_id, root, hours) is not None
        return {"status": "complete" if complete else "incomplete", "complete": complete,
                "reason": reason, "round": None, "full_walk_reason": None, "deferred": True,
                "walk_position": None if position is None else {
                    "folders_listed": position["folders_listed"],
                    "folders_left": len(position["queue"]),
                    "items_held": len(position["queue"][0]["held"])},
                "counts": _new_counts(),
                "cursor": ("walk_position" if position is not None
                           else "drive_delta" if follows else None)}

    def capture_root(self, reader: Any, drive_id: str, root: str, *, max_depth: int,
                     max_folders: int) -> dict[str, Any]:
        """One root walked and finished on its own (``run`` walks all roots first)."""
        return self._finish(self._walk(reader, drive_id, root, max_depth=max_depth,
                                       max_folders=max_folders))

    # ---------------------------------------------------------------- per run

    def run(self, scope: CaptureScope) -> dict[str, Any]:
        """One capture run over the scope; per-target and per-root outcomes plus
        ``complete_bound`` / ``bound_total`` over every scope root (a root of a target
        that could not be resolved is bound and incomplete, with the target's reason).
        A ``discover`` rule adds one target (``discovered``, root ``/``) per library it
        finds (:meth:`_discover`) and one incomplete bound per discovery gap. Each root
        either follows its drive's change feed or is walked, and every root is done
        before any is reconciled (see :meth:`_finish`)."""
        self.list_item_columns = scope.list_item_columns
        self.permission_reread_hours = scope.permission_reread_hours
        targets: list[dict[str, Any]] = []
        plans: list[tuple[dict[str, Any], Any, str, str, CaptureTarget]] = []
        walks: list[tuple[dict[str, Any], dict[str, Any]]] = []
        claimed: dict[str, list[str]] = {}   # drive id -> roots of earlier targets
        for index, target in enumerate(scope.targets):
            entry: dict[str, Any] = {"index": index, "roots": []}
            targets.append(entry)
            try:
                drive_id = target.drive_id or self._site(target.site)
            except SourceNotFound:
                entry.update(status="site_not_found", complete=False)
                continue
            except (SourceUnavailable, NotVisible, ValueError) as exc:
                detail = unavailable_detail(exc) if isinstance(exc, SourceUnavailable) else None
                entry.update(status="source_error:" + (detail or "site_unresolved"),
                             complete=False)
                continue
            earlier = claimed.setdefault(drive_id, [])
            if any(_under(a, b) or _under(b, a) for a in target.folders for b in earlier):
                # Two targets on one drive whose roots nest: nothing of this target is
                # walked (one item would be attributed to two roots).
                entry.update(status="scope_root_nested", complete=False)
                continue
            earlier.extend(target.folders)
            reader = self.readers(drive_id)
            refused = self._classify(scope, reader, drive_id, target.site)
            if refused is not None:
                entry.update(status=refused, complete=False)
                continue
            entry["drive_id"] = drive_id
            for root in target.folders:
                plans.append((entry, reader, drive_id, root, target))
        discovery = (None if scope.discover is None
                     else self._discover(scope, claimed, targets, plans))
        # The (drive id, root) this run planned: kept on the capture, never in the run's
        # result (which callers print), for the CLI's cross-run progress.
        self.planned = [(p[2], p[3]) for p in plans]
        # The items a caller names first (module docstring, Named items first): before
        # any root, without touching a root's state.
        named = self._named_pass(plans)
        # The capture scheduler's order (capture_mail.schedule_units, shared with mail).
        ordered = schedule_units(plans, lambda plan: self._standing(plan[2], plan[3]))
        if self.retry_first is not None:
            self._retry_first(ordered)
        feeds = self._feeds(ordered, scope.full_walk_hours)
        # Which roots this run walks (resumed, first, due or uncursored), not follows: a
        # root's fair slice is the budget left over the walks after it, never over the
        # complete roots its change feed carries in a moment.
        walking = [self._will_walk(drive_id, root, feeds[drive_id], scope.full_walk_hours)
                   for _entry, _reader, drive_id, root, _target in ordered]
        results: dict[tuple[int, str], dict[str, Any]] = {}
        bounds: list[CaptureTarget] = []   # each walk's target (its depth and folder bounds)
        # The budget the first pass leaves for the walks its slices cut.
        reserve = (0.0 if self._deadline is None else (1 - FIRST_WALK_PASS_SHARE)
                   * max(0.0, self._deadline - self._monotonic()))
        # The walk a run cut that started earliest (first in the schedule) resumes with at
        # least ROOT_SLICE_MIN_SECONDS, so a root larger than one budget still completes
        # within a bounded number of runs; only it, so it cannot starve the rest.
        oldest_floored = False
        for number, (entry, reader, drive_id, root, target) in enumerate(ordered):
            feed, walk = feeds[drive_id], None
            if self._spent():
                results[(entry["index"], root)] = self._deferred(drive_id, root,
                                                                 scope.full_walk_hours)
                continue
            position = self.store.walk_position(drive_id, root)
            if position is not None:
                why = "resumed"
            else:
                why = self._full_walk_due(drive_id, root, scope.full_walk_hours)
                if why is None and root in feed["expire"]:
                    why = feed["why"]
                elif why is None and (feed["changes"] is None
                                      or self.store.position(drive_id, root) is None):
                    why = feed["why"] or "no_cursor"
                if why is None:
                    held = (self.store.permission_position(drive_id, root)
                            if root in feed["reread"] else None)
                    walk = self._follow(reader, drive_id, root, feed["changes"],
                                        reread_since=None if held is None
                                        else held["reread_at"] or feed["read_at"])
                    why = "structural_change"
            if walk is None:
                # A refresh is never sliced (decision 28e628682928: it resumes ahead of
                # the roots no walk has reached); any other walk gets its share.
                refresh = (bool(position.get("refresh")) if position is not None
                           else why == "walk_due")
                # This root walks (a change feed can force a complete one to), and so
                # do the planned walks after it.
                floor = why == "resumed" and not refresh and not oldest_floored
                oldest_floored = oldest_floored or floor
                walk = self._sliced_walk(None if refresh else 1 + sum(walking[number + 1:]),
                                         reader, drive_id, root, reserve=reserve,
                                         least=ROOT_SLICE_MIN_SECONDS if floor else 0.0,
                                         max_depth=target.max_depth,
                                         max_folders=target.max_folders, why=why,
                                         cursor=feed["cursor"], cursor_read_at=feed["read_at"])
            walks.append((entry, walk))
            bounds.append(target)
        for _pass in range(ROOT_PASSES_MAX):
            # Budget left after every root had its share: the walks a slice cut resume,
            # the fewest folders left first, each with its share of what is left (at
            # least ROOT_SLICE_MIN_SECONDS, so the budget completes walks, not spreads).
            again = sorted((k for k, (_e, w) in enumerate(walks) if w.get("slice_cut")),
                           key=lambda k: (walks[k][1]["position"]["folders_left"], k))
            if not again or self._spent():
                break
            for number, k in enumerate(again):
                if self._spent():
                    break
                entry, earlier = walks[k]
                walk = self._sliced_walk(len(again) - number, earlier["reader"],
                                         earlier["drive_id"], earlier["root"],
                                         least=ROOT_SLICE_MIN_SECONDS,
                                         max_depth=bounds[k].max_depth,
                                         max_folders=bounds[k].max_folders)
                # One root, one result: what every slice read, the outcome of the last.
                walk["counts"] = {key: earlier["counts"].get(key, 0) + value
                                  for key, value in walk["counts"].items()}
                walk["why"], walk["slices"] = earlier["why"], earlier.get("slices", 1) + 1
                walks[k] = (entry, walk)
        finished = [(entry, walk, self._finish(walk)) for entry, walk in walks]
        for entry, walk, result in finished:
            drive_id, root = walk["drive_id"], walk["root"]
            feed = feeds[drive_id]
            if walk["round"] == "incremental" and not walk.get("cut") and not walk["reasons"]:
                # Every entry applied: the root moves to the run's new cursor. A re-read
                # that ended moves its permission position to the cursor of its first run
                # (every entry since read again at or after it).
                self.store.save_position(drive_id, root, feed["cursor"], feed["read_at"])
                if walk["reread"]:
                    held = self.store.permission_position(drive_id, root) or {}
                    self.store.save_permission_position(
                        drive_id, root, held.get("reread_url") or feed["cursor"],
                        held.get("reread_at") or feed["read_at"])
            elif "walk_cursor" in walk:
                # A walk that ended starts from the cursor read before its first run; when
                # it listed everything, every item's permissions were read after it.
                self.store.save_position(drive_id, root, walk["walk_cursor"],
                                         walk["cursor_read_at"])
                if result["reason"] in (None, CONTENT_INCOMPLETE):
                    self.store.save_permission_position(drive_id, root, walk["walk_cursor"],
                                                        walk["cursor_read_at"])
            if walk.get("cut"):
                result["deferred"] = True   # complete, its follow left for a later run
                if walk["reread"]:   # the next run resumes it past what it read
                    self.store.open_reread(drive_id, root, feed["cursor"], feed["read_at"])
        for drive_id, feed in feeds.items():
            for root in feed["expire"]:
                # Its position is gone (HTTP 410) or its feed unreadable: unless a walk
                # of it ended this run, it walks next run.
                if self.store.walk_position(drive_id, root) is None and not any(
                        w["drive_id"] == drive_id and w["root"] == root and "walk_cursor" in w
                        for _e, w in walks):
                    self.store.expire_walk(drive_id, root)
        for entry, walk, result in finished:
            # ``walk_position``: the next run resumes the cut walk; ``drive_delta``: it
            # follows its drive's change feed from its position; ``None``: it walks again.
            follows = self._follows(walk["drive_id"], walk["root"],
                                    scope.full_walk_hours) is not None
            result["cursor"] = ("walk_position" if walk.get("position") is not None
                                else "drive_delta" if follows else None)
            if walk.get("slices", 1) > 1:
                result["walk_slices"] = walk["slices"]   # resumed by this run's later passes
            results[(entry["index"], walk["root"])] = result
        for entry, _reader, _drive_id, root, _target in plans:   # in the scope's order
            entry["roots"].append(results[(entry["index"], root)])
        for entry in targets:
            if "drive_id" in entry:
                # Complete only when every root is, and was brought current this run.
                entry["complete"] = all(r["complete"] and not r.get("deferred")
                                        for r in entry["roots"])
                entry["status"] = "complete" if entry["complete"] else "incomplete"
        bound = [(r["complete"], r["reason"]) for t in targets for r in t["roots"]]
        bound += [(False, t["status"]) for t, target in zip(targets, scope.targets)
                  if "drive_id" not in t for _root in target.folders]
        result = {"targets": targets,
                  "complete": bool(targets) and all(t["complete"] for t in targets)}
        if named is not None:
            result["named_items"] = named
        if discovery is not None:
            bound += [(False, gap) for gap in discovery["gaps"]]
            result["discovery"] = discovery
            result["complete"] = (all(t["complete"] for t in targets)
                                  and discovery["complete"])
        return {**result, "complete_bound": sum(1 for done, _r in bound if done),
                "bound_total": len(bound),
                "incomplete_reasons": sorted({r for done, r in bound if not done and r})}

    def _classify(self, scope: CaptureScope, reader: Any, drive_id: str,
                  site: str | None, library: Any = None) -> str | None:
        """Record the class the scope gives this drive's library; the refusal status
        when it may not be walked (module docstring, Library classes), else None.
        ``library`` is the listed library (discovery); a named target reads its own."""
        security_class = scope.library_class(drive_id, site, None)
        if security_class is None:
            if library is None:
                try:
                    library = reader.library(self.actor)
                except (NotVisible, SourceNotFound, SourceUnavailable) as exc:
                    detail = (unavailable_detail(exc) if isinstance(exc, SourceUnavailable)
                              else None) or "not_visible"
                    return "library_unclassified:" + detail
            name = library.get("name")
            security_class = scope.library_class(drive_id, site, name)
            if security_class is None and taxonomy.known_library_class(name) is not None:
                self.store.save_drive_class(drive_id, taxonomy.UNRESOLVED_CLASS)
                return "restricted_library_unclassified"
        self.store.save_drive_class(drive_id, security_class)
        return None

    def _discover(self, scope: CaptureScope, claimed: dict[str, list[str]],
                  targets: list[dict[str, Any]], plans: list[tuple]) -> dict[str, Any]:
        """Every readable library of every site on the configured host or its tenant's
        OneDrive host (:func:`estate_hosts`), planned as a
        ``discovered`` target with root ``/`` unless excluded (``rule``), not a document
        library, or already a drive of an explicit target (that target governs it).
        Nothing is dropped silently: a site listing that fails or is cut, a site whose
        libraries cannot be listed, and dropped ids are ``gaps`` (each an incomplete
        bound); a library the capture actor may not read is planned and its root ends
        incomplete with the walk's reason (``not_visible``, ``source_error:http_403``). A
        restricted library the scope does not classify is refused, counted and a gap.
        ``sites``: each seen site's URL name -> ``listed``, ``failed`` when its libraries
        could not be listed or one was refused, ``locked`` (a personal site answering
        HTTP 423, counted ``sites_locked``, no gap), ``excluded`` or ``other_host`` (on any
        host but those two; a personal site has no URL name);
        ``sites_complete``: the site listing itself was read whole (never printed by the
        CLI; it names only sites a registry already names, :func:`registry_site_outcomes`)."""
        rule = scope.discover
        counts = {key: 0 for key in DISCOVERY_COUNTS}
        gaps: list[str] = []
        sites_seen: dict[str | None, str] = {}
        sites_complete = False

        def outcome() -> dict[str, Any]:
            return {"status": "incomplete" if gaps else "complete", "complete": not gaps,
                    "gaps": gaps, "counts": counts, "sites": sites_seen,
                    "sites_complete": sites_complete}

        if self.estate is None:
            gaps.append("discovery_unavailable")
            return outcome()
        try:
            sites, done = self.estate.sites(max_items=rule.max_sites)
        except (SourceUnavailable, NotVisible) as exc:
            gaps.append("discovery_sites:" + (unavailable_detail(exc) or "not_visible"))
            return outcome()
        if not done.get("complete", False):
            gaps.append("discovery_sites_truncated")
        if done.get("provenance_dropped"):
            gaps.append("discovery_provenance_dropped")
        sites_complete = not gaps
        hosts = estate_hosts(self.estate.host)
        people = _OneDriveOwners(self.estate)
        for site in sites:
            counts["sites_seen"] += 1
            if site["host"] not in hosts:
                # Content downloads are pinned to the tenant's two hosts: a site on any
                # other host is out of scope, counted.
                counts["sites_other_host"] += 1
                sites_seen.setdefault(site["name"], "other_host")
                continue
            personal = site["host"] != self.estate.host
            if personal:
                # A personal site on the tenant's OneDrive host: walked like any other
                # library, every item re-gated per reader on its item permissions.
                counts["sites_personal"] += 1
                # Unless its owner is an excluded person (``people_exclusion``): refused
                # before its libraries are listed, counted only. Without a usable guard,
                # or with an owner the path does not name, every OneDrive is withheld
                # (fail closed, a typed gap).
                refusal = people.unavailable(site)
                if refusal == "excluded":
                    counts["sites_people_excluded"] += 1
                    continue
                if refusal:
                    counts["sites_people_withheld"] += 1
                    if refusal not in gaps:
                        gaps.append(refusal)
                    sites_seen.setdefault(site["name"], "withheld")
                    continue
            if rule.site_excluded(site["name"]):
                counts["sites_excluded"] += 1
                sites_seen[site["name"]] = "excluded"
                continue
            try:
                libraries, done = self.estate.libraries(site["id"], max_items=rule.max_libraries)
            except (SourceUnavailable, NotVisible) as exc:
                detail = unavailable_detail(exc)
                if personal and detail in LOCKED_DETAILS:
                    # A personal site SharePoint has locked (HTTP 423: a OneDrive held
                    # for a removed user), readable by nobody: counted and typed
                    # (``sites_locked``), not a discovery gap. A locked site on the site
                    # host stays a gap (decision 2026-10-17-capture-persistence-site-locked).
                    counts["sites_locked"] += 1
                    sites_seen.setdefault(site["name"], "locked")
                    continue
                gaps.append("library_listing:" + (detail or "not_visible"))
                sites_seen[site["name"]] = "failed"
                continue
            counts["sites_listed"] += 1
            if sites_seen.get(site["name"]) != "failed":
                sites_seen[site["name"]] = "listed"
            if not done.get("complete", False):
                gaps.append("library_listing_truncated")
                sites_seen[site["name"]] = "failed"
            if done.get("provenance_dropped"):
                gaps.append("discovery_provenance_dropped")
                sites_seen[site["name"]] = "failed"
            for library in libraries:
                counts["libraries_seen"] += 1
                # Graph types a OneDrive for Business drive "business": a library on the
                # OneDrive host only; on the site host only "documentLibrary" is walked.
                if library["drive_type"] not in (PERSONAL_DRIVE_TYPES if personal
                                                 else ("documentLibrary",)):
                    counts["libraries_not_document_library"] += 1
                elif rule.library_excluded(site["name"], library["name"]):
                    counts["libraries_excluded"] += 1
                elif library["id"] in claimed:
                    counts["libraries_covered_by_target"] += 1
                elif self._classify(scope, None, library["id"], site["name"], library):
                    counts["libraries_restricted_unclassified"] += 1
                    gaps.append("restricted_library_unclassified")
                    sites_seen[site["name"]] = "failed"
                else:
                    claimed[library["id"]] = ["/"]
                    target = CaptureTarget({"id": "discovered", "drive_id": library["id"],
                                            "folders": ["/"], "max_depth": rule.max_depth,
                                            "max_folders": rule.max_folders})
                    entry = {"index": len(targets), "discovered": True, "roots": [],
                             "drive_id": library["id"], "site": site["name"],
                             "personal": personal}
                    targets.append(entry)
                    plans.append((entry, self.readers(library["id"]), library["id"], "/",
                                  target))
                    counts["libraries_planned"] += 1
                    counts["libraries_personal_planned"] += personal
        return outcome()

    def _site(self, site: str | None) -> str:
        if site is None or self.site_drive is None:
            raise ValueError("site_unresolved")
        return self.site_drive(site)


# ------------------------------------------------------------------ CLI (counts only)

CAPTURE_PRINCIPAL = "svc:sharepoint-capture"
# The host state directory of the capture check (``default_state_dir``), below the host's
# state base: the pattern of ``capture_teams`` and ``eval.heldout``.
CHECK_STATE = ("plx-workbench", "sharepoint-capture-check")


def default_state_dir(env: Mapping[str, str], *, injected: bool) -> Path:
    """The host's state directory of the capture check, kept across its runs
    (:func:`capture_mail.check_state_dir`): ``$PLX_WORKBENCH_STATE_DIR/sharepoint-capture-check``,
    else ``$XDG_STATE_HOME/plx-workbench/sharepoint-capture-check``, else under
    ``$HOME/.local/state``, never in Git. An injected transport gets its own
    subdirectory, so a fixture run never shares the live store. None set to an absolute
    path: refused, never a store in a place nobody chose."""
    return check_state_dir(env, CHECK_STATE[-1], injected=injected)[0]


STATE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")


def state_name_dir(env: Mapping[str, str], name: Any, *, injected: bool
                   ) -> tuple[Path, Path, str]:
    """:func:`capture_mail.check_state_dir` for the named store ``sharepoint-capture-
    check-<name>`` (``--state-name``); a name that is not 1..48 lowercase letters, digits
    and hyphens is refused (``ValueError``) before any directory is made."""
    if not isinstance(name, str) or STATE_NAME.fullmatch(name) is None:
        raise ValueError("state_name_invalid")
    return check_state_dir(env, f"{CHECK_STATE[-1]}-{name}", injected=injected)


def incomplete_scope(run: Mapping[str, Any], scope: "CaptureScope", *,
                     persisted: bool) -> list[dict[str, Any]]:
    """Every root that ended ``run`` incomplete, by target and root ordinal with its
    typed reasons: never counted complete. A scope target is named by its id, a library
    an estate rule discovered by its ordinal in the run (``library<n>``: never its name).
    ``resumable``: the time budget cut it, so its walk (the stored position, or from the
    start for a root no walk reached) carries on in the next run; ``persisted``: the
    store was carried into this invocation (observed: it existed before, and the caller
    did not say it is not kept), so the next run finds it; a resumable cut over a store
    not proven kept names the ``later_ask`` live-host-persistent-state-dir. Ids,
    ordinals, enums and booleans only."""
    out: list[dict[str, Any]] = []
    for entry in run["targets"]:
        target = (f"library{entry['index']}" if entry.get("discovered")
                  else scope.targets[entry["index"]].id)
        roots = entry["roots"] or [None]
        for ordinal, root in enumerate(roots):
            if root is not None and root["complete"]:
                continue
            reasons = sorted(_root_reasons(root)) if root is not None else \
                [str(entry.get("status") or "unresolved")]
            resumable = TIME_BUDGET in reasons
            out.append({"target": target, "root": ordinal, "reasons": reasons,
                        "resumable": resumable, "persisted": persisted,
                        "later_ask": PERSISTENT_STATE_ASK if resumable and not persisted
                        else None})
    return out


def blocked_scope(entries: Sequence[Mapping[str, Any]], gaps: Sequence[str] = (), *,
                  carried: bool | None = None) -> list[dict[str, Any]]:
    """The verifier's ``blocked_upstream`` parts (cause ``later_ask``) of an
    :func:`incomplete_scope` that only a human step settles, by the one rule of
    :func:`capture_mail.budget_cut_block`: every incomplete root is cut by the time
    budget alone and its resume has no kept store. One root incomplete any other way,
    a discovery gap (``gaps``), or a cut that resumes over a kept store (it finishes by
    itself, the check fails until it has), yields no part: the check fails, never
    blocked. ``carried``: the store was carried into this run (default: any entry's
    ``persisted``; pass it when only discovery gaps may be named)."""
    units = [(f"{e['target']}.root{e['root']}", e["reasons"]) for e in entries]
    units += [("estate.discovery", [gap]) for gap in gaps]
    if carried is None:
        carried = any(e["persisted"] for e in entries)
    return budget_cut_block(units, carried=carried)


def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default=None,
                        help="Workbench state directory (outside every git work tree); "
                             "default the host's, kept across runs (default_state_dir)")
    parser.add_argument("--state-name", default=None,
                        help="keep this check's store in its own named directory of the "
                             "host's state base (state_name_dir), carried across the "
                             "check's runs, beside the default store and never shared "
                             "with it; excludes --state-dir")
    parser.add_argument("--scope", default=str(DEFAULT_SCOPE),
                        help="plx-sharepoint-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    parser.add_argument("--withheld-principal", default="staff:sharepoint-capture-withheld",
                        help="a principal the audience does not grant; the store is read "
                             "as it to prove a withheld file leaks nothing")
    parser.add_argument("--folder-only-principal", default="staff:sharepoint-folder-only",
                        help="a principal shaped like a live employee: granted every "
                             "captured root folder but holding no source identity, so the "
                             "item permissions must withhold every file from it")
    parser.add_argument("--runs", type=int, default=2,
                        help="capture runs; run 2+ re-walks and keeps unchanged versions")
    parser.add_argument("--max-store-bytes", type=int, default=MAX_STORE_BYTES,
                        help="bound on the capture store (SQLite + blobs); default "
                             "MAX_STORE_BYTES")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="wall-time bound over all runs; a root it cuts is incomplete "
                             "with reason time_budget")
    parser.add_argument("--registry", default=None,
                        help="plx-source-registry/v1 file: an estate run names every site its "
                             "sharepoint site entries name, by outcome, with the typed reason "
                             "of each one not captured (registry_sites)")
    parser.add_argument("--named-originals", action="store_true",
                        help="each run first captures every SharePoint item a case original "
                             "under <state-dir>/case-originals names (named items first; "
                             "within the scope and the people exclusion only)")


def _root_reasons(root: Mapping[str, Any]) -> set[str]:
    return set(filter(None, (root["reason"] or "").split(",")))


BUDGET_CUT_CAUSES = ("unreached", "start_cut", "walk_cut", "finish_cut")


def budget_cut_causes(run: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Every root the time budget cut (reason ``time_budget``) by its root class
    (``personal``: a discovered OneDrive library, ``site``: a discovered site library,
    ``named``: a scope target) and its typed cause: ``unreached`` (deferred, nothing of it
    read this run), ``start_cut`` (its walk began but the budget ended before it listed
    any folder, so no position is kept and the next run walks it from the start),
    ``walk_cut`` (walked, its walk position kept: the budget ended before it listed
    everything), ``finish_cut`` (listed everything, the budget ended while reconciling
    it). The causes are exhaustive: the counts sum to ``roots_budget_cut``."""
    out: dict[str, dict[str, int]] = {}
    for target in run["targets"]:
        kind = ("named" if not target.get("discovered")
                else "personal" if target.get("personal") else "site")
        for root in target["roots"]:
            if TIME_BUDGET not in _root_reasons(root):
                continue
            if root.get("round") is None:
                cause = "unreached"
            elif root.get("walk_position") is not None:
                cause = "walk_cut"
            elif (root.get("round") == "full"
                  and not (root.get("counts") or {}).get("folders_listed")):
                cause = "start_cut"
            else:
                cause = "finish_cut"
            row = out.setdefault(kind, {c: 0 for c in BUDGET_CUT_CAUSES})
            row[cause] += 1
    return dict(sorted(out.items()))


def registry_site_names(path: str | Path) -> list[str]:
    """The site URL names of a source registry's ``sharepoint`` ``site`` entries (names
    already committed there, so a report naming them discloses nothing new). Anything
    malformed is refused."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not all(isinstance(e, Mapping) for e in entries):
        raise CaptureError("registry_invalid")
    names = [e.get("name") for e in entries
             if e.get("system") == "sharepoint" and e.get("kind") == "site"]
    if not all(isinstance(n, str) and grammar.TYPES["site_name"].valid(n) for n in names):
        raise CaptureError("registry_invalid")
    return names


def registry_site_outcomes(run: Mapping[str, Any], names: Sequence[str]
                           ) -> dict[str, Any]:
    """Every one of ``names`` (registry site names, matched case-insensitively) by the
    run's outcome for that site, and ``reasons``: the typed reason of each one not
    captured (the first of its gaps, by name):

    * ``captured``: its libraries listed whole and every library of it the run planned
      walked to the end (an incomplete source at most);
    * ``partial``: one library of it walked to the end, another cut by the time budget
      (``time_budget``): never credited as captured;
    * ``unexercised``: nothing of it walked: every planned library cut by the time
      budget (``time_budget``), none planned (``no_library_planned``), the site left out
      by the scope (``site_excluded``) or on another host (``other_host``);
    * ``failed``: a library of it with a walk gap (that gap, e.g. ``not_visible``), its
      libraries not listed whole or one refused (``library_listing_failed``), or the
      discovery did not list the site (``not_listed``; ``discovery_incomplete`` when the
      site listing itself failed or was cut).

    Without a discovery (a scope of named targets only) nothing is named."""
    out: dict[str, Any] = {"captured": [], "partial": [], "unexercised": [], "failed": [],
                           "reasons": {}}
    discovery = run.get("discovery")
    if discovery is None:
        return out
    seen: dict[str, str] = {}
    for name, state in discovery["sites"].items():   # a site on this host wins a name
        if state != "other_host" or str(name or "").casefold() not in seen:
            seen[str(name or "").casefold()] = state
    roots: dict[str, list[set[str]]] = {}
    for target in run["targets"]:
        if target.get("discovered"):
            roots.setdefault(str(target.get("site") or "").casefold(), []).extend(
                _root_reasons(r) for r in target["roots"])
    for name in sorted(set(names)):
        key = name.casefold()
        state, reasons = seen.get(key), roots.get(key, [])
        gaps = sorted({g for r in reasons for g in r - {TIME_BUDGET} - INCOMPLETE_SOURCE})
        walked = sum(1 for r in reasons if not r - INCOMPLETE_SOURCE)
        if state is None:
            outcome, why = "failed", ("not_listed" if discovery.get("sites_complete")
                                      else "discovery_incomplete")
        elif state in ("excluded", "other_host"):
            outcome, why = "unexercised", "site_excluded" if state == "excluded" else state
        elif gaps:
            outcome, why = "failed", gaps[0]
        elif state == "failed":
            outcome, why = "failed", "library_listing_failed"
        elif state == "locked":
            outcome, why = "failed", "site_locked"
        elif not reasons:
            outcome, why = "unexercised", "no_library_planned"
        elif walked == len(reasons):
            outcome, why = "captured", None
        else:
            outcome, why = ("partial" if walked else "unexercised"), TIME_BUDGET
        out[outcome].append(name)
        if why is not None:
            out["reasons"][name] = why
    return out


NO_ACCESS_GAPS = ("not_visible", "source_error:http_401", "source_error:http_403")


def registry_site_readback(run: Mapping[str, Any], names: Sequence[str],
                           outcomes: Mapping[str, Any], store: SharePointCaptureStore,
                           actor: Actor, audience: LiveSourceAudience) -> dict[str, Any]:
    """Counts only, per registry site name (names the registry already commits): the
    ``libraries`` the run planned, ``items_read`` (current files the capture actor holds
    in them), ``withheld`` (items the listings withheld from it this run), ``items_listed``
    (the two together) and, when nothing was read, the typed ``reason``:

    * ``no_access``: a library or its listing refused the capture principal, or the
      listings withheld every item;
    * ``empty_site``: its libraries were walked and held no file;
    * ``not_in_scope``: the site was not listed, was excluded, is on another host or has
      no library planned;
    * ``not_walked``: the time budget reached none of its libraries;
    * ``source_error``: another walk gap stopped it.

    ``outcomes`` is :func:`registry_site_outcomes` of the same run."""
    current = {r["drive_id"]: r["current_files"]
               for r in store.completeness(actor, audience)["roots"]}
    drives: dict[str, list[tuple[str, list[dict[str, Any]]]]] = {}
    for target in run["targets"]:
        if target.get("discovered"):
            drives.setdefault(str(target.get("site") or "").casefold(), []).append(
                (target["drive_id"], target["roots"]))
    out: dict[str, Any] = {}
    for name in sorted(set(names)):
        planned = drives.get(name.casefold(), [])
        read = sum(current.get(drive_id, 0) for drive_id, _roots in planned)
        withheld = sum(r["counts"]["withheld"] for _d, roots in planned for r in roots)
        why = outcomes["reasons"].get(name)
        reason = None
        if not read:
            if withheld or why in NO_ACCESS_GAPS or why == "library_listing_failed":
                reason = "no_access"
            elif why == TIME_BUDGET:
                reason = "not_walked"
            elif why in (None, "no_library_planned") and planned:
                reason = "empty_site"
            elif why in (None, "site_excluded", "other_host", "not_listed",
                         "discovery_incomplete", "no_library_planned"):
                reason = "not_in_scope"
            else:
                reason = "source_error"
        out[name] = {"libraries": len(planned), "items_listed": read + withheld,
                     "items_read": read, "withheld": withheld, "reason": reason}
    return out


def credit_readback(outcomes: dict[str, Any], readback: Mapping[str, Any]) -> None:
    """A site whose libraries were all walked but which holds no file read nothing: it
    moves from ``captured`` to ``unexercised`` with its typed reason, so the registry
    never credits an empty site as read."""
    for name in list(outcomes["captured"]):
        if not readback[name]["items_read"]:
            outcomes["captured"].remove(name)
            outcomes["unexercised"].append(name)
            outcomes["reasons"][name] = readback[name]["reason"]
    outcomes["unexercised"].sort()


def _report_run(number: int, run: Mapping[str, Any]) -> dict[str, Any]:
    targets = []
    for entry in run["targets"]:
        roots = [{"status": r["status"], "complete": r["complete"], "reason": r["reason"],
                  "round": r["round"], "full_walk_reason": r["full_walk_reason"],
                  "walk_position": r["walk_position"], "counts": dict(r["counts"])}
                 for r in entry["roots"]]
        targets.append({"index": entry["index"], "status": entry["status"],
                        "complete": entry["complete"], "roots": roots,
                        "discovered": bool(entry.get("discovered"))})
    report = {"run": number, "complete": run["complete"], "targets": targets,
              "complete_bound": run["complete_bound"], "bound_total": run["bound_total"],
              "incomplete_reasons": run["incomplete_reasons"]}
    if "discovery" in run:
        report["discovery"] = {"status": run["discovery"]["status"],
                               "complete": run["discovery"]["complete"],
                               "gaps": sorted(set(run["discovery"]["gaps"])),
                               "counts": dict(run["discovery"]["counts"])}
    if "named_items" in run:   # counts and typed outcomes only, never an item
        named = run["named_items"]
        report["named_items"] = {"named": named["named"],
                                 "outcomes": {k: v for k, v in named["outcomes"].items() if v},
                                 "counts": {k: v for k, v in named["counts"].items() if v}}
    return report


def folder_only_audience(audience: LiveSourceAudience, principal: str,
                         roots: Sequence[tuple[str, str]]) -> LiveSourceAudience:
    """``principal`` granted the captured root folders (``(drive id, root)``) and
    everything under them, with the item permissions required on those drives and no
    source identity: the live shape of an employee whose folder grant is wider than
    the files shared with them."""
    grants: dict[str, list[str]] = {}
    for drive_id, root in roots:
        grants[f"sharepoint:{drive_id}:{root}"] = [principal]
        grants[f"sharepoint:{drive_id}:{root.rstrip('/')}/*"] = [principal]
    return LiveSourceAudience(grants, tenant=audience.tenant,
                              source_acl_required=sorted({f"sharepoint:{d}:*"
                                                          for d, _ in roots}))


def _progress_marks(store: SharePointCaptureStore, actor: Actor,
                    audience: LiveSourceAudience) -> dict[str, Any]:
    """The store's estate progress marks: ``complete``, the capture actor's complete
    roots (``summary.roots_complete``), and ``visited``, every (drive id, root) some walk
    has reached: one walk of it ended (complete or with a gap) or holds a position. A
    root only deferred so far (``time_budget``, nothing read) is not visited."""
    visited = set()
    for row in store.db.execute("SELECT drive_id, root_path, status, reason, "
                                "rounds_complete FROM roots"):
        root = (row["drive_id"], row["root_path"])
        if (row["rounds_complete"] or row["reason"] not in (None, TIME_BUDGET)
                or row["status"] == "complete"
                or store.walk_position(*root) is not None):
            visited.add(root)
    return {"visited": visited,
            "complete": sum(1 for r in store.completeness(actor, audience)["roots"]
                            if r["complete"])}


def _walk_ages(store: SharePointCaptureStore, roots: Sequence[tuple[str, str]],
               hours: int) -> dict[str, Any]:
    """The roots by the age of their last full walk that listed everything
    (``full_walk_age``: ``fresh`` younger than ``hours``, ``due`` up to ``STALE_HOURS``,
    ``stale`` older, ``never``), and the oldest age in hours (None: none walked)."""
    bands = {"fresh": 0, "due": 0, "stale": 0, "never": 0}
    ages = [store.full_walk_age(*root)["age_hours"] for root in roots]
    for age in ages:
        bands["never" if age is None else "fresh" if age < hours
              else "due" if age < STALE_HOURS else "stale"] += 1
    walked = [age for age in ages if age is not None]
    return {"full_walk_age": bands, "full_walk_age_max_hours": max(walked, default=None)}


def carried_estate(store: SharePointCaptureStore, actor: Actor, audience: LiveSourceAudience,
                   planned: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """The estate judged across runs, not by one (decision 2026-10-21-sharepoint-census-
    across-runs): of the planned roots, ``roots_complete`` are complete for ``actor`` (a
    full walk listed everything and every current file's bytes are captured) with their
    last full walk younger than ``STALE_HOURS`` (a due refresh inside that window keeps
    serving its complete state; one older is ``roots_stale``), ``roots_unvisited`` no walk
    has reached, ``roots_incomplete`` visited but not complete. ``complete`` only when
    roots are planned and every one is complete and not stale: one unvisited, stale or
    incomplete root fails it. Counts and a boolean only."""
    rows = {(r["drive_id"], r["root"]): r for r in store.completeness(actor, audience)["roots"]}
    marks = _progress_marks(store, actor, audience)["visited"]
    complete = stale = unvisited = incomplete = 0
    for root in planned:
        row = rows.get(root)
        if root not in marks:
            unvisited += 1
        elif row is None or not row["complete"]:
            incomplete += 1
        elif row["age_hours"] is None or row["age_hours"] >= STALE_HOURS:
            stale += 1
        else:
            complete += 1
    return {"roots_planned": len(planned), "roots_complete": complete,
            "roots_unvisited": unvisited, "roots_incomplete": incomplete,
            "roots_stale": stale,
            "complete": bool(planned) and complete == len(planned)}


def run_cli(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
            sender: Any = None, sleep: Callable[[float], None] | None = None,
            monotonic: Callable[[], float] = time.monotonic,
            retry_first: Callable[[Mapping[str, Any]], bool] | None = None) -> dict[str, Any]:
    """Capture ``--runs`` times, then read the store back as the capture actor, as a
    withheld actor and as a folder-only actor (:func:`folder_only_audience`). Prints
    counts, booleans and enums only. ``retry_first``: the degraded versions whose bytes
    each run retries before walking (:meth:`SharePointCapture._retry_first`; reported as
    ``retried_first``, the bytes it recovered)."""

    from .graph_read import SharePointReader, SharePointSites, build_graph, resolve_site_drive
    from .live_source_guard import BoundedBackoff, sharepoint_host_from_env

    if isinstance(args.runs, bool) or not 1 <= args.runs <= 5:
        raise ValueError("runs must be 1..5")
    state_name = getattr(args, "state_name", None)
    if state_name is not None and args.state_dir is not None:
        raise ValueError("state_name_with_state_dir")
    if state_name is not None and STATE_NAME.fullmatch(str(state_name)) is None:
        raise ValueError("state_name_invalid")
    scope = CaptureScope.from_file(args.scope)
    audience = LiveSourceAudience.from_file(args.live_audience)
    registry = getattr(args, "registry", None)
    registry_sites = None if registry is None else registry_site_names(registry)
    env = os.environ if environ is None else environ
    state_base = None
    if args.state_dir is None:
        state_dir, base, state_base = (
            state_name_dir(env, state_name, injected=sender is not None)
            if state_name is not None else
            check_state_dir(env, CHECK_STATE[-1], injected=sender is not None))
        assert_outside_git(state_dir)
        # The directories it makes below the host's state base: 0700.
        for n in range(1, len(state_dir.relative_to(base).parts) + 1):
            base.joinpath(*state_dir.relative_to(base).parts[:n]).mkdir(
                mode=0o700, parents=True, exist_ok=True)
    else:
        state_dir = Path(args.state_dir).expanduser()
    carried = store_exists(state_dir)
    store = SharePointCaptureStore(state_dir, max_store_bytes=MAX_STORE_BYTES if
                                   args.max_store_bytes is None else args.max_store_bytes)
    try:
        api, transport = build_graph(environ, sender=sender, retry=BoundedBackoff(
            sleep=sleep or time.sleep, budget_seconds=60))
        host = sharepoint_host_from_env(environ)
        readers: dict[str, Any] = {}

        def reader_for(drive_id: str) -> Any:
            if drive_id not in readers:
                readers[drive_id] = SharePointReader(api, audience, drive_id,
                                                     transport=transport,
                                                     read_permissions=True,
                                                     max_acl_reads=MAX_ACL_READS)
            return readers[drive_id]

        def site_drive(name: str) -> str:
            if host is None:
                raise ValueError("site_host_unconfigured")
            return resolve_site_drive(api, host, name)

        actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
        withheld = Actor(audience.tenant, frozenset({args.withheld_principal}), "restricted")
        capture = SharePointCapture(store, reader_for, actor, site_drive=site_drive,
                                    estate=None if host is None else SharePointSites(api, host),
                                    max_content_bytes=scope.max_content_bytes,
                                    budget_seconds=getattr(args, "budget_seconds", None),
                                    monotonic=monotonic, sleep=sleep or time.sleep,
                                    read_content=getattr(args, "read_content", True))
        capture.retry_first = retry_first
        if getattr(args, "named_originals", False):
            from operating.captured_originals import sharepoint_items
            capture.named_items = sharepoint_items(state_dir)
        before = _progress_marks(store, actor, audience)
        raw_runs = [capture.run(scope) for _n in range(args.runs)]
        runs = [_report_run(n, run) for n, run in enumerate(raw_runs, 1)]
        # Offline resolution of what was captured (no source read).
        index = store.index(actor, audience, limit=INDEX_LIMIT)
        resolved = sum(1 for item in index["items"]
                       if store.resolve(item["l1_handle"], actor, audience) is not None)
        pinned = [i for i in index["items"] if i["content_sha256"]]
        verified, failed_bytes = 0, {}
        for i in pinned:
            # Pinned bytes that fail the re-hash (or are gone, or fail any other way) are
            # a typed count, never a crash.
            try:
                verified += store.original_verified(
                    f"{i['l1_handle']}#sha256={i['content_sha256']}", actor, audience)
            except Exception as exc:   # noqa: BLE001 - each failure is counted by type
                code = (exc.code if isinstance(exc, CaptureError) else "blob_unreadable"
                        if isinstance(exc, OSError) else "unexpected:" + type(exc).__name__)
                failed_bytes[code] = failed_bytes.get(code, 0) + 1
        hidden_index = store.index(withheld, audience, limit=INDEX_LIMIT)
        hidden_counts = store.counts(withheld, audience)
        hidden_roots = store.completeness(withheld, audience)["roots"]
        hidden_resolved = sum(1 for item in index["items"]
                              if store.resolve(item["l1_handle"], withheld, audience)
                              is not None)
        walked = [(r["drive_id"], r["root_path"]) for r in store.db.execute(
            "SELECT drive_id, root_path FROM roots ORDER BY drive_id, root_path")]
        folder_principal = getattr(args, "folder_only_principal",
                                   "staff:sharepoint-folder-only")
        folder_actor = Actor(audience.tenant, frozenset({folder_principal}), "restricted")
        folder_audience = folder_only_audience(audience, folder_principal, walked)
        folder_rows = store.index(folder_actor, folder_audience, limit=INDEX_LIMIT)["items"]
        folder_resolved = sum(1 for item in index["items"]
                              if store.resolve(item["l1_handle"], folder_actor,
                                               folder_audience) is not None)
        counts = store.counts(actor, audience)
        completeness = store.completeness(actor, audience)
        last = runs[-1]
        report = {
            "check": "capture.sharepoint",
            "evidence_label": "injected-transport" if transport.sender_injected
            else "live-read",
            "read_only_guard": "GET-only; token POST only",
            "storage": {"outside_git_tree": True,
                        "under_state_dir": Path(store.root).parent == state_dir.resolve(),
                        # Kept across runs in the host's state directory (no --state-dir),
                        # and carried: the store existed before this invocation.
                        "host_state": args.state_dir is None, "carried": carried,
                        # Which host state base (capture_mail.check_state_dir): only
                        # workbench_state_dir is kept between the verifier's daily runs.
                        "state_base": state_base},
            "runs": runs,
            # What the last run left incomplete, by scope target and root, typed (module
            # docstring, Scope not finished); [] only when every scope root is complete.
            "incomplete_scope": (named := incomplete_scope(
                raw_runs[-1], scope,
                persisted=carried and getattr(args, "state_kept", None) is not False)),
            "blocked_upstream": blocked_scope(
                named, (raw_runs[-1].get("discovery") or {}).get("gaps") or [],
                carried=carried and getattr(args, "state_kept", None) is not False),
            "summary": {"roots": len(completeness["roots"]),
                        "roots_complete": sum(1 for r in completeness["roots"]
                                              if r["complete"]),
                        "files_current": counts["files_current"],
                        "versions": counts["versions"],
                        "contents_captured": counts["contents_captured"],
                        # Content a metadata-only capture (read_content False) skipped.
                        "contents_skipped": counts["contents_skipped"],
                        "roots_metadata_complete": sum(1 for r in completeness["roots"]
                                                       if r["metadata_complete"]),
                        "texts_extracted": counts["texts_extracted"],
                        "texts_ocr": counts["texts_ocr"],
                        "texts_ocr_unavailable": counts["texts_ocr_unavailable"],
                        "content_exceeds_cap": sum(r["content_exceeds_cap"]
                                                   for r in completeness["roots"]),
                        "content_unavailable": sum(r["content_unavailable"]
                                                   for r in completeness["roots"]),
                        # New versions written by the last re-walk (unchanged files keep
                        # their version; None after a single run).
                        "rewalk_versions_new": sum(
                            r["counts"]["versions_new"] for t in last["targets"]
                            for r in t["roots"]) if len(runs) > 1 else None,
                        "store_bytes": store.store_bytes(),
                        "store_full": store.store_full()},
            # Over a truncated index (more than INDEX_LIMIT current files) the counts are
            # a lower bound: index_truncated, and the check is never ok.
            "resolve_check": {"index_truncated": index["truncated"] is not False,
                              "handles_checked": len(index["items"]),
                              "resolved_offline": resolved,
                              "contents_checked": len(pinned),
                              "content_bytes_verified": verified,
                              "content_bytes_failed": failed_bytes},
            "leak_check": {"withheld_actor_roots": len(hidden_roots),
                           "withheld_actor_rows": len(hidden_index["items"]),
                           "withheld_actor_counts": sum(hidden_counts.values()),
                           "withheld_actor_resolved": hidden_resolved,
                           "folder_only_actor_rows": len(folder_rows),
                           "folder_only_actor_counts": sum(
                               store.counts(folder_actor, folder_audience).values()),
                           "folder_only_actor_resolved": folder_resolved},
            # The ontology relation (source family sharepoint) this run holds records of.
            "relations": ({"driveItems": counts["files_current"]}
                          if counts["files_current"] else {}),
            "requests_sent": transport.requests_sent,
            # Throttled reads read again once after the source's own Retry-After wait.
            "retry_after_waits": capture.retry_after_waits,
            "retry_after_seconds": round(capture.retry_after_seconds, 1),
            # The Graph reads it made (grammar operations): the endpoints a pass evaluates.
            "operations_read": sorted(transport.operations_read),
        }
        if retry_first is not None:
            report["retried_first"] = capture.first_counts["contents_recovered"]
        if getattr(args, "named_originals", False):
            # How many of the named items the store holds as current now (a count only).
            report["named_items_held"] = sum(
                1 for drive_id, item_id in set(map(tuple, capture.named_items))
                if (row := store.current(drive_id, item_id)) is not None
                and row["removed_reason"] is None)
        if scope.discover is None:
            settled = bool(last["complete"] and report["summary"]["roots"]
                           and report["summary"]["roots_complete"]
                           == report["summary"]["roots"])
        else:
            # The estate outlasts one check's wall time: a root the time budget cut is
            # reported partial (summary.roots_complete < summary.roots, estate
            # .roots_budget_cut), never as read. So is a root walked to the end whose
            # source withheld some files' bytes (estate.roots_incomplete_source; the files'
            # metadata and permissions are captured, their bytes retried every run). Every
            # reason is named (estate.root_gaps, estate.contents_unavailable by detail); any
            # other gap fails the check (estate.roots_other_gaps).
            reasons = [_root_reasons(r) for t in last["targets"] for r in t["roots"]]
            gaps = [r - {TIME_BUDGET} for r in reasons]
            named: dict[str, int] = {}
            for reason in sorted(x for r in gaps for x in r):
                named[reason] = named.get(reason, 0) + 1
            unavailable: dict[str, int] = {}
            for row in store._visible_rows(actor, audience):
                if str(row["content_status"]).startswith(_UNAVAILABLE):
                    detail = row["content_status"][len(_UNAVAILABLE):]
                    unavailable[detail] = unavailable.get(detail, 0) + 1
            after = _progress_marks(store, actor, audience)
            planned = capture.planned
            unvisited_before = sum(1 for root in planned if root not in before["visited"])
            unvisited = sum(1 for root in planned if root not in after["visited"])
            report["estate"] = {**last["discovery"]["counts"],
                                "discovery_complete": all(r["discovery"]["complete"]
                                                          for r in runs),
                                "roots_budget_cut": sum(1 for r in reasons if TIME_BUDGET in r),
                                # Each cut root's typed cause by root class
                                # (:func:`budget_cut_causes`).
                                "roots_budget_cut_by": budget_cut_causes(raw_runs[-1]),
                                "roots_incomplete_source": sum(
                                    1 for r in gaps if r and r <= INCOMPLETE_SOURCE),
                                "roots_other_gaps": sum(1 for r in gaps
                                                        if r - INCOMPLETE_SOURCE),
                                "root_gaps": dict(sorted(named.items())),
                                "contents_unavailable": dict(sorted(unavailable.items())),
                                # How old each planned root's captured state is
                                # (counts by band, module docstring, Refresh walks).
                                **_walk_ages(store, capture.planned, scope.full_walk_hours),
                                # Complete roots whose due full walk (a refresh) the time
                                # budget cut: they keep their state and resume next run.
                                "roots_refresh_cut": sum(
                                    1 for t in raw_runs[-1]["targets"] for r in t["roots"]
                                    if r.get("walk_position") is not None
                                    and TIME_BUDGET not in _root_reasons(r)),
                                # The estate judged across runs (:func:`carried_estate`).
                                "carried": carried_estate(store, actor, audience, planned),
                                # Across invocations on the host state (module docstring,
                                # Estate progress): complete roots never fewer, roots no
                                # walk has reached fewer each run until none is left.
                                "progress": {
                                    "carried": carried,
                                    "roots_complete_before": before["complete"],
                                    "roots_complete": after["complete"],
                                    "roots_complete_not_falling":
                                        after["complete"] >= before["complete"],
                                    "roots_unvisited_before": unvisited_before,
                                    "roots_unvisited": unvisited,
                                    "roots_unvisited_falling": (unvisited < unvisited_before
                                                                or unvisited == 0)}}
            if registry_sites is not None:
                report["registry_sites"] = registry_site_outcomes(raw_runs[-1],
                                                                  registry_sites)
                readback = registry_site_readback(raw_runs[-1], registry_sites,
                                                  report["registry_sites"], store, actor,
                                                  audience)
                credit_readback(report["registry_sites"], readback)
                report["registry_sites"]["readback"] = readback
            settled = bool(report["estate"]["discovery_complete"]
                           and all(t["roots"] for t in last["targets"])
                           and not report["estate"]["roots_other_gaps"]
                           and report["summary"]["roots_complete"])
        sound = bool(not report["resolve_check"]["index_truncated"]
                     and resolved == len(index["items"]) and verified == len(pinned)
                     and not any(report["leak_check"].values()))
        report["ok"] = bool(settled and sound)
        if not sound:   # blocked only while the scope is the whole failure
            report["blocked_upstream"] = []
        return report
    finally:
        store.close()


# ------------------------------------------------------------------ compact (one-time)

def compact_cli(argv: Sequence[str] | None = None) -> int:
    """``python3 -m integrations.capture_sharepoint compact --state-dir D``: compact a
    store captured before compact storage (:meth:`SharePointCaptureStore.compact`),
    holding the host's cycle lock (``<D>/host/cycle.lock``) so no capture runs beside it.
    Prints counts and bytes only."""
    from .capture_mail import _locked

    parser = argparse.ArgumentParser(prog="python3 -m integrations.capture_sharepoint compact",
                                     description=compact_cli.__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True,
                        help="Workbench state directory holding sharepoint-capture/")
    parser.add_argument("--no-vacuum", action="store_true",
                        help="compress and drop only; leave the SQLite file as it is")
    parser.add_argument("--defer-ocr", action="store_true",
                        help="also drop the bytes of scans whose OCR is pending (status "
                             "ocr_deferred; a later walk re-fetches and OCR-reads them)")
    args = parser.parse_args(argv)
    return _locked(args.state_dir, lambda store: store.compact(
        vacuum=not args.no_vacuum, defer_ocr=args.defer_ocr),
        open_store=lambda state: SharePointCaptureStore(state, max_store_bytes=None),
        exists=store_exists, missing="sharepoint_capture_missing")


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["compact"]:
        return compact_cli(argv[1:])
    print("usage: python3 -m integrations.capture_sharepoint compact --state-dir D "
          "[--no-vacuum] [--defer-ocr]\n(capture runs: python3 -m integrations.graph_read "
          "sharepoint-capture ...)")
    return 2


__all__ = ["ABSENT", "CAPTURE_PRINCIPAL", "CaptureScope", "CaptureTarget", "DISCOVERY_COUNTS",
           "CONTENT_NOT_READ", "CONTENT_SKIPPED", "DiscoveryRule", "EXCEEDS_CAP", "MAX_STORE_BYTES", "PRUNED",
           "FOLDER_COUNTS", "MOVED_OUT", "OCR_FAILED", "OCR_UNAVAILABLE", "SHAREPOINT_KIND",
           "SharePointCapture", "SharePointCaptureStore", "TEXT_OCR", "add_cli_arguments",
           "compact_cli", "folder_only_audience", "main", "run_cli", "store_exists",
           "version_handle"]


if __name__ == "__main__":
    raise SystemExit(main())
