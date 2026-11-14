#!/usr/bin/env python3
"""Walk the app's reads under the API's least-privilege Postgres role before the switch.

    python3 deploy/pg/role_walk.py [--port 8791] [--role plx_api] [--no-ask]
                                   [--files-out FILE.json]

Starts a development API (``--dev --dev-ticket-login``, the development session secret,
never the live one) on a spare loopback port whose every Postgres session runs as ROLE
(``options=-c role=ROLE`` on the host's own login: the role, not a pg_hba change), signs in
as the owner reader (never another person), walks what Home, a record page, search and Ask
read, and reports each route's status and every "permission denied" the database logged
while it ran. Nothing is posted but one Ask (and the dev sign-in); business-system keys are
dropped from the API's environment, so it reads Postgres and the stores only, and live
sending is off. With --files-out the API runs under strace and the state paths it opened
for writing and for reading are written there (directories, no content): the file
inventory a separate API user needs (deploy/release/README-api-user.md).

Prints routes, status codes, object types and table names only: no record id, text,
ticket or key. Exit 0: every route answered below 500 and nothing was denied; 1: a denial
or a server error (the grant to add is printed); 2: could not start or sign in.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Optional
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path(os.environ.get("PLX_CONFIG", Path.home() / ".config" / "plx"))
# business-system and send credentials: the walk reads Postgres and the stores, nothing live
DROP_PREFIXES = ("AZURE_", "BC_", "MICROSOFT_GRAPH_", "SHAREPOINT_", "MC_", "AWS_",
                 "OPENAI_", "ANTHROPIC_API", "PLX_WORKBENCH_API_KEY", "PLX_ACTIONS_",
                 "PLX_EVAL_READER_VINCE")
# any other credential-shaped name (a system added later, a key in the caller's shell): the
# development session secret, read from its own file above, is the one kept
CREDENTIAL_NAME = re.compile(r"SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|_KEY$|"
                             r"ACCESS_KEY|PRIVATE", re.IGNORECASE)
HOME_READS = ("/api/me", "/api/home", "/api/briefing",
              "/api/suggestions", "/api/late", "/api/my-work", "/api/approvals",
              "/api/replies", "/api/alert-kinds", "/api/watch", "/api/trust",
              "/api/actions/state", "/api/actions/log", "/api/work-log", "/api/time-saved",
              "/api/ops/status", "/api/autopilot", "/api/team", "/api/workload",
              "/api/brief/meetings", "/api/meetings/action-items", "/api/labour",
              "/api/rd-projects", "/api/feedback",
              # every other page's first read (app/src: Briefing, Autopilot, Briefs, Writes,
              # Sources, Work log, View as)
              "/api/briefing/items?event_type=vendor_no_reply",
              "/api/briefing/unjudged?check=vendor_no_reply",
              "/api/autopilot/catalog",
              "/api/autopilot/schedules", "/api/autopilot/audit", "/api/brief/weekly",
              "/api/brief/qc-daily", "/api/brief/qc-audit", "/api/writes",
              "/api/work-log/autopilot-days", "/api/view-as/people")
SEARCHES = ("order", "invoice", "shipment")
RECORD_READS = ("/api/object/{t}/{i}", "/api/explore/{t}/{i}", "/api/history/{t}/{i}",
                "/api/record-pack/{t}/{i}", "/api/brief/{t}/{i}", "/api/label-changes/{i}",
                "/api/object-section/late/{t}/{i}", "/api/object-section/people/{t}/{i}",
                "/api/object-section/landed_cost/{t}/{i}",
                "/api/object-section/trail/{t}/{i}", "/api/object-section/margin/{t}/{i}",
                "/api/object-section/knows/{t}/{i}", "/api/label-revisions/{i}",
                "/api/as-of/{t}/{i}", "/api/object-impact/why/{t}/{i}")
ASK_QUESTION = "Which open sales orders are late, and why?"
DENIED = re.compile(r"permission denied for (table|relation|sequence|schema|function|"
                    r"view|materialized view|database|large object|type|language)\s+"
                    r"\"?([A-Za-z0-9_.\"]+)")
GRANT = {"table": "GRANT SELECT ON {n} TO {r};", "relation": "GRANT SELECT ON {n} TO {r};",
         "view": "GRANT SELECT ON {n} TO {r};", "materialized view": "GRANT SELECT ON {n} TO {r};",
         "sequence": "GRANT SELECT ON SEQUENCE {n} TO {r};",
         "schema": "GRANT USAGE ON SCHEMA {n} TO {r};",
         "function": "GRANT EXECUTE ON FUNCTION {n} TO {r};  -- name its argument types"}


def env_file(path: Path) -> dict[str, str]:
    """KEY=value lines (systemd EnvironmentFile shape, optional quotes)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            value = m.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[m.group(1)] = value
    return out


def api_env(dsn: str, base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The development API's environment: the host's settings and the development session
    secret, without business-system or send credentials and without the live secret."""
    env = dict(base if base is not None else os.environ)
    env.update(env_file(CONFIG / "plx.env"))
    env.update(live_unit_settings())
    env.update(env_file(CONFIG / "workbench-session.env"))      # development secret only
    for key in list(env):
        if key.startswith(DROP_PREFIXES) or (CREDENTIAL_NAME.search(key)
                                             and key != "PLX_WORKBENCH_SESSION_SECRET"):
            del env[key]
    role = urllib.parse.unquote(dsn.rsplit("options=", 1)[1]) if "options=" in dsn else ""
    # PGOPTIONS too: every libpq session of the process tree (an Ask's helpers, a DSN read
    # from another variable) runs as the role, not only those opened from PLX_PG_DSN
    env.update({"PLX_PG_DSN": dsn, "PGOPTIONS": role, "PGAPPNAME": "plx-role-walk",
                "PLX_ACTIONS_LIVE": "0",
                "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"})
    if not env.get("PLX_WORKBENCH_SESSION_SECRET"):
        raise SystemExit("role_walk: no development session secret (workbench-session.env)")
    if not env.get("PLX_EXCLUDED_PEOPLE_OIDS"):
        raise SystemExit("role_walk: PLX_EXCLUDED_PEOPLE_OIDS unset: the exclusion fails "
                         "closed, refusing to walk")
    return env


def live_unit_settings() -> dict[str, str]:
    """The live API's non-secret settings: /etc/plx/workbench-api.env and its drop-ins'
    Environment= lines (search mode, Postgres reads, facts cache), so the walk reads what
    the live API reads. PLX_API_ARGS and the send settings are not taken."""
    out = env_file(Path("/etc/plx/workbench-api.env"))
    got = subprocess.run(["systemctl", "show", "plx-api", "-p", "Environment", "--value"],
                         capture_output=True, text=True, timeout=30)
    for item in shlex.split(got.stdout) if got.returncode == 0 else []:
        key, _, value = item.partition("=")
        if key:
            out[key] = value
    out.pop("PLX_API_ARGS", None)
    out.pop("PGOPTIONS", None)
    return out


def role_dsn(base: str, role: str) -> str:
    sep = "&" if "?" in base else "?"
    return base + sep + "options=" + urllib.parse.quote(f"-c role={role}")


def psql(sql: str, dsn: str) -> str:
    return subprocess.run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-d", dsn, "-c", sql],
                          capture_output=True, text=True, check=True, timeout=60).stdout.strip()


def check_role(base_dsn: str, role: str) -> None:
    row = psql("SELECT rolsuper::text || ',' || rolbypassrls::text FROM pg_roles "
               f"WHERE rolname = '{role}'", base_dsn)
    if row != "false,false":
        raise SystemExit(f"role_walk: role {role} missing or privileged ({row or 'absent'}); "
                         "run deploy/pg/plx_api_role.sql first")
    got = psql("SELECT current_user", role_dsn(base_dsn, role))
    if got != role:
        raise SystemExit(f"role_walk: options=-c role did not take ({got})")


# the API's only database writes (deploy/pg/plx_api_role.sql), each as the code issues it,
# inside one transaction that is rolled back: the walk proves the grants without leaving a
# row (a real rating would feed the learning from corrections)
WRITES = (
    "INSERT INTO search.query_log (actor, question, intent, entities, rewrites, results, "
    "timings) VALUES ('role-walk', 'role-walk', NULL, '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, "
    "'{}'::jsonb) RETURNING id",
    "DELETE FROM search.query_log WHERE at < now() - make_interval(days => 36500)",
    "INSERT INTO search.feedback (at, actor, question, answer_id, rating, note) VALUES "
    "(now(), 'role-walk', 'role-walk', 'role-walk', 1, NULL) RETURNING id",
    "SET temp_file_limit = '1GB'",
    "CREATE TEMP TABLE role_walk_tmp (n int) ON COMMIT DROP",
)


def check_writes(base_dsn: str, role: str) -> list[str]:
    """Each write the API makes, run as ROLE and rolled back. Returns the refused ones."""
    refused = []
    for sql in WRITES:
        got = subprocess.run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-d", base_dsn],
                             input=f"BEGIN;\nSET LOCAL ROLE {role};\n{sql};\nROLLBACK;\n",
                             capture_output=True, text=True, timeout=120)
        if got.returncode != 0:
            refused.append(f"{sql.split(' (')[0][:60]}: {got.stderr.strip().splitlines()[-1:]}")
    return refused


def refuse_other_reader(env: dict[str, str]) -> None:
    """The walk signs in as the owner reader only (Agents never sign in as
    another person): the claims must not name the full-read person in reader-vince.env."""
    def ids(claims: dict[str, Any]) -> set[str]:
        return {str(claims.get(k, "")).lower() for k in ("email", "entraObjectId", "uid")
                if claims.get(k)}
    try:
        owner = json.loads(env.get("PLX_EVAL_READER", "") or "{}")
    except ValueError:
        raise SystemExit("role_walk: PLX_EVAL_READER is not JSON claims")
    other = env_file(CONFIG / "reader-vince.env").get("PLX_EVAL_READER_VINCE", "")
    try:
        other_ids = ids(json.loads(other)) if other else set()
    except ValueError:
        other_ids = set()
    if not ids(owner) or ids(owner) & other_ids:
        raise SystemExit("role_walk: the reader is not the owner reader; refusing")


def mint(env: dict[str, str], state: Path, host: Path, out: Path,
         reader_env: str = "PLX_EVAL_READER") -> str:
    got = subprocess.run([sys.executable, "-m", "operator_api.workspace.console_ticket", "mint",
                    "--state-dir", str(state), "--host-config", str(host),
                    "--reader-env", reader_env, "--ttl-minutes", "30", "--out", str(out)],
                   cwd=ROOT, env=env, capture_output=True, timeout=120, check=False)
    if got.returncode != 0:      # the refusal reason (a JSON status line, never a ticket)
        raise SystemExit(f"role_walk: mint for {reader_env} refused: "
                         f"{got.stdout.decode('utf-8', 'replace').strip()[-300:]}")
    return out.read_text(encoding="utf-8").strip().split("#access=", 1)[1]


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        # signs this client in again (a fresh development ticket): a walk on a busy host
        # outlives the ticket's 30 minutes, and every route after that would read 401
        self.relogin: Optional[Any] = None
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, path: str, body: Optional[dict] = None, timeout: float = 180,
             _retried: bool = False) -> tuple[int, Any, float]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json",
                                              "Origin": self.base})
        started = time.monotonic()
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            return 0, {"error": type(exc).__name__}, time.monotonic() - started
        if (status == 401 and self.relogin is not None and not path.startswith("/api/auth/")
                and not _retried and self.relogin()):
            return self.call(path, body, timeout, _retried=True)
        try:
            payload: Any = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode("utf-8", "replace")
        return status, payload, time.monotonic() - started


def records(payload: Any, limit: int = 40) -> Iterable[tuple[str, str]]:
    """(object_type, object_id) pairs anywhere in a response, in order (a suggestion names
    its type ``type``, a page part ``object_type``)."""
    stack, seen = [payload], 0
    while stack and seen < limit:
        node = stack.pop(0)
        if isinstance(node, dict):
            t, i = node.get("object_type") or node.get("type"), node.get("object_id")
            if isinstance(t, str) and isinstance(i, str) and t and i:
                seen += 1
                yield t, i
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


FILE_DENIED = re.compile(r"(PermissionError|Permission denied|attempt to write a readonly "
                         r"database|unable to open database file|Read-only file system)"
                         r"[^\n]{0,160}")


def file_denials(text: str) -> list[str]:
    """Lines where the API was refused a file (the API's own user, deploy/release/
    README-api-user.md): the message and the path it names, never record text."""
    out = []
    for m in FILE_DENIED.finditer(text):
        path = re.search(r"'(/[^']+)'|\"(/[^\"]+)\"", m.group(0))
        out.append(m.group(1) + (f": {path.group(1) or path.group(2)}" if path else ""))
    return sorted(set(out))


def denials(text: str) -> list[tuple[str, str]]:
    return sorted({(m.group(1), m.group(2).replace('"', "")) for m in DENIED.finditer(text)})


def grants(found: Iterable[tuple[str, str]], role: str) -> list[str]:
    return [GRANT.get(kind, "-- {n}: grant by hand ({k})").format(n=name, r=role, k=kind)
            for kind, name in found]


def pg_log_tail(path: Path, offset: int) -> Optional[str]:
    """The server log from OFFSET (adm-readable: read through sudo -n when not readable)."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", "replace")
    except PermissionError:
        got = subprocess.run(["sudo", "-n", "tail", "-c", f"+{offset + 1}", str(path)],
                             capture_output=True, timeout=60)
        return got.stdout.decode("utf-8", "replace") if got.returncode == 0 else None
    except FileNotFoundError:
        return None


def pg_log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except PermissionError:
        got = subprocess.run(["sudo", "-n", "stat", "-c", "%s", str(path)],
                             capture_output=True, text=True, timeout=30)
        return int(got.stdout.strip() or 0) if got.returncode == 0 else 0
    except FileNotFoundError:
        return 0


WRITE_FLAGS = re.compile(r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND")
OPENED = re.compile(r'^\d+\s+(?:\d+:\d+:\d+\.\d+\s+)?(openat|open|creat|mkdir|mkdirat|rename|'
                    r'renameat2?|unlink|unlinkat)\((?:AT_FDCWD|\d+)?,?\s*"([^"]+)"(.*)$')


def file_inventory(trace: Path, roots: Iterable[Path], depth: int = 2) -> dict[str, list[str]]:
    """The paths under ROOTS the traced API wrote and read, cut to DEPTH components below
    the root they fall under (directories and file names, never content)."""
    roots = [Path(r).resolve() for r in roots]
    wrote, read = set(), set()
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        m = OPENED.match(line)
        if not m or " = -1 " in line:
            continue
        call, path, rest = m.groups()
        p = Path(path)
        for root in roots:
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            key = str(root / Path(*rel.parts[:depth])) if rel.parts else str(root)
            is_write = call != "openat" and call != "open" or bool(WRITE_FLAGS.search(rest))
            (wrote if is_write else read).add(key)
            break
    return {"wrote": sorted(wrote), "read_only": sorted(read - wrote)}


def walk(client: Client, ask: bool, report: list[dict]) -> list[tuple[str, str]]:
    def hit(path: str, label: str, body: Optional[dict] = None, timeout: float = 180) -> Any:
        status, payload, secs = client.call(path, body, timeout)
        report.append({"route": label, "status": status, "seconds": round(secs, 2)})
        return payload
    found: list[tuple[str, str]] = []
    for path in HOME_READS:
        found.extend(records(hit(path, path)))
    for q in SEARCHES:
        found.extend(records(hit("/api/suggest?q=" + urllib.parse.quote(q), "/api/suggest")))
    by_type: dict[str, str] = {}
    for t, i in found:
        if t not in by_type and len(by_type) < 4:
            by_type[t] = i
    if not by_type:
        report.append({"route": "record pages", "status": 0, "seconds": 0,
                       "note": "no record reached from Home or search"})
    for t, i in by_type.items():
        for tmpl in RECORD_READS:
            path = tmpl.format(t=urllib.parse.quote(t, safe=""), i=urllib.parse.quote(i, safe=""))
            hit(path, tmpl.replace("{t}", t).replace("/{i}", "/<id>"))
    if ask:
        status, payload, secs = client.call("/api/ask", {"question": ASK_QUESTION}, timeout=600)
        events = re.findall(r"^event: (\w+)", payload if isinstance(payload, str) else "",
                            re.M)
        report.append({"route": "/api/ask", "status": status, "seconds": round(secs, 2),
                       "last_event": events[-1] if events else None})
    return found


# records scoped to a mailbox or a chat: a reader with neither may open none of them
PRIVATE_TYPES = ("Email", "Attachment", "TeamsChatMessage")
SYNTHETIC_VAR = "PLX_WALK_SYNTHETIC_READER"


def synthetic_host(host_json: Path, owner_claims: dict, tmp: Path,
                   synthetic: bool = True) -> tuple[Path, dict]:
    """A private copy of the host config the dev API serves: it reads no directory
    (``entra_directory`` off: the walk reads Postgres and the stores, nothing live; the
    owner and every snapshot employee resolve from the Portal snapshot as before). With
    ``synthetic``, one more employee: a made-up reader (Portal level L1, one department,
    active, not full-read, no mailbox or chat), so a dev API can sign in someone who is not
    the owner and is no real person. Returns the copy's host.json and the claims."""
    import shutil
    import uuid
    dst = tmp / "synthetic-host"
    shutil.copytree(host_json.parent, dst)
    host = json.loads((dst / host_json.name).read_text(encoding="utf-8"))
    host["entra_directory"] = False
    (dst / host_json.name).write_text(json.dumps(host), encoding="utf-8")
    if not synthetic:
        return dst / host_json.name, {}
    portal_path = dst / host["portal_access"]
    policy = json.loads((dst / host["employee_policy"]).read_text(encoding="utf-8"))
    portal = json.loads(portal_path.read_text(encoding="utf-8"))
    oid = str(uuid.uuid4())
    domain = str(owner_claims["email"]).rsplit("@", 1)[1]
    departments = [d for p in policy.get("principals", {}).values()
                   for d in p.get("department_ids", [])][:1]
    claims = {"uid": "walk-" + oid[:8], "email": f"plx-walk-{oid[:8]}@{domain}",
              "entraTenantId": policy["entra_tenant_id"], "entraObjectId": oid}
    portal["users"].append({
        "id": claims["uid"], "email": claims["email"], "entraObjectId": oid,
        "entraTenantId": claims["entraTenantId"], "accessLevel": "L1", "role": "STAFF",
        "isActive": True, "departmentIds": departments, "accessScopes": [],
        "mrpProduction": False, "mrpRnD": False, "mrpWarehouse": False})
    portal_path.write_text(json.dumps(portal), encoding="utf-8")
    (dst / host_json.name).write_text(json.dumps(host), encoding="utf-8")
    return dst / host_json.name, claims


def restricted_checks(client: Client, owner_found: list[tuple[str, str]],
                      report: list[dict]) -> list[str]:
    """SEC-S-14's non-owner walk on live data: what the synthetic reader must not get.
    Returns the failures (route and object type only)."""
    failures: list[str] = []

    def hit(path: str, label: str) -> tuple[int, Any]:
        status, payload, secs = client.call(path, timeout=180)
        report.append({"route": "[reader] " + label, "status": status, "seconds": round(secs, 2)})
        return status, payload
    status, me = hit("/api/me", "/api/me")
    if status != 200:
        return [f"/api/me {status}: the synthetic reader did not get a session"]
    for flag in ("can_view_as", "ops_reader", "feedback_admin"):
        if isinstance(me, dict) and me.get(flag):
            failures.append(f"/api/me grants {flag} to a non-owner")
    for path in ("/api/view-as/people", "/api/ops/status"):
        status, _ = hit(path, path)
        if status not in (401, 403, 404):
            failures.append(f"{path} answered {status} to a non-owner")
    seen: list[tuple[str, str]] = []
    for path in ("/api/home", "/api/my-work"):
        seen.extend(records(hit(path, path)[1], limit=400))
    for q in SEARCHES:
        seen.extend(records(hit("/api/suggest?q=" + urllib.parse.quote(q), "/api/suggest")[1],
                            limit=400))
    leaked = sorted({t for t, _ in seen if t in PRIVATE_TYPES})
    if leaked:
        failures.append(f"the reader's Home/search lists {', '.join(leaked)} records")
    private = [(t, i) for t, i in dict.fromkeys(owner_found) if t in PRIVATE_TYPES][:20]
    if not private:
        report.append({"route": "[reader] owner's mail records", "status": 0, "seconds": 0,
                       "note": "none reached by the owner's walk: the record check did not run"})
    for t, i in private:
        status, _ = hit(f"/api/object/{urllib.parse.quote(t, safe='')}/"
                        f"{urllib.parse.quote(i, safe='')}", f"/api/object/{t}/<owner's>")
        if status != 404:
            failures.append(f"/api/object/{t}: {status} for a record of a mailbox or chat "
                            "the reader is not in")
    return failures


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--role", default="plx_api",
                    help="'none': the same walk on the host's own login (a baseline)")
    ap.add_argument("--pg-dsn", default="postgresql:///plx", help="the host's own login")
    ap.add_argument("--state-dir", type=Path, default=Path("/data/plx-state"))
    ap.add_argument("--host-config", type=Path,
                    default=CONFIG / "workbench-host-vince" / "host.json")
    ap.add_argument("--pg-log", type=Path,
                    default=Path("/var/log/postgresql/postgresql-17-main.log"))
    ap.add_argument("--no-ask", action="store_true")
    ap.add_argument("--reader", choices=("owner", "synthetic"), default="owner",
                    help="synthetic: also sign in a made-up L1 employee (a private copy of "
                    "the host config; no real person) and check what it must not get")
    ap.add_argument("--start-seconds", type=float, default=600)
    ap.add_argument("--api-url", help="walk this running API (e.g. the plx-api-trial unit "
                    "on 8792) instead of starting one; it must sign sessions with the "
                    "development secret")
    ap.add_argument("--unit", help="with --api-url: the unit whose journal is the API log")
    ap.add_argument("--files-out", type=Path,
                    help="run the API under strace; write the state paths it wrote/read here")
    args = ap.parse_args(argv)
    if args.port == 8780 or (args.api_url or "").rstrip("/").endswith(":8780"):
        raise SystemExit("role_walk: 8780 is the live API; pick a spare port")
    if args.api_url:
        return walk_running(args)

    baseline = args.role == "none"     # the same walk on the host's own login, to compare
    write_refusals: list[str] = []
    if not baseline:
        check_role(args.pg_dsn, args.role)
        write_refusals = check_writes(args.pg_dsn, args.role)
    dsn = args.pg_dsn if baseline else role_dsn(args.pg_dsn, args.role)
    env = api_env(dsn)
    if baseline:
        env.pop("PGOPTIONS", None)
    refuse_other_reader(env)
    tmp = Path(tempfile.mkdtemp(prefix="plx-role-walk-"))
    log = tmp / "api.log"
    args.host_config, synthetic = synthetic_host(
        args.host_config, json.loads(env["PLX_EVAL_READER"]), tmp,
        synthetic=args.reader == "synthetic")
    if synthetic:
        env[SYNTHETIC_VAR] = json.dumps(synthetic)
    cmd = [sys.executable, "-m", "operator_api", "serve", "--dev", "--dev-ticket-login",
           "--bind", "127.0.0.1", "--port", str(args.port), "--insecure-cookie",
           "--state-dir", str(args.state_dir), "--host-config", str(args.host_config),
           "--pg-dsn", dsn, "--app-data-dir", str(tmp / "app-data"), "--warm", "0"]
    if not args.no_ask:
        cmd += ["--ask", "--ask-workers", "1"]
    trace = tmp / "files.trace"
    if args.files_out:
        cmd = ["strace", "-f", "-qq", "--seccomp-bpf", "-o", str(trace), "-e",
               "trace=openat,open,creat,mkdir,mkdirat,rename,renameat,renameat2,unlink,unlinkat"
               ] + cmd
    def stop(signum: int, frame: Any) -> None:
        raise SystemExit(2)
    signal.signal(signal.SIGTERM, stop)      # a stopped walk still stops its API
    offset = pg_log_size(args.pg_log)
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log.open("wb"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    report: list[dict] = []
    restricted: list[str] = []
    try:
        client = Client(f"http://127.0.0.1:{args.port}")
        deadline = time.monotonic() + args.start_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                print(f"role_walk: the API exited ({proc.returncode}); log {log}", file=sys.stderr)
                return 2
            if client.call("/api/health", timeout=5)[0] == 200:
                break
            time.sleep(2)
        else:
            print(f"role_walk: the API did not answer in {args.start_seconds:.0f} s", file=sys.stderr)
            return 2
        def signer(c: Client, reader_env: str, name: str) -> Any:
            def sign_in() -> bool:
                ticket = mint(env, args.state_dir, args.host_config, tmp / name,
                              reader_env=reader_env)
                ok = c.call("/api/auth/dev-login", {"ticket": ticket})[0] == 200
                (tmp / name).unlink(missing_ok=True)
                return ok
            return sign_in
        if not signer(client, "PLX_EVAL_READER", "ticket.url")():
            print("role_walk: sign-in refused", file=sys.stderr)
            return 2
        client.relogin = signer(client, "PLX_EVAL_READER", "ticket.url")
        owner_found = walk(client, not args.no_ask, report)
        if args.reader == "synthetic":
            other = Client(client.base)
            other.relogin = signer(other, SYNTHETIC_VAR, "reader.url")
            restricted = (restricted_checks(other, owner_found, report) if other.relogin()
                          else ["the synthetic reader's sign-in was refused"])
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=30)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        import shutil      # the host config copy and the ticket files go; the API log stays
        shutil.rmtree(tmp / "synthetic-host", ignore_errors=True)
        for name in ("ticket.url", "reader.url"):
            (tmp / name).unlink(missing_ok=True)
    api_text = log.read_text(encoding="utf-8", errors="replace")
    code = report_findings(args, report, offset, api_text, str(log))
    code = report_writes(write_refusals, args.role) or code
    if args.reader == "synthetic":
        print("\nnon-owner walk (synthetic L1 reader): " + (
            "nothing it may not get" if not restricted else f"{len(restricted)} failure(s)"))
        for line in restricted:
            print("  " + line)
        code = code or (1 if restricted else 0)
    if args.files_out:
        inv = file_inventory(trace, [args.state_dir, Path.home(), Path("/data"), Path("/etc"),
                                     Path("/tmp"), Path("/var")])
        args.files_out.write_text(json.dumps(inv, indent=1) + "\n", encoding="utf-8")
        print(f"\nfile inventory: {args.files_out}")
    return code


def walk_running(args: argparse.Namespace) -> int:
    """Walk an API a unit runs (its own user, its own sandbox): sign in with a development
    ticket for the owner reader, walk, read the unit's journal and the Postgres log."""
    env = api_env(role_dsn(args.pg_dsn, args.role))
    refuse_other_reader(env)
    tmp = Path(tempfile.mkdtemp(prefix="plx-role-walk-"))
    offset = pg_log_size(args.pg_log)
    since = time.time()
    client = Client(args.api_url.rstrip("/"))
    if client.call("/api/health", timeout=10)[0] != 200:
        print(f"role_walk: {args.api_url} is not up", file=sys.stderr)
        return 2
    def sign_in() -> bool:
        ticket = mint(env, args.state_dir, args.host_config, tmp / "ticket.url")
        ok = client.call("/api/auth/dev-login", {"ticket": ticket})[0] == 200
        (tmp / "ticket.url").unlink(missing_ok=True)
        return ok
    if not sign_in():
        print("role_walk: sign-in refused: the API must run on the development secret",
              file=sys.stderr)
        return 2
    client.relogin = sign_in
    report: list[dict] = []
    walk(client, not args.no_ask, report)
    api_text = ""
    if args.unit:
        cmd = ["journalctl", "-u", args.unit, "--since", f"@{int(since)}", "-o", "cat",
               "--no-pager"]
        got = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if got.returncode != 0 or not got.stdout.strip():
            got = subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True,
                                 timeout=120)
        api_text = got.stdout
    code = report_findings(args, report, offset, api_text, f"journalctl -u {args.unit}")
    return report_writes(check_writes(args.pg_dsn, args.role), args.role) or code


def report_writes(refused: list[str], role: str) -> int:
    print(f"database writes under {role} (rolled back): "
          + ("all granted" if not refused else f"{len(refused)} refused"))
    for line in refused:
        print("  " + line)
    return 1 if refused else 0


def report_findings(args: argparse.Namespace, report: list[dict], offset: int,
                    api_text: str, log_name: str) -> int:
    code = 0
    pg_text = pg_log_tail(args.pg_log, offset)
    found = denials((pg_text or "") + "\n" + api_text)
    files = file_denials(api_text)
    for row in report:
        extra = "  " + row["note"] if row.get("note") else (
            f"  last event: {row['last_event']}" if row.get("last_event") else "")
        print(f"{row['status']:>3}  {row['seconds']:>7.2f}s  {row['route']}{extra}")
    if pg_text is None:
        print(f"Postgres log {args.pg_log} not readable: check it by hand for "
              "'permission denied' since the walk started")
        code = 1
    server_errors = [r for r in report if r["status"] >= 500 or r["status"] == 0]
    if found:
        code = 1
        print(f"\npermission denied under {args.role} (add to deploy/pg/plx_api_role.sql "
              "and its test):")
        for line in grants(found, args.role):
            print("  " + line)
    if files:
        code = 1
        print("\nfile access refused (the API's user; deploy/release/api_user_setup.sh):")
        for line in files:
            print("  " + line)
    if server_errors:
        code = 1
        print(f"\n{len(server_errors)} route(s) failed; the API log is {log_name}")
    print(f"\n{len(report)} routes walked as the owner reader under role {args.role}; "
          f"{len(found)} database denial(s), {len(files)} file denial(s)")
    return code


if __name__ == "__main__":
    sys.exit(main())
