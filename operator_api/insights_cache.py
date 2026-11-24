"""Insights open in well under a second: each tile's statement result is kept per reader
for a few minutes and read again in the background, never shared between readers.

What is kept is ``plx.sql``'s own answer to ONE reader for ONE statement (``read_sql``: their
door, their rows and cells), under a key of who they are now (their principals, clearance,
full-read grant and whether the session views as someone or is the canary): another person,
or the same person under another grant, never reads it. The Team rule, the hidden people
and the chart are applied to it on every open (``insights.result``), so a change there shows
at once.

A kept answer is served only while the permission door's stamp is the one it was read under
(``door.check``: every change to what a door decides, the people exclusion included, moves
it) and the door serves; an unreadable stamp serves nothing kept (fail closed: read now).
Younger than ``FRESH_SECONDS``: served as is. Older (or a person's Refresh): read again.

* **One read per statement and reader at a time.** Sixteen tiles opened at once used to
  queue sixteen reads behind each other in the reads worker (4-9 s each on live data, 11-19);
  a second open of a tile being read waits for that read instead of starting another.
* **Read ahead.** Listing the dashboard starts the reads of its tiles (``warm``, at most
  ``WORKERS`` at a time), so a tile's own request finds its answer kept or in flight.
* **Kept warm.** While someone has opened Insights in the last ``ACTIVE_SECONDS`` and their
  session is valid, the tiles they opened are read again as them before they go stale
  (``Keeper.tick``, every ``TICK_SECONDS``), so the next open is instant.

* **Kept across restarts.** Each answer is also written to the app's kept-pages file on disk
  (``page_store.PageStore``: owner-only, in the app's own data directory, under
  ``INSIGHT_FORMAT``, not the code version: a deploy keeps the tiles whose statement is the same), keyed by the reader key, the statement and the door stamp, so the API finds after
  a restart what it (or the warm-up process, which reads the known readers' tiles ahead) read
  before: Insights opened 9-18 s cold after every restart, one tile never (it reads in
  30-60 s under load and was cut at 30 s each time).
* **Served while read again.** A kept answer under the same door stamp that is older than
  ``FRESH_SECONDS`` but younger than ``STALE_MOST_SECONDS`` is served at once (its own read
  time shows on the tile) while it is read again in the background (``later``: the
  ``Keeper``'s reads, which may take ``AHEAD_SECONDS``); a person's Refresh still reads now.

Memory is bounded (``MOST_KEPT`` answers of at most 200 rows each); the file is bounded by
the page store's own pruning. Evidence: isolated-fixture
(``operator_api/tests/test_insights.py``)."""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Mapping, Optional, Sequence

# the version the file keeps a tile under: raise it only when a kept answer's own shape changes
# (an answer that no longer unpickles is a miss, read again)
INSIGHT_FORMAT = "insight-1"
FRESH_ENV = "PLX_INSIGHTS_FRESH_SECONDS"
FRESH_SECONDS = 300.0     # a kept answer is served this long under an unchanged door stamp
ACTIVE_SECONDS = 3600.0   # a reader's tiles are kept warm this long after they last looked
TICK_SECONDS = 60.0
WORKERS = 3               # reads ahead at once (each one plx.sql read in the reads worker)
MOST_KEPT = 4000
MOST_READERS = 200
# a kept answer under the same door stamp is served this long (read again behind it); the
# file keeps it as long
STALE_MOST_SECONDS = 30 * 60.0   # a policy change moves neither reader key nor door stamp
# a read that cannot finish (too slow, busy, tables being rebuilt, unreadable) is answered by
# the reader's last good answer for the statement of this age or younger, whatever the door's
# stamp has moved to since (a refresh of the business tables moves it for everyone), shown
# with the time it was read; nothing older, and never after a refusal about access
LAST_GOOD_SECONDS = 30 * 60.0
UNFINISHED = frozenset({"insight_too_slow", "insight_not_ready", "insight_unreadable", "busy"})
AHEAD_SECONDS = 1800.0    # one read ahead or in the background: a hung read only (no person waits on it)


def fresh_seconds(environ: Mapping[str, str] = os.environ) -> float:
    """``FRESH_ENV`` seconds (0: nothing kept is served); unset or bad: ``FRESH_SECONDS``."""
    try:
        value = float(environ.get(FRESH_ENV, "") or FRESH_SECONDS)
    except ValueError:
        return FRESH_SECONDS
    return max(0.0, min(value, 3600.0))


def reader_key(reader: Any) -> Optional[tuple]:
    """Who ``reader`` is now, for keeping their answers apart from everyone else's; None (keep
    nothing) for a session that is not a plain signed-in person."""
    session = getattr(reader, "session", None)
    principals = getattr(session, "principals", None)
    if session is None or not principals:
        return None
    return (tuple(sorted(principals)), getattr(session, "clearance", None),
            getattr(session, "access", None), bool(getattr(session, "view_as", False)),
            bool(getattr(session, "canary", False)))


class Kept:
    """The kept answers (LRU) and the reads in flight, by (reader key, statement)."""

    def __init__(self, stamp: Callable[[], Optional[int]], *,
                 fresh: Optional[float] = None, clock: Callable[[], float] = time.monotonic,
                 most: int = MOST_KEPT, disk: Any = None,
                 later: Optional[Callable[[tuple], Any]] = None,
                 stale_most: float = STALE_MOST_SECONDS,
                 wall: Callable[[], float] = time.time,
                 disk_code: Optional[str] = None) -> None:
        """``disk``: the kept-pages file (``page_store.PageStore``: ``get``/``put``), None:
        memory only. ``later(key)``: read ``key`` again in the background; None: a stale
        answer is read now, never served."""
        self.stamp, self.clock, self.most = stamp, clock, most
        self.fresh = fresh_seconds() if fresh is None else fresh
        self.disk, self.later, self.stale_most, self.wall = disk, later, stale_most, wall
        # the version the file keeps a tile under: None = the store's own (the code's, so a
        # deploy leaves every tile unread); a fixed one serves the tile across deploys while
        # its statement, reader and door stamp are the same (it is read again behind it)
        self._disk_kw = {} if disk_code is None else {"code": disk_code}
        self._kept: "OrderedDict[tuple, tuple[Any, int, float]]" = OrderedDict()
        self._flight: dict[tuple, Future] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple, read: Callable[[], Any], *, again: bool = False,
            prior: Optional[Callable[[Any], bool]] = None,
            stamp: Optional[Callable[[], Optional[int]]] = None) -> Any:
        """The answer for ``key``: kept (fresh, same door stamp: in memory, else on disk),
        or kept and stale but young enough (``stale_most``) while ``later`` reads it again,
        or the read in flight for it, or ``read()`` now (kept for the next). ``again``:
        never a kept one. An unreadable stamp serves nothing kept.

        ``prior(answer)``: with nothing kept under the door's stamp now (the business tables
        refreshed since, in this process or before a restart), the last answer read for
        ``key`` under any stamp (``previous``: ``stale_most`` or younger) is served while
        ``later`` reads it again, only when ``prior`` says it may be (the caller's check
        that the reader's access state is the one it was read under). Every tile read cold
        after each refresh and each restart (11-22: Insights 32-56 s, 504s).

        ``stamp``: the door's stamp as the caller read it (a dashboard reads it once for all
        its tiles); None: read here."""
        now_stamp = self._stamp() if stamp is None else self._valid(stamp)
        stale = None
        with self._lock:
            hit = self._kept.get(key)
            if hit is not None and not again and now_stamp is not None \
                    and hit[1] == now_stamp:
                age = self.clock() - hit[2]
                if age < self.fresh:
                    self._kept.move_to_end(key)
                    return hit[0]
                if age < self.stale_most:
                    stale = hit[0]
        if not again and now_stamp is not None and self.fresh > 0:
            if stale is None:
                got = self._from_disk(key, now_stamp)
                if got is not None:
                    value, age = got
                    with self._lock:
                        self._keep(key, value, now_stamp, self.clock() - age)
                    if age < self.fresh:
                        return value
                    stale = value
            if stale is None and prior is not None and self.later is not None:
                held = self.previous(key)
                try:
                    stale = held if held is not None and prior(held) else None
                except Exception:  # noqa: BLE001 - undecided: read now
                    stale = None
            if stale is not None and self.later is not None:
                try:
                    self.later(key)
                except Exception:  # noqa: BLE001 - read now below instead
                    stale = None
                if stale is not None:
                    return stale
        with self._lock:
            flying = self._flight.get(key)
            if flying is None:
                flying = Future()
                self._flight[key] = flying
                mine = True
            else:
                mine = False
        if not mine:
            return flying.result()
        try:
            got = read()
        except BaseException as exc:
            with self._lock:
                self._flight.pop(key, None)
            flying.set_exception(exc)
            raise
        with self._lock:
            self._flight.pop(key, None)
            if now_stamp is not None and self.fresh > 0:
                self._keep(key, got, now_stamp, self.clock())
        if now_stamp is not None and self.fresh > 0 and self.disk is not None:
            disk_key = self._disk_key(key, now_stamp)
            if disk_key is not None:
                try:
                    self.disk.put(disk_key, got, self.stale_most, **self._disk_kw)
                    # the last answer whatever the stamp (``previous``), across restarts
                    self.disk.put(("insight_last",) + disk_key[1:3], got, self.stale_most,
                                  **self._disk_kw)
                except Exception:  # noqa: BLE001 - kept in memory only
                    pass
        flying.set_result(got)
        return got

    def last_good(self, key: tuple, most: float = LAST_GOOD_SECONDS) -> Any:
        """``key``'s last answer read for this reader in this process, of ``most`` seconds
        or younger, whatever door stamp it was read under (None: none). Served only for a
        read that could not finish (``UNFINISHED``), never as a kept answer."""
        with self._lock:
            hit = self._kept.get(key)
        if hit is None or self.clock() - hit[2] > most:
            return None
        return hit[0]

    def servable(self, key: tuple, prior: Optional[Callable[[Any], bool]] = None,
                 stamp: Optional[Callable[[], Optional[int]]] = None) -> bool:
        """Whether ``get(key, ..., prior=prior)`` would answer now without reading: a kept
        answer under the door's stamp (fresh, or stale with ``later`` to read it again), or
        the reader's last one that ``prior`` allows. Reads nothing; answers nothing."""
        now_stamp = self._stamp() if stamp is None else self._valid(stamp)
        if now_stamp is None or self.fresh <= 0:
            return False
        with self._lock:
            hit = self._kept.get(key)
        age = None
        if hit is not None and hit[1] == now_stamp:
            age = self.clock() - hit[2]
        else:
            got = self._from_disk(key, now_stamp)
            age = got[1] if got is not None else None
        if age is not None and (age < self.fresh or (age < self.stale_most
                                                     and self.later is not None)):
            return True
        if prior is None or self.later is None:
            return False
        held = self.previous(key)
        try:
            return held is not None and bool(prior(held))
        except Exception:  # noqa: BLE001 - undecided: not servable
            return False

    def previous(self, key: tuple) -> Any:
        """``key``'s last answer under any door stamp, ``stale_most`` seconds old or
        younger: in memory, else on disk (written with every read); None without one."""
        with self._lock:
            hit = self._kept.get(key)
        if hit is not None:
            return hit[0] if self.clock() - hit[2] < self.stale_most else None
        if self.disk is None or self._disk_key(key, 0) is None:
            return None
        try:
            got = self.disk.get(("insight_last", key[0], key[1]), **self._disk_kw)
        except Exception:  # noqa: BLE001 - a miss
            return None
        if not got or self.wall() - float(got[2]) >= self.stale_most:
            return None
        return got[1]

    def drop(self, key: tuple) -> None:
        """Forget ``key``'s kept answer, in memory and on disk (under every door stamp the
        disk holds it for: the current one)."""
        with self._lock:
            self._kept.pop(key, None)
        stamp = self._stamp()
        if self.disk is not None and stamp is not None:
            disk_key = self._disk_key(key, stamp)
            pop = getattr(self.disk, "pop", None)
            if disk_key is not None and callable(pop):
                try:
                    pop(disk_key, **self._disk_kw)
                    pop(("insight_last",) + disk_key[1:3], **self._disk_kw)
                except Exception:  # noqa: BLE001 - it ages out
                    pass

    def _keep(self, key: tuple, value: Any, stamp: int, at: float) -> None:
        """In memory (under the lock)."""
        self._kept[key] = (value, stamp, at)
        self._kept.move_to_end(key)
        while len(self._kept) > self.most:
            self._kept.popitem(last=False)

    @staticmethod
    def _disk_key(key: tuple, stamp: int) -> Optional[tuple]:
        """The file's key: the reader key, the statement and the door stamp (a moved stamp
        is another key: never served); None for a key that is not (reader key, statement)."""
        if not (isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], tuple)
                and isinstance(key[1], str)):
            return None
        return ("insight", key[0], key[1], int(stamp))

    def _from_disk(self, key: tuple, stamp: int) -> Optional[tuple[Any, float]]:
        """(answer, its age in seconds) kept on disk for ``key`` under ``stamp``, young
        enough to serve; None otherwise (any failure of the file is a miss)."""
        disk_key = self._disk_key(key, stamp) if self.disk is not None else None
        if disk_key is None:
            return None
        try:
            got = self.disk.get(disk_key, **self._disk_kw)
        except Exception:  # noqa: BLE001 - a miss
            return None
        if not got:
            return None
        _left, value, read_at = got
        age = max(0.0, self.wall() - float(read_at))
        return (value, age) if age < self.stale_most else None

    def age(self, key: tuple) -> Optional[float]:
        """Seconds since ``key``'s kept answer was read (None: none kept); one kept on
        disk under the door's stamp now is taken into memory first."""
        with self._lock:
            hit = self._kept.get(key)
        if hit is None and self.disk is not None:
            stamp = self._stamp()
            got = self._from_disk(key, stamp) if stamp is not None else None
            if got is None:
                return None
            with self._lock:
                self._keep(key, got[0], stamp, self.clock() - got[1])
            return got[1]
        return None if hit is None else self.clock() - hit[2]

    @staticmethod
    def _valid(stamp: Callable[[], Optional[int]]) -> Optional[int]:
        try:
            got = stamp()
        except Exception:  # noqa: BLE001 - an unreadable door: nothing kept is served
            return None
        return got if isinstance(got, int) else None

    def _stamp(self) -> Optional[int]:
        try:
            got = self.stamp()
        except Exception:  # noqa: BLE001 - an unreadable door: nothing kept is served
            return None
        return got if isinstance(got, int) else None


def door_stamp(connect: Callable[[], Any], schema: str = "biz") -> Callable[[], Optional[int]]:
    """The door's stamp of ``schema`` read on a ``connect()`` connection; None when it has no
    door or does not serve (the people exclusion not decided as it is now). The tiles of one
    visit ask it at once: they share one read that started after each asked
    (``shared_read``), never an older one."""
    from .shared_read import SharedRead

    def read() -> Optional[int]:
        from operating.pg import door
        with connect() as pg:
            got = door.check(pg, schema)
        if got is None or not got[1]:
            return None
        return int(got[0])
    return SharedRead(read)


class Keeper:
    """Reads ahead and keeps warm: the readers who opened Insights lately (their reader
    object, valid while their session is) and the statements they opened."""

    def __init__(self, kept: Kept, read: Callable[[Any, str], Any], *,
                 clock: Callable[[], float] = time.monotonic,
                 pool: Optional[ThreadPoolExecutor] = None) -> None:
        self.kept, self.read, self.clock = kept, read, clock
        self._pool = pool
        self._seen: "OrderedDict[tuple, tuple[Any, set[str], float]]" = OrderedDict()
        # the last background read of a (reader key, statement) that failed, until one succeeds:
        # a stored-only request says why nothing is stored instead of waiting for ever
        self.failed: dict[tuple, Exception] = {}
        # (reader key, statement) reads queued or running here: a poll of a cold tile asks again
        # every few seconds and must never queue a second read behind the first
        self._pending: set[tuple] = set()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        if kept.later is None:
            kept.later = self.again   # a stale kept answer is served while read again here

    def again(self, key: tuple) -> Optional[Future]:
        """Read ``key`` (reader key, statement) again in the background as the reader who
        opened it (``opened``); None when it is being read already. ``LookupError`` when
        that reader is not known here (the stale answer is then not served: read now)."""
        with self._lock:
            got = self._seen.get(key[0])
        session = getattr(got[0], "session", None) if got is not None else None
        if session is None or (callable(getattr(session, "expired", None))
                               and session.expired()):
            raise LookupError("reader not seen")
        with self.kept._lock:
            if key in self.kept._flight:
                return None
        return self._submit(got[0], key[0], key[1])

    def _submit(self, reader: Any, key: tuple, sql: str) -> Optional[Future]:
        """Queue one read of (key, sql) unless one is queued or running already (None)."""
        slot = (key, sql)
        with self._lock:
            if slot in self._pending:
                return None
            self._pending.add(slot)
        try:
            fut = self.pool().submit(self._quiet, reader, key, sql)
        except BaseException:
            with self._lock:
                self._pending.discard(slot)
            raise
        fut.add_done_callback(lambda _f: self._done(slot))
        return fut

    def _done(self, slot: tuple) -> None:
        with self._lock:
            self._pending.discard(slot)

    def pool(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=WORKERS,
                                            thread_name_prefix="insights-ahead")
        return self._pool

    def opened(self, reader: Any, sql: str) -> None:
        key = reader_key(reader)
        if key is None:
            return
        with self._lock:
            got = self._seen.get(key)
            sqls = got[1] if got else set()
            sqls.add(sql)
            self._seen[key] = (reader, sqls, self.clock())
            self._seen.move_to_end(key)
            while len(self._seen) > MOST_READERS:
                self._seen.popitem(last=False)

    def ahead(self, reader: Any, sqls: Sequence[str]) -> list[Future]:
        """Start the reads of ``sqls`` as ``reader`` that are neither kept fresh nor in
        flight (the dashboard was listed: its tiles are about to be asked for)."""
        key = reader_key(reader)
        if key is None:
            return []
        out = []
        for sql in dict.fromkeys(sqls):
            age = self.kept.age((key, sql))
            if age is not None and age < self.kept.fresh * 0.8:
                continue
            fut = self._submit(reader, key, sql)
            if fut is not None:
                out.append(fut)
        return out

    def _quiet(self, reader: Any, key: tuple, sql: str) -> None:
        try:
            # read again (joining one in flight): a kept answer this old is replaced
            self.kept.get((key, sql), lambda: self.read(reader, sql), again=True)
            self.failed.pop((key, sql), None)
        except Exception as exc:  # noqa: BLE001 - kept in ``failed``: the tile's request says why
            self.failed[(key, sql)] = exc
            # a read that now fails because the reader's access narrowed leaves no old
            # answer to be served again from memory or disk; one that could not finish
            # keeps the last good one (``Kept.last_good``)
            if getattr(exc, "code", None) not in UNFINISHED:
                self.kept.drop((key, sql))

    def tick(self) -> list[Future]:
        """Read again, as each recent reader whose session is still valid, the statements
        they opened whose kept answers are about to go stale; forget readers gone quiet."""
        now = self.clock()
        with self._lock:
            for key in [k for k, (_, _, at) in self._seen.items()
                        if now - at > ACTIVE_SECONDS]:
                self._seen.pop(key, None)
                for slot in [f for f in self.failed if f[0] == key]:
                    self.failed.pop(slot, None)   # a reader gone quiet keeps no old failures
            seen = [(k, r, set(s)) for k, (r, s, _) in self._seen.items()]
        out = []
        for key, reader, sqls in seen:
            session = getattr(reader, "session", None)
            if session is None or (callable(getattr(session, "expired", None))
                                   and session.expired()):
                with self._lock:
                    self._seen.pop(key, None)
                continue
            for sql in sqls:
                age = self.kept.age((key, sql))
                if age is None or age >= self.kept.fresh * 0.6:
                    fut = self._submit(reader, key, sql)
                    if fut is not None:
                        out.append(fut)
        return out

    def start(self) -> None:
        """The keep-warm loop on a daemon thread (once)."""
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                time.sleep(TICK_SECONDS)
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - next tick
                    pass
        self._thread = threading.Thread(target=loop, name="insights-keep", daemon=True)
        self._thread.start()
