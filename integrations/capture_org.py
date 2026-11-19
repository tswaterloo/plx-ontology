"""Org chart capture: who our staff are and who each reports to, read GET-only from the
tenant directory into its own capture store ("a company model
that knows the people and who reports to whom").

Reads (``integrations/live_source_grammar.py`` operations, the one guarded Graph client,
the mail application ``MICROSOFT_GRAPH`` by default, which holds ``User.Read.All``):

* ``graph.users`` ``GET /users?$select=id,userType,accountEnabled,displayName,
  userPrincipalName,mail,jobTitle,department,officeLocation,employeeType&$top=999``, every
  page (the listing the mail capture's directory scope also reads);
* ``graph.user_manager`` ``GET /users/{oid}/manager?$select=id`` once per kept user (a
  404 is "no manager"; any other failure leaves that user's manager unread).

Kept users: members (no guests) with a mail address whose object id and addresses the
people exclusion decides are not an excluded person's, and whom the mail capture's HR /
payroll deny list (``mail_all_mailboxes_scope.json``) does not match. Fail closed: with
the exclusion unusable the run is blocked before any request; an excluded or undecided
user, and a deny-listed one, is never stored (counted only), and no request names them.
A manager is kept only when it is itself a kept user of the same listing: an excluded,
undecided, deny-listed, guest or unlisted manager is recorded as ``withheld`` (no id).

Store: ``<state>/org-capture/capture.db`` (a ``CaptureStore``; one record per kept user,
kind ``org_user``, cited ``org-user://<object id>``, unit ``org:directory``, resource
``mail:directory``). A run that listed every page and read every manager is
``complete`` (a user gone from the directory is then marked missing); anything else is
``partial`` and marks nothing missing. Rows: object id, UPN, display name, job title,
department, office, account enabled, employee type, manager object id and
``manager_state`` (``listed`` | ``none`` | ``withheld`` | ``unread``).

Consumer: the ``biz.staff`` business view (``operating.pg.business_staff``) reads the
current rows at business-view build and joins them to the served staff ``Person`` objects
by exact object id (no ontology type: the org chart is not part of the model's schema).

CLI (counts, booleans and enums only, never ids, names or addresses)::

    python3 -m integrations.capture_org --state-dir D \\
        --live-audience examples/org_capture_audience.live-check.json

Live-unverified until live check ``graph.org_capture`` passes."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.types import Actor

from . import live_source_grammar as grammar
from . import people_exclusion
from .capture_mail_scope import assert_outside_git
from .capture_mail_standing import graph_block
from .capture_store import (CaptureError, CaptureStore, CapturedRecord, SourceKind, keep_store_dir_mode,
                            register_source_kind)
from .live_source_guard import LiveSourceAudience, SourceUnavailable

STORE_DIR, DB_FILE, ORG_KIND_NAME = "org-capture", "capture.db", "org_user"
LINEAGE = "mail"
UNIT = "org:directory"
RESOURCE = "mail:directory"  # granted only by an exact or the mail:* grant
CAPTURE_PRINCIPAL = "svc:org-capture"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRECTORY_SCOPE = REPO_ROOT / "integrations" / "mail_all_mailboxes_scope.json"
GRAPH_PERMISSIONS = ("User.Read.All",)
USER_SELECT = ("id,userType,accountEnabled,displayName,userPrincipalName,mail,jobTitle,"
               "department,officeLocation,employeeType,proxyAddresses")
MAX_PAGES, MAX_ITEMS = 50, 5000
WORKERS = 4
MAX_TEXT = 200
MANAGER_STATES = ("listed", "none", "withheld", "unread")


def _oid(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not grammar.TYPES["user_id"].valid(text):
        raise ValueError("org_user path is not an object id")
    return text


def _path(parts: Sequence[str]) -> tuple[str]:
    if len(parts) != 1:
        raise ValueError("org_user path has one part")
    return (_oid(parts[0]),)


ORG_KIND = register_source_kind(SourceKind(
    name=ORG_KIND_NAME, scheme="org-user", path_parts=1, canonical_path=_path,
    unit=lambda p: UNIT, resources=lambda p: (RESOURCE,), key=lambda p: {"object_id": p[0]}))


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:MAX_TEXT] if text else None


def _addresses(user: Mapping[str, Any]) -> list[str]:
    out = [user.get("mail"), user.get("userPrincipalName")]
    for proxy in user.get("proxyAddresses") or ():
        if isinstance(proxy, str) and proxy.lower().startswith("smtp:"):
            out.append(proxy[5:])
    return sorted({a.strip().lower() for a in out if isinstance(a, str) and "@" in a})


def user_row(user: Mapping[str, Any], manager: str | None, state: str) -> dict[str, Any]:
    """One kept user's row (no proxy addresses: the Person keeps the mail capture's)."""
    if state not in MANAGER_STATES:
        raise ValueError("manager_state")
    return {"object_id": _oid(user.get("id")),
            "upn": (_text(user.get("userPrincipalName")) or "").lower() or None,
            "display_name": _text(user.get("displayName")),
            "job_title": _text(user.get("jobTitle")),
            "department": _text(user.get("department")),
            "office": _text(user.get("officeLocation")),
            "employee_type": _text(user.get("employeeType")),
            "account_enabled": user.get("accountEnabled") is True,
            "manager_object_id": manager if state == "listed" else None,
            "manager_state": state}


def open_store(state_dir: str | Path, audience: Any) -> CaptureStore:
    root = assert_outside_git(state_dir) / STORE_DIR
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    keep_store_dir_mode(root)
    return CaptureStore(root / DB_FILE, audience)


class OrgCapture:
    """One pass: the directory listing, each kept user's manager, saved as one run."""

    def __init__(self, store: CaptureStore, api: Any, *, workers: int = WORKERS,
                 directory_scope: str | Path = DEFAULT_DIRECTORY_SCOPE) -> None:
        self.store, self.api = store, api
        self.people = people_exclusion.from_api(api)
        self.workers = max(1, min(int(workers), 16))
        self.directory_scope = directory_scope

    def run(self) -> dict[str, Any]:
        counts = {k: 0 for k in ("users_listed", "users_guest", "users_no_mail",
                                 "users_excluded", "users_denied", "users_kept",
                                 "managers_listed", "managers_none", "managers_withheld",
                                 "managers_unread", "versions_new")}
        report: dict[str, Any] = {"counts": counts, "blocked_upstream": [], "reasons": []}
        try:
            self.people.require()        # no I/O: refuse before any request
            self.people.bind(self.api)
        except people_exclusion.ExclusionUnavailable:
            report["blocked_upstream"].append(self.people.block_part("org.people_exclusion"))
            return {**report, "status": "blocked", "reasons": ["blocked:people_exclusion"]}
        from .mail_directory_scope import DirectoryScope, DirectoryScopeError
        try:
            deny = DirectoryScope.from_file(self.directory_scope).deny
        except DirectoryScopeError:
            return {**report, "status": "blocked", "reasons": ["blocked:deny_list_unreadable"]}
        try:
            users, completeness = self.api.get_pages(
                grammar.url("graph.users", {"$select": USER_SELECT, "$top": "999"}),
                max_pages=MAX_PAGES, max_items=MAX_ITEMS)
        except SourceUnavailable as exc:
            part = graph_block("org.directory", "graph.users", {"graph.users": "User.Read.All"},
                               getattr(exc, "http_status", None))
            if part is not None:
                report["blocked_upstream"].append(part)
                return {**report, "status": "blocked", "reasons": ["blocked:graph.users"]}
            return {**report, "status": "failed",
                    "reasons": [f"source_error:{type(exc).__name__}"]}
        reasons: set[str] = set()
        if not completeness.get("complete"):
            reasons.add("cap:directory_listing")
        kept: dict[str, Mapping[str, Any]] = {}
        for user in users:
            if not isinstance(user, Mapping):
                continue
            oid = str(user.get("id") or "").strip().lower()
            if not grammar.TYPES["user_id"].valid(oid):
                continue
            counts["users_listed"] += 1
            if user.get("userType") == "Guest":
                counts["users_guest"] += 1
                continue
            if not isinstance(user.get("mail"), str) or not user["mail"].strip():
                counts["users_no_mail"] += 1
                continue
            try:
                excluded = self.people.excluded([oid, *_addresses(user)])
            except people_exclusion.ExclusionUnavailable:
                excluded = True     # undecided: never kept
            if excluded:
                counts["users_excluded"] += 1
                continue
            if deny.denies(user):
                counts["users_denied"] += 1
                continue
            kept[oid] = user
        counts["users_kept"] = len(kept)

        def manager(oid: str) -> tuple[str | None, str]:
            try:
                found = self.api.get_json(grammar.url("graph.user_manager", {"$select": "id"},
                                                      user=oid))
            except SourceUnavailable as exc:
                return None, ("none" if getattr(exc, "http_status", None) == 404 else "unread")
            boss = str((found or {}).get("id") or "").strip().lower()
            if not boss:
                return None, "none"
            # only a kept user of this listing: anyone else (excluded, undecided, denied,
            # guest, unlisted) is withheld, never named
            return (boss, "listed") if boss in kept and boss != oid else (None, "withheld")

        order = sorted(kept)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            found = dict(zip(order, pool.map(manager, order)))
        records = []
        for oid in order:
            boss, state = found[oid]
            counts["managers_" + state] += 1
            row = user_row(kept[oid], boss, state)
            digest = sha256(json.dumps(row, sort_keys=True).encode("utf-8")).hexdigest()
            records.append(CapturedRecord((oid,), {"row": row, "meta": {
                "source_api": "graph.users", "manager_api": "graph.user_manager"}},
                digest, LINEAGE))
        if counts["managers_unread"]:
            reasons.add("managers_unread")
        run = self.store.begin_run(ORG_KIND.name, UNIT, [RESOURCE])
        saved = self.store.save_page(run["run_id"], records, None)
        counts["versions_new"] = saved["new_versions"]
        state = "complete" if not reasons else "partial"
        self.store.finish_run(run["run_id"], state,
                              reason=None if state == "complete" else sorted(reasons)[0])
        report["reasons"] = sorted(reasons)
        report["status"] = "complete" if not reasons else "incomplete"
        return report


def current_rows(path: str | Path) -> dict[str, dict[str, Any]]:
    """object id -> the current row of every user the org store at ``path`` holds (not
    superseded, not missing from the directory, a known ``manager_state``), read-only;
    ``{}`` without a readable store. A malformed row is skipped."""
    import sqlite3
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT payload FROM versions WHERE kind = ? AND superseded_at IS NULL "
                "AND missing_from_source_since IS NULL", (ORG_KIND_NAME,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for (payload,) in rows:
        try:
            row = json.loads(payload).get("row")
            oid = _oid(row.get("object_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if row.get("manager_state") in MANAGER_STATES:
            out[oid] = row
    return out


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m integrations.capture_org")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--live-audience", required=True)
    parser.add_argument("--principal", default=CAPTURE_PRINCIPAL)
    parser.add_argument("--withheld-principal", default="staff:org-withheld")
    parser.add_argument("--workers", type=int, default=WORKERS)
    args = parser.parse_args(argv)
    from .graph_read import build_graph
    env = os.environ if environ is None else environ
    audience = LiveSourceAudience.from_file(args.live_audience)
    store = open_store(args.state_dir, audience)
    try:
        api, transport = build_graph(env, sender=sender)
        result = OrgCapture(store, api, workers=args.workers).run()
        actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
        withheld = Actor(audience.tenant, frozenset({args.withheld_principal}), "restricted")

        def visible(who: Actor) -> int:
            seen, after = 0, None
            while True:
                try:
                    page = store.records(who, ORG_KIND.name, UNIT, after_path=after,
                                         limit=1000)
                except CaptureError:   # a refused unit is 0 rows seen
                    return seen
                seen += len(page["items"])
                after = page["next_after_path"]
                if after is None:
                    return seen
        report = {"check": "graph.org_capture",
                  "evidence_label": "injected-transport" if transport.sender_injected
                  else "live-read",
                  "read_only_guard": "GET-only; token POST only",
                  "graph_permissions_required": list(GRAPH_PERMISSIONS),
                  "status": result["status"], "reasons": result["reasons"],
                  "blocked_upstream": result["blocked_upstream"], "counts": result["counts"],
                  "capture_actor_rows": visible(actor), "withheld_actor_rows": visible(withheld),
                  "gets_sent": transport.gets_sent,
                  "operations_read": sorted(transport.operations_read)}
        report["ok"] = (result["status"] == "complete" and report["withheld_actor_rows"] == 0
                        and report["capture_actor_rows"] == result["counts"]["users_kept"])
    finally:
        store.close()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["CAPTURE_PRINCIPAL", "DB_FILE", "GRAPH_PERMISSIONS", "MANAGER_STATES", "ORG_KIND",
           "OrgCapture", "STORE_DIR", "current_rows", "main", "open_store", "user_row"]
