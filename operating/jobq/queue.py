"""The job queue in Postgres: schema, enqueue, admission and claims, leases, retries, the
dead letter, preemption marks and the status read.

States: ``queued`` -> ``running`` -> ``done`` | ``failed`` (an inline job that failed: its
caller has the outcome) | ``queued`` again (a unit job's retry, after backoff) | ``dead``
(a unit job out of attempts: the dead letter). ``running`` -> ``preempting`` -> ``queued``
(no attempt spent). ``queued`` -> ``cancelled`` (a waiter that died, or by hand). Every
transition names the state it leaves and, for a held job, its owner (a fenced update: a
reaped owner's late finish changes nothing).

Admission (``try_admit``) runs in one transaction under ``pg_try_advisory_xact_lock``: the
queued rows due now, ``FOR UPDATE SKIP LOCKED``, in priority order. The first one whose
class has a free slot is the head; it starts only when
``MemAvailable - outstanding - need >= floor`` and the declared CPUs fit the budget, where
``outstanding`` is what the running jobs (and active class units the queue did not start)
may still take: ``max(0, need - memory.current)``. A head that does not fit holds every
lower job back except a short one (its class declares a ``max_runtime_s`` under
``classes.SHORT_RUNTIME_S``: landings, test runs), which still starts when it fits beside
the floor: a delta that cannot fit never holds them for good, and the
ops alarm ``jobq_head_wait`` opens when a head has waited over 20 minutes. Backups and
alerts keep waiting behind it. A due job that fits but is not the
caller's to take (another waiter's, or a unit job for the worker) is reserved and the
pass goes on, so several jobs start together when they all fit.

What a job declares: its class gives the defaults, and the caller (``tools/job.py``,
``tools/testslot.py``) may declare the job's own cores, memory and peak disk, and names the
workspace that started it. Three more checks then hold a job back, each a refusal with its
own reason prefix:

- ``workspaces:`` a job of a class in the ``workspaces`` pool fits only when the declared
  cores and memory of the pool's held jobs plus its own stay within ``workspace_budget``
  (the cores and memory of ``plx-workspaces.slice``: ``PLX_JOBQ_WORKSPACE_CPUS``,
  ``PLX_JOBQ_WORKSPACE_MEM_GB``),
  less the share a ``reserve`` class (the lander) keeps while none of it is held: cores are
  counted, not runs, so fifteen 16-worker test runs can no longer start at once;
- ``disk:`` a job declaring disk fits only when its file system's free space, less what
  the held jobs on that file system declared, keeps ``ops_health.disk_floor_bytes`` free;
- ``workspace:`` a workspace holds at most ``PLX_JOBQ_WORKSPACE_MAX`` (default 2) jobs at
  once, landings aside (``status()["workspaces"]`` shows each workspace's load).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import random
from typing import Any, Callable, Iterable, Mapping

from . import classes as jc
from . import hygiene

ADMIT_LOCK = 0x6A6F6271  # "jobq"
LEASE_S = 60.0
FLOOR_ENV = "PLX_JOBQ_FLOOR_GB"
CPU_ENV = "PLX_JOBQ_CPU_BUDGET"
DSN_ENV = "PLX_JOBQ_DSN"
DEFAULT_DSN = "dbname=plx_jobq"
DEFAULT_FLOOR_GB = 4.0
WORKSPACE_CPUS_ENV = "PLX_JOBQ_WORKSPACE_CPUS"
WORKSPACE_MEM_ENV = "PLX_JOBQ_WORKSPACE_MEM_GB"
WORKSPACE_MAX_ENV = "PLX_JOBQ_WORKSPACE_MAX"
DEFAULT_WORKSPACE_MEM_GB = 26.0   # plx-workspaces.slice MemoryHigh (MemoryMax 28G)
HOST_CORES = 2               # cores outside plx-workspaces.slice (AllowedCPUs): Postgres, the API
DEFAULT_WORKSPACE_MAX = 2
DEFAULT_DISK_PATH = "/data"
MAX_PREEMPTIONS = 3
REFUSAL_EVERY_S = 60   # a waiting job's refusal count grows at most once a minute per reason
CHANNEL = "jobq"
HELD = ("running", "preempting")
TERMINAL = ("done", "failed", "dead", "cancelled")


COLUMNS = ("id, class, kind, unit, idem_key, label, priority, mem_mb, cpus, state, attempts, "
           "max_attempts, preemptions, run_after, enqueued_at, started_at, finished_at, owner, "
           "lease_until, host, pid, pid_start, pgid, invocation, exit_code, result, last_error, "
           "refusals, last_refusal, last_refusal_at, waited_s, disk_mb, disk_path, workspace")


def dsn(environ: Mapping[str, str] = os.environ) -> str:
    return environ.get(DSN_ENV) or DEFAULT_DSN


def floor_mb(environ: Mapping[str, str] = os.environ) -> int:
    try:
        value = float(environ.get(FLOOR_ENV, ""))
    except ValueError:
        value = -1
    return int((value if value >= 0 else DEFAULT_FLOOR_GB) * 1024)


def cpu_budget(environ: Mapping[str, str] = os.environ) -> float:
    try:
        value = float(environ.get(CPU_ENV, ""))
    except ValueError:
        value = 0
    return value if value > 0 else 2.0 * (os.cpu_count() or 1)


def _positive(environ: Mapping[str, str], name: str) -> float | None:
    try:
        value = float(environ.get(name, ""))
    except ValueError:
        return None
    return value if value > 0 else None


def workspace_budget(environ: Mapping[str, str] = os.environ) -> tuple[float, int]:
    """(cores, memory MB) of the workspaces pool: ``plx-workspaces.slice``'s AllowedCPUs and
    MemoryHigh unless the environment says otherwise."""
    cpus = (_positive(environ, WORKSPACE_CPUS_ENV)
            or float(max(1, (os.cpu_count() or 1) - HOST_CORES)))
    mem = _positive(environ, WORKSPACE_MEM_ENV) or DEFAULT_WORKSPACE_MEM_GB
    return cpus, int(mem * 1024)


def workspace_ceiling(cls: jc.JobClass, classes: Mapping[str, jc.JobClass],
                 environ: Mapping[str, str] = os.environ) -> tuple[float, int]:
    """The most cores and memory MB one job of workspaces class `cls` can ever be admitted with:
    the pool, less the reserve classes' share unless `cls` is one (a bigger declaration
    would wait for good and hold the workspace jobs behind it)."""
    cpus, mem = workspace_budget(environ)
    if not cls.reserve:
        for other in classes.values():
            if other.pool == "workspaces" and other.reserve:
                cpus -= other.cpus
                mem -= other.mem_mb
    return cpus, mem


def workspace_max(environ: Mapping[str, str] = os.environ) -> int:
    value = _positive(environ, WORKSPACE_MAX_ENV)
    return int(value) if value else DEFAULT_WORKSPACE_MAX


def disk_floor_mb(mount: str, environ: Mapping[str, str] = os.environ) -> int:
    """The floor of mount point `mount` (``ops_health.DISK_FLOOR_GB``), in MB."""
    from operating import disk_floor
    return disk_floor.mount_floor_bytes(mount, environ) >> 20


class Probe:
    """What admission reads from the machine; tests give their own."""

    def mem_available_mb(self) -> int:
        return hygiene.mem_available_mb()

    def unit_memory_mb(self, unit: str) -> int | None:
        return hygiene.unit_memory_mb(unit)

    def disk_free_mb(self, path: str) -> int:
        import shutil
        return shutil.disk_usage(path).free >> 20

    def mount(self, path: str) -> str:
        from operating import disk_floor
        return disk_floor.mount_of(path)


@dataclass
class Decision:
    """One admission pass: the job claimed for the caller (or None) and, when the head did
    not fit, the head and why."""
    claimed: dict[str, Any] | None = None
    blocked: dict[str, Any] | None = None
    reason: str | None = None
    busy: bool = False
    headroom_mb: int | None = None
    reserved: list[int] = field(default_factory=list)


class Lost(Exception):
    """The job is no longer this owner's (reaped, cancelled or finished elsewhere)."""


class Queue:
    def __init__(self, conninfo: str | None = None, *, classes: Mapping[str, jc.JobClass] | None = None,
                 probe: Probe | None = None, lease_s: float = LEASE_S,
                 environ: Mapping[str, str] = os.environ, connect_timeout: int = 10):
        import psycopg
        from psycopg.rows import dict_row
        self.conninfo = conninfo or dsn(environ)
        self.classes = dict(classes) if classes is not None else jc.load(environ)
        self.probe = probe or Probe()
        self.lease_s = lease_s
        self.floor_mb = floor_mb(environ)
        self.cpu_budget = cpu_budget(environ)
        self.workspace_cpus, self.workspace_mem_mb = workspace_budget(environ)
        self.workspace_max = workspace_max(environ)
        self.environ = environ
        self.host = os.uname().nodename
        self.conn = psycopg.connect(self.conninfo, autocommit=True, row_factory=dict_row,
                                    connect_timeout=connect_timeout,
                                    application_name="plx-jobq")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Queue":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ schema
    def init(self) -> None:
        """Apply the queue database's pending migrations (``operating/jobq/migrations``)."""
        from . import schema
        schema.apply(self.conn)

    def _event(self, job_id: int | None, kind: str, detail: str | None = None) -> None:
        self.conn.execute("INSERT INTO jobq.events (job_id, kind, detail) VALUES (%s, %s, %s)",
                          (job_id, kind, detail))

    def _notify(self) -> None:
        self.conn.execute(f"NOTIFY {CHANNEL}")

    # ------------------------------------------------------------------ enqueue
    def enqueue(self, class_name: str, *, key: str | None = None, label: str | None = None,
                kind: str = "unit", once: bool = False, waiter_pid: int | None = None,
                mem_gb: float | None = None, cpus: float | None = None,
                disk_gb: float | None = None, disk_path: str | None = None,
                workspace: str | None = None) -> tuple[int | None, str]:
        """(job id, what happened): ``queued``, ``coalesced`` (a job of the key was already
        queued: that one's id; an inline caller must not run it), ``done_once`` (``once``
        and the key already succeeded: no id). ``kind='unit'`` needs a class with a unit.
        `mem_gb`, `cpus`, `disk_gb` (on `disk_path`, default ``/data``) declare this job's own
        need over its class's defaults; `workspace` names the workspace that started it."""
        cls = jc.get(class_name, self.classes)
        if kind not in ("unit", "inline"):
            raise ValueError(f"unknown job kind {kind!r}")
        if kind == "unit" and not cls.unit:
            raise ValueError(f"class {class_name!r} has no unit to start")
        idem = key or (class_name if kind == "unit" else f"{class_name}:{os.getpid()}:{random.getrandbits(48):x}")
        pid = waiter_pid if kind == "inline" else None
        need_cpus = cls.cpus if cpus is None else float(cpus)
        need_mem = cls.mem_mb if mem_gb is None else int(float(mem_gb) * 1024)
        need_disk = 0 if not disk_gb else int(float(disk_gb) * 1024)
        if need_cpus < 0 or need_mem < 0 or need_disk < 0:
            raise ValueError("a job's declared cores, memory and disk are never negative")
        if cls.pool == "workspaces":
            most_cpus, most_mem = workspace_ceiling(cls, self.classes, self.environ)
            if need_cpus > most_cpus:
                raise ValueError(f"{need_cpus:g} cores is more than the {most_cpus:g} a {cls.name}"
                                 " job of the workspaces pool can ever be admitted with")
            if need_mem > most_mem:
                raise ValueError(f"{need_mem} MB is more than the {most_mem} MB a {cls.name}"
                                 " job of the workspaces pool can ever be admitted with")
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (idem,))
            if once and self.conn.execute(
                    "SELECT 1 FROM jobq.jobs WHERE idem_key = %s AND state = 'done' LIMIT 1",
                    (idem,)).fetchone():
                return None, "done_once"
            row = self.conn.execute(
                "INSERT INTO jobq.jobs (class, kind, unit, idem_key, label, priority, mem_mb, cpus,"
                " state, max_attempts, run_after, host, pid, pid_start, lease_until, disk_mb,"
                " disk_path, workspace)"
                " SELECT %(class)s, %(kind)s, %(unit)s, %(key)s, %(label)s, %(prio)s, %(mem)s,"
                " %(cpus)s, 'queued', %(att)s,"
                " greatest(now(), coalesce((SELECT max(finished_at) FROM jobq.jobs"
                "   WHERE idem_key = %(key)s AND state = 'done'), '-infinity')"
                "   + make_interval(secs => %(cool)s)),"
                " %(host)s, %(pid)s, %(start)s,"
                " CASE WHEN %(kind)s = 'inline' THEN now() + make_interval(secs => %(lease)s) END,"
                " %(disk)s, %(disk_path)s, %(workspace)s"
                " ON CONFLICT (idem_key) WHERE state = 'queued' DO NOTHING RETURNING id",
                {"class": cls.name, "kind": kind, "unit": cls.unit if kind == "unit" else None,
                 "key": idem, "label": label, "prio": cls.priority, "mem": need_mem,
                 "cpus": need_cpus, "disk": need_disk,
                 "disk_path": (disk_path or DEFAULT_DISK_PATH) if need_disk else None,
                 "workspace": workspace, "att": cls.max_attempts if kind == "unit" else 1,
                 "cool": cls.cooldown_s, "host": self.host, "pid": pid,
                 "start": hygiene.process_start(pid) if pid else None,
                 "lease": self.lease_s}).fetchone()
            if row is None:
                existing = self.conn.execute(
                    "SELECT id FROM jobq.jobs WHERE idem_key = %s AND state = 'queued'",
                    (idem,)).fetchone()
                return (existing["id"] if existing else None), "coalesced"
            self._event(row["id"], "enqueued", f"{cls.name} {kind}")
            self._notify()
            return row["id"], "queued"

    # ------------------------------------------------------------------ admission
    def _outstanding(self, running: list[dict[str, Any]]) -> tuple[int, float, dict[str, int], set[str]]:
        """What the held jobs and the unmanaged active class units may still take:
        (memory MB, CPUs, held per class, held keys)."""
        mem, cpus, per_class, keys, units = 0, 0.0, {}, set(), set()
        for job in running:
            current = self.probe.unit_memory_mb(job["unit"]) if job["unit"] else None
            mem += max(0, job["mem_mb"] - (current or 0))
            cpus += job["cpus"]
            per_class[job["class"]] = per_class.get(job["class"], 0) + 1
            keys.add(job["idem_key"])
            if job["unit"]:
                units.add(job["unit"])
        for cls in self.classes.values():
            if not cls.unit or cls.unit in units:
                continue
            current = self.probe.unit_memory_mb(cls.unit)
            if current is None:
                continue
            mem += max(0, cls.mem_mb - current)
            cpus += cls.cpus
            per_class[cls.name] = per_class.get(cls.name, 0) + 1
            keys.add(cls.name)  # a unit job's default key is its class
        return mem, cpus, per_class, keys

    def _pool_load(self, running: list[dict[str, Any]]) -> dict[str, Any]:
        """The workspaces pool's declared cores and memory held, the reserve classes held, the
        jobs held per workspace and the declared disk held per file system."""
        load = {"cpus": 0.0, "mem": 0, "reserve_held": set(), "workspaces": {}, "disk": {}}
        for job in running:
            self._count(load, job)
        return load

    def _count(self, load: dict[str, Any], job: Mapping[str, Any]) -> None:
        cls = self.classes.get(job["class"])
        if cls is not None and cls.pool == "workspaces":
            load["cpus"] += job["cpus"]
            load["mem"] += job["mem_mb"]
            if cls.reserve:
                load["reserve_held"].add(cls.name)
        if job.get("workspace") and not (cls and cls.reserve):
            load["workspaces"][job["workspace"]] = load["workspaces"].get(job["workspace"], 0) + 1
        if job.get("disk_mb"):
            mount = self._mount(job.get("disk_path") or DEFAULT_DISK_PATH)
            load["disk"][mount] = load["disk"].get(mount, 0) + job["disk_mb"]

    def _mount(self, path: str) -> str:
        try:
            return self.probe.mount(path)
        except OSError:
            return path

    def _declared_refusal(self, job: Mapping[str, Any], load: dict[str, Any]
                          ) -> tuple[str | None, bool]:
        """Why the workspaces pool, the disk or the job's workspace holds `job` back (None:
        it fits), and whether the refusal blocks the jobs behind it (the workspaces pool) or
        only this one (a full disk, a workspace at its cap: other jobs still start)."""
        cls = self.classes.get(job["class"])
        workspace = job.get("workspace")
        held_here = load["workspaces"].get(workspace, 0) if workspace else 0
        if workspace and not (cls and cls.reserve) and held_here >= self.workspace_max:
            return (f"workspace: {workspace} already holds {held_here} of its "
                    f"{self.workspace_max} jobs at once", False)
        if job.get("disk_mb"):
            path = job.get("disk_path") or DEFAULT_DISK_PATH
            mount = self._mount(path)
            try:
                free = self.probe.disk_free_mb(path)
            except OSError:
                free = None
            floor = disk_floor_mb(mount, self.environ)
            held = load["disk"].get(mount, 0)
            if free is not None and free - held - job["disk_mb"] < floor:
                return (f"disk: {free} MB free on {mount}, {held} MB declared by held jobs, "
                        f"needs {job['disk_mb']} MB above a {floor} MB floor", False)
        if cls is not None and cls.pool == "workspaces":
            keep_cpus = keep_mem = 0.0
            if not cls.reserve:
                for other in self.classes.values():
                    if other.pool == "workspaces" and other.reserve \
                            and other.name not in load["reserve_held"]:
                        keep_cpus += other.cpus
                        keep_mem += other.mem_mb
            if load["cpus"] + job["cpus"] > self.workspace_cpus - keep_cpus:
                return (f"workspaces: {load['cpus']:g} of {self.workspace_cpus:g} workspace cores "
                        f"declared by held jobs, {keep_cpus:g} kept for the lander, needs {job['cpus']:g}",
                        True)
            if load["mem"] + job["mem_mb"] > self.workspace_mem_mb - keep_mem:
                return (f"workspaces: {load['mem']} of {self.workspace_mem_mb} MB workspace memory "
                        f"declared by held jobs, {int(keep_mem)} MB kept for the lander, needs "
                        f"{job['mem_mb']} MB", True)
        return None, False

    def try_admit(self, owner: str, take: Callable[[dict[str, Any]], bool],
                  renew: Iterable[int] = (), pid: int | None = None) -> Decision:
        """One admission pass for `owner` (module docstring). `take(job)` says whether the
        caller runs that job; `renew` are the caller's own queued inline rows, whose waiter
        lease this pass renews. Queued inline rows whose waiter's lease lapsed are cancelled
        first (a dead waiter never blocks the head)."""
        decision = Decision()
        with self.conn.transaction():
            if not self.conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok",
                                     (ADMIT_LOCK,)).fetchone()["ok"]:
                decision.busy = True
                return decision
            renew = list(renew)
            if renew:
                self.conn.execute(
                    "UPDATE jobq.jobs SET lease_until = now() + make_interval(secs => %s)"
                    " WHERE id = ANY(%s) AND state = 'queued'", (self.lease_s, renew))
            for gone in self.conn.execute(
                    "UPDATE jobq.jobs SET state = 'cancelled', finished_at = now(),"
                    " last_error = 'waiter gone' WHERE state = 'queued' AND kind = 'inline'"
                    " AND lease_until < now() RETURNING id").fetchall():
                self._event(gone["id"], "cancelled", "waiter gone")
            running = self.conn.execute(
                f"SELECT {COLUMNS} FROM jobq.jobs WHERE state IN ('running', 'preempting')"
            ).fetchall()
            queued = self.conn.execute(
                f"SELECT {COLUMNS} FROM jobq.jobs WHERE state = 'queued' AND run_after <= now()"
                " ORDER BY priority, enqueued_at, id LIMIT 500 FOR UPDATE SKIP LOCKED").fetchall()
            avail = self.probe.mem_available_mb()
            outstanding, cpus, per_class, keys = self._outstanding(running)
            load = self._pool_load(running)
            decision.headroom_mb = avail - outstanding - self.floor_mb
            for job in queued:
                cls = self.classes.get(job["class"])
                limit = cls.max_concurrent if cls else 1
                if per_class.get(job["class"], 0) >= limit or job["idem_key"] in keys:
                    continue
                if decision.blocked is not None and not (cls and cls.short):
                    continue          # behind a blocked head only short jobs may start
                short_mem = avail - outstanding - job["mem_mb"] < self.floor_mb
                short_cpu = cpus + job["cpus"] > self.cpu_budget
                blocks = True
                if short_mem or short_cpu:
                    reason = (f"memory: {avail} MB available, {outstanding} MB promised to "
                              f"running and earlier admitted jobs, needs {job['mem_mb']} MB above a "
                              f"{self.floor_mb} MB floor" if short_mem else
                              f"cpu: {cpus:g} of {self.cpu_budget:g} declared CPUs in use, "
                              f"needs {job['cpus']:g}")
                else:
                    reason, blocks = self._declared_refusal(job, load)
                if reason is not None:
                    self._refuse(job, reason)
                    if blocks and decision.blocked is None:
                        decision.blocked, decision.reason = job, reason
                    elif not blocks and take(job) and decision.reason is None:
                        decision.reason = reason   # the caller's own job, held back alone
                    continue
                if take(job):
                    decision.claimed = self._claim(job, owner, pid)
                    return decision
                outstanding += job["mem_mb"]
                cpus += job["cpus"]
                self._count(load, job)
                per_class[job["class"]] = per_class.get(job["class"], 0) + 1
                keys.add(job["idem_key"])
                decision.reserved.append(job["id"])
        return decision

    def _refuse(self, job: dict[str, Any], reason: str) -> None:
        kind = reason.split(":", 1)[0]
        last = (job.get("last_refusal") or "").split(":", 1)[0]
        row = self.conn.execute(
            "UPDATE jobq.jobs SET refusals = refusals + 1, last_refusal = %s, last_refusal_at = now()"
            " WHERE id = %s AND (last_refusal_at IS NULL OR last_refusal_at < now() - make_interval(secs => %s)"
            " OR split_part(coalesce(last_refusal, ''), ':', 1) <> %s) RETURNING id",
            (reason, job["id"], REFUSAL_EVERY_S, kind)).fetchone()
        if row is None:
            self.conn.execute("UPDATE jobq.jobs SET last_refusal = %s WHERE id = %s",
                              (reason, job["id"]))
        if row is not None and kind != last:
            self._event(job["id"], "refused", reason)

    def _claim(self, job: dict[str, Any], owner: str, pid: int | None) -> dict[str, Any]:
        row = self.conn.execute(
            f"UPDATE jobq.jobs SET state = 'running', owner = %s, attempts = attempts + 1,"
            " started_at = now(), heartbeat_at = now(), finished_at = NULL,"
            " lease_until = now() + make_interval(secs => %s), host = %s,"
            " pid = coalesce(%s, pid), pid_start = coalesce(%s, pid_start),"
            " waited_s = extract(epoch FROM now() - enqueued_at)"
            f" WHERE id = %s AND state = 'queued' RETURNING {COLUMNS}",
            (owner, self.lease_s, self.host, pid, hygiene.process_start(pid) if pid else None,
             job["id"])).fetchone()
        self._event(job["id"], "started", f"owner {owner}")
        return row

    # ------------------------------------------------------------------ held jobs
    def heartbeat(self, job_id: int, owner: str) -> str:
        """Renew the lease; the job's state (``running`` or ``preempting``). Lost when it is
        no longer this owner's."""
        row = self.conn.execute(
            "UPDATE jobq.jobs SET lease_until = now() + make_interval(secs => %s), heartbeat_at = now()"
            " WHERE id = %s AND owner = %s AND state IN ('running', 'preempting') RETURNING state",
            (self.lease_s, job_id, owner)).fetchone()
        if row is None:
            raise Lost(f"job {job_id} is no longer held by {owner}")
        return row["state"]

    def attach(self, job_id: int, owner: str, *, pgid: int | None = None,
               invocation: str | None = None) -> None:
        self.conn.execute(
            "UPDATE jobq.jobs SET pgid = coalesce(%s, pgid), invocation = coalesce(%s, invocation)"
            " WHERE id = %s AND owner = %s", (pgid, invocation, job_id, owner))

    def adopt(self, job_id: int, owner: str) -> bool:
        """Take over a held job whose lease lapsed (its unit still runs)."""
        row = self.conn.execute(
            "UPDATE jobq.jobs SET owner = %s, lease_until = now() + make_interval(secs => %s),"
            " heartbeat_at = now() WHERE id = %s AND state IN ('running', 'preempting')"
            " AND lease_until < now() RETURNING id", (owner, self.lease_s, job_id)).fetchone()
        if row:
            self._event(job_id, "adopted", f"owner {owner}")
        return row is not None

    def finish(self, job_id: int, owner: str | None, ok: bool, *, exit_code: int | None = None,
               result: str | None = None, error: str | None = None) -> str:
        """Record the end of a held job; its next state: ``done``, ``failed``, ``queued``
        (a retry after backoff, or a preempted job queued again with no attempt spent) or
        ``dead``. `owner` None: a reaper finishing a lapsed lease. Lost when the job is not
        held (by that owner)."""
        with self.conn.transaction():
            job = self.conn.execute(
                f"SELECT {COLUMNS} FROM jobq.jobs WHERE id = %s AND state IN ('running', 'preempting')"
                " AND (%s::text IS NULL OR owner = %s) FOR UPDATE", (job_id, owner, owner)).fetchone()
            if job is None:
                raise Lost(f"job {job_id} is not held by {owner}")
            cls = self.classes.get(job["class"])
            again, delay = None, 0.0
            if ok:
                state = "done"
            elif job["state"] == "preempting":
                state, again, delay = "queued", "preempted", 10.0
            elif job["kind"] == "unit" and job["attempts"] < job["max_attempts"]:
                base = cls.backoff_s if cls else 60.0
                cap = cls.backoff_cap_s if cls else 1800.0
                delay = min(cap, base * 2 ** (job["attempts"] - 1)) * (1 + random.random() / 4)
                state, again = "queued", "retry"
            else:
                state = "dead" if job["kind"] == "unit" else "failed"
            if state == "queued" and self.conn.execute(
                    "SELECT 1 FROM jobq.jobs WHERE idem_key = %s AND state = 'queued'",
                    (job["idem_key"],)).fetchone():
                state, again = "failed", None   # a newer job of the key is queued: it runs instead
                error = (error or result or "failed") + "; a newer queued job of the key runs instead"
            if state == "queued":
                self.conn.execute(
                    "UPDATE jobq.jobs SET state = 'queued', owner = NULL,"
                    " lease_until = CASE WHEN kind = 'inline' THEN now() + make_interval(secs => %s) END,"
                    " run_after = now() + make_interval(secs => %s), exit_code = %s, result = %s,"
                    " last_error = %s, attempts = attempts - %s, preemptions = preemptions + %s,"
                    " invocation = NULL, pgid = NULL WHERE id = %s",
                    (self.lease_s, delay, exit_code, result, error, int(again == "preempted"),
                     int(again == "preempted"), job_id))
                self._event(job_id, again or "queued", f"{result or ''} {error or ''}".strip()[:500])
            else:
                self.conn.execute(
                    "UPDATE jobq.jobs SET state = %s, finished_at = now(), lease_until = NULL,"
                    " exit_code = %s, result = %s, last_error = %s WHERE id = %s",
                    (state, exit_code, result, None if ok else error, job_id))
                self._event(job_id, state, f"{result or ''} {error or ''}".strip()[:500] or None)
            self._notify()
        return state

    def preempt(self, job_id: int, reason: str) -> bool:
        """Mark a running preemptible job for a graceful stop (its runner stops it)."""
        row = self.conn.execute(
            "UPDATE jobq.jobs SET state = 'preempting', last_error = %s"
            " WHERE id = %s AND state = 'running' AND preemptions < %s RETURNING id",
            (f"preempted: {reason}", job_id, MAX_PREEMPTIONS)).fetchone()
        if row:
            self._event(job_id, "preempting", reason)
            self._notify()
        return row is not None

    def cancel(self, job_id: int, why: str = "cancelled") -> bool:
        row = self.conn.execute(
            "UPDATE jobq.jobs SET state = 'cancelled', finished_at = now(), last_error = %s,"
            " lease_until = NULL WHERE id = %s AND state = 'queued' RETURNING id",
            (why, job_id)).fetchone()
        if row:
            self._event(job_id, "cancelled", why)
            self._notify()
        return row is not None

    def retry(self, job_id: int) -> bool:
        """Queue a dead (or failed unit) job again with fresh attempts."""
        with self.conn.transaction():
            job = self.conn.execute("SELECT idem_key FROM jobq.jobs WHERE id = %s AND state IN"
                                    " ('dead', 'failed') AND kind = 'unit' FOR UPDATE",
                                    (job_id,)).fetchone()
            if job is None or self.conn.execute(
                    "SELECT 1 FROM jobq.jobs WHERE idem_key = %s AND state = 'queued'",
                    (job["idem_key"],)).fetchone():
                return False
            self.conn.execute("UPDATE jobq.jobs SET state = 'queued', attempts = 0, run_after = now(),"
                              " finished_at = NULL, owner = NULL WHERE id = %s", (job_id,))
            self._event(job_id, "requeued", "by hand")
            self._notify()
        return True

    def get(self, job_id: int) -> dict[str, Any] | None:
        return self.conn.execute(f"SELECT {COLUMNS} FROM jobq.jobs WHERE id = %s", (job_id,)).fetchone()

    def held(self, owner: str | None = None) -> list[dict[str, Any]]:
        return self.conn.execute(
            f"SELECT {COLUMNS} FROM jobq.jobs WHERE state IN ('running', 'preempting')"
            " AND (%s::text IS NULL OR owner = %s) ORDER BY id", (owner, owner)).fetchall()

    def lapsed(self) -> list[dict[str, Any]]:
        return self.conn.execute(
            f"SELECT {COLUMNS} FROM jobq.jobs WHERE state IN ('running', 'preempting')"
            " AND lease_until < now() ORDER BY id").fetchall()

    def prune(self, days: int = 14) -> int:
        with self.conn.transaction():
            gone = self.conn.execute(
                "DELETE FROM jobq.jobs WHERE state IN ('done', 'failed', 'cancelled')"
                " AND finished_at < now() - make_interval(days => %s)", (days,)).rowcount
            self.conn.execute("DELETE FROM jobq.events WHERE at < now() - make_interval(days => %s)",
                              (days,))
            if self.conn.execute("SELECT to_regclass('jobq.event_queue') AS t").fetchone()["t"]:
                self.conn.execute("DELETE FROM jobq.event_queue WHERE state = 'done' AND done_at"
                                  " < now() - make_interval(days => %s)", (days,))
        return gone

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        """Depth, held, waiting, dead letters, recent ends and the memory picture: class
        names, keys, labels, ages and numbers only."""
        q = self.conn.execute
        depth = {r["class"]: r["n"] for r in q(
            "SELECT class, count(*) AS n FROM jobq.jobs WHERE state = 'queued' GROUP BY class")}
        held = q("SELECT id, class, kind, unit, label, state, mem_mb, cpus, disk_mb, workspace,"
                 " attempts, preemptions,"
                 " extract(epoch FROM now() - started_at)::int AS running_s, waited_s"
                 " FROM jobq.jobs WHERE state IN ('running', 'preempting') ORDER BY priority, id").fetchall()
        for job in held:
            job["current_mb"] = self.probe.unit_memory_mb(job["unit"]) if job["unit"] else None
        waiting = q("SELECT id, class, kind, label, workspace, priority, mem_mb, cpus, attempts,"
                    " refusals, last_refusal,"
                    " extract(epoch FROM now() - enqueued_at)::int AS waiting_s,"
                    " greatest(0, extract(epoch FROM run_after - now()))::int AS not_before_s"
                    " FROM jobq.jobs WHERE state = 'queued' ORDER BY priority, enqueued_at, id"
                    " LIMIT 50").fetchall()
        dead = q("SELECT id, class, label, attempts, result, last_error, finished_at"
                 " FROM jobq.jobs WHERE state = 'dead' ORDER BY finished_at DESC LIMIT 20").fetchall()
        recent = q("SELECT id, class, state, result, exit_code, waited_s,"
                   " extract(epoch FROM finished_at - started_at)::int AS ran_s, finished_at"
                   " FROM jobq.jobs WHERE finished_at IS NOT NULL AND state <> 'cancelled'"
                   " ORDER BY finished_at DESC LIMIT 15").fetchall()
        hour = q("SELECT kind, count(*) AS n FROM jobq.events WHERE at > now() - interval '1 hour'"
                 " GROUP BY kind").fetchall()
        unmanaged = []
        held_units = {j["unit"] for j in held if j["unit"]}
        for cls in self.classes.values():
            if cls.unit and cls.unit not in held_units:
                current = self.probe.unit_memory_mb(cls.unit)
                if current is not None:
                    unmanaged.append({"class": cls.name, "unit": cls.unit, "mem_mb": cls.mem_mb,
                                      "current_mb": current})
        try:
            avail = self.probe.mem_available_mb()
        except OSError:
            avail = None
        outstanding = self._outstanding(self.held())[0]
        for rows in (dead, recent):
            for r in rows:
                r["finished_at"] = r["finished_at"].isoformat() if r.get("finished_at") else None
        for r in held + waiting + recent:
            if r.get("waited_s") is not None:
                r["waited_s"] = round(float(r["waited_s"]), 1)
        events = q("SELECT topic, state, count(*) AS n,"
                   " extract(epoch FROM now() - min(published_at))::int AS oldest_s"
                   " FROM jobq.event_queue WHERE state IN ('ready', 'inflight', 'dead')"
                   " GROUP BY topic, state ORDER BY topic, state").fetchall() \
            if q("SELECT to_regclass('jobq.event_queue') AS t").fetchone()["t"] else []
        return {"depth": depth, "queued": sum(depth.values()), "running": held,
                "events": events,
                "waiting": waiting, "dead": dead, "recent": recent, "unmanaged": unmanaged,
                "last_hour": {r["kind"]: r["n"] for r in hour},
                "memory": {"available_mb": avail, "promised_mb": outstanding,
                           "floor_mb": self.floor_mb,
                           "headroom_mb": None if avail is None else avail - outstanding - self.floor_mb},
                "cpu_budget": self.cpu_budget,
                "workspace_pool": self._workspace_pool(held),
                "workspaces": self._workspaces(held, waiting)}

    def _workspace_pool(self, held: list[dict[str, Any]]) -> dict[str, Any]:
        mine = [j for j in held
                if getattr(self.classes.get(j["class"]), "pool", "") == "workspaces"]
        return {"cpus_held": round(sum(float(j["cpus"]) for j in mine), 1),
                "cpus": self.workspace_cpus,
                "mem_held_mb": sum(j["mem_mb"] for j in mine), "mem_mb": self.workspace_mem_mb}

    @staticmethod
    def _workspaces(held: list[dict[str, Any]], waiting: list[dict[str, Any]]) -> dict[str, Any]:
        """Each workspace's load: held jobs, their declared cores and memory, and waiting jobs
        (who is using the machine)."""
        workspaces: dict[str, dict[str, Any]] = {}
        for rows, held_row in ((held, True), (waiting, False)):
            for j in rows:
                if not j.get("workspace"):
                    continue
                row = workspaces.setdefault(j["workspace"], {"held": 0, "cpus": 0.0, "mem_mb": 0,
                                                   "waiting": 0})
                if held_row:
                    row["held"] += 1
                    row["cpus"] = round(row["cpus"] + float(j["cpus"]), 1)
                    row["mem_mb"] += j["mem_mb"]
                else:
                    row["waiting"] += 1
        return dict(sorted(workspaces.items()))
