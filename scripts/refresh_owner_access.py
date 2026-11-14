"""Keep an owner-declared host's Portal access snapshot inside its age bound, re-checked.

SEC-004 bounds every Portal access snapshot by ``read_at`` (7 days by default, a host sets up
to 30): past it every employee of that host is denied. The owner hosts' snapshots are not
Portal exports: ``scripts/eval_owner_host.py`` writes the owner's declared grant for named
readers (the owner, Vince). Written once, the served owner host would lock everyone out 30
days later. A real Portal export is never re-stamped here (refused: ``not_owner_declared``);
it is exported again (``scripts/rollout_access.py snapshot``).

For each row this re-checks, at run time, what can change under a declared grant:
* Entra: the row's object id is still an enabled account (one ``GET /users/{oid}`` through
  the GET-only Graph reader). Required.
* Portal: when the row's id is a Portal user (``public.app_user``, live through the GET-only
  reporting client, else the host's source mirror), that user is still active. The role and
  access level are the owner's declaration, so they are not compared.
Only when every row passes is the file rewritten (atomic, 0600, checked by the hosts' own
parser) with ``read_at`` = now and the re-check named in ``source``; any failed row refuses that snapshot
(left as it is, so it ages out and the credential expiry list shows it). Never prints a claim,
an email or an id: counts and codes only.

    python3 -m scripts.refresh_owner_access --snapshot DIR/portal_access.json [--snapshot ...]
        [--mirror source-mirror.sqlite] [--no-live] [--dry-run]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_users(env: Mapping[str, str], mirror: Path | None, live: bool,
               client: Any = None) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """The Portal users (``rollout_access.portal_rows``) and the relation's basis; live
    wins, the mirror fills in; ``None`` when neither could be read."""
    tables: dict[str, list[dict[str, Any]]] = {}
    basis: dict[str, dict[str, Any]] = {}
    if live:
        if client is None and env.get("PLX_WORKBENCH_API_KEY") and env.get(
                "PLX_WORKBENCH_API_URL"):
            from integrations.workbench_read_api import WorkbenchReadApiClient
            client = WorkbenchReadApiClient(env["PLX_WORKBENCH_API_URL"],
                                            env["PLX_WORKBENCH_API_KEY"])
        if client is not None:
            try:
                tables, basis = ra.read_live(client, (ra.USERS,))
            except Exception:  # noqa: BLE001 - a Portal outage: the mirror fills in
                tables, basis = {}, {}
    if ra.USERS not in tables and mirror is not None:
        try:
            tables, basis = ra.read_mirror(mirror, (ra.USERS,))
        except ra.RolloutError:
            pass
    if ra.USERS not in tables:
        return None, {"basis": "unavailable"}
    return ra.portal_rows(tables), basis.get(ra.USERS, {})


def graph_enabled(env: Mapping[str, str]) -> Callable[[str], tuple[bool, str]]:
    from integrations.graph_directory import GraphDirectoryReader
    from integrations.graph_read import build_graph
    api, _transport = build_graph(env)
    return GraphDirectoryReader(api).enabled


def _instant(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return moment if moment.tzinfo else None


DECLARED_SOURCE = "scripts/eval_owner_host.py"
RECHECK_MARK = "; re-checked by scripts/refresh_owner_access.py at "


def confirm(users: Sequence[Mapping[str, Any]], portal: Sequence[Mapping[str, Any]] | None,
            enabled: Callable[[str], tuple[bool, str]]) -> list[str]:
    """Problem codes (``row<i>:<code>``) for the rows that fail a re-check; empty when every
    row is an enabled Entra account and, if a Portal user, still active there.
    ``portal`` None: the Portal users could not be read (``portal_unread``)."""
    if not users:
        return ["snapshot_empty"]
    if portal is None:
        return ["portal_unread"]
    by_id = {p["id"]: p for p in portal}
    problems = []
    for i, user in enumerate(users):
        ok, reason = enabled(str(user.get("entraObjectId") or ""))
        if not ok:
            problems.append(f"row{i}:entra_{reason or 'not_enabled'}")
        match = by_id.get(user.get("id"))
        if match is not None and match.get("isActive") is not True:
            problems.append(f"row{i}:portal_inactive")
        if user.get("isActive") is not True:
            problems.append(f"row{i}:declared_inactive")
    return problems


def refresh(path: Path, portal: Sequence[Mapping[str, Any]] | None,
            basis: Mapping[str, Any], enabled: Callable[[str], tuple[bool, str]], *,
            now: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"snapshot": path.parent.name, "refreshed": False}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        users = doc["users"]
        if not isinstance(users, list):
            raise TypeError("users")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {**out, "problems": [f"snapshot_unreadable:{type(error).__name__}"]}
    out["rows"] = len(users)
    if not str(doc.get("source") or "").startswith(DECLARED_SOURCE):
        return {**out, "problems": ["not_owner_declared"]}
    problems = confirm(users, portal, enabled)
    new_at = _instant(now or _now())
    if new_at is None:
        problems.append("time_invalid")
    if problems:
        return {**out, "problems": problems}
    doc["read_at"] = new_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    base = str(doc["source"]).split(RECHECK_MARK, 1)[0].rstrip("; ")
    doc["source"] = (f"{base}{RECHECK_MARK}{doc['read_at']} (Entra account enabled, "
                     f"Portal user active: {basis.get('basis') or 'unread'}, "
                     f"{len(users)} rows)")
    if dry_run:
        return {**out, "would_refresh": True}
    from operating import employee_permissions as ep
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".portal_access.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        ep.PortalAccessSnapshot(tmp)        # the hosts' own parser accepts it
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {**out, "refreshed": True, "read_at": doc["read_at"]}


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None,
         client: Any = None, enabled: Callable[[str], tuple[bool, str]] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.refresh_owner_access",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, action="append", required=True)
    parser.add_argument("--mirror", type=Path, help="the host's source-mirror.sqlite")
    parser.add_argument("--no-live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    portal, basis = read_users(env, args.mirror, not args.no_live, client)
    try:
        check = enabled or graph_enabled(env)
    except Exception as error:  # noqa: BLE001 - no Graph: nothing is re-stamped
        print(json.dumps({"error": "graph_unavailable", "kind": type(error).__name__}),
              file=sys.stderr)
        return 2
    results = [refresh(p, portal, basis, check, dry_run=args.dry_run) for p in args.snapshot]
    print(json.dumps({"portal_users": None if portal is None else len(portal),
                      "portal_basis": basis.get("basis"), "snapshots": results},
                     sort_keys=True))
    return 0 if all(r.get("refreshed") or r.get("would_refresh") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
