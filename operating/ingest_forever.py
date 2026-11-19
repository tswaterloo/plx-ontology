"""The host's ingest mode: every connected source's own capture CLI, as one host cycle.

Always be ingesting is a mode of ``operating.host``, never a
runner of its own: a host config with
``"ingest": {"mode": "capture_clis"}`` makes each ``Host.cycle`` (``run``, ``serve``'s
interval, ``check``) re-run the existing capture command of every source the config
names, as a child process, into the host's state directory. One scheduler
(``Host.serve``), one config schema (``plx-host-config/v1``), one cycle lock
(``<DIR>/host/cycle.lock``), one ledger and one projection; this module is the library
that cycle reads its sources with. It reads no source itself, edits no capture module and
keeps no cursor of its own: each capture keeps its resume cursor in its own store there
(the mail deltaLink, the Teams delta/high-water marks and the owner's content
confirmation gate, the SharePoint walk position and drive delta, the SharePoint list
cursor, the BC modified-since watermark, the Portal mirror's ``since`` plan), the same
stores and the same capture classes the host's own readers use.

The config's ingest-mode source sections are this module's table (:data:`SOURCES`:
``portal``, ``bc``, ``mail``, ``sharepoint``, ``sharepoint_lists``, ``teams``, ``meetings``,
``mc``), each key checked against its CLI's fields (:func:`section_options`; a path must name a
file); ``live_audience`` defaults to the host's ``capture_audience``, the Portal's
``audience`` to its ``mirror_audience``, ``tenant`` and ``principal`` to the host's.

Each cycle (:func:`read_sources`) first checks the people-exclusion list
(``integrations.people_exclusion.PeopleExclusion.from_env``: ``PLX_EXCLUDED_PEOPLE_OIDS``
and/or ``PLX_EXCLUDED_PEOPLE_FILE``); when it does not load every configured source is
``blocked`` with its typed part (later_ask ``excluded-people-oids``) and no capture runs.
Each capture re-checks the list itself before it reads anyone.

Isolation: each source is a child process in its own session, bounded by its budget plus
``grace_seconds`` and killed with its process group past that
(``integrations.child_process.stop``), and recorded in the host's
``host/cycle-children.json`` while it runs, so the next holder of the cycle lock stops
it if this process is killed first (``operating.host.CycleChildren``). A source that
fails, times out, prints no report or lacks its credentials is that source's typed
result; the cycle goes on with the next. A child sees the parent's environment minus
every credential-shaped variable other than its own source's (a BC capture never holds
the Graph secret). Egress is the capture CLIs' own GET-only clients; this module opens no
connection.

Per source the cycle report keeps the capture's result under ``capture``: ``status``
(``complete``, ``partial``, ``blocked``, ``failed``, ``not_configured``), requests sent,
units complete/resumed, the capture's numeric summary and typed blocks (``part``,
``source``, ``error_code``, ``cause``, ``http_status``, ``later_ask``). Strings are code
tokens only (no address, object ID, URL, path, name or message; anything else is dropped).

Concurrency: a cycle (:func:`read_sources`) runs its children concurrently, at most
``ingest.max_parallel`` at once (default one per configured source, at most
:data:`MAX_PARALLEL`); each writes only its own store, the host alone writes its ledger
and the children record (``CycleChildren``, under a lock). The continuous mode
(:class:`Continuous`, the host's ``serve`` in ingest mode) gives each source its own
cadence (``min_interval_seconds`` of its section, default
:data:`DEFAULT_MIN_INTERVAL_SECONDS`) and hands each finished source to the host, which
records it, while the other children go on reading; it stops on the stop file, low disk
or a signal. The host never builds a projection generation (the row deltas keep Postgres
current, docs/no-builds.md), so no capture ever waits for one.

Resume kinds: ``cursor`` sources (Portal, BC, mail, SharePoint, Teams) continue from a
cursor after a completed cycle; SharePoint lists keep a cursor only for an interrupted
run (``interrupted_only``) and Mission Control is a snapshot (``snapshot``), so both
re-read in full after a completed cycle, and the host's ``check``
(:func:`cycle_checks`) reports them as ``full_reread_sources`` rather than as resumed.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from integrations import child_process
from integrations.people_exclusion import ENV_FILE, ENV_IDS, PeopleExclusion

from .warm_store import STATE_ENV

ROOT = Path(__file__).resolve().parents[1]
MODE = "capture_clis"   # the host config's ``ingest.mode``
DEFAULT_GRACE_SECONDS, MAX_GRACE_SECONDS = 180, 1800
DEFAULT_TIMEOUT_SECONDS = 600   # a source whose CLI takes no budget
MAX_BUDGET_SECONDS = 7200
SHAREPOINT_VERIFY_SAMPLE = 500   # handles the SharePoint CLI's self-check resolves
MAX_PARALLEL = 8   # capture children at once (``ingest.max_parallel``; default: one each)
# Continuous mode (``Continuous``): a source that finished runs again this long after
# (``min_interval_seconds`` of its section); a failed or blocked one waits twice as long
# per failure in a row, at most ``MAX_RETRY_SECONDS``.
DEFAULT_MIN_INTERVAL_SECONDS, MAX_RETRY_SECONDS = 120, 3600
# Graceful drain (``run_source`` ``drain``): a child asked to stop gets this long to end
# its run at a safe point before its process group is killed (``ingest.drain_seconds``).
DEFAULT_DRAIN_SECONDS, MAX_DRAIN_SECONDS = 60, 600
# Freshness-first scheduling (``Continuous``): the staleness a source is kept under, in
# seconds since its last run that captured (complete or partial) started; a section's
# ``freshness_target_seconds`` sets or overrides it. Mail: 15 minutes (owner, 11-14).
# Portal: 1 hour (owner, 11-13; the ``capture:portal:progress`` alarm's bound). Without
# a target the Portal was a background source: it re-ran only its 2 h cadence after each
# ~25 min run, so it made progress every ~145 min and the alarm stayed open (11-15).
DEFAULT_FRESHNESS_TARGETS = {"mail": 900, "portal": 3600}
STOPPED = "ingest_stopped"   # a child stopped by the stop file, the disk guard or a signal
GAP_LOCK_NOT_HELD = "gap_lock_not_held"   # a stop came in a maintenance gap: lock not taken back
POLL_SECONDS = 0.5
EPHEMERAL_ROOTS = ("/tmp", "/dev/shm", "/run")
STATUSES = ("complete", "partial", "blocked", "failed", "not_configured")
# the live verifier CAUSES: what a typed block may name as its cause.
CAUSES = frozenset({"upstream_outage", "projection_timeout", "missing_bindings",
                    "host_misconfiguration", "capture_gap", "relation_unavailable",
                    "source_gap", "later_ask"})
RESUME_KINDS = ("cursor", "interrupted_only", "snapshot")
# A child never holds another source's secret: names shaped like a credential are passed
# only to the source that declares them (the shape the live verifier hides).
CREDENTIAL_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|COOKIE|DSN|CREDENTIAL|"
                             r"SESSION|^GIT_CONFIG_", re.IGNORECASE)
GRAPH_ENV = ("MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_GRAPH_CLIENT_ID",
             "MICROSOFT_GRAPH_CLIENT_SECRET")
BC_ENV = ("BC_TENANT_ID", "BC_CLIENT_ID", "BC_CLIENT_SECRET", "BC_ENVIRONMENT")
PORTAL_ENV = ("PLX_WORKBENCH_API_URL", "PLX_WORKBENCH_API_KEY")
MC_ENV = ("MC_MCP_API_KEY",)
BRAIN_OPTIONAL_ENV = ("BRAIN_API_KEY", "BRAIN_BASE_URL", "VMC_API_KEY", "VMC_API_URL")
# The meetings capture reads with the admin application (``integrations.capture_meetings``
# ``CREDENTIALS_ENV``, default ``AZURE``): its secret is required, the prefix setting and
# the mail application's names are passed when set (``PLX_MEETINGS_GRAPH_CREDENTIALS=
# MICROSOFT_GRAPH`` reads with the mail application instead).
MEETINGS_ENV = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
MEETINGS_OPTIONAL_ENV = ("PLX_MEETINGS_GRAPH_CREDENTIALS", "PLX_MEETINGS_TRANSCRIPTS_GRANTED",
                         *GRAPH_ENV)
DEFAULT_PRINCIPAL = "svc:workbench-host"   # operating/host.py DEFAULT_PRINCIPAL
MAX_ITEMS = 20
MAX_COUNTS = 40

_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:\-]{0,79}")
_OPAQUE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}|[0-9a-fA-F]{16,}|\d{7,}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,80}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}")




class IngestError(Exception):
    """A refusal with a code (no value of any input in it)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ sanitizing

def code(value: Any) -> str | None:
    """``value`` when it is a code token (no address, ID, URL, path or prose), else None."""
    if isinstance(value, str) and _CODE.fullmatch(value) and not _OPAQUE.search(value):
        return value
    return None


def count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _codes(values: Any) -> list[str]:
    found = values if isinstance(values, (list, tuple, set, frozenset)) else [values]
    return sorted({c for c in (code(v) for v in found) if c})[:MAX_ITEMS]


def typed_block(entry: Any, *, part: str | None = None, source: str | None = None
                ) -> dict[str, Any]:
    """One ``blocked_upstream`` entry, code tokens and an HTTP status only."""
    entry = entry if isinstance(entry, Mapping) else {}
    status = entry.get("http_status")
    block = {"part": code(entry.get("part")) or part,
             "source": code(entry.get("source")) or source,
             "http_status": status if type(status) is int and 100 <= status <= 599 else None,
             "error_code": code(entry.get("error_code")),
             "cause": entry.get("cause") if entry.get("cause") in CAUSES else None}
    if code(entry.get("later_ask")):
        block["later_ask"] = entry["later_ask"]
    names = [n for n in entry.get("missing_settings") or () if isinstance(n, str)
             and _ENV_NAME.fullmatch(n)]
    if names:
        block["missing_settings"] = sorted(set(names))[:MAX_ITEMS]
    return block


def is_typed(block: Mapping[str, Any]) -> bool:
    return bool(block.get("error_code")) and block.get("cause") in CAUSES


# ------------------------------------------------------------------ the sources

@dataclass(frozen=True)
class SourceSpec:
    """One capture CLI: how to call it into the state directory and read its report."""

    name: str
    source: str                         # the blocked-part ``source`` label
    argv: Callable[[Path, Mapping[str, Any]], list[str]]
    summarize: Callable[[Mapping[str, Any]], dict[str, Any]]
    fields: Mapping[str, str]           # config key -> kind (_option: path, str, seconds, ...)
    required: frozenset[str] = frozenset()
    env: tuple[str, ...] = ()           # every name must be set, else blocked
    resume_kind: str = "cursor"
    budget_field: str | None = "budget_seconds"
    # The projection's source family of the store this capture writes, which the check
    # reads for duplicates (None: not projected), and the relations of that family it
    # writes when another source shares the family (None: the rest of the family's).
    projected: str | None = None
    relations: frozenset[str] | None = None
    # The store the host serves this source from, when not the family's state store (the
    # Portal's working mirror): recorded as the ledger's ``served_path``.
    served_store: Callable[[Path], Path] | None = None
    # Every SQLite store file this capture writes (None: none the host knows of; the
    # ops alarms read their WAL sizes, ``operating.ops_health``).
    writes: Callable[[Path], tuple[Path, ...]] | None = None
    # Credential-shaped names passed to the child when set, never required.
    optional_env: tuple[str, ...] = ()
    # At least one of these must be set (a source with alternative credential pairs),
    # else blocked like a missing ``env`` name; each is passed when set.
    env_one_of: tuple[str, ...] = ()
    # The sources this one never runs beside and gives way to (:class:`Continuous`): it
    # does not start while one of them runs or is due, and once one of them is due while
    # it runs it is drained (``run_source`` ``drain``). The mail backlog writes the mail
    # store, which keeps one writer, and never holds fresh mail back.
    yields_to: tuple[str, ...] = ()
    # The units the CLI captures by default that a section's explicit list leaves out
    # (options -> names): each one is a config warning (``<name>.<key>:unlisted:<unit>``),
    # so a set added to the capture but missing from a hand list is on Home, never a set
    # that silently never runs (CT-D-04: projects, plxCustomerShipTos, contactsInformation).
    unlisted: Callable[[Mapping[str, Any]], list[str]] | None = None


def _python() -> str:
    return sys.executable or "python3"


def _private_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _budget(options: Mapping[str, Any]) -> list[str]:
    value = options.get("budget_seconds")
    return [] if value is None else ["--budget-seconds", str(value)]


# A child's worker threads or processes (capture_mail.MAX_WORKERS).
MAX_CHILD_WORKERS = 16
# The mail store's backlog passes (``capture_mail.after_capture``): their own source,
# ``mail_backlog`` (``capture_mail backlog``), never inside the mail capture child
# (2026-11-13: a deferred-attachment pass in the mail child held fresh mail back
# 2 h 22 min, and a 67-minute mail run another hour).
BACKLOG_SECONDS = ("apply_fetched_seconds", "deferred_attachments_seconds",
                   "image_ocr_seconds")
BACKLOG_OPTIONS = BACKLOG_SECONDS + (
    "deferred_workers", "deferred_extract_workers", "deferred_min_free_gb", "image_ocr_workers",
    "apply_fetched_dir")
_BACKLOG_FIELDS = {"deferred_attachments_seconds": "seconds", "deferred_workers": "workers",
                   "deferred_extract_workers": "workers", "deferred_min_free_gb": "gib",
                   "image_ocr_seconds": "seconds", "image_ocr_workers": "workers",
                   "apply_fetched_seconds": "seconds", "apply_fetched_dir": "dir"}


def _backlog_argv(state: Path, options: Mapping[str, Any]) -> list[str]:
    args = [_python(), "-m", "integrations.capture_mail", "backlog", "--state-dir",
            str(state), "--live-audience", options["live_audience"], "--host-child"]
    if options.get("principal"):
        args += ["--principal", options["principal"]]
    for key in BACKLOG_OPTIONS:
        if options.get(key) is not None:
            args += ["--" + key.replace("_", "-"), str(options[key])]
    return args + _budget(options)


def summarize_backlog(report: Mapping[str, Any]) -> dict[str, Any]:
    """One unit per pass the backlog command ran: complete when it ended on its own (its
    backlog read or nothing left to do), else cut by the budget, a drain or the disk
    floor (the rest stays in the store's backlog for the next run). An exclusion that
    cannot say whom it withholds is a typed block (nothing was read)."""
    passes = report.get("passes")
    passes = passes if isinstance(passes, Mapping) else {}
    units, blocked = [], []
    for name, counts in sorted(passes.items()):
        counts = counts if isinstance(counts, Mapping) else {}
        error = code(counts.get("error"))
        if error == "people_exclusion_unavailable":
            blocked.append({"part": f"mail_backlog.{code(name) or 'pass'}",
                            "source": "microsoft-graph", "error_code": error,
                            "cause": "later_ask", "later_ask": "excluded-people-oids"})
            continue
        cut = next((key for key in ("disk_low", "budget_spent") if counts.get(key)), None)
        if report.get("drained") is True and (cut or counts.get("abandoned")):
            cut = "drained"
        units.append({"complete": error is None and cut is None, "resumed": False,
                      "resumable": False, "reason": error or cut})
    return _units(units, report, requests=0, blocked=blocked, basis="passes")


def _graph_argv(command: str) -> Callable[[Path, Mapping[str, Any]], list[str]]:
    def argv(state: Path, options: Mapping[str, Any]) -> list[str]:
        args = [_python(), "-m", "integrations.graph_read", command, "--state-dir", str(state),
                "--live-audience", options["live_audience"], "--runs", "1"]
        # mail: summarize_mail reads the runs only; the store-wide counts after the capture
        # (unbudgeted, the whole 24.7 GB store) ran every run past its timeout (11-13)
        args += ["--runs-only"] if command == "mail-capture" else []
        if options.get("scope"):
            args += ["--scope", options["scope"]]
        if options.get("page_size"):
            args += ["--page-size", str(options["page_size"])]
        if options.get("registry"):
            args += ["--registry", options["registry"]]
        if options.get("principal"):   # the host's capture principal, not the CLI default
            args += ["--principal", options["principal"]]
        if options.get("named_originals") is True:   # case originals' items first
            args += ["--named-originals"]
        if command == "sharepoint-capture":
            # the post-run self-check samples its handles: unsampled, it outran the grace
            # every cycle on the whole estate (child_timeout)
            args += ["--verify-sample", str(SHAREPOINT_VERIFY_SAMPLE)]
        return args + _budget(options)
    return argv


def portal_mirror(state: Path) -> Path:
    from .case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE   # the host's working mirror

    return state / CASE_RUNTIME_DIR / MIRROR_FILE


def _portal_argv(state: Path, options: Mapping[str, Any]) -> list[str]:
    database = _private_parent(portal_mirror(state))
    args = [_python(), "-m", "integrations.source_mirror", "--db", str(database),
            "--audience", options["audience"], "--tenant", options.get("tenant", "plx"),
            "--principal", options.get("principal", DEFAULT_PRINCIPAL),
            "--clearance", options.get("clearance", "restricted"), "sync"]
    for relation in options.get("relations") or ():
        args += ["--relation", relation]
    if options.get("relation_budget_seconds") is not None:   # each relation's first slice
        args += ["--relation-budget-seconds", str(options["relation_budget_seconds"])]
    if options.get("full_below_rows") is not None:   # small relations read whole (GA-F12)
        args += ["--full-below-rows", str(options["full_below_rows"])]
    # summarize_portal reads the runs only: the sync's coverage (whole reads of the
    # mirror after the capture, no budget) outran the child's timeout (11-13)
    return args + ["--no-coverage"] + _budget(options)


def _bc_argv(state: Path, options: Mapping[str, Any]) -> list[str]:
    from .host import state_capture_paths   # the host's BC store: <state>/bc-capture

    database = _private_parent(state_capture_paths(state)["bc"])
    # The host's capture audience and principal: the host's store is bound to that tenant
    # (``operating.host`` ``_bc``), so a child bound to the BC tenant itself is refused
    # (``binding_mismatch``) and its records would not resolve for the host's readers.
    args = [_python(), "-m", "integrations.bc_read", "capture", "--db", str(database),
            "--mode", "auto", "--passes", "1", "--live-audience", options["live_audience"],
            "--principal", options.get("principal", DEFAULT_PRINCIPAL)]
    for entity_set in options.get("entity_sets") or ():
        args += ["--entity-set", entity_set]
    if options.get("page_size"):
        args += ["--page-size", str(options["page_size"])]
    return args + _budget(options)


def _bc_unlisted(options: Mapping[str, Any]) -> list[str]:
    """``bc_read.CAPTURE_ENTITY_SETS`` a section's ``entity_sets`` does not name (none
    when the section names no list: the CLI then reads every capture set)."""
    from integrations.bc_read import CAPTURE_ENTITY_SETS

    listed = options.get("entity_sets")
    if listed is None:
        return []
    return [f"entity_sets:unlisted:{name}" for name in CAPTURE_ENTITY_SETS
            if name not in set(listed)]


def _mc_argv(state: Path, options: Mapping[str, Any]) -> list[str]:
    from integrations.mc_sources import store_path

    # The host's capture audience and principal, as for BC, mail and SharePoint: a store
    # stamped with the CLI's own live-check tenant refuses every host ``mc://`` read and
    # claim (``binding_mismatch``); the CLI moves such a store aside and re-captures.
    args = [_python(), "-m", "integrations.mc_sources", "capture",
            "--store", str(_private_parent(store_path(state))), "--scope", options["scope"],
            "--passes", "1", "--live-audience", options["live_audience"],
            "--principal", options.get("principal", DEFAULT_PRINCIPAL)]
    for query in options.get("queries") or ():
        args += ["--query", query]
    if options.get("max_task_reads"):
        args += ["--max-task-reads", str(options["max_task_reads"])]
    if options.get("fast_poll_seconds"):
        args += ["--fast-poll-seconds", str(options["fast_poll_seconds"])]
    if options.get("operator_email_default"):
        args += ["--operator-email-default", options["operator_email_default"]]
    return args + _budget(options)


def _state_store(family: str) -> Callable[[Path], tuple[Path, ...]]:
    def writes(state: Path) -> tuple[Path, ...]:
        from .host import state_capture_paths

        return (state_capture_paths(state)[family],)
    return writes


def _sharepoint_store(index: int) -> Callable[[Path], tuple[Path, ...]]:
    def writes(state: Path) -> tuple[Path, ...]:
        from ontology.project import SharePointCaptures   # (drive store, list-item store)

        return (SharePointCaptures.stores(state)[index],)
    return writes


def _units(units: list[dict[str, Any]], report: Mapping[str, Any], *,
           requests: Any = None, blocked: Any = None, excluded: int = 0,
           basis: str = "requests_sent") -> dict[str, Any]:
    raw = report.get("blocked_upstream") if blocked is None else blocked
    return {"units": units, "requests": count(report.get("requests_sent")
                                              if requests is None else requests),
            "requests_basis": basis, "excluded": excluded,
            "blocked": [typed_block(b) for b in (raw or ()) if isinstance(b, Mapping)],
            "evidence_label": code(report.get("evidence_label"))}


def _last_run(report: Mapping[str, Any]) -> Mapping[str, Any]:
    runs = report.get("runs")
    return runs[-1] if isinstance(runs, list) and runs and isinstance(runs[-1], Mapping) \
        else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) \
        else []


def summarize_mail(report: Mapping[str, Any]) -> dict[str, Any]:
    """Folders of the last run: complete, resumed from the stored delta/next link."""
    units = [{"complete": folder.get("complete") is True,
              "resumed": folder.get("resumed_from") in ("delta_url", "next_url"),
              "reason": folder.get("reason") or folder.get("status")}
             for mailbox in _rows(_last_run(report).get("mailboxes"))
             for folder in _rows(mailbox.get("folders"))]
    # a mailbox the capture never opened (not_visible, forbidden, ...) has no folder: it is
    # one incomplete unit with its own status, never "no units reported"
    units += [{"complete": False, "resumed": False, "reason": mailbox.get("status")}
              for mailbox in _rows(_last_run(report).get("mailboxes"))
              if not _rows(mailbox.get("folders")) and mailbox.get("status") != "people_excluded"
              and mailbox.get("complete") is not True]
    summary = _units(units, report)
    if "source_unavailable" in (_last_run(report).get("incomplete_reasons") or ()):
        # The capture stopped reading because Graph failed every mailbox it tried
        # (capture_mail.OUTAGE_MAILBOXES): the run failed, whatever it read before.
        summary["error_code"] = "source_unavailable"
    return summary


def summarize_teams(report: Mapping[str, Any]) -> dict[str, Any]:
    """Conversations of the last run; an excluded person's is withheld, not a gap, and
    so is a chat no token of this tenant may read (``not_readable``: hosted in another
    tenant), counted with them, never a failed or blocked unit."""
    rows = _rows(_last_run(report).get("conversations"))
    kept = [row for row in rows if row.get("status") not in ("people_excluded",
                                                              "not_readable")]
    units = [{"complete": row.get("status") == "complete",
              "resumed": row.get("resumed_from") is not None,
              "reason": (_codes(row.get("reasons")) or [row.get("status")])[0]}
             for row in kept]
    # A member's chat listing cut at the client's cap left chats undiscovered: one
    # incomplete unit each (``cap:chat_listing_<why>``), so the source is partial with that
    # reason in the host ledger, never a silent complete.
    for part in (_last_run(report).get("discovery"), report.get("census")):
        part = part if isinstance(part, Mapping) else {}
        why = next((c for c in _codes(part.get("reasons"))
                    if c.startswith("cap:chat_listing_")), "cap:chat_listing_page_cap")
        units += [{"complete": False, "resumed": False, "reason": why}
                  for _ in range(min(count(part.get("chat_listings_capped")) or 0, MAX_ITEMS))]
    return _units(units, report, excluded=len(rows) - len(kept))


def summarize_meetings(report: Mapping[str, Any]) -> dict[str, Any]:
    """Calendars and plans of the last run, plus the transcript part; what the people
    guard withheld (events, tasks, mailboxes) is counted as excluded, never a gap."""
    run = _last_run(report)
    units = [{"complete": row.get("status") == "complete",
              "resumed": row.get("resumed_from") is not None,
              "reason": (_codes(row.get("reasons")) or [row.get("status")])[0]}
             for row in [*_rows(run.get("mailboxes")), *_rows(run.get("plans"))]]
    transcripts = run.get("transcripts")
    if isinstance(transcripts, Mapping) and transcripts:
        units.append({"complete": transcripts.get("status") == "complete", "resumed": False,
                      "reason": (_codes(transcripts.get("reasons"))
                                 or [transcripts.get("status")])[0]})
    counts = run.get("counts") if isinstance(run.get("counts"), Mapping) else {}
    excluded = sum(count(counts.get(k)) or 0 for k in ("mailboxes_excluded", "events_withheld",
                                                        "tasks_withheld"))
    return _units(units, report, excluded=excluded)


def summarize_sharepoint(report: Mapping[str, Any]) -> dict[str, Any]:
    """Roots of the last run: incremental (change feed) or a cut walk continued."""
    units = [{"complete": root.get("complete") is True,
              "resumed": root.get("round") == "incremental"
              or root.get("full_walk_reason") == "resumed",
              "reason": root.get("reason") or root.get("status")}
             for target in _rows(_last_run(report).get("targets"))
             for root in _rows(target.get("roots"))]
    units += [{"complete": False, "resumed": False, "reason": target.get("status")}
              for target in _rows(_last_run(report).get("targets"))
              if not _rows(target.get("roots")) and target.get("complete") is not True]
    discovery = _last_run(report).get("discovery")
    gaps = _codes(discovery.get("gaps")) if isinstance(discovery, Mapping) else []
    found = discovery.get("counts") if isinstance(discovery, Mapping) else None
    if units and isinstance(found, Mapping) and (count(found.get("libraries_carried")) or 0):
        # Roots carried from the store because a listing failed (capture_sharepoint
        # ``_carry``): captured, but the estate was not listed, so the run is partial with
        # the listing's own gap, never complete.
        units.append({"complete": False, "resumed": False,
                      "reason": next((g for g in gaps if g.startswith("discovery_sites")),
                                     gaps[0] if gaps else "discovery_carried")})
    if report.get("throttle_hold") is True:
        # Graph held the app (the shared 429/503 hold, ``graph_throttle``) past what a read
        # may wait: the run stopped there, every position kept, the rest read next run.
        units.append({"complete": False, "resumed": False, "reason": "throttle_hold"})
    summary = _units(units, report)
    if not units and gaps:
        # An estate scope plans its roots from discovery: one that found nothing to plan
        # failed with its own typed gap (``discovery_sites:retried.http_429``: Graph
        # throttled the site listing), never the bare ``no_units_reported`` (11-19).
        summary["error_code"] = next((g for g in gaps if g.startswith("discovery_sites")),
                                     gaps[0])
    return summary


def summarize_lists(report: Mapping[str, Any]) -> dict[str, Any]:
    units = [{"complete": row.get("status") == "complete",
              "resumed": row.get("resumed_from") == "cursor",
              "reason": (_codes(row.get("reasons")) or [row.get("status")])[0]}
             for row in _rows(_last_run(report).get("lists"))]
    return _units(units, report)


def summarize_bc(report: Mapping[str, Any]) -> dict[str, Any]:
    """Units of every company: incremental = read from the modified-since watermark. A
    unit without a read plan (the company record), a set with calculated fields
    (``full_reason`` ``calculated_fields``) and the lines of a header set whose posted
    documents BC edits in place (receipts, shipments: ``bc_read`` skips lines only for
    ``POSTED_DOCUMENT_STATUSES`` sets) are read in full every pass by design: they have no
    cursor to resume from and are not ``resumable``."""
    from integrations.bc_read import POSTED_DOCUMENT_STATUSES
    from integrations.live_source_grammar import BC_DOCUMENT_LINES

    full_lines = {lines for header, lines in BC_DOCUMENT_LINES.items()
                  if header not in POSTED_DOCUMENT_STATUSES}
    units = [{"complete": unit.get("complete") is True,
              "resumed": unit.get("mode") == "incremental",
              "resumable": "mode" in unit and unit.get("full_reason") != "calculated_fields"
              and unit.get("entity_set") not in full_lines,
              "reason": unit.get("reason") or unit.get("state")}
             for company in _rows(report.get("companies"))
             for unit in _rows(company.get("units"))]
    return _units(units, report)


def _brain_argv(state: Path, options: Mapping[str, Any]) -> list[str]:
    return [_python(), "-m", "integrations.knowledge_source", "capture",
            "--state-dir", str(state), "--namespaces", options["namespaces"],
            "--principal", options.get("principal", DEFAULT_PRINCIPAL)]


def summarize_brain(report: Mapping[str, Any]) -> dict[str, Any]:
    """The namespaces read (one unit each), complete when the listing reached the
    brain's total unchanged; a missing key is a typed block, no unit."""
    units = [{"complete": unit.get("state") == "complete", "resumed": False,
              "resumable": False, "reason": unit.get("reason") or unit.get("state")}
             for unit in _rows(report.get("units"))]
    return _units(units, report)


def summarize_mc(report: Mapping[str, Any]) -> dict[str, Any]:
    """Units of the last pass; a failed one's reason names its error code
    (``source_error:auth_refused``: MC refused the key), never a bare class."""
    passes = _rows(report.get("passes"))
    units = [{"complete": unit.get("state") == "complete", "resumed": False,
              "reason": (f"{unit['reason']}:{unit['error']}"
                         if code(unit.get("reason")) and code(unit.get("error"))
                         else unit.get("reason") or unit.get("state"))}
             for unit in _rows(passes[-1].get("units") if passes else [])]
    return _units(units, report)


_PORTAL_CAUSES = {"failed": "upstream_outage", "unavailable": "relation_unavailable",
                  "access_revoked": "missing_bindings"}


def summarize_portal(report: Mapping[str, Any]) -> dict[str, Any]:
    """Relations of the sync: an incremental refresh read from the stored ``since``.
    Requests are counted as pages (plus the catalog read), the CLI prints no total."""
    runs = _rows(report.get("runs"))
    units, blocked = [], []
    for run in runs:
        outcome = run.get("outcome")
        units.append({"complete": outcome == "captured",
                      # "none": the kept capture was current, nothing re-read
                      "resumed": outcome == "captured"
                      and run.get("mode") in ("incremental", "none"),
                      "reason": run.get("failure_class") or outcome})
        if outcome in _PORTAL_CAUSES:
            blocked.append({"part": "portal.relation", "source": "portal-workbench-api",
                            "http_status": run.get("http_status"),
                            "error_code": run.get("failure_class") or outcome,
                            "cause": _PORTAL_CAUSES[outcome]})
    pages = sum(count(run.get("pages")) or 0 for run in runs)
    return _units(units, report, requests=pages + 1 if runs else 0, blocked=blocked,
                  basis="pages")


_GRAPH_FIELDS = {"scope": "path", "live_audience": "path", "budget_seconds": "seconds"}
SOURCES: dict[str, SourceSpec] = {spec.name: spec for spec in (
    SourceSpec("portal", "portal-workbench-api", _portal_argv, summarize_portal,
               {"audience": "path", "relations": "strs", "principal": "str",
                "clearance": "str", "tenant": "str", "budget_seconds": "seconds",
                "relation_budget_seconds": "seconds", "full_below_rows": "int"},
               frozenset({"audience"}), PORTAL_ENV, projected="portal_api",
               served_store=portal_mirror, writes=lambda state: (portal_mirror(state),)),
    SourceSpec("bc", "business-central", _bc_argv, summarize_bc,
               {"entity_sets": "strs", "page_size": "int", "budget_seconds": "seconds",
                "live_audience": "path", "principal": "str"},
               frozenset({"live_audience"}), BC_ENV, projected="bc",
               writes=_state_store("bc"), unlisted=_bc_unlisted),
    SourceSpec("mail", "microsoft-graph", _graph_argv("mail-capture"), summarize_mail,
               {**_GRAPH_FIELDS, "page_size": "int", "principal": "str"},
               frozenset({"live_audience"}),
               GRAPH_ENV, projected="graph_mail", writes=_state_store("graph_mail")),
    # The mail store's backlogs (attachments the OCR backlog deferred, the fast path's
    # readings, unread images) as their own child with its own hard budget: never on the
    # mail capture's path, never beside it (one writer), drained once mail is due.
    SourceSpec("mail_backlog", "microsoft-graph", _backlog_argv, summarize_backlog,
               {"live_audience": "path", "principal": "str", "budget_seconds": "seconds",
                **_BACKLOG_FIELDS},
               frozenset({"live_audience", "budget_seconds"}), GRAPH_ENV,
               writes=_state_store("graph_mail"), yields_to=("mail",)),
    SourceSpec("sharepoint", "microsoft-graph", _graph_argv("sharepoint-capture"),
               summarize_sharepoint, {**_GRAPH_FIELDS, "registry": "path", "principal": "str",
                                     # the host check's coverage census (not the CLI's)
                                     "census_budget_seconds": "seconds",
                                     # true: each run first captures the items the
                                     # case originals name (--named-originals)
                                     "named_originals": "flag"},
               frozenset({"live_audience"}), GRAPH_ENV, projected="sharepoint",
               writes=_sharepoint_store(0)),
    SourceSpec("sharepoint_lists", "microsoft-graph", _graph_argv("sharepoint-list-capture"),
               summarize_lists, {"scope": "path", "live_audience": "path",
                                 # the pass stops itself between pages after the budget
                                 # (resumable), killed only after the grace (11-16)
                                 "budget_seconds": "seconds",
                                 "timeout_seconds": "seconds"},
               frozenset({"live_audience"}), GRAPH_ENV, "interrupted_only", None,
               projected="sharepoint", relations=frozenset({"listItems"}),
               writes=_sharepoint_store(1)),
    SourceSpec("teams", "microsoft-graph", _graph_argv("teams-capture"), summarize_teams,
               dict(_GRAPH_FIELDS), frozenset({"live_audience"}), GRAPH_ENV,
               projected="teams", writes=_state_store("teams")),
    SourceSpec("meetings", "microsoft-graph", _graph_argv("meetings-capture"),
               summarize_meetings, {**_GRAPH_FIELDS, "principal": "str"},
               frozenset({"live_audience"}), MEETINGS_ENV, projected="meetings",
               writes=_state_store("meetings"), optional_env=MEETINGS_OPTIONAL_ENV),
    # The brain's knowledge items (``agent/items``, GET only through
    # ``integrations.vmc``) into the kept knowledge store the host already projects as
    # ``KnowledgeItem``: needs a brain or VMC key (never the MC key), and the host's
    # ``knowledge_namespaces`` authority granting the capture principal.
    SourceSpec("brain", "vmc-brain", _brain_argv, summarize_brain,
               {"namespaces": "path", "principal": "str", "timeout_seconds": "seconds"},
               frozenset({"namespaces"}), (), "snapshot", None, projected="brain",
               writes=_state_store("brain"), optional_env=BRAIN_OPTIONAL_ENV,
               env_one_of=("BRAIN_API_KEY", "VMC_API_KEY")),
    SourceSpec("mc", "mission-control-mcp", _mc_argv, summarize_mc,
               {"scope": "str", "queries": "strs", "max_task_reads": "int",
                "fast_poll_seconds": "int",
                "operator_email_default": "str", "timeout_seconds": "seconds",
                "budget_seconds": "seconds", "live_audience": "path", "principal": "str"},
               frozenset({"scope", "live_audience"}), MC_ENV, "snapshot", None,
               projected="mission_control", writes=_state_store("mission_control")),
)}
# Capture CLIs on main this runner does not call: capture-breadth repeats the mail and
# SharePoint captures above into the same stores.
NOT_RUN = {"capture_breadth": "duplicates_mail_and_sharepoint"}



def _number(value: Any, low: float, high: float) -> bool:
    return type(value) in (int, float) and low <= value <= high


def _string(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 300 and not value.startswith("-")
            and all(ord(c) >= 32 for c in value))


def _option(kind: str, value: Any, base: Path) -> Any:
    if kind == "seconds" and _number(value, 1, MAX_BUDGET_SECONDS):
        return value
    if kind == "int" and type(value) is int and 1 <= value <= 100_000:
        return value
    if kind == "workers" and type(value) is int and 1 <= value <= MAX_CHILD_WORKERS:
        return value
    if kind == "gib" and _number(value, 0, 100_000):   # a free-space floor, 0 allowed
        return value
    if kind == "flag" and type(value) is bool:
        return value
    if kind == "str" and _string(value):
        return value
    if kind == "strs" and isinstance(value, list) and value and len(value) <= 1000 \
            and all(_string(v) for v in value):
        return list(value)
    if kind == "dir" and _string(value):   # a directory another job writes (never Git)
        path = (base / value).resolve()
        if path.is_dir():
            return str(path)
    if kind == "path" and _string(value):
        path = (base / value).resolve()
        if path.is_file():
            return str(path)
    raise IngestError("source_config_invalid")


# ------------------------------------------------------------------ one source

def child_env(environ: Mapping[str, str], spec: SourceSpec, state: Path) -> dict[str, str]:
    env = {name: value for name, value in environ.items()
           if not CREDENTIAL_NAME.search(name) or name in spec.env
           or name in spec.optional_env}
    env[STATE_ENV] = str(state)
    from integrations import cpu_budget, drain
    env[drain.ENV] = "1"   # SIGTERM drains it (run_source ``drain``)
    env.pop(cpu_budget.TOTAL_ENV, None)
    if spec.name in cpu_budget.EXTRACTING and cpu_budget.BUDGET_ENV not in env:
        # its share of the host's one extraction budget (``ingest.cpu_budget``)
        env[cpu_budget.BUDGET_ENV] = str(cpu_budget.child_share(environ))
    return env


_TRACEBACK = "Traceback (most recent call last):"
_RAISED = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?::|$)")


def _stderr_code(stderr: bytes) -> tuple[str | None, int | None]:
    """The error class/code and HTTP status of the last JSON line on stderr; never its
    message. Without one, a child that died of an uncaught exception is named by its
    class (``uncaught:IntegrityError``, from the traceback's last line, the message cut
    off): else the ledger says only ``exit_1`` and the cause is lost with the pipe
    (2026-11-02: Teams exited 1 every run for a day with nothing to tell why)."""
    lines = stderr.decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, Mapping):
            status = value.get("http_status")
            return (code(value.get("error_code")) or code(value.get("error"))
                    or code(value.get("error_class")),
                    status if type(status) is int and 100 <= status <= 599 else None)
    if _TRACEBACK in lines:
        after = [line for line in lines[len(lines) - 1 - lines[::-1].index(_TRACEBACK):]
                 if line and not line[0].isspace()]
        # the first unindented line after the frames: the exception's own (a message
        # running on over more lines is never read)
        raised = _RAISED.match(after[1]) if len(after) > 1 else None
        if raised is not None:
            return code("uncaught:" + raised.group(1).rsplit(".", 1)[-1]), None
    return None, None


def _numeric(report: Mapping[str, Any]) -> dict[str, int]:
    held = report.get("summary")
    held = held if isinstance(held, Mapping) else report.get("_summary")
    held = held if isinstance(held, Mapping) else report
    out = {key: value for key, value in held.items()
           if code(key) and count(value) is not None}
    return dict(sorted(out.items())[:MAX_COUNTS])


def _base(spec: SourceSpec) -> dict[str, Any]:
    return {"name": spec.name, "status": None, "resume_kind": spec.resume_kind,
            "exit_code": None, "seconds": 0.0, "requests": None, "requests_basis": None,
            "units": 0, "units_complete": 0, "units_resumed": 0, "units_full_by_design": 0,
            "units_excluded": 0,
            "resumed_from_cursor": False, "blocked": [], "reasons": [], "error_code": None,
            "counts": {}}


def blocked_result(spec: SourceSpec, blocks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {**_base(spec), "status": "blocked", "blocked": [dict(b) for b in blocks]}


def _judge(result: dict[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    units = summary["units"]
    total, complete = len(units), sum(u["complete"] for u in units)
    cursor_units = [u for u in units if u["complete"] and u.get("resumable", True)]
    resumed = sum(u["resumed"] for u in cursor_units)
    blocked = summary["blocked"][:MAX_ITEMS]
    result.update(units=total, units_complete=complete, units_resumed=resumed,
                  units_full_by_design=complete - len(cursor_units),
                  units_excluded=summary["excluded"], requests=summary["requests"],
                  requests_basis=summary["requests_basis"], blocked=blocked,
                  resumed_from_cursor=bool(cursor_units) and resumed == len(cursor_units))
    if summary.get("evidence_label"):
        result["evidence_label"] = summary["evidence_label"]
    if code(summary.get("error_code")):
        result.update(status="failed", error_code=code(summary["error_code"]))
    elif total and complete == total and not blocked:
        result["status"] = "complete"
    elif blocked and not complete:
        result["status"] = "blocked"
    elif not total:
        result.update(status="failed", error_code="no_units_reported")
    else:
        reasons = {code(u["reason"]) or "incomplete" for u in units if not u["complete"]}
        result.update(status="partial",
                      reasons=sorted(reasons | ({"blocked_parts"} if blocked else set()))
                      [:MAX_ITEMS])
    return result


STDERR_DIR = ("host", "logs")   # <state>/host/logs/<source>.stderr: each child's last run
STDERR_KEEP_BYTES = 256 * 1024     # its tail
STACK_DUMP_SECONDS = 5.0           # a timed-out child's stack dump is waited for this long


def keep_stderr(state: Path, name: str, stderr: bytes | None) -> None:
    """The tail of a capture child's stderr, kept in ``<state>/host/logs/<name>.stderr``
    (0600, replaced by its next run): the runner reads only the last JSON line's code,
    and a child cut by its timeout reported nothing at all (2026-11-12: mail capture
    wrote nothing for a day and its stderr, the one place a cause could be, was
    dropped). Never raises: a log that cannot be written costs only the log."""
    if not code(name):
        return
    try:
        folder = state.joinpath(*STDERR_DIR)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / f"{name}.stderr"
        _write_private(target, (stderr or b"")[-STDERR_KEEP_BYTES:])
    except OSError:
        pass


def _store_file_mode(folder: Path) -> int:
    """0o660 for a file in a shared-group directory (setgid: the state setup,
    deploy/release/api_user_setup.sh, gives it to host/), else 0o600. The ingest unit's
    UMask=0077 would otherwise leave these files 0600 and the API user could not read
    them: the cause of the capture-perm-hold repairs (11-16 ops-fix). Others get nothing."""
    try:
        return 0o660 if os.stat(folder).st_mode & stat.S_ISGID else 0o600
    except OSError:
        return 0o600


def _write_private(target: Path, data: bytes) -> None:
    """``data`` into ``target`` through a temp file and a rename, the file's mode set
    explicitly (a umask cannot narrow the shared group's read)."""
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), _store_file_mode(target.parent))
        handle.write(data)
    os.replace(tmp, target)


def _dump_stacks(child: "subprocess.Popen") -> None:
    """Ask a timed-out child for every thread's stack (SIGUSR1: ``faulthandler`` in
    ``integrations.graph_read``) and give it :data:`STACK_DUMP_SECONDS` to write it; the
    output stays buffered in ``child`` for the ``communicate`` after the kill. A child
    without the handler ends on the signal, as it was about to."""
    if not hasattr(signal, "SIGUSR1"):
        return
    try:
        os.kill(child.pid, signal.SIGUSR1)
        child.communicate(timeout=STACK_DUMP_SECONDS)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass


def run_source(spec: SourceSpec, options: Mapping[str, Any], state: Path,
               environ: Mapping[str, str], *, grace: float, cwd: Path = ROOT,
               children: Any = None, cancel: threading.Event | None = None,
               drain: threading.Event | None = None,
               drain_seconds: float = DEFAULT_DRAIN_SECONDS) -> dict[str, Any]:
    """One capture CLI as a child process; every outcome is this source's typed result.
    ``children`` (``operating.host.CycleChildren``): the child is recorded there while it
    runs, so the next cycle stops it if this runner is killed first. ``cancel``: once set,
    the child is stopped (its process group killed) and the result is ``failed`` with
    :data:`STOPPED`, which a continuous run never records (the capture resumes from its
    own cursor next time).

    ``drain``: once set, the child is sent SIGTERM once and given ``drain_seconds``. A
    capture that drains (``integrations.drain``) ends its run at its next safe point,
    every cursor kept, and prints its report, which is this source's result as any other
    (``drained`` true). One that ends on the signal without a report, or is still running
    at the bound (its process group then killed), is :data:`STOPPED` (``drain_overran``
    for the latter): never recorded, resumed from its cursor next time."""
    missing = [name for name in spec.env if not environ.get(name)]
    if spec.env_one_of and not any(environ.get(name) for name in spec.env_one_of):
        missing += list(spec.env_one_of)
    if missing:
        return blocked_result(spec, [typed_block({
            "part": f"{spec.name}.credentials", "source": spec.source,
            "error_code": "credentials_absent", "cause": "later_ask",
            "later_ask": "credentials", "missing_settings": missing})])
    result = _base(spec)
    budget = options.get(spec.budget_field) if spec.budget_field else None
    timeout = (budget + grace) if budget else options.get("timeout_seconds",
                                                           DEFAULT_TIMEOUT_SECONDS)
    started = time.monotonic()
    try:
        argv = spec.argv(state, options)
        child = subprocess.Popen(argv, cwd=str(cwd), env=child_env(environ, spec, state),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
    except Exception as exc:  # noqa: BLE001 - this source's failure, never the cycle's
        return {**result, "status": "failed", "error_code": code(type(exc).__name__)
                or "start_failed"}
    stdout = stderr = None
    drained_at: float | None = None
    try:
        if children is not None:
            children.add(child.pid, spec.name)
        deadline = started + timeout
        while True:   # communicate again after a timeout loses no output (subprocess)
            try:
                stdout, stderr = child.communicate(timeout=max(0.01, min(
                    POLL_SECONDS, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:   # the finally below kills its group
                if cancel is not None and cancel.is_set():
                    return {**result, "status": "failed", "error_code": STOPPED,
                            "seconds": round(time.monotonic() - started, 1)}
                if drain is not None and drain.is_set() and drained_at is None:
                    drained_at = time.monotonic()
                    try:
                        os.kill(child.pid, signal.SIGTERM)   # the child, not its group
                    except OSError:
                        pass
                    deadline = min(deadline, drained_at + drain_seconds)
                    continue
                if drained_at is not None and time.monotonic() >= deadline:
                    return {**result, "status": "failed", "error_code": STOPPED,
                            "drain_overran": True,
                            "seconds": round(time.monotonic() - started, 1)}
                if time.monotonic() >= deadline:
                    _dump_stacks(child)
                    return {**result, "status": "failed", "error_code": "child_timeout",
                            "seconds": round(time.monotonic() - started, 1)}
    finally:
        child_process.stop(child)
        if stderr is None:   # cut: what it wrote before the kill
            try:
                _out, stderr = child.communicate(timeout=STACK_DUMP_SECONDS)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                stderr = None
        keep_stderr(state, spec.name, stderr)
        for pipe in (child.stdout, child.stderr):
            pipe.close()
        if children is not None:
            children.remove(child.pid)
    result.update(exit_code=child.returncode, seconds=round(time.monotonic() - started, 1))
    try:
        report = json.loads(stdout.decode("utf-8"))
    except ValueError:
        report = None
    if drained_at is not None:
        if not isinstance(report, Mapping) or child.returncode != 0 and \
                report.get("drained") is not True:
            # ended by the signal (a capture that does not drain): resumed next time
            return {**result, "status": "failed", "error_code": STOPPED}
        result["drained"] = True
    if not isinstance(report, Mapping) or (report.get("ok") is False and "error" in report):
        error, status = _stderr_code(stderr)
        if isinstance(report, Mapping):
            # the refusal's own code (mc_sources: ``error``) before its class: a bare
            # ``CaptureError`` names no cause
            error = code(report.get("error")) or code(report.get("error_class")) or error
        return {**result, "status": "failed", "http_status": status,
                "error_code": error or f"exit_{child.returncode}"}
    try:
        summary = spec.summarize(report)
    except Exception as exc:  # noqa: BLE001 - a report this runner cannot read
        return {**result, "status": "failed",
                "error_code": f"report_unread:{code(type(exc).__name__) or 'error'}"}
    result["counts"] = _numeric(report)
    return _judge(result, summary)


def ledger_outcome(spec: SourceSpec, result: Mapping[str, Any], state: Path
                   ) -> dict[str, Any]:
    """``result`` (:func:`run_source`) as the host ledger records a source's outcome
    (``Host._record``), the result itself kept under ``capture``: complete is
    ``captured``, partial ``partial`` (a new generation); ``blocked`` and ``failed`` keep
    the generation served, with the refused parts as ``blocked_upstream`` (a failure with
    an HTTP status is one part of the source) and the first as the source's ``outage``,
    as the host's own readers report them; ``not_configured`` leaves the row as it was."""
    status = result.get("status")
    base = {"upstream": spec.source, "capture": dict(result)}
    if status == "not_configured":
        return {**base, "state": "not_configured", "reasons": []}
    counts = {key: result.get(key) for key in ("requests", "units", "units_complete",
                                               "units_resumed")}
    if status in ("complete", "partial"):
        outcome = {**base, "state": "captured" if status == "complete" else "partial",
                   "complete": status == "complete", "counts": counts,
                   "reasons": list(result.get("reasons") or [])}
        if spec.served_store is not None:
            outcome["served_path"] = str(spec.served_store(state))
        return outcome
    parts = [dict(b) for b in result.get("blocked") or () if is_typed(b)]
    if not parts and result.get("http_status") is not None:
        parts = [typed_block({"part": spec.name, "source": spec.source,
                              "http_status": result["http_status"],
                              "error_code": result.get("error_code"),
                              "cause": "upstream_outage"})]
    reasons = _codes([result.get("error_code"), *(result.get("reasons") or ()),
                      *(b.get("error_code") for b in result.get("blocked") or ())])
    outcome = {**base, "state": "blocked" if status == "blocked" else "failed",
               "complete": False, "counts": counts,
               "reasons": reasons or [code(status) or "failed"]}
    if parts:
        outcome["blocked_upstream"] = parts
        outcome["outage"] = {"http_status": parts[0].get("http_status"),
                             "error_code": parts[0].get("error_code")}
    return outcome


# ------------------------------------------------------------------ check

def _secret_tokens(environ: Mapping[str, str], state: Path | None) -> list[str]:
    tokens = {v.strip() for n, v in environ.items()
              if CREDENTIAL_NAME.search(n) and isinstance(v, str) and len(v.strip()) >= 6}
    tokens |= {v for v in re.split(r"[\s,;]+", environ.get(ENV_IDS) or "") if len(v) >= 6}
    for name in ("PLX_WORKBENCH_API_URL", ENV_FILE):
        if len(environ.get(name) or "") >= 6:
            tokens.add(environ[name])
    if state is not None:
        tokens.add(str(state))
    return sorted(tokens, key=len, reverse=True)


def leaks(text: str, tokens: Sequence[str]) -> int:
    """Occurrences of a secret, an excluded ID, the state path, an address, an object
    ID or a URL in ``text`` (a count; never which)."""
    return (sum(text.count(t) for t in tokens) + len(_EMAIL.findall(text))
            + len(_GUID.findall(text)) + text.count("://"))


def _by_name(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {r["name"]: r for r in report.get("sources") or () if isinstance(r, Mapping)}


def evaluate(cycles: Sequence[Mapping[str, Any]], expected: Sequence[str]) -> dict[str, Any]:
    """The live check's counters over consecutive cycles (all must be 0 but the
    ``resume_checked``/``full_reread_sources`` tallies)."""
    checks = {"cycles_run": sum(not c.get("refused") for c in cycles),
              "sources_expected": len(expected), "sources_without_status": 0,
              "resume_checked": 0, "resume_blocked": 0, "completed_not_resumed": 0,
              "requests_grew": 0, "full_reread_sources": 0, "blocked_without_cause": 0,
              "untyped_status": 0}
    for cycle in cycles:
        held = _by_name(cycle)
        checks["sources_without_status"] += sum(held.get(name, {}).get("status")
                                                not in STATUSES for name in expected)
        for result in held.values():
            blocked = result.get("blocked") or []
            if result.get("status") == "blocked" and (
                    not blocked or not all(is_typed(b) for b in blocked)):
                checks["blocked_without_cause"] += 1
            if result.get("status") not in ("complete", "blocked") and not (
                    result.get("error_code") or result.get("reasons")
                    or any(is_typed(b) for b in blocked)):
                checks["untyped_status"] += 1
    for before, after in zip(cycles, cycles[1:]):
        if before.get("refused") or after.get("refused"):
            continue
        later = _by_name(after)
        for name, first in _by_name(before).items():
            if first.get("status") != "complete":
                continue
            if first.get("resume_kind") != "cursor":
                checks["full_reread_sources"] += 1
                continue
            second = later.get(name, {})
            if second.get("status") == "blocked" and second.get("blocked") \
                    and all(is_typed(b) for b in second["blocked"]):
                checks["resume_blocked"] += 1
                continue
            if second.get("status") == "complete" and second.get("units_complete") \
                    == second.get("units_full_by_design"):
                checks["full_reread_sources"] += 1   # no unit of it keeps a cursor
                continue
            checks["resume_checked"] += 1
            if second.get("status") != "complete" or not second.get("resumed_from_cursor"):
                checks["completed_not_resumed"] += 1
            one, two = count(first.get("requests")), count(second.get("requests"))
            if one is None or two is None or two > one:
                checks["requests_grew"] += 1
    return checks



def _family_relations(family: str, spec: SourceSpec,
                      sources: Mapping[str, SourceSpec]) -> list[str]:
    """The relations of ``family`` the schema binds that ``spec``'s capture writes: its
    own ``relations``, else the family's minus those another source of the table names."""
    from ontology.schema import load_schema

    if spec.relations is not None:
        return sorted(spec.relations)
    claimed = {r for other in sources.values() if other is not spec
               and other.projected == family for r in other.relations or ()}
    schema = load_schema()
    return sorted({b["relation"] for type_id, b in schema.bindings.values()
                   if b["source"] == family and (schema.object_types.get(type_id) or {})
                   .get("availability", {}).get("state") != "not_served"} - claimed)


def duplicates(state: Path, spec: SourceSpec, sources: Mapping[str, SourceSpec], *,
               internal_domains: Sequence[str] = ()) -> dict[str, Any]:
    """Items ``spec``'s capture store holds twice, read the way the projection reads it
    (``ontology.project`` capture sources): a record (relation, audience, key, field) with
    more than one current version, or two versions in a row with the same content (a
    re-read stored again instead of seen). Counts only."""
    from ontology import project
    from .host import capture_held, open_state_capture

    if spec.projected is None:
        return {"state": "not_projected"}
    family = spec.projected
    path = spec.served_store(state) if spec.served_store is not None else None
    if not (path.is_file() if path is not None else capture_held(state, family)):
        return {"state": "not_held"}
    versions: dict[tuple[str, ...], list[tuple[str, str, bool]]] = {}
    try:
        opened = (project.capture_source(family, path) if path is not None else
                  open_state_capture(state, family, internal_domains=internal_domains))
        with opened as source:
            gap = getattr(source, "capture_gap", None)
            relations = [r for r in _family_relations(family, spec, sources)
                         if gap is None or gap(r) != "source_not_captured"]
            records = source.records(relations)
            for relation in relations:
                for record in records[relation]:
                    versions.setdefault(
                        (relation, record.audience_key, record.record_key,
                         record.field_prefix), []).append(
                        (record.recorded_at, record.digest, record.superseded_at is None))
    except Exception as exc:  # noqa: BLE001 - an unread store is unchecked, never clean
        return {"state": "unreadable", "error_code": code(type(exc).__name__) or "error"}
    duplicate = 0
    for held_versions in versions.values():
        ordered = sorted(held_versions)
        if sum(current for _, _, current in ordered) > 1 or any(
                one[1] == two[1] for one, two in zip(ordered, ordered[1:])):
            duplicate += 1
    return {"state": "checked", "relations": len(relations), "items_checked": len(versions),
            "versions_checked": sum(len(v) for v in versions.values()),
            "duplicate_items": duplicate}




# ------------------------------------------------------------------ the host's ingest mode

# Retired settings (no host builds a projection generation, docs/no-builds.md): the
# values that meant "never build" (``builds`` ``off``, ``build_inputs`` ``live``) are taken
# as they are; any other is named in ``warnings`` and builds nothing, never a refusal.
RETIRED = {"builds": "off", "build_inputs": "live"}


INGEST_KEYS = frozenset({"mode", "grace_seconds", "max_parallel", "cpu_budget",
                         "drain_seconds", "graph_notifications", *RETIRED})


def _tolerant_bool(value: Any, name: str, warnings: list[str] | None) -> bool:
    """A true/false setting. The strings "true"/"false" (any case) are accepted; any other
    value is off and named in ``warnings`` (``ingest.<name>_invalid``): a mistyped optional
    setting never makes the host refuse its whole config (the 11-07 restart loop)."""
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    if warnings is not None:
        warnings.append(f"ingest.{name}_invalid")
    return False


def ingest_settings(value: Any, warnings: list[str] | None = None) -> dict[str, Any]:
    """The host config's ``ingest`` section: ``mode`` :data:`MODE`, ``grace_seconds``
    (the time a child gets past its budget), ``max_parallel`` (capture children at
    once, 1 to :data:`MAX_PARALLEL`; absent: one per configured source, at most
    :data:`MAX_PARALLEL`) and ``cpu_budget`` (cores the mail and SharePoint children's text
    extraction and OCR share, 1 to 256; absent: ``integrations.cpu_budget.host_budget`` as
    each child starts) and ``drain_seconds`` (the time a child asked to stop gets to end
    its run at a safe point, 1 to :data:`MAX_DRAIN_SECONDS`) and ``graph_notifications`` (true:
    the continuous mode starts mail and Teams early on a Graph change notification's wake,
    ``integrations.graph_notifications``; absent or false: no wake is read). A key this code does not know (a newer checkout's setting, a typo)
    is ignored and named in ``warnings`` (``ingest.<key>``), never a refusal: one forward
    key in the shared config held the continuous ingest in a restart loop for 23 h
    (2026-11-07). So is a retired setting (:data:`RETIRED`) asking for a build
    (``ingest.<key>_retired``): captures go on and nothing is built. A non-boolean
    ``graph_notifications`` ("true"/"false" strings are accepted) is off, named in
    ``warnings`` (``ingest.graph_notifications_invalid``). Any other known key with a
    bad value is still refused."""
    if isinstance(value, Mapping) and warnings is not None:
        warnings.extend(f"ingest.{key}" for key in sorted(map(str, set(value) - INGEST_KEYS)))
        warnings.extend(f"ingest.{key}_retired" for key, kept in sorted(RETIRED.items())
                        if key in value and value[key] != kept)
    if not isinstance(value, Mapping) or value.get("mode") != MODE:
        raise IngestError("ingest_config_invalid")
    grace = value.get("grace_seconds", DEFAULT_GRACE_SECONDS)
    parallel = value.get("max_parallel")
    budget = value.get("cpu_budget")
    drain = value.get("drain_seconds", DEFAULT_DRAIN_SECONDS)
    notify = _tolerant_bool(value.get("graph_notifications", False),
                          "graph_notifications", warnings)
    if not _number(grace, 1, MAX_GRACE_SECONDS) or not _number(drain, 1, MAX_DRAIN_SECONDS) \
            or parallel is not None and (
            type(parallel) is not int or not 1 <= parallel <= MAX_PARALLEL) \
            or budget is not None and (type(budget) is not int or not 1 <= budget <= 256):
        raise IngestError("ingest_config_invalid")
    return {"mode": MODE, "grace_seconds": grace, "max_parallel": parallel,
            "cpu_budget": budget, "drain_seconds": drain, "graph_notifications": notify}


def budget_environ(settings: Mapping[str, Any], environ: Mapping[str, str]
                   ) -> Mapping[str, str]:
    """``environ`` with the configured extraction budget for the runner to hand out
    (``child_env``); unchanged when none is configured."""
    from integrations import cpu_budget
    if settings.get("cpu_budget") is None:
        return environ
    return {**environ, cpu_budget.TOTAL_ENV: str(settings["cpu_budget"])}


def max_parallel(settings: Mapping[str, Any], configured: int) -> int:
    """Children at once: the configured ``max_parallel``, else one per configured source,
    never more than :data:`MAX_PARALLEL` and never fewer than one."""
    return max(1, min(settings.get("max_parallel") or configured, MAX_PARALLEL))


def section_options(spec: SourceSpec, section: Any, base: Path,
                    defaults: Mapping[str, Any], warnings: list[str] | None = None,
                    name: str = "source") -> dict[str, Any]:
    """One source section, every known key one of ``spec.fields`` and of its kind (a path
    names a file, resolved against ``base``), the host's ``defaults`` filling a field it
    leaves out; a required field still missing refuses it (``source_config_invalid``). A
    key the CLI does not take is never passed to it: ignored and named in ``warnings``
    (``<name>.<key>``), so a forward key never stops the ingest host. A capture unit the
    CLI reads by default that the section's own list leaves out (``spec.unlisted``) is
    named there too (``<name>.<key>:unlisted:<unit>``)."""
    # ``min_interval_seconds``: the continuous mode's cadence and
    # ``freshness_target_seconds`` its freshness-first order (``Continuous``), every
    # source's; never passed to its CLI.
    fields = {**spec.fields, "min_interval_seconds": "seconds",
              "freshness_target_seconds": "seconds"}
    if not isinstance(section, Mapping):
        raise IngestError("source_config_invalid")
    unknown = sorted(map(str, set(section) - set(fields)))
    if warnings is not None:
        warnings.extend(f"{name}.{key}" for key in unknown)
    options = {key: _option(fields[key], value, base) for key, value in section.items()
               if key in fields}
    for key, value in defaults.items():
        if key in spec.fields and key not in options and value is not None:
            options[key] = _option(spec.fields[key], str(value), base)
    if spec.required - set(options):
        raise IngestError("source_config_invalid")
    if warnings is not None and spec.unlisted is not None:
        warnings.extend(f"{name}.{unit}" for unit in spec.unlisted(options))
    return options


def split_mail_backlog(document: Mapping[str, Any], table: Mapping[str, SourceSpec],
                       warnings: list[str] | None = None) -> Mapping[str, Any]:
    """A config written before the mail backlog was its own source (its pass keys in the
    ``mail`` section): those keys become a ``mail_backlog`` section (its budget their
    seconds summed, its cadence the mail section's), so the passes go on running, off
    the capture path, and ``mail.backlog_keys_moved`` is warned. A ``mail_backlog``
    section already there wins (the mail section's pass keys are then only ignored, each
    warned). Nothing changes for a table without ``mail_backlog``."""
    mail = document.get("mail")
    if "mail_backlog" not in table or not isinstance(mail, Mapping) \
            or "mail_backlog" in document:
        return document
    moved = {key: mail[key] for key in _BACKLOG_FIELDS if key in mail}
    if not any(key in moved for key in BACKLOG_SECONDS):
        return document
    seconds = [moved[key] for key in BACKLOG_SECONDS if _number(moved.get(key), 0, 1e9)]
    section: dict[str, Any] = {**moved, "budget_seconds": sum(seconds) or None}
    for key in ("live_audience", "principal", "min_interval_seconds"):
        if key in mail:
            section[key] = mail[key]
    if section["budget_seconds"] is None:
        del section["budget_seconds"]   # refused below as the section's own error
    if warnings is not None:
        warnings.append("mail.backlog_keys_moved")
    return {**document, "mail": {k: v for k, v in mail.items() if k not in moved},
            "mail_backlog": section}


def ephemeral(path: Path) -> bool:
    """Under a temporary root the host throws away, both sides resolved (a link into /tmp,
    macOS /private/tmp): the system temporary directory, /tmp, /dev/shm or /run."""
    roots = {Path(tempfile.gettempdir()).resolve(), *(Path(r).resolve() for r in
                                                      EPHEMERAL_ROOTS)}
    resolved = {path, path.resolve()}
    return any(p == root or root in p.parents for p in resolved for root in roots)


def exclusion_refusal(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """The typed part every source is blocked on when the people-exclusion list does not
    load (checked before each capture starts); None when it loads."""
    exclusion = PeopleExclusion.from_env(environ)
    return None if not exclusion.unavailable else typed_block(
        {**exclusion.block_part("ingest.people_exclusion"), "missing_settings": [ENV_IDS]})


# A capture the people exclusion stopped (its list or its directory listing unreadable:
# every read fails closed). 2026-11-10..12: mail and Teams failed closed for two days as an
# untyped ``ExclusionUnavailable`` crash among other failures; it is its own state now.
EXCLUSION_UNAVAILABLE = "people_exclusion_unavailable"
_EXCLUSION_CODES = frozenset({"ExclusionUnavailable", EXCLUSION_UNAVAILABLE})


def _exclusion_code(value: Any) -> bool:
    value = code(value) or ""
    return value in _EXCLUSION_CODES or value.startswith("excluded_people_") \
        or value.startswith("people_exclusion:")


def surface_exclusion(spec: SourceSpec, result: dict[str, Any]) -> dict[str, Any]:
    """``result`` marked ``exclusion_unavailable`` when the people exclusion stopped the
    capture, however it said so (a crash naming ``ExclusionUnavailable``, a typed block
    of the guard, a reason ``people_exclusion:...``): a failure becomes ``blocked`` with
    one typed part (later_ask ``excluded-people-oids``), so the ledger, the run record
    (``SourceRuns``) and the ops alarm (``ops_health`` ``capture_exclusion``) name it at
    once. Nothing else changes: the capture read nothing it could not decide."""
    blocks = [b for b in result.get("blocked") or () if isinstance(b, Mapping)]
    if not (_exclusion_code(result.get("error_code"))
            or any(_exclusion_code(b.get("error_code")) for b in blocks)
            or any(_exclusion_code(r) for r in result.get("reasons") or ())):
        return result
    result = {**result, "exclusion_unavailable": True}
    if result.get("status") == "failed":
        part = typed_block({"part": f"{spec.name}.people_exclusion", "source": spec.source,
                            "error_code": EXCLUSION_UNAVAILABLE, "cause": "later_ask",
                            "later_ask": "excluded-people-oids"})
        result.update(status="blocked", blocked=[*blocks, part] if blocks else [part])
    return result


def _guarded_run(spec: SourceSpec, options: Mapping[str, Any], state: Path,
                 environ: Mapping[str, str], **kwargs: Any) -> dict[str, Any]:
    """:func:`run_source` on a worker thread: anything it raises is this source's
    ``failed`` result, never the run's; an exclusion stop is surfaced as its own state
    (:func:`surface_exclusion`)."""
    try:
        result = run_source(spec, options, state, environ, **kwargs)
    except Exception as exc:  # noqa: BLE001 - one source never stops the others
        result = {**_base(spec), "status": "failed",
                  "error_code": getattr(exc, "code", None) and code(exc.code)
                  or code(type(exc).__name__) or "error"}
    if result.get("error_code") == STOPPED:
        return result
    return surface_exclusion(spec, result)


def read_sources(table: Mapping[str, SourceSpec], sections: Mapping[str, Mapping[str, Any]],
                 state: Path, environ: Mapping[str, str], *, grace: float,
                 children: Any = None, cwd: Path = ROOT, parallel: int = 1,
                 on_outcome: Callable[[str, dict[str, Any]], Any] | None = None
                 ) -> dict[str, dict[str, Any]]:
    """One ingest-mode host cycle's reads: every configured source of ``table`` once,
    ``parallel`` children at a time (each source writes its own store), each outcome as
    the host ledger records it (:func:`ledger_outcome`), in ``table`` order. Refused
    before any capture when the people-exclusion list does not load: every configured
    source blocked. ``on_outcome(name, outcome)``: called with a source's outcome as its
    child ends, not when the slowest one does (the returned dict is that same object);
    a failure in it is skipped."""
    refusal = exclusion_refusal(environ)
    results: dict[str, Any] = {}
    run = [name for name in table if name in sections and refusal is None]
    cancel = threading.Event()   # set on the way out (SIGTERM): every child is stopped
    pool = ThreadPoolExecutor(max_workers=max(1, min(parallel, len(run) or 1)),
                              thread_name_prefix="ingest")
    ended: dict[str, dict[str, Any]] = {}

    def ended_with(name: str) -> Callable[[Future], None]:
        def done(future: Future) -> None:
            try:
                outcome = ledger_outcome(table[name], future.result(), state)
                ended[name] = outcome
                if on_outcome is not None:
                    on_outcome(name, outcome)
            except Exception:  # noqa: BLE001 - recorded with the rest at the end
                ended.pop(name, None)
        return done

    def submit(name: str) -> Future:
        future = pool.submit(_guarded_run, table[name], sections[name], state, environ,
                             grace=grace, cwd=cwd, children=children, cancel=cancel)
        future.add_done_callback(ended_with(name))
        return future

    # One writer: a source that gives way (``SourceSpec.yields_to``: the mail backlog to
    # mail) starts only once every source it gives way to has finished this cycle.
    yielding = {name for name in run if any(p in run for p in table[name].yields_to)}
    try:
        futures = {name: submit(name) for name in run if name not in yielding}
        for name in [n for n in run if n in yielding]:
            for peer in table[name].yields_to:
                if peer in futures:
                    wait([futures[peer]])
            futures[name] = submit(name)
        for name, spec in table.items():
            if name not in sections:
                results[name] = {**_base(spec), "status": "not_configured",
                                 "reasons": ["not_in_config"]}
            elif refusal is not None:
                results[name] = blocked_result(spec, [refusal])
            else:
                results[name] = futures[name].result()
    except BaseException:
        cancel.set()
        raise
    finally:
        pool.shutdown(wait=True)
    return {name: ended[name] if name in ended else ledger_outcome(table[name], result, state)
            for name, result in results.items()}


def _resolved(path: Path) -> str:
    return str(Path(path).expanduser().resolve())


class Continuous:
    """The continuous ingest mode: every configured source on its own cadence, at most
    ``parallel`` capture children at once. A source runs again ``min_interval_seconds``
    (default :data:`DEFAULT_MIN_INTERVAL_SECONDS`) after it finished, never waiting for
    a slower one; a failed or blocked one waits twice as long per failure in a row (at
    most :data:`MAX_RETRY_SECONDS`). Finished sources are handed to ``refresh`` (the host
    records them) on this thread; outcomes that finish during one refresh go together into
    the next. No capture waits for a refresh or another capture: nothing is built. The
    people-exclusion list is checked before each capture starts (unloaded: that source is
    ``blocked``, no child).

    ``should_stop()`` is asked every :data:`POLL_SECONDS`: a reason (the stop file, low
    disk, a signal) stops new captures and drains the running children (``run_source``
    ``drain``: SIGTERM, then ``drain_seconds`` for each to end its run at a safe point,
    its cursor kept and its outcome recorded; one still running then is killed, resumes
    from its cursor next time and is never recorded as a failure), then the outcomes
    finished so far are refreshed (``refresh(outcomes, reason)``) and
    :meth:`run` returns. A restart therefore never needs a gap between captures.

    Freshness first: the due sources start in order of staleness against their target
    (:meth:`target`: since the start of their last run that captured), those without a
    target (heavy walks and passes) after them; while a source with a target is not
    running, one slot stays free for it (``parallel`` above one), and one past its target
    with no free slot drains the latest heavy run for its slot. A source that gives way
    (``SourceSpec.yields_to``: the mail backlog to mail) never runs beside the source it
    gives way to, never starts while that one is due, and is drained once it is due.
    ``max_refreshes`` (tests, ``serve --max-cycles``): stop after that many refreshes, a
    hard cap: outcomes finished after the last one are not refreshed on the way out (they
    are in ``SourceRuns``; the summary's ``unrefreshed`` names their sources).

    ``on_outcome(name, outcome)``: called with each source's ledger outcome the moment its
    child ends, before any refresh (the host records it then); the same dict is later in
    the refresh's batch. A failure in it is skipped: the refresh records the outcome as it
    always did.

    ``gap`` (the host's ``CycleGap``; ``integrations.cycle_gap``): asked with ``wanted()``
    every :data:`POLL_SECONDS`; while a maintenance step wants the cycle lock, no capture
    starts and the running ones drain as on a stop; once none runs and their outcomes are
    refreshed, ``give()`` lets go of the lock until the step is done and takes it back
    (it returns early on a stop), then the sources start again as they fall due. No
    restart: the run goes on (``gaps`` in the summary counts them).

    ``wakes`` (``integrations.graph_notifications.Wakes``, only with the ingest setting
    ``graph_notifications``; None: nothing below happens): a wake newer than a source's
    last start makes that source due now, or ``WAKE_MIN_GAP_SECONDS`` after its last
    start, whichever is later. A wake that came before the source's current or latest run
    started is covered by that run; a source in failure backoff keeps its backoff; a
    running source keeps the wake until it ends. Its own capture runs as on its cadence
    (``woken`` in the summary counts them)."""

    def __init__(self, table: Mapping[str, SourceSpec],
                 sections: Mapping[str, Mapping[str, Any]], state: Path,
                 environ: Mapping[str, str], *, grace: float, parallel: int,
                 refresh: Callable[[dict[str, dict[str, Any]], str | None], Any],
                 should_stop: Callable[[], str | None], children: Any = None,
                 cwd: Path = ROOT, max_refreshes: int = 0,
                 monotonic: Callable[[], float] = time.monotonic,
                 wait_idle: Callable[[float], Any] = time.sleep,
                 drain_seconds: float = DEFAULT_DRAIN_SECONDS,
                 on_outcome: Callable[[str, dict[str, Any]], Any] | None = None,
                 gap: Any = None, wakes: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.on_outcome, self.gap = on_outcome, gap
        self.wakes, self.clock = wakes, clock
        self.table, self.state, self.environ = table, state, environ
        self.sections = {name: sections[name] for name in table if name in sections}
        self.grace, self.children, self.cwd = grace, children, cwd
        self.parallel = max(1, parallel)
        self.refresh, self.should_stop = refresh, should_stop
        self.max_refreshes, self.monotonic, self.wait_idle = (max_refreshes, monotonic,
                                                              wait_idle)
        self.drain_seconds = drain_seconds

    def _interval(self, name: str, failures: int) -> float:
        base = self.sections[name].get("min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS)
        return base if not failures else min(base * 2 ** failures, MAX_RETRY_SECONDS)

    def target(self, name: str) -> float | None:
        """The source's freshness target in seconds (``freshness_target_seconds`` of its
        section, else :data:`DEFAULT_FRESHNESS_TARGETS`), None for none."""
        value = self.sections[name].get("freshness_target_seconds",
                                        DEFAULT_FRESHNESS_TARGETS.get(name))
        return float(value) if value else None

    def _gives_way(self, name: str) -> tuple[str, ...]:
        return tuple(n for n in self.table[name].yields_to if n in self.sections)

    def run(self) -> dict[str, Any]:
        start = self.monotonic()
        due = dict.fromkeys(self.sections, start)
        failures = dict.fromkeys(self.sections, 0)
        runs = dict.fromkeys(self.sections, 0)
        running: dict[str, Future] = {}
        drains: dict[str, threading.Event] = {}   # per running child: drain it
        started_at: dict[str, float] = {}
        fresh_at: dict[str, float | None] = dict.fromkeys(self.sections)
        preempted = dict.fromkeys(self.sections, 0)
        overran: list[str] = []
        pending: dict[str, dict[str, Any]] = {}
        stopped: list[str] = []
        cancel = threading.Event()
        reason, refreshes = None, 0
        gapping, gaps = False, 0
        begun_wall: dict[str, float] = {}       # wall time of each source's latest start
        last_start: dict[str, float] = {}       # its monotonic time
        wake_seen: dict[str, float] = {}         # the newest wake already acted on
        woken = dict.fromkeys(self.sections, 0)
        pool = ThreadPoolExecutor(max_workers=self.parallel, thread_name_prefix="ingest")
        outcomes = SourceRuns(self.state)
        targets = {n: self.target(n) for n in self.sections}
        # every source that gives way, by the sources it gives way to
        yielders = {n: self._gives_way(n) for n in self.sections if self._gives_way(n)}

        def staleness(name: str, now: float) -> float:
            return float("inf") if fresh_at[name] is None else now - fresh_at[name]

        def overdue(name: str, now: float) -> bool:
            return targets[name] is not None and staleness(name, now) >= targets[name]

        def capped() -> bool:
            """``max_refreshes`` refreshes ran: no other starts, not even the stop's own
            (the outcomes it would refresh stay in ``SourceRuns``; ``unrefreshed``)."""
            return bool(self.max_refreshes) and refreshes >= self.max_refreshes

        def finished(name: str, result: Mapping[str, Any]) -> None:
            drains.pop(name, None)
            began = started_at.pop(name, None)
            if result.get("drain_overran"):
                overran.append(name)
            if result.get("error_code") == STOPPED:
                stopped.append(name)
                return
            outcomes.record(name, result)
            runs[name] += 1
            if result.get("status") in ("complete", "partial"):
                failures[name] = 0
                fresh_at[name] = began if began is not None else self.monotonic()
            else:
                failures[name] += 1
            due[name] = self.monotonic() + self._interval(name, failures[name])
            pending[name] = ledger_outcome(self.table[name], result, self.state)
            if self.on_outcome is not None:
                try:
                    self.on_outcome(name, pending[name])
                except Exception:  # noqa: BLE001 - the refresh records it as before
                    pass

        def order(now: float) -> list[str]:
            # Freshness first: sources with a target, the stalest against it first; then
            # the rest (heavy passes, long walks) in the order they fell due.
            ready = [n for n in self.sections if n not in running and due[n] <= now]
            return sorted(ready, key=lambda n: (0, -staleness(n, now) / targets[n], 0.0)
                          if targets[n] is not None else (1, 0.0, due[n]))

        def blocked_by_peer(name: str, now: float) -> bool:
            # One writer: a source never runs beside one it gives way to, and gives way
            # to one that is due; the one due waits for the yielder to drain.
            if any(n in running or due[n] <= now for n in yielders.get(name, ())):
                return True
            return any(name in peers and n in running for n, peers in yielders.items())

        def take_wakes(now: float) -> None:
            from integrations.graph_notifications import WAKE_MIN_GAP_SECONDS
            for name, at in self.wakes.latest(self.sections).items():
                if name in running or at <= wake_seen.get(name, float("-inf")):
                    continue   # a running source keeps its wake until it ends
                wake_seen[name] = at
                if at <= begun_wall.get(name, float("-inf")) or failures[name]:
                    continue   # covered by the latest run; or keep the failure backoff
                since = now - last_start.get(name, float("-inf"))
                soon = now + max(0.0, WAKE_MIN_GAP_SECONDS - since)
                if soon < due[name]:
                    due[name] = soon
                    woken[name] += 1

        def start(name: str) -> None:
            drains[name] = threading.Event()
            started_at[name] = last_start[name] = self.monotonic()
            begun_wall[name] = self.clock()
            running[name] = pool.submit(
                _guarded_run, self.table[name], self.sections[name], self.state,
                self.environ, grace=self.grace, cwd=self.cwd, children=self.children,
                cancel=cancel, drain=drains[name], drain_seconds=self.drain_seconds)

        def drain(name: str, why: str) -> None:
            event = drains.get(name)
            if event is not None and not event.is_set():
                event.set()
                if why == "preempted":
                    preempted[name] += 1

        try:
            while True:
                reason = reason or self.should_stop()
                if reason is not None:
                    # Drain, never kill: each child ends its run at a safe point, cursors
                    # kept, within ``drain_seconds`` (a restart needs no gap).
                    for name in list(running):
                        drain(name, "stopping")
                elif gapping or (self.gap is not None and self.gap.wanted()):
                    # A maintenance step wants the cycle lock: nothing starts, the
                    # running captures drain (cursors kept, outcomes recorded).
                    gapping = True
                    for name in list(running):
                        drain(name, "gap")
                else:
                    now = self.monotonic()
                    if self.wakes is not None:
                        take_wakes(now)
                    # a source due drains every running source that gives way to it
                    for name, peers in yielders.items():
                        if name in running and any(due[p] <= now and p not in running
                                                   for p in peers):
                            drain(name, "preempted")
                    targeted = [n for n in self.sections if targets[n] is not None]
                    for name in order(now):
                        if blocked_by_peer(name, now):
                            continue
                        reserve = min(sum(1 for n in targeted if n not in running),
                                      self.parallel - 1)
                        if targets[name] is None and len(running) + 1 + reserve > self.parallel:
                            continue   # the last slots stay for the fresh sources
                        if len(running) >= self.parallel:
                            if overdue(name, now):
                                # no slot for a source past its target: the latest
                                # heavy run started gives its slot back (drained)
                                heavy = sorted((n for n in running if targets[n] is None
                                                and not drains[n].is_set()),
                                               key=lambda n: started_at.get(n, 0.0))
                                if heavy:
                                    drain(heavy[-1], "preempted")
                            continue
                        refusal = exclusion_refusal(self.environ)
                        if refusal is not None:
                            finished(name, surface_exclusion(self.table[name], blocked_result(
                                self.table[name], [refusal])))
                            continue
                        start(name)
                if running:
                    wait(list(running.values()), timeout=POLL_SECONDS,
                         return_when=FIRST_COMPLETED)
                elif reason is None and not pending and not gapping:
                    nearest = min(due.values()) - self.monotonic()
                    self.wait_idle(max(0.01, min(POLL_SECONDS, nearest)))
                for name in [n for n, f in running.items() if f.done()]:
                    finished(name, running.pop(name).result())
                if pending and not capped():
                    batch, pending = pending, {}
                    self.refresh(batch, reason)
                    refreshes += 1
                    if self.max_refreshes and refreshes >= self.max_refreshes:
                        reason = reason or "max_refreshes"
                if reason is not None and not running and (not pending or capped()):
                    break
                if gapping and reason is None and not running and not pending:
                    held = self.gap.give()   # back once the step let go of the lock
                    gapping, gaps = False, gaps + 1
                    if not held:
                        # A stop came in the gap and the cycle lock was not taken back:
                        # the run ends here, so no capture ever starts without the lock
                        # beside the step that holds it (even if the stop then clears).
                        reason = self.should_stop() or GAP_LOCK_NOT_HELD
        finally:
            cancel.set()
            pool.shutdown(wait=True)   # every child stopped and reaped before returning
        return {"state": "stopped", "reason": reason, "refreshes": refreshes,
                "runs": {name: runs[name] for name in self.sections},
                "stopped_sources": sorted(set(stopped)),
                "preempted": {n: c for n, c in preempted.items() if c},
                "drain_overran": sorted(set(overran)), "unrefreshed": sorted(pending),
                "gaps": gaps, **({"woken": {n: c for n, c in woken.items() if c}}
                                 if self.wakes is not None else {})}


# Each source's run outcomes as they finish (``SourceRuns``), read by the ops alarms
# (``operating.ops_health.capture_run_checks``) without waiting for the next refresh.
RUNS_FILE = "source-runs.json"   # under <state>/host
RUNS_SCHEMA = "plx-source-runs/v1"
_SUCCESS = ("complete", "partial")


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SourceRuns:
    """``<state>/host/source-runs.json``: per source, its last run (when, status, error
    code), the last successful run, and the failures in a row with when they began and
    why. Written as each capture child finishes (the host ledger records an outcome only
    at the next refresh, which can wait hours behind a build), kept across restarts, so a
    source failing run after run is an alarm within one ops pass. Codes only, never a
    message. A write that fails is skipped: it never stops the ingest."""

    def __init__(self, state: Path, now: Callable[[], str] = _utc_now) -> None:
        self.path = Path(state) / "host" / RUNS_FILE
        self.now = now
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
            sources = held.get("sources") if isinstance(held, dict) \
                and held.get("schema") == RUNS_SCHEMA else None
        except (OSError, ValueError):
            sources = None
        self.sources: dict[str, dict[str, Any]] = {
            str(k): dict(v) for k, v in (sources or {}).items() if isinstance(v, dict)}

    def record(self, name: str, result: Mapping[str, Any]) -> None:
        status = code(result.get("status")) or "failed"
        reason = (code(result.get("error_code"))
                  or next((code(r) for r in result.get("reasons") or () if code(r)), None)
                  or next((code(b.get("error_code")) for b in result.get("blocked") or ()
                           if isinstance(b, Mapping) and code(b.get("error_code"))), None)
                  or status)
        at = self.now()
        row = self.sources.setdefault(name, {})
        row.update(last_run_at=at, last_status=status, last_error=None
                   if status in _SUCCESS else reason)
        if result.get("exclusion_unavailable"):
            # the people exclusion stopped it: its own state (ops_health
            # ``capture_exclusion``), whatever the run's status
            row["exclusion_unavailable_since"] = row.get("exclusion_unavailable_since") or at
            row["exclusion_unavailable_runs"] = int(
                row.get("exclusion_unavailable_runs") or 0) + 1
        else:
            row.update(exclusion_unavailable_since=None, exclusion_unavailable_runs=0)
        if status in _SUCCESS:
            row.update(last_success_at=at, consecutive_failures=0, failing_since=None,
                       failing_reason=None, reason_since=None)
        else:
            failures = int(row.get("consecutive_failures") or 0)
            row["consecutive_failures"] = failures + 1
            if not failures:
                row["failing_since"] = at
            if row.get("failing_reason") != reason:
                row.update(failing_reason=reason, reason_since=at)
        self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_private(self.path, json.dumps(
                {"schema": RUNS_SCHEMA, "sources": self.sources}, indent=1,
                sort_keys=True).encode("utf-8"))
        except OSError:
            pass


def cycle_checks(reports: Sequence[Mapping[str, Any]], configured: Sequence[str],
                 table: Mapping[str, SourceSpec], state: Path, environ: Mapping[str, str], *,
                 internal_domains: Sequence[str] = ()) -> dict[str, Any]:
    """What the host's ``check`` adds in ingest mode over its cycle ``reports``: the
    resume counters (:func:`evaluate`), per projected source the items its store holds
    twice after the last cycle (:func:`duplicates`), and leaked secrets, excluded IDs, the
    state path, addresses, GUIDs or URLs in the cycle reports (a count). ``blocked``: one
    typed part when no completed cursor source was there to check (nothing was proved)."""
    cycles = [{"sources": [o["capture"] for o in r["sources"].values() if "capture" in o]}
              for r in reports]
    checks = evaluate(cycles, list(configured))
    found = {name: duplicates(state, table[name], table, internal_domains=internal_domains)
             for name in configured}
    captured = {r["name"] for c in cycles for r in c["sources"]
                if r.get("status") in ("complete", "partial")}
    checks["duplicate_items"] = sum(d.get("duplicate_items", 0) for d in found.values())
    checks["duplicates_unchecked"] = sum(
        1 for name, d in found.items() if name in captured
        and d["state"] not in ("checked", "not_projected"))
    checks["leaked"] = leaks(json.dumps(list(reports), sort_keys=True),
                             _secret_tokens(environ, state))
    blocked = [] if checks["resume_checked"] else [typed_block({
        "part": "ingest.resume", "source": "workbench-host",
        "error_code": "no_completed_source_to_resume", "cause": "capture_gap"})]
    return {"checks": checks, "duplicates": found, "blocked": blocked}


__all__ = ["CAUSES", "EXCLUSION_UNAVAILABLE", "MODE", "SOURCES", "split_mail_backlog",
           "surface_exclusion", "STATUSES", "Continuous", "IngestError",
           "SourceSpec", "cycle_checks", "duplicates", "ephemeral", "evaluate",
           "exclusion_refusal", "ingest_settings", "ledger_outcome", "leaks", "max_parallel",
           "read_sources", "run_source", "section_options",
           "summarize_bc", "summarize_lists", "summarize_mail", "summarize_mc",
           "summarize_meetings", "summarize_portal", "summarize_sharepoint", "summarize_teams"]
