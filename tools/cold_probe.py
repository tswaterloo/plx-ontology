"""The cold probe (speed specs 1.4, piece SV0; a tool, so it lives in tools/: the product tree's
HTTP-egress gate admits no client but the reviewed senders): every done-check of the serving pieces is a line of
this tool. It times the API's GET routes, per reader, on records nobody opened before, and says
PASS only when every probed route met its budget (``operator_api.route_budgets``) with no
``not_run`` answer and no door decided inside a request.

    python3 tools/cold_probe.py --routes all|<route>[,<route>...] --n 50
        --readers owner,lead,l3,l2 [--dsn ...] [--base http://127.0.0.1:PORT]
        [--fresh-process] [--json OUT] [--budget-scale 1.0] [--equiv OLD_BASE]
        [--reader-id NAME=EMPLOYEE_OBJECT_ID ... --host-config FILE]

Routes: ``all`` is every route of ``route_budgets.ROUTES`` with a budget; a given route is a
prefix of a route's path whose literal segments fill that route's parameters
(``/api/page/record`` fills ``/api/page/{kind}/{key}`` with kind=record). Parameters are filled
from the API's own contract (``openapi.json``): an enum parameter is probed once per value (each
value its own line), a record parameter (``object_type`` / ``object_id``, ``record``, a page key
of kind ``record``, the ``kind`` of ``/api/biz/page``) from records drawn per kind, anything
else from ``--param NAME=VALUE``. A route with a parameter nothing fills is not guessed: it is a
failing line ``not_measured`` with the parameter named.

Where: ``--fresh-process`` (an API of its own, on a Spark), or ``--deploy-gate`` with ``--base``
(the release's probe of an API no reader is routed to yet); never the API people use.

Records: per kind (``--kinds``), n ids drawn by ``ORDER BY md5(record_id || seed)`` from the
standby's ``biz.records`` (``--dsn`` must be a standby: the primary is refused) excluding the ids in ``--seen`` (appended after each run), so none was opened
by an earlier probe. Requests of a route cycle through the kinds.

Readers: each name has a session: ``--session NAME=FILE`` (an access link file; the owner's
default is the measure session ``sudo plx-mint-link --measure`` writes), or ``--view-as
NAME=KEY`` (the owner's measure session viewing as that person: only the routes view-as serves,
``operator_api.view_as.ROUTES``; any other route is ``not_measured`` for that reader). A reader
with neither is ``not_measured`` on every route, never left out.

Per route x reader: n requests one after another, then 8 at once; n, p50, p95, p99, max,
first_ms (``--fresh-process``: the first request of the route in a just-started API, kept out
of the percentiles and judged against the budget on its own), bytes, the ``src`` share
(``X-PLX-Src`` header, else the body's ``status`` when stale / not_run, else ``unstated``) and,
with ``--dsn`` and pg_stat_statements, the database-wide statement count delta per request
(``stmts_db_wide``: other sessions' statements are in it; reported, never judged).

``--reader-id NAME=OBJECT_ID`` (with ``--host-config``, and only with ``--fresh-process``): the
probe makes a throwaway session secret for the child API it starts, in that child's environment
only (never printed, written or exported to anything else), and mints one session ticket per
named employee from the host's Portal access snapshot (the row's own ``id``, e-mail and Entra
ids), valid one hour and only for that child: a process holding any other secret refuses it.
Every named reader (the owner too: its measure link is signed with the real secret) signs in
with its ticket. A reader with no active row in the snapshot is ``not_measured``, never left out.

Each request waits at most ``REQUEST_TIMEOUT_SECONDS`` (a host limit: the slowest route today
takes ~150 s at p95); a timeout is a failing request timed at the timeout, never dropped.

``inline_door`` counts the API log's ``door_not_ahead`` lines over the run: the just-started
API's own output with ``--fresh-process``, else the output of ``--server-log-cmd`` (``{since}``
is the run's start, epoch seconds). With neither it is not measured, which fails the run.

Result, exactly one of:
  ``COLD PROBE PASS routes=<k> requests=<n> worst=<route> p99=<ms> over_budget=0 not_run=0
  inline_door=0 load=<1-min load>`` (exit 0);
  ``COLD PROBE FAIL ...`` then one line per failing route (exit 1);
  ``COLD PROBE INVALID load=<x>`` (exit 3) when the probed host's 1-min load was above its core
  count at the start or the end (a result under overload is not a measurement). The probed
  host's load is read here, so the API must run on this host (a loopback ``--base`` or
  ``--fresh-process``); any other base is INVALID with load=unknown.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OPENAPI_FILE = ROOT / "operator_api" / "openapi.json"
MEASURE_LINK = Path.home() / ".config/plx/page-measure-access-link"
SEEN_FILE = Path("/var/lib/plx-workbench/probe-seen.txt")
# speed specs SV3: the record kinds every record route is probed on
KINDS = ("PurchaseOrder", "SalesOrder", "Customer", "Supplier", "Product", "Batch",
         "SalesInvoice", "SupplierInvoice")
CONCURRENT = 8
REQUEST_TIMEOUT_SECONDS = 180.0
READY_TIMEOUT_SECONDS = 900.0
COOKIE = "plx_app_session"
VIEW_AS_HEADER = "x-plx-view-as"
DOOR_MISS = "door_not_ahead"
# parameters a drawn record fills (the C2 page key of kind ``record`` is a record id too)
RECORD_TYPE_PARAMS = frozenset({"object_type"})
RECORD_ID_PARAMS = frozenset({"object_id", "record"})


class Usage(Exception):
    """A probe that cannot be run as asked (exit 2): never a partial measurement."""


# ---- routes -------------------------------------------------------------------------------------

@dataclass
class Route:
    template: str            # the route_budgets key
    label: str               # the template with its enum / prefix values filled
    budget_ms: Optional[int]
    fixed: dict[str, str]    # parameters filled by the route argument or an enum value
    path_params: list[str]
    query: list[dict[str, Any]]   # required query parameters from the contract
    record: bool             # needs a drawn record per request


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _param(seg: str) -> Optional[str]:
    return seg[1:-1].split(":", 1)[0] if seg.startswith("{") and seg.endswith("}") else None


def _contract(openapi: Mapping[str, Any], template: str) -> tuple[dict[str, list], list[dict]]:
    """(enum values per path parameter, required query parameters) of a route's GET."""
    plain = re.sub(r"\{(\w+):path\}", r"{\1}", template)
    get = (openapi.get("paths", {}).get(plain) or {}).get("get") or {}
    enums, query = {}, []
    for p in get.get("parameters") or ():
        schema = p.get("schema") or {}
        if p.get("in") == "path" and schema.get("enum"):
            enums[p["name"]] = list(schema["enum"])
        elif p.get("in") == "query" and p.get("required"):
            query.append({"name": p["name"], "enum": list(schema.get("enum") or ())})
    return enums, query


def routes(wanted: str, budgets: Mapping[str, Any], openapi: Mapping[str, Any]) -> list[Route]:
    """The probe's routes, each enum value its own line. ``wanted``: ``all`` or prefixes; a
    prefix that matches no route is a usage error, never an empty probe."""
    chosen: list[tuple[str, dict[str, str]]] = []
    if wanted.strip() == "all":
        chosen = [(t, {}) for t, b in budgets.items() if b.budget_ms is not None]
    else:
        for arg in [a.strip() for a in wanted.split(",") if a.strip()]:
            hits = []
            for t, b in budgets.items():
                segs, want = _segments(t), _segments(arg)
                if len(want) > len(segs):
                    continue
                fixed: dict[str, str] = {}
                for s, w in zip(segs, want):
                    name = _param(s)
                    if name is None and s != w:
                        break
                    if name is not None:
                        fixed[name] = w
                else:
                    if b.budget_ms is None:
                        raise Usage(f"{t} has no budget (family {b.family}): not a page")
                    hits.append((t, fixed))
            if not hits:
                raise Usage(f"no GET route matches {arg!r} (operator_api.route_budgets)")
            chosen += hits
    out: list[Route] = []
    for template, fixed in chosen:
        enums, query = _contract(openapi, template)
        params = [p for p in (_param(s) for s in _segments(template)) if p]
        variants = [dict(fixed)]
        for name in params:
            if name in enums and name not in fixed:
                variants = [dict(v, **{name: e}) for v in variants for e in enums[name]]
        for v in variants:
            label = "/" + "/".join(v.get(_param(s) or "", s) if _param(s) in v else s
                                   for s in _segments(template))
            record = any(p in RECORD_ID_PARAMS or p in RECORD_TYPE_PARAMS
                         or (p == "key" and v.get("kind") == "record") for p in params) \
                or any(q["name"] == "kind" and template == "/api/biz/page" for q in query)
            out.append(Route(template, label, budgets[template].budget_ms, v, params, query,
                             record))
    return out


def _snake(kind: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", kind).lower()


def fill(route: Route, kind: Optional[str], rid: Optional[str],
         given: Mapping[str, str]) -> tuple[Optional[str], Optional[str]]:
    """(path, None) or (None, the parameter nothing fills)."""
    parts = []
    for s in _segments(route.template):
        name = _param(s)
        if name is None:
            parts.append(s)
        elif name in route.fixed:
            parts.append(urllib.parse.quote(route.fixed[name], safe=""))
        elif name in RECORD_TYPE_PARAMS and kind:
            parts.append(kind)
        elif (name in RECORD_ID_PARAMS or (name == "key" and route.fixed.get("kind") == "record")) \
                and rid:
            parts.append(urllib.parse.quote(rid, safe=""))
        elif name in given:
            parts.append(urllib.parse.quote(given[name], safe=""))
        else:
            return None, name
    query = {}
    for q in route.query:
        name = q["name"]
        if name in given:
            query[name] = given[name]
        elif route.template == "/api/biz/page" and name == "kind" and kind \
                and _snake(kind) in q["enum"]:
            query[name] = _snake(kind)
            query["record"] = rid or ""
        else:
            return None, name
    path = "/" + "/".join(parts)
    return (path + ("?" + urllib.parse.urlencode(query) if query else "")), None


# ---- records ------------------------------------------------------------------------------------

def read_seen(path: Path) -> tuple[set[str], str]:
    try:
        return {x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()}, ""
    except FileNotFoundError:
        return set(), f"seen file {path} absent: 0 ids excluded"


def draw_ids(pg: Any, kinds: Sequence[str], n: int, seed: str, seen: set[str]
             ) -> dict[str, list[str]]:
    """n never-probed record ids per kind, one statement per kind (``n`` is the sample the
    caller asked for; a kind with fewer records gives what it has, and the caller says so)."""
    out: dict[str, list[str]] = {}
    with pg.cursor() as cur:
        for kind in kinds:
            cur.execute("SELECT record_id FROM biz.records WHERE type = %s AND biz "
                        "AND NOT (record_id = ANY(%s)) ORDER BY md5(record_id || %s) LIMIT %s",
                        (kind, sorted(seen), seed, n))
            out[kind] = [r[0] for r in cur.fetchall()]
    return out


# ---- sessions and requests ----------------------------------------------------------------------

@dataclass
class Session:
    name: str
    base: str
    cookie: Optional[str] = None
    view_as: Optional[str] = None
    missing: str = ""          # why this reader cannot be measured ("" when it can)

    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self.cookie:
            h["Cookie"] = f"{COOKIE}={self.cookie}"
        if self.view_as:
            h[VIEW_AS_HEADER] = self.view_as
        return h


def ticket_of(link_text: str) -> Optional[str]:
    frag = urllib.parse.urlsplit(link_text.strip()).fragment
    got = urllib.parse.parse_qs(frag)
    for key in ("ticket", "access"):
        if got.get(key):
            return got[key][0]
    return None


def sign_in(base: str, link_file: Path) -> str:
    """The session cookie of an access link file (its ticket is single use: one sign-in)."""
    ticket = ticket_of(link_file.read_text(encoding="utf-8"))
    if not ticket:
        raise Usage(f"{link_file}: no ticket in the link")
    req = urllib.request.Request(base + "/api/auth/dev-login", method="POST",
                                 data=json.dumps({"ticket": ticket}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        for value in resp.headers.get_all("Set-Cookie") or ():
            m = re.match(rf"{COOKIE}=([^;]+)", value)
            if m:
                return m.group(1)
    raise Usage(f"{link_file}: sign-in gave no session cookie")


SESSION_SECRET_ENV = "PLX_WORKBENCH_SESSION_SECRET"
TICKET_TTL_SECONDS = 3600


def snapshot_user(snapshot: Mapping[str, Any], object_id: str) -> Optional[Mapping[str, Any]]:
    """The one active Portal row of the Entra object id, else None (none, two rows, inactive:
    the reader is not measured, never guessed)."""
    rows = [u for u in snapshot.get("users") or ()
            if isinstance(u, dict) and u.get("entraObjectId") == object_id]
    return rows[0] if len(rows) == 1 and rows[0].get("isActive") is True else None


def mint_tickets(secret: str, snapshot: Mapping[str, Any], readers: Mapping[str, str],
                 ttl: int = TICKET_TTL_SECONDS) -> tuple[dict[str, str], dict[str, str]]:
    """({name: ticket}, {name: why not}) for ``readers`` {name: Entra object id}, signed with
    ``secret`` (the throwaway of this probe's child API)."""
    from operating import session_identity as si
    ok: dict[str, str] = {}
    refused: dict[str, str] = {}
    for name, oid in readers.items():
        row = snapshot_user(snapshot, oid)
        if row is None or not all(isinstance(row.get(k), str) and row[k].strip() for k in
                                  ("id", "email", "entraTenantId")):
            refused[name] = f"reader {name}: no single active Portal row for that employee"
            continue
        ok[name] = si.mint_session_ticket(
            uid=row["id"], email=row["email"], tenant_id=row["entraTenantId"], object_id=oid,
            author=si.AUTHOR_HUMAN, secret=secret, ttl_seconds=ttl)
    return ok, refused


def sign_in_ticket(base: str, ticket: str) -> str:
    """The session cookie of a ticket (single use)."""
    req = urllib.request.Request(base + "/api/auth/dev-login", method="POST",
                                 data=json.dumps({"ticket": ticket}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        for value in resp.headers.get_all("Set-Cookie") or ():
            m = re.match(rf"{COOKIE}=([^;]+)", value)
            if m:
                return m.group(1)
    raise Usage("sign-in with a minted ticket gave no session cookie")


@dataclass
class Answer:
    status: int
    ms: float
    size: int
    src: str
    body: bytes = b""


def get(session: Session, path: str, timeout: float = REQUEST_TIMEOUT_SECONDS,
        keep: bool = False) -> Answer:
    """One timed GET: from sending the request to the last byte of the body."""
    req = urllib.request.Request(session.base + path, headers=session.headers())
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body, status, src = resp.read(), resp.status, resp.headers.get("X-PLX-Src")
    except urllib.error.HTTPError as err:
        body, status, src = err.read(), err.code, err.headers.get("X-PLX-Src")
    except (TimeoutError, socket.timeout):
        return Answer(0, timeout * 1000, 0, "timeout")
    except urllib.error.URLError as err:
        if isinstance(err.reason, (TimeoutError, socket.timeout)):
            return Answer(0, timeout * 1000, 0, "timeout")
        return Answer(-1, (time.perf_counter() - t) * 1000, 0, f"unreachable:{type(err.reason).__name__}")
    ms = (time.perf_counter() - t) * 1000
    if not src:
        src = "unstated"
        if body[:1] == b"{":
            try:
                status_field = json.loads(body).get("status")
            except ValueError:
                status_field = None
            if status_field in ("stale", "not_run"):
                src = status_field
    return Answer(status, ms, len(body), src, body if keep else b"")


# ---- measuring ----------------------------------------------------------------------------------

def pct(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (a p99 of 20 samples is the largest; said, never interpolated)."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[max(0, min(len(s) - 1, int(-(-p * len(s) // 100)) - 1))]


@dataclass
class Line:
    route: str
    reader: str
    budget_ms: Optional[int]
    ms: list[float] = field(default_factory=list)
    conc_ms: list[float] = field(default_factory=list)
    first_ms: Optional[float] = None
    statuses: dict[str, int] = field(default_factory=dict)
    src: dict[str, int] = field(default_factory=dict)
    size: list[int] = field(default_factory=list)
    stmts_db_wide: Optional[float] = None
    not_measured: str = ""
    kinds_short: dict[str, int] = field(default_factory=dict)

    def add(self, a: Answer, concurrent: bool = False) -> None:
        (self.conc_ms if concurrent else self.ms).append(a.ms)
        self.statuses[str(a.status)] = self.statuses.get(str(a.status), 0) + 1
        self.src[a.src] = self.src.get(a.src, 0) + 1
        self.size.append(a.size)

    @property
    def all_ms(self) -> list[float]:
        return self.ms + self.conc_ms

    def errors(self) -> int:
        # 404 is an answer (a record this reader may not see answers as one that does not exist)
        return sum(n for s, n in self.statuses.items() if not (s.startswith("2") or s == "404"))

    def summary(self) -> dict[str, Any]:
        v = self.all_ms
        return {"route": self.route, "reader": self.reader, "budget_ms": self.budget_ms,
                "n": len(v), "p50": round(pct(v, 50), 1), "p95": round(pct(v, 95), 1),
                "p99": round(pct(v, 99), 1), "max": round(max(v), 1) if v else None,
                "conc_p99": round(pct(self.conc_ms, 99), 1) if self.conc_ms else None,
                "first_ms": None if self.first_ms is None else round(self.first_ms, 1),
                "bytes_p50": int(statistics.median(self.size)) if self.size else None,
                "statuses": self.statuses, "src": self.src,
                "stmts_db_wide": self.stmts_db_wide, "not_measured": self.not_measured,
                "kinds_short": self.kinds_short}

    def failures(self, scale: float) -> list[str]:
        if self.not_measured:
            return [f"not_measured ({self.not_measured})"]
        why = []
        budget = None if self.budget_ms is None else self.budget_ms * scale
        if budget is not None and self.all_ms and pct(self.all_ms, 99) > budget:
            why.append("over_budget")
        if budget is not None and self.first_ms is not None and self.first_ms > budget:
            why.append(f"first_ms {self.first_ms:.0f} over budget")
        if self.src.get("not_run"):
            why.append(f"not_run {self.src['not_run']}")
        if self.errors():
            why.append("errors " + ",".join(f"{s}x{n}" for s, n in sorted(self.statuses.items())
                                             if not (s.startswith("2") or s == "404")))
        return why


def _stmt_calls(pg: Any) -> Optional[int]:
    if pg is None:
        return None
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT sum(calls)::bigint FROM pg_stat_statements")
            return int(cur.fetchone()[0] or 0)
    except Exception as err:  # noqa: BLE001 - the extension is optional: said, not judged
        pg.rollback()
        print(f"stmts_db_wide not measured: {type(err).__name__}", file=sys.stderr)
        return None


def probe(route_list: Sequence[Route], sessions: Sequence[Session], ids: Mapping[str, list[str]],
          *, n: int, given: Mapping[str, str], view_as_routes: Iterable[str] = (),
          fresh: bool = False, pg: Any = None, equiv: Optional[Callable] = None,
          getter: Callable[..., Answer] = get) -> list[Line]:
    view_as_ok = frozenset(view_as_routes)
    kinds = [k for k in ids if ids[k]] or [None]
    out: list[Line] = []
    stmts_ok = pg is not None
    for route in route_list:
        for sess in sessions:
            line = Line(route.label, sess.name, route.budget_ms)
            out.append(line)
            if sess.missing:
                line.not_measured = sess.missing
                continue
            if sess.view_as and route.template not in view_as_ok:
                line.not_measured = "view-as does not serve this route"
                continue
            paths: list[str] = []
            for i in range(n + CONCURRENT + (1 if fresh else 0)):
                kind = kinds[i % len(kinds)] if route.record else None
                pool = ids.get(kind or "", []) if route.record else []
                if route.record and not pool:
                    line.not_measured = "no records drawn for any kind"
                    break
                rid = pool[(i // len(kinds)) % len(pool)] if pool else None
                path, missing = fill(route, kind, rid, given)
                if path is None:
                    line.not_measured = f"no value for {{{missing}}}"
                    break
                paths.append(path)
            if line.not_measured:
                continue
            for k in kinds if route.record else ():
                if len(ids.get(k or "", [])) < n:
                    line.kinds_short[str(k)] = len(ids.get(k or "", []))
            if fresh:
                line.first_ms = getter(sess, paths.pop(0)).ms
            sequential, together = paths[:n], paths[n:]  # all n + CONCURRENT are requested
            before = _stmt_calls(pg) if stmts_ok else None
            for path in sequential:
                a = getter(sess, path, keep=equiv is not None)
                line.add(a)
                if equiv is not None:
                    equiv(sess, path, a)
            after = _stmt_calls(pg) if stmts_ok else None
            if before is not None and after is not None and n:
                line.stmts_db_wide = round((after - before) / n, 1)
            with ThreadPoolExecutor(CONCURRENT) as pool_:
                for a in pool_.map(lambda p: getter(sess, p), together):
                    line.add(a, concurrent=True)
    return out


# ---- the result ---------------------------------------------------------------------------------

def load_of_host() -> tuple[float, int]:
    return os.getloadavg()[0], os.cpu_count() or 1


def verdict(lines: Sequence[Line], *, load: Optional[float], cores: int, inline_door: Optional[int],
            scale: float = 1.0, notes: Sequence[str] = ()) -> tuple[int, list[str]]:
    """(exit code, output lines): PASS / FAIL / INVALID exactly as the module says."""
    if load is None:
        return 3, ["COLD PROBE INVALID load=unknown (the API is not on this host)"]
    if load > cores:
        return 3, [f"COLD PROBE INVALID load={load:.1f} cores={cores}"]
    requests = sum(len(l.all_ms) + (l.first_ms is not None) for l in lines)
    measured = [l for l in lines if not l.not_measured and l.all_ms]
    worst = max(measured, key=lambda l: pct(l.all_ms, 99), default=None)
    over = sum(1 for l in lines if "over_budget" in l.failures(scale))
    not_run = sum(l.src.get("not_run", 0) for l in lines)
    failing = [(l, l.failures(scale)) for l in lines]
    failing = [(l, w) for l, w in failing if w]
    door = "unmeasured" if inline_door is None else str(inline_door)
    head = (f"routes={len({l.route for l in lines})} requests={requests} "
            f"worst={worst.route if worst else '-'} "
            f"p99={pct(worst.all_ms, 99) if worst else 0:.0f}")
    if not failing and inline_door == 0 and lines:
        return 0, [f"COLD PROBE PASS {head} over_budget=0 not_run=0 inline_door=0 "
                   f"load={load:.1f}", *notes]
    out = [f"COLD PROBE FAIL {head} over_budget={over} not_run={not_run} inline_door={door} "
           f"load={load:.1f}"]
    if inline_door is None:
        out.append("FAIL inline_door not measured: no API log (--fresh-process or --server-log-cmd)")
    elif inline_door:
        out.append(f"FAIL inline_door={inline_door}: a request decided a door ({DOOR_MISS})")
    if not lines:
        out.append("FAIL no route was probed")
    for l, why in sorted(failing, key=lambda x: -pct(x[0].all_ms, 99)):
        s = l.summary()
        out.append(f"FAIL {l.route} reader={l.reader} p99={s['p99']:.0f} "
                   f"budget={l.budget_ms} n={s['n']} first_ms={s['first_ms']} why={'; '.join(why)}")
    return 1, out + list(notes)


# ---- the fresh process --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fresh:
    """An ``operator_api serve`` of the code under test, started for this probe on a free
    loopback port with the caller's environment (its settings and the session secret are the
    caller's to give, as for the release it stands for), its output kept to count door misses."""

    def __init__(self, cmd: str, env: Optional[Mapping[str, str]] = None) -> None:
        self.port = free_port()
        self.log = tempfile.NamedTemporaryFile(prefix="cold-probe-api-", suffix=".log", delete=False)
        argv = shlex.split(cmd.replace("{port}", str(self.port)))
        self.proc = subprocess.Popen(argv, stdout=self.log, stderr=subprocess.STDOUT,
                                     env=None if env is None else {**os.environ, **env})
        self.base = f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout: float = READY_TIMEOUT_SECONDS) -> float:
        t = time.monotonic()
        while time.monotonic() - t < timeout:
            if self.proc.poll() is not None:
                raise Usage(f"the API under test exited {self.proc.returncode} before ready "
                            f"(log {self.log.name})")
            try:
                with urllib.request.urlopen(self.base + "/api/ready", timeout=5) as r:
                    if r.status == 200:
                        return time.monotonic() - t
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)
        raise Usage(f"the API under test was not ready within {timeout:.0f} s (log {self.log.name})")

    def door_misses(self) -> int:
        return Path(self.log.name).read_text(encoding="utf-8", errors="replace").count(DOOR_MISS)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def log_misses(cmd: str, since: float) -> int:
    got = subprocess.run(cmd.replace("{since}", str(int(since))), shell=True, check=True,
                         capture_output=True, text=True, timeout=120)
    return got.stdout.count(DOOR_MISS)


# ---- equivalence of two APIs (``--equiv``) ------------------------------------------------------

def canonical(value: Any) -> Any:
    """Order-free form of a JSON value: lists of objects sorted by their JSON text."""
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return sorted((canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def differences(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    """Field-by-field differences of two order-normalised JSON values."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in sorted(set(old) | set(new)):
            if k not in new:
                out.append({"at": f"{path}.{k}", "old": old[k], "new": "(absent)"})
            elif k not in old:
                out.append({"at": f"{path}.{k}", "old": "(absent)", "new": new[k]})
            else:
                out += differences(old[k], new[k], f"{path}.{k}")
        return out
    return [] if old == new else [{"at": path or ".", "old": old, "new": new}]


# ---- command line -------------------------------------------------------------------------------

def _pairs(values: Sequence[str], what: str) -> dict[str, str]:
    out = {}
    for v in values:
        k, sep, rest = v.partition("=")
        if not sep or not k or not rest:
            raise Usage(f"{what} takes NAME=VALUE, got {v!r}")
        out[k] = rest
    return out


def sessions_for(base: str, readers: Sequence[str], links: Mapping[str, str],
                 view_as: Mapping[str, str], signer: Callable[[str, Path], str] = sign_in,
                 tickets: Optional[Mapping[str, str]] = None,
                 refused: Optional[Mapping[str, str]] = None,
                 ticket_signer: Callable[[str, str], str] = sign_in_ticket) -> list[Session]:
    """``tickets`` (minted for this probe's own API): the readers they name sign in with them,
    and only they and ``view_as`` readers can be measured (the real secret's links do not
    verify there)."""
    out, owner_cookie = [], None
    for name in readers:
        s = Session(name, base)
        if tickets is not None and name in tickets:
            s.cookie = ticket_signer(base, tickets[name])
            if name == "owner":
                owner_cookie = s.cookie
        elif tickets is not None and name not in view_as:
            s.missing = (refused or {}).get(name) or (
                f"reader {name} has no --reader-id (a link made with the real secret does "
                "not verify in this probe's own API)")
        elif tickets is None and (name in links or (name == "owner" and MEASURE_LINK.exists())):
            s.cookie = signer(base, Path(links.get(name) or MEASURE_LINK))
            if name == "owner":
                owner_cookie = s.cookie
        elif name in view_as:
            s.view_as = view_as[name]
        else:
            s.missing = f"reader {name} has no session (--session or --view-as)"
        out.append(s)
    for s in out:
        if s.view_as:
            if owner_cookie is None:
                s.missing, s.view_as = "view-as needs the owner's session", None
            else:
                s.cookie = owner_cookie
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 tools/cold_probe.py",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--routes", required=True)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--readers", default="owner")
    ap.add_argument("--kinds", default=",".join(KINDS))
    ap.add_argument("--dsn", default=os.environ.get("PLX_PG_DSN", "postgresql:///plx"))
    ap.add_argument("--base", default="")
    ap.add_argument("--fresh-process", action="store_true")
    ap.add_argument("--serve-cmd", default=f"{shlex.quote(sys.executable)} -m operator_api serve "
                                           "--bind 127.0.0.1 --port {port} "
                                           + os.environ.get("PLX_PROBE_SERVE_ARGS", ""))
    ap.add_argument("--server-log-cmd", default="")
    ap.add_argument("--deploy-gate", action="store_true",
                    help="probe --base: a just-built release's API that serves no reader yet")
    ap.add_argument("--session", action="append", default=[], help="NAME=ACCESS_LINK_FILE")
    ap.add_argument("--view-as", action="append", default=[], help="NAME=VIEW_AS_KEY")
    ap.add_argument("--param", action="append", default=[], help="NAME=VALUE")
    ap.add_argument("--seen", type=Path, default=SEEN_FILE)
    ap.add_argument("--seed", default="")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--budget-scale", type=float, default=1.0)
    ap.add_argument("--equiv", default="", help="OLD_BASE: compare each answer with this API's")
    ap.add_argument("--reader-id", action="append", default=[], help="NAME=ENTRA_OBJECT_ID")
    ap.add_argument("--host-config", type=Path, default=None)
    ap.add_argument("--old-session", action="append", default=[], help="NAME=ACCESS_LINK_FILE")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except Usage as err:
        print(f"COLD PROBE USAGE {err}", file=sys.stderr)
        return 2


def run(args: argparse.Namespace) -> int:
    from operator_api.route_budgets import ROUTES
    from operator_api.view_as import ROUTES as VIEW_AS_ROUTES
    if args.n < 1:
        raise Usage("--n is at least 1")
    if not args.fresh_process and not args.deploy_gate:
        raise Usage("--fresh-process (an API of its own, on a Spark): a probe never sends its "
                    "8 concurrent requests to the API people use; --deploy-gate is the release's "
                    "own probe of an API no reader is routed to yet")
    route_list = routes(args.routes, ROUTES, json.loads(OPENAPI_FILE.read_text(encoding="utf-8")))
    readers = [r.strip() for r in args.readers.split(",") if r.strip()]
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    links, view_as, given = (_pairs(args.session, "--session"), _pairs(args.view_as, "--view-as"),
                             _pairs(args.param, "--param"))
    notes: list[str] = []
    seen, said = read_seen(args.seen)
    if said:
        notes.append(said)
    seed = args.seed or str(time.time_ns())
    pg = None
    ids: dict[str, list[str]] = {}
    if any(r.record for r in route_list):
        import psycopg
        pg = psycopg.connect(args.dsn, autocommit=True)
        if not pg.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
            pg.close()
            raise Usage("--dsn is the primary: records are drawn from the standby (the box "
                        "serves people; a probe reads elsewhere)")
        ids = draw_ids(pg, kinds, args.n + CONCURRENT, seed, seen)
        notes += [f"kind {k}: {len(v)} of {args.n + CONCURRENT} ids drawn" for k, v in ids.items()
                  if len(v) < args.n + CONCURRENT]
    tickets, refused, child_env = None, {}, None
    if args.reader_id:
        if not args.fresh_process or args.equiv:
            raise Usage("--reader-id mints tickets for this probe's own API: it needs "
                        "--fresh-process and no --equiv (the old API holds another secret)")
        if args.host_config is None:
            raise Usage("--reader-id needs --host-config (the Portal access snapshot's file)")
        import secrets
        from operating.host import HostConfig
        config = HostConfig.from_file(args.host_config)
        if config.portal_access is None:
            raise Usage("the host config names no Portal access snapshot")
        snapshot = json.loads(Path(config.portal_access).read_text(encoding="utf-8"))
        secret = secrets.token_hex(32)
        tickets, refused = mint_tickets(secret, snapshot, _pairs(args.reader_id, "--reader-id"))
        child_env = {SESSION_SECRET_ENV: secret}   # the child's environment only
        notes.append(f"minted {len(tickets)} tickets for this probe's own API only")
    fresh = Fresh(args.serve_cmd, child_env) if args.fresh_process else None
    started = time.time()
    try:
        if fresh is not None:
            notes.append(f"fresh API ready in {fresh.wait_ready():.1f} s")
            base = fresh.base
        elif args.base:
            base = args.base.rstrip("/")
        else:
            raise Usage("--base or --fresh-process")
        host = urllib.parse.urlsplit(base).hostname or ""
        local = host in ("127.0.0.1", "localhost", "::1")
        load0, cores = load_of_host()
        sessions = sessions_for(base, readers, links, view_as, tickets=tickets, refused=refused)
        equiv_diffs: list[dict[str, Any]] = []
        compare = None
        if args.equiv:
            old = {s.name: s for s in sessions_for(args.equiv.rstrip("/"), readers,
                                                   _pairs(args.old_session, "--old-session"),
                                                   view_as)}

            def compare(sess: Session, path: str, new: Answer) -> None:
                o = old.get(sess.name)
                if o is None or o.missing:
                    equiv_diffs.append({"reader": sess.name, "path": path, "old_failed": True})
                    return
                was = get(o, path, keep=True)
                if was.status != new.status:
                    equiv_diffs.append({"reader": sess.name, "path": path,
                                        "diff": [{"at": "status", "old": was.status,
                                                  "new": new.status}]})
                    return
                try:
                    d = differences(canonical(json.loads(was.body or b"null")),
                                    canonical(json.loads(new.body or b"null")))
                except ValueError:
                    d = [] if was.body == new.body else [{"at": "body", "old": "bytes", "new": "bytes"}]
                if d:
                    equiv_diffs.append({"reader": sess.name, "path": path, "diff": d})
        lines = probe(route_list, sessions, ids, n=args.n, given=given,
                      view_as_routes=VIEW_AS_ROUTES, fresh=fresh is not None, pg=pg,
                      equiv=compare)
        load1, _ = load_of_host()
        if fresh is not None:
            inline = fresh.door_misses()
        elif args.server_log_cmd:
            inline = log_misses(args.server_log_cmd, started)
        else:
            inline = None
    finally:
        if fresh is not None:
            fresh.stop()
        if pg is not None:
            pg.close()
    probed = [i for v in ids.values() for i in v]
    try:
        with args.seen.open("a", encoding="utf-8") as fh:
            fh.write("".join(f"{i}\n" for i in probed))
    except OSError as err:
        notes.append(f"seen file {args.seen} not written ({type(err).__name__}): the next run "
                     f"may draw these {len(probed)} ids again")
    if args.equiv:
        notes.insert(0, f"different={len(equiv_diffs)}")
    code, out = verdict(lines, load=max(load0, load1) if local else None, cores=cores,
                        inline_door=inline, scale=args.budget_scale, notes=notes)
    print("\n".join(out), flush=True)
    if args.json:
        args.json.write_text(json.dumps({"result": out[0], "lines": [l.summary() for l in lines],
                                         "equiv": equiv_diffs, "seed": seed,
                                         "started": started}, indent=1, default=str),
                             encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
