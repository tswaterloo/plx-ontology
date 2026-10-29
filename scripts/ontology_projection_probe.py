"""Live check of the ontology projection (lane L7): counts, booleans and timings only.

    python3 -m scripts.ontology_projection_probe --db DIR/mirror.sqlite \
        --store DIR/ontology.sqlite [--budget-seconds N] [--claim-sample N] [--max-rss-mb N]
        [--previous-report DAILY.json] [--build-only] [--session-projection SESSION_DIR]
    python3 -m scripts.ontology_projection_probe --produce-session \
        --session-projection SESSION_DIR --db DIR/mirror.sqlite --store DIR/x.sqlite \
        [--bc-capture] [--budget-seconds N] [--previous-report DAILY.json]
        [--mail-capture-scope SCOPE --live-audience AUDIENCE --capture-page-budget N]
    ... [--sharepoint-capture-scope SCOPE --sharepoint-state DIR --live-audience AUDIENCE
        [--sharepoint-budget-seconds N]]

GET only. Fills a throwaway Portal source mirror through
``scripts.mirror_measure.ordered_full_coverage`` (bound relations first; key from
``PLX_WORKBENCH_API_KEY``, never printed), projects it under the curated schema
(``ontology.schema.load_schema``) twice into a throwaway store, then measures the
09-CRITIQUE L7 acceptance on real data. Its ``completeness`` block is the one figure
(``scripts.mirror_measure.completeness``): ``complete_bound_relation_count /
bound_total``, and ``mirror_complete`` is its ``all_bound_complete``. Relations the last daily report (``--previous-report``) names unstable are
not staged at all, leave ``bound_total`` and put the gap ``capture_unstable_ids`` on each
of their bindings (``gap_codes``, ``capture_unstable_bindings``). A bound relation the
mirror's catalog marks unavailable upstream (a reason such as approved columns missing or
not granted after a Sage full load) is an upstream outage, never an incomplete capture: it
is one ``blocked_upstream`` part (cause ``relation_unavailable``, at most
``MAX_BLOCKED_PARTS`` in all; ``mirror_unavailable_bound_count`` counts every one) and
``mirror_complete`` is judged over the other bound relations (``_mirror_complete``), so the
verifier reports the check blocked, never a pass. A relation unserved without such a reason
stays incomplete; a ``--build-only`` build keeps the strict figure and adds no such part.

* ``rebuild_digest_equal``: two builds from the same captures have one content digest;
* ``claim_pass_rate``: a sample of projected values, each claimed at its L1 handle and
  field path, through the real claim gate (``operating.claim_checks.check_items``) over
  the rows the served host reads for the reader (``served_rows``; quantity roles
  grounded by the store's schema contracts);
* ``reader_objects`` and ``denied_leaks``: objects the served host shows the reader, and
  rows a reader gets back that it is not granted (must be 0), both by the host's own trim
  (``operating.host.Host.trim_check``): an actor with no grant on a sample of objects
  (``denied_objects_probed``) and the reader on a sample of the objects whose audience
  units its own grants withhold (``reader_denied_objects_probed``, 0 when it is granted
  everything), plus the sampled values' citations the reader is withheld that the served
  reader still resolves for it (``withheld_citations_resolved``). A trim that did not run
  reports each of these ``not_checked`` (never 0), which fails every expectation on them;
* ``removal_stretches`` and the ``removal_*`` blocks (ING-09, REC-04): what the source's
  removals did to the served projection, by the one removal rule
  (``ontology.store.removed_at_sql`` / ``removals_at``) and the one withdrawal path
  (``ontology.reviews.state_at`` as ``plx.review`` reads it, the links ``build_store``
  wrote), never a rule of the probe's own (``removal_checks``).
  ``removals_reach_projection``: when the mirror holds soft-deleted rows of a bound
  relation, the projection carries ``source_soft_delete`` stretches;
* ``maxrss_within_budget``: the process's peak resident memory (mirror sync, two
  streamed builds and the checks) stayed under ``--max-rss-mb`` (decision
  projection-memory: the build streams per binding, so the peak must not track the
  capture size).

Every read is the served path: the captures and the projection are laid out as a host
state directory (``publish_served``: the layout ``operating.host`` serves) and read through
``Host.served_tools`` as one real principal (``--capture-principal``, a restricted
reader) under the host's audiences (``--mirror-audience`` / ``--live-audience``; default:
that principal granted what the probe captured), never through a probe-only resolver or
policy. A narrowed audience narrows what the probe counts exactly as it narrows what the
plx tools serve: the claim sample's values the reader is withheld are not claims
(``claim_values_withheld``), their citations must resolve for nobody, and the trim probes
what the narrowing withholds, so the live checks run narrowed (``--mirror-audience
examples/projection_probe_mirror_audience.live-check.json``) to measure trimming on real data.

With ``--mail-capture-scope`` and ``--live-audience`` (Graph credentials from the
environment) the probe also captures that mail scope once into a throwaway versioned
mail capture in the served state (``integrations.capture_mail``, GET only, as
``--capture-principal``), projects it as Email/Attachment documents
(``ontology.project.MailCaptures``) and writes stored mention links into the same
generation (the ``mentions`` step of ``operating.build_steps``, the one ``post_project``
list every build of the probe and the host runs; decision
2026-10-02-mail-and-mentions-store). It then reports document and mention-link counts and,
through the served ``plx.around`` (each linked object back over its mention link types),
how many mention links the reader (granted the mail) is served on a sample of linked
objects and ``mention_denied_leaks``: mention links and documents served to
``MAIL_WITHHELD_PRINCIPAL``, a principal the host's audiences grant the records but not the
mail (must be 0; ``mention_objects_visible_to_withheld`` counts the linked objects it is
served, so a vacuous check is visible).

With ``--sharepoint-capture-scope`` and ``--sharepoint-state`` (Graph credentials and
``SHAREPOINT_SITE_ID`` from the environment) the probe captures that SharePoint scope once
with the capture ``ontology.document_links`` runs (``capture_sharepoint.run_cli``, GET only,
as ``--capture-principal`` under ``--live-audience``, read back as ``DENIED_PRINCIPAL``,
within ``--sharepoint-budget-seconds`` and ``SYNC_SHARE`` of what is left) into that
directory, projects it in the same builds (source ``sharepoint``, its documents read by the
same ``document_fields`` and ``mentions`` steps) and lays it out in the served state
(``serve_sharepoint``: its ``sharepoint-capture`` names that capture), so the served host
reads it under the same audience; a run without it serves none. ``sharepoint_capture``
reports its evidence label, completeness and counts; one that raises (with
``--bc-capture``), holds no file and is incomplete, or is not configured (no
``SHAREPOINT_SITE_ID``: nothing read) is a ``sharepoint_capture`` part; one incomplete
that holds files is served as far as it got and named in ``partial_upstream`` (the same
part shape, never blocking). An entry at the served state's ``sharepoint-capture`` the
probe did not link is refused (``ServedCaptureConflict``), never served.

With ``--bc-capture`` (BC credentials from the environment) the probe captures BC itself
(``integrations.bc_read.capture_probe``, GET only, the entity sets the host captures:
``integrations.bc_read.CAPTURE_ENTITY_SETS``) into the served state's BC capture store and
projects it in the same builds (source ``bc``). A ``--db`` or served state already there
(the verifier's ``{session_state}``, which a session producer filled) is topped up, never
read again from scratch: the mirror sync refreshes it (``SourceMirror.sync``: incrementally
where a relation's ``since`` allows) and the BC capture reads every unit the verifier session
did not complete (``capture_bc``, ``bc_capture.units_reused``; a unit complete from an
earlier invocation is read again, ``bc_capture.capture_age_seconds``). Every source becomes
one part: a Portal that is not configured or fails the sync as a whole (the catalog read,
or every bound relation failing upstream with none complete), a BC capture that raises or
captures no record and a mail capture that raises are each one ``blocked_upstream`` entry
(``{part, source, http_status, error_code}``), a mail capture Graph refused on every bound
mailbox adds its refused parts (``capture_mail.refused_parts``: ``mail.mailbox_lookup`` or
``mail.folder_read``, cause ``host_misconfiguration``), and the builds go on from the rest; a Portal
outage blocks parts, not the probe. ``sources_blocked`` names why nothing could be measured
(``no_record_source``: neither Portal nor BC was projected; ``no_document_source``: the mail
asked for was not captured), so the verifier reports the check blocked, never a pass.
``citations`` counts the sampled values' handles the served host's claim gate resolves
offline for the reader, by scheme (``api``, ``bc``, ``graph-mail``; ``served_citations``),
and ``citations.by_reader`` the claim gate's and ``plx.read``'s counts each on its own.

``--budget-seconds`` bounds the whole probe, not only the mirror sync: the mirror sync gets
``SYNC_SHARE`` of it, and the clock is checked between the phases (sync, mail capture, each
build, the claim and leak checks). A spent budget stops the probe at the next check: it
prints a partial report (``status: "budget_exhausted"``, the ``phase`` reached and the counts
so far: the mention tables too once the first build ended) and exits ``EXIT_BUDGET`` (4),
well inside the budget a harness timeout sits above. The claim, leak and mention checks run on the
first build; its digest is kept and its store (with the served link and the generations
that hold it; a link to the session generation frees nothing of the session's) is removed
before the rebuild, which then takes its place in the served state, so the probe never
holds two full stores at once. A rebuild that
cannot fit in what is left (less than the first build took) is not started: the report is
that budget stop at ``rebuild_skipped`` (``rebuild_skipped`` true, no
``rebuild_digest_equal``); on the session path, whose first build reuses the session
generation (about 0 s), that forecast is the ``build_seconds`` the session manifest
recorded. ``--checks-budget-seconds`` bounds the claim, trim, removal and mention checks and
the rebuild from the end of the first build (a spent one is the same budget stop, with
``checks_budget_seconds``), so a verifier schedules a shared run by it. The claim sample is
read by rowid seeks spread over the store (``sample_values``: O(claim_sample) work and
rows, never a scan of the store), and the claim gate's quantity-role contracts for it in
one pass (``sampled_contracts``), so ``claims_timing`` (``scan_seconds``,
``gate_seconds``) stays far under the budget at live size. Every report carries
``phase_seconds``: the seconds spent in each phase entered (``PHASES``), so a stop names
where the budget went. ``--max-rss-mb`` is enforced: a watchdog thread samples the resident
memory (``VmRSS``) every ``RSS_SAMPLE_SECONDS`` and over the limit prints a partial report
(``status: "rss_exceeded"``, the ``phase`` and ``store`` reached, ``rss_mb``, the counts so
far) and exits ``EXIT_RSS`` (5), so a kill by the host below the limit is the host's (memory
pressure), not the probe's. An error line names the ``phase`` too. ``bc_capture.unit_reasons`` counts the BC units not complete by
reason (``company_etag_missing``: BC lists a company without an etag, its records are
captured all the same); ``bc_capture.reason`` is set only for a capture that held nothing.

``--build-only`` is the build a consumer of the served state runs (``ontology.traverse``,
``workspace.object_view``, ``process.dependencies``): one build laid out as the served state, its counts and parts,
no rebuild and no claim, trim or mention checks (determinism and claims are measured
only by this probe's own check), so a long first build never cancels the consumer. On a
budget stop a consumer passes on ``budget_partial`` of the partial report: only the
``PARTIAL_FIELDS`` counts, flags and codes, so a key the build adds later never reaches
the consumer's record unreviewed.

**Session projection** (``ontology.session_projection``, directive 56ca42c8a936): one
projection per verifier session, which every consumer opens instead of projecting the
session's mirror cold. ``--produce-session`` (the producer) never reads the Portal and
never copies the session mirror (``--db``, filled by ``mirror.full_coverage``; none there is
an ``error``, so its consumers project cold): it first removes the manifest of any earlier
run (a run that does not commit leaves none, so no earlier run's counts sit beside this
run's captures), reads the mirror's completeness offline (``session_mirror``), then pins
the mirror's state in one read transaction it holds until the manifest is written
(``mirror_state``: a digest of its meta, captures and relation states) and projects the
file itself; a writer that changed it meanwhile is an ``error`` (``session_mirror_changed``,
nothing committed). It captures BC itself with
``--bc-capture`` into the same directory, as the served hosts capture it (bound to the
served tenant, so every consumer's host resolves its handles; ``bc.capture_full``'s store
is bound to the BC tenant and is served by none of them), keeps
``ontology.project_incremental.build_generation`` current in ``SESSION_DIR`` and then
writes ``SESSION_MANIFEST`` (the committed generation, its sources, blocked parts, mirror
block, BC capture block and the mirror file it read with its pinned ``mirror_state``; names,
counts and that one path, in the session's own directory); its report's
``build_seconds_split`` says where the build's seconds went (``build_split``: the
fingerprint pass, each projector stage, the finish). A consumer given
``--session-projection`` opens that generation (``open_session``) only when it was
committed in this verifier session (``bc_read.FRESH_SINCE_ENV``, which the verifier sets;
unset outside a session, so nothing is reused), the manifest names it and the session
mirror still holds the pinned state (a top-up after the producer: cold): then it neither
syncs the Portal nor captures BC, projects from the producer's captures, and
``build_generation(..., reuse=True)`` returns the session generation itself when every unit
key matches (``session_projection.mode`` ``reused``: nothing projected, the store linked
read-only) or projects only what differs (``incremental``: its mail units). Otherwise it
projects cold as before (``session_projection.mode`` ``cold``), but a producer that captured
BC and stopped before it committed still seeds the consumer's empty BC capture
(``seed_session_bc``, ``bc_capture.session_seeded``): the units this session completed are
reused (``bc_capture.units_reused``), not read again. The rebuild of the full
probe stays a cold build, so ``rebuild_digest_equal`` then compares the session generation
with a cold build of the same captures. Every report that compared carries
``rebuild_digest_mismatches`` (1 when the digests differ, else 0).

**Cold mail run** (``ontology.mentions``; decision 2026-10-14-mentions-cold-path, option 3):
a full probe with ``--mail-capture-scope`` that did not open the session projection builds
once and runs no rebuild (two cold builds of the live captures do not fit the host's disk).
Its report has no ``rebuild_digest_equal`` and no ``rebuild_skipped``: it records
``rebuild_digest_not_asserted`` ``cold_path_single_build`` (``COLD_SINGLE_BUILD``) and
``rebuild_digest_mismatches`` 0. Full-size determinism is asserted by ``ontology.projection``
(no mail, whose cold path still rebuilds) and ``ontology.session_projection``; the session
path of the mail run still rebuilds cold and asserts ``rebuild_digest_equal``.

**Session mail** (decision 2026-10-14-mentions-cold-path, option 4): the mail consumers
(``workspace.object_view``, ``session.replay``, ``ontology.mentions``) share one store and
served state per session (the verifier's ``/ontology.sqlite``). The producer given
``--mail-capture-scope``, ``--live-audience`` and ``--store`` (that shared store) captures
that mail once into its served state (``session_mail``: ``captured``, ``complete``,
``reasons``, ``messages_seen``, or a failure's ``error_code`` / ``http_status``; never a
blocked source of the session projection, which holds no mail, and Graph not configured
is such a failure) and names it in the manifest's ``mail`` block: the served state, the
capture's identity (``mail_identity``: its scope and audience files, principal and page
budget), what it holds (``mail_mark``) and its summary. A consumer of the opened session
under the same identity whose capture still holds that mark does not capture
(``session_projection.mail_capture`` ``session``; else ``own``, its own capture as before).
Its generation is its own (``<store>.generations``): when the one it committed in this
session over the same session generation and mail mark is there (``SESSION_BASE``),
``build_generation`` starts from it (``reused`` when the keys match: nothing written);
otherwise it first frees the store it replaces (``free_store``), then builds from the
session generation (``incremental``: its mail units) and keeps that one generation only,
so the session holds one mail store, never one per consumer.

A SQLite error that stops the probe prints its error line with ``code`` the SQLite error
name (``sqlite_code``: ``SQLITE_FULL``, ``SQLITE_BUSY``, ``SQLITE_IOERR``...), the ``phase``
reached and the ``store`` that phase writes (``STORES``; a read of its sources inside the
phase is named by it too; a check after the builds names the projection it reads, the
session's when the store is the session generation), so a daily names a host limit or a
lock without its message. Its ``disk`` holds numbers only: ``free_bytes`` (available to the
probe) and ``total_bytes`` of the file systems holding that store's directory
(``store_dir``) and SQLite's temp directory (``sqlite_temp_dir``: ``SQLITE_TMPDIR``, else the
system temp directory), never a path. Its ``phase_seconds`` times the phases up to the
stop, as a report's does. A consumer of ``--build-only`` passes a failed
build's line on (``build_failure``): its ``code``, ``phase``, ``store`` and ``disk``.

Disk (counts only): before a full build (the producer's, a cold build and the rebuild) the
probe compares the free bytes of the store's file system with what the build needs: the
largest ``disk.peak.build_bytes`` a check of the previous daily report recorded under the
projector's current working-file layout (``previous_peak``; ``disk.peak.layout`` is
``ontology.project.WORK_LAYOUT``, so a peak of an older build is never this one's need),
else a forecast: ``BUILD_BYTES_PER_SOURCE_BYTE`` times the bytes of the build's source
files, never less than an older layout's recorded peak times ``LAYOUT_PEAK_SCALE``
(``forecast_floor``). A mail consumer's build from the session generation (after it freed the store
it replaces) needs the session store's bytes (its copy) plus
``BUILD_BYTES_PER_SOURCE_BYTE`` times the mail capture's, never a cold build's peak; one it
reuses needs nothing; one on its own earlier generation in which a unit changed needs that
store's bytes plus the same, checked just before the copy (``build_generation``'s
``before_seed``). Short, no
build starts: the line is ``error`` and ``code`` ``disk_insufficient`` with ``need_bytes``
and ``free_bytes`` (after a first build that ran its checks, the report keeps them and has
no ``rebuild_digest_equal``). Every report that built carries ``disk.peak`` (``PEAK_KEYS``):
``build_bytes``, the most free space one build took (sampled while it ran), ``store_bytes``
of the largest store it wrote, ``mirror_bytes`` of the mirror it read,
``scratch_bytes``, what a build took beyond its store (the projector's working file and
SQLite's temp files), ``work_bytes`` of the largest working file, ``free_bytes`` free when
the build that took the most started, ``objects`` of the largest build (after the ETL
copies are dropped, ``etl_duplicates_collapsed``) and ``layout``; and ``disk.stages``
(``STAGE_KEYS``), what the working file and the generation held when each projector stage
ended (``ontology.project.Projector.on_stage``).

The output never contains a row value, key or citation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

PROBE = "ontology-projection/v1"
SYNC_SHARE = 0.6  # of --budget-seconds; the builds and checks run in the rest
# With --bc-capture the Portal sync gets half its share, and the BC capture SYNC_SHARE of
# what is left after it (all of it goes to BC when the Portal is down at once).
PORTAL_SHARE_WITH_BC = 0.5
SERVED_DIR = "served-state"      # the throwaway host state directory, next to the store
SERVED_CONFIG = "probe-host.json"  # its plx-host-config/v1
TENANT = "plx"
DENIED_PRINCIPAL = "probe:denied-reader"
# Granted the records (the generated mirror audience, the example audience files), never
# the mail: the reader of the mention leak check.
MAIL_WITHHELD_PRINCIPAL = "probe:mail-withheld-reader"
# Granted only the SharePoint capture's root folders, by the capture's own read-back
# (capture_sharepoint.folder_only_audience), never by an audience file: apart from
# DENIED_PRINCIPAL, so the read-back's withheld and folder-only readers are two.
SHAREPOINT_FOLDER_PRINCIPAL = "probe:sharepoint-folder-only-reader"
NOT_CHECKED = "not_checked"   # a leak count whose check did not run: never reported as 0
PORTAL, BC, GRAPH = "portal-workbench-api", "business-central", "microsoft-graph"
# devloop.live_verify.MAX_BLOCKED_PARTS: the verifier keeps no more parts than this.
MAX_BLOCKED_PARTS = 20
EXIT_BUDGET = 4
EXIT_RSS = 5   # the watchdog stopped the probe over --max-rss-mb
RSS_SAMPLE_SECONDS = 1.0   # how often the watchdog reads the resident memory
SESSION_MANIFEST = "session.json"   # in the session projection directory (module text)
SESSION_FORMAT = "plx-session-projection/v1"
SESSION_CAPTURES = "captures"       # the producer's BC capture
SESSION_BC = "bc-capture.sqlite"
# Beside a mail consumer's generations (``<store>.generations``): the session generation
# and the mail capture its current generation was built on (``_reusable_local``).
SESSION_BASE = "session-base.json"
# The phases ``_Budget.at`` enters, in order (``start``: before the first), each timed in
# ``phase_seconds``.
# ``served``: the served state's host config (``prepare_served``), timed apart from the sync.
PHASES = ("start", "mirror_snapshot", "sync", "served", "bc_capture", "mail_capture",
          "sharepoint_capture", "projection", "rebuild", "publish", "claims", "denied", "removals", "mentions")
REMOVAL_SAMPLE = 200   # removed objects read back through the served host (removal_checks)
# The store's latest knowledge as a read time (``ontology.store.LATEST_SQL``): a removal
# still open is in force.
LATEST_AT = "9999-12-31T23:59:59.999Z"
# The store a phase writes, named by a SQLite error raised in it (``_Budget.at``).
STORES = ("mirror", "served_state", "ontology_store", "session_projection", "bc_capture",
          "mail_capture", "sharepoint_capture")
# SQLite's own error name (``sqlite3.Error.sqlite_errorname``, Python 3.11+), else the
# primary code its message stands for: classified here, the message never printed.
SQLITE_NAME = re.compile(r"SQLITE_[A-Z_]{1,40}")
SQLITE_MESSAGES = (("database or disk is full", "SQLITE_FULL"),
                   ("disk i/o error", "SQLITE_IOERR"),
                   ("database table is locked", "SQLITE_LOCKED"),
                   ("database schema is locked", "SQLITE_LOCKED"),
                   ("database is locked", "SQLITE_BUSY"),
                   ("unable to open database", "SQLITE_CANTOPEN"),
                   ("readonly database", "SQLITE_READONLY"),
                   ("disk image is malformed", "SQLITE_CORRUPT"),
                   ("file is not a database", "SQLITE_NOTADB"))
SQLITE_UNCLASSIFIED = "sqlite_unclassified"
TRANSFORMING_PARSERS = frozenset({"identifier", "yyyymmdd_date", "iso_timestamp",
                                  "boolean_text"})
_DROP = object()
# What a consumer of --build-only may pass on from a budget stop's partial report: a
# field's kind, a nested block's own fields, a list's item fields. Anything else is dropped.
_KINDS: dict[str, Callable[[Any], bool]] = {
    "count": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "code": lambda v: isinstance(v, str), "flag": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "name": lambda v: isinstance(v, str) and NAME.fullmatch(v) is not None}
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:\-]{0,63}")
# The file systems an error line's ``disk`` measures (``disk_space``).
DISK_DIRS = ("store_dir", "sqlite_temp_dir")
# A report's ``disk.peak`` (module text, "Disk"); the verifier records these
# (devloop/live_verify.py ENVELOPE_DISK).
PEAK_KEYS = ("build_bytes", "store_bytes", "mirror_bytes", "scratch_bytes", "work_bytes",
             "free_bytes", "objects", "layout")
# A report's ``disk.stages``: per projector stage (``ontology.project.STAGES``), the most its
# working file and generation held on disk when that stage ended.
STAGE_KEYS = ("load", "links", "lineage", "versions", "values", "conflicts", "events",
              "links_written")
# A session build's ``build_seconds_split`` (``build_split``): its fingerprint pass, each
# projector stage and its finish, in build order.
SPLIT_KEYS = ("fingerprint", *STAGE_KEYS, "finish")
DISK_INSUFFICIENT = "disk_insufficient"
# The build report count of the ETL re-inserted copies the projection dropped
# (``ontology.project.ETL_REINSERTED``): reported as ``etl_duplicates_collapsed``.
ETL_COLLAPSED = "objects.etl_duplicates_collapsed"
# Why a report has no rebuild_digest_equal although it ran its checks: the mail run's cold
# path builds once (module text, "Cold mail run").
COLD_SINGLE_BUILD = "cold_path_single_build"
# A FORECAST, not a measurement: the need before a peak of the current working-file layout
# was recorded. Measured 2026-10-14 (dailies 426bde2e, 5a0a9e47) under layout 1: 16-21 GB
# over a 5-6 GB session mirror and its BC capture (3x). Layout 2 (work file once,
# compressed; store v6) is measured offline on a stride sample only, so the need is the
# larger of this ratio and an older layout's recorded peak scaled by ``LAYOUT_PEAK_SCALE``
# (``forecast_floor``), until a daily records a layout-2 peak.
BUILD_BYTES_PER_SOURCE_BYTE = 2
# Forecast: a layout-2 build's peak over a layout-1 build's on the same captures (offline,
# stride-4 sample of the session mirror; devloop/DECISIONS/2026-10-18-projection-store-v6.md):
# 1.83 GB under layout 2 + store v6 against 3.35 GB under layout 1 + store v5 (0.55),
# rounded up so the floor errs toward refusing.
LAYOUT_PEAK_SCALE = 0.6
DISK_SAMPLE_SECONDS = 1.0  # how often a running build's free space is read
# What a consumer of --build-only passes on from a failed build's error line
# (``build_failure``); anything else is dropped.
FAILURE_FIELDS: Mapping[str, Any] = {
    "error": "name", "code": "name", "phase": "name", "store": "name",
    "need_bytes": "int", "free_bytes": "int",
    "disk": {**{name: {"free_bytes": "int", "total_bytes": "int"} for name in DISK_DIRS},
             "peak": {name: "int" for name in PEAK_KEYS},
             "stages": {name: "int" for name in STAGE_KEYS}}}
PARTIAL_FIELDS: Mapping[str, Any] = {
    "phase": "code", "budget_seconds": "count", "elapsed_seconds": "count",
    "phase_seconds": {name: "count" for name in PHASES}, "rebuild_skipped": "flag",
    "checks_budget_seconds": "count",
    "maxrss_mb": "count", "rss_mb": "count", "max_rss_budget_mb": "count",
    "build_seconds": "count", "objects": "count", "values": "count",
    "session_projection": {"mode": "code", "units_rebuilt": "count",
                           "mail_capture": "code"},
    "mirror_relation_count": "count", "rows_current_total": "count",
    "completeness": {"metric": "code", "scope": "code", "bound_declared_total": "count",
                     "bound_total": "count", "complete_bound_relation_count": "count",
                     "completeness_rate": "count", "all_bound_complete": "flag"},
    "bc_capture": {"evidence_label": "code", "topped_up": "flag", "units_reused": "count",
                   "session_seeded": "flag",
                   "capture_age_seconds": "count",
                   "environment_is_production": "flag", "companies_captured": "count",
                   "units_total": "count", "units_reached": "count",
                   "units_complete": "count", "all_complete": "flag",
                   "stopped_reason": "code", "records_current": "count", "seconds": "count",
                   "reason": "code"},
    "blocked_upstream": [{"part": "code", "source": "code", "http_status": "count",
                          "error_code": "code"}]}


class _Budget:
    """One probe-wide clock: ``check(phase)`` raises once the budget is spent; ``at``
    times each phase (``timings``)."""

    class Spent(Exception):
        def __init__(self, phase: str) -> None:
            super().__init__(phase)
            self.phase = phase

    def __init__(self, seconds: float | None, monotonic: Callable[[], float],
                 checks_seconds: float | None = None) -> None:
        self._monotonic = monotonic
        self.seconds = seconds
        self.checks_seconds = checks_seconds
        self._start = monotonic()
        self.where: dict[str, str | None] = {"phase": "start", "store": None}
        self.directory: str | None = None
        self._entered = self._start
        self._seconds: dict[str, float] = {}
        self._checks_until: float | None = None   # elapsed seconds the checks must end by

    def at(self, phase: str, store: str, directory: str) -> None:
        """The phase the probe is in (``PHASES``), the store (``STORES``) it writes there
        and the directory that store is in (measured by ``disk_space``, never printed)."""
        if phase not in PHASES:
            raise ValueError(f"unknown probe phase {phase!r}")
        self._close()
        self.where = {"phase": phase, "store": store}
        self.directory = directory

    def _close(self) -> None:
        now = self._monotonic()
        name = str(self.where["phase"])
        self._seconds[name] = self._seconds.get(name, 0.0) + now - self._entered
        self._entered = now

    def timings(self) -> dict[str, float]:
        """Seconds spent in each phase entered so far, the current one up to now."""
        self._close()
        return {name: round(seconds, 3) for name, seconds in self._seconds.items()}

    def elapsed(self) -> float:
        return self._monotonic() - self._start

    def start_checks(self) -> None:
        """The checks and the rebuild end ``checks_seconds`` from now (within the probe-wide
        budget; ``--checks-budget-seconds``)."""
        if self.checks_seconds is not None:
            self._checks_until = self.elapsed() + self.checks_seconds

    def _deadline(self) -> float | None:
        ends = [end for end in (self.seconds, self._checks_until) if end is not None]
        return min(ends) if ends else None

    def remaining(self) -> float | None:
        end = self._deadline()
        return None if end is None else max(0.0, end - self.elapsed())

    def checkpoint(self, phase: str) -> Callable[[], None]:
        """A no-argument check for loops inside one phase."""
        return lambda: self.check(phase)

    def check(self, phase: str) -> None:
        end = self._deadline()
        if end is not None and self.elapsed() >= end:
            raise _Budget.Spent(phase)


def sqlite_code(exc: sqlite3.Error) -> str:
    """The SQLite error name of ``exc`` (module text); ``SQLITE_UNCLASSIFIED`` when neither
    its name nor its message is a known one."""
    name = getattr(exc, "sqlite_errorname", None)
    if isinstance(name, str) and SQLITE_NAME.fullmatch(name):
        return name
    text = str(exc).lower()
    return next((code for fragment, code in SQLITE_MESSAGES if fragment in text),
                SQLITE_UNCLASSIFIED)


def _space(directory: str) -> dict[str, int] | None:
    """Available and total bytes of the file system holding ``directory`` (its nearest
    existing ancestor), or None when it cannot be measured."""
    directory = os.path.abspath(directory)
    while not os.path.isdir(directory) and os.path.dirname(directory) != directory:
        directory = os.path.dirname(directory)
    try:
        st = os.statvfs(directory)
    except (OSError, AttributeError):   # AttributeError: no statvfs on this platform
        return None
    return {"free_bytes": st.f_bavail * st.f_frsize, "total_bytes": st.f_blocks * st.f_frsize}


class DiskInsufficient(Exception):
    """A full build not started: the store's file system has less free space than it needs
    (module text, "Disk")."""

    code = DISK_INSUFFICIENT

    def __init__(self, need_bytes: int, free_bytes: int, directory: str) -> None:
        super().__init__(DISK_INSUFFICIENT)
        self.need_bytes, self.free_bytes, self.directory = need_bytes, free_bytes, directory


def file_bytes(path: str | None) -> int:
    """The bytes of a SQLite file with its ``-wal`` (its data; the ``-shm`` index is not),
    or of a directory's files; 0 for none."""
    if not path:
        return 0
    if os.path.isdir(path):
        return sum(os.path.getsize(os.path.join(top, name))
                   for top, _, names in os.walk(path) for name in names
                   if not os.path.islink(os.path.join(top, name)))
    return sum(os.path.getsize(path + suffix) for suffix in ("", "-wal")
               if os.path.isfile(path + suffix))


def previous_peak(path: str | None) -> int | None:
    """The largest ``disk.peak.build_bytes`` a check of an earlier verifier report
    recorded under the projector's current working-file layout (``disk.peak.layout`` ==
    ``ontology.project.WORK_LAYOUT``: one full build's need), or None without one. A peak
    of another layout, or with none, measured a different build and is not a need."""
    from ontology.project import WORK_LAYOUT

    found = [value for value, layout in _recorded_peaks(path) if layout == WORK_LAYOUT]
    return max(found) if found else None


def forecast_floor(path: str | None) -> int | None:
    """The largest ``disk.peak.build_bytes`` an earlier verifier report recorded under an
    older working-file layout (``disk.peak.layout`` absent or not ``WORK_LAYOUT``),
    times ``LAYOUT_PEAK_SCALE``: a forecast floor under the need while no peak of the
    current layout exists (``previous_peak``). None without one."""
    from ontology.project import WORK_LAYOUT

    found = [value for value, layout in _recorded_peaks(path) if layout != WORK_LAYOUT]
    return int(max(found) * LAYOUT_PEAK_SCALE) if found else None


def _recorded_peaks(path: str | None) -> list[tuple[int, Any]]:
    """(``build_bytes``, ``layout``) of every ``disk.peak`` the report at ``path`` holds."""
    if path is None:
        return []
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    checks = report.get("checks") if isinstance(report, Mapping) else None
    found = []
    for check in checks if isinstance(checks, list) else []:
        detail = check.get("detail") if isinstance(check, Mapping) else None
        fields = detail.get("fields") if isinstance(detail, Mapping) else None
        disk = fields.get("disk") if isinstance(fields, Mapping) else None
        peak = disk.get("peak") if isinstance(disk, Mapping) else None
        if not isinstance(peak, Mapping):
            continue
        value = peak.get("build_bytes")
        if type(value) is int and value > 0:
            found.append((value, peak.get("layout")))
    return found


class _Disk:
    """The probe's free-space check before each full build and what its builds took
    (``disk.peak``; module text, "Disk")."""

    def __init__(self, need: int | None, monotonic: Callable[[], float], *,
                 floor: int | None = None) -> None:
        self.need = need   # the previous run's peak (``previous_peak``), else None
        self.floor = floor   # without one: the forecast's floor (``forecast_floor``)
        self._monotonic = monotonic
        self.peak: dict[str, int] = {}
        self.stages: dict[str, int] = {}

    def require(self, directory: str, sources: Sequence[str | None]) -> None:
        """Raise ``DiskInsufficient`` when the file system of ``directory`` has less free
        space than one build over ``sources`` needs."""
        self.require_bytes(directory, self.need if self.need is not None else max(
            BUILD_BYTES_PER_SOURCE_BYTE * sum(file_bytes(source) for source in sources),
            self.floor or 0))

    def require_bytes(self, directory: str, need: int) -> None:
        """Raise ``DiskInsufficient`` when ``directory``'s file system has less than
        ``need`` bytes free."""
        space = _space(directory)
        if space is not None and space["free_bytes"] < need:
            raise DiskInsufficient(need, space["free_bytes"], directory)

    @contextmanager
    def measured(self, directory: str, tick: Callable[[], None]) -> Iterator[Callable[[], None]]:
        """Run one build with ``tick`` (its checkpoint) wrapped to sample the free space
        of ``directory``; ``build_bytes`` keeps the most it took."""
        start = _space(directory)
        least = [None if start is None else start["free_bytes"]]
        last = [self._monotonic()]

        def sample() -> None:
            space = _space(directory)
            if space is not None and least[0] is not None:
                least[0] = min(least[0], space["free_bytes"])

        def sampled() -> None:
            now = self._monotonic()
            if now - last[0] >= DISK_SAMPLE_SECONDS:
                last[0] = now
                sample()
            tick()
        try:
            yield sampled
        finally:
            sample()
            if start is not None and least[0] is not None:
                took = start["free_bytes"] - least[0]
                if took >= self.peak.get("build_bytes", 0):
                    self.peak["free_bytes"] = start["free_bytes"]
                self.keep("build_bytes", took)

    def stage(self, name: str, work_bytes: int, build_bytes: int) -> None:
        """A projector stage ended (``Projector.on_stage``): keep what it held on disk."""
        if name in STAGE_KEYS:
            self.stages[name] = max(self.stages.get(name, 0), work_bytes + build_bytes)
        self.keep("work_bytes", work_bytes)

    def built(self, report: Mapping[str, Any]) -> None:
        """Keep the object count of the largest build (the de-duplicated count)."""
        objects = (report.get("counts") or {}).get("objects")
        if type(objects) is int:
            self.keep("objects", objects)

    def keep(self, name: str, value: int) -> None:
        self.peak[name] = max(self.peak.get(name, 0), max(0, int(value)))

    def report(self) -> dict[str, Any]:
        """``{"disk": {"peak": ...}}`` once a build ran, else empty."""
        if "build_bytes" not in self.peak:
            return {}
        from ontology.project import WORK_LAYOUT

        self.keep("scratch_bytes",
                  self.peak["build_bytes"] - self.peak.get("store_bytes", 0))
        self.peak["layout"] = WORK_LAYOUT
        out: dict[str, Any] = {"peak": {name: self.peak.get(name, 0) for name in PEAK_KEYS}}
        if self.stages:
            out["stages"] = {name: self.stages[name] for name in STAGE_KEYS
                             if name in self.stages}
        return {"disk": out}


def disk_insufficient(exc: DiskInsufficient) -> dict[str, Any]:
    """The fields a disk refusal reports: its code and byte counts (never a path)."""
    return {"error": DISK_INSUFFICIENT, "code": DISK_INSUFFICIENT,
            "need_bytes": exc.need_bytes, "free_bytes": exc.free_bytes}


def mirror_state(conn: sqlite3.Connection) -> str:
    """A digest of what a source mirror holds as ``conn`` reads it: its meta, captures and
    their states, relation states and removal labels. A sync or relabel changes it."""
    parts = [conn.execute("SELECT name, value FROM mirror_meta ORDER BY name").fetchall(),
             conn.execute("SELECT COUNT(*), MAX(capture_id) FROM capture").fetchall(),
             conn.execute("SELECT state, COUNT(*), MAX(capture_id) FROM capture_state "
                          "GROUP BY state ORDER BY state").fetchall(),
             conn.execute("SELECT relation, last_capture_id, last_full_capture_id, "
                          "last_refresh_at, row_count, absent_count, watermark "
                          "FROM relation_state ORDER BY relation").fetchall(),
             conn.execute("SELECT COUNT(*), COUNT(replaced_by) FROM row_removal").fetchall()]
    return "sha256:" + sha256(json.dumps(parts, default=str).encode()).hexdigest()


def read_mirror_state(db: str) -> str | None:
    """``mirror_state`` of the file ``db`` read now (read-only), None when it cannot be."""
    try:
        conn = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True)
        try:
            return mirror_state(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None


def upstream_unavailable(db: str) -> set[str]:
    """The relations the mirror file ``db``'s cached upstream catalog lists with an
    ``unavailable`` reason (read-only); empty when it cannot be read, so nothing is
    excused (fail closed). A relation unserved without a reason is not among them."""
    try:
        conn = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT relations_json FROM upstream_catalog "
                               "WHERE id = 1").fetchone()
        finally:
            conn.close()
        catalog = json.loads(row[0]) if row is not None else {}
    except (sqlite3.Error, OSError, ValueError):
        return set()
    if not isinstance(catalog, Mapping):
        return set()
    return {name for name, entry in catalog.items()
            if isinstance(entry, Mapping) and entry.get("unavailable")}


def _mirror_complete(block: Mapping[str, Any], unavailable: set[str],
                     blocked: list[dict[str, Any]]) -> dict[str, Any]:
    """``mirror_complete`` over the bound relations available upstream: every bound
    relation complete but those ``unavailable`` (``upstream_unavailable``), and at least
    one available. Each unavailable bound relation becomes a ``blocked_upstream`` part
    appended to ``blocked`` (up to ``MAX_BLOCKED_PARTS`` parts in all). A block without
    ``bound_incomplete`` excuses nothing."""
    incomplete = block.get("bound_incomplete")
    if not isinstance(incomplete, list):
        return {"mirror_complete": block["all_bound_complete"],
                "mirror_unavailable_bound_count": 0, "mirror_unavailable_bound": []}
    excused = sorted(set(incomplete) & unavailable)
    room = max(0, MAX_BLOCKED_PARTS - len(blocked))
    blocked += [{**_part(name, PORTAL, error_code="relation_unavailable"),
                 "cause": "relation_unavailable"} for name in excused[:room]]
    return {"mirror_complete": not set(incomplete) - unavailable
            and block["bound_total"] - len(excused) > 0,
            "mirror_unavailable_bound_count": len(excused),
            "mirror_unavailable_bound": excused[:MAX_BLOCKED_PARTS]}


def disk_space(directory: str | None) -> dict[str, Any]:
    """An error line's ``disk``: ``_space`` of the store's ``directory`` and of SQLite's
    temp directory (``SQLITE_TMPDIR``, else the system temp directory). Numbers only."""
    temp = os.environ.get("SQLITE_TMPDIR") or tempfile.gettempdir()
    return {"store_dir": None if directory is None else _space(directory),
            "sqlite_temp_dir": _space(temp)}


def error_line(exc: BaseException) -> dict[str, Any]:
    """The error line of an exception that stopped the probe: its class and code, and for
    a SQLite error the SQLite code with the phase and store it was raised in, the
    ``disk`` space where it stopped and the probe's ``phase_seconds`` up to then (when the
    probe timed it). Never its message."""
    if isinstance(exc, DiskInsufficient):
        where = getattr(exc, "probe_where", None)
        where = where if isinstance(where, dict) else {}
        return {"probe": PROBE, **disk_insufficient(exc), "phase": where.get("phase"),
                "store": where.get("store"), "disk": disk_space(exc.directory)}
    if not isinstance(exc, sqlite3.Error):
        where = getattr(exc, "probe_where", None)
        return {"probe": PROBE, "error": type(exc).__name__, "code": getattr(exc, "code", None),
                **({"phase": where.get("phase")} if isinstance(where, dict) else {})}
    where = getattr(exc, "probe_where", None)
    where = where if isinstance(where, dict) else {}
    disk = getattr(exc, "probe_disk", None)
    timings = getattr(exc, "probe_phase_seconds", None)
    return {"probe": PROBE, "error": type(exc).__name__, "code": sqlite_code(exc),
            "phase": where.get("phase"), "store": where.get("store"),
            **({"disk": disk} if isinstance(disk, dict) else {}),
            **({"phase_seconds": timings} if isinstance(timings, dict) else {})}


def consumer_error(exc: BaseException, probe: str, phase: str | None, store: str
                   ) -> dict[str, Any]:
    """The error line of what stopped a consumer of the build (``probe``): a SQLite error
    raised in the consumer's own ``phase`` (after a successful build) as the projection
    probe's ``error_line``: its code, the phase and the disk space next to ``store``,
    where the served state is laid out (``store`` None: the served host reads several).
    Anything else, or a failure before the build succeeded, its class only.
    Never a message or a path."""
    if phase is None or not isinstance(exc, sqlite3.Error):
        return {"probe": probe, "error": type(exc).__name__}
    exc.probe_where = {"phase": phase, "store": None}
    exc.probe_disk = disk_space(os.path.dirname(os.path.abspath(store)))
    return {**error_line(exc), "probe": probe}


def build_failure(text: str) -> dict[str, Any]:
    """What a consumer of ``--build-only`` passes on from a failed build's output (its
    error line, ``error_line``): ``FAILURE_FIELDS`` only, the build's ``error`` as
    ``build_error``; nothing from output that is not one JSON object."""
    try:
        line = json.loads(text)
    except ValueError:
        return {}
    kept = _keep(line, FAILURE_FIELDS)
    if kept is _DROP:
        return {}
    if "error" in kept:
        kept["build_error"] = kept.pop("error")
    return kept


def _part(part: str, source: str, http_status: Any = None, error_code: Any = None
          ) -> dict[str, Any]:
    """One ``blocked_upstream`` entry: names, a status and a code only (never a message)."""
    return {"part": part, "source": source,
            **({"http_status": http_status} if isinstance(http_status, int)
               and not isinstance(http_status, bool) else {}),
            **({"error_code": error_code} if isinstance(error_code, str) and error_code
               else {})}


def _failure_part(part: str, source: str, exc: Exception) -> dict[str, Any]:
    """A source's raised failure as a part: the guarded sources' ``http_status`` /
    ``error_code`` (``live_source_guard``), else ``status`` / ``code``, else the class."""
    status = getattr(exc, "http_status", None)
    status = status if isinstance(status, int) else getattr(exc, "status", None)
    code = getattr(exc, "error_code", None)
    code = code if isinstance(code, str) else getattr(exc, "code", None)
    return _part(part, source, status, code if isinstance(code, str) else type(exc).__name__)


def cited_handles(value: Any) -> Iterator[str]:
    """Every L1 handle a tool answer cites (``l1_handle`` and ``origin.citation``)."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in ("l1_handle", "citation") and isinstance(item, str):
                yield item
            else:
                yield from cited_handles(item)
    elif isinstance(value, list):
        for item in value:
            yield from cited_handles(item)


def scheme(handle: str) -> str:
    """The scheme of a capture handle (``api``, ``bc``, ``graph-mail``, ...)."""
    return handle.split("://", 1)[0] if "://" in handle else "other"


def _directory(path: str) -> str:
    """The directory a store file is in."""
    return os.path.dirname(os.path.abspath(path))


def served_state_path(store: str) -> str:
    """The throwaway host state directory the probe serves from, next to ``store``: the
    layout ``operating.host`` serves (mirror, BC and mail captures, projection)."""
    return os.path.join(os.path.dirname(os.path.abspath(store)), SERVED_DIR)


def _link(source: str, target: str) -> None:
    """``target`` names ``source``'s file: a hard link, else a copy."""
    import shutil
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.lexists(target):
        os.unlink(target)
    try:
        os.link(source, target)
    except OSError:  # a file system without hard links
        shutil.copyfile(source, target)


def _write_json(path: str, document: Mapping[str, Any]) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True)
    return path


def prepare_served(store: str, principal: str, *, relations: Sequence[str] = (),
                   bc: bool = False, mirror_audience: str | None = None,
                   live_audience: str | None = None, also: Sequence[str] = ()) -> str:
    """Write the host config of the probe's served state directory (``SERVED_CONFIG``)
    with ``principal`` as the capture principal. The audiences are the given files, else
    grants to ``principal`` of what the probe captures: every mirrored relation's schema
    family (to the ``also`` principals too), and BC (``bc:*``) with ``bc``. Returns the
    state directory."""
    from operating import host

    state = served_state_path(store)
    os.makedirs(state, exist_ok=True)
    if mirror_audience is None:
        mirror_audience = _write_json(os.path.join(state, "mirror-audience.json"), {
            "schema": "plx-source-mirror-audience/v1", "tenant": TENANT,
            "audiences": {f"{name}.*": [principal, *also] for name in
                          sorted({relation.split(".", 1)[0] for relation in relations})}})
    if live_audience is None and bc:
        live_audience = _write_json(os.path.join(state, "live-audience.json"), {
            "schema": "plx-live-source-audience/v1", "tenant": TENANT,
            "audiences": {"bc:*": [principal]}})
    config = {"schema": host.CONFIG_SCHEMA, "tenant": TENANT, "capture_principal": principal,
              "mirror_audience": os.path.abspath(mirror_audience)}
    if live_audience is not None:
        config["live_audience"] = os.path.abspath(live_audience)
    _write_json(os.path.join(state, SERVED_CONFIG), config)
    return state


def publish_served(store: str, db: str) -> None:
    """Lay the mirror and the projection out where the host serves them."""
    from operating import host
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE

    state = served_state_path(store)
    if os.path.exists(db):
        _link(db, os.path.join(state, CASE_RUNTIME_DIR, MIRROR_FILE))
    _link(store, str(host.projection_path(Path(state))))


def served_host(store: str, environ: Mapping[str, str] | None = None) -> Any:
    """The ``operating.host.Host`` over the state ``publish_served`` laid out, offline: no
    credentials, so no live reader is configured and nothing is requested. Of ``environ``
    (default: the process's) it reads the people exclusion only, which the captured mail
    and Teams it serves are re-checked against (absent: none of them is served)."""
    from integrations.people_exclusion import ENV_FILE, ENV_IDS
    from operating.host import Host, HostConfig

    env = os.environ if environ is None else environ
    state = served_state_path(store)
    return Host(state, HostConfig.from_file(os.path.join(state, SERVED_CONFIG)),
                environ={k: env[k] for k in (ENV_IDS, ENV_FILE) if k in env})


def reader_actor(host: Any) -> Any:
    """The served host's capture principal as a reader."""
    from ontology.types import Actor
    return Actor(host.config.tenant, frozenset({host.config.principal}), "restricted")


def served_rows(served: Any, actor: Any) -> Callable[[str], Mapping[str, Any] | None]:
    """The cited row of a handle as the served host's claim gate reads it for ``actor``, or
    None: ``plx.investigate``'s reader (``citation_rows``: ``CaseRuntime.claim_row``, the
    one resolver of every scheme, a captured mail version included). Nothing falls back to
    another reader, so a handle the claim gate cannot read counts as unresolved even where
    ``plx.read`` reads it (``served_reads``; ``served_citations`` reports both)."""
    from operating.ontology_tools_investigate import citation_rows

    claim_rows = citation_rows(served.runtime)
    return lambda handle: claim_rows(handle, actor)


def served_reads(served: Any, actor: Any) -> Callable[[str], Mapping[str, Any] | None]:
    """``plx.read``'s result for a handle as the served host reads it for ``actor``
    (``served.evidence.reader``) when it resolved, else None."""
    reader = served.evidence.reader

    def read(handle: str) -> Mapping[str, Any] | None:
        try:
            found = reader.read(handle, actor)
        except Exception:  # noqa: BLE001 - unreadable for this reader: no row
            return None
        return found if found.get("status") == "resolved" else None
    return read


def served_citations(served: Any, actor: Any) -> Callable[[Sequence[str]], dict[str, Any]]:
    """``citation_counts`` of handles over the claim gate (``served_rows``), with each
    served reader's own counts (``by_reader``)."""
    rows, reads = served_rows(served, actor), served_reads(served, actor)

    def count(handles: Sequence[str]) -> dict[str, Any]:
        unique = sorted(set(handles))
        return citation_counts(unique, {h for h in unique if rows(h) is not None},
                               {h for h in unique if reads(h) is not None})
    return count


def citation_counts(handles: Sequence[str], resolved: Any, read: Any = None
                    ) -> dict[str, Any]:
    """Counts of ``handles`` the claim gate ``resolved``, by scheme; with ``read`` (the
    handles ``plx.read`` resolved) also ``by_reader``: each reader's counts on its own, so
    a handle one reader serves and the other does not shows as a difference, never hidden
    by the other."""
    def counts(found: Any) -> dict[str, Any]:
        by_scheme: dict[str, int] = {}
        for handle in handles:
            if handle in found:
                by_scheme[scheme(handle)] = by_scheme.get(scheme(handle), 0) + 1
        total = sum(by_scheme.values())
        return {"checked": len(handles), "resolved": total,
                "unresolved": len(handles) - total, "by_scheme": by_scheme}
    out = counts(resolved)
    if read is not None:
        out["by_reader"] = {"claim_gate": counts(resolved), "plx_read": counts(read)}
    return out


def _claim(value: Mapping[str, Any], resolved: Mapping[str, Any] | None,
           normalizer: Callable[[str, Any], Any] | None, kinds: Mapping[tuple[str, str], Any]
           ) -> tuple[dict[str, Any] | None, str | None]:
    """The value claim a reader would make for one projected value, or a problem code."""
    from ontology.project import _parse
    from operating.claim_checks import dig

    if resolved is None:
        return None, "handle_unresolved"
    found, cell = dig(resolved, value["field_path"])
    if not found:
        return None, "field_absent"
    if value["value"] is None:
        return None, None if cell is None else "null_mismatch"
    claimed = value["value"]
    if value["parser"] in TRANSFORMING_PARSERS:
        kind = kinds.get((value["object_type"], value["property"]))
        parsed, problem = _parse(value["parser"], cell, kind=kind, normalizer=normalizer)
        if problem is not None or parsed != value["value"]:
            return None, "parse_not_reproduced"
        claimed = str(cell).strip()
    if not claimed or len(claimed) > 200:
        return None, "value_not_claimable"
    claim = {"value": claimed, "citation": value["l1_handle"], "field": value["field_path"]}
    if value["value_type"] == "quantity":
        claim["role"] = value["quantity_role"]
    return claim, None


def sample_values(conn: sqlite3.Connection, claim_sample: int, tick: Callable[[], None]
                  ) -> tuple[int, list[dict[str, Any]]]:
    """``values_total`` and ``min(claim_sample, values_total)`` values spread over the
    store, in rowid order, each fetched by a rowid seek, so the work and the rows held are
    O(claim_sample) whatever the store's size (no scan or sort of 9.39M values):

    * one seek per ``span // claim_sample``-th cell rowid from the first (``span`` the
      rowid range), each taking the first value at or after it;
    * targets in a rowid gap (deleted values) meet the same value, so a shortfall is
      topped up round by round: each value taken last round takes the next value after
      it that is not taken yet (a value whose next one is taken stops; that one carries
      on), so the extra values spread over every part of the store the stride reached,
      not only after the gap. At most 3 * claim_sample seeks: each takes a value or
      stops a run.

    Deterministic for a store; with dense rowids written in key order it is the key-order
    stride of every ``values_total // claim_sample``-th value. ``tick``: per seek."""
    # Value rows, packed records too (store layout v10): a count without a filter is the
    # table's own (no scan); the seek below reads the cells only (a record's values have
    # no row number), so the rows a record takes are stepped over, never sampled.
    total = conn.execute("SELECT COUNT(*) FROM value_cells").fetchone()[0]
    low, high = conn.execute("SELECT (SELECT MIN(rowid) FROM value_cells), "
                             "(SELECT MAX(rowid) FROM value_cells)").fetchone()
    want = min(max(0, claim_sample), total)
    taken: dict[int, dict[str, Any]] = {}

    def seek(rowid: int) -> dict[str, Any] | None:
        tick()
        row = conn.execute(
            """SELECT v.*, o.object_type, o.audience_key FROM object_values_numbered v
               JOIN objects o ON o.object_id = v.object_id
               WHERE v.row_no >= ? ORDER BY v.row_no LIMIT 1""", (rowid,)).fetchone()
        return None if row is None else dict(row)

    if want:
        step = max(1, (high - low + 1) // claim_sample)
        for index in range(claim_sample):
            value = seek(low + index * step)
            if value is None:
                break
            taken.setdefault(value["row_no"], value)
    runs = sorted(taken)
    while len(taken) < want and runs:
        carried = []
        for rowid in runs:
            if len(taken) >= want:
                break
            value = seek(rowid + 1)
            if value is not None and value["row_no"] not in taken:
                taken[value["row_no"]] = value
                carried.append(value["row_no"])
        runs = carried
    sample = [taken[rowid] for rowid in sorted(taken)]
    for value in sample:
        del value["row_no"]
    return total, sample


def audience_families(conn: sqlite3.Connection) -> list[str]:
    """The audience families of the store's objects, from ``store_meta.audience_keys``
    (written at build time, as ``ProjectionStore`` reads it), else a scan of ``objects``."""
    row = conn.execute("SELECT value FROM store_meta WHERE key = 'audience_keys'").fetchone()
    keys = json.loads(row[0]) if row is not None else None
    if not (isinstance(keys, list) and all(isinstance(k, str) for k in keys)):
        keys = [r[0] for r in conn.execute("SELECT DISTINCT audience_key FROM objects")]
    return sorted({key.split(":", 1)[0] for key in keys if ":" in key} |
                  {"" for key in keys if ":" not in key})


def sampled_contracts(store: Any, actor: Any, pairs: set[tuple[str, str]]
                      ) -> Callable[[str, Mapping[str, Any], str], Mapping[str, Any] | None]:
    """The store's own batched claim gate for the sampled ``(handle, field)`` ``pairs``:
    ``store.claim_contracts(actor, pairs)`` (one query, not one scan per claim)."""
    return store.claim_contracts(actor, pairs)


def check_store(store_path: str, rows: Callable[[str], Mapping[str, Any] | None], actor: Any,
                schema: Any, *, normalizer: Callable[[str, Any], Any] | None,
                policy: Any, claim_sample: int,
                checkpoint: Callable[[], None] | None = None,
                reads: Callable[[str], Mapping[str, Any] | None] | None = None,
                monotonic: Callable[[], float] = time.monotonic
                ) -> dict[str, Any]:
    """Claim-gate a sample of values (counts only). ``rows``: the served host's claim gate
    for ``actor`` (``served_rows``), so a value counts only when its citation resolves as
    the host serves it; ``citations`` counts the claimed handles resolved, by scheme, and,
    with ``reads`` (``served_reads``: ``plx.read``), each reader's own counts
    (``by_reader``). A withheld citation either reader resolves is a leak.
    ``policy``: the served ``ReadPolicy`` for ``actor`` (``policy_now``). A sampled value
    whose object's audience unit it withholds from ``actor`` is no claim
    (``claim_values_withheld``); its citation (one no granted value shares) must resolve
    for nobody: each that ``rows`` resolves is a leak (``withheld_citations_resolved``).
    The quantity-role contracts are read from the values it lets ``actor`` read.
    ``claims_timing`` splits the seconds: ``scan_seconds`` reading the sample from the
    store (``sample_values``), ``gate_seconds`` the served reads and the claim gate.
    ``checkpoint``: called per sampled value; it may raise to stop."""
    tick = checkpoint or (lambda: None)
    from ontology.store import ProjectionStore, ReadPolicy
    from ontology.store_ddl import MARKINGS
    from operating import claim_checks

    started = monotonic()
    kinds = {(tid, p["name"]): p.get("identifier_kind")
             for tid, t in schema.object_types.items() for p in t.get("properties", [])}
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        total, sample = sample_values(conn, claim_sample, tick)
        families = audience_families(conn)
    finally:
        conn.close()
    scanned = monotonic()
    granted = [v for v in sample if policy.may_read(actor, v["audience_key"])]
    handles = sorted({v["l1_handle"] for v in granted})
    withheld = sorted({v["l1_handle"] for v in sample} - set(handles))
    # A value the projection holds as superseded is history, checked as of its own moment:
    # its revision being superseded since is expected. A current value's is not (the
    # projection is behind its source), so that claim fails claim_changed_since.
    history = {v["l1_handle"] for v in granted if v.get("superseded_at") is not None}
    resolved, read, leaked = {}, set(), 0
    for handle in handles:
        tick()
        found = rows(handle)
        if found is not None and handle in history:
            found = {**found, "changed_since": False}
        if found is not None:
            resolved[handle] = found
        if reads is not None and reads(handle) is not None:
            read.add(handle)
    for handle in withheld:
        tick()
        leaked += rows(handle) is not None or (reads is not None
                                               and reads(handle) is not None)
    citations = citation_counts(handles, resolved, None if reads is None else read)
    items, problems, nulls = [], {}, 0
    for index, value in enumerate(granted):
        tick()
        claim, problem = _claim(value, resolved.get(value["l1_handle"]), normalizer, kinds)
        if problem is not None:
            problems[problem] = problems.get(problem, 0) + 1
            continue
        if claim is None:
            nulls += 1
            continue
        items.append({"id": f"F{index + 1}", "texts": [],
                      "citations": [{"handle": value["l1_handle"], "status": "resolved"}],
                      "value_claims": claim_checks.parse_value_claims(
                          [claim], [value["l1_handle"]], [])})
    # The schema's declared quantity roles of the values the served policy grants
    # ``actor`` (a contract lookup, every marking held: the claim still needs the row).
    contracts_policy = ReadPolicy(
        audiences={family: (lambda reader, rest, _f=family:
                            policy.may_read(reader, f"{_f}:{rest}")) for family in families},
        marking_principals={m: frozenset(actor.principals) for m in MARKINGS})
    with ProjectionStore(store_path, contracts_policy) as store:
        pairs = {(v["l1_handle"], v["field_path"]) for v in granted}
        result = claim_checks.check_items(items, rows=lambda h: resolved.get(h),
                                          contracts=sampled_contracts(store, actor, pairs))
    failures: dict[str, int] = {}
    for failure in result["failures"]:
        failures[failure["code"]] = failures.get(failure["code"], 0) + 1
    checked = result["counts"]["claims"]
    passed = result["counts"]["passed"]
    return {"values_total": total, "claim_sample": len(sample), "claims_checked": checked,
            "claim_values_withheld": len(sample) - len(granted),
            "withheld_citations_probed": len(withheld), "withheld_citations_resolved": leaked,
            "claims_passed": passed, "claim_null_values": nulls,
            "claim_problems": problems, "claim_failures": failures,
            "claim_pass_rate": round(passed / checked, 4) if checked else None,
            "all_sampled_claims_pass": checked > 0 and passed == checked and not problems,
            "citations": citations,
            "claims_timing": {"scan_seconds": round(scanned - started, 3),
                              "gate_seconds": round(monotonic() - scanned, 3)}}


def denied_checks(host: Any, *, withheld: str = DENIED_PRINCIPAL,
                  citation_leaks: int = 0) -> dict[str, Any]:
    """The served host's own trim (``Host.trim_check``: the tools ``mcp`` serves, under
    the policy each call is served with) as the reader and as ``withheld``, a principal
    without any grant: objects the reader is served, and everything the withheld one gets
    back on a sample of objects and the reader on a sample of the objects its grants
    withhold (objects, values, identifiers, links, events, identifier lookups), plus
    ``citation_leaks`` (``check_store``'s ``withheld_citations_resolved``). A trim that
    did not run is ``not_checked`` in every count, never 0."""
    trim = host.trim_check(withheld)
    if trim.get("state") != "checked":
        return dict.fromkeys(("reader_objects", "denied_leaks", "denied_objects_probed",
                              "reader_denied_objects_probed"), NOT_CHECKED)
    return {"reader_objects": trim["capture_principal_objects"],
            "denied_leaks": (trim["withheld_leaks"] + trim["withheld_principal_objects"]
                             + trim["capture_principal_leaks"] + citation_leaks),
            "denied_objects_probed": trim["objects_probed"],
            "reader_denied_objects_probed": trim["capture_principal_denied_probed"]}


def removal_checks(host: Any, store: str, *, sample: int = REMOVAL_SAMPLE,
                   checkpoint: Callable[[], None] | None = None) -> dict[str, Any]:
    """What the source's removals (missing from it, or soft-deleted by it) did to the
    served projection (ING-09, REC-04), counts only, each by the existing rule:

    * ``removal_stretches``: the ``object_removals`` rows by basis; ``removed_objects``:
      the records not held now by the one removal test (``removed_at_sql``), by the basis
      in force (``removals_at``);
    * ``removal_reader``: a sample of those (``sample``, spread over them) read through
      the served host as the reader. One whose audience the served policy withholds
      (``withheld``) must not resolve through the claim gate or ``plx.read``
      (``withheld_leaks``, 0). Of a granted Portal record (an ``api://`` latest version;
      others ``not_mirrored``): ``hidden_from_rows`` (``mirror.rows`` by its key returns
      it not), ``hidden_from_resolve`` of ``resolve_probed`` (``mirror.resolve`` of each
      identifier value it carries names it not) and ``readable_with_label`` (``plx.read``
      of its citation resolves with the gap of its basis, ``REMOVAL_REASONS``);
    * ``removal_reviews``: the served review store's link decisions (``link_history``)
      as ``plx.review`` states them at the store's latest knowledge (``_states``:
      ``ontology.reviews.state_at``): ``checked``, ``citing_removed`` and
      ``withdrawn_source_removed``;
    * ``removal_links_ended`` / ``removal_links_open``: the links on a removed record by
      binding kind, ended at one of its removals' start or still open (``build_store`` and
      its review step end the reviewed, mention and document links there; every reader
      applies the removal test to the rest)."""
    from datetime import datetime
    from types import SimpleNamespace

    from integrations.source_mirror import (DEFAULT_IDENTIFIERS, MirrorError,
                                            _identifier_value, parse_citation)
    from ontology.store import removals_at, removed_at_sql
    from operating.ontology_tools import REMOVAL_REASONS
    from operating.ontology_tools_review import _states

    tick = checkpoint or (lambda: None)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        stretches = dict(conn.execute(
            "SELECT basis, COUNT(*) FROM object_removals GROUP BY basis").fetchall())
        removed = [row[0] for row in conn.execute(
            "SELECT c.object_id FROM (SELECT object_id FROM objects WHERE "
            "missing_from_source_since IS NOT NULL UNION SELECT object_id FROM "
            "object_removals) c WHERE " + removed_at_sql("c.object_id") +
            " ORDER BY c.object_id")]
        bases = removals_at(conn, removed, LATEST_AT)
        by_basis: dict[str, int] = {}
        for basis, _since in bases.values():
            by_basis[basis] = by_basis.get(basis, 0) + 1
        picked = removed[::max(1, -(-len(removed) // max(1, sample)))][:sample]
        latest = {}
        for object_id in picked:
            latest[object_id] = conn.execute(
                "SELECT ov.l1_handle, o.audience_key FROM object_versions ov JOIN objects o "
                "ON o.object_id = ov.object_id WHERE ov.object_id = ? "
                "ORDER BY julianday(ov.recorded_at) DESC LIMIT 1", (object_id,)).fetchone()
        ended: dict[str, int] = {}
        still: dict[str, int] = {}
        ids = json.dumps(removed)
        for kind, at_removal, open_ in conn.execute(
                "SELECT l.binding_kind, SUM(EXISTS (SELECT 1 FROM object_removals r WHERE "
                "r.object_id IN (l.from_object, l.to_object) AND r.since = l.superseded_at)), "
                "SUM(l.superseded_at IS NULL) FROM links l WHERE l.link_id IN ("
                "SELECT link_id FROM links WHERE from_object IN (SELECT value FROM json_each(?)) "
                "UNION SELECT link_id FROM links WHERE to_object IN "
                "(SELECT value FROM json_each(?))) GROUP BY l.binding_kind", (ids, ids)):
            ended[kind], still[kind] = at_removal, open_
    finally:
        conn.close()
    reader = reader_actor(host)
    counts = dict.fromkeys(("sampled", "withheld", "withheld_leaks", "not_mirrored",
                            "hidden_from_rows", "resolve_probed", "hidden_from_resolve",
                            "readable_with_label"), 0)
    counts["sampled"] = len(picked)
    with host.served_tools(reader) as served:
        policy, rows = served.policy_now(reader), served_rows(served, reader)
        reads, mirror = served_reads(served, reader), served.runtime.mirror
        for object_id in picked:
            tick()
            found = latest[object_id]
            if found is None:
                continue
            handle, audience = found
            if not policy.may_read(reader, audience):
                counts["withheld"] += 1
                counts["withheld_leaks"] += rows(handle) is not None or reads(handle) is not None
                continue
            if not handle.startswith("api://"):
                counts["not_mirrored"] += 1
                continue
            relation, _, key, _ = parse_citation(handle)
            try:
                page = mirror.rows(reader, relation, limit=10,
                                   where=[[column, "eq", v] for column, v in key.items()])
            except MirrorError:
                page = None
            counts["hidden_from_rows"] += page is not None and not any(
                item["key"] == key for item in page["rows"])
            read = reads(handle) or {}   # plx.read's result when it resolved
            reason = REMOVAL_REASONS.get(bases.get(object_id, ("",))[0])
            counts["readable_with_label"] += any(gap.get("reason") == reason
                                                 for gap in read.get("gaps", ()))
            cited = {name[4:]: value for name, value in (read.get("fields") or {}).items()
                     if name.startswith("row.")}
            literals = {str(v).strip() for column, *_ in DEFAULT_IDENTIFIERS.get(relation, ())
                        if (v := _identifier_value(cited, column)) not in (None, "")
                        and str(v).strip()}
            if literals:
                counts["resolve_probed"] += 1
                try:
                    named = any(c["relation"] == relation and c["key"] == key
                                for literal in sorted(literals)
                                for c in mirror.resolve(reader, literal, limit=200)["candidates"])
                except MirrorError:
                    named = True   # not checked: never counted hidden
                counts["hidden_from_resolve"] += not named
        history = served.reviews.link_history(TENANT) if served.reviews is not None else []
        states = _states(served.tools, SimpleNamespace(
            at=datetime.fromisoformat(LATEST_AT.replace("Z", "+00:00"))), history)
    gone = set(removed)
    decisions = [r for r in history if r["decision"] != "retract"]
    return {"removal_stretches": stretches, "removal_stretches_total": sum(stretches.values()),
            "removed_objects": by_basis, "removed_objects_total": len(removed),
            "removal_reader": counts,
            "removal_reviews": {
                "checked": len(decisions),
                "citing_removed": sum(any(item["object_id"] in gone for item in r["evidence"])
                                      for r in decisions),
                "withdrawn_source_removed": sum(states.get(r["review_id"]) ==
                                                ("withdrawn", "source_removed")
                                                for r in decisions)},
            "removal_links_ended": ended, "removal_links_open": still}


def mail_mention_checks(host: Any, store: str, *, withheld: str = MAIL_WITHHELD_PRINCIPAL,
                        sample: int = 200) -> dict[str, Any]:
    """Counts of projected mail documents and stored mention links, and what the served
    host (``Host.served_tools``, the policy each call is served with) serves through
    ``plx.around`` from each of a sample of linked objects back over its mention link types
    (every tier) to the reader and to ``withheld``, a principal granted the records but not
    the mail: mention links the reader is served, linked objects ``withheld`` is served,
    and every mention link, document and reached count ``withheld`` gets (leaks)."""
    from ontology.types import Actor
    from operating.ontology_tools_traverse import TIER_ORDER

    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        types = dict(conn.execute("SELECT object_type, COUNT(*) FROM objects WHERE "
                                  "object_type IN ('Email', 'Attachment') "
                                  "GROUP BY object_type").fetchall())
        pairs = conn.execute(
            "SELECT DISTINCT to_object, link_type FROM links WHERE binding_kind = 'mention' "
            "AND to_object IN (SELECT DISTINCT to_object FROM links WHERE binding_kind = "
            "'mention' ORDER BY to_object LIMIT ?) ORDER BY to_object, link_type",
            (sample,)).fetchall()
    finally:
        conn.close()
    reader = reader_actor(host)
    other = Actor(host.config.tenant, frozenset({withheld}), "restricted")
    visible = leaks = 0
    seen: set[str] = set()
    with host.served_tools(reader) as served:
        for object_id, link_type in pairs:
            arguments = {"from": object_id, "limit": 50, "tier_floor": TIER_ORDER[-1],
                         "path": [{"link_type": link_type, "direction": "reverse"}]}
            out = served.call("plx.around", arguments, reader)
            visible += len(out.get("edges") or ())
            out = served.call("plx.around", arguments, other)
            if "error" not in out:
                seen.add(object_id)
                leaks += (len(out["edges"]) + len(out["objects"])
                          + (out["hops"][0]["count"] or 0))
    return {"email_objects": types.get("Email", 0),
            "attachment_objects": types.get("Attachment", 0),
            "mention_objects_probed": len({object_id for object_id, _ in pairs}),
            "mention_objects_visible_to_withheld": len(seen),
            "mention_links_visible_to_reader": visible, "mention_denied_leaks": leaks}


def mail_text_find_checks(host: Any, store: str, *, withheld: str = MAIL_WITHHELD_PRINCIPAL,
                          sample: int = 50) -> dict[str, Any]:
    """What the served host's ``plx.find`` answers for words sampled from the stored text
    of up to ``sample`` captured mail documents (``ontology/text_index.py``): for each
    chunk, its (up to) three longest plain words as the query, read as the capture
    principal and as ``withheld`` (granted the records but not the mail). Counts only:
    found (a candidate that is the chunk's object, cited to its field's citation and
    path), truncated (not found, the answer incomplete), missed (not found, the answer
    complete: a fail), leaks (any mail object ``withheld`` is served) and the slowest
    call's seconds; never a word, value or citation."""
    from ontology.text_index import chunk_text
    from ontology.types import Actor

    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT f.object_id, f.citation, f.field_path, c.body FROM text_fields f "
            "JOIN text_chunks c ON c.field_id = f.field_id JOIN objects o "
            "ON o.object_id = f.object_id WHERE o.object_type IN ('Email', 'Attachment') "
            "AND c.start = 0 ORDER BY f.field_id LIMIT ?", (sample,)).fetchall()
        mail = {row[0] for row in conn.execute(
            "SELECT object_id FROM objects WHERE object_type IN ('Email', 'Attachment')")}
        fields = conn.execute("SELECT COUNT(*) FROM text_fields").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0]
    finally:
        conn.close()
    reader = reader_actor(host)
    other = Actor(host.config.tenant, frozenset({withheld}), "restricted")
    counts = {"text_find_sampled": 0, "text_find_found": 0, "text_find_truncated": 0,
              "text_find_missed": 0, "text_find_denied_leaks": 0, "text_find_errors": 0}
    slowest = 0.0
    with host.served_tools(reader) as served:
        for object_id, citation, field_path, body in rows:
            words = sorted(set(re.findall(r"(?<![^\W_])[A-Za-z]{4,}(?![^\W_])",
                                           chunk_text(body))), key=lambda w: (-len(w), w))[:3]
            if not words:
                continue
            counts["text_find_sampled"] += 1
            arguments = {"query": " ".join(words), "limit": 50}
            started = time.monotonic()
            out = served.call("plx.find", arguments, reader)
            slowest = max(slowest, time.monotonic() - started)
            if "error" in out:
                counts["text_find_errors"] += 1
                continue
            hit = any(c["object"]["object_id"] == object_id
                      and c["matched"]["raw"]["origin"].get("citation") == citation
                      and c["matched"]["raw"]["origin"].get("field_path") == field_path
                      for c in out.get("candidates") or ())
            shown = out.get("completeness") or {}   # a later page is not read: truncated
            complete = shown.get("complete") is True and not shown.get("next_cursor")
            key = "found" if hit else "missed" if complete else "truncated"
            counts["text_find_" + key] += 1
            denied = served.call("plx.find", arguments, other)
            counts["text_find_denied_leaks"] += sum(
                1 for c in denied.get("candidates") or () if c["object"]["object_id"] in mail)
    return {**counts, "text_find_seconds_max": round(slowest, 3),
            "text_index_fields": fields, "text_index_chunks": chunks}


def projection_probe(client: Any, db: str, store: str, *, budget_seconds: float | None = None,
                     claim_sample: int = 2000, max_rss_mb: float | None = None,
                     schema: Any = None,
                     normalizer: Callable[[str, Any], Any] | None = None,
                     unstable: Sequence[str] = (),
                     monotonic: Callable[[], float] = time.monotonic,
                     mail: Mapping[str, Any] | None = None,
                     bc: Mapping[str, Any] | None = None,
                     principal: str | None = None, mirror_audience: str | None = None,
                     live_audience: str | None = None, build_only: bool = False,
                     session: str | None = None, fresh_since: str | None = None,
                     produce: bool = False, disk_need: int | None = None,
                     disk_floor: int | None = None,
                     mail_blocked: Mapping[str, Any] | None = None,
                     checks_budget_seconds: float | None = None,
                     sharepoint: Mapping[str, Any] | None = None,
                     environ: Mapping[str, str] | None = None,
                     **mirror_options: Any) -> dict[str, Any]:
    """``mail``: ``{"scope": CaptureScope, "reader": GraphMailReader, "capture_actor":
    Actor, "page_budget": int | None}`` to capture
    and project mail with stored mention links; ``bc``: ``{"environ", "sender", "books"}``
    (``bc_read.capture_probe``'s) to capture and project BC, every source then one part and
    ``client`` may be None (the Portal not configured) (see the module text).
    ``principal`` reads the served host (default: the mail capture principal, else
    ``operating.host.DEFAULT_PRINCIPAL``) under ``mirror_audience`` / ``live_audience``
    (files; default: grants of what the probe captured, ``publish_served``).
    ``build_only``: one build and its served layout, no rebuild and no checks (``--build-only``).
    ``session``: the session projection directory (``--session-projection``), produced
    (``produce``) or opened when committed since ``fresh_since`` (module text).
    ``disk_need``: the bytes one full build needs (``previous_peak``; None: from its
    sources, never less than ``disk_floor``, ``forecast_floor``), checked before each
    (module text, "Disk"). ``mail_blocked``: the part of a
    session producer's mail capture that could not start (its Graph reader), never a
    blocked source of the session projection. ``checks_budget_seconds``: the seconds the
    claim, trim, removal and mention checks and the rebuild may take once the first build
    is done (``--checks-budget-seconds``), within ``budget_seconds``. ``sharepoint``:
    ``{"scope", "state", "live_audience", "principal", "environ", "budget_seconds"}`` to
    capture that SharePoint scope and project and serve it (``capture_sharepoint``).
    ``environ``: the people exclusion the served host re-checks captured mail against
    (``served_host``; default: the process's)."""
    if principal is None:
        from operating.host import DEFAULT_PRINCIPAL
        principal = (sorted(mail["capture_actor"].principals)[0] if mail is not None
                     else DEFAULT_PRINCIPAL)
    served = {"principal": principal, "mirror_audience": mirror_audience,
              "live_audience": live_audience, "environ": environ}
    budget = _Budget(budget_seconds, monotonic, checks_budget_seconds)
    disk = _Disk(disk_need, monotonic, floor=disk_floor)
    partial: dict[str, Any] = {}
    with _RssWatchdog(max_rss_mb, budget, partial, disk):
        return _probe_guarded(budget, disk, partial, client, db, store, claim_sample,
                              max_rss_mb, schema, normalizer, unstable, monotonic, mail, bc,
                              served, build_only, session, fresh_since, produce, mail_blocked,
                              budget_seconds, mirror_options, principal, sharepoint)


def _probe_guarded(budget, disk, partial, client, db, store, claim_sample, max_rss_mb,
                   schema, normalizer, unstable, monotonic, mail, bc, served, build_only,
                   session, fresh_since, produce, mail_blocked, budget_seconds,
                   mirror_options, principal, sharepoint=None) -> dict[str, Any]:
    try:
        if produce:
            if schema is None:
                from ontology.schema import load_schema
                schema = load_schema()
            if normalizer is None:
                from ontology.project import _default_normalizer
                normalizer = _default_normalizer()
            result = _produce(budget, partial, db, session, bc, principal, unstable, schema,
                              normalizer, monotonic, max_rss_mb, disk,
                              store=store, mail=mail, mail_blocked=mail_blocked)
        else:
            result = _run_probe(budget, partial, client, db, store, claim_sample, max_rss_mb,
                                schema, normalizer, unstable, monotonic, mail, mirror_options,
                                bc, served, build_only, session=session,
                                opened=open_session(session, fresh_since),
                                fresh_since=fresh_since, disk=disk, sharepoint=sharepoint)
        if result.get("rebuild_skipped"):   # every check ran, the rebuild did not fit
            result = {"probe": PROBE, "status": "budget_exhausted", "phase": "rebuild_skipped",
                      "budget_seconds": budget_seconds,
                      "elapsed_seconds": round(budget.elapsed(), 3), **result}
        return {**result, **disk.report(), "phase_seconds": budget.timings()}
    except DiskInsufficient as exc:
        exc.probe_where = dict(budget.where)
        raise
    except sqlite3.Error as exc:
        exc.probe_where = dict(budget.where)   # error_line: where it stopped
        exc.probe_disk = disk_space(budget.directory)
        exc.probe_phase_seconds = budget.timings()
        raise
    except _Budget.Spent as spent:
        return {"probe": PROBE, "status": "budget_exhausted", "phase": spent.phase,
                "budget_seconds": budget_seconds, "elapsed_seconds": round(budget.elapsed(), 3),
                **({"checks_budget_seconds": budget.checks_seconds}
                   if budget.checks_seconds is not None else {}),
                "maxrss_mb": _maxrss_mb(), **partial, **disk.report(),
                "phase_seconds": budget.timings()}
    except Exception as exc:
        if not isinstance(getattr(exc, "probe_where", None), dict):
            exc.probe_where = dict(budget.where)   # error_line: the phase it stopped in
        raise


def _keep(value: Any, spec: Any) -> Any:
    """``value`` cut to ``spec``: a field not listed, or not of its kind (a null kept), is
    ``_DROP``ped; so is a list item or block that is no mapping."""
    if isinstance(spec, str):
        return value if value is None or _KINDS[spec](value) else _DROP
    if isinstance(spec, list):
        return ([kept for item in value if (kept := _keep(item, spec[0])) is not _DROP]
                if isinstance(value, list) else _DROP)
    if not isinstance(value, Mapping):
        return _DROP
    return {key: kept for key, sub in spec.items()
            if key in value and (kept := _keep(value[key], sub)) is not _DROP}


def budget_partial(report: Mapping[str, Any]) -> dict[str, Any]:
    """A budget stop's partial report cut to ``PARTIAL_FIELDS`` (``_keep``)."""
    kept = _keep(report, PARTIAL_FIELDS)
    return {} if kept is _DROP else kept


def bc_capture_path(store: str) -> str:
    """The throwaway BC capture store ``--bc-capture`` writes: where the host keeps its
    own (``operating.host`` ``BC_DIR``/``BC_FILE``) in the served state directory."""
    from operating.host import BC_DIR, BC_FILE
    return os.path.join(served_state_path(store), BC_DIR, BC_FILE)


def capture_bc(bc: Mapping[str, Any], db: str, budget_seconds: float | None, *,
               audience: Any = None, actor: Any = None,
               monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One BC capture pass into ``db`` (GET only) of the entity sets the host captures
    (``bc_read.CAPTURE_ENTITY_SETS``, ``operating.host``'s default), as the host captures:
    under ``audience`` as ``actor``; counts and states only. Paused units resume from their
    cursors; in a verifier session (``bc_read.FRESH_SINCE_ENV`` in ``bc["environ"]``) the
    units that session completed are not read again (``units_reused``), and every other
    unit is read, a store from an earlier invocation included (a refresh)."""
    from integrations import bc_read

    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    report = bc_read.capture_probe(bc.get("environ"), sender=bc.get("sender"), db=db,
                                   budget_seconds=budget_seconds,
                                   entity_sets=bc_read.CAPTURE_ENTITY_SETS, mode="full",
                                   resolve_sample=0, books=bc.get("books"),
                                   audience=audience, actor=actor, monotonic=monotonic,
                                   fresh_since=bc_read.session_fresh_since(bc.get("environ")))
    units = [u for c in report["companies"] for u in c["units"]]
    records = sum(u.get("records_current") or 0 for u in units)
    reasons: dict[str, int] = {}
    for unit in units:
        if not unit.get("complete") and unit.get("reason"):
            reasons[unit["reason"]] = reasons.get(unit["reason"], 0) + 1
    return {"evidence_label": report["evidence_label"],
            "topped_up": report["units_reused"] > 0, "units_reused": report["units_reused"],
            "capture_age_seconds": report["capture_age_seconds"],
            "environment_is_production": report["environment_is_production"],
            "companies_captured": report["companies_captured"],
            "units_total": report["units_total"], "units_reached": report["units_reached"],
            "units_complete": report["units_complete"], "all_complete": report["all_complete"],
            "stopped_reason": report["stopped_reason"],
            "records_current": records,
            "seconds": report["capture_seconds"], "requests_sent": report["requests_sent"],
            # why each unit not complete is not (``company_etag_missing``: BC lists the
            # company without an etag; its records are captured all the same)
            "unit_reasons": reasons,
            # why a capture that held nothing did (its blocked part's code), else None
            "reason": None if records else next((u.get("reason") for u in units
                                                 if u.get("reason")), None)}


def _write_atomic(path: Path, document: Mapping[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def session_mirror(db: str, unstable: Sequence[str] = ()) -> dict[str, Any]:
    """What a mirror file holds, read offline (no client: its cached catalog), as the
    probe's report counts it: relation and current-row counts, the relations served and
    complete, and the one completeness block (``scripts.mirror_measure.completeness``;
    ``unstable`` relations leave its denominator). Names and counts only."""
    from integrations.source_mirror import (PROBE_PRINCIPAL, AudienceMap, SourceMirror,
                                            load_ingest_config, mirror_options,
                                            soft_delete_counts)
    from ontology.core import Actor
    from scripts.mirror_measure import bound_relations, completeness

    actor = Actor(TENANT, frozenset({PROBE_PRINCIPAL}))
    with SourceMirror(db, None, AudienceMap({}, tenant=TENANT), tenant=TENANT,
                      **mirror_options(load_ingest_config())) as mirror:
        catalog, _, _ = mirror._catalog()
        names = set(catalog) | {row[0] for row in mirror._db.execute(
            "SELECT relation FROM relation_state")}
        mirror.set_audience_map(AudienceMap(
            {f"{schema}.*": [PROBE_PRINCIPAL] for schema in
             sorted({name.split(".", 1)[0] for name in names})}, tenant=TENANT))
        coverage = mirror.coverage(actor)
    relations = coverage["relations"]
    complete = sorted(item["relation"] for item in relations if item["complete"])
    return {"relation_count": len(catalog),
            "rows_current_total": sum(item["rows_current"] for item in relations),
            "relations": sorted(item["relation"] for item in relations),
            "served": sorted(item["relation"] for item in relations if item["served"]),
            "complete": complete, "unstable_relations": sorted(set(unstable)),
            "completeness": completeness(complete, bound=bound_relations(),
                                         unstable=unstable),
            **soft_delete_counts(coverage)}


def _session_mirror(root: Path, manifest: Mapping[str, Any]) -> Any:
    """The mirror file a session manifest names: its path relative to the projection
    (``mirror_relpath``) when that file exists, so a projection the warm store restored
    with its mirror into a later session directory (``operating.warm_store``) still finds
    the sibling mirror it was built from; else the absolute ``mirror_file``. Either way
    ``open_session`` still requires the pinned ``mirror_state``, so a moved or rewritten
    mirror never opens a projection built from other rows."""
    relpath = manifest.get("mirror_relpath")
    if isinstance(relpath, str) and relpath and not os.path.isabs(relpath):
        candidate = root / relpath
        if candidate.is_file():
            return str(candidate.resolve())
    return manifest.get("mirror_file")


def open_session(directory: str | None, fresh_since: str | None) -> dict[str, Any] | None:
    """The session projection a consumer may open (module text): ``{"generation",
    "manifest", "mirror", "bc"}`` (the session mirror the producer read and its BC capture,
    None where its manifest names no such source), or None: no directory or session start,
    no generation committed since that start, a manifest that does not name exactly that
    generation, or a session mirror that no longer holds the state the producer pinned."""
    from ontology.project_incremental import current_generation

    if not directory or not fresh_since:
        return None
    generation = current_generation(directory, committed_since=fresh_since)
    if generation is None:
        return None
    root = Path(directory)
    try:
        manifest = json.loads((root / SESSION_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, Mapping) or manifest.get("format") != SESSION_FORMAT \
            or manifest.get("generation") != generation.id \
            or manifest.get("content_digest") != generation.content_digest \
            or not isinstance(manifest.get("sources"), list) \
            or not isinstance(manifest.get("blocked_upstream"), list) \
            or not isinstance(manifest.get("mirror"), (Mapping, type(None))) \
            or ("portal_api" in manifest["sources"]) != isinstance(manifest["mirror"], Mapping) \
            or not isinstance(manifest.get("bc_capture"), (Mapping, type(None))):
        return None
    found = {"mirror": _session_mirror(root, manifest) if "portal_api" in manifest["sources"]
             else None,
             "bc": str(root / SESSION_CAPTURES / SESSION_BC) if "bc" in manifest["sources"]
             else None}
    if any(path is not None and (not isinstance(path, str) or not os.path.isfile(path))
           for path in found.values()):
        return None
    if found["mirror"] is not None and (
            not isinstance(manifest.get("mirror_state"), str)
            or read_mirror_state(found["mirror"]) != manifest["mirror_state"]):
        return None
    return {"generation": generation, "manifest": manifest, **found}


def seed_session_bc(directory: str | None, fresh_since: str | None, target: str) -> bool:
    """Start a consumer's BC capture ``target`` from this verifier session's producer
    capture (``SESSION_DIR/captures/SESSION_BC``) when the session projection did not open
    (the producer captured BC, then stopped before it committed): a consistent copy (the
    SQLite backup API, the producer's file opened read-only and never written), so
    ``capture_bc`` reuses the units the session completed (``units_reused``, its
    ``fresh_since`` rule: a unit begun before the session start is read again) and reads
    only the rest. Nothing is copied outside a session (no directory or start), over a
    capture the consumer already holds (a store a sibling consumer of the session topped
    up), or from no producer capture; a copy SQLite refuses is removed (the capture runs
    cold). True when copied."""
    if not directory or not fresh_since or os.path.lexists(target):
        return False
    source = Path(directory) / SESSION_CAPTURES / SESSION_BC
    if not source.is_file():
        return False
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    try:
        reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        try:
            writer = sqlite3.connect(target)
            try:
                reader.backup(writer)
            finally:
                writer.close()
        finally:
            reader.close()
    except sqlite3.Error:
        for leftover in (target, f"{target}-journal", f"{target}-wal", f"{target}-shm"):
            if os.path.lexists(leftover):
                os.unlink(leftover)
        return False
    return True


def mail_identity(scope: str, audience: str, principal: str, page_budget: int | None) -> str:
    """What a mail capture is bound to: the bytes of its scope and live audience files, its
    capture principal and page budget, as one digest. A consumer reuses the session's mail
    capture only under the identity the producer captured it with."""
    digest = sha256()
    for path in (scope, audience):
        digest.update(Path(path).read_bytes())
        digest.update(b"\x00")
    digest.update(json.dumps([principal, page_budget]).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def mail_mark(state: str) -> str | None:
    """A digest of what the mail capture in ``state`` holds (its message and attachment
    versions, events, blobs and folder rounds; read-only), or None without one: any capture
    run that writes changes it."""
    from integrations.capture_mail import DB_FILE, STORE_DIR

    path = Path(state) / STORE_DIR / DB_FILE
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = [conn.execute(query).fetchall() for query in (
                "SELECT COUNT(*), MAX(version_id) FROM messages",
                "SELECT COUNT(*), MAX(recorded_at) FROM attachments",
                "SELECT COUNT(*) FROM message_events", "SELECT COUNT(*) FROM blobs",
                "SELECT mailbox_id, folder_key, status, reason, round_started_at, "
                "last_run_at, last_complete_at FROM folders ORDER BY mailbox_id, folder_key")]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return "sha256:" + sha256(json.dumps([[list(r) for r in found] for found in rows],
                                         default=str).encode("utf-8")).hexdigest()


def session_mail_capture(manifest: Mapping[str, Any] | None, mail: Mapping[str, Any],
                         state: str) -> dict[str, Any] | None:
    """The producer's capture summary when the session's mail capture is the one this
    consumer would make (module text): the manifest's ``mail`` block names this served
    state and this consumer's capture identity, and the capture still holds what the
    producer captured (``mail_mark``). Anything else (no block, another identity, a
    consumer without one, a capture written since): None, and the consumer captures."""
    held = manifest.get("mail") if isinstance(manifest, Mapping) else None
    identity = mail.get("identity")
    if not isinstance(held, Mapping) or not isinstance(identity, str) or not identity \
            or held.get("identity") != identity \
            or held.get("state") != os.path.abspath(state):
        return None
    summary = held.get("capture")
    counts = summary.get("counts") if isinstance(summary, Mapping) else None
    if not isinstance(counts, Mapping) or not isinstance(summary.get("complete"), bool) \
            or not isinstance(summary.get("reasons"), list) \
            or type(summary.get("folders")) is not int \
            or type(counts.get("messages_seen")) is not int:
        return None
    mark = mail_mark(state)
    if mark is None or held.get("mark") != mark:
        return None
    return dict(summary)


def _reusable_local(store: str, base: Any, mail_state: str, fresh_since: str | None,
                    sharepoint: bool = False) -> Any:
    """The consumer's own generation (``<store>.generations``) committed in this session
    over the session generation ``base`` and the mail capture as it is now
    (``SESSION_BASE``), else None. ``build_generation`` still decides from the unit keys
    whether it is reused; this only says which base to build on. A build with a SharePoint
    capture never builds on one (its capture lives only for its run), and one built with
    it records so: a later consumer without SharePoint builds on the session generation."""
    from ontology.project_incremental import current_generation

    if not fresh_since or sharepoint:
        return None
    local = current_generation(store + ".generations", committed_since=fresh_since)
    if local is None:
        return None
    try:
        recorded = json.loads(Path(store + ".generations", SESSION_BASE)
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mark = mail_mark(mail_state)
    if not isinstance(recorded, Mapping) or mark is None or recorded != {
            "generation": local.id, "content_digest": local.content_digest,
            "base": base.id, "base_digest": base.content_digest, "mail_mark": mark}:
        return None
    return local


def _produce(budget: _Budget, partial: dict[str, Any], db: str, directory: str,
             bc: Mapping[str, Any] | None, principal: str, unstable: Sequence[str],
             schema: Any, normalizer: Callable[[str, Any], Any] | None,
             monotonic: Callable[[], float], max_rss_mb: float | None,
             disk: _Disk, *, store: str | None = None, mail: Mapping[str, Any] | None = None,
             mail_blocked: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``--produce-session``: the session projection of the session's captures (module
    text); counts and codes only. With ``mail`` it also captures that mail once into
    ``store``'s served state, for the session's mail consumers (``session_mail``)."""
    if not os.path.isfile(db):
        return {"probe": PROBE, "error": "session_mirror_missing"}
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    # Commits or leaves none: never an earlier run's manifest beside this run's captures.
    (root / SESSION_MANIFEST).unlink(missing_ok=True)
    budget.at("mirror_snapshot", "mirror", _directory(db))
    # Its completeness, read first: the mirror's own writer opens it (and brings it to the
    # rule in force), so the state pinned next is the one every later read sees.
    held = session_mirror(db, unstable)
    pin = sqlite3.connect(db, isolation_level=None)   # the session's own file, never copied
    try:
        pin.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # every commit in the file itself
        pin.execute("BEGIN")
        pinned = mirror_state(pin)   # the read transaction holds this state until the end
        return _produce_pinned(budget, partial, db, root, bc, principal, unstable, schema,
                               normalizer, monotonic, max_rss_mb, disk, pinned, held,
                               store=store, mail=mail, mail_blocked=mail_blocked)
    finally:
        pin.close()


def _produce_pinned(budget: _Budget, partial: dict[str, Any], db: str, root: Path,
                    bc: Mapping[str, Any] | None, principal: str, unstable: Sequence[str],
                    schema: Any, normalizer: Callable[[str, Any], Any] | None,
                    monotonic: Callable[[], float], max_rss_mb: float | None,
                    disk: _Disk, pinned: str, held: Mapping[str, Any], *,
                    store: str | None = None, mail: Mapping[str, Any] | None = None,
                    mail_blocked: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``_produce`` over the session mirror ``db`` whose state ``pinned`` is held."""
    from contextlib import ExitStack

    from integrations.live_source_guard import LiveSourceAudience
    from ontology.project import MirrorCaptures
    from ontology.project_bc import BcCaptures
    from ontology.project_incremental import build_generation
    from ontology.types import Actor
    from operating import build_steps
    from scripts.mirror_measure import unstable_gaps

    captures = root / SESSION_CAPTURES
    captures.mkdir(parents=True, exist_ok=True)
    disk.keep("mirror_bytes", file_bytes(db))
    blocked: list[dict[str, Any]] = []
    partial.update(blocked_upstream=blocked, mirror_relation_count=held["relation_count"],
                   rows_current_total=held["rows_current_total"],
                   completeness=held["completeness"])
    portal_held = held["completeness"]["complete_bound_relation_count"] > 0
    if not portal_held:
        blocked.append(_part("portal_mirror", PORTAL, error_code="session_mirror_incomplete"))
    budget.check("mirror_snapshot")
    bc_db, bc_result = None, None
    if bc is not None:
        audience = _write_json(str(captures / "live-audience.json"), {
            "schema": "plx-live-source-audience/v1", "tenant": TENANT,
            "audiences": {"bc:*": [principal]}})
        left = budget.remaining()
        budget.at("bc_capture", "bc_capture", str(captures))
        try:
            bc_result = capture_bc(bc, str(captures / SESSION_BC),
                                   None if left is None else left * SYNC_SHARE,
                                   audience=LiveSourceAudience.from_file(audience),
                                   actor=Actor(TENANT, frozenset({principal}), "restricted"),
                                   monotonic=monotonic)
        except Exception as exc:  # noqa: BLE001 - a source read: a part, never a message
            blocked.append(_failure_part("bc_capture", BC, exc))
        else:
            if bc_result["records_current"]:
                bc_db = str(captures / SESSION_BC)
            else:
                blocked.append(_part("bc_capture", BC,
                                     error_code=bc_result["reason"] or "no_records_captured"))
        partial["bc_capture"] = bc_result
        budget.check("bc_capture")
    sources = [name for name, there in (("bc", bc_db is not None),
                                        ("portal_api", portal_held)) if there]
    parts = {"blocked_upstream": blocked, "sources": sources,
             **({"bc_capture": bc_result} if bc is not None else {})}
    if not sources:
        return {"probe": PROBE, "sources_blocked": "no_record_source", **parts,
                "mirror_complete": False, "session_projection": {"committed": False},
                "maxrss_mb": _maxrss_mb()}
    held_mail = None
    if mail_blocked is not None:
        parts["session_mail"] = {"captured": False, **{
            name: mail_blocked[name] for name in ("error_code", "http_status")
            if name in mail_blocked}}
    elif mail is not None and store is not None:
        held_mail, parts["session_mail"] = _produce_mail(budget, store, mail)
    started = monotonic()
    budget.at("projection", "session_projection", str(root))
    disk.require(str(root), [db if portal_held else None, bc_db])
    ended: list[tuple[str, float]] = []   # each projector stage's end (build_seconds_split)

    def on_stage(name: str, work_bytes: int, build_bytes: int) -> None:
        ended.append((name, time.monotonic()))
        disk.stage(name, work_bytes, build_bytes)
    with ExitStack() as stack:
        opened: dict[str, Any] = {}
        if portal_held:
            opened["portal_api"] = stack.enter_context(MirrorCaptures(db))
        if bc_db is not None:
            opened["bc"] = stack.enter_context(BcCaptures(bc_db))
        tick = stack.enter_context(disk.measured(str(root), budget.checkpoint("projection")))
        began = time.monotonic()
        generation = build_generation(root, schema, opened, normalizer=normalizer,
                                      post_project=build_steps.post_project(root, TENANT),
                                      checkpoint=tick, on_stage=on_stage)
    build_seconds = round(monotonic() - started, 3)
    split = build_split(began, generation.seconds, ended)
    disk.keep("store_bytes", file_bytes(str(generation.store)))
    disk.built(generation.report)
    if portal_held and read_mirror_state(db) != pinned:   # a writer committed meanwhile
        return {"probe": PROBE, "error": "session_mirror_changed", **parts,
                "session_projection": {"committed": False}, "maxrss_mb": _maxrss_mb()}
    _write_atomic(root / SESSION_MANIFEST, {
        "format": SESSION_FORMAT, "generation": generation.id,
        "content_digest": generation.content_digest, "sources": sources,
        "blocked_upstream": blocked, "bc_capture": bc_result,
        "mirror": held if portal_held else None,
        "mirror_file": str(Path(db).resolve()) if portal_held else None,
        "mirror_relpath": os.path.relpath(Path(db).resolve(), root.resolve())
        if portal_held else None,
        "mirror_state": pinned if portal_held else None, "mail": held_mail,
        "build_seconds": build_seconds})
    counts = generation.report["counts"]
    judged = (_mirror_complete(held["completeness"], upstream_unavailable(db), blocked)
              if portal_held else {"mirror_complete": False})
    gap_codes: dict[str, int] = {}
    unstable_bindings = unstable_gaps(unstable, schema=schema) if portal_held else []
    for gap in [*generation.report["gaps"], *unstable_bindings]:
        gap_codes[gap["code"]] = gap_codes.get(gap["code"], 0) + gap["count"]
    peak = _maxrss_mb()
    return {"probe": PROBE, **parts,
            "session_projection": {"committed": True, "mode": generation.mode,
                                   "units": generation.units,
                                   "units_rebuilt": generation.units_rebuilt},
            **judged,
            "mirror_relation_count": held["relation_count"],
            "completeness": held["completeness"],
            "capture_unstable_bindings": [gap["where"] for gap in unstable_bindings],
            "rows_current_total": held["rows_current_total"],
            **{name: counts.get(name, 0) for name in ("objects", "versions", "values",
                                                      "links", "identifiers", "events")},
            "etl_duplicates_collapsed": counts.get(ETL_COLLAPSED, 0),
            "gap_codes": gap_codes, "build_seconds": build_seconds,
            "build_seconds_split": split, "maxrss_mb": peak,
            "max_rss_budget_mb": max_rss_mb,
            "maxrss_within_budget": (None if max_rss_mb is None or peak is None
                                     else peak <= max_rss_mb)}


def build_split(began: float, seconds: Mapping[str, Any],
                ended: Sequence[tuple[str, float]]) -> dict[str, float]:
    """Where a session build's seconds went (``build_seconds_split``, ``SPLIT_KEYS``):
    ``fingerprint`` (``build_generation`` reading every capture for its unit keys), each
    projector stage (``STAGE_KEYS``: from the end of the one before, ``load`` from the
    fingerprint's end; ``ended`` holds each stage's end on ``time.monotonic``, as
    ``began`` does) and ``finish`` (the ``post_project`` steps, the content digest and the
    commit). A stage that did not end is absent."""
    out = {"fingerprint": round(float(seconds.get("fingerprint", 0.0)), 3)}
    last = began + out["fingerprint"]
    for name, at in ended:
        if name in STAGE_KEYS:
            out[name] = round(max(0.0, at - last), 3)
            last = at
    if "finish" in seconds:
        out["finish"] = round(float(seconds["finish"]), 3)
    return {name: out[name] for name in SPLIT_KEYS if name in out}


def _produce_mail(budget: _Budget, store: str, mail: Mapping[str, Any]
                  ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The session producer's one mail capture into ``store``'s served state, where every
    mail consumer of the session keeps its mail capture (module text, "Session mail"):
    (the manifest's ``mail`` block, None when it failed; the ``session_mail`` counts). A
    failure is a code, never a blocked source of the session projection (which holds no
    mail); the consumers then capture mail themselves."""
    from integrations.capture_mail import capture_into

    state = served_state_path(store)
    os.makedirs(state, exist_ok=True)
    budget.at("mail_capture", "mail_capture", state)
    try:
        captured = capture_into(state, mail["scope"], mail["reader"], mail["capture_actor"],
                                page_budget=mail.get("page_budget"))
    except Exception as exc:  # noqa: BLE001 - a source read: a code, never a message
        part = _failure_part("mail_capture", GRAPH, exc)
        return None, {"captured": False, **{name: part[name] for name in
                                            ("error_code", "http_status") if name in part}}
    budget.check("mail_capture")
    counts = {"captured": True, "complete": captured["complete"],
              "reasons": captured["reasons"],
              "messages_seen": captured["counts"]["messages_seen"]}
    mark = mail_mark(state)
    identity = mail.get("identity")
    if mark is None or not isinstance(identity, str) or not identity:
        return None, counts
    return {"state": os.path.abspath(state), "identity": identity, "mark": mark,
            "capture": captured}, counts


def _portal_down(coverage: Mapping[str, Any]) -> dict[str, Any] | None:
    """A Portal part blocked when no bound relation completed and the source itself failed
    the sync of some (a whole outage behind a served catalog), else None."""
    from integrations.source_mirror import upstream_failures

    failed = upstream_failures(coverage.get("relation_outcomes", ()))
    if coverage.get("complete_bound_relation_count") or not failed:
        return None
    first = failed[sorted(failed)[0]]
    return _part("portal_mirror", PORTAL, first.get("http_status"), first.get("error_code"))


def capture_sharepoint(sharepoint: Mapping[str, Any], budget_seconds: float | None, *,
                       monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One run of the SharePoint capture ``ontology.document_links`` runs
    (``capture_sharepoint.run_cli``) of ``sharepoint["scope"]`` into ``sharepoint["state"]``
    as its principal under its live audience, read back as ``DENIED_PRINCIPAL`` (granted
    nothing) and as ``SHAREPOINT_FOLDER_PRINCIPAL`` (granted the captured folders only,
    not the files shared in them): its evidence label, completeness, counts and enums only, and the ``seconds``
    it took. ``read_back_leaks`` sums what the read-back served the readers with no grant
    of the files (must be 0)."""
    from integrations import capture_sharepoint as cs

    started = monotonic()
    report = cs.run_cli(argparse.Namespace(
        state_dir=sharepoint["state"], scope=sharepoint["scope"],
        live_audience=sharepoint["live_audience"], principal=sharepoint["principal"],
        withheld_principal=DENIED_PRINCIPAL, folder_only_principal=SHAREPOINT_FOLDER_PRINCIPAL,
        runs=1, max_store_bytes=None, budget_seconds=budget_seconds),
        environ=sharepoint["environ"], monotonic=monotonic)
    run, summary = report["runs"][-1], report["summary"]
    return {"evidence_label": report["evidence_label"], "complete": run["complete"],
            "incomplete_reasons": run["incomplete_reasons"],
            **{key: summary[key] for key in ("roots", "roots_complete", "files_current",
                                             "texts_extracted", "texts_ocr")},
            "read_back_leaks": sum(report["leak_check"].values()),
            "requests_sent": report["requests_sent"],
            "seconds": round(monotonic() - started, 3)}


class ServedCaptureConflict(LookupError):
    """The served state's ``sharepoint-capture`` is an entry the probe did not lay out (no
    link of its own): never served as this run's capture, never removed."""

    code = "served_capture_not_a_link"


def serve_sharepoint(store: str, state: str | None) -> None:
    """The served state's SharePoint capture is ``state``'s (its ``sharepoint-capture``
    names that directory), else there is none: a run over the same served state without
    one never serves an earlier run's. Any other entry there is refused
    (``ServedCaptureConflict``)."""
    from integrations.capture_sharepoint import STORE_DIR

    served = os.path.join(served_state_path(store), STORE_DIR)
    if os.path.islink(served):
        os.unlink(served)
    elif os.path.lexists(served):
        raise ServedCaptureConflict(ServedCaptureConflict.code)
    if state is not None:
        os.symlink(os.path.join(os.path.abspath(state), STORE_DIR), served,
                   target_is_directory=True)


def _run_probe(budget: _Budget, partial: dict[str, Any], client: Any, db: str, store: str,
               claim_sample: int, max_rss_mb: float | None, schema: Any,
               normalizer: Callable[[str, Any], Any] | None, unstable: Sequence[str],
               monotonic: Callable[[], float], mail: Mapping[str, Any] | None,
               mirror_options: Mapping[str, Any],
               bc: Mapping[str, Any] | None, served: Mapping[str, Any],
               build_only: bool = False, *, session: str | None = None,
               opened: Mapping[str, Any] | None = None,
               fresh_since: str | None = None, disk: _Disk | None = None,
               sharepoint: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The probe's phases in order; ``partial`` collects counts as each phase ends.
    ``opened``: the session projection (``open_session``) it projects from instead of
    syncing and capturing; when it did not open, a BC capture starts from the session
    producer's (``seed_session_bc``)."""
    from contextlib import ExitStack

    from integrations.source_mirror import MirrorError
    from ontology.project import (MailCaptures, MirrorCaptures, SharePointCaptures,
                                  build_store)
    from ontology.project_bc import BcCaptures
    from ontology.project_incremental import build_generation
    from ontology.types import Actor
    from operating import build_steps
    from scripts.mirror_measure import bound_relations, ordered_full_coverage, unstable_gaps

    disk = disk if disk is not None else _Disk(None, monotonic)
    blocked: list[dict[str, Any]] = []
    partial["blocked_upstream"] = blocked
    coverage: dict[str, Any] | None = None
    # The projection does not need the key-only counts of unstable relations: they are
    # simply not staged here (mirror.full_coverage measures them).
    remaining = budget.remaining()
    share = SYNC_SHARE * (PORTAL_SHARE_WITH_BC if bc is not None else 1.0)
    manifest = opened["manifest"] if opened is not None else None
    if manifest is not None:   # the session's captures: no sync, no capture of its own
        blocked += manifest["blocked_upstream"]
        coverage = manifest["mirror"]
        if coverage is not None:
            db = opened["mirror"]
            partial.update(mirror_relation_count=coverage["relation_count"],
                           rows_current_total=coverage["rows_current_total"],
                           completeness=coverage["completeness"])
    elif client is None and bc is not None:
        blocked.append(_part("portal_mirror", PORTAL, error_code="not_configured"))
    else:
        budget.at("sync", "mirror", _directory(db))
        try:
            coverage = ordered_full_coverage(client, db, monotonic=monotonic, resolve_sample=0,
                                             budget_seconds=None if remaining is None
                                             else remaining * share,
                                             unstable=unstable, key_only_unstable=False,
                                             **mirror_options)
        except MirrorError as exc:
            if bc is None or exc.code != "source_unavailable":
                raise
            blocked.append(_part("portal_mirror", PORTAL, exc.status,
                                 exc.upstream_code or exc.code))
        else:
            partial.update(mirror_relation_count=coverage["relation_count"],
                           rows_current_total=coverage["rows_current_total"],
                           completeness=coverage["completeness"])
            down = _portal_down(coverage) if bc is not None else None
            if down is not None:
                blocked.append(down)
    portal_held = coverage is not None and (
        "portal_api" in manifest["sources"] if manifest is not None else not blocked)
    budget.check("sync")
    if normalizer is None:
        # The build's own default (ontology/identifiers.py): the claim check must re-run
        # the normaliser the build used, or every identifier value reads
        # parse_not_reproduced.
        from ontology.project import _default_normalizer
        normalizer = _default_normalizer()
    if schema is None:
        from ontology.schema import load_schema   # lane L4
        schema = load_schema()
    session_bc = manifest is not None and "bc" in manifest["sources"]
    budget.at("served", "served_state", served_state_path(store))
    prepare_served(store, served["principal"],
                   relations=coverage["relations"] if portal_held else (),
                   bc=bc is not None or session_bc,
                   mirror_audience=served["mirror_audience"],
                   live_audience=served["live_audience"],
                   also=(MAIL_WITHHELD_PRINCIPAL,) if mail is not None else ())
    bc_db, bc_result = None, None
    if manifest is not None:
        bc_result = manifest["bc_capture"]
        if session_bc:   # served where the host reads its BC capture
            _link(opened["bc"], bc_capture_path(store))
            bc_db = bc_capture_path(store)
        if bc is not None or bc_result is not None:
            partial["bc_capture"] = bc_result
    elif bc is not None:
        from integrations.live_source_guard import LiveSourceAudience
        from operating.host import HostConfig
        config = HostConfig.from_file(os.path.join(served_state_path(store), SERVED_CONFIG))
        left = budget.remaining()
        budget.at("bc_capture", "bc_capture", _directory(bc_capture_path(store)))
        seeded = seed_session_bc(session, fresh_since, bc_capture_path(store))
        try:
            bc_result = capture_bc(bc, bc_capture_path(store),
                                   None if left is None else left * SYNC_SHARE,
                                   audience=LiveSourceAudience.from_file(config.live_audience),
                                   actor=Actor(config.tenant, frozenset({config.principal}),
                                               "restricted"), monotonic=monotonic)
        except Exception as exc:  # noqa: BLE001 - a source read: a part, never a message
            blocked.append(_failure_part("bc_capture", BC, exc))
        else:
            bc_result["session_seeded"] = seeded
            if bc_result["records_current"]:
                bc_db = bc_capture_path(store)
            else:
                blocked.append(_part("bc_capture", BC,
                                     error_code=bc_result["reason"] or "no_records_captured"))
        partial["bc_capture"] = bc_result
        budget.check("bc_capture")
    mail_state, captured, mail_origin = None, None, None
    if mail is not None:
        from integrations.capture_mail import capture_into
        state = served_state_path(store)   # where the host keeps its mail capture
        # The session producer's capture of this very scope, as it left it: not read again.
        captured = session_mail_capture(manifest, mail, state)
        if captured is not None:
            mail_state, mail_origin = state, "session"
        else:
            budget.at("mail_capture", "mail_capture", state)
            try:
                captured = capture_into(state, mail["scope"], mail["reader"],
                                        mail["capture_actor"],
                                        page_budget=mail.get("page_budget"))
            except Exception as exc:  # noqa: BLE001 - a part when BC makes every source one
                if bc is None:
                    raise
                blocked.append(_failure_part("mail_capture", GRAPH, exc))
            else:
                mail_state, mail_origin = state, "own"
            budget.check("mail_capture")
        if captured is not None:
            # The mailboxes Graph refused outright (capture_mail.refused_parts: the host's
            # grant is missing) are parts, as operating.case_mail reports them: a run whose
            # mail was never read is blocked by that refusal, never measured as no mail.
            room = max(0, MAX_BLOCKED_PARTS - len(blocked))
            blocked += list(captured.get("blocked_upstream") or [])[:room]
    sharepoint_state, sharepoint_result, partial_upstream = None, None, []
    if sharepoint is not None and not sharepoint["environ"].get("SHAREPOINT_SITE_ID"):
        # no host to download from (live_source_guard.sharepoint_host_from_env): not read
        blocked.append(_part("sharepoint_capture", GRAPH, error_code="not_configured"))
    elif sharepoint is not None:
        left = budget.remaining()
        allowed = [b for b in (sharepoint["budget_seconds"],
                               None if left is None else left * SYNC_SHARE) if b is not None]
        budget.at("sharepoint_capture", "sharepoint_capture", sharepoint["state"])
        try:
            sharepoint_result = capture_sharepoint(sharepoint, min(allowed, default=None),
                                                   monotonic=monotonic)
        except Exception as exc:  # noqa: BLE001 - a part when BC makes every source one
            if bc is None:
                raise
            blocked.append(_failure_part("sharepoint_capture", GRAPH, exc))
        else:
            if sharepoint_result["complete"] or sharepoint_result["files_current"]:
                sharepoint_state = sharepoint["state"]
                if not sharepoint_result["complete"]:   # served as far as it got, marked
                    partial_upstream.append(_part("sharepoint_capture", GRAPH, error_code=(
                        sharepoint_result["incomplete_reasons"] or ["incomplete"])[0]))
            else:   # nothing to project, and why
                blocked.append(_part("sharepoint_capture", GRAPH, error_code=(
                    sharepoint_result["incomplete_reasons"] or ["no_files_captured"])[0]))
        budget.check("sharepoint_capture")
    serve_sharepoint(store, sharepoint_state)
    sources_blocked = ("no_record_source" if not portal_held and bc_db is None else
                       "no_document_source" if mail is not None and mail_state is None
                       else None)
    if sources_blocked is not None:
        return {"probe": PROBE, "sources_blocked": sources_blocked, "sources": [],
                "blocked_upstream": blocked, "mirror_complete": False,
                **({"bc_capture": bc_result} if bc is not None else {}),
                "maxrss_mb": _maxrss_mb()}

    reuse: dict[str, Any] = {}
    sharepoint_db = None
    if sharepoint_state is not None:   # the capture store a build reads (its disk need)
        from integrations.capture_sharepoint import DB_FILE, STORE_DIR as SHAREPOINT_DIR
        sharepoint_db = os.path.join(sharepoint_state, SHAREPOINT_DIR, DB_FILE)
    # The store the served projection is: the session generation the store names, else
    # the store itself; every check after the builds reads it.
    projected = "session_projection" if manifest is not None else "ontology_store"

    def build(target: str, phase: str) -> dict[str, Any]:
        budget.at(phase, projected if target == store else "ontology_store",
                  _directory(target))
        session_build = manifest is not None and target == store
        base = opened["generation"] if session_build else None
        before_seed = None
        if session_build and mail_state is not None:
            # A mail consumer's generation: its own from earlier in this session over the
            # same base and mail capture (reused, nothing written), else built anew in its
            # place, never beside it: the session generation copied and its mail units
            # built, the need checked against that, never a cold build's peak. A build on
            # its own generation copies that store beside it only when a unit changed:
            # the same need is checked then, before the copy.
            from integrations.capture_mail import STORE_DIR

            def need(seed: Any) -> None:
                disk.require_bytes(_directory(target), file_bytes(str(seed))
                                   + BUILD_BYTES_PER_SOURCE_BYTE * (file_bytes(
                                       os.path.join(mail_state, STORE_DIR))
                                       + file_bytes(sharepoint_db)))
            local = _reusable_local(store, base, mail_state, fresh_since,
                                    sharepoint_state is not None)
            if local is not None:
                base, before_seed = local, need
            else:
                free_store(store)
                need(base.store)
        elif not session_build:
            # The session generation over exactly its own captures is reused: nothing written.
            disk.require(_directory(target), [db if portal_held else None, bc_db,
                                              mail_state, sharepoint_db])
        with ExitStack() as stack:
            tick = stack.enter_context(disk.measured(_directory(target),
                                                     budget.checkpoint(phase)))
            sources: dict[str, Any] = {}
            if portal_held:
                sources["portal_api"] = stack.enter_context(MirrorCaptures(db))
            if bc_db is not None:
                sources["bc"] = stack.enter_context(BcCaptures(bc_db))
            if mail_state is not None:
                sources["graph_mail"] = stack.enter_context(MailCaptures(
                    mail_state, internal_domains=mail["scope"].internal_domains))
            if sharepoint_state is not None:
                sources["sharepoint"] = stack.enter_context(SharePointCaptures(
                    sharepoint_state))
            # the host's one step list (operating.build_steps) over the served state, so
            # the probe builds what the host builds
            steps = build_steps.post_project(served_state_path(store), TENANT)
            if manifest is None or target != store:
                report = build_store(target, schema, sources, normalizer=normalizer,
                                     post_project=steps, checkpoint=tick,
                                     on_stage=disk.stage)
                disk.keep("store_bytes", file_bytes(target))
                disk.built(report)
                return report
            # The session generation as it is when nothing differs, else only what does.
            generation = build_generation(store + ".generations", schema, sources,
                                          previous=base, reuse=True,
                                          normalizer=normalizer, post_project=steps,
                                          checkpoint=tick, before_seed=before_seed,
                                          on_stage=disk.stage)
        _link(str(generation.store), store)
        if generation.mode != "reused":   # written here, not the session's own
            disk.keep("store_bytes", file_bytes(str(generation.store)))
            disk.built(generation.report)
            if mail_state is not None:   # one full store of its own, never two
                _keep_generation(store + ".generations", generation.id)
        if mail_state is not None:
            _write_atomic(Path(store + ".generations", SESSION_BASE), {
                "generation": generation.id, "content_digest": generation.content_digest,
                "base": opened["generation"].id,
                "base_digest": opened["generation"].content_digest,
                "mail_mark": mail_mark(mail_state),
                **({"sharepoint": True} if sharepoint_state is not None else {})})
        reuse.update(mode=generation.mode, units_rebuilt=generation.units_rebuilt,
                     **({"mail_capture": mail_origin} if mail_origin is not None else {}))
        return generation.report

    started = monotonic()
    first = build(store, "projection")
    build_seconds = monotonic() - started
    partial.update(objects=first["counts"].get("objects", 0),
                   values=first["counts"].get("values", 0),
                   build_seconds=round(build_seconds, 3))
    mail_result: dict[str, Any] = {}
    if mail_state is not None:   # the build's mention tables, kept by any later stop
        step = first["post_project"]["mentions"]["counts"]
        mail_result = {"mail_capture_complete": captured["complete"],
                       "mail_capture_reasons": captured["reasons"],
                       "mail_folders": captured["folders"],
                       "mail_messages_seen": captured["counts"]["messages_seen"],
                       "mention_documents": step["documents"],
                       "mention_links": step["links"],
                       "mentions_ambiguous": step["ambiguous_mentions"],
                       "mentions_not_found": step["not_found"],
                       "mention_undeclared_target": step["undeclared_target"]}
        partial.update(mail_result)
    budget.check("projection")
    second, checks, host, refused = None, {}, None, {}
    budget.at("publish", projected, _directory(store))
    publish_served(store, db)
    if not build_only:
        budget.at("claims", projected, _directory(store))
        budget.start_checks()
        host = served_host(store, served.get("environ"))
        actor = reader_actor(host)
        with host.served_tools(actor) as tools:
            checks = check_store(store, served_rows(tools, actor), actor, schema,
                                 normalizer=normalizer, policy=tools.policy_now(actor),
                                 claim_sample=claim_sample,
                                 checkpoint=budget.checkpoint("claims"),
                                 reads=served_reads(tools, actor))
        budget.check("claims")
        budget.at("denied", projected, _directory(store))
        checks.update(denied_checks(host,
                                    citation_leaks=checks["withheld_citations_resolved"]))
        budget.at("removals", projected, _directory(store))
        checks.update(removal_checks(host, store, checkpoint=budget.checkpoint("removals")))
        # Soft-deleted mirror rows of a relation the projection binds (and stages) reach
        # it as source_soft_delete stretches; not known (an older manifest): None, fails.
        soft = coverage.get("soft_deleted_rows") if portal_held else {}
        bound = set(bound_relations()) - set(unstable)
        held = (None if soft is None else
                sum(count for relation, count in soft.items() if relation in bound))
        checks.update(mirror_soft_deleted_bound=held, removals_reach_projection=(
            None if held is None else
            held == 0 or checks["removal_stretches"].get("source_soft_delete", 0) > 0))
        if mail_state is not None:
            budget.at("mentions", projected, _directory(store))
            mail_result.update(mail_mention_checks(host, store))
            mail_result.update(mail_text_find_checks(host, store))
    # The mail run (ontology.mentions) on the cold path builds once: determinism at full
    # size is ontology.projection's and ontology.session_projection's, so its rebuild is
    # not asserted (decision 2026-10-14-mentions-cold-path, option 3).
    single = not build_only and mail is not None and manifest is None
    # A second build of this size that cannot fit is not started: the checks ran on the
    # first and the report is a budget stop at rebuild_skipped (no determinism verdict).
    # On the session path the first build only reused the session generation (about 0 s):
    # the rebuild is a cold build, forecast from what the producer's build took.
    recorded = manifest.get("build_seconds") if manifest is not None else None
    forecast = max(build_seconds, recorded if isinstance(recorded, (int, float))
                   and not isinstance(recorded, bool) else 0.0)
    left = None if build_only or single else budget.remaining()
    skipped = left is not None and left < forecast
    if skipped:
        partial["rebuild_skipped"] = True
    if not build_only and not single and not skipped:
        free_store(store)   # its digest is kept: never two full stores at once
        again = store + ".again"
        try:
            second = build(again, "rebuild")
        except DiskInsufficient as exc:
            refused = disk_insufficient(exc)
        else:   # the rebuild takes the first's place in the served state
            os.replace(again, store)
            free_store(again)
            publish_served(store, db)
            budget.check("rebuild")
    peak = _maxrss_mb()
    counts = first["counts"]
    gap_codes: dict[str, int] = {}
    unstable_bindings = (unstable_gaps(coverage["unstable_relations"], schema=schema)
                         if portal_held else [])
    for gap in [*first["gaps"], *unstable_bindings]:
        gap_codes[gap["code"]] = gap_codes.get(gap["code"], 0) + gap["count"]
    sources = [name for name, held in (("bc", bc_db is not None),
                                       ("graph_mail", mail_state is not None),
                                       ("portal_api", portal_held),
                                       ("sharepoint", sharepoint_state is not None)) if held]
    # owner rule 1b5e555dce17: the completeness block's figure, as mirror.full_coverage
    # gates it, never the 93-relation catalog count; over the bound relations available.
    # A --build-only build (the probes that build through this one) keeps the strict
    # figure and adds no part: an unavailable relation is this check's finding.
    judged = (_mirror_complete(coverage["completeness"], upstream_unavailable(db), blocked)
              if portal_held and not build_only else
              {"mirror_complete": portal_held and coverage["completeness"]["all_bound_complete"]})
    parts: dict[str, Any] = {}
    if bc is not None or manifest is not None:
        parts = {"blocked_upstream": blocked, "bc_capture": bc_result, "sources": sources}
    elif blocked:
        parts = {"blocked_upstream": blocked}
    if sharepoint is not None:
        parts["sharepoint_capture"] = sharepoint_result
    if partial_upstream:   # served, not blocked: a part a pass may carry
        parts["partial_upstream"] = partial_upstream
    if session is not None:
        parts["session_projection"] = reuse or {
            "mode": "cold", **({"mail_capture": mail_origin} if mail_origin is not None else {})}
    return {"probe": PROBE, **mail_result, **parts, **judged,
            "mirror_relation_count": coverage["relation_count"] if portal_held else 0,
            "completeness": coverage["completeness"] if portal_held else None,
            "capture_unstable_bindings": [gap["where"] for gap in unstable_bindings],
            "rows_current_total": coverage["rows_current_total"] if portal_held else 0,
            "objects": counts.get("objects", 0), "versions": counts.get("versions", 0),
            "values": counts.get("values", 0), "links": counts.get("links", 0),
            "identifiers": counts.get("identifiers", 0), "events": counts.get("events", 0),
            "etl_duplicates_collapsed": counts.get(ETL_COLLAPSED, 0),
            "link_tiers": {key.split(".", 1)[1]: value for key, value in counts.items()
                           if key.startswith("links.")},
            "gap_codes": gap_codes,
            **({"rebuild_digest_not_asserted": COLD_SINGLE_BUILD,
                "rebuild_digest_mismatches": 0} if single else {} if second is None else
               {"rebuild_digest_equal": first["content_digest"] == second["content_digest"],
                "rebuild_digest_mismatches":
                    int(first["content_digest"] != second["content_digest"])}),
            **({"rebuild_skipped": True} if skipped else {}),
            "build_seconds": round(build_seconds, 3), "maxrss_mb": peak,
            "max_rss_budget_mb": max_rss_mb,
            "maxrss_within_budget": (None if max_rss_mb is None or peak is None
                                     else peak <= max_rss_mb), **checks,
            **({"phase": "rebuild", **refused} if refused else {})}


def _keep_generation(state: str, current: str) -> None:
    """Remove every generation directory of ``state`` but ``current`` (a consumer's own
    generations: the one it serves is the only one it keeps)."""
    root = Path(state, "generations")
    for directory in root.iterdir() if root.is_dir() else ():
        if directory.is_dir() and directory.name != current:
            shutil.rmtree(directory, ignore_errors=True)


def free_store(store: str) -> None:
    """Remove a built store: its file (with ``-wal``/``-shm``), its served link and the
    generations an incremental build kept beside it. A store linked to the session
    generation frees only its links (the session's own file stays)."""
    from operating import host

    for path in (store, str(host.projection_path(Path(served_state_path(store))))):
        for name in (path, f"{path}-wal", f"{path}-shm", f"{path}-journal"):
            if os.path.lexists(name):
                os.unlink(name)
    shutil.rmtree(store + ".generations", ignore_errors=True)


def _current_rss_mb() -> float | None:
    """Resident memory of this process now in MB (``VmRSS``); None where /proc has none."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _hard_exit(code: int) -> None:
    sys.stdout.flush()
    os._exit(code)


class _RssWatchdog:
    """Enforces ``--max-rss-mb``: a thread reading ``_current_rss_mb`` that, over the
    limit, prints the partial report (``rss_exceeded``) and ends the process, whatever the
    probe's thread is doing."""

    def __init__(self, limit_mb: float | None, budget: "_Budget", partial: dict[str, Any],
                 disk: "_Disk") -> None:
        self._limit, self._budget, self._partial, self._disk = limit_mb, budget, partial, disk
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rss-watchdog")

    def __enter__(self) -> "_RssWatchdog":
        if self._limit is not None:
            self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(RSS_SAMPLE_SECONDS):
            rss = _current_rss_mb()
            if rss is not None and rss > self._limit:
                self._fire(rss)
                return

    def _fire(self, rss: float) -> None:
        where = dict(self._budget.where)
        try:
            partial = dict(self._partial)
            extra = {**self._disk.report(), "phase_seconds": self._budget.timings()}
        except Exception:  # noqa: BLE001 - the report must still name the phase
            partial, extra = {}, {}
        print(json.dumps({**partial, **extra, "probe": PROBE, "status": "rss_exceeded",
                          "phase": where.get("phase"), "store": where.get("store"),
                          "rss_mb": rss, "max_rss_budget_mb": self._limit,
                          "maxrss_mb": _maxrss_mb(),
                          "elapsed_seconds": round(self._budget.elapsed(), 3)},
                         sort_keys=True, default=str), flush=True)
        _hard_exit(EXIT_RSS)


def _maxrss_mb() -> float | None:
    """Peak resident memory of this process in MB (projection-memory decision)."""
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         bc_sender: Any = None, bc_books: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.ontology_projection_probe")
    parser.add_argument("--db", required=True, help="throwaway mirror file")
    parser.add_argument("--store", default=None,
                        help="throwaway ontology store file (with --produce-session: the "
                             "store whose served state keeps its mail capture)")
    parser.add_argument("--budget-seconds", type=float, default=None)
    parser.add_argument("--workers", type=int, default=1,
                        help="relations the mirror sync reads at a time (see "
                             "mirror.full_coverage)")
    parser.add_argument("--checks-budget-seconds", type=float, default=None,
                        help="seconds the claim, trim, removal and mention checks and the "
                             "rebuild may take once the first build is done (within "
                             "--budget-seconds); the verifier schedules a shared run by it")
    parser.add_argument("--claim-sample", type=int, default=2000)
    parser.add_argument("--max-rss-mb", type=float, default=None,
                        help="peak resident memory budget for the whole probe")
    parser.add_argument("--previous-report", default=None,
                        help="last daily verifier report: its unstable relations are not "
                             "staged and leave the completeness denominator")
    parser.add_argument("--mail-capture-scope", default=None,
                        help="plx-mail-capture-scope/v1 file: capture and project that mail "
                             "with stored mention links (needs --live-audience)")
    parser.add_argument("--live-audience", default=None,
                        help="plx-live-source-audience/v1 file granting --capture-principal: "
                             "the mail capture's and the served host's live audience")
    parser.add_argument("--mirror-audience", default=None,
                        help="plx-source-mirror-audience/v1 file of the served host "
                             "(default: --capture-principal granted every mirrored family)")
    parser.add_argument("--capture-principal", default="svc:mail-capture",
                        help="captures mail and reads the served host")
    parser.add_argument("--capture-page-budget", type=int, default=None)
    parser.add_argument("--sharepoint-capture-scope", default=None,
                        help="plx-sharepoint-capture-scope/v1 file: capture that SharePoint "
                             "scope into --sharepoint-state, project it and serve it (needs "
                             "--live-audience)")
    parser.add_argument("--sharepoint-state", default=None,
                        help="the directory the SharePoint capture is kept in (never the "
                             "served state: the served state names it)")
    parser.add_argument("--sharepoint-budget-seconds", type=float, default=None)
    parser.add_argument("--bc-capture", action="store_true",
                        help="capture and project BC too (BC credentials from the "
                             "environment); every source is then one part and the Portal "
                             "may be down or not configured")
    parser.add_argument("--build-only", action="store_true",
                        help="one build laid out as the served state, no rebuild and no "
                             "checks (the build ontology.traverse, workspace.object_view "
                             "and process.dependencies run)")
    parser.add_argument("--session-projection", default=None,
                        help="the verifier session's projection directory: opened when "
                             "this session committed it, else the probe projects cold")
    parser.add_argument("--produce-session", action="store_true",
                        help="produce --session-projection from the session mirror --db "
                             "(never synced) and, with --bc-capture, a BC capture; with "
                             "--mail-capture-scope, capture that mail once for the mail "
                             "consumers")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        if args.produce_session and args.session_projection is None:
            raise LookupError("--produce-session needs --session-projection")
        if not args.produce_session and args.store is None:
            raise LookupError("--store is required")
        base_url, key = env.get("PLX_WORKBENCH_API_URL"), env.get("PLX_WORKBENCH_API_KEY")
        if (not base_url or not key) and not args.bc_capture and not args.produce_session:
            raise LookupError("missing environment variables: PLX_WORKBENCH_API_URL, "
                              "PLX_WORKBENCH_API_KEY")
        from integrations import bc_read
        from integrations.workbench_read_api import WorkbenchReadApiClient
        client = WorkbenchReadApiClient(base_url, key) if base_url and key \
            and not args.produce_session else None
        bc = ({"environ": env, "sender": bc_sender, "books": bc_books}
              if args.bc_capture else None)
        mail, mail_blocked = None, None
        if args.mail_capture_scope is not None:
            from integrations.capture_mail import CaptureScope
            from integrations.graph_read import GraphMailReader, build_graph
            from integrations.live_source_guard import LiveSourceAudience
            from ontology.types import Actor
            if args.live_audience is None:
                raise LookupError("--mail-capture-scope needs --live-audience")
            if args.produce_session and args.store is None:
                raise LookupError("--produce-session --mail-capture-scope needs --store")
            audience = LiveSourceAudience.from_file(args.live_audience)
            scope = CaptureScope.from_file(args.mail_capture_scope)
            try:
                api, _transport = build_graph(env)
            except Exception as exc:  # noqa: BLE001 - the producer's mail is optional
                if not args.produce_session:
                    raise
                mail_blocked = _failure_part("mail_capture", GRAPH, exc)
            else:
                mail = {"scope": scope, "reader": GraphMailReader(api, audience),
                        "capture_actor": Actor(audience.tenant,
                                               frozenset({args.capture_principal}),
                                               "restricted"),
                        "page_budget": args.capture_page_budget,
                        "identity": mail_identity(args.mail_capture_scope,
                                                  args.live_audience,
                                                  args.capture_principal,
                                                  args.capture_page_budget)}
        sharepoint = None
        if args.sharepoint_capture_scope is not None:
            if args.live_audience is None or args.sharepoint_state is None:
                raise LookupError("--sharepoint-capture-scope needs --live-audience and "
                                  "--sharepoint-state")
            if args.produce_session:
                raise LookupError("--sharepoint-capture-scope is a consumer's, never "
                                  "--produce-session's")
            if os.path.abspath(args.sharepoint_state) == served_state_path(args.store):
                raise LookupError("--sharepoint-state must not be the served state")
            sharepoint = {"scope": args.sharepoint_capture_scope,
                          "state": args.sharepoint_state, "live_audience": args.live_audience,
                          "principal": args.capture_principal, "environ": env,
                          "budget_seconds": args.sharepoint_budget_seconds}
        from scripts.mirror_measure import previous_unstable
        result = projection_probe(client, args.db, args.store,
                                  budget_seconds=args.budget_seconds,
                                  checks_budget_seconds=args.checks_budget_seconds,
                                  claim_sample=args.claim_sample,
                                  max_rss_mb=args.max_rss_mb, workers=args.workers,
                                  unstable=previous_unstable(args.previous_report),
                                  mail=mail, bc=bc, principal=args.capture_principal,
                                  mirror_audience=args.mirror_audience,
                                  live_audience=args.live_audience,
                                  build_only=args.build_only,
                                  session=args.session_projection,
                                  fresh_since=bc_read.session_fresh_since(env),
                                  produce=args.produce_session,
                                  disk_need=previous_peak(args.previous_report),
                                  disk_floor=forecast_floor(args.previous_report),
                                  mail_blocked=mail_blocked, sharepoint=sharepoint,
                                  environ=env)
    except Exception as exc:  # noqa: BLE001 - a typed error line, never a value
        print(json.dumps(error_line(exc)))
        return 1
    print(json.dumps(result, sort_keys=True))
    return EXIT_BUDGET if result.get("status") == "budget_exhausted" else \
        1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
