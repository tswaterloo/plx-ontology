"""The console's session journal: what an employee did in a live console session, as
counts, routes and enums, kept in the host's state directory.

    python3 -m operator_ui.session_journal summary --state-dir DIR [--session REF]

A recorded employee session (requirement WS-16, docs/runbooks/ws16-first-employee-session.md)
needs a record of what the person opened and decided that does not depend on someone's
notes. ``operator_ui.server`` gives every ``/api/*`` request to ``SessionJournal.record``
after it answered: the session's reference (``SessionIdentity.session_ref``, the SHA-256 of
the ticket id, never the ticket), the employee principal, how the session was proven
(``SessionIdentity.proof``: a signed session ticket, or ``entra_access_token`` for an
employee signed in with their PLX Microsoft account), the route (path only; a case
route's identifier and every query string are dropped), the status, a refusal code, the
type of an opened object (the id's type prefix when it is a schema object type name,
else "other"; never the id) and, for an
object view or a case view, how many items each panel showed (timeline, originals, gaps,
conflicts, reviews, documents), and for an Ask or an investigation how many claims and
citations it carried and how many claim checks or re-reads passed and failed. No value,
identifier, citation, question or answer is written. ``summary``
prints, per session, counts and enums only (and the session reference), and
``minted_by``: who minted the session's ticket according to ``DIR/console-ticket-audit.jsonl``
(``host`` for ``operator_ui.console_ticket mint``, "other" for an unknown minter,
"unrecorded" without an audit row): what to attach to a recorded session's evidence beside
the observer's notes.

``check`` is the live check ``session.employee_recorded``: from the journal and the audit
file it reports, as counts and booleans only, whether one session in which a non-eval
employee signed in with their own Entra access token (proof ``entra_access_token``; a
ticket the host minted, ``console_ticket mint``, is the loopback operator's stand-in and
is counted as ``host_minted`` but never qualifies) opened an object or case view with
timeline, originals and conflicts and asked or investigated with citations (the eval
reader is ``PLX_EVAL_READER``'s employee; its sessions never count; unset or unusable it
reads blocked, never qualifying). Without the console's Entra app registration
(``PLX_CONSOLE_ENTRA_RESOURCE``: the ``plx-entra-resource/v1`` file the console's
``--entra-resource`` reads, with at least one signing key; unset, the committed
operating/entra_resource.json; ``entra_app_registration_absent``, ``_incomplete`` (a field
still a typed unset) or ``_invalid``), a state directory, a journal or a recorded session
it reports ``blocked_reason``.

A journal that cannot be written never fails the person's request: the console names the
failure on stderr (its class only) and the summary is then incomplete for that session,
which the run book's observer notes record. A write waits at most half a second for the
database lock and each write keeps only the newest ``KEEP_ROWS`` rows.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping, Sequence


JOURNAL_FILE = "console-journal.sqlite"
# ``operator_ui.console_ticket`` appends one row per host mint and revoke here (beside the
# journal, in the state directory); ``summary`` reads who minted each session from it.
AUDIT_FILE = "console-ticket-audit.jsonl"
MINTED_BY_HOST = "host"
_MINTERS = frozenset({MINTED_BY_HOST})
MAX_AUDIT_BYTES = 16 * 1024 * 1024
_DDL = """
CREATE TABLE IF NOT EXISTS request (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, session_ref TEXT, employee TEXT,
  method TEXT NOT NULL, route TEXT NOT NULL, status INTEGER NOT NULL, refusal TEXT,
  object_type TEXT, panels TEXT, proof TEXT)
"""
# How a session was proven (``operating.session_identity`` PROOF_*); anything else "other".
PROOF_ENTRA = "entra_access_token"
_PROOFS = frozenset({"signed_session_ticket", PROOF_ENTRA, "configured_employee", "fixture"})
# Routes kept as they are; a path under one of the prefixes is kept as the prefix (its
# tail is a case identifier); anything else is "other".
_ROUTES = frozenset({"/api/session", "/api/cases", "/api/objects", "/api/object",
                     "/api/object-source", "/api/investigate", "/api/object-actions",
                     "/api/object-case", "/api/object-review", "/api/object-follow-up",
                     "/api/object-action", "/api/object-ask", "/api/investigate-record",
                     "/api/object-replay", "/api/objective", "/api/status-card"})
_PREFIXES = ("/api/case-citation", "/api/case-original-pdf", "/api/case-document-metadata",
             "/api/case/")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRINCIPAL = re.compile(r"^employee:[0-9a-f-]{36}$")
# Rows kept: each write prunes everything but the newest ones (a session is a few
# hundred requests; the journal never grows without bound on the host's volume).
KEEP_ROWS = 50_000
# A write waits this long for the database lock, then is dropped like any failed write.
LOCK_WAIT_SECONDS = 0.5
# The object view's panels the journal counts.
PANELS = ("timeline", "originals", "gaps", "conflicts", "reviews", "documents")
CASE_PANELS = ("timeline", "originals", "gaps", "conflicts")
VIEW_PANELS = ("timeline", "originals", "conflicts")
STATE_DIR_ENV = "PLX_WORKBENCH_STATE_DIR"
EVAL_READER_ENV = "PLX_EVAL_READER"
# The console's Entra app registration: the plx-entra-resource/v1 file its --entra-resource
# reads (operating.oidc_auth), by default the committed one. Absent or incomplete, no
# employee can sign in, so the check is blocked.
ENTRA_RESOURCE_ENV = "PLX_CONSOLE_ENTRA_RESOURCE"


def route_of(path: str) -> str:
    if path in _ROUTES:
        return path
    for prefix in _PREFIXES:
        if path.startswith(prefix):
            return prefix
    return "other"


_SCHEMA_TYPES: frozenset[str] | None = None


def _schema_types() -> frozenset[str]:
    """The ontology schema's object type names, loaded once; empty if it cannot load."""
    global _SCHEMA_TYPES
    if _SCHEMA_TYPES is None:
        try:
            from ontology.schema import load_schema
            _SCHEMA_TYPES = frozenset(load_schema().object_types)
        except Exception:  # noqa: BLE001 - a journal never fails a request
            _SCHEMA_TYPES = frozenset()
    return _SCHEMA_TYPES


def object_type_of(object_id: Any) -> str | None:
    """The type prefix of an object id (``<type>:<key>``) when it names a schema object
    type, else "other"; None without an id. Never the key, never free text."""
    if not isinstance(object_id, str) or ":" not in object_id:
        return None
    head = object_id.split(":", 1)[0]
    return head if head in _schema_types() else "other"


def _count(panel: Any, *keys: str) -> int | None:
    if isinstance(panel, list):
        return len(panel)
    if not isinstance(panel, Mapping):
        return None
    total = panel.get("total")
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    for key in keys:
        if isinstance(panel.get(key), list):
            return len(panel[key])
    return None


def panel_counts(view: Any) -> dict[str, int] | None:
    """Items each panel of an object view showed; None for anything but a read view."""
    if not isinstance(view, Mapping) or view.get("status") != "read":
        return None
    counts = {name: _count(view.get(name), "items", "events", "reviews")
              for name in PANELS}
    withheld = (view.get("originals") or {}).get("withheld") \
        if isinstance(view.get("originals"), Mapping) else None
    if isinstance(withheld, Mapping) and isinstance(withheld.get("shown"), int):
        counts["originals_withheld"] = withheld["shown"]
    return {name: value for name, value in counts.items() if value is not None}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def ask_counts(value: Any) -> dict[str, int] | None:
    """Claims, citations and claim-check verdicts of an Ask's answer (counts only)."""
    answer = value.get("answer") if isinstance(value, Mapping) else None
    if not isinstance(answer, Mapping) or not isinstance(answer.get("claims"), list):
        return None
    claims = [claim for claim in answer["claims"] if isinstance(claim, Mapping)]
    counts = {"claims": len(claims),
              "citations": sum(len(claim["citations"]) for claim in claims
                               if isinstance(claim.get("citations"), list))}
    checks = answer.get("claim_checks")
    verdicts = checks.get("counts") if isinstance(checks, Mapping) else None
    for name in ("passed", "failed"):
        if isinstance(verdicts, Mapping) and _int(verdicts.get(name)) is not None:
            counts["checks_" + name] = verdicts[name]
    return counts


def investigate_counts(value: Any) -> dict[str, int] | None:
    """Answer entries, their citations and re-read verdicts of an investigation."""
    result = value.get("investigation") if isinstance(value, Mapping) else None
    entries = result.get("answer") if isinstance(result, Mapping) else None
    if not isinstance(entries, list):
        return None
    entries = [entry for entry in entries if isinstance(entry, Mapping)]
    return {"claims": len(entries),
            "citations": sum(1 for entry in entries if entry.get("citation")),
            "checks_passed": sum(1 for entry in entries if entry.get("check") == "passed"),
            "checks_failed": sum(1 for entry in entries if entry.get("check") == "failed")}


def case_counts(view: Any) -> dict[str, int] | None:
    """Items the case view's timeline, originals, gaps and conflicts showed."""
    if not isinstance(view, Mapping) or view.get("view") != "workspace":
        return None
    counts = {name: len(view[name]) for name in CASE_PANELS
              if isinstance(view.get(name), list)}
    return counts or None


_COUNTERS = {"/api/object": panel_counts, "/api/object-ask": ask_counts,
             "/api/objective": ask_counts,
             "/api/investigate": investigate_counts, "/api/case/": case_counts}


def _minters(directory: Path) -> dict[str, str]:
    """Session reference -> who minted its ticket, from the ticket audit in ``directory``:
    a known minter (``host``), else "other". Reads the newest ``MAX_AUDIT_BYTES`` of the
    file; unreadable lines are skipped."""
    found: dict[str, str] = {}
    try:
        with open(directory / AUDIT_FILE, "rb") as handle:
            start = max(0, handle.seek(0, 2) - MAX_AUDIT_BYTES)
            # One byte before the window too: when it is a newline the window starts
            # on a whole row, and only the empty piece before it is dropped below.
            handle.seek(max(0, start - 1))
            raw = handle.read(MAX_AUDIT_BYTES + (1 if start else 0))
    except OSError:
        return found
    lines = raw.split(b"\n")
    if start:   # the first piece is cut mid-row (or empty)
        lines = lines[1:]
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("event") != "mint":
            continue
        ref = row.get("session_ref")
        if isinstance(ref, str) and _SHA256.match(ref):
            minter = row.get("minted_by")
            found[ref] = minter if isinstance(minter, str) and minter in _MINTERS \
                else "other"
    return found


class SessionJournal:
    """Appends one row per answered ``/api/*`` request to ``DIR/console-journal.sqlite``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(_DDL)
            if "proof" not in _columns(db):   # a journal written before proofs were kept
                db.execute("ALTER TABLE request ADD COLUMN proof TEXT")

    @classmethod
    def for_state_dir(cls, state_dir: str | Path) -> "SessionJournal":
        return cls(Path(state_dir) / JOURNAL_FILE)

    def record(self, *, method: str, path: str, status: int, session: Any = None,
               refusal: str | None = None, object_id: Any = None,
               value: Any = None) -> None:
        """Record one answered request. Raises ``sqlite3.Error``/``OSError`` only."""
        route = route_of(path)
        session_ref = getattr(session, "session_ref", None)
        employee = getattr(getattr(session, "employee", None), "principal", None)
        proof = getattr(session, "proof", None)
        counter = _COUNTERS.get(route)
        panels = counter(value) if counter is not None and status == 200 else None
        row = (datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
               session_ref if isinstance(session_ref, str) and _SHA256.match(session_ref)
               else None,
               employee if isinstance(employee, str) and _PRINCIPAL.match(employee) else None,
               method if method in ("GET", "POST") else "other", route, int(status),
               refusal if isinstance(refusal, str) and _CODE.match(refusal) else
               (None if refusal is None else "other"),
               object_type_of(object_id), None if panels is None else json.dumps(panels),
               None if session is None else proof if proof in _PROOFS else "other")
        with closing(sqlite3.connect(self.path, timeout=LOCK_WAIT_SECONDS)) as db, db:
            cursor = db.execute("INSERT INTO request(at, session_ref, employee, method, route, "
                                "status, refusal, object_type, panels, proof) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?)", row)
            db.execute("DELETE FROM request WHERE id <= ?", (cursor.lastrowid - KEEP_ROWS,))

    def minted_by(self) -> dict[str, str]:
        """Session reference -> who minted its ticket (``_minters``)."""
        return _minters(self.path.parent)

    def summary(self, session_ref: str | None = None) -> dict[str, Any]:
        """Per session: who minted its ticket (``minted_by``: ``host``, "other", or
        "unrecorded" without an audit row), how it was proven (``proofs``: requests per
        proof enum), requests by route and status, object types
        opened, panel totals, first and last time; refusals without a session apart.
        Counts and enums only."""
        minters = self.minted_by()
        sessions: dict[str, dict[str, Any]] = {}
        unauthenticated: dict[str, int] = {}
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT at, session_ref, method, route, status, refusal, "
                              "object_type, panels, proof FROM request ORDER BY id").fetchall()
        for at, ref, method, route, status, refusal, object_type, panels, proof in rows:
            if ref is None:
                key = f"{status}:{refusal or 'none'}"
                unauthenticated[key] = unauthenticated.get(key, 0) + 1
                continue
            if session_ref is not None and ref != session_ref:
                continue
            entry = sessions.setdefault(ref, {
                "minted_by": minters.get(ref, "unrecorded"), "first_at": at, "last_at": at, "requests": 0, "by_route": {},
                "objects_opened": {}, "panel_items": {}, "views_with": {}, "refused": {},
                "proofs": {}})
            entry["last_at"] = at
            entry["requests"] += 1
            proof = proof or "unrecorded"
            entry["proofs"][proof] = entry["proofs"].get(proof, 0) + 1
            key = f"{method} {route} {status}"
            entry["by_route"][key] = entry["by_route"].get(key, 0) + 1
            if refusal is not None:
                entry["refused"][refusal] = entry["refused"].get(refusal, 0) + 1
            if panels is not None:
                if object_type is not None:
                    entry["objects_opened"][object_type] = \
                        entry["objects_opened"].get(object_type, 0) + 1
                for name, count in json.loads(panels).items():
                    entry["panel_items"][name] = entry["panel_items"].get(name, 0) + count
                    if count:
                        entry["views_with"][name] = entry["views_with"].get(name, 0) + 1
        return {"schema": "plx-console-journal-summary/v1", "sessions": sessions,
                "unauthenticated": unauthenticated}


def _columns(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(request)")}


def entra_registration(environ: Mapping[str, str]) -> str | None:
    """None when the console's Entra resource (``PLX_CONSOLE_ENTRA_RESOURCE``, unset the
    committed ``operating.oidc_auth.COMMITTED_RESOURCE``) is usable (a valid
    ``plx-entra-resource/v1`` file whose JWKS file holds at least one signing key), else
    ``entra_app_registration_absent`` (no file), ``_incomplete`` (a field is still a typed
    unset, a human's later ask) or ``_invalid``. Reads local files only; no value is
    returned."""
    from operating.oidc_auth import (COMMITTED_RESOURCE, MAX_JWKS_BYTES, EntraResource,
                                     EntraResourceUnset, parse_jwks)
    raw = (environ.get(ENTRA_RESOURCE_ENV) or "").strip() or str(COMMITTED_RESOURCE)
    if not Path(raw).is_file():
        return "entra_app_registration_absent"
    try:
        jwks = EntraResource.from_file(raw).jwks.path
        if jwks.stat().st_size > MAX_JWKS_BYTES:
            return "entra_app_registration_invalid"
        keys = parse_jwks(json.loads(jwks.read_text("utf-8")))
    except EntraResourceUnset:
        return "entra_app_registration_incomplete"
    except (OSError, ValueError, TypeError, AttributeError):
        return "entra_app_registration_invalid"
    return None if keys else "entra_app_registration_invalid"


def _eval_principal(environ: Mapping[str, str]) -> str | None | bool:
    """The eval reader's employee principal (``PLX_EVAL_READER``), None when the variable
    is unset, False when it is set but unusable (then no session can be told from it)."""
    raw = (environ.get(EVAL_READER_ENV) or "").strip()
    if not raw:
        return None
    try:
        from operating.employee_permissions import EmployeePrincipal
        claims = json.loads(raw)
        return EmployeePrincipal(claims["entraTenantId"], claims["entraObjectId"]).principal
    except (ValueError, KeyError, TypeError):
        return False


def employee_recorded(state_dir: str | Path | None,
                      environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The ``session.employee_recorded`` measurement: counts and booleans, never an
    identifier. A session qualifies when every request of it was proven by an Entra access
    token (the employee signed in; ``signed_in``), it names one employee who is not the
    eval reader, and the same session opened an object or case view with timeline,
    originals and conflicts all above zero and asked or investigated with citations above
    zero. A host-minted ticket session is counted (``host_minted``) and never qualifies.
    ``blocked_reason`` names what is missing (the console's Entra app registration, no
    state directory, no journal, no authenticated session, an unset or unusable eval
    reader), else None."""
    environ = os.environ if environ is None else environ
    out: dict[str, Any] = {
        "schema": "plx-session-employee-recorded/v2", "blocked_reason": None, "sessions": 0,
        "host_minted": 0, "signed_in": 0, "employee_identity": 0, "with_view": 0,
        "with_citations": 0, "qualifying_sessions": 0}
    registration = entra_registration(environ)
    if registration is not None:
        # No app registration: nobody can sign in yet (a later ask), whatever the journal holds.
        return {**out, "blocked_reason": registration}
    if state_dir is None or not str(state_dir).strip():
        return {**out, "blocked_reason": "state_dir_unset"}
    directory = Path(state_dir)
    path = directory / JOURNAL_FILE
    if not path.is_file():
        return {**out, "blocked_reason": "journal_absent"}
    eval_principal = _eval_principal(environ)
    if eval_principal is None:
        # Without the eval identity nothing separates an eval reader from an employee:
        # fail closed rather than let an eval session qualify.
        return {**out, "blocked_reason": "eval_reader_unset"}
    if eval_principal is False:
        return {**out, "blocked_reason": "eval_reader_unusable"}
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                                     timeout=LOCK_WAIT_SECONDS)) as db:
            # Read-only: a journal from before proofs were kept has none (never signed in).
            proof = "proof" if "proof" in _columns(db) else "NULL"
            rows = db.execute("SELECT session_ref, employee, route, panels FROM request "
                              "WHERE session_ref IS NOT NULL AND status = 200 "
                              "ORDER BY id").fetchall()
            proofs: dict[str, set[Any]] = {}
            for ref, value in db.execute(f"SELECT session_ref, {proof} FROM request "
                                         "WHERE session_ref IS NOT NULL"):
                proofs.setdefault(ref, set()).add(value)
            known = set(proofs)
    except sqlite3.Error:
        return {**out, "blocked_reason": "journal_unreadable"}
    if not known:
        return {**out, "blocked_reason": "no_session_recorded"}
    minters = _minters(directory)
    employees: dict[str, set[str]] = {}
    viewed: set[str] = set()
    cited: set[str] = set()
    for ref, employee, route, panels in rows:
        if employee is not None:
            employees.setdefault(ref, set()).add(employee)
        counts = json.loads(panels) if panels else {}
        if route in ("/api/object", "/api/case/") \
                and all(counts.get(name, 0) > 0 for name in VIEW_PANELS):
            viewed.add(ref)
        if route in ("/api/object-ask", "/api/investigate") and counts.get("citations", 0) > 0:
            cited.add(ref)
    hosted = {ref for ref in known if minters.get(ref) == MINTED_BY_HOST}
    signed = {ref for ref in known if proofs[ref] == {PROOF_ENTRA}}
    people = {ref for ref in signed
              if len(employees.get(ref, ())) == 1 and eval_principal not in employees[ref]}
    out.update(sessions=len(known), host_minted=len(hosted), signed_in=len(signed),
               employee_identity=len(people),
               with_view=len(people & viewed), with_citations=len(people & cited),
               qualifying_sessions=len(people & viewed & cited))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operator_ui.session_journal",
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    summary = sub.add_parser("summary", help="counts and enums per console session")
    summary.add_argument("--state-dir", required=True, type=Path)
    summary.add_argument("--session", default=None,
                         help="one session's reference (the console's /api/session "
                              "identity does not show it; console_ticket mint prints it)")
    checking = sub.add_parser("check", help="the session.employee_recorded measurement")
    checking.add_argument("--state-dir", type=Path, default=None,
                          help=f"default: ${STATE_DIR_ENV}")
    args = parser.parse_args(argv)
    if args.command == "check":
        state_dir = args.state_dir or os.environ.get(STATE_DIR_ENV)
        print(json.dumps(employee_recorded(state_dir), indent=1, sort_keys=True))
        return 0
    path = args.state_dir.resolve() / JOURNAL_FILE
    if args.session is not None and not _SHA256.match(args.session):
        parser.error("--session is a session reference (64 hex characters)")
    if not path.is_file():
        print(json.dumps({"status": "refused", "reason": "journal_absent"}))
        return 2
    print(json.dumps(SessionJournal(path).summary(args.session), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
