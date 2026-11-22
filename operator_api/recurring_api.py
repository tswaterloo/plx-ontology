"""A person's recurring instructions kept, listed, changed and stopped
(``/api/instructions/recurring``): the trigger is read again on the server from the line
itself (``operating.instruct.recurring``), never taken from the request; every row is the
reader's own (``reader.key``), and another's row answers 404 as one that does not exist.
A due one runs (``recurring_run_due``, once a minute in the primary process only, never the
standby) as its owner through the Ask box's instruction path (``instruct_ask``) with the
"every ..." words taken out: read as the owner may read, people excluded, and each run
prepares work for the owner to confirm; nothing is written or sent on its own. An owner who
is not signed in (no unexpired session), or whose session was revoked, is not run as: the
run is skipped and says so."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from operating import instruct as ins
from operating import recurring_instructions as ri

from . import models as m
from .auth import Reader
from .errors import UserFacing


class RecurringMixin:
    def _recurring_store(self) -> ri.RecurringStore:
        data_dir = getattr(self, "data_dir", None)
        if data_dir is None:
            raise UserFacing("busy", "Recurring instructions are not kept on this server.", 503)
        return ri.RecurringStore(data_dir)

    def recurring_list(self, reader: Reader) -> list[dict[str, Any]]:
        return self._recurring_store().list(reader.key)

    def _recurring_mirror(self) -> None:
        """The active event rules, for the autopilot's fast feed to note fires against."""
        state = getattr(self, "state_dir", None)
        if state is None:
            return
        from pathlib import Path
        from operating import recurring_events as rev
        try:
            rev.write_rules(Path(state) / "autopilot", [
                {"rid": r["id"], "owner": r["owner"], "email": r["params"].get("email", ""),
                 "from": r["params"].get("from", ""), "subject": r["params"].get("subject", "")}
                for r in self._recurring_store().events()])
        except OSError as error:
            logging.getLogger("operator_api").warning("recurring rules not mirrored: %s",
                                                      type(error).__name__)

    def recurring_keep(self, reader: Reader, text: str, label: Optional[str] = None,
                       hour: Optional[int] = None, sender: Optional[str] = None,
                       subject: Optional[str] = None) -> dict[str, Any]:
        try:
            line = ins.clean_text(text)
        except ins.InstructRefused:
            raise UserFacing("invalid_request", "Type the instruction to keep.", 422) from None
        trigger = ins.recurring(line)
        if trigger is None:
            raise UserFacing("invalid_request", "That line does not say when it repeats "
                             "(\"every Monday\", \"every day\").", 422)
        if trigger["kind"] == "message.received":   # only the owner's own mailbox
            trigger = {**trigger, "params": {"from": (sender or "").strip()[:120],
                                             "subject": (subject or "").strip()[:120],
                                             "email": (getattr(reader, "email", None) or "")}}
        try:
            row = self._recurring_store().add(
                reader.key, line, (label or trigger["words"]).strip()[:120], trigger,
                ri.DEFAULT_HOUR if hour is None else hour)
        except ri.RecurringRefused as refused:
            raise UserFacing("invalid_request", str(refused), 422) from None
        self._recurring_mirror()
        return row

    def recurring_change(self, reader: Reader, rid: str, **change: Any) -> dict[str, Any]:
        try:
            row = self._recurring_store().change(reader.key, rid, **change)
        except ri.RecurringRefused as refused:
            raise UserFacing("invalid_request", str(refused), 422) from None
        if row is None:
            raise UserFacing("not_found", "No such recurring instruction of yours.", 404)
        self._recurring_mirror()
        return row

    def recurring_stop(self, reader: Reader, rid: str) -> None:
        if not self._recurring_store().stop(reader.key, rid):
            raise UserFacing("not_found", "No such recurring instruction of yours.", 404)
        self._recurring_mirror()

    def recurring_run_due(self, now: Optional[float] = None) -> int:
        """Run each due recurring instruction once, as its owner; how many were due."""
        data_dir = getattr(self, "data_dir", None)
        if data_dir is None or getattr(self, "identity_for", None) is None:
            return 0
        store = ri.RecurringStore(data_dir)
        rows = store.due(now)
        state = getattr(self, "state_dir", None)
        if not rows and not (state is not None and (
                __import__("pathlib").Path(state) / "autopilot" / "recurring-fires.sqlite").exists()):
            return 0
        readers: dict[str, Reader] = {}
        for oid, held in self.known_readers().items():  # type: ignore[attr-defined]
            try:
                if self.revoked(str(held["ref"])):  # type: ignore[attr-defined]
                    continue
                reader = Reader(self.identity_for(oid, int(held["exp"])),  # type: ignore[attr-defined]
                                self.reader_mode, "", None)  # type: ignore[attr-defined]
                readers[reader.key] = reader
            except Exception:
                continue
        for row in rows:
            # moved on before it runs: a restart mid-run never runs it twice, and a row
            # another process took first is left to it
            if not store.claim(row, now):
                continue
            digest = row["params"].get("digest")
            store.result(row["owner"], row["id"],
                         self._digest_once(readers.get(row["owner"]), row)
                         if digest else
                         self._recurring_once(readers.get(row["owner"]), row["text"]))
        return len(rows) + self._recurring_fired(store, readers)

    def _recurring_fired(self, store: ri.RecurringStore, readers: dict[str, Reader]) -> int:
        """Run each fire the autopilot's fast feed noted for a "whenever" instruction, as its
        owner, on the mail that fired it (read as the owner may read it)."""
        state = getattr(self, "state_dir", None)
        if state is None:
            return 0
        from pathlib import Path
        from operating import recurring_events as rev
        fires = rev.Fires(Path(state) / "autopilot").take()
        for fire in fires:
            row = store.get(fire["owner"], fire["rid"])
            if row is None or row["state"] != "active":
                continue
            store.ran(row["owner"], row["id"], "Running")
            store.result(row["owner"], row["id"], self._recurring_once(
                readers.get(row["owner"]), row["text"], object_id=fire["object_id"]))
        return len(fires)

    def _recurring_once(self, reader: Optional[Reader], text: str,
                        object_id: Optional[str] = None) -> str:
        if reader is None:
            return "Not run: you were not signed in. Sign in so it can run as you."
        try:
            answer = None
            until = time.monotonic() + RECURRING_RUN_SECONDS
            events = self.instruct_ask(  # type: ignore[attr-defined]
                reader, m.AskRequest(question=ins.one_off(text)[:1000],
                                     object_id=object_id))
            for event, payload in events:
                if event == "answer":
                    answer = payload
                if time.monotonic() > until:   # the others due this minute are not held
                    close = getattr(events, "close", None)
                    if callable(close):
                        close()
                    return "Not run: it took too long; the next run tries again."
        except UserFacing as refused:
            return f"Not run: {refused.message}"
        except Exception as error:  # noqa: BLE001 - said on the row; the next run tries again
            logging.getLogger("operator_api").warning("recurring run failed: %s",
                                                      type(error).__name__)
            return "Not run: the run failed."
        part = getattr(answer, "instruction", None)
        made = len(getattr(part, "writes", None) or []) + len(getattr(part, "documents", None)
                                                               or [])
        if made:
            return f"Prepared {made} for you to confirm"
        return "Ran; nothing to prepare" if answer is not None else "Not run: no answer."

    # ---- digests: a switched-on digest is a recurring row with params.digest ----------------

    def _digest_store(self) -> Any:
        from operating import digests
        data_dir = getattr(self, "data_dir", None)
        if data_dir is None:
            raise UserFacing("busy", "Digests are not kept on this server.", 503)
        return digests.ReportStore(data_dir)

    def digest_list(self, reader: Reader) -> list[dict[str, Any]]:
        """Every digest on offer with whether it is on for you, its hour and its latest report
        (yours alone). Off unless you switched it on."""
        from operating import digests
        mine = {r["params"].get("digest"): r for r in self._recurring_store().list(reader.key)
                if r["params"].get("digest")}
        out = []
        for spec in digests.CATALOG:
            row = mine.get(spec["id"])
            report = self._digest_store().get(reader.key, row["id"]) if row else None
            out.append({"id": spec["id"], "title": spec["title"], "when": spec["when"],
                        "on": bool(row and row["state"] == "active"),
                        "hour": row["hour"] if row else spec["hour"],
                        "last_run": row["last_run"] if row else None,
                        "next_run": row["next_run"] if row else None,
                        "last_result": row["last_result"] if row else None,
                        "report": report["text"] if report else None,
                        "report_at": report["ran_at"] if report else None})
        return out

    def digest_set(self, reader: Reader, digest_id: str, on: bool,
                   hour: Optional[int] = None) -> list[dict[str, Any]]:
        from operating import digests
        spec = digests.entry(digest_id)
        if spec is None:
            raise UserFacing("not_found", "No such digest.", 404)
        store = self._recurring_store()
        row = next((r for r in store.list(reader.key)
                    if r["params"].get("digest") == digest_id), None)
        try:
            if on and row is None:
                store.add(reader.key, spec["question"], spec["title"], digests.trigger(spec),
                          spec["hour"] if hour is None else hour)
            elif on:
                store.change(reader.key, row["id"], state="active",
                             hour=row["hour"] if hour is None else hour)
            elif row is not None:
                store.stop(reader.key, row["id"])
                self._digest_store().drop(reader.key, row["id"])
        except ri.RecurringRefused as refused:
            raise UserFacing("invalid_request", str(refused), 422) from None
        return self.digest_list(reader)

    def _digest_once(self, reader: Optional[Reader], row: dict[str, Any]) -> str:
        """Run one digest as its owner through the plain Ask (read only: nothing is prepared
        or sent) and keep the answer's words for them."""
        from operating import digests
        spec = digests.entry(row["params"].get("digest", ""))
        if reader is None:
            return "Not run: you were not signed in. Sign in so it can run as you."
        if spec is None:
            return "Not run: this digest is no longer offered."
        try:
            answer = None
            until = time.monotonic() + RECURRING_RUN_SECONDS
            events = self.ask(reader, m.AskRequest(question=spec["question"]))  # type: ignore[attr-defined]
            for event, payload in events:
                if event == "answer":
                    answer = payload
                if time.monotonic() > until:
                    close = getattr(events, "close", None)
                    if callable(close):
                        close()
                    return "Not run: it took too long; the next run tries again."
        except UserFacing as refused:
            return f"Not run: {refused.message}"
        except Exception as error:  # noqa: BLE001 - said on the row; the next run tries again
            logging.getLogger("operator_api").warning("digest run failed: %s",
                                                      type(error).__name__)
            return "Not run: the run failed."
        text = str(getattr(answer, "direct_answer", "") or "").strip()
        if not text:
            return "Not run: no answer."
        self._digest_store().put(reader.key, row["id"], text)
        return "Ran; the report is on your Digests list"

    def start_recurring(self) -> None:
        """The minute loop that runs due recurring instructions (the primary process)."""
        if getattr(self, "_recurring_thread", None) is not None:
            return

        def loop() -> None:
            stopped = threading.Event()   # a daemon thread: ends with the process
            while not stopped.wait(RECURRING_TICK_SECONDS):
                try:
                    self.recurring_run_due()
                except Exception:  # noqa: BLE001 - the next minute tries again
                    logging.getLogger("operator_api").exception("recurring runs failed")

        self._recurring_thread = threading.Thread(target=loop, name="recurring-runs",
                                                  daemon=True)
        self._recurring_thread.start()


RECURRING_TICK_SECONDS = 60.0
RECURRING_RUN_SECONDS = 240.0   # one run at most; a slow one never holds the rest
