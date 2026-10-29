"""The host's ingest mode: every connected source's own capture CLI, as one host cycle.

Always be ingesting (owner directive 2026-10-21) is a mode of ``operating.host``, never a
runner of its own (decision 2026-10-21-ingest-one-writer): a host config with
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
``portal``, ``bc``, ``mail``, ``sharepoint``, ``sharepoint_lists``, ``teams``, ``mc``),
each key checked against its CLI's fields (:func:`section_options`; a path must name a
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
records it and builds the next projection generation, serially, while the other children
go on reading; it stops on the stop file, low disk or a signal. A build holds one read
snapshot of every store it opens until it ends, and no checkpoint can reset a WAL past
that snapshot: a capture writing such a store meanwhile grows its WAL by gigabytes
(live 2026-10-27, the Portal mirror under Sage lazy fills, 4-6 GB per overlap). So a
capture whose store the next build reads (``SourceSpec.writes`` against the host's
``build_reads``) never runs beside a build: a build waits for every such running capture
to finish (none of them starts meanwhile), the host checkpoints their stores (TRUNCATE),
then builds; captures of stores the build does not read go on in parallel. The wait is
in the cycle report (``build_exclusion``).

Resume kinds: ``cursor`` sources (Portal, BC, mail, SharePoint, Teams) continue from a
cursor after a completed cycle; SharePoint lists keep a cursor only for an interrupted
run (``interrupted_only``) and Mission Control is a snapshot (``snapshot``), so both
re-read in full after a completed cycle, and the host's ``check``
(:func:`cycle_checks`) reports them as ``full_reread_sources`` rather than as resumed.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
from pathlib import Path
import re
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
MAX_PARALLEL = 7   # capture children at once (``ingest.max_parallel``; default: one each)
# Continuous mode (``Continuous``): a source that finished runs again this long after
# (``min_interval_seconds`` of its section); a failed or blocked one waits twice as long
# per failure in a row, at most ``MAX_RETRY_SECONDS``.
DEFAULT_MIN_INTERVAL_SECONDS, MAX_RETRY_SECONDS = 120, 3600
STOPPED = "ingest_stopped"   # a child stopped by the stop file, the disk guard or a signal
POLL_SECONDS = 0.5
EPHEMERAL_ROOTS = ("/tmp", "/dev/shm", "/run")
STATUSES = ("complete", "partial", "blocked", "failed", "not_configured")
# devloop/live_verify.py CAUSES: what a typed block may name as its cause.
CAUSES = frozenset({"upstream_outage", "projection_timeout", "missing_bindings",
                    "host_misconfiguration", "capture_gap", "relation_unavailable",
                    "source_gap", "later_ask"})
RESUME_KINDS = ("cursor", "interrupted_only", "snapshot")
# A child never holds another source's secret: names shaped like a credential are passed
# only to the source that declares them (the shape devloop/live_verify.py hides).
CREDENTIAL_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|COOKIE|DSN|CREDENTIAL|"
                             r"SESSION|^GIT_CONFIG_", re.IGNORECASE)
GRAPH_ENV = ("MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_GRAPH_CLIENT_ID",
             "MICROSOFT_GRAPH_CLIENT_SECRET")
BC_ENV = ("BC_TENANT_ID", "BC_CLIENT_ID", "BC_CLIENT_SECRET", "BC_ENVIRONMENT")
PORTAL_ENV = ("PLX_WORKBENCH_API_URL", "PLX_WORKBENCH_API_KEY")
MC_ENV = ("MC_MCP_API_KEY",)
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
    fields: Mapping[str, str]           # config key -> kind (path, str, strs, seconds, int)
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
    # Every SQLite store file this capture writes (None: none the host knows of). A
    # capture writing a store the next build reads never overlaps that build
    # (:class:`Continuous`).
    writes: Callable[[Path], tuple[Path, ...]] | None = None


def _python() -> str:
    return sys.executable or "python3"


def _private_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _budget(options: Mapping[str, Any]) -> list[str]:
    value = options.get("budget_seconds")
    return [] if value is None else ["--budget-seconds", str(value)]


def _graph_argv(command: str) -> Callable[[Path, Mapping[str, Any]], list[str]]:
    def argv(state: Path, options: Mapping[str, Any]) -> list[str]:
        args = [_python(), "-m", "integrations.graph_read", command, "--state-dir", str(state),
                "--live-audience", options["live_audience"], "--runs", "1"]
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
    return args + _budget(options)


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
    return _units(units, report)


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
    return _units(units, report, excluded=len(rows) - len(kept))


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
    return _units(units, report)


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
                "clearance": "str", "tenant": "str", "budget_seconds": "seconds"},
               frozenset({"audience"}), PORTAL_ENV, projected="portal_api",
               served_store=portal_mirror, writes=lambda state: (portal_mirror(state),)),
    SourceSpec("bc", "business-central", _bc_argv, summarize_bc,
               {"entity_sets": "strs", "page_size": "int", "budget_seconds": "seconds",
                "live_audience": "path", "principal": "str"},
               frozenset({"live_audience"}), BC_ENV, projected="bc",
               writes=_state_store("bc")),
    SourceSpec("mail", "microsoft-graph", _graph_argv("mail-capture"), summarize_mail,
               {**_GRAPH_FIELDS, "page_size": "int", "principal": "str"},
               frozenset({"live_audience"}),
               GRAPH_ENV, projected="graph_mail", writes=_state_store("graph_mail")),
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
                                 "timeout_seconds": "seconds"},
               frozenset({"live_audience"}), GRAPH_ENV, "interrupted_only", None,
               projected="sharepoint", relations=frozenset({"listItems"}),
               writes=_sharepoint_store(1)),
    SourceSpec("teams", "microsoft-graph", _graph_argv("teams-capture"), summarize_teams,
               dict(_GRAPH_FIELDS), frozenset({"live_audience"}), GRAPH_ENV,
               projected="teams", writes=_state_store("teams")),
    SourceSpec("mc", "mission-control-mcp", _mc_argv, summarize_mc,
               {"scope": "str", "queries": "strs", "max_task_reads": "int",
                "operator_email_default": "str", "timeout_seconds": "seconds",
                "budget_seconds": "seconds", "live_audience": "path", "principal": "str"},
               frozenset({"scope", "live_audience"}), MC_ENV, "snapshot", None,
               projected="mission_control", writes=_state_store("mission_control")),
)}
# Capture CLIs on main this runner does not call: capture-breadth repeats the mail and
# SharePoint captures above into the same stores; the brain capture
# (integrations.knowledge_source) takes only a fresh directory, never a kept one.
NOT_RUN = {"capture_breadth": "duplicates_mail_and_sharepoint",
           "knowledge_source": "fresh_state_dir_only"}



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
    if kind == "flag" and type(value) is bool:
        return value
    if kind == "str" and _string(value):
        return value
    if kind == "strs" and isinstance(value, list) and value and len(value) <= 1000 \
            and all(_string(v) for v in value):
        return list(value)
    if kind == "path" and _string(value):
        path = (base / value).resolve()
        if path.is_file():
            return str(path)
    raise IngestError("source_config_invalid")


# ------------------------------------------------------------------ one source

def child_env(environ: Mapping[str, str], spec: SourceSpec, state: Path) -> dict[str, str]:
    env = {name: value for name, value in environ.items()
           if not CREDENTIAL_NAME.search(name) or name in spec.env}
    env[STATE_ENV] = str(state)
    return env


def _stderr_code(stderr: bytes) -> tuple[str | None, int | None]:
    """The error class/code and HTTP status of the last JSON line on stderr; never its
    message."""
    for line in reversed(stderr.decode("utf-8", "replace").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, Mapping):
            status = value.get("http_status")
            return (code(value.get("error_code")) or code(value.get("error"))
                    or code(value.get("error_class")),
                    status if type(status) is int and 100 <= status <= 599 else None)
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
    if total and complete == total and not blocked:
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


def run_source(spec: SourceSpec, options: Mapping[str, Any], state: Path,
               environ: Mapping[str, str], *, grace: float, cwd: Path = ROOT,
               children: Any = None, cancel: threading.Event | None = None
               ) -> dict[str, Any]:
    """One capture CLI as a child process; every outcome is this source's typed result.
    ``children`` (``operating.host.CycleChildren``): the child is recorded there while it
    runs, so the next cycle stops it if this runner is killed first. ``cancel``: once set,
    the child is stopped (its process group killed) and the result is ``failed`` with
    :data:`STOPPED`, which a continuous run never records (the capture resumes from its
    own cursor next time)."""
    missing = [name for name in spec.env if not environ.get(name)]
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
                if time.monotonic() >= deadline:
                    return {**result, "status": "failed", "error_code": "child_timeout",
                            "seconds": round(time.monotonic() - started, 1)}
    finally:
        child_process.stop(child)
        for pipe in (child.stdout, child.stderr):
            pipe.close()
        if children is not None:
            children.remove(child.pid)
    result.update(exit_code=child.returncode, seconds=round(time.monotonic() - started, 1))
    try:
        report = json.loads(stdout.decode("utf-8"))
    except ValueError:
        report = None
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

def ingest_settings(value: Any) -> dict[str, Any]:
    """The host config's ``ingest`` section: ``mode`` :data:`MODE`, ``grace_seconds``
    (the time a child gets past its budget) and ``max_parallel`` (capture children at
    once, 1 to :data:`MAX_PARALLEL`; absent: one per configured source, at most
    :data:`MAX_PARALLEL`); anything else is refused."""
    if not isinstance(value, Mapping) or set(value) - {"mode", "grace_seconds",
                                                       "max_parallel"} \
            or value.get("mode") != MODE:
        raise IngestError("ingest_config_invalid")
    grace = value.get("grace_seconds", DEFAULT_GRACE_SECONDS)
    parallel = value.get("max_parallel")
    if not _number(grace, 1, MAX_GRACE_SECONDS) or parallel is not None and (
            type(parallel) is not int or not 1 <= parallel <= MAX_PARALLEL):
        raise IngestError("ingest_config_invalid")
    return {"mode": MODE, "grace_seconds": grace, "max_parallel": parallel}


def max_parallel(settings: Mapping[str, Any], configured: int) -> int:
    """Children at once: the configured ``max_parallel``, else one per configured source,
    never more than :data:`MAX_PARALLEL` and never fewer than one."""
    return max(1, min(settings.get("max_parallel") or configured, MAX_PARALLEL))


def section_options(spec: SourceSpec, section: Any, base: Path,
                    defaults: Mapping[str, Any]) -> dict[str, Any]:
    """One source section, every key one of ``spec.fields`` and of its kind (a path names
    a file, resolved against ``base``), the host's ``defaults`` filling a field it leaves
    out; a required field still missing refuses it (``source_config_invalid``)."""
    # ``min_interval_seconds``: the continuous mode's cadence (``Continuous``), every
    # source's; never passed to its CLI.
    fields = {**spec.fields, "min_interval_seconds": "seconds"}
    if not isinstance(section, Mapping) or set(section) - set(fields):
        raise IngestError("source_config_invalid")
    options = {key: _option(fields[key], value, base) for key, value in section.items()}
    for key, value in defaults.items():
        if key in spec.fields and key not in options and value is not None:
            options[key] = _option(spec.fields[key], str(value), base)
    if spec.required - set(options):
        raise IngestError("source_config_invalid")
    return options


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


def _guarded_run(spec: SourceSpec, options: Mapping[str, Any], state: Path,
                 environ: Mapping[str, str], **kwargs: Any) -> dict[str, Any]:
    """:func:`run_source` on a worker thread: anything it raises is this source's
    ``failed`` result, never the run's."""
    try:
        return run_source(spec, options, state, environ, **kwargs)
    except Exception as exc:  # noqa: BLE001 - one source never stops the others
        return {**_base(spec), "status": "failed",
                "error_code": getattr(exc, "code", None) and code(exc.code)
                or code(type(exc).__name__) or "error"}


def read_sources(table: Mapping[str, SourceSpec], sections: Mapping[str, Mapping[str, Any]],
                 state: Path, environ: Mapping[str, str], *, grace: float,
                 children: Any = None, cwd: Path = ROOT, parallel: int = 1
                 ) -> dict[str, dict[str, Any]]:
    """One ingest-mode host cycle's reads: every configured source of ``table`` once,
    ``parallel`` children at a time (each source writes its own store), each outcome as
    the host ledger records it (:func:`ledger_outcome`), in ``table`` order. Refused
    before any capture when the people-exclusion list does not load: every configured
    source blocked."""
    refusal = exclusion_refusal(environ)
    results: dict[str, Any] = {}
    run = [name for name in table if name in sections and refusal is None]
    cancel = threading.Event()   # set on the way out (SIGTERM): every child is stopped
    pool = ThreadPoolExecutor(max_workers=max(1, min(parallel, len(run) or 1)),
                              thread_name_prefix="ingest")
    try:
        futures = {name: pool.submit(_guarded_run, table[name], sections[name], state,
                                     environ, grace=grace, cwd=cwd, children=children,
                                     cancel=cancel)
                   for name in run}
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
    return {name: ledger_outcome(table[name], result, state)
            for name, result in results.items()}


def _resolved(path: Path) -> str:
    return str(Path(path).expanduser().resolve())


class Continuous:
    """The continuous ingest mode: every configured source on its own cadence, at most
    ``parallel`` capture children at once. A source runs again ``min_interval_seconds``
    (default :data:`DEFAULT_MIN_INTERVAL_SECONDS`) after it finished, never waiting for
    a slower one; a failed or blocked one waits twice as long per failure in a row (at
    most :data:`MAX_RETRY_SECONDS`). Each finished source is handed to ``refresh`` (the
    host records it and builds the next projection generation) on this thread, so
    records and builds are serial while the other children keep reading; outcomes that
    finish during one refresh go together into the next. The people-exclusion list is
    checked before each capture starts (unloaded: that source is ``blocked``, no child).

    Build exclusion: a source is *exclusive* when a store it writes
    (``SourceSpec.writes``) is one ``build_reads()`` names (every store file the next
    build may open). A refresh never starts while an exclusive capture runs: it waits
    for each to finish, no exclusive capture starts meanwhile (the others still do), and
    since captures start only on this thread none starts during the refresh either. The
    refresh gets ``exclusion``: ``exclusive`` (the batch's exclusive sources, whose
    stores the host checkpoints before it builds), ``waited_for``, ``wait_seconds`` and
    ``held_back`` (due sources not started because a build waited).

    ``should_stop()`` is asked every :data:`POLL_SECONDS`: a reason (the stop file, low
    disk, a signal) stops new captures and the running children (their process groups
    killed; each capture resumes from its own cursor next time, and a stopped run is
    never recorded as a failure), then the outcomes finished so far are refreshed
    (``refresh(outcomes, reason, exclusion)``) and :meth:`run` returns.
    ``max_refreshes`` (tests, ``serve --max-cycles``): stop after that many refreshes."""

    def __init__(self, table: Mapping[str, SourceSpec],
                 sections: Mapping[str, Mapping[str, Any]], state: Path,
                 environ: Mapping[str, str], *, grace: float, parallel: int,
                 refresh: Callable[[dict[str, dict[str, Any]], str | None,
                                    dict[str, Any]], Any],
                 should_stop: Callable[[], str | None], children: Any = None,
                 cwd: Path = ROOT, max_refreshes: int = 0,
                 monotonic: Callable[[], float] = time.monotonic,
                 wait_idle: Callable[[float], Any] = time.sleep,
                 build_reads: Callable[[], Sequence[Path]] = tuple) -> None:
        self.table, self.state, self.environ = table, state, environ
        self.sections = {name: sections[name] for name in table if name in sections}
        self.grace, self.children, self.cwd = grace, children, cwd
        self.parallel = max(1, parallel)
        self.refresh, self.should_stop = refresh, should_stop
        self.max_refreshes, self.monotonic, self.wait_idle = (max_refreshes, monotonic,
                                                              wait_idle)
        self.build_reads = build_reads

    def _interval(self, name: str, failures: int) -> float:
        base = self.sections[name].get("min_interval_seconds", DEFAULT_MIN_INTERVAL_SECONDS)
        return base if not failures else min(base * 2 ** failures, MAX_RETRY_SECONDS)

    def exclusive(self) -> set[str]:
        """The configured sources writing a store the next build reads. Asked afresh
        each time (a store the build reads may change between builds); a ``writes`` or
        ``build_reads`` that fails counts every source with a ``writes`` exclusive."""
        writers = {n: self.table[n].writes for n in self.sections if self.table[n].writes}
        try:
            reads = {_resolved(p) for p in self.build_reads()}
            return {n for n, writes in writers.items()
                    if any(_resolved(p) in reads for p in writes(self.state))}
        except Exception:  # noqa: BLE001 - unknown is exclusive, never an overlap
            return set(writers)

    def run(self) -> dict[str, Any]:
        start = self.monotonic()
        due = dict.fromkeys(self.sections, start)
        failures = dict.fromkeys(self.sections, 0)
        runs = dict.fromkeys(self.sections, 0)
        running: dict[str, Future] = {}
        pending: dict[str, dict[str, Any]] = {}
        stopped: list[str] = []
        cancel = threading.Event()
        reason, refreshes = None, 0
        waited_since: float | None = None
        waited_for: set[str] = set()
        held_back: set[str] = set()
        pool = ThreadPoolExecutor(max_workers=self.parallel, thread_name_prefix="ingest")

        def finished(name: str, result: Mapping[str, Any]) -> None:
            if result.get("error_code") == STOPPED:
                stopped.append(name)
                return
            runs[name] += 1
            failures[name] = 0 if result.get("status") in ("complete", "partial") \
                else failures[name] + 1
            due[name] = self.monotonic() + self._interval(name, failures[name])
            pending[name] = ledger_outcome(self.table[name], result, self.state)

        try:
            while True:
                reason = reason or self.should_stop()
                # A build waits (outcomes to refresh): no exclusive capture starts.
                exclusive = self.exclusive() if pending else set()
                if reason is not None:
                    cancel.set()
                else:
                    now = self.monotonic()
                    for name in sorted((n for n in self.sections if n not in running
                                        and due[n] <= now), key=lambda n: due[n]):
                        if pending and name in exclusive:
                            held_back.add(name)
                            continue
                        if len(running) >= self.parallel:
                            break
                        refusal = exclusion_refusal(self.environ)
                        if refusal is not None:
                            finished(name, blocked_result(self.table[name], [refusal]))
                            continue
                        running[name] = pool.submit(
                            _guarded_run, self.table[name], self.sections[name],
                            self.state, self.environ, grace=self.grace, cwd=self.cwd,
                            children=self.children, cancel=cancel)
                if running:
                    wait(list(running.values()), timeout=POLL_SECONDS,
                         return_when=FIRST_COMPLETED)
                elif reason is None and not pending:
                    nearest = min(due.values()) - self.monotonic()
                    self.wait_idle(max(0.01, min(POLL_SECONDS, nearest)))
                for name in [n for n, f in running.items() if f.done()]:
                    finished(name, running.pop(name).result())
                blocking = sorted(set(running) & self.exclusive()) if pending else []
                if blocking:
                    if waited_since is None:
                        waited_since = self.monotonic()
                    waited_for.update(blocking)
                elif pending:
                    batch, pending = pending, {}
                    exclusion = {
                        "exclusive": sorted(set(batch) & self.exclusive()),
                        "waited_for": sorted(waited_for),
                        "wait_seconds": 0.0 if waited_since is None
                        else round(self.monotonic() - waited_since, 3),
                        "held_back": sorted(held_back)}
                    waited_since, waited_for, held_back = None, set(), set()
                    self.refresh(batch, reason, exclusion)
                    refreshes += 1
                    if self.max_refreshes and refreshes >= self.max_refreshes:
                        reason = reason or "max_refreshes"
                if reason is not None and not running and not pending:
                    break
        finally:
            cancel.set()
            pool.shutdown(wait=True)   # every child stopped and reaped before returning
        return {"state": "stopped", "reason": reason, "refreshes": refreshes,
                "runs": {name: runs[name] for name in self.sections},
                "stopped_sources": sorted(set(stopped))}


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


__all__ = ["CAUSES", "MODE", "SOURCES", "STATUSES", "Continuous", "IngestError",
           "SourceSpec", "cycle_checks", "duplicates", "ephemeral", "evaluate",
           "exclusion_refusal", "ingest_settings", "ledger_outcome", "leaks", "max_parallel",
           "read_sources", "run_source", "section_options",
           "summarize_bc", "summarize_lists", "summarize_mail", "summarize_mc",
           "summarize_portal", "summarize_sharepoint", "summarize_teams"]
