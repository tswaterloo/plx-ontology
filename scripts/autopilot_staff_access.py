"""Autopilot lookups for opted-in staff (and, with ``--all-staff``, Assist drafts for every
staff member): their own row in the Autopilot host's access snapshot.

The Autopilot's tool sessions (``operator_api.autopilot_context.Lookup``) read the records as
the person an item is for, through the Autopilot host (``PLX_AUTOPILOT_HOST_CONFIG``): only a
person with a row in that host's Portal access snapshot and an entry in the readers file
(``PLX_AUTOPILOT_READERS_FILE``) gets a session. Those files were written for the owner and
Vince only, so anyone else who switched a workflow on got no lookups and no system proposals.

Each run (a timer; cheap when nothing changed) brings both files in line with who has
switched Autopilot on for themselves (the write path's audit journal,
``autopilot_write.prepared_for``):

* **Added**: a person with a workflow on and no row yet, when ALL of these hold: exactly one
  active Portal user whose e-mail the Entra directory resolves to that same object id (by
  exact UPN, through the GET-only Graph reader), the Entra account enabled, the people
  exclusion clearing them (unusable exclusion: nobody is added). The row is their own Portal
  row as the Portal holds it (role, access level, departments, scopes, denies: live through
  the GET-only reporting client, else the host's source mirror), never a grant made up here;
  the Portal's scopes and denies must have been read. What they then read is decided by the
  hosts' own rules (the staff rule, the markings) exactly as for any other reader.
* **All staff** (``--all-staff``, Assist drafts a reply for every staff member,
  each sees only their own): everyone the same checks admit counts as wanted, opted in or not:
  each active Portal user whose e-mail resolves to exactly one Entra object id. A person the
  people exclusion names is never wanted (no row, no readers entry; a row this script added
  earlier is taken out). A lookup that fails for anyone removes nobody that run. A readers
  entry gives the drafting and Autopilot sessions an identity only; what is sent or written
  still needs the person's own confirm (Autopilot writes stay opt-in by the journal).
* **Removed**: a row (and its readers entry) this script added whose person no longer has a
  workflow on. Rows it did not add (the owner-declared ones) are never touched here; the
  daily re-check (``scripts/refresh_owner_access.py``) drops any row whose person left.

The journal unreadable, the Portal users unread, Graph unavailable: nothing is changed.
Files are written atomically, 0600, and the snapshot is checked by the hosts' own parser
first. Never prints a claim, an e-mail or an id: counts and codes only.

    python3 -m scripts.autopilot_staff_access --snapshot DIR/portal_access.json
        --readers autopilot-readers.json --journal STATE/autopilot-write/AUDIT
        [--mirror source-mirror.sqlite] [--no-live] [--dry-run] [--all-staff]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import rollout_access as ra  # noqa: E402

# beside the snapshot: the object ids of the rows this script added (only these it removes)
ADDED_FILE = "autopilot_staff.json"
CLAIMS = ("uid", "email", "entraTenantId", "entraObjectId")


def opted_in(journal: Path) -> set[str] | None:
    """The person ids with any workflow on by their own switch; None when the journal
    cannot be read (nothing is changed then). No journal yet: nobody."""
    from operating import autopilot_engine as ae
    from operating import autopilot_write as aw
    from operating.card_actions import Journal, JournalBroken
    if not journal.exists():
        return set()
    try:
        rows = Journal(journal).rows()
    except (OSError, ValueError, JournalBroken):
        return None
    return {who for who, flows in aw.prepared_for(rows, {}, ae.DRAFT_NEVER).items() if flows}


def _write(path: Path, doc: Any, check: Callable[[str], Any] | None = None) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        if check is not None:
            check(tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def plan(snapshot: Mapping[str, Any], readers: Mapping[str, Any], added: Sequence[str],
         wanted: set[str], portal: Sequence[Mapping[str, Any]] | None,
         lookup: Callable[[str], str | None] | None,
         enabled: Callable[[str], tuple[bool, str]] | None, guard: Any,
         directory: Mapping[str, str] | None = None
         ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], dict[str, int]]:
    """(the snapshot's users, the readers, the added ids, counts) after this run. ``portal``
    (``rollout_access.portal_rows``), ``lookup`` (e-mail -> object id) and ``enabled`` are
    needed only when someone is to be added (None: nobody is added). ``directory``
    (``--all-staff``: object id -> address of each staff member the org chart lists): a
    wanted person no Portal user carries (no row at all under their address, active or
    not) gets the plain staff row (``staff_row``) instead of a Portal row."""
    users = [dict(u) for u in snapshot.get("users") or ()]
    readers = {str(k).lower(): dict(v) for k, v in readers.items()}
    have = {str(u.get("entraObjectId") or "").lower() for u in users}
    added_now = [a for a in added if a in have]
    counts = {"wanted": len(wanted), "added": 0, "removed": 0, "not_added": 0}
    tenant = {str(u.get("entraTenantId") or "") for u in users} - {""}
    # removed: only rows this script added, whose person switched everything off
    for oid in [a for a in added_now if a not in wanted]:
        users = [u for u in users if str(u.get("entraObjectId") or "").lower() != oid]
        readers.pop(oid, None)
        added_now.remove(oid)
        counts["removed"] += 1
    todo = sorted(w for w in wanted if w not in have)
    if todo and (portal is None or lookup is None or enabled is None or guard is None
                 or len(tenant) != 1):
        counts["not_added"] += len(todo)
        return users, readers, added_now, counts
    by_email: dict[str, list[Mapping[str, Any]]] = {}
    any_email: set[str] = set()
    for row in portal or ():
        email = str(row.get("_served_email") or "").strip().lower()
        any_email.add(email)
        if email and row.get("isActive") is True:
            by_email.setdefault(email, []).append(row)
    resolved: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    if todo:
        for email, rows in sorted(by_email.items()):
            try:
                oid = str(lookup(email) or "").lower()
            except Exception:  # noqa: BLE001 - an unresolved address adds nobody
                oid = ""
            if oid in todo:
                resolved.setdefault(oid, []).extend((email, r) for r in rows)
    for oid in todo:
        found = resolved.get(oid) or []
        plain = str((directory or {}).get(oid) or "").strip().lower()
        if not found and plain and plain not in any_email:
            found = [(plain, staff_row(oid))]
        ok = len(found) == 1
        if ok:
            try:
                ok = enabled(oid)[0] is True
            except Exception:  # noqa: BLE001
                ok = False
        if ok:
            email, row = found[0]
            new = {k: v for k, v in row.items() if not k.startswith("_")}
            new.update(email=email, entraTenantId=next(iter(tenant)), entraObjectId=oid)
            kept, _dropped = ra.drop_excluded([new], guard)
            ok = bool(kept)
        if not ok:
            counts["not_added"] += 1
            continue
        users.append(new)
        readers[oid] = {"uid": str(new["id"]), "email": email,
                        "entraTenantId": new["entraTenantId"], "entraObjectId": oid}
        added_now.append(oid)
        counts["added"] += 1
    return users, readers, sorted(set(added_now)), counts


def staff_row(oid: str) -> dict[str, Any]:
    """The plain row of a staff member with no Portal user: the Portal's own staff role at
    the level it bootstraps for it (``LEGACY_ROLE_BOOTSTRAP``), no department, scope or MRP
    module. What they read is the hosts' staff
    rule (every employee: the business records, their own mail and chats); no marking
    (finance, pay) is granted through it."""
    return {"id": oid, "role": "STAFF", "accessLevel": "L2", "isActive": True,
            "mrpWarehouse": False, "mrpProduction": False, "mrpRnD": False,
            "departmentIds": [], "accessScopes": [], "policyDenies": []}


def directory_staff(rows: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """object id -> address of each enabled member the org chart capture lists
    (``integrations.capture_org.current_rows``: members with a mail address, never an
    excluded, undecided or HR / payroll deny-listed user)."""
    out: dict[str, str] = {}
    for oid, row in rows.items():
        upn = str(row.get("upn") or "").strip().lower()
        if row.get("account_enabled") is True and "@" in upn and "#ext#" not in upn:
            out[str(oid).lower()] = upn
    return out


def everyone(portal: Sequence[Mapping[str, Any]], lookup: Callable[[str], str | None],
             guard: Any, directory: Mapping[str, str] | None = None
             ) -> tuple[set[str], bool]:
    """(every staff member's object id, whether any lookup failed): each active Portal user
    whose e-mail resolves and each staff member of ``directory``, minus anyone the people
    exclusion names (fail closed)."""
    staff: set[str] = set()
    unsure = False
    for oid, email in (directory or {}).items():
        if ra.drop_excluded([{"email": email, "entraObjectId": oid}], guard)[0]:
            staff.add(oid)
    for row in portal:
        email = str(row.get("_served_email") or "").strip().lower()
        if not email or row.get("isActive") is not True:
            continue
        try:
            oid = str(lookup(email) or "").lower()
        except Exception:  # noqa: BLE001 - unresolved: not added, and nobody is removed
            unsure = True
            continue
        if oid and ra.drop_excluded([{"email": email, "entraObjectId": oid}], guard)[0]:
            staff.add(oid)
    return staff, unsure


def _memo(look: Callable[[str], str | None]) -> Callable[[str], str | None]:
    seen: dict[str, str | None] = {}

    def once(email: str) -> str | None:
        if email not in seen:
            seen[email] = look(email)
        return seen[email]
    return once


def run(snapshot_path: Path, readers_path: Path, journal: Path, *,
        portal_reader: Callable[[], tuple[list[dict[str, Any]] | None, Mapping[str, Any]]],
        lookup: Callable[[], Callable[[str], str | None]],
        enabled: Callable[[], Callable[[str], tuple[bool, str]]],
        guard: Callable[[], Any], dry_run: bool = False,
        all_staff: bool = False,
        directory: Callable[[], Mapping[str, str]] = dict) -> dict[str, Any]:
    from operating import employee_permissions as ep
    from operator_api import autopilot_context as ac
    out: dict[str, Any] = {"changed": False}
    wanted = opted_in(journal)
    if wanted is None:
        return {**out, "problems": ["journal_unreadable"]}
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        readers = json.loads(readers_path.read_text(encoding="utf-8"))
        if not isinstance(snapshot.get("users"), list) or not isinstance(readers, dict):
            raise TypeError("shape")
    except (OSError, ValueError, TypeError, AttributeError) as error:
        return {**out, "problems": [f"files_unreadable:{type(error).__name__}"]}
    added_path = snapshot_path.parent / ADDED_FILE
    try:
        added = [str(a).lower() for a in json.loads(added_path.read_text())["added"]]
    except FileNotFoundError:
        added = []
    except (OSError, ValueError, TypeError, KeyError):
        return {**out, "problems": ["added_unreadable"]}
    have = {str(u.get("entraObjectId") or "").lower() for u in snapshot["users"]}
    portal = look = check = keep = None
    problems: list[str] = []
    if wanted - have or all_staff:
        portal, basis = portal_reader()
        if portal is None:
            problems.append("portal_unread")
        elif any((basis.get(r) or {}).get("basis") not in ("live", "mirror")
                 for r in (ra.GRANTS, ra.SCOPES)):
            portal = None                  # never a row without the Portal's denies
            problems.append("portal_denies_unavailable")
        try:
            look, check, keep = lookup(), enabled(), guard()
        except Exception as error:  # noqa: BLE001 - no Graph or exclusion: nobody added
            problems.append(f"unavailable:{type(error).__name__}")
            look = check = keep = None
    staff_dir: Mapping[str, str] | None = None
    if all_staff:
        try:
            staff_dir = directory()
        except Exception:  # noqa: BLE001 - handled as unread below
            staff_dir = {}
        if portal is None or look is None or keep is None or not staff_dir:
            # never fall back to the opted-in set alone: that would take every staff row out
            return {**out, "problems": problems or ["staff_unread"]}
        look = _memo(look)
        staff, unsure = everyone(portal, look, keep, staff_dir)
        out["staff"] = len(staff)
        wanted = wanted | staff
        if unsure:
            wanted |= set(added)       # a lookup outage never removes anyone
            problems.append("lookup_failed")
    users, new_readers, new_added, counts = plan(snapshot, readers, added, wanted, portal,
                                                 look, check, keep, staff_dir)
    out.update(counts)
    if problems:
        out["problems"] = problems
    if users == snapshot["users"] and new_readers == {str(k).lower(): v
                                                      for k, v in readers.items()}:
        return out
    if dry_run:
        return {**out, "would_change": True}
    _write(snapshot_path, {**snapshot, "users": users}, lambda p: ep.PortalAccessSnapshot(
        p, max_age_hours=float("1e9")))
    _write(added_path, {"added": new_added})
    _write(readers_path, new_readers)
    if set(ac.readers(readers_path)) != set(new_readers):
        out.setdefault("problems", []).append("readers_entry_refused")
    return {**out, "changed": True}


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    from scripts import refresh_owner_access as roa
    parser = argparse.ArgumentParser(prog="python3 -m scripts.autopilot_staff_access",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--readers", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--no-live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all-staff", action="store_true",
                        help="every staff member (org chart + Portal), not excluded, is wanted")
    parser.add_argument("--org", type=Path, help="the org chart capture store (--all-staff)")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ

    def portal() -> tuple[list[dict[str, Any]] | None, Mapping[str, Any]]:
        tables: dict[str, list[dict[str, Any]]] = {}
        basis: dict[str, Any] = {}
        if not args.no_live and env.get("PLX_WORKBENCH_API_KEY") and env.get(
                "PLX_WORKBENCH_API_URL"):
            from integrations.workbench_read_api import WorkbenchReadApiClient
            try:
                tables, basis = ra.read_live(WorkbenchReadApiClient(
                    env["PLX_WORKBENCH_API_URL"], env["PLX_WORKBENCH_API_KEY"]),
                    ra.PEOPLE_RELATIONS)
            except Exception:  # noqa: BLE001 - a Portal outage: the mirror fills in
                tables, basis = {}, {}
        if args.mirror is not None and any(r not in tables for r in ra.PEOPLE_RELATIONS):
            try:
                tables, basis = ra.merge((tables, basis),
                                         ra.read_mirror(args.mirror, ra.PEOPLE_RELATIONS))
            except ra.RolloutError:
                pass
        if ra.USERS not in tables:
            return None, {}
        try:
            return ra.portal_rows(tables), basis
        except ra.RolloutError:
            return None, {}

    def org() -> dict[str, str]:
        from integrations import capture_org
        return directory_staff(capture_org.current_rows(args.org)) if args.org else {}

    result = run(args.snapshot, args.readers, args.journal, portal_reader=portal,
                 lookup=lambda: ra._graph_lookup(env), enabled=lambda: roa.graph_enabled(env),
                 guard=lambda: ra.exclusion_guard(env), dry_run=args.dry_run,
                 all_staff=args.all_staff, directory=org)
    print(json.dumps(result, sort_keys=True))
    return 0 if not result.get("problems") else 1


if __name__ == "__main__":
    raise SystemExit(main())
