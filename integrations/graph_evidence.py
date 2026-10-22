"""Case mail evidence shapes: attachment citations, file-attachment decoding, mail people.

Mail as a product source inside cases. The reads
themselves go through ``integrations.graph_read.GraphMailReader`` (one guarded
GET path, audience before any content read); this module owns the parts that
are pure evidence shape: the attachment citation grammar, the decoding and
verification of one file attachment, and the projection of a message's
sender/recipients into related people. No I/O happens here.

The Graph delta clients and page normalizers that used to live here
(``GraphMailClient``, ``SharePointDriveClient``, ``normalize_*``) were deleted in
deletion-8: their importers (``integrations/transaction_sync.py``,
``integrations/stream_host.py``) are gone and no root reached them.
Commit dfabd0d ("Delete the unreached Graph
delta clients and MC list_conflicts") made that deletion; deletion-10 once listed
the delta code as kept, and that line was stale (corrected in
git history).
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping
from urllib.parse import quote

from ontology import identifiers as _ids
from ontology.core import ValidationError

from . import live_source_grammar as grammar
from .http import SourceUnavailable


MAIL_CITATION_SCHEME = "graph-mail"
ATTACHMENT_FRAGMENT_KEYS = frozenset({"attachment", "sha256"})
FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"
MAX_ATTACHMENT_BYTES = 20_000_000  # Graph's file attachment limit (base64 < 28 MB)
_SHA256 = _ids.system_pattern("sha256_hex")
_ADDRESS = _ids.system_pattern("mail_address")


def mail_attachment_citation(message_citation: str, attachment_id: str,
                             content_sha256: str) -> str:
    """``graph-mail://<mailbox>/<message>@<changeKey>#attachment=<id>&sha256=<hex>``.

    ``message_citation`` is the reader's unpinned message citation (no
    fragment). The attachment is pinned by the SHA-256 of its decoded bytes:
    the citation stays ``current`` while the same bytes are still attached to
    that message, whatever the message's changeKey does (read, flag, move).
    """
    if (not isinstance(message_citation, str)
            or not message_citation.startswith(MAIL_CITATION_SCHEME + "://")
            or "#" in message_citation):
        raise ValueError("attachment citation needs an unpinned graph-mail message citation")
    if not grammar.TYPES["mail_id"].valid(attachment_id):
        raise ValueError("attachment id invalid")
    if not isinstance(content_sha256, str) or _SHA256.fullmatch(content_sha256) is None:
        raise ValueError("attachment sha256 invalid")
    return (f"{message_citation}#attachment={quote(attachment_id, safe='')}"
            f"&sha256={content_sha256}")


def parse_mail_attachment_citation(handle: Any) -> dict[str, str]:
    """(mailbox, message id, cited changeKey, attachment id, sha256) of an
    attachment citation; ``ValueError`` for anything else (a message citation,
    an unknown fragment key, a bad id or hash)."""
    from .live_source_guard import parse_handle

    (mailbox, message_id), change_key, extra = parse_handle(handle, MAIL_CITATION_SCHEME, 2)
    if set(extra) != ATTACHMENT_FRAGMENT_KEYS:
        raise ValueError("citation_handle_invalid")
    attachment_id, digest = extra["attachment"], extra["sha256"]
    if (not grammar.TYPES["user_id"].valid(mailbox) or mailbox != mailbox.lower()
            or not grammar.TYPES["mail_id"].valid(message_id)
            or not grammar.TYPES["mail_id"].valid(attachment_id)
            or _SHA256.fullmatch(digest) is None):
        raise ValueError("citation_handle_invalid")
    return {"mailbox": mailbox, "message_id": message_id, "change_key": change_key,
            "attachment_id": attachment_id, "sha256": digest,
            "message_citation": handle.split("#", 1)[0]}


def is_mail_attachment_citation(handle: Any) -> bool:
    try:
        parse_mail_attachment_citation(handle)
    except ValueError:
        return False
    return True


def decode_file_attachment(raw: Any, attachment_id: str, *,
                           max_bytes: int = MAX_ATTACHMENT_BYTES) -> tuple[dict[str, Any], bytes]:
    """Metadata and decoded bytes of one Graph ``fileAttachment`` GET.

    Only a file attachment carries bytes; an item or reference attachment
    (an attached message, a OneDrive link) is refused rather than guessed.
    The decoded length must equal Graph's ``size`` when Graph reports one
    and stay within ``max_bytes``. Names are untrusted sender text.
    """
    if not isinstance(raw, Mapping) or raw.get("id") != attachment_id:
        raise SourceUnavailable("Graph attachment read returned another identity")
    if raw.get("@odata.type") != FILE_ATTACHMENT_TYPE:
        raise SourceUnavailable("Graph attachment is not a file attachment")
    encoded = raw.get("contentBytes")
    if not isinstance(encoded, str):
        raise SourceUnavailable("Graph file attachment lacks contentBytes")
    if len(encoded) > (max_bytes * 4) // 3 + 4:
        raise SourceUnavailable("Graph file attachment exceeds the byte bound")
    import base64
    import binascii

    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise SourceUnavailable("Graph file attachment contentBytes is not base64") from None
    if len(content) > max_bytes:
        raise SourceUnavailable("Graph file attachment exceeds the byte bound")
    size = raw.get("size")
    name = raw.get("name")
    metadata = {"id": attachment_id, "name": name if isinstance(name, str) else None,
                "content_type": raw.get("contentType") if isinstance(raw.get("contentType"), str)
                else None,
                "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
                "is_inline": raw.get("isInline") is True,
                "kind": FILE_ATTACHMENT_TYPE,
                "last_modified": raw.get("lastModifiedDateTime"),
                "bytes": len(content),
                "content_sha256": hashlib.sha256(content).hexdigest()}
    # Graph's ``size`` is the attachment's stored size (MIME part, a few hundred
    # bytes above the file); it bounds, it never equals, the decoded bytes.
    if metadata["size"] is not None and len(content) > metadata["size"]:
        raise SourceUnavailable("Graph file attachment bytes exceed its reported size")
    return metadata, content


def _mail_domain(address: str) -> str:
    return address.rsplit("@", 1)[1]


def mail_people(messages: list[Mapping[str, Any]], *,
                internal_domains: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Senders and recipients of visible messages as related people (WS-08).

    ``messages`` are ``{"citation", "record"}`` pairs the reader may read now
    (``record`` = a ``GraphMailReader`` record: ``from``, ``to``, ``cc``).
    Identity is the e-mail address only: no display name is read, no two
    addresses are merged into one person and no address is resolved to a
    Portal contact or an employee; ``person_resolved`` is always false. A
    mailbox's own addresses are the mailbox, not a person, and are marked
    ``is_mailbox``. Every person lists the message citations it appears in, so
    a reader who later loses a message loses that evidence with it.
    """
    people: dict[str, dict[str, Any]] = {}
    for message in messages:
        record = message.get("record") if isinstance(message, Mapping) else None
        citation = message.get("citation") if isinstance(message, Mapping) else None
        if not isinstance(record, Mapping) or not isinstance(citation, str):
            raise ValidationError("mail people need cited message records")
        mailbox_ids = {str(a).lower() for a in ((record.get("source_acl") or {})
                                                .get("identities") or [])
                       if isinstance(a, str)} | {str(record.get("mailbox_upn") or "").lower()}
        roles = [("from", [record.get("from")]), ("to", record.get("to") or []),
                 ("cc", record.get("cc") or [])]
        for role, addresses in roles:
            for address in addresses:
                if not isinstance(address, str):
                    continue
                address = address.strip().lower()
                if _ADDRESS.fullmatch(address) is None:
                    continue
                domain = _mail_domain(address)
                person = people.setdefault(address, {
                    "address": address, "domain": domain,
                    "internal": any(domain == d or domain.endswith("." + d)
                                    for d in internal_domains),
                    "is_mailbox": False, "roles": {"from": 0, "to": 0, "cc": 0},
                    "citations": [], "identity_basis": "email_address",
                    "person_resolved": False})
                person["is_mailbox"] = person["is_mailbox"] or address in mailbox_ids
                person["roles"][role] += 1
                if citation not in person["citations"]:
                    person["citations"].append(citation)
    return sorted(people.values(), key=lambda p: (p["is_mailbox"], p["internal"],
                                                  p["address"]))
