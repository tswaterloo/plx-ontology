"""Readers' doors decided ahead (SERVE decision Q1, speed specs 1.2.2): a GET never decides a
reader's policy or door. ``Doors.decide`` runs the exact code that decides them today (the
reader's workspace policy, ``ontology_tools_sql.reader_door`` and ``_decide``) on a
background thread; a request takes the decided door from memory (``get``) and reads on its
own thread through the page pool (``operating.page_reads.PageReads.call`` with ``door=``).

Nothing is stored outside the process, and nothing is kept past ``MAX_AGE_SECONDS`` (the same
120 s ``_decide`` keeps a door): the refresher decides every recently served reader again
every ``REFRESH_SECONDS`` and at once when the door's stamp moves (read every
``LOOP_SECONDS``), on ``DECIDERS`` threads, the most recently served first, so a revoked grant
stops being served within ``REFRESH_SECONDS`` plus one pass. Readers are decided at start
(every reader with a recent session, before the process says it is ready: ``ready``) and at
sign-in (before the session cookie is returned). A GET whose reader has no door (a refresher
bug: alarmed) is decided inline once (``decide_now``), logged as
``operator_api.serving_miss {"why": "door_not_ahead"}``.

The key is everything a reader's policy is decided from that the session carries (tenant,
principals, clearance, the declared full-read grant, service, mode, view-as), so two readers
can never share a door.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import threading
import time
from typing import Any, Optional

from operating.ontology_tools_sql import DOOR_MAX_AGE_SECONDS as MAX_AGE_SECONDS
from .serving import DOOR_KEEP_RECENT_SECONDS as RECENT_SECONDS   # not served this long: dropped

REFRESH_SECONDS = MAX_AGE_SECONDS / 2   # decided again at least this often while served
LOOP_SECONDS = 2.0            # the refresher's pass (the stamp is read each pass)
DECIDERS = max(1, min(8, (os.cpu_count() or 2) // 2))   # decisions at once
LOG_EVERY_SECONDS = 300.0
log = logging.getLogger("operator_api.doors")
miss_log = logging.getLogger("operator_api.serving_miss")


def reader_key(reader: Any) -> Optional[tuple]:
    """The reader's door key, or None when the session does not say who it is (never kept)."""
    s = getattr(reader, "session", None)
    principals = getattr(s, "principals", None)
    if s is None or not principals:
        return None
    return (getattr(s, "tenant", None), tuple(sorted(principals)), getattr(s, "clearance", None),
            getattr(s, "access", None), getattr(s, "service", None),
            getattr(reader, "mode", None), bool(getattr(s, "view_as", False)))


class Held:
    __slots__ = ("door", "dsn", "stamp", "decided_at", "served_at", "reader", "comms")

    def __init__(self, door: Any, dsn: str, stamp: Optional[int], reader: Any,
                 comms: Optional[dict] = None) -> None:
        self.door, self.dsn, self.stamp, self.reader = door, dsn, stamp, reader
        self.comms = comms   # the communications views' six settings, decided with the door
        self.decided_at = time.monotonic()
        self.served_at = self.decided_at


class Doors:
    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._held: dict[tuple, Held] = {}
        self._lock = threading.Lock()
        self._started = False
        self._first_pass = threading.Event()
        self._stamp: Optional[int] = None
        self._counts = {"decides": 0, "inline": 0, "failed": 0}
        self._last_log = time.monotonic()

    # ------------------------------------------------------------------ the request side
    def get(self, reader: Any) -> Optional[tuple[Any, str]]:
        """(the reader's door, the store's dsn) decided ahead and younger than
        ``MAX_AGE_SECONDS`` at the stamp last read, else None (and the reader is decided
        ahead from now on)."""
        key = reader_key(reader)
        if key is None:
            return None
        now = time.monotonic()
        with self._lock:
            held = self._held.get(key)
            if held is not None:
                held.served_at = now
                held.reader = reader
        if held is None or now - held.decided_at > MAX_AGE_SECONDS or (
                self._stamp is not None and held.stamp != self._stamp):
            self._counts["inline"] += 1
            miss_log.info(json.dumps({"why": "door_not_ahead", "held": held is not None}))
            self.want(reader)
            return None
        return held.door, held.dsn, held.comms

    def stamp(self) -> Optional[int]:
        """The door stamp the refresher read last (None before its first pass)."""
        return self._stamp

    def access_mark(self, reader: Any) -> Optional[str]:
        """The stable access digest of ``reader``'s current decided door, or None before
        one is fresh at the stamp last read. This is the request-side key for a kept page:
        unlike the schema-wide stamp it does not move when a refresh rebuilds the tables
        but leaves what this reader may read unchanged. No decision is started here; an
        unknown or stale door simply carries no header, so nothing new is stored."""
        key = reader_key(reader)
        if key is None:
            return None
        now = time.monotonic()
        with self._lock:
            held = self._held.get(key)
            if held is not None:
                held.served_at = now
                held.reader = reader
        if held is None or now - held.decided_at > MAX_AGE_SECONDS or (
                self._stamp is not None and held.stamp != self._stamp):
            return None
        door = held.door
        decided = door if isinstance(door, dict) else getattr(door, "decided", None)
        if not isinstance(decided, dict):
            return None
        mark = decided.get("access_mark")
        return mark if isinstance(mark, str) and mark else None

    def decide_now(self, reader: Any) -> Optional[tuple[Any, str]]:
        """Decide ``reader``'s door on this thread (a GET that found none: ``get`` logged it),
        then the door, or None when it could not be decided (the read is refused)."""
        if not self.decide(reader):
            return None
        key = reader_key(reader)
        with self._lock:
            held = self._held.get(key) if key is not None else None
        return None if held is None or held.door is None else (held.door, held.dsn, held.comms)

    def ready(self) -> bool:
        """Whether the readers known at start were each decided once (or refused, logged)."""
        return self._first_pass.is_set()

    def want(self, reader: Any) -> None:
        """Decide ``reader`` on the refresher's next pass."""
        key = reader_key(reader)
        if key is None:
            return
        with self._lock:
            held = self._held.get(key)
            if held is None:
                self._held[key] = Held(None, "", None, reader)
                self._held[key].decided_at = float("-inf")
            else:
                held.reader = reader

    # ------------------------------------------------------------------ deciding
    def decide(self, reader: Any) -> bool:
        """Decide ``reader``'s door now, on a workspace worker (the policy) and then in a
        read transaction of the page pool (the door at the current stamp, kept by
        ``_decide`` for the request). False when it could not be decided (the reader is
        then read the old way)."""
        key = reader_key(reader)
        if key is None:
            return False
        worker = getattr(self.backend, "access_worker", None) or getattr(self.backend, "reads",
                                                                        None)
        if worker is None:
            return False
        from operating.page_reads import grant_ahead
        try:
            # no deadline (business rule 11-23): the decision is waited for to its end; the owner's
            # first one reads the Teams windows for a minute and more
            if hasattr(worker, "submit"):
                door, dsn, stamp, comms = worker.submit(reader, grant_ahead).result()
            else:
                door, dsn, stamp, comms = worker.run(reader, grant_ahead)
        except Exception as exc:  # noqa: BLE001 - logged; the reader is read the old way
            self._counts["failed"] += 1
            log.warning("door not decided ahead: %s", type(exc).__name__)
            with self._lock:
                self._held.pop(key, None)
            return False
        with self._lock:
            held = self._held.get(key)
            if (held is not None and held.door is not None
                    and held.stamp is not None and stamp is not None
                    and stamp < held.stamp):
                # decisions finish out of order on the decider threads; a slower decision
                # from an older stamp must not overwrite a newer held door for the same key
                return True
            new = Held(door, dsn, stamp, reader, comms)
            if held is not None:
                new.served_at = held.served_at
            self._held[key] = new
            if stamp is not None and (self._stamp is None or stamp >= self._stamp):
                # decisions finish out of order on the decider threads; the schema stamp
                # itself never goes back, so a slower older decision must not move this back
                self._stamp = stamp
        self._counts["decides"] += 1
        return True

    def _due(self) -> list[Any]:
        """Readers to decide now, the most recently served first."""
        now = time.monotonic()
        out = []
        with self._lock:
            for key, held in list(self._held.items()):
                if now - held.served_at > RECENT_SECONDS:
                    self._held.pop(key, None)
                    continue
                if now - held.decided_at >= REFRESH_SECONDS or held.door is None or (
                        self._stamp is not None and held.stamp != self._stamp):
                    out.append((held.served_at, held.reader))
        return [r for _, r in sorted(out, key=lambda x: -x[0])]

    def _read_stamp(self) -> None:
        from operating.page_reads import door_stamp
        dsn = next((h.dsn for h in list(self._held.values()) if h.dsn), None)
        if dsn is None:
            return
        try:
            self._stamp = door_stamp(dsn)
        except Exception as exc:  # noqa: BLE001 - logged; every door ages out by itself
            log.warning("door stamp not read: %s", type(exc).__name__)

    def run_once(self) -> int:
        self._read_stamp()
        due = self._due()
        if len(due) > 1:
            with ThreadPoolExecutor(DECIDERS, thread_name_prefix="doors-decide") as pool:
                done = sum(1 for ok in pool.map(self.decide, due) if ok)
        else:
            done = sum(1 for reader in due if self.decide(reader))
        self._first_pass.set()
        now = time.monotonic()
        if now - self._last_log >= LOG_EVERY_SECONDS:
            with self._lock:
                held = [h for h in self._held.values() if h.door is not None]
            log.info(json.dumps({"held": len(held), "oldest_s": round(max(
                (now - h.decided_at for h in held), default=0.0), 1),
                "decides_5m": self._counts["decides"], "inline_5m": self._counts["inline"],
                "failed_5m": self._counts["failed"]}))
            self._counts = {"decides": 0, "inline": 0, "failed": 0}
            self._last_log = now
        return done

    def start(self, readers: Any = ()) -> None:
        """The refresher thread (once); ``readers``: decided on its first pass, before
        ``ready`` (the readers with a recent session)."""
        with self._lock:
            if self._started:
                return
            self._started = True
        for reader in readers:
            self.want(reader)

        def loop() -> None:
            while True:
                try:
                    self.run_once()
                except Exception as exc:  # noqa: BLE001 - logged; the next pass tries again
                    log.warning("doors pass failed: %s", type(exc).__name__)
                time.sleep(LOOP_SECONDS)
        threading.Thread(target=loop, name="doors-ahead", daemon=True).start()
