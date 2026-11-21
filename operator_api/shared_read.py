"""One read shared by the requests that ask for it at the same moment, never an older one.

A screen of many tiles asks the same small check (a door stamp, an exclusion stamp) once per
tile, all at once: 22 Insights tiles made 22 Postgres reads of the same stamp in the same
100 ms, each waiting for a connection and the interpreter lock (11-21 profile: about half
of a tile's request time). ``SharedRead(read)()`` is ``read()``'s answer from a read that
STARTED AFTER the caller asked: a caller that arrives while a read is under way waits for
the next one (which starts when that ends, for every caller that waited), so nothing a
caller gets was read before it asked, and a change made before the ask is always seen. One
read runs at a time; a failed read raises its error to every caller it served.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class SharedRead(Generic[T]):
    def __init__(self, read: Callable[[], T]) -> None:
        self._read = read
        self._cond = threading.Condition()
        self._busy = False
        self._started = 0    # reads started so far (numbered 1, 2, ...)
        self._finished = 0   # the number of the last read that ended
        self._last: tuple[bool, Any] = (False, None)

    def __call__(self) -> T:
        with self._cond:
            want = self._started + 1   # the first read that starts after this ask
            while True:
                if self._finished >= want:
                    ok, value = self._last
                    if ok:
                        return value
                    raise value
                if not self._busy:
                    self._busy = True
                    self._started += 1
                    number = self._started
                    break
                self._cond.wait()
        try:
            result: tuple[bool, Any] = (True, self._read())
        except BaseException as exc:  # noqa: BLE001 - handed to every caller it served
            result = (False, exc)
        with self._cond:
            self._finished, self._last, self._busy = number, result, False
            self._cond.notify_all()
        if result[0]:
            return result[1]
        raise result[1]
