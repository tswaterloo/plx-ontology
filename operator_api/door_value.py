"""``X-PLX-Door``: one short value that changes exactly when what the signed-in person may
read can have changed (SPEED-SPECS 10.9, store rules 1-2).

The desktop app keeps the answers the server sent a person on that person's machine
(``desktop/src/store.js``). It must forget them when the door moves, so the server says,
on every answer to a signed-in reader, a digest of everything that decides that reader's
door:

* the reader's own door key (``doors.reader_key``: tenant, principals, clearance, the
  declared full-read grant, service, mode: the one definition of who a reader is);
* the reader's own access mark (``access_mark`` of the decided door: the classes,
  column allow-lists, document-hidden handles, and revocation epoch, never the
  schema-wide rebuild stamp or row counts that move with ordinary data refreshes);
* the people exclusion's rule stamp (``permissions.exclusion_rule_stamp``: the list and its
  file, never the set's ``covered_ver`` that moves with each delta cycle).

It is the SESSION's own reader, also on a view-as read (the owner's store never takes a
view-as answer). When any part is unknown the header is OMITTED, never guessed: the app then
stores nothing from that answer and wipes nothing.

A refused session (``AuthRequired``) carries ``X-PLX-Session: refused``: the one fact that
makes the app forget everything. A route that answers 403 as its ordinary "not for you"
(the ops alarms of a non-Team reader, the grant sheet) does not carry it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Optional

from .doors import reader_key

log = logging.getLogger("operator_api.door_value")
DOOR_HEADER = "X-PLX-Door"
SESSION_HEADER = "X-PLX-Session"
SESSION_REFUSED = "refused"
_RULE_KEPT_SECONDS = 2.0   # the rule stamp reads a file's mtime; one read per 2 s per process
_rule: tuple[float, Optional[str]] = (0.0, None)
_lock = threading.Lock()
_failed_logged = False


def _rule_stamp() -> Optional[str]:
    """The people exclusion's rule stamp, kept ``_RULE_KEPT_SECONDS``; None when unreadable
    (said once in the log)."""
    global _rule, _failed_logged
    now = time.monotonic()
    with _lock:
        if now - _rule[0] < _RULE_KEPT_SECONDS and _rule[0] > 0:
            return _rule[1]
    try:
        from operating.pg.permissions import exclusion_rule_stamp
        got: Optional[str] = exclusion_rule_stamp()
    except Exception as exc:  # noqa: BLE001 - no header (the app stores nothing); logged
        if not _failed_logged:
            log.warning("X-PLX-Door: the exclusion rule stamp is unreadable: %s", type(exc).__name__)
            _failed_logged = True
        got = None
    with _lock:
        _rule = (now, got)
    return got


def door_value(reader: Any, access_mark: Optional[str]) -> Optional[str]:
    """The door value of ``reader`` at ``access_mark``, or None when any part is unknown."""
    key = reader_key(reader)
    rule = _rule_stamp()
    if key is None or not isinstance(access_mark, str) or not access_mark or rule is None:
        return None
    body = json.dumps([list(key), access_mark, rule], default=str, sort_keys=False)
    return hashlib.sha256(body.encode()).hexdigest()[:32]
