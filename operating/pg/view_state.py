"""Freshness and failures of the business views, one row per view (contract C6,
``biz.view_state``, migration 0060; speed specs P2 + P6), and the refresh's per-view
isolation.

The refresh (``business_views.refresh``) builds each stale view on its own: a view that
fails keeps its old table (its rows keep serving, with their age), is recorded here with
its reason, class and signature, and never stops the other views. The same signature twice
in a row holds the view (``quarantined_until``): code, data and memory failures for
``HOLD`` (2 h); a disk failure until the disk budget (``operating.disk_budget``) can grant
the view's estimate (no timed retry); a lock is never held, it is retried next cycle. A new
release (the view's code print differs from ``version``) clears the hold. Every hold ends
(``PAUSES EXPIRE``) and is an ops alarm while it lasts (``alarm_checks``).

``last_ok_at`` is when the view was last known to hold every change of its inputs: built
(from a read that started then), or checked and nothing it reads had moved. It is the "as of"
of what is shown from the view. ``newest_source_at`` is the newest change-feed cycle of a
type the view reads.

Classes (``classify``), from the exception type and its SQLSTATE, not from words where a code
exists: ``disk`` (the disk budget's refusal; before P3 the build's floor message, matched in
this one function), ``lock`` (55P03, 40P01), ``memory`` (MemoryError, 53200, a worker killed),
``data`` (SQLSTATE classes 22 and 23), ``code`` (any other error inside a view's builder),
``dependency`` (a view it reads is failing: not detected yet, no view reads another today).
Signature = class + exception type + the first 80 characters with digits as ``#``.

The run is watched (``Heartbeat``): every 30 s it pings systemd's watchdog only when the
refresh moved (one of its database sessions started or finished a statement, or a view
finished); a run with no progress for ``WatchdogSec`` is stopped by systemd instead of
holding the advisory lock for hours.

``python3 -m operating.pg.view_state status`` prints every row; ``release <view>`` ends a
hold now.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

TABLE = "view_state"
HOLD = dt.timedelta(hours=2)          # code / data / memory, after the same failure twice
HELD_CLASSES = ("code", "data", "memory", "dependency")
CLASSES = ("disk", "lock", "memory", "code", "data", "dependency")
BEHIND_MINUTES = 60                  # PB1: a view this far behind its inputs is an alarm
BEAT_SECONDS = 30
# peak disk of a rebuild, as a multiple of the live table (build_pause.py's measured note:
# a whole comms build ~5x; the others ~3x: table + stage + WAL)
PEAK_FACTOR = {"comms": 5.0, "plain": 3.0}
_FLOOR_TEXT = "disk below"            # business_comms._disk_guard's message (until train B)


class ViewsFailed(RuntimeError):
    """Views not built, without ``biz.view_state`` to record them in (before migration
    0060): ``failed`` maps each to ``"<Type>: <message>"``."""

    def __init__(self, failed: Mapping[str, str]):
        self.failed = dict(failed)
        super().__init__("views not built (old tables kept): " + "; ".join(
            f"{n}: {r}" for n, r in self.failed.items()))


def classify(type_name: str, message: str) -> tuple[str, str]:
    """(class, signature) of a failure ``<type_name>: <message>`` (module doc). The SQLSTATE
    comes from psycopg's own class of that name."""
    sqlstate = ""
    try:
        import psycopg.errors as pe
        cls = getattr(pe, type_name, None)
        sqlstate = getattr(cls, "sqlstate", None) or ""
    except ImportError:
        pass
    if type_name == "Refused" or _FLOOR_TEXT in message:
        klass = "disk"
    elif sqlstate in ("55P03", "40P01"):
        klass = "lock"
    elif type_name in ("MemoryError", "BrokenProcessPool") or sqlstate == "53200":
        klass = "memory"
    elif sqlstate[:2] in ("22", "23"):
        klass = "data"
    else:
        klass = "code"
    # guard-ok: the signature is defined on a message's first 80 characters (speed specs P2)
    sig = f"{klass}:{type_name}:" + re.sub(r"\d", "#", message[:80])
    return klass, sig


def split(text: str) -> tuple[str, str]:
    """(type name, message) of ``"<Type>: <message>"``."""
    head, sep, rest = text.partition(": ")
    if not sep or not re.fullmatch(r"[A-Za-z_][\w.]*", head):
        return "Exception", text
    return head.rsplit(".", 1)[-1], rest


def ready(conn: Any, schema: str) -> bool:
    """Whether migration 0060 made ``<schema>.view_state``."""
    return conn.execute("SELECT to_regclass(%s)", (f"{schema}.{TABLE}",)).fetchone()[0] \
        is not None


_COLS = ("view", "watermark", "newest_source_at", "last_ok_at", "failing_since", "reason",
         "version", "fail_class", "fail_sig", "fail_count", "quarantined_until",
         "last_seconds", "heartbeat_at", "phase")


def _rows(conn: Any, schema: str) -> dict[str, dict[str, Any]]:
    got = conn.execute(f"SELECT {', '.join(_COLS)} FROM {schema}.{TABLE}").fetchall()
    return {r[0]: dict(zip(_COLS, r)) for r in got}


def _ensure(conn: Any, schema: str, names: Iterable[str]) -> None:
    conn.execute(f"INSERT INTO {schema}.{TABLE} (view) SELECT unnest(%s::text[]) "
                 "ON CONFLICT (view) DO NOTHING", (sorted(set(names)),))


def ok(conn: Any, schema: str, views: Iterable[str], *, watermark: Any, at: dt.datetime,
       versions: Mapping[str, str] | None = None,
       seconds: Mapping[str, Any] | None = None) -> None:
    """``views`` hold every change up to ``watermark`` as of ``at``: the failure and any
    hold end."""
    views = list(views)
    if not views:
        return
    _ensure(conn, schema, views)
    mark = watermark if isinstance(watermark, int) and not isinstance(watermark, bool) else None
    for v in views:
        took = (seconds or {}).get(v)
        conn.execute(
            f"UPDATE {schema}.{TABLE} SET watermark = coalesce(%s, watermark), last_ok_at = %s, "
            "failing_since = NULL, reason = NULL, fail_class = NULL, fail_sig = NULL, "
            "fail_count = 0, quarantined_until = NULL, version = coalesce(%s, version), "
            "last_seconds = coalesce(%s, last_seconds), phase = 'ok' WHERE view = %s",
            (mark, at, (versions or {}).get(v), took, v))


def failed(conn: Any, schema: str, view: str, text: str, *, version: str | None = None,
           now: dt.datetime | None = None) -> dict[str, Any]:
    """Record one failure (``"<Type>: <message>"``); returns ``{class, count, until}``."""
    now = now or dt.datetime.now(dt.timezone.utc)
    klass, sig = classify(*split(text))
    _ensure(conn, schema, [view])
    row = _rows(conn, schema)[view]
    count = (row["fail_count"] or 0) + 1 if row["fail_sig"] == sig else 1
    until = None
    if count >= 2 and klass in HELD_CLASSES:
        until = now + HOLD
    elif count >= 2 and klass == "disk":
        until = dt.datetime.max.replace(tzinfo=dt.timezone.utc)   # until the budget grants
    conn.execute(
        f"UPDATE {schema}.{TABLE} SET failing_since = coalesce(failing_since, %s), reason = %s, "
        "fail_class = %s, fail_sig = %s, fail_count = %s, quarantined_until = %s, "
        "version = coalesce(version, %s), phase = 'failed' WHERE view = %s",
        (now, text, klass, sig, count, until, version, view))
    return {"class": klass, "count": count, "until": until}


def needs_train(conn: Any, schema: str, view: str, *, version: str | None = None,
                now: dt.datetime | None = None) -> None:
    """``view`` (a comms table) was built by other code than the code now deployed: its rows
    keep serving and keep their in-place updates, never a whole rebuild on the box; said in
    ``reason`` (an alarm while it lasts), never held (speed specs P4)."""
    _ensure(conn, schema, [view])
    conn.execute(f"UPDATE {schema}.{TABLE} SET failing_since = coalesce(failing_since, %s), "
                 "reason = %s, fail_class = 'code', version = coalesce(%s, version) "
                 "WHERE view = %s",
                 (now or dt.datetime.now(dt.timezone.utc), NEEDS_TRAIN, version, view))


NEEDS_TRAIN = ("needs_train: the code's print differs from the built table's; a Spark train "
               "builds it (never on the box), the old rows keep serving")


def beat(conn: Any, schema: str, views: Sequence[str], phase: str) -> None:
    """The heartbeat of ``views``' current step (and nothing else)."""
    if views:
        _ensure(conn, schema, views)
        conn.execute(f"UPDATE {schema}.{TABLE} SET heartbeat_at = now(), phase = %s "
                     "WHERE view = ANY(%s)", (phase, list(views)))


def runnable(conn: Any, schema: str, views: Sequence[str], codes: Mapping[str, str],
             available_gb: Callable[[], float] | None = None,
             now: dt.datetime | None = None) -> tuple[list[str], dict[str, str]]:
    """(``views`` that may build now, the held ones with why). A hold ends when its time
    passed, when the view's code print differs from the one it failed with (a new release),
    or, for a disk hold, when the disk budget can grant the view's peak estimate."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = _rows(conn, schema)
    go, held = [], {}
    for v in views:
        row = rows.get(v)
        until = row and row["quarantined_until"]
        if not until or until <= now:
            go.append(v)
            continue
        if v in codes and row["version"] not in (None, codes[v]):
            release(conn, schema, v)
            go.append(v)
            continue
        if row["fail_class"] == "disk":
            need = estimate_gb(conn, schema, v)
            have = available_gb() if available_gb else None
            if have is not None and have >= need:
                go.append(v)
                continue
            held[v] = (f"held: disk ({need:.1f} GB needed, "
                       f"{'unknown' if have is None else f'{have:.1f} GB'} available): "
                       f"{row['reason']}")
            continue
        held[v] = (f"held until {until.isoformat(timespec='minutes')} after the same "
                   f"{row['fail_class']} failure {row['fail_count']} times: {row['reason']}")
    return go, held


def estimate_gb(conn: Any, schema: str, view: str, comms: bool | None = None) -> float:
    """The peak disk of rebuilding ``view``: its live table's size x ``PEAK_FACTOR``."""
    size = conn.execute("SELECT coalesce(pg_total_relation_size(to_regclass(%s)), 0)",
                        (f"{schema}.{view}",)).fetchone()[0]
    if comms is None:
        comms = conn.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
                             "table_schema = %s AND table_name = %s AND column_name = 'unit')",
                             (schema, view)).fetchone()[0]
    return size / 1024 ** 3 * PEAK_FACTOR["comms" if comms else "plain"]


def release(conn: Any, schema: str, view: str) -> bool:
    got = conn.execute(f"UPDATE {schema}.{TABLE} SET quarantined_until = NULL, fail_count = 0 "
                       "WHERE view = %s RETURNING view", (view,)).fetchone()
    return got is not None


def newest_source(conn: Any, types_by_view: Mapping[str, Sequence[str]]
                  ) -> dict[str, dt.datetime | None]:
    """Per view, when the newest change of a type it reads was applied (the change feed's
    cycle time; a migration's change, ``kind = 'm'``, or one of no known type counts for
    every view, as in ``business_views_access.moved_since``). None without the feed."""
    if conn.execute("SELECT to_regclass('ontology.change_feed')").fetchone()[0] is None:
        return {n: None for n in types_by_view}
    got = conn.execute(
        "SELECT CASE WHEN f.kind = 'm' THEN NULL ELSE f.object_type END, max(c.applied_at) "
        "FROM ontology.change_feed f JOIN ontology.change_cycles c USING (cycle_id) "
        "GROUP BY 1").fetchall()
    by_type: dict[str | None, dt.datetime] = {t: at for t, at in got if at is not None}
    every = by_type.get(None)
    out: dict[str, dt.datetime | None] = {}
    for view, types in types_by_view.items():
        times = [by_type[t] for t in types if t in by_type] + ([every] if every else [])
        out[view] = max(times) if times else None
    return out


def seen(conn: Any, schema: str, newest: Mapping[str, dt.datetime | None]) -> None:
    _ensure(conn, schema, newest)
    for view, at in newest.items():
        conn.execute(f"UPDATE {schema}.{TABLE} SET newest_source_at = %s WHERE view = %s",
                     (at, view))


def read(conn: Any, schema: str) -> list[dict[str, Any]]:
    """Every row with ``behind_minutes``: the newest change of its inputs it does not hold,
    against the time it was last known current (0 when it holds all of them; None when never
    known current)."""
    out = []
    for name, row in sorted(_rows(conn, schema).items()):
        ok_at, newest = row["last_ok_at"], row["newest_source_at"]
        if ok_at is None:
            behind = None
        elif newest is None or newest <= ok_at:
            behind = 0.0
        else:   # the newest change it lacks, against the newest it holds (speed specs P6)
            behind = round((newest - ok_at).total_seconds() / 60, 1)
        out.append({**row, "behind_minutes": behind})
    return out


def as_of(conn: Any, schema: str, views: Iterable[str]) -> dt.datetime | None:
    """The oldest ``last_ok_at`` of ``views`` (None when one was never known current)."""
    views = list(views)
    rows = _rows(conn, schema)
    times = [rows.get(v, {}).get("last_ok_at") for v in views]
    if not views or any(t is None for t in times):
        return None
    return min(times)


def alarm_checks(dsn: str | None, schema: str = "biz") -> list[dict[str, Any]]:
    """``view_behind:<view>`` per view: breach when failing, held, or behind its inputs
    longer than ``BEHIND_MINUTES``; unknown when never known current or without migration
    0060 (said)."""
    import psycopg
    from operating.ops_health import _check
    with psycopg.connect(dsn or "postgresql:///plx", connect_timeout=5) as conn:
        if not ready(conn, schema):
            return [_check("view_behind", None, 0, None, "views",
                           "biz.view_state missing (migration 0060 not applied)")]
        out = []
        for row in read(conn, schema):
            behind = row["behind_minutes"]
            if row["quarantined_until"] is not None:
                breach, detail = True, f"held ({row['fail_class']}): {row['reason']}"
            elif row["failing_since"] is not None:
                breach, detail = True, (f"failing since {row['failing_since']:%H:%M} UTC "
                                        f"({row['fail_class']}): {row['reason']}")
            elif behind is None:
                breach, detail = None, "never known current"
            else:
                breach, detail = behind > BEHIND_MINUTES, f"{behind:.0f} min behind its inputs"
            out.append(_check(f"view_behind:{row['view']}", behind, BEHIND_MINUTES, breach,
                              "min", _said(detail or "")))
        from operating.pg import named_views   # every view the registry / Ask catalog names
        return out + named_views.alarm_checks(conn, schema)


DETAIL_CHARS = 300   # an ops check's detail line (the banner); the whole reason is in the row


def _said(text: str) -> str:
    from operating.limits import cut
    got = cut(text, DETAIL_CHARS, say="ops banner line; the whole reason: view_state status")
    return got.text + (f" [{got.note}]" if got.cut else "")


# ---- the watchdog ----------------------------------------------------------------------------

def notify(message: str, environ: Mapping[str, str] = os.environ) -> bool:
    """One sd_notify message through ``systemd-notify`` (the unit has ``NotifyAccess=all``;
    the tree's egress gate allows no socket); False when not under a notify unit or when the
    message could not be sent (said on stderr: a run that cannot ping is stopped by systemd)."""
    if not environ.get("NOTIFY_SOCKET"):
        return False
    import subprocess
    try:
        subprocess.run(["systemd-notify", f"--pid={os.getpid()}", message], check=True,
                       timeout=10, env=dict(environ), capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"sd_notify_failed": f"{message}: {type(exc).__name__}"}),
              file=sys.stderr, flush=True)
        return False
    return True


PROGRESS_SQL = ("SELECT coalesce(max(greatest(state_change, query_start)), 'epoch') "
                "FROM pg_stat_activity WHERE application_name = %s AND pid <> pg_backend_pid()")


class Heartbeat:
    """Every ``BEAT_SECONDS`` while the refresh runs: when it moved (``progress()`` returned
    something new, or ``moved()`` was called), ``WATCHDOG=1`` and ``beat`` of the current
    step's views; when it did not, nothing (systemd's ``WatchdogSec`` then stops a stuck
    run). ``progress``: the newest statement start or finish of the refresh's own database
    sessions (``PROGRESS_SQL`` on its ``application_name``)."""

    def __init__(self, progress: Callable[[], Any], on_beat: Callable[[Sequence[str], str], None],
                 environ: Mapping[str, str] = os.environ, every: float = BEAT_SECONDS):
        self._progress, self._on_beat, self._env, self.every = progress, on_beat, environ, every
        self.views: Sequence[str] = ()
        self.phase = "start"
        self._last: Any = None
        self._moved = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.beats = 0

    def start(self) -> "Heartbeat":
        notify("READY=1", self._env)
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()
        return self

    def step(self, phase: str, views: Sequence[str]) -> None:
        self.phase, self.views = phase, tuple(views)
        self._moved.set()

    def moved(self) -> None:
        self._moved.set()

    def tick(self) -> bool:
        """One check: True (and a beat) when the run moved since the last check."""
        try:
            now = self._progress()
        except Exception as exc:  # noqa: BLE001 - said; no beat (systemd decides)
            print(json.dumps({"heartbeat_read_failed": f"{type(exc).__name__}: {exc}"}),
                  file=sys.stderr, flush=True)
            now = self._last
        moved = self._moved.is_set() or now != self._last
        self._moved.clear()
        self._last = now
        if not moved:
            return False
        self.beats += 1
        notify("WATCHDOG=1", self._env)
        try:
            self._on_beat(self.views, self.phase)
        except Exception as exc:  # noqa: BLE001 - the beat row is bookkeeping; said
            print(json.dumps({"heartbeat_write_failed": f"{type(exc).__name__}: {exc}"}),
                  file=sys.stderr, flush=True)
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            self.tick()

    def stop(self) -> None:
        self._stop.set()
        notify("STOPPING=1", self._env)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="view_state")
    ap.add_argument("command", choices=("status", "release"))
    ap.add_argument("view", nargs="?")
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    import psycopg
    from operating.pg import config
    from operating.pg.business_views import SCHEMA
    with psycopg.connect(args.dsn or config.dsn(), autocommit=True) as conn:
        if not ready(conn, SCHEMA):
            print(json.dumps({"error": f"{SCHEMA}.view_state missing: apply migration 0060"}))
            return 2
        if args.command == "release":
            if not args.view:
                ap.error("release needs a view")
            print(json.dumps({"released": release(conn, SCHEMA, args.view)}))
            return 0
        for row in read(conn, SCHEMA):
            print(json.dumps(row, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
