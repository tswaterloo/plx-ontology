"""The Workbench's one confirmation form: a single-use, expiring MCP elicitation request.

Both human confirmations the Workbench asks for use it: an original's document fields
(``operating.kind_confirmation``, purpose ``document_fields``) and a playbook adoption
(``operating.playbook_tools``, purpose ``playbook_adoption``). A caller ``open``s a form for
the one principal it asks and sends the returned id to the session's client as the id of an
``elicitation/create`` request; the client's JSON-RPC response to that id (``is_response``)
is the answer, which the caller ``claim``s and ``finish``es.

* **One id.** ``cf-`` and 128 random bits, never part of a tool result. The store keeps only
  its SHA-256, so a reader of the file cannot answer a form.
* **One requester, one answerer.** A form names its purpose, tenant, subject (the original or
  proposal it is about) and the principal asked (``requested_by``). A newer form for the same
  purpose, tenant, subject and principal supersedes that principal's open ones: only the
  latest form the person saw can be answered. Only the principal asked answers; the caller
  says who answers (``answerer``: its own identity rule, e.g. one employee principal and no
  agent principal).
* **One TTL** (``DEFAULT_TTL_SECONDS`` unless the host sets another).
* **Single use.** ``claim`` spends a pending form once (``answering``), atomically, before the
  caller acts on the answer; a refused claim (answerer, expiry) spends it too. The caller's
  ``finish`` records the final state and outcome. A form that is not pending is refused with
  its state (``confirmation_request_<state>``); an unknown id, another tenant's or another
  purpose's form is ``unknown_confirmation_request`` and stays as it was.
* **Persisted.** Forms live in a table of a SQLite file in the Workbench state directory
  (the host's ``kind_confirmation.store_path``): a pending form survives a restart of the host
  and is answered by the principal asked from any of their sessions, once.

Nothing here proves that a person, not a scripted client, answered (``kind_confirmation`` and
``playbook_tools`` state that residual). Evidence label: isolated-fixture / injected-transport,
live-unverified.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Iterable, Mapping

PREFIX = "cf-"
DEFAULT_TTL_SECONDS = 600
UNKNOWN = "unknown_confirmation_request"
_TOKEN = re.compile(r"cf-[0-9a-f]{32}")
_FINAL = ("confirmed", "declined", "cancelled", "refused")


class FormRefused(PermissionError):
    """A form that cannot be answered; ``code`` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_response(message: Any) -> bool:
    """A JSON-RPC response (no method) to a confirmation form."""
    return (isinstance(message, Mapping) and "method" not in message
            and isinstance(message.get("id"), str) and message["id"].startswith(PREFIX)
            and ("result" in message or "error" in message))


class ConfirmationForms:
    """Confirmation forms in the ``confirmation_form`` table of the SQLite file ``path``."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.path = str(path)
        self._clock = clock or (lambda: _now())
        self._ttl = timedelta(seconds=ttl_seconds)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS confirmation_form (
                token_sha256 TEXT PRIMARY KEY, purpose TEXT NOT NULL, tenant TEXT NOT NULL,
                subject TEXT NOT NULL, requested_by TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
                outcome TEXT)""")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def open(self, purpose: str, tenant: str, subject: str, requested_by: str,
             payload: Mapping[str, Any]) -> tuple[str, str]:
        """Open one form asking ``requested_by`` about ``subject``; returns ``(id,
        expires_at)``. The id must only travel to the client on the MCP transport."""
        token = PREFIX + secrets.token_hex(16)
        now = self._clock()
        expires = (now + self._ttl).isoformat()
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""UPDATE confirmation_form SET state='superseded' WHERE purpose=?
                    AND tenant=? AND subject=? AND requested_by=? AND state='pending'""",
                           (purpose, tenant, subject, requested_by))
                db.execute("INSERT INTO confirmation_form VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    _sha(token), purpose, tenant, subject, requested_by,
                    json.dumps(payload, sort_keys=True), now.isoformat(), expires, "pending",
                    None))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return token, expires

    def claim(self, token: Any, *, purpose: str, tenant: Any,
              answerer: Callable[[], str]) -> dict[str, Any]:
        """Spend the pending form ``token`` for its answer (single use) and return its
        ``payload``, ``requested_by`` and ``created_at``.

        ``answerer()`` is the answering session's principal under the caller's identity rule;
        when it raises, or names another principal than the one asked, or the form expired,
        the form is spent as refused and the refusal raised."""
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise FormRefused(UNKNOWN)
        key = _sha(token)
        with closing(self._db()) as db:
            row = db.execute("SELECT * FROM confirmation_form WHERE token_sha256=?",
                             (key,)).fetchone()
        if row is None or row["purpose"] != purpose or row["tenant"] != tenant:
            raise FormRefused(UNKNOWN)
        if row["state"] != "pending":
            raise FormRefused(f"confirmation_request_{row['state']}")
        try:
            if answerer() != row["requested_by"]:
                raise FormRefused("answerer_is_not_the_requester")
            if self._clock() > datetime.fromisoformat(row["expires_at"]):
                raise FormRefused("confirmation_request_expired")
        except Exception as refused:
            self._close(key, "pending", "refused", getattr(refused, "code", None))
            raise
        if not self._close(key, "pending", "answering", None):
            with closing(self._db()) as db:   # a concurrent answer or a newer form won
                state = db.execute("SELECT state FROM confirmation_form WHERE "
                                   "token_sha256=?", (key,)).fetchone()["state"]
            raise FormRefused(f"confirmation_request_{state}")
        return {"payload": json.loads(row["payload_json"]),
                "requested_by": row["requested_by"], "created_at": row["created_at"]}

    def finish(self, token: str, state: str, outcome: str | None) -> None:
        """Record the final ``state`` and ``outcome`` of a form ``claim`` spent."""
        if state not in _FINAL:
            raise ValueError("form_state_invalid")
        self._close(_sha(token), "answering", state, outcome)

    def _close(self, key: str, current: str, state: str, outcome: str | None) -> bool:
        with closing(self._db()) as db:
            return db.execute("UPDATE confirmation_form SET state=?, outcome=? WHERE "
                              "token_sha256=? AND state=?",
                              (state, outcome, key, current)).rowcount == 1

    def pending(self, purpose: str, tenant: str, subject: str) -> int:
        """How many forms about ``subject`` can still be answered."""
        with closing(self._db()) as db:
            return db.execute("SELECT COUNT(*) FROM confirmation_form WHERE purpose=? AND "
                              "tenant=? AND subject=? AND state='pending' AND expires_at>?",
                              (purpose, tenant, subject,
                               self._clock().isoformat())).fetchone()[0]

    def last_outcome(self, purpose: str, tenant: str, subject: str,
                     requested_by: Iterable[str]) -> str | None:
        """The outcome of the latest answered form about ``subject`` that asked one of
        ``requested_by``, or None (forms are answered in the order they were opened: a newer
        one supersedes the open ones)."""
        asked = sorted({str(p) for p in requested_by})
        if not asked:
            return None
        with closing(self._db()) as db:
            row = db.execute(
                "SELECT outcome FROM confirmation_form WHERE purpose=? AND tenant=? AND "
                f"subject=? AND requested_by IN ({','.join('?' * len(asked))}) AND "
                "outcome IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                (purpose, tenant, subject, *asked)).fetchone()
        return None if row is None else row["outcome"]


__all__ = ["ConfirmationForms", "DEFAULT_TTL_SECONDS", "FormRefused", "PREFIX", "UNKNOWN",
           "is_response"]
