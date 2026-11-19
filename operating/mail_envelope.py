"""The sending platform of captured mail, read from what the mail capture already stores.

Bulk mail often wears a person's address ("jane.doe@supplier.com" writing "Hi Greg, ...")
and a personal text, so its sender and its words do not mark it. Its envelope does: the
receiving server records, in the ``Authentication-Results`` header the capture keeps per
message (``record.authentication_results``, ``integrations.graph_read``), the envelope
sender's host (``smtp.mailfrom=``) and the DKIM signer (``header.d=``). A mailing platform
signs and bounces on its own hosts or on a sending sub-domain the sender delegates to it
(``bounce.``, ``em1234.``, ``send.``, ``*.mcsv.net``, ``*.hubspotemail.net``, ...:
``PLATFORM``); a person's own mail is sent from their company's domain.

Read-only: ``reader`` opens the capture read-only per call and answers, for Email object
ids, only whether the stored envelope names a mailing platform. A message the capture does
not hold, or whose header was not recorded, is absent from the answer: no envelope signal,
so the rules keep it (they never drop a message on a missing fact). No capture writer
changes, no new table. Callers keep the exemptions (``inbox_triage.by_platform``): a
platform's relay of a person's message ("reply above this line", ``RELAY``) and a
conversation one of ours already wrote in earlier (a real contact who sends through a
platform).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

# A mailing platform's host label: a bounce/sending sub-domain a sender delegates to it, or
# the platform's own domain. Matched per dotted label run (``host + "."``).
PLATFORM = re.compile(
    r"(?:^|\.)(?:bounces?\d*|bouncing|[\w-]*-bounces?|em-?\d*|send|email|mailer|mail\d+|news|"
    r"gsemail|mktomail|exacttarget|mcsv|mcdlv|rsgsv|sendgrid|mailgun|mandrillapp|"
    r"sparkpostmail|hubspotemail|klaviyomail|ccsend|constantcontact|createsend|cmail\d+|"
    r"mailchimpapp|sendinblue|brevo|mailjet|mtasv|postmarkapp|salesforce|pd25|smtp2go|"
    r"elasticemail|mailerlite|activecampaign|acems\d*|emsend\d*|sendibt\d*|zsend|zcsend)\.",
    re.I)
# A platform relaying one person's message to another (a booking or ticket conversation):
# the person wrote it, the platform only carries it.
RELAY = re.compile(r"\b(?:reply|respond)\s+above\s+this\s+line\b|\balways\s+communicate\s+"
                   r"through\b|\breply\s+(?:directly\s+)?to\s+this\s+(?:e-?mail|message)\s+to\s+"
                   r"(?:respond|reply)\b", re.I)
_MAILFROM = re.compile(r"\bsmtp\.mailfrom=([\w.-]+)", re.I)
_SIGNER = re.compile(r"\bheader\.d=([\w.-]+)", re.I)
BINDING = "graph_mail.messages"


def hosts(authentication_results: Any) -> list[str]:
    """The envelope sender hosts and DKIM signer domains of the stored header values (a
    list of strings, as the capture keeps them); anything else names none."""
    if not isinstance(authentication_results, (list, tuple)):
        return []
    out: list[str] = []
    for value in authentication_results:
        if isinstance(value, str):
            out += [h.lower().rstrip(".") for h in _MAILFROM.findall(value)]
            out += [h.lower().rstrip(".") for h in _SIGNER.findall(value)]
    return out


def platform(authentication_results: Any) -> bool:
    """Whether the stored envelope names a mailing platform (``PLATFORM``)."""
    return any(PLATFORM.search(h + ".") for h in hosts(authentication_results))


def message_key(oid: str) -> tuple[str, str] | None:
    """The capture's ``(mailbox_id, message_id)`` of an Email object id
    (``Email:graph_mail.messages:<{"id", "mailbox"} JSON>``); None for any other id."""
    from urllib.parse import unquote
    parts = str(oid).split(":", 2)
    if len(parts) != 3 or parts[0] != "Email" or parts[1] != BINDING:
        return None
    try:
        native = json.loads(unquote(parts[2]))
    except ValueError:
        return None
    if not isinstance(native, dict):
        return None
    mailbox, message = native.get("mailbox"), native.get("id")
    if not isinstance(mailbox, str) or not isinstance(message, str) or not mailbox or not message:
        return None
    return mailbox, message


def reader(capture_db: str | Path) -> Callable[[Iterable[str]], dict[str, bool]]:
    """oids -> {oid: sent by a mailing platform} for the Email objects whose current
    captured record the mail capture at ``capture_db`` holds (read-only, its own
    connection per call: rules run on worker threads). An unreadable capture answers
    nothing (every message kept)."""
    path = Path(capture_db)

    def read(oids: Iterable[str]) -> dict[str, bool]:
        keys = {oid: k for oid in oids for k in [message_key(oid)] if k is not None}
        if not keys or not path.is_file():
            return {}
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        except sqlite3.Error:
            return {}
        out: dict[str, bool] = {}
        try:
            for oid, (mailbox, message) in keys.items():
                row = conn.execute(
                    "SELECT json_extract(record, '$.authentication_results') FROM messages "
                    "WHERE mailbox_id = ? AND message_id = ? AND superseded_at IS NULL "
                    "ORDER BY version_id DESC LIMIT 1", (mailbox, message)).fetchone()
                if row is None or row[0] is None:
                    continue   # not held, or no header recorded: no signal
                try:
                    value = json.loads(row[0])
                except ValueError:
                    continue
                if hosts(value):
                    out[oid] = platform(value)
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        return out

    return read
