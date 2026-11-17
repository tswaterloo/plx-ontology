"""Command-line helpers of the live case workspace: reader, ingest, host cycle, stderr causes
and replica-gap totals (``operating.case_workspace_live``, which re-exports them)."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Mapping
import warnings

from .case_workspace_live_model import OpenError, _PRINCIPAL


# ------------------------------------------------------------------ CLI


def _reader(text: str, tenant: str, clearance: str):
    from ontology.core import Actor

    principals = [part.strip() for part in str(text).split(",") if part.strip()]
    if not principals or any(not _PRINCIPAL.fullmatch(p) or "*" in p for p in principals):
        raise OpenError("reader_principals_invalid")
    return Actor(tenant, frozenset(principals), clearance)


def _ingest(args: argparse.Namespace, environ: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
    """Run ``operating.ingestion run`` into the state directory; counts-only result."""
    from . import ingestion

    argv = ["run", "--state-dir", str(args.state_dir), "--sources", str(args.ingest_sources),
            "--mirror-audience", str(args.mirror_audience), "--runs", str(args.ingest_runs)]
    if args.ingest_live_audience is not None:
        argv += ["--live-audience", str(args.ingest_live_audience)]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ingestion.main(argv, environ=environ)
    try:
        report = json.loads(out.getvalue() or "{}")
    except ValueError:
        report = {}
    if code != 0:
        try:
            error = json.loads(err.getvalue() or "{}").get("error")
        except ValueError:
            error = None
        return code, {"exit_code": code, "error": error if isinstance(error, str) else
                      "ingestion_failed"}
    status = report.get("status") or {}
    return 0, {"exit_code": 0, "runs": [run.get("summary") for run in report.get("runs") or []],
               "cursor_regressions": report.get("cursor_regressions"),
               "row_data_check": report.get("row_data_check"),
               "status_summary": status.get("summary"),
               # Per relation: rows read and why a unit is incomplete (cap reasons).
               "units": [{key: unit.get(key) for key in ("unit_id", "kind", "state",
                                                          "reasons", "counts")}
                         for unit in status.get("units") or []]}


def _host_cycle(args: argparse.Namespace, environ: Mapping[str, str],
                options: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """One capture cycle of the host into the state directory; counts-only result."""
    from .host import Host, HostConfig, HostError

    try:
        report = Host(args.state_dir, HostConfig.from_file(args.host_config),
                      environ=environ, **options).cycle()
    except HostError as error:
        return 1, {"exit_code": 1, "error": error.code}
    return 0, {"exit_code": 0, "state": report.get("state"),
               "sources": {name: {key: outcome.get(key) for key in (
                   "state", "complete", "counts", "reasons") if key in outcome}
                   for name, outcome in (report.get("sources") or {}).items()},
               "projection": {**{key: (report.get("projection") or {}).get(key)
                                 for key in ("state", "objects")},
                              "reference_alias": _reference_alias(report.get("projection")),
                              "reviews": _review_step(report.get("projection"))}}


def _reference_alias(projection: Mapping[str, Any] | None) -> dict[str, Any]:
    """The supplier-number alias join of the build (the acceptance bar): its typed outcome
    (``ontology.mentions.reference_alias_outcome``: ``alias_joined``, a pending lead, or
    why no candidate was found) and counts only, from the mention step's counts."""
    from ontology.mentions import reference_alias_outcome

    counts = ((projection or {}).get("steps") or {}).get("mentions") or {}
    return {"outcome": reference_alias_outcome(counts),
            **{key: counts.get(key) for key in (
                "reviewed_alias_links", "reviewed_alias_links_open", "alias_numbers",
                "alias_leads", "alias_no_candidate")}}


def _review_step(projection: Mapping[str, Any] | None) -> dict[str, int]:
    """The people's decisions the build projected (``ontology.reviews.link_step``'s
    counts: links, allocations and why a decision was not projected), counts only."""
    counts = ((projection or {}).get("steps") or {}).get("reviews") or {}
    return {key: value for key, value in counts.items()
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)}


def _host_books(path: Path | None) -> dict[str, dict[str, str]] | None:
    """The BC company books the host config's capture pinned (``bc.books``), so a live
    lookup pins the companies the capture read; None: the default map."""
    if path is None:
        return None
    from integrations.bc_read import load_company_books

    from .host import HostConfig

    books = (HostConfig.from_file(path).sources.get("bc") or {}).get("books")
    return None if books is None else load_company_books(books)


STDERR_CAUSES_MAX = 12
_CAUSE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,23}(?:\.[A-Za-z_][A-Za-z0-9_]{0,23})?")


class _StderrCauses(contextlib.AbstractContextManager):
    """Counts, by typed cause, the noise the run would otherwise write to stderr: pypdf's
    logging records on a malformed mailed PDF, which the reading keeps off stderr where
    pypdf reads (``document_understanding.pypdf_record_listener``), and ``warnings``
    messages, which it keeps off stderr: ``causes`` (``{"records": n, "by_cause":
    {"log:<logger, two name parts>:<LEVEL>" | "warning:<Category>": n}}``), never a
    message, which may carry a document's text. At most ``STDERR_CAUSES_MAX`` causes are
    named; the rest count as ``other``. Any other logging record, a traceback logged with
    ``exc_info`` included, reaches stderr unchanged (the live verifier reads its
    exception class), as does an exception escaping the run. Warnings are caught
    process-wide (``warnings.catch_warnings``: not thread-safe), so it wraps the CLI only."""

    def __init__(self, causes: dict[str, Any]):
        from integrations import document_understanding

        self.causes = causes
        causes.update(records=0, by_cause={})
        self._lock = threading.Lock()
        self._pypdf = document_understanding.pypdf_record_listener(lambda record: self._count(
            f"log:{_cause_name('.'.join(record.name.split('.')[:2]))}:"
            f"{_cause_name(record.levelname)}"))
        self._warnings = warnings.catch_warnings(record=True)

    def _count(self, cause: str) -> None:
        with self._lock:   # pypdf reads in the capture's worker threads too
            by_cause = self.causes["by_cause"]
            if cause not in by_cause and len(by_cause) >= STDERR_CAUSES_MAX:
                cause = "other"
            by_cause[cause] = by_cause.get(cause, 0) + 1
            self.causes["records"] += 1

    def __enter__(self) -> "_StderrCauses":
        self._pypdf.__enter__()
        self._caught = self._warnings.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.flush()
        self._warnings.__exit__(*exc)
        self._pypdf.__exit__(*exc)

    def flush(self) -> None:
        """Count the warnings caught so far (a summary printed mid-run shows them)."""
        while self._caught:
            self._count(f"warning:{_cause_name(self._caught.pop(0).category.__name__)}")


def _cause_name(text: str) -> str:
    return text if _CAUSE_NAME.fullmatch(text) else "other"


GAP_REASON = "not_in_served_replica"  # the check's blocked string (source_gap)
_GAP_ROOT_REASONS = re.compile(r"no_portal_row_for_\w+|not_in_served_replica")


def _months(db: sqlite3.Connection, object_type: str) -> dict[str, int]:
    """Held ``object_type`` objects by month (``YYYY-MM``) of their latest event, the
    pick's own ordering; ``undated`` for none."""
    from ontology.store import removed_at_sql

    out: dict[str, int] = {}
    for (when,) in db.execute(
            "SELECT MAX(COALESCE(e.time, e.window_end)) FROM objects o "
            "LEFT JOIN event_objects eo ON eo.object_id = o.object_id "
            "LEFT JOIN events e ON e.event_id = eo.event_id "
            f"WHERE o.object_type = ? AND NOT {removed_at_sql('o.object_id')} "
            "GROUP BY o.object_id", (object_type,)):
        key = str(when)[:7] if re.fullmatch(r"\d{4}-\d{2}.*", str(when)) else "undated"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _totals(counts: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for each in counts:
        for key, value in (each or {}).items():
            out[key] = out.get(key, 0) + int(value)
    return dict(sorted(out.items()))


def _direction_totals(picks: Any) -> dict[str, dict[str, Any]]:
    """The join picks' per-direction counts summed over the run (``_join_pick``
    ``directions``): numbers added, ``joined`` / ``tried`` / ``budget_spent`` /
    ``bc.reader`` as how many picks, reasons and lookup states per key (``reverse.bc``:
    the BC point lookups of the reverse direction). Counts only; a key the totals do not
    name is dropped, never echoed."""
    bc = {"reader": 0, "orders": 0, "orders_unreadable": 0, "requests": 0, "lookups": 0,
          "lookup_states": {}, "hits": 0, "scanned": 0}
    out: dict[str, dict[str, Any]] = {"forward": {"joined": 0, "scanned": 0,
                                                  "budget_spent": 0},
                                      "reverse": {"joined": 0, "linked_held": 0,
                                                  "linked_matched": 0, "reasons": {},
                                                  "scanned": 0, "start_held": 0,
                                                  "tried": 0, "budget_spent": 0,
                                                  "bc": bc}}

    def add(total: dict[str, Any], counts: Mapping[str, Any]) -> None:
        for key, value in counts.items():
            if key not in total:
                continue
            if isinstance(total[key], dict) and isinstance(value, Mapping):
                if key in ("reasons", "lookup_states"):
                    total[key] = _totals([total[key], value])
                else:
                    add(total[key], value)
            elif isinstance(value, (int, bool)) and not isinstance(total[key], dict):
                total[key] += int(value)

    for pick in picks:
        for name, counts in ((pick or {}).get("directions") or {}).items():
            if name in out and isinstance(counts, Mapping):
                add(out[name], counts)
    return out


def _write(directory: Path, index: int, reader: Any, view: Mapping[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    name = "_".join(sorted(p.replace(":", "-") for p in reader.principals))[:120]
    (directory / f"case-{index}-{name}.json").write_text(
        json.dumps(view, indent=1, sort_keys=True, default=str), "utf-8")
