"""The mail capture's stand-alone commands (compact, sender-index, ocr-deferred,
deferred-attachments, backlog) and the cycle-lock runner they share.

Split out of ``integrations.capture_mail`` (module size cap) with no behaviour change:
``capture_mail`` keeps the same names as thin lazy wrappers, and every name used here
is read from ``capture_mail`` at call time, so a patch on ``capture_mail.<name>`` still
takes effect.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import capture_mail as cm


def compact_cli(argv: Sequence[str] | None = None) -> int:
    """``python3 -m integrations.capture_mail compact --state-dir D``: compact a store
    captured before compact storage (:meth:`MailCaptureStore.compact`), holding the
    host's cycle lock (``<D>/host/cycle.lock``) so no capture runs beside it. Prints
    counts and bytes only."""
    parser = argparse.ArgumentParser(prog="python3 -m integrations.capture_mail compact",
                                     description=compact_cli.__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True,
                        help="Workbench state directory holding mail-capture/")
    parser.add_argument("--no-vacuum", action="store_true",
                        help="prune and compress only; leave the SQLite file as it is")
    parser.add_argument("--defer-ocr", action="store_true",
                        help="also drop the bytes of scans whose OCR is pending (status "
                             "ocr_deferred; read later by the ocr-deferred command)")
    args = parser.parse_args(argv)
    return _locked(args.state_dir, lambda store: store.compact(
        vacuum=not args.no_vacuum, defer_ocr=args.defer_ocr))


def sender_index_cli(argv: Sequence[str] | None = None) -> int:
    """``python3 -m integrations.capture_mail sender-index --state-dir D``: key the
    messages stored before the sender lookup (:meth:`MailCaptureStore.fill_sender_keys`),
    holding the host's cycle lock. Opening the store as a writer adds the column and
    its index; this fills the rows stored before. Prints counts only."""
    parser = argparse.ArgumentParser(prog="python3 -m integrations.capture_mail sender-index",
                                     description=sender_index_cli.__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True,
                        help="Workbench state directory holding mail-capture/")
    args = parser.parse_args(argv)
    return _locked(args.state_dir, lambda store: store.fill_sender_keys())


def _locked(state_dir: str, work: Callable[[Any], dict[str, Any]], *,
            open_store: Callable[[Path], Any] = lambda state: cm.MailCaptureStore(state),
            exists: Callable[[Path], bool] = lambda state: cm.store_exists(state),
            missing: str = "mail_capture_missing", wait_seconds: float = 0,
            purpose: str = "maintenance", poll: float = 1.0) -> int:
    """``work`` on the store under ``state_dir`` (``open_store``; the mail store, or the
    SharePoint store's ``compact``) while holding the host's cycle lock
    (``<D>/host/cycle.lock``: no capture runs beside it); prints its counts.
    ``wait_seconds`` > 0: a held lock is waited for that long, with a gap request
    (``integrations.cycle_gap``) the continuous ingest answers by draining its captures
    and letting go of the lock; refused (``host_cycle_running``) only past that."""
    import fcntl

    from integrations import cycle_gap

    state = Path(state_dir).expanduser()
    if not exists(state):
        print(json.dumps({"ok": False, "error": missing}))
        return 2
    lock_dir = state / "host"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    asked: Path | None = None
    try:
        with open(lock_dir / "cycle.lock", "a+") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        print(json.dumps({"ok": False, "error": "host_cycle_running"}))
                        return 2
                    if asked is None:
                        asked = cycle_gap.request(lock_dir, purpose=purpose)
                    time.sleep(poll)
            try:
                store = open_store(state)
                try:
                    result = work(store)
                finally:
                    store.close()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        cycle_gap.withdraw(asked)   # after the lock is let go: the host takes it back
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


def ocr_deferred_cli(argv: Sequence[str] | None = None, *,
                     environ: Mapping[str, str] | None = None, sender: Any = None) -> int:
    """``python3 -m integrations.capture_mail ocr-deferred --state-dir D --live-audience F
    --budget-seconds N``: OCR the attachments whose OCR was deferred
    (:func:`ocr_deferred_pass`), re-fetching each original by one guarded Graph GET
    (credentials from the environment, as the capture). Needs a local OCR engine. Holds
    the host's cycle lock. Prints counts only."""
    from .graph_read import GraphMailReader, build_graph
    from .live_source_guard import BoundedBackoff

    parser = argparse.ArgumentParser(prog="python3 -m integrations.capture_mail ocr-deferred",
                                     description=ocr_deferred_cli.__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=cm.CAPTURE_PRINCIPAL)
    parser.add_argument("--budget-seconds", type=float, required=True)
    args = parser.parse_args(argv)
    if not args.budget_seconds > 0:
        print(json.dumps({"ok": False, "error": "budget_seconds_invalid"}))
        return 2
    if not cm._ocr_engine():
        print(json.dumps({"ok": False, "error": "ocr_engine_unavailable"}))
        return 2
    audience = cm.LiveSourceAudience.from_file(args.live_audience)
    env = os.environ if environ is None else environ
    api, _transport = build_graph(env, sender=sender, retry=BoundedBackoff(budget_seconds=60),
                                  capture=True)
    reader = GraphMailReader(api, audience)
    actor = cm.Actor(audience.tenant, frozenset({args.principal}), "restricted")

    def work(store: cm.MailCaptureStore) -> dict[str, Any]:
        store.people = reader.people   # every read applies this run's exclusion
        return cm.ocr_deferred_pass(store, reader, actor, audience,
                                 budget_seconds=args.budget_seconds)
    return _locked(args.state_dir, work)


def deferred_attachments_cli(argv: Sequence[str] | None = None, *,
                             environ: Mapping[str, str] | None = None,
                             sender: Any = None) -> int:
    """``python3 -m integrations.capture_mail deferred-attachments --state-dir D
    --live-audience F --budget-seconds N [--workers K] [--extract-workers E] [--limit L]
    [--min-free-gb G] [--dry-run]``: read the attachments not downloaded while the OCR
    backlog was full (:func:`deferred_attachments_pass`), by guarded Graph GETs
    (credentials and the people exclusion from the environment, as the capture). Holds
    the host's cycle lock. ``--dry-run`` reads nothing from Graph: it counts the rows a
    pass would read and those the exclusion withholds. Prints counts only."""
    from .graph_read import GraphMailReader, build_graph
    from .live_source_guard import BoundedBackoff

    parser = argparse.ArgumentParser(
        prog="python3 -m integrations.capture_mail deferred-attachments",
        description=deferred_attachments_cli.__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=cm.CAPTURE_PRINCIPAL)
    parser.add_argument("--budget-seconds", type=float, required=True)
    parser.add_argument("--workers", type=int, default=cm.MAX_WORKERS,
                        help="download threads (each mailbox still at most "
                             f"{cm.MAILBOX_CONCURRENCY} at once)")
    parser.add_argument("--extract-workers", type=int, default=None,
                        help="text extraction processes (default: every core but one)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--min-free-gb", type=float,
                        default=cm.DEFERRED_MIN_FREE_BYTES / (1 << 30))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.budget_seconds > 0:
        print(json.dumps({"ok": False, "error": "budget_seconds_invalid"}))
        return 2
    audience = cm.LiveSourceAudience.from_file(args.live_audience)
    env = os.environ if environ is None else environ
    api, _transport = build_graph(env, sender=sender, retry=BoundedBackoff(budget_seconds=60),
                                  capture=True)
    reader = GraphMailReader(api, audience)
    actor = cm.Actor(audience.tenant, frozenset({args.principal}), "restricted")

    def work(store: cm.MailCaptureStore) -> dict[str, Any]:
        store.people = reader.people   # every read applies this run's exclusion
        if args.dry_run:
            withheld = store.withheld_mailboxes(reader.people)
            if withheld is None:
                return {"error": "people_exclusion_unavailable"}
            marks = ",".join("?" * len(withheld))
            total, held = store.db.execute(
                "SELECT COUNT(*), " + (f"SUM(a.mailbox_id IN ({marks}))" if withheld
                                       else "0") +
                " FROM attachments a JOIN messages m ON m.l1_handle=a.message_handle AND "
                "m.superseded_at IS NULL WHERE a.content_status=?",
                (*withheld, cm.ATTACHMENT_DEFERRED)).fetchone()
            return {"dry_run": True, "deferred": total, "excluded": held or 0}
        return cm.deferred_attachments_pass(
            store, reader, actor, budget_seconds=args.budget_seconds, workers=args.workers,
            extract_workers=args.extract_workers, limit=args.limit,
            min_free_bytes=int(args.min_free_gb * (1 << 30)))
    return _locked(args.state_dir, work)


def backlog_cli(argv: Sequence[str] | None = None, *,
                environ: Mapping[str, str] | None = None, sender: Any = None,
                monotonic: Callable[[], float] = time.monotonic) -> int:
    """``python3 -m integrations.capture_mail backlog --state-dir D --live-audience F
    --budget-seconds N [--apply-fetched-seconds S --apply-fetched-dir P]
    [--deferred-attachments-seconds S] [--image-ocr-seconds S] [--host-child]``: the mail
    store's backlog passes (:func:`after_capture`) as their own process, never inside a
    mail capture, so a capture never waits on attachment reading or OCR (2026-11-13: a
    pass inside the mail child held fresh mail back 2 h 22 min). ``--budget-seconds`` is
    a hard deadline for every pass together, the work in flight included. SIGTERM drains
    it (``integrations.drain``): each pass stops at its next row, what is unfinished stays
    in the store's backlog for the next run, and the counts are printed. The ingest host
    runs it as its ``mail_backlog`` source with ``--host-child``: the host already holds
    the cycle lock and never runs it beside a mail capture (one writer to the store);
    without that flag it takes the cycle lock like the other stand-alone commands.
    Credentials and the people exclusion come from the environment, as the capture's.
    Prints counts only."""
    from .graph_read import GraphMailReader, build_graph
    from .live_source_guard import BoundedBackoff

    parser = argparse.ArgumentParser(prog="python3 -m integrations.capture_mail backlog",
                                     description=backlog_cli.__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the capture principal")
    parser.add_argument("--principal", default=cm.CAPTURE_PRINCIPAL)
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="every pass together (default: the passes' seconds summed)")
    parser.add_argument("--host-child", action="store_true",
                        help="run by the ingest host, which holds the cycle lock")
    cm.add_backlog_arguments(parser)
    args = parser.parse_args(argv)
    try:
        cm.check_after(args)
        if args.budget_seconds is not None and not (
                0 < args.budget_seconds < float("inf")):
            raise ValueError("budget_seconds must be positive")
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": cm.code_of(str(exc))}))
        return 2
    cm.drain.install()
    clock = cm.drain.clock(monotonic)
    audience = cm.LiveSourceAudience.from_file(args.live_audience)
    env = os.environ if environ is None else environ
    api, _transport = build_graph(env, sender=sender, retry=BoundedBackoff(budget_seconds=60),
                                  capture=True)
    reader = GraphMailReader(api, audience)
    actor = cm.Actor(audience.tenant, frozenset({args.principal}), "restricted")
    started = monotonic()

    def work(store: cm.MailCaptureStore) -> dict[str, Any]:
        store.people = reader.people   # every read applies this run's exclusion
        passes = cm.after_capture(store, reader, actor, args, monotonic=clock,
                               budget_seconds=args.budget_seconds) or {}
        return {"check": "graph.mail_backlog", "passes": passes,
                "drained": cm.drain.requested(),
                "seconds": round(monotonic() - started, 1)}

    if not args.host_child:
        return _locked(args.state_dir, work)
    state = Path(args.state_dir).expanduser()
    if not cm.store_exists(state):
        print(json.dumps({"ok": False, "error": "mail_capture_missing"}))
        return 2
    store = cm.MailCaptureStore(state)
    try:
        result = work(store)
    finally:
        store.close()
    print(json.dumps({"ok": True, **result}, sort_keys=True), flush=True)
    return 0
