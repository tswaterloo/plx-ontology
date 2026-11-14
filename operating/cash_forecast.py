"""A 13-week cash-flow forecast for one reader (``plx.aggregate`` mode ``cash_forecast``).

Inflows are the open receivables, each at its due date moved by its customer's own median
lateness over the trailing year (``proactive_cash.lateness``; a customer with fewer than
``LATENESS_MIN`` paid invoices: its due date); outflows are the open payables at their due
date and the open purchase orders (the one status definition, ``po_status.read``) at their
requested-by date, less what payables already invoice against their number. Every figure sums
cited values in one currency (``Decimal``; never converted, never summed across
currencies); an entry expected before today is listed as past expected, never moved into a
week. What the book holds is reader-independent (``proactive_cash.forecast_book``) and kept
per store generation and day; each answer counts only the entries whose every record the
reader may read and whose every marking it holds. A reader that does not hold ``finance``
gets no figure at all; one that may not read every record gets ``complete: false`` and no
counts of what it may not see.

``register`` serves it as a mode of ``plx.aggregate`` in the host tool table (no new listed
tool: the Ask agent is at its tool budget) and starts the book's read in the background, so a question waits at most ``WAIT_SECONDS`` for it.
Fixture runs are ``isolated-fixture``.
"""

from __future__ import annotations

from collections import defaultdict
import datetime as dt
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Mapping, MutableMapping

from ontology.json_contract import validate
from ontology import parent_watch

TOOL_NAME = "plx.aggregate"   # served as its mode ``cash_forecast``, never a listed tool
MODE = "cash_forecast"
FORMAT = "plx-cash-forecast/v1"
WEEKS = 13
LARGEST = 12            # the largest entries listed with their citations
WAIT_SECONDS = 35.0     # a question waits this long for the book (plx.aggregate's limit: 45 s)
FINANCE = "finance"
_TEXT = re.compile(r"^[A-Za-z0-9 ./#_-]{0,64}$")   # a code or number shown as it is


def _contracts() -> Mapping[str, Any]:
    from .ontology_tools import load_contracts
    return load_contracts()


_ANSWER = _contracts()["$defs"]["cash_forecast_answer"]


def _shown(text: str) -> str:
    """A party code or document number as the source states it, or nothing when it holds
    anything but plain code characters (never free source text)."""
    text = str(text or "").strip()[:64]
    return text if _TEXT.match(text) else ""


def _error(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message[:600],
                      **({"hint": hint[:600]} if hint else {})}}


def _money(n: Decimal) -> str:
    return format(n.quantize(Decimal("0.01")), "f")


def _marks(text: str) -> set[str] | None:
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return set(parsed) if isinstance(parsed, list) else None


def visible(entry: Any, audiences: Mapping[str, str], may_read: Callable[[str], bool],
            held: frozenset[str]) -> bool:
    """Whether a reader may count this entry: every record it rests on in an audience the
    reader may read, and every marking of every cited value held. Unknown fails closed."""
    for oid in entry.records:
        key = audiences.get(oid)
        if key is None or not may_read(key):
            return False
    for _handle, _oid, text in entry.handles:
        marks = _marks(text)
        if marks is None or not marks <= held:
            return False
    return bool(entry.handles)


def answer(book: Mapping[str, Any], as_of: dt.date, *, may_read: Callable[[str], bool],
           held: frozenset[str], weeks: int = WEEKS, currency: str | None = None
           ) -> dict[str, Any]:
    """The forecast one reader may see, from a book (``proactive_cash.forecast_book``)."""
    if FINANCE not in held:
        return _error("finance_not_held", "the cash forecast is made of finance-marked "
                      "figures, and this reader does not hold finance",
                      "ask someone who holds the finance marking")
    entries = [e for e in book["entries"] if currency is None or e.currency == currency]
    shown = [e for e in entries if visible(e, book["audiences"], may_read, held)]
    complete = len(shown) == len(entries)
    end = as_of + dt.timedelta(days=7 * weeks)
    cells: dict[tuple[int, str], dict[str, Any]] = {}
    past: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"in": Decimal(0),
                                                               "out": Decimal(0)})
    beyond = 0
    for e in shown:
        if e.day < as_of:
            past[e.currency][e.direction] += e.amount
            continue
        if e.day >= end:
            beyond += 1
            continue
        week = (e.day - as_of).days // 7 + 1
        cell = cells.setdefault((week, e.currency), {
            "in": Decimal(0), "out": Decimal(0), "orders": Decimal(0), "n_in": 0, "n_out": 0})
        cell[e.direction] += e.amount
        cell["n_" + e.direction] += 1
        if e.source == "purchase_order":
            cell["orders"] += e.amount
    currencies = sorted({c for _, c in cells} | set(past))
    out_weeks = []
    for week in range(1, weeks + 1):
        start = as_of + dt.timedelta(days=7 * (week - 1))
        rows = []
        for code in currencies:
            cell = cells.get((week, code))
            if cell is None:
                continue
            rows.append({"currency": code, "inflow": _money(cell["in"]),
                         "outflow": _money(cell["out"]),
                         "outflow_purchase_orders": _money(cell["orders"]),
                         "net": _money(cell["in"] - cell["out"]),
                         "inflow_items": cell["n_in"], "outflow_items": cell["n_out"]})
        out_weeks.append({"week": week, "starts": start.isoformat(),
                          "ends": (start + dt.timedelta(days=6)).isoformat(),
                          "by_currency": rows})
    totals = []
    for code in currencies:
        got = [cells[k] for k in cells if k[1] == code]
        inflow = sum((c["in"] for c in got), Decimal(0))
        outflow = sum((c["out"] for c in got), Decimal(0))
        totals.append({"currency": code, "inflow": _money(inflow), "outflow": _money(outflow),
                       "net": _money(inflow - outflow),
                       "past_expected_inflow": _money(past[code]["in"]),
                       "past_expected_outflow": _money(past[code]["out"])})
    in_window = [e for e in shown if as_of <= e.day < end]
    largest = sorted(in_window, key=lambda e: (-e.amount, e.object_id))[:LARGEST]
    result = {
        "mode": MODE, "format": FORMAT, "as_of": as_of.isoformat(), "weeks": out_weeks, "totals": totals,
        "largest": [{"direction": e.direction, "source": e.source,
                     "object_id": e.object_id, "party": _shown(e.party), "number": _shown(e.number),
                     "amount": _money(e.amount), "currency": e.currency,
                     "day": e.day.isoformat(), "basis": e.basis,
                     "citations": [h[0] for h in e.handles][:8]} for e in largest],
        "beyond_horizon": beyond,
        "basis": ("Receivables at their due date moved by the customer's median days paid "
                  "late over the last year; payables at their due date; open purchase "
                  "orders not yet invoiced at their requested-by date. One currency per figure, "
                  "never converted."),
        "complete": complete,
        "evidence": "store",
    }
    if complete:
        result["not_counted"] = dict(book.get("not_counted") or {})
    return result


def store_file(base: Any) -> str | None:
    """The served store's file, which a book is read over and keyed by: SQLite's main
    database (``PRAGMA database_list``); Postgres has none, so the projection path the host
    serves the store for (``served_path``, set by ``ontology_host.aggregate_modes``), which
    the proactive engine's store and people exclusion read the same way. None: no book
    (the mode answers ``store_unavailable``, never a failed host)."""
    try:
        rows = base.store.conn.execute("PRAGMA database_list").fetchall()
    except Exception:  # noqa: BLE001 - no listing: the served path decides
        rows = []
    main = next((row[2] for row in rows if row and len(row) > 2 and row[2]), None)
    if main:
        return str(main)
    served = getattr(base, "served_path", None)
    return str(served) if served else None


BOOK_CACHE_ENV = "PLX_BOOK_CACHE_DIR"   # where isolated books are kept (default: a private temp dir)
BOOK_KEEP_SECONDS = 2 * 86400


def isolated(read: Callable[..., Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Mark a module-level book reader as read in a child process (``_read_isolated``):
    the cash book's read peaks near 3 GB and the demand history's near 1.5 GB, and every
    process of the server (the app, each section and Ask worker) would hold its own."""
    read.isolated = True   # type: ignore[attr-defined]
    return read


def _cache_dir() -> Path | None:
    """The book cache directory, created private; None when it is not this user's alone
    (a pickle is only ever loaded from a directory nobody else can write)."""
    import tempfile
    path = Path(os.environ.get(BOOK_CACHE_ENV)
                or Path(tempfile.gettempdir()) / f"plx-books-{os.getuid()}")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = path.stat()
    except OSError:
        return None
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        return None
    return path


def _read_isolated(read: Callable[..., Mapping[str, Any]], path: str, as_of: dt.date,
                   digest: str) -> Mapping[str, Any]:
    """``read(path, as_of)`` run in a spawned child process, its book kept on disk per
    (reader, store file, generation, day): one read per machine and day, whichever server
    process asks first (the others wait on its lock), its memory returned when it ends."""
    import fcntl
    import hashlib
    import multiprocessing
    import pickle
    import time
    from concurrent.futures import ProcessPoolExecutor

    folder = _cache_dir()
    name = hashlib.sha256(json.dumps([read.__module__, read.__qualname__, str(path), digest,
                                      as_of.isoformat()]).encode()).hexdigest()[:40]

    def compute() -> Mapping[str, Any]:
        with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"),
                                 **parent_watch.pool_init()) as pool:
            return pool.submit(read, path, as_of).result()

    if folder is None:
        return compute()
    kept = folder / f"{name}.pkl"
    with open(folder / f"{name}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(kept, "rb") as handle:
                return pickle.load(handle)
        except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError):
            pass
        book = compute()
        try:
            part = folder / f"{name}.{os.getpid()}.part"
            with open(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
                pickle.dump(book, out, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(part, kept)
        except OSError:
            pass   # not kept: the next process reads it again
    cutoff = time.time() - BOOK_KEEP_SECONDS
    for old in folder.glob("*.pkl"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
                (folder / f"{old.stem}.lock").unlink(missing_ok=True)
        except OSError:
            pass
    return book


class _Books:
    """The reader-independent book per (store file, generation, day): read once, in the
    background on registration and on the first question of a new day. ``shared``: one per
    (store file, reader function) in the process, so every host the server opens (one per
    worker and session) asks the same book instead of reading its own copy."""

    _shared: dict[tuple, "_Books"] = {}
    _shared_lock = threading.Lock()

    @classmethod
    def shared(cls, path: str | None,
               read: Callable[[str, dt.date], Mapping[str, Any]]) -> "_Books":
        if not path:
            return cls(path, read)   # no store: never kept, the mode answers unavailable
        with cls._shared_lock:
            books = cls._shared.get((path, read))
            if books is None:
                books = cls._shared[(path, read)] = cls(path, read)
            return books

    def __init__(self, path: str, read: Callable[[str, dt.date], Mapping[str, Any]]) -> None:
        self.path, self.read = path, read
        self.lock = threading.Lock()
        self.kept: dict[tuple, dict[str, Any]] = {}
        self.events: dict[tuple, threading.Event] = {}

    def get(self, key: tuple, as_of: dt.date, wait: float) -> Mapping[str, Any] | None:
        with self.lock:
            if key in self.kept:
                return self.kept[key]
            event = self.events.get(key)
            if event is None:
                event = self.events[key] = threading.Event()
                threading.Thread(target=self._fill, args=(key, as_of, event), daemon=True,
                                 name="cash-forecast-book").start()
        event.wait(wait)
        with self.lock:
            return self.kept.get(key)

    def _fill(self, key: tuple, as_of: dt.date, event: threading.Event) -> None:
        try:
            if not self.path:
                raise LookupError("no_store_file")
            made = dict(_read_isolated(self.read, self.path, as_of, str(key[1]))
                        if getattr(self.read, "isolated", False)
                        else self.read(self.path, as_of))
        except Exception as exc:  # noqa: BLE001 - the error is the answer
            made = {"error": type(exc).__name__}
        with self.lock:
            self.kept = {k: v for k, v in self.kept.items() if k[:2] == key[:2]}
            self.kept[key] = made
            self.events.pop(key, None)
        event.set()


@isolated
def read_book(path: str, as_of: dt.date) -> dict[str, Any]:
    """The book over one store file (its own read-only connections; in a child process:
    ``isolated``)."""
    from . import po_status
    from . import proactive_cash as cash
    from .proactive_engine import _Store

    st = _Store(path)
    try:
        return cash.forecast_book(st, as_of, po_source=po_status.StoreSource(st))
    finally:
        st.close()


class CashForecastTool:
    """``plx.aggregate`` mode ``cash_forecast`` over a host's ``OntologyTools`` (store, policy, clock)."""

    def __init__(self, base: Any, read: Callable[[str, dt.date], Mapping[str, Any]] = read_book,
                 wait: float = WAIT_SECONDS) -> None:
        self.base, self.wait = base, wait
        self.books = _Books.shared(store_file(base), read)

    def key(self, as_of: dt.date) -> tuple:
        meta = self.base._store_meta() if hasattr(self.base, "_store_meta") else {}
        return (self.books.path, (meta or {}).get("content_digest", ""), as_of.isoformat())

    def prime(self, as_of: dt.date) -> None:
        self.books.get(self.key(as_of), as_of, 0.0)

    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        # the listed schema still asks plx.aggregate's own fields (measure, set or
        # object_type): the mode reads none of them, so they are ignored, never an error
        if not isinstance(arguments, dict) or arguments.get("mode") != MODE:
            return self._checked(_error("invalid_input", "mode must be cash_forecast",
                                        "ask with {\"mode\": \"cash_forecast\"}"))
        policy = self.base.policy
        held = frozenset(policy.held_markings(actor))
        if FINANCE not in held:
            return self._checked(answer({"entries": [], "audiences": {}}, dt.date.today(),
                                        may_read=lambda _k: False, held=held))
        as_of = self.base._as_known_at({}).date()
        book = self.books.get(self.key(as_of), as_of, self.wait)
        if book is None:
            return self._checked(_error("forecast_preparing", "the forecast's book is still "
                                        "being read from the store", "ask again in a minute"))
        if "error" in book:
            return self._checked(_error("store_unavailable", "the forecast's book could not "
                                        f"be read ({book['error']})"))
        return self._checked(answer(book, as_of, may_read=lambda key: policy.may_read(actor, key),
                                    held=held))

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        contracts = _contracts()
        schema = _ANSWER if "error" not in result else contracts["$defs"]["error_result"]
        problems = validate(result, schema, root=contracts)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + str(problems[0]))[:600])
        if len(json.dumps(result)) > int(contracts["limits"]["max_result_bytes"]) - 2000:
            return _error("result_too_large", "the forecast exceeds the result budget")
        return result


def register(table: MutableMapping[str, dict[str, Any]], base: Any, *,
             prime: bool = False, other_modes: bool = False) -> CashForecastTool:
    """Serve ``plx.aggregate`` mode ``cash_forecast`` in a host tool table: a call naming
    the mode goes to the forecast, a call naming another mode is refused here (or, with
    ``other_modes``, goes to the handler before it: ``aggregate_modes``' refusal naming
    every mode), and every other call goes to plx.aggregate as before. A table without
    plx.aggregate serves no forecast. ``prime``: start today's book now."""
    tool = CashForecastTool(base)
    entry = table.get(TOOL_NAME)
    if entry is None:
        return tool   # a host without plx.aggregate serves no mode of it
    aggregate = entry["handler"]

    def handler(arguments: Any, actor: Any) -> dict[str, Any]:
        if isinstance(arguments, dict) and "mode" in arguments and (
                arguments["mode"] == MODE or not other_modes):
            return tool.call(arguments, actor)
        return aggregate(arguments, actor)
    table[TOOL_NAME] = dict(entry, handler=handler)
    if prime:
        tool.prime(base._as_known_at({}).date())
    return tool


__all__ = ["FORMAT", "MODE", "TOOL_NAME", "CashForecastTool", "answer",
           "read_book", "register", "store_file", "visible"]
