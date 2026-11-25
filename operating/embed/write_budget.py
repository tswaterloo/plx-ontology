"""A rolling WAL-write budget for the search follower's embedding step.

The follower is a short run started again every minute. A per-run row or seconds cap alone
does not bound its writes: enough small runs still write unbounded WAL over an hour. This
budget records the WAL bytes measured around each committed embedding batch in a small
state file, keeps the last hour, and refuses another batch once the hour is full. The file
is accounting only; losing it is fail-closed for a budgeted run, never a licence to write
blind.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable


class WriteBudgetError(RuntimeError):
    """The rolling write budget could not be read or written."""


class RollingWriteBudget:
    """WAL bytes spent in the last ``window_seconds``, persisted across one-shot runs."""

    def __init__(self, path: Path, bytes_per_hour: float, *, window_seconds: float = 3600.0,
                 clock: Callable[[], float] = time.time) -> None:
        if not bytes_per_hour > 0:
            raise ValueError("bytes_per_hour must be positive")
        self.path, self.cap, self.window, self.clock = (Path(path), float(bytes_per_hour),
                                                        float(window_seconds), clock)
        self.events: list[tuple[float, float]] = []
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise WriteBudgetError(f"write_budget_unreadable:{type(exc).__name__}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
            raise WriteBudgetError("write_budget_unreadable:shape")
        events: list[tuple[float, float]] = []
        for event in raw["events"]:
            try:
                at, written = float(event[0]), float(event[1])
            except (TypeError, ValueError, IndexError) as exc:
                raise WriteBudgetError("write_budget_unreadable:event") from exc
            if written > 0:
                events.append((at, written))
        self.events = events

    def prune(self) -> None:
        floor = self.clock() - self.window
        self.events = [(at, written) for at, written in self.events if at >= floor]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(json.dumps({"version": 1, "events": self.events}, separators=(",", ":")),
                            encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            raise WriteBudgetError(f"write_budget_unwritable:{type(exc).__name__}") from exc

    @property
    def spent(self) -> float:
        self.prune()
        return sum(written for _, written in self.events)

    def blocked(self) -> bool:
        return self.spent >= self.cap

    def record(self, written: float) -> None:
        if written > 0:
            self.events.append((self.clock(), float(written)))
        self.prune()
        self.save()

    def status(self) -> dict[str, Any]:
        spent = self.spent
        return {"bytes_per_hour": int(self.cap), "window_seconds": int(self.window),
                "bytes_spent": int(spent), "bytes_left": int(max(0.0, self.cap - spent)),
                "blocked": spent >= self.cap, "path": str(self.path)}


class WalMeter:
    """Measure WAL written around one committed batch on this database.

    The diff includes any concurrent writer between the two positions. For this guard that
    is conservative in the right direction: other capture work keeps its priority and the
    optional embedding step writes less. On a standby (or if the positions cannot be read)
    the meter is unavailable and a budgeted embedding run must refuse before writing.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def position(self) -> str | None:
        try:
            value = self.conn.execute("SELECT pg_current_wal_insert_lsn()").fetchone()[0]
            self.conn.commit()
            return str(value)
        except Exception:  # guard-ok: unreadable is fail-closed; the caller refuses before writing
            try:
                self.conn.rollback()
            except Exception:  # guard-ok: rollback is best-effort; the outer handler already fails closed
                pass
            return None

    def since(self, before: str) -> int | None:
        try:
            value = self.conn.execute(
                "SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s::pg_lsn)::bigint",
                (before,)).fetchone()[0]
            self.conn.commit()
            return max(0, int(value))
        except Exception:  # guard-ok: unreadable is fail-closed; the caller refuses before writing
            try:
                self.conn.rollback()
            except Exception:  # guard-ok: rollback is best-effort; the outer handler already fails closed
                pass
            return None
