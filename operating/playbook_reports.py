"""Append-only, hash-chained store of playbook evaluation reports (M3-B).

An evaluation report (``playbook_eval.evaluate``) is evidence. This store keeps each one exactly as
produced, in a SQLite table that cannot be updated or deleted (triggers) and whose rows are chained
(``prev_hash`` -> ``row_hash``) like ``cases.CaseStore`` events. Every read re-verifies the row hash,
the report's own ``report_sha256`` and that the indexed columns still match the stored report.

Audience: a report belongs to its case's audience. The store is built with ``can_read(actor,
case_id)``; the default denies everyone. Every read (``get``, ``list``, ``latest``, ``integrity``)
needs an actor, and a report the actor may not read is indistinguishable from an absent one.
``record`` also requires the recorder (``evaluated_by``) to be able to read the case, so a duplicate
answer never reveals a report to someone outside the audience. The store performs no business writes.

Sources: the case audience is not a source audience. A stored report's ``evidence_manifest`` names
every cited source (handle, digests, per-role identity). ``get``/``latest`` return it filtered per
reader: an entry whose source the reader may not read keeps its status and what cites it but loses
its handle, digests and identity (a ``withheld`` marker instead), and every source handle
anywhere else in the returned report is replaced by the same marker. The
filter is ``can_read_source(actor, case_id, handle)`` when given. Without it only the recorder
(who read the sources under their own permission) sees the live-source handles (``original://``,
``sharepoint://``, ``graph-mail://``, ``bc://``); ``api://`` mirror rows stay with the case audience. A filtered copy no longer matches
``report_sha256`` and says so (``report_filtered``); the stored row is untouched.

Playbook changes: the same file keeps, in a second append-only chain (``playbook_change``), the
proposed playbook versions, their comparisons, failed evaluations and adoptions of
``operating.semantic_proposals``
(``changes``). Those records name proposer, evaluator and steward principals, frozen case ids
and evidence citations; they have no per-record audience and no reader API here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from operating.playbook_eval import verify_report_hash

_GENESIS = "0" * 64
_SCHEMA_VERSION = 1
_MAX_REPORT_BYTES = 8_000_000
CanRead = Callable[[Any, str], bool]
CanReadSource = Callable[[Any, str, str], bool]
WITHHELD = "withheld:source_not_readable_by_this_reader"
_AUDIENCE_GATED = ("original://", "sharepoint://", "graph-mail://", "bc://")
_HANDLE = re.compile(r"(?:api|original|sharepoint|graph-mail|bc)://[^\s\"')\]]+")
_ENTRY_SOURCE_KEYS = ("handle", "recorded_digest", "current_digest", "reread_payload_sha256",
                      "identity")

_DDL = """
CREATE TABLE IF NOT EXISTS playbook_report (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_sha256 TEXT NOT NULL UNIQUE,
  case_id TEXT NOT NULL,
  playbook_id TEXT NOT NULL,
  playbook_version TEXT NOT NULL,
  definition_sha256 TEXT NOT NULL,
  evaluator_version TEXT NOT NULL,
  case_boundary_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  passed_all INTEGER NOT NULL CHECK(passed_all IN (0,1)),
  passed_all_clean INTEGER NOT NULL CHECK(passed_all_clean IN (0,1)),
  recorded_at TEXT NOT NULL,
  recorded_by TEXT NOT NULL,
  transcript_source TEXT NOT NULL,
  report_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  row_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_playbook_report_case ON playbook_report(case_id, playbook_id, id);
CREATE TRIGGER IF NOT EXISTS playbook_report_no_update BEFORE UPDATE ON playbook_report
BEGIN SELECT RAISE(ABORT, 'playbook_report_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS playbook_report_no_delete BEFORE DELETE ON playbook_report
BEGIN SELECT RAISE(ABORT, 'playbook_report_is_append_only'); END;
CREATE TABLE IF NOT EXISTS playbook_change (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  row_hash TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS playbook_change_no_update BEFORE UPDATE ON playbook_change
BEGIN SELECT RAISE(ABORT, 'playbook_change_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS playbook_change_no_delete BEFORE DELETE ON playbook_change
BEGIN SELECT RAISE(ABORT, 'playbook_change_is_append_only'); END;
"""

_COLUMNS = ("report_sha256", "case_id", "playbook_id", "playbook_version", "definition_sha256",
            "evaluator_version", "case_boundary_sha256", "manifest_sha256", "passed_all",
            "passed_all_clean", "recorded_at", "recorded_by", "transcript_source", "report_json")
_SUMMARY_COLUMNS = tuple(name for name in _COLUMNS if name != "report_json")


class ReportError(ValueError):
    """The report or request is not acceptable."""


class ReportIntegrityError(RuntimeError):
    """A stored report or the chain no longer verifies."""


class ReportNotFound(LookupError):
    """No such report, or the actor may not read its case (deliberately indistinguishable)."""


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _deny_all(actor: Any, case_id: str) -> bool:
    return False


def _row_hash(row: Mapping[str, Any], prev_hash: str) -> str:
    return _sha(_canon({"prev_hash": prev_hash, **{name: row[name] for name in _COLUMNS}}))


def _change_hash(prev_hash: str, record_json: str) -> str:
    return _sha(_canon({"prev_hash": prev_hash, "record_json": record_json}))


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"{name}_required")
    return value


def _indexed(report: Mapping[str, Any]) -> dict[str, Any]:
    """The indexed columns, derived from the report itself (never trusted from the caller)."""
    try:
        summary, playbook = report["summary"], report["playbook"]
        return {
            "report_sha256": _text(report["report_sha256"], "report_sha256"),
            "case_id": _text(report["case_id"], "case_id"),
            "playbook_id": _text(playbook["id"], "playbook_id"),
            "playbook_version": _text(str(playbook["version"]), "playbook_version"),
            "definition_sha256": _text(report["definition_sha256"], "definition_sha256"),
            "evaluator_version": _text(str(report["evaluator_version"]), "evaluator_version"),
            "case_boundary_sha256": _sha(_canon(report["case_boundary"])),
            "manifest_sha256": _text(report["evidence_manifest"]["manifest_sha256"],
                                     "manifest_sha256"),
            "passed_all": 1 if summary["passed_all"] is True else 0,
            "passed_all_clean": 1 if summary["passed_all_clean"] is True else 0}
    except (KeyError, TypeError) as error:
        raise ReportError(f"report_not_a_frozen_evaluation:{error!r}") from None


class PlaybookReportStore:
    def __init__(self, path: str | Path, *, can_read: CanRead | None = None,
                 can_read_source: CanReadSource | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.path = str(path)
        self._can_read = can_read or _deny_all
        self._can_read_source = can_read_source
        self._clock = clock or _now
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            if db.execute("PRAGMA user_version").fetchone()[0] > _SCHEMA_VERSION:
                raise ReportIntegrityError("report_store_schema_newer_than_code")
            db.executescript(_DDL)
            db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        finally:
            db.close()

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            if not write:
                yield db
                return
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    def _at(self) -> str:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ReportError("clock_must_be_timezone_aware")
        return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _need_actor(actor: Any) -> None:
        if actor is None or (isinstance(actor, str) and not actor.strip()):
            raise PermissionError("actor_required")

    def _allowed(self, actor: Any, case_id: str) -> bool:
        self._need_actor(actor)
        try:
            return self._can_read(actor, case_id) is True
        except Exception:  # a failing audience check denies
            return False

    @staticmethod
    def _label(actor: Any) -> str | None:
        """The recorder label an actor reads as: a string as is, an ``Actor`` as its sorted
        principals (``operating.cases.actor_label``). Anyone with the same principals is the
        recorder for this purpose."""
        if isinstance(actor, str):
            return actor
        principals = getattr(actor, "principals", None)
        if principals is None:
            return None
        try:
            return ",".join(sorted(principals))
        except TypeError:
            return None

    def _source_ok(self, actor: Any, case_id: str, handle: str, recorded_by: str) -> bool:
        if self._can_read_source is not None:
            try:
                return self._can_read_source(actor, case_id, handle) is True
            except Exception:  # a failing source check withholds
                return False
        if not handle.startswith(_AUDIENCE_GATED):
            return True
        return self._label(actor) == recorded_by

    def _for_reader(self, actor: Any, row: sqlite3.Row,
                    report: dict[str, Any]) -> dict[str, Any]:
        """``report`` with the sources ``actor`` may not read withheld (module doc)."""
        manifest = report.get("evidence_manifest")
        entries = manifest.get("entries") if isinstance(manifest, dict) else None
        if not isinstance(entries, list):
            return report
        memo: dict[str, bool] = {}

        def ok(handle: str) -> bool:
            if handle not in memo:
                memo[handle] = self._source_ok(actor, row["case_id"], handle, row["recorded_by"])
            return memo[handle]

        shown = []
        for entry in entries:
            handle = entry.get("handle") if isinstance(entry, dict) else None
            if not isinstance(handle, str) or ok(handle):
                shown.append(entry)
            else:
                shown.append({**{k: v for k, v in entry.items() if k not in _ENTRY_SOURCE_KEYS},
                              "handle": None, "withheld": WITHHELD})
        out = {**report, "evidence_manifest": {**manifest, "entries": shown}}
        claims = report.get("claim_checks")
        if isinstance(claims, dict) and isinstance(claims.get("items"), list):
            hidden = {item.get("item") for item in claims["items"] if isinstance(item, dict)
                      for claim in item.get("claims") or []
                      if isinstance(claim.get("citation"), str) and not ok(claim["citation"])}
            if hidden:  # like plx.case_get: an item citing a withheld source shows no content
                out["claim_checks"] = {**claims, "items": [
                    {"item": item["item"], "withheld": WITHHELD, "claims": [], "leads": []}
                    if item.get("item") in hidden else item for item in claims["items"]],
                    **({"failures": [
                        {"item": f.get("item"), "code": f.get("code"), "withheld": WITHHELD}
                        if f.get("item") in hidden else f for f in claims["failures"]]}
                       if isinstance(claims.get("failures"), list) else {})}

        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                return _HANDLE.sub(lambda m: m.group(0) if ok(m.group(0)) else WITHHELD, value)
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {scrub(k): scrub(v) for k, v in value.items()}
            return value
        out = scrub(out)
        return {**out, "report_filtered": True} if out != report else report

    @staticmethod
    def _check_row(row: sqlite3.Row, previous: str) -> dict[str, Any]:
        """Verify one row (chain link, row hash, report hash, indexed columns); return the report."""
        if row["prev_hash"] != previous or row["row_hash"] != _row_hash(row, previous):
            raise ReportIntegrityError(f"report_chain_broken:{row['id']}")
        try:
            report = json.loads(row["report_json"])
        except ValueError:
            raise ReportIntegrityError(f"report_json_unreadable:{row['id']}") from None
        if (not isinstance(report, dict) or not verify_report_hash(report)
                or row["report_json"] != _canon(report)):
            raise ReportIntegrityError(f"report_hash_mismatch:{row['id']}")
        try:
            expected = _indexed(report)
        except ReportError:
            raise ReportIntegrityError(f"report_columns_unreadable:{row['id']}") from None
        if any(row[name] != value for name, value in expected.items()):
            raise ReportIntegrityError(f"report_columns_mismatch:{row['id']}")
        return report

    def _verified(self, db: sqlite3.Connection, where: str = "",
                  args: tuple = ()) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
        """Rows matching ``where``, each verified. The chain is verified up to the newest match,
        so a row is never returned after a break before it."""
        rows = db.execute("SELECT * FROM playbook_report ORDER BY id").fetchall()
        wanted = {row["id"] for row in db.execute(
            f"SELECT id FROM playbook_report {where}", args)} if where else None
        previous, out = _GENESIS, []
        for row in rows:
            report = self._check_row(row, previous)
            previous = row["row_hash"]
            if wanted is None or row["id"] in wanted:
                out.append((row, report))
        return out

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        out = {"id": row["id"], **{name: row[name] for name in _SUMMARY_COLUMNS}}
        out["passed_all"] = bool(out["passed_all"])
        out["passed_all_clean"] = bool(out["passed_all_clean"])
        return out

    # ---- writing --------------------------------------------------------

    def record(self, report: Mapping[str, Any], *, evaluated_by: Any,
               transcript_source: str) -> dict[str, Any]:
        if not isinstance(report, Mapping):
            raise ReportError("report_must_be_an_object")
        if not verify_report_hash(report):
            raise ReportError("report_hash_mismatch")
        evaluated_by = _text(evaluated_by, "evaluated_by")
        transcript_source = _text(transcript_source, "transcript_source")
        columns = _indexed(report)
        if not self._allowed(evaluated_by, columns["case_id"]):
            raise PermissionError("recorder_cannot_read_case")
        report_json = _canon(report)
        if len(report_json.encode("utf-8")) > _MAX_REPORT_BYTES:
            raise ReportError("report_too_large")
        with self._connect(write=True) as db:
            existing = db.execute("SELECT id FROM playbook_report WHERE report_sha256=?",
                                  (columns["report_sha256"],)).fetchone()
            if existing:
                return {"id": existing["id"], "duplicate": True,
                        "report_sha256": columns["report_sha256"]}
            last = db.execute("SELECT row_hash FROM playbook_report ORDER BY id DESC "
                              "LIMIT 1").fetchone()
            prev = last["row_hash"] if last else _GENESIS
            row = {**columns, "recorded_at": self._at(), "recorded_by": evaluated_by,
                   "transcript_source": transcript_source, "report_json": report_json}
            cursor = db.execute(
                f"INSERT INTO playbook_report({','.join(_COLUMNS)},prev_hash,row_hash) "
                f"VALUES({','.join('?' * (len(_COLUMNS) + 2))})",
                (*(row[name] for name in _COLUMNS), prev, _row_hash(row, prev)))
            return {"id": cursor.lastrowid, "duplicate": False,
                    "report_sha256": columns["report_sha256"]}

    # ---- reading (all through can_read) ----------------------------------

    def get(self, actor: Any, report_id: int) -> dict[str, Any]:
        """The stored record with its report filtered for this reader (module doc). Refuses a
        tampered chain or row."""
        return self._get(actor, report_id, filtered=True)

    def get_verifiable(self, actor: Any, report_id: int) -> dict[str, Any]:
        """The stored record with the report exactly as recorded (its ``report_sha256`` verifies),
        for integrity checks such as adoption's. Same case-audience check as ``get``; never a
        display path, because the manifest keeps every source handle."""
        return self._get(actor, report_id, filtered=False)

    def _get(self, actor: Any, report_id: int, *, filtered: bool) -> dict[str, Any]:
        self._need_actor(actor)
        with self._connect() as db:
            found = db.execute("SELECT case_id FROM playbook_report WHERE id=?",
                               (report_id,)).fetchone()
            if not found or not self._allowed(actor, found["case_id"]):
                raise ReportNotFound("report_not_found")
            for row, report in self._verified(db, "WHERE id=?", (report_id,)):
                return {**self._summary(row),
                        "report": self._for_reader(actor, row, report) if filtered else report}
        raise ReportNotFound("report_not_found")

    def list(self, actor: Any, case_id: str | None = None,
             playbook_id: str | None = None) -> list[dict[str, Any]]:
        """Summaries (no report body), newest first, of the readable reports only."""
        self._need_actor(actor)
        clauses, args = [], []
        for column, value in (("case_id", case_id), ("playbook_id", playbook_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as db:
            rows = [row for row, _ in self._verified(db, where, tuple(args))]
        readable: dict[str, bool] = {}
        out = []
        for row in reversed(rows):
            if row["case_id"] not in readable:
                readable[row["case_id"]] = self._allowed(actor, row["case_id"])
            if readable[row["case_id"]]:
                out.append(self._summary(row))
        return out

    def latest(self, actor: Any, case_id: str, playbook_id: str,
               version: str) -> dict[str, Any] | None:
        self._need_actor(actor)
        if not self._allowed(actor, case_id):
            return None
        with self._connect() as db:
            found = [(row, report) for row, report in self._verified(
                db, "WHERE case_id=? AND playbook_id=? AND playbook_version=?",
                (case_id, playbook_id, str(version)))]
        if not found:
            return None
        row, report = found[-1]
        return {**self._summary(row), "report": self._for_reader(actor, row, report)}

    def integrity(self, actor: Any) -> dict[str, Any]:
        """Verify the whole chain and every stored report. Reports counts and the first bad id
        only (never case ids), so any authenticated actor may ask."""
        self._need_actor(actor)
        previous, count = _GENESIS, 0
        with self._connect() as db:
            for row in db.execute("SELECT * FROM playbook_report ORDER BY id").fetchall():
                try:
                    self._check_row(row, previous)
                except ReportIntegrityError as error:
                    return {"ok": False, "reports": count, "error": str(error)}
                previous = row["row_hash"]
                count += 1
            try:
                changes, _ = self._change_records(db)
            except ReportIntegrityError as error:
                return {"ok": False, "reports": count, "error": str(error)}
        return {"ok": True, "reports": count, "changes": len(changes), "head": previous}

    # ---- playbook change records (operating.semantic_proposals) ------------

    @contextmanager
    def changes(self) -> Iterator[tuple[list[dict[str, Any]], Callable[[dict[str, Any]], None]]]:
        """The verified change records and an appender, in one write transaction: what is
        appended commits when the block ends (nothing does when it raises) and no other
        writer interleaves. A broken change chain raises ``ReportIntegrityError``."""
        with self._connect(write=True) as db:
            records, previous = self._change_records(db)

            def append(record: dict[str, Any]) -> None:
                nonlocal previous
                record_json = _canon(record)
                if len(record_json.encode("utf-8")) > _MAX_REPORT_BYTES:
                    raise ReportError("change_record_too_large")
                row_hash = _change_hash(previous, record_json)
                db.execute("INSERT INTO playbook_change(record_json,prev_hash,row_hash) "
                           "VALUES(?,?,?)", (record_json, previous, row_hash))
                previous = row_hash
                records.append(json.loads(record_json))
            yield records, append

    @staticmethod
    def _change_records(db: sqlite3.Connection) -> tuple[list[dict[str, Any]], str]:
        previous, records = _GENESIS, []
        for row in db.execute("SELECT * FROM playbook_change ORDER BY id").fetchall():
            if row["prev_hash"] != previous or row["row_hash"] != _change_hash(
                    previous, row["record_json"]):
                raise ReportIntegrityError(f"change_chain_broken:{row['id']}")
            try:
                record = json.loads(row["record_json"])
            except ValueError:
                record = None
            if not isinstance(record, dict):
                raise ReportIntegrityError(f"change_record_unreadable:{row['id']}")
            previous = row["row_hash"]
            records.append(record)
        return records, previous
