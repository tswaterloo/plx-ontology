"""GET-only Entra directory reader: one employee's user object and transitive groups.

Why: the ``plxwb_`` Portal key and the Graph app token see everything, so the
Workbench decides per employee (operating/employee_permissions.py). This
module supplies the Entra facts that decision intersects: the user exists, is
this object and is enabled, its UPN/mail/department, and its complete
transitive group membership. It only reads; every request goes through
``integrations.live_source_guard`` (closed grammar, GET-only sender,
same-collection ``@odata.nextLink`` rule, page and item caps).

Reads (``integrations/live_source_grammar.py`` operations):

* ``graph.user`` ``GET /users/{oid|upn}?$select=id,accountEnabled,
  userPrincipalName,mail,department`` (``User.Read.All``).
* ``graph.user_transitive_groups`` ``GET /users/{oid}/transitiveMemberOf/
  microsoft.graph.group?$select=id&$top=999`` (``GroupMember.Read.All``),
  every page or nothing.
* ``graph.user_joined_teams`` ``GET /users/{oid}/joinedTeams?$select=id``
  (``Team.ReadBasic.All``, read live by the Teams capture), only when the
  membership read was refused for consent; every page or nothing.
* ``graph.users`` ``GET /users?$select=id,accountEnabled`` (probe only).

Fail closed: ``read`` returns ``status == "read"`` only when the user resolved
to the asked identity, ``accountEnabled`` is exactly ``true`` and every
membership page was read with well-formed group ids. Anything else (404, 403,
another non-2xx, a nextLink that leaves the collection, a page or item cap, a
malformed item, a disabled account) is ``unavailable`` with one reason from
``REASONS`` and no facts at all, so a caller can never mistake a partial list
for the whole one. One exception: 403 on the membership read is the consent
blocker (``GroupMember.Read.All``, docs/reference/permissions.md), not an unknown
user, so the user facts are kept and the groups are derived from the user's
joined Teams (``group_basis`` ``joined_teams``: a team's id is its Microsoft
365 group's id, and an M365 group has no nested groups). Those are positive
evidence only (the resolver never reads an unlisted group as non-membership),
and a joined-Teams listing that is refused, partial or malformed derives no
group at all (``group_basis`` ``none``; the resolver leaves the rights
unresolved). Only a 404 on the user object is ``user_not_found``: a listing
that 404s after the user was read is ``unreadable``. When the consent appears the
membership read succeeds and wins; joinedTeams is not read.

Mirrors the Portal's Entra reader (plx-customer-portal@8f566f8
``portal/src/lib/m365/rbac-entra/graph.ts:readManagedGroupMembers``: paged ``$select=id&$top=999``,
pinned nextLink, missing or duplicate ids refused); the Portal reads from the
managed-group side (``portal/src/lib/auth/entra-projection/manifest.ts``), the
Workbench from the user side because it decides for one employee.

Approver identities (``resolve_roster``): a host that serves actions resolves, at its
start, each approver roster entry's (``operating/approver_roster.json``) Entra object id
from the entry's UPN (its key) with the same ``graph.user`` read (``object_id``: the
enabled user whose UPN is exactly that one, else no id). The ids go only into the
host's state directory (``directory/approver-oids.json``, outside Git, owner-only), each
with its read time; they are never pinned into the committed roster. A transient failure
(``unreadable``: 5xx, throttling, transport) keeps an approver's earlier id, with its
earlier read time, while it is at most ``APPROVER_OID_MAX_AGE`` old; any other outcome
(not found, mismatch, disabled, no permission, Graph not configured) drops it. A state
file that cannot be written is the summary's ``state_file: write_failed`` (status
``unverified``), never an exception out of host start. ``resolved_approver_oids`` is the
one reader of that file (``operating.goal2_mc_approval.load_approver_roster(state_dir=...)``,
for every host: MCP, console, case workspace); the roster records each id's provenance
(``graph_users`` and its read time) and checks the age at every approval check
(``approver_oid_fresh``), so a long-running host refuses an id gone stale; an id read
after the moment the file is read (a future ``read_at``) is not taken at all. Graph is read
at host start only: a long-running host re-reads this file when its copy ages out
(``operating.action_path.ActionPath``, ``approver_roster_reload``), and an approver is
unverified until a host start resolves it again. An unresolved approver keeps no oid and
stays refused. The summary is counts, reasons and a status
only, never an id or address.

Evidence: injected-transport tests only; **live-unverified**
(verify/live-checks/graph.directory_entitlements.json).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Sequence

from ontology import identifiers as _ids
from operating.employee_permissions import (DIRECTORY_SOURCE, GROUP_BASIS_JOINED_TEAMS,
                                            GROUP_BASIS_NONE, GROUP_BASIS_TRANSITIVE,
                                            EntraDirectoryRead)

from . import live_source_grammar as grammar
from .live_source_guard import (ReadOnlyGuardError, SourceApi, SourceNotFound,
                                SourceUnavailable, failure_diagnostics, unavailable_detail)

USER_PERMISSION_MISSING = "graph_permission_missing:User.Read.All"
# Same token as operating.employee_permissions.GROUP_READ_BLOCKER.
GROUP_PERMISSION_MISSING = "graph_permission_missing:GroupMember.Read.All"
PAGE_SIZE = 999
MAX_PAGES = 5
MAX_GROUPS = 4995
REASONS = frozenset({USER_PERMISSION_MISSING, GROUP_PERMISSION_MISSING, "user_not_found",
                     "user_mismatch", "account_disabled", "membership_truncated",
                     "membership_item_invalid", "unreadable"})
# Approver identity resolution (module doc): reasons beyond ``REASONS``, and the file.
GRAPH_UNAVAILABLE = "graph_unavailable"
UPN_INVALID = "upn_invalid"
APPROVER_OIDS_SCHEMA = "plx-approver-oids/v1"
APPROVER_OIDS_DIR, APPROVER_OIDS_FILE = "directory", "approver-oids.json"
APPROVER_OID_MAX_AGE = timedelta(hours=24)
# The committed approver roster and its schema id (one definition, re-exported by
# operating.goal2_mc_approval, the roster's loader): integrations import no operating
# module that reaches back into integrations.
APPROVER_ROSTER_SCHEMA = "plx-approver-roster/v1"
DEFAULT_APPROVER_ROSTER = Path(__file__).resolve().parents[1] / "operating" / \
    "approver_roster.json"
_USER_SELECT = "id,accountEnabled,userPrincipalName,mail,department"
_GROUP_TYPE = "#microsoft.graph.group"
_TEAM_TYPE = "#microsoft.graph.team"
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ADDRESS = _ids.system_pattern("upn")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _guid(value: Any) -> str | None:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if _GUID.fullmatch(text) else None


def _address(value: Any) -> str:
    """A lower-case UPN/SMTP address, or "" for anything else (never an identity)."""
    text = value.strip() if isinstance(value, str) else ""
    return text.lower() if _ADDRESS.fullmatch(text) else ""


def _stamp(clock: Callable[[], datetime]) -> str:
    moment = clock()
    if moment.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unreadable(exc: SourceUnavailable) -> str:
    detail = unavailable_detail(exc)
    return f"unreadable:{detail}" if detail else "unreadable"


def reason_ok(reason: Any) -> bool:
    """Whether ``reason`` is in the fixed vocabulary (``unreadable:<detail token>``)."""
    if not isinstance(reason, str):
        return False
    base, _, detail = reason.partition(":")
    return reason in REASONS or (base == "unreadable"
                                 and re.fullmatch(r"[a-z0-9_.]{1,40}", detail) is not None)


class _Incomplete(Exception):
    """A listing that is not one complete collection of well-formed, distinct ids."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GraphDirectoryReader:
    """Directory reads for per-employee permissions; GET-only through the guard."""

    def __init__(self, api: SourceApi, *, clock: Callable[[], datetime] = _utc_now,
                 max_pages: int = MAX_PAGES) -> None:
        if not isinstance(api, SourceApi):
            raise TypeError("SourceApi required")
        if not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_PAGES:
            raise ValueError(f"max_pages must be 1..{MAX_PAGES}")
        self._api = api
        self._clock = clock
        self.max_pages = max_pages

    def __repr__(self) -> str:
        return "GraphDirectoryReader()"

    def read(self, user_ref: Any) -> EntraDirectoryRead:
        """The directory facts of one user (object id or UPN); all or nothing, except
        that a membership read refused for consent derives the groups (module doc).

        Raises ``ValueError`` for a ``user_ref`` that is neither, and
        ``ReadOnlyGuardError`` for a request the guard refused (a bug, never
        data); every source failure is an ``unavailable`` read.
        """
        oid = _guid(user_ref)
        upn = "" if oid else _address(user_ref)
        if not oid and not upn:
            raise ValueError("directory_user_ref_invalid")

        def unavailable(reason: str, object_id: str | None = None) -> EntraDirectoryRead:
            return EntraDirectoryRead("unavailable", object_id or oid or "", reason=reason)

        user, found, reason = self._user(oid or "", upn)
        if user is None:
            return unavailable(reason, found)

        url = grammar.url("graph.user_transitive_groups",
                          {"$select": "id", "$top": str(PAGE_SIZE)}, user=found)
        basis, why = GROUP_BASIS_TRANSITIVE, ""
        try:
            groups = self._ids(url, _GROUP_TYPE)
        except _Incomplete as exc:
            return unavailable(exc.reason, found)
        except SourceUnavailable as exc:
            if getattr(exc, "http_status", None) != 403:
                return unavailable(_unreadable(exc), found)
            groups, basis, why = self._joined_teams(found)
        department = user.get("department")
        department = department.strip()[:128] if isinstance(department, str) else ""
        return EntraDirectoryRead(
            "read", found, user_principal_name=_address(user.get("userPrincipalName")),
            mail=_address(user.get("mail")), department=department,
            group_ids=groups, source=f"{DIRECTORY_SOURCE}/users/{found}",
            read_at=_stamp(self._clock), group_basis=basis, membership_reason=why)

    def object_id(self, upn: Any) -> tuple[str | None, str]:
        """``(oid, "")`` of the enabled user whose UPN is exactly ``upn``, else
        ``(None, reason)``: one user read, no membership."""
        address = _address(upn)
        if not address:
            return None, UPN_INVALID
        user, found, reason = self._user("", address)
        return (found, "") if user is not None else (None, reason)

    def _user(self, oid: str, upn: str) -> tuple[Mapping[str, Any] | None, str | None, str]:
        """The enabled user object asked for (by id, or by exact UPN) and its id; or
        ``None``, the id Graph answered with (if any) and the reason."""
        url = grammar.url("graph.user", {"$select": _USER_SELECT}, user=oid or upn)
        try:
            user = self._api.get_json(url)
        except SourceNotFound:
            return None, None, "user_not_found"
        except SourceUnavailable as exc:
            if getattr(exc, "http_status", None) == 403:
                return None, None, USER_PERMISSION_MISSING
            return None, None, _unreadable(exc)
        found = _guid(user.get("id")) if isinstance(user, Mapping) else None
        if found is None:
            return None, None, "unreadable:record_invalid"
        # Conservative identity: the object Graph answered must be the one asked
        # for (by id, or by exact UPN).
        if (oid and found != oid) or (upn and _address(user.get("userPrincipalName")) != upn):
            return None, found, "user_mismatch"
        if user.get("accountEnabled") is not True:  # missing or null is not enabled
            return None, found, "account_disabled"
        return user, found, ""

    def _ids(self, url: str, kind: str) -> frozenset[str]:
        """Every id of one paged listing, each a distinct GUID of ``kind``, or ``_Incomplete``."""
        items, completeness = self._api.get_pages(url, max_pages=self.max_pages,
                                                  max_items=MAX_GROUPS)
        if not completeness.get("complete"):
            raise _Incomplete("membership_truncated")
        ids: set[str] = set()
        for item in items:
            found = _guid(item.get("id")) if isinstance(item, Mapping) else None
            if found is None or found in ids or item.get("@odata.type", kind) != kind:
                raise _Incomplete("membership_item_invalid")
            ids.add(found)
        return frozenset(ids)

    def _joined_teams(self, user_id: str) -> tuple[frozenset[str], str, str]:
        """The M365 groups of the user's joined Teams, after the membership read's 403."""
        url = grammar.url("graph.user_joined_teams", {"$select": "id"}, user=user_id)
        try:
            return self._ids(url, _TEAM_TYPE), GROUP_BASIS_JOINED_TEAMS, GROUP_PERMISSION_MISSING
        except _Incomplete as exc:
            reason = exc.reason
        except SourceUnavailable as exc:
            reason = _unreadable(exc)
        return frozenset(), GROUP_BASIS_NONE, f"{GROUP_PERMISSION_MISSING},joined_teams:{reason}"


# ------------------------------------------------------------------ approver identities

def roster_upns(roster: str | Path | None = None) -> list[str]:
    """The UPN keys of the approver roster (default: the committed one)."""
    document = json.loads(Path(roster or DEFAULT_APPROVER_ROSTER).read_text("utf-8"))
    if not isinstance(document, Mapping) or document.get("schema") != APPROVER_ROSTER_SCHEMA \
            or not isinstance(document.get("approvers"), Mapping):
        raise ValueError("roster_document_invalid")
    return [str(key) for key in document["approvers"]]


def _read_time(stamp: Any) -> datetime | None:
    """A ``_stamp`` (``YYYY-MM-DDTHH:MM:SSZ``) as an aware datetime, else None."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def approver_oid_fresh(read_at: Any, now: datetime) -> bool:
    """Whether a Graph read stamped ``read_at`` is at most ``APPROVER_OID_MAX_AGE`` old at
    ``now`` (aware); a malformed or future stamp is not."""
    read = _read_time(read_at)
    return read is not None and timedelta(0) <= now - read <= APPROVER_OID_MAX_AGE


def _transient(reason: str) -> bool:
    """A failure that says nothing about the user (5xx, throttling, transport). Every
    ``unreadable:*`` but ``record_invalid`` counts, a 401 from a broken credential too
    (not tightened): the earlier id it keeps still expires
    ``APPROVER_OID_MAX_AGE`` after its original read."""
    return reason.partition(":")[0] == "unreadable" and reason != "unreadable:record_invalid"


def _write_oids(state_dir: str | Path, moment: str,
                resolved: Mapping[str, Mapping[str, str]]) -> str:
    """Replace the state directory's file atomically: ``written`` or ``write_failed``."""
    from .capture_mail import CaptureError, assert_outside_git
    temp = None
    try:
        folder = assert_outside_git(state_dir) / APPROVER_OIDS_DIR
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / APPROVER_OIDS_FILE
        temp = path.with_name(f".{APPROVER_OIDS_FILE}.{os.getpid()}.tmp")
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump({"schema": APPROVER_OIDS_SCHEMA, "read_at": moment,
                       "approvers": resolved}, out, sort_keys=True)
        os.replace(temp, path)
        return "written"
    except (OSError, CaptureError):
        if temp is not None:
            try:
                temp.unlink()
            except OSError:
                pass
        return "write_failed"


def resolve_roster(reader: GraphDirectoryReader | None, state_dir: str | Path | None = None,
                   *, roster: str | Path | None = None,
                   clock: Callable[[], datetime] = _utc_now) -> dict[str, Any]:
    """Each roster entry's object id by its UPN (``object_id``); ``reader`` None: Graph is
    not configured, nothing resolves. With ``state_dir`` the ids replace the directory's
    file: an approver read now gets this read's time, one whose read failed transiently
    keeps its earlier id while fresh (``kept``), any other loses it (module doc). Returns
    counts, reasons, ``state_file`` and ``status`` (``verified``: every entry read now and
    the file written) only."""
    upns, moment = roster_upns(roster), _stamp(clock)
    now = _read_time(moment)
    earlier = resolved_approver_oids(state_dir, now=now) if state_dir is not None else {}
    resolved: dict[str, dict[str, str]] = {}
    reasons: dict[str, int] = {}
    read = kept = 0
    for upn in upns:
        found, reason = (None, GRAPH_UNAVAILABLE) if reader is None else reader.object_id(upn)
        if found is not None:
            resolved[_address(upn)] = {"oid": found, "read_at": moment}
            read += 1
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
        held = earlier.get(_address(upn)) if _transient(reason) else None
        if held is not None and approver_oid_fresh(held["read_at"], now):
            resolved[_address(upn)] = dict(held)
            kept += 1
    summary: dict[str, Any] = {"entries": len(upns), "resolved": read, "kept": kept,
                               "unresolved": len(upns) - read - kept,
                               "reasons": dict(sorted(reasons.items()))}
    verified = bool(upns) and read == len(upns)
    if state_dir is not None:
        summary["state_file"] = _write_oids(state_dir, moment, resolved)
        verified = verified and summary["state_file"] == "written"
    summary["status"] = "verified" if verified else "unverified"
    return summary


def resolved_approver_oids(state_dir: str | Path | None, *, now: datetime | None = None
                           ) -> dict[str, dict[str, str]]:
    """``{upn: {"oid", "read_at"}}`` as the host last resolved them, whatever their age
    (the roster checks it at each use, ``approver_oid_fresh``); a missing, foreign or
    malformed file or entry, or one read after ``now`` (default: the current time), resolves
    nobody. The one reader of the file."""
    now = now or _utc_now()
    try:
        document = json.loads((Path(state_dir) / APPROVER_OIDS_DIR / APPROVER_OIDS_FILE)
                              .read_text("utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    entries = document.get("approvers") if isinstance(document, Mapping) \
        and document.get("schema") == APPROVER_OIDS_SCHEMA else None
    out: dict[str, dict[str, str]] = {}
    for upn, entry in (entries or {}).items() if isinstance(entries, Mapping) else ():
        oid = _guid(entry.get("oid")) if isinstance(entry, Mapping) else None
        read = _read_time(entry.get("read_at")) if oid else None
        if oid and _address(upn) and read is not None and timedelta(0) <= now - read:
            out[_address(upn)] = {"oid": oid, "read_at": entry["read_at"]}
    return out


# ------------------------------------------------------------------ factory + probe

def probe(environ: Mapping[str, str] | None = None, *, sender=None) -> dict[str, Any]:
    """Counts and fixed enums only: never an id, name, UPN, mail or department.

    Reads up to ten user ids and the directory facts of the first enabled one, then
    resolves the committed approver roster (``resolve_roster``, nothing written). A user
    listing Graph refuses for User.Read.All (``graph_read.probe_block``) is the
    ``blocked_upstream`` part ``directory.user_listing`` and ``ok`` false (exit 1); the
    roster is still read, ``roster_refused`` counting its reads refused for the same grant.
    """
    from .graph_read import build_graph, probe_block
    api, transport = build_graph(os.environ if environ is None else environ, sender=sender)
    reader = GraphDirectoryReader(api)
    report: dict[str, Any] = {
        "source": "microsoft-graph-directory",
        "evidence_label": "injected-transport" if transport.sender_injected else "live-read",
        "read_only_guard": "GET-only; token POST only"}
    blocked: list[dict[str, Any]] = []
    try:
        page = api.get_json(grammar.url("graph.users",
                                         {"$select": "id,accountEnabled", "$top": "10"}))
    except SourceUnavailable as exc:
        found = probe_block("directory.user_listing", "graph.users", exc)
        if found is None:
            raise
        blocked.append(found)
        page = None
    report["token"] = "ok"
    users = page.get("value", []) if isinstance(page, Mapping) else []
    users = [u for u in users if isinstance(u, Mapping)]
    enabled = [u["id"] for u in users if u.get("accountEnabled") is True and _guid(u.get("id"))]
    if page is not None:
        report["users_page_count"] = len(users)
        report["enabled_users_in_page"] = len(enabled)
    if enabled:
        found = reader.read(enabled[0])
        report["directory_status"] = found.status
        report["directory_reason"] = found.reason or "none"
        report["group_count"] = len(found.group_ids)
        report["group_basis"] = found.group_basis if found.status == "read" else "none"
        # 1 when the groups came from a complete listing (transitive or joinedTeams); 0 is
        # group_basis none, where the resolver leaves every employee's rights unresolved.
        report["group_listing_read"] = int(report["group_basis"] != GROUP_BASIS_NONE)
        report["department_present"] = bool(found.department)
        report["upn_present"] = bool(found.user_principal_name)
    roster = resolve_roster(reader)
    report.update({"roster_entries": roster["entries"], "roster_resolved": roster["resolved"],
                   "roster_unresolved": roster["unresolved"],
                   "roster_reason": next(iter(roster["reasons"]), "none"),
                   "roster_refused": roster["reasons"].get(USER_PERMISSION_MISSING, 0)})
    report["requests_sent"] = transport.requests_sent
    if blocked:
        report.update(ok=False, blocked_upstream=blocked)
    return report


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.graph_directory",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="read-only directory counts (no id or name printed)")
    parser.parse_args(argv)
    try:
        result = probe(environ, sender=sender)
    except ReadOnlyGuardError as exc:
        print(json.dumps({"error": "read_only_guard", "message": str(exc)}), file=sys.stderr)
        return 2
    except (SourceUnavailable, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, **failure_diagnostics(exc)}),
              file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0 if result.get("ok", True) else 1


__all__ = ["APPROVER_OID_MAX_AGE", "APPROVER_ROSTER_SCHEMA", "DEFAULT_APPROVER_ROSTER",
           "GROUP_PERMISSION_MISSING", "GraphDirectoryReader",
           "REASONS", "USER_PERMISSION_MISSING", "approver_oid_fresh", "main", "probe",
           "reason_ok",
           "resolve_roster", "resolved_approver_oids", "roster_upns"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
