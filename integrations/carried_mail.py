"""Who a carried email is from and to, and whether the people exclusion clears it (DOC-008).

An allowed message can carry another person's mail: an attached ``.eml``/``.msg``, one
inside a zip, a forward as ``message/rfc822``.

:func:`carried_people` walks a container's members structurally
(``operating.use_case_parts.members``: no text is read, no OCR) and returns, per carried
mail (the container itself when it is one, path ``.``), the addresses its header names:
From, Sender, Reply-To, To, Cc, Bcc and their ``Resent-`` forms for an RFC 822 message;
the sender, sent-representing and every recipient's SMTP address (else its e-mail
address) and the transport header block's addresses for an Outlook ``.msg``. A mail whose
header block does not parse, that names no sender, or names an identity that is not an
address (an Exchange DN without its SMTP form) is ``complete=False``: it cannot be cleared.

:func:`verdict` decides one container with a people exclusion guard the way the notices
reader does (``ontology.project._UndecidedWithheld``): a bound guard (the live capture's)
decides every address; an offline one only an address in the tenant's mail domains, a
tenant person's; an outside one is no person of ours;
without known tenant domains nothing is cleared (fail closed). ``excluded`` when a
carried mail names an excluded person; ``undecided`` when one cannot be cleared (the
guard is unavailable or cannot decide a tenant address, a header is unreadable, the
container itself cannot be walked); else ``clear``. A verdict is never kept: what is kept
is the walk (:func:`encode`: each carried mail's member path, senders and recipients, the
mail capture's ``carried_people``, keyed by the container's bytes digest), decided again
with the guard of the moment (:func:`decided`), so a change to the exclusion reaches
every stored container without reading its bytes again. A verdict holds no address.

This module is not a reading module (``captured_text.READING_MODULES``) nor a member
module (``kept_members.MEMBER_MODULES``): it changes no kept reading.

Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from typing import Any, Iterable, Mapping

CLEAR, EXCLUDED, UNDECIDED = "clear", "excluded", "undecided"

MAIL_HEADERS = ("from", "sender", "reply-to", "to", "cc", "bcc", "resent-from",
                "resent-sender", "resent-reply-to", "resent-to", "resent-cc", "resent-bcc")
SENDER_HEADERS = ("from", "sender", "resent-from", "resent-sender")

# Outlook .msg properties (MS-OXPROPS), Unicode (001F) or 8-bit (001E) strings
_MSG_SENDERS = ("5D01", "0C1F", "5D02", "0065")   # sender SMTP/address, sent-representing
_MSG_RECIPIENT = ("39FE", "3003")                 # SMTP address, else e-mail address
_MSG_HEADERS = "007D"                              # transport message headers
_STRING_TYPES = ("001F", "001E")
_STREAM_LIMIT = 1 << 20


@dataclass(frozen=True)
class CarriedMail:
    """One carried mail: its member path (``.``: the container itself), the addresses
    its header names (lower case) and whether every identity it names was read as an
    address (``complete``)."""

    path: str
    addresses: frozenset[str]
    complete: bool


def _header_addresses(block: bytes) -> tuple[set[str], bool]:
    """(addresses, complete) of an RFC 822 header block."""
    try:
        headers = BytesHeaderParser(policy=policy.compat32).parsebytes(block)
    except Exception:  # noqa: BLE001 - an unreadable header block clears nobody
        return set(), False
    found, complete, senders = set(), True, False
    for name in MAIL_HEADERS:
        values = headers.get_all(name) or []
        for value in values:
            try:
                pairs = getaddresses([str(value)])
            except Exception:  # noqa: BLE001 - a malformed value is undecidable
                complete = False
                continue
            for _, address in pairs:
                address = address.strip().lower()
                if not address:
                    continue
                if "@" not in address:
                    complete = False
                    continue
                found.add(address)
                senders = senders or name in SENDER_HEADERS
    return found, complete and senders


def _eml(data: bytes) -> tuple[set[str], bool]:
    head = data.split(b"\r\n\r\n", 1)[0] if b"\r\n\r\n" in data[:1 << 20] \
        else data.split(b"\n\n", 1)[0]
    return _header_addresses(head)


def _msg_string(cfb: Any, storage: int, prop: str) -> str | None:
    """A string property of ``storage`` (Unicode or 8-bit), else None; raises on a
    stream that is there and does not read."""
    for kind in _STRING_TYPES:
        index = cfb.child(storage, f"__substg1.0_{prop}{kind}")
        if index is None:
            continue
        raw = cfb.stream(index, _STREAM_LIMIT)
        text = raw.decode("utf-16-le" if kind == "001F" else "cp1252", errors="strict")
        return text.rstrip("\x00").strip() or None
    return None


def _msg(data: bytes) -> tuple[set[str], bool]:
    from operating import msg_reader
    try:
        cfb = msg_reader.Cfb(data)
        found: set[str] = set()
        complete = True
        senders = [s for s in (_msg_string(cfb, 0, p) for p in _MSG_SENDERS) if s]
        smtp = [s.lower() for s in senders if "@" in s]
        found.update(smtp)
        if not smtp:
            complete = False             # no sender address (none, or an Exchange DN)
        headers = _msg_string(cfb, 0, _MSG_HEADERS)
        if headers:
            more, whole = _header_addresses(headers.encode("utf-8", "surrogateescape"))
            found.update(more)
            complete = complete and whole
        for child in cfb.children(0):
            if not cfb.entries[child][0].startswith(msg_reader.RECIPIENT_PREFIX):
                continue
            address = next((v for v in (_msg_string(cfb, child, p) for p in _MSG_RECIPIENT)
                            if v), None)
            if address is None or "@" not in address:
                complete = False
            else:
                found.add(address.strip().lower())
        return found, complete
    except (ValueError, OverflowError, UnicodeDecodeError, IndexError, KeyError):
        return set(), False


def _mail_kind(data: bytes, name: str, hint: str | None) -> str | None:
    from operating import use_case_parts as parts
    try:
        kind = parts._sniff(data, name, hint)
    except Exception:  # noqa: BLE001 - an unsniffable part is no mail we can name
        return None
    if kind == "mail":
        return "eml"
    if kind == "cfb" and parts._is_msg(data):
        return "msg"
    return None


def _people(data: bytes, kind: str) -> tuple[set[str], bool]:
    return _eml(data) if kind == "eml" else _msg(data)


def carried_people(content: bytes, name: str, mime_type: str | None = None
                   ) -> tuple[list[CarriedMail], bool] | None:
    """(each carried mail of ``content``, whether the walk read every part it could
    hold one in), or None when ``content`` is not a container (nothing carried)."""
    from operating import use_case_parts as parts
    if not parts.is_container(content, name, mime_type):
        return None
    mails: list[CarriedMail] = []
    top = _mail_kind(content, name, mime_type)
    if top is not None:
        found, complete = _people(content, top)
        mails.append(CarriedMail(".", frozenset(found), complete))
    try:
        listed = parts.members(content, name, mime_type)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - a container that cannot be walked clears nobody
        return mails, False
    if listed is None:
        return mails, False
    for item in listed["files"]:
        kind = _mail_kind(item["data"], item["name"], item["hint"])
        if kind is None:
            continue
        found, complete = _people(item["data"], kind)
        mails.append(CarriedMail(item["path"], frozenset(found), complete))
    # a part that was not read (unreadable, over a cap) may be a mail nobody saw
    whole = not any(_may_hide_mail(g) for g in listed["gaps"])
    return mails, whole


# Gaps of a walk where a carried mail may be that a reader of the container's text could
# still read: a part skipped over a cap (a container's, an original's), a carried
# message's headers or storage not read, a compound file not parsed, and a zip member
# skipped whose name may be a mail or a container (no suffix, or one). Bytes no reader
# opens (a zip that does not open, an encrypted member) hold nothing anybody reads.
_HIDING_KINDS = ("container", "cfb", "mail_headers", "msg_storage", "original")
_HIDING_SUFFIXES = ("", ".eml", ".msg", ".zip")


def _may_hide_mail(gap: Mapping[str, Any]) -> bool:
    from pathlib import PurePosixPath
    kind = gap.get("kind")
    if kind in _HIDING_KINDS:
        return True
    if kind == "zip_member":
        name = str(gap.get("path") or "").rsplit("!/", 1)[-1]
        return PurePosixPath(name).suffix.lower() in _HIDING_SUFFIXES
    return False


def _tenant_domains(guard: Any) -> frozenset[str]:
    try:
        return frozenset(str(d).lower() for d in guard.known_domains())
    except Exception:  # noqa: BLE001 - no domains: nothing can be decided
        return frozenset()


def decide(guard: Any, addresses: Iterable[str]) -> str:
    """``excluded``, ``undecided`` or ``clear`` for the addresses one mail names."""
    if guard is None or getattr(guard, "unavailable", True):
        return UNDECIDED
    if getattr(guard, "bound", False) is True:
        tenant = list(addresses)        # a bound guard decides every address itself
    else:
        domains = _tenant_domains(guard)
        if not domains:
            return UNDECIDED
        tenant = [a for a in addresses if a.rsplit("@", 1)[-1] in domains]
    undecided = False
    for address in tenant:
        try:
            if guard.is_excluded(address):
                return EXCLUDED
        except Exception:  # noqa: BLE001 - an address it cannot decide clears nothing
            undecided = True
    return UNDECIDED if undecided else CLEAR


@dataclass(frozen=True)
class Verdict:
    """A container's verdict and the member paths it withholds (a carried mail not
    cleared and every member under it; ``.``: the whole container)."""

    state: str
    withheld: frozenset[str]
    carried: int

    def withholds(self, path: str) -> bool:
        return "." in self.withheld or any(
            path == p or path.startswith(p + "!/") for p in self.withheld)


def walk(content: bytes, name: str, mime_type: str | None) -> Any:
    """:func:`carried_people`, with a container that cannot be walked at all as
    ``([], False)`` (nothing seen, not whole: it clears nobody)."""
    try:
        return carried_people(content, name, mime_type)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - an unwalkable container clears nobody
        return [], False


def verdict(content: bytes, name: str, mime_type: str | None, guard: Any) -> Verdict:
    """The people exclusion's verdict on what ``content`` carries (module docstring)."""
    return decided(walk(content, name, mime_type), guard)


def decided(found: Any, guard: Any) -> Verdict:
    """The verdict on one walk (:func:`carried_people`'s value: None, nothing carried),
    decided with ``guard`` now: a kept walk is decided again whenever the exclusion
    changes, never kept as a verdict."""
    if found is None:
        return Verdict(CLEAR, frozenset(), 0)
    mails, whole = found
    states: dict[str, str] = {}
    for mail in mails:
        state = decide(guard, mail.addresses)
        if state == CLEAR and not mail.complete:
            state = UNDECIDED
        states[mail.path] = state
    withheld = frozenset(p for p, s in states.items() if s != CLEAR)
    if EXCLUDED in states.values():
        state = EXCLUDED
    elif withheld or not whole:
        state = UNDECIDED
    else:
        state = CLEAR
    if state == UNDECIDED and not withheld:
        withheld = frozenset({"."})          # a part nobody saw: the whole container
    return Verdict(state, withheld, len(mails))


# ------------------------------------------------------------------ kept identities

@functools.lru_cache(maxsize=1)
def code() -> str:
    """Digest of what a walk depends on (this module, the container walk, the .msg
    reader): a walk kept under another code is walked again."""
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256(b"plx-carried-mail/v1\n")
    for module in ("integrations/carried_mail.py", "operating/use_case_parts.py",
                   "operating/msg_reader.py"):
        digest.update(module.encode() + b"\n" + hashlib.sha256(
            (root / module).read_bytes()).digest())
    return digest.hexdigest()


def encode(found: Any) -> str:
    """A walk as kept (JSON): ``null`` for nothing carried, else ``{"whole": bool,
    "mails": [[path, [address, ...], complete], ...]}``: the carried mails' senders and
    recipients, each with its member path."""
    import json
    if found is None:
        return "null"
    mails, whole = found
    return json.dumps({"whole": bool(whole), "mails": [
        [m.path, sorted(m.addresses), bool(m.complete)] for m in mails]}, sort_keys=True)


def decode(value: str) -> Any:
    """A kept walk (:func:`encode`); raises ``ValueError`` on anything else (a caller
    walks the bytes again)."""
    import json
    data = json.loads(value)
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("whole"), bool) \
            or not isinstance(data.get("mails"), list):
        raise ValueError("carried_walk_invalid")
    mails = []
    for item in data["mails"]:
        path, addresses, complete = item
        if not isinstance(path, str) or not isinstance(complete, bool) or not all(
                isinstance(a, str) for a in addresses):
            raise ValueError("carried_walk_invalid")
        mails.append(CarriedMail(path, frozenset(addresses), complete))
    return mails, data["whole"]


def may_carry(head: bytes, name: str, mime_type: str | None) -> bool:
    """Whether bytes starting with ``head`` (their first 8 bytes at least) may be a
    container (:func:`operating.use_case_parts.is_container` needs them all): a zip, a
    compound file, or a mail by its name or type. False only when it cannot be one."""
    from operating import use_case_parts as parts
    try:
        kind = parts._sniff(head, name, mime_type)
    except Exception:  # noqa: BLE001 - unsniffable: walk it
        return True
    return kind in ("mail", "zip", "cfb")


@functools.lru_cache(maxsize=256)
def _walk_blob(blobs: str, digest: str, name: str, mime_type: str | None) -> Any:
    from integrations.captured_text import read_blob
    content = read_blob(blobs, digest)
    if content is None:
        return [], False          # bytes gone: nothing can be cleared
    return walk(content, name, mime_type)


def _blob_head(blobs: Any, digest: str, size: int = 4096) -> bytes | None:
    """The first bytes of a stored blob (its bytes form, else its compressed form), or
    None when neither is held."""
    import zlib
    from integrations.captured_text import COMPRESSED_SUFFIX, blob_path
    try:
        with open(blob_path(blobs, digest), "rb") as handle:
            return handle.read(size)
    except OSError:
        pass
    try:
        with open(blob_path(blobs, digest, COMPRESSED_SUFFIX), "rb") as handle:
            return zlib.decompressobj().decompress(handle.read(size), size)
    except (OSError, zlib.error):
        return None


@functools.lru_cache(maxsize=8192)
def _held_may_carry(blobs: str, digest: str, name: str, mime: str | None) -> bool:
    head = _blob_head(blobs, digest)
    if head is None:
        raise FileNotFoundError(digest)
    return may_carry(head, name, mime)


def _stored_may_carry(blobs: str, digest: str, name: str, mime: str | None
                      ) -> bool | None:
    """Whether stored bytes may be a container (:func:`may_carry` on their head),
    memoised per bytes (content-addressed); None when they are not held (never
    memoised: bytes gone now are decided again)."""
    try:
        return _held_may_carry(blobs, digest, name, mime)
    except FileNotFoundError:
        return None


WHOLE = Verdict(UNDECIDED, frozenset({"."}), 0)   # withheld whole, nothing decided
NOTHING = Verdict(CLEAR, frozenset(), 0)          # carries no mail


def stored_verdict(blobs: Any, row: Mapping[str, Any], guard: Any, state_dir: Any = None,
                   kept: Any = None) -> Verdict:
    """The verdict now on the mail a stored file carries (``row``: its metadata with
    ``content_sha256``, as ``captured_text.member_view`` takes it): the resolve-time gate
    of the file and its members, for files stored before the capture-time drop and for
    every exclusion change since. ``kept``: the file's kept walk (``(walk,)``), else its
    bytes are walked (memoised per bytes) when their head may be a container. A file
    whose bytes were never stored is served as metadata only (nothing carried is
    served). Bytes no longer held (an original dropped, its readings kept) of what its
    name or type says is a mail, an unwalkable file, or any failure
    withholds it whole (fail closed); ``guard`` unavailable (the list unreadable)
    decides no carried mail. ``guard`` unbound: decided with what the mail capture under
    ``state_dir`` recorded (``people_exclusion.with_directory``)."""
    from integrations import people_exclusion
    from integrations.captured_text import file_type
    try:
        mime, name = file_type(row)
        digest = row.get("content_sha256")
        if kept is not None:
            found = kept[0]
        elif not isinstance(digest, str):
            return NOTHING                    # no bytes stored: no text or bytes served
        else:
            held = _stored_may_carry(str(blobs), digest, name, mime)
            if held is None:
                return WHOLE if may_carry(b"", name, mime) else NOTHING
            if not held:
                return NOTHING
            found = _walk_blob(str(blobs), digest, name, mime)
        if found is None or (not found[0] and found[1] is True):
            return NOTHING                    # walked whole, no mail carried
        current = getattr(guard, "current", None)
        if callable(current):
            guard = current()                 # a reloading guard: the list as it is now
        if state_dir is not None:
            guard = people_exclusion.with_directory(guard, state_dir)
        return decided(found, guard)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - not decided: withheld whole
        return WHOLE


def member_withheld(blobs: Any, row: Mapping[str, Any], path: str, guard: Any,
                    state_dir: Any = None, kept: Any = None) -> bool:
    """Whether the member ``path`` of the captured container ``row`` is withheld from
    every reader (:func:`stored_verdict`): a carried mail not cleared, or anything under
    one (the resolve-time gate of a member citation)."""
    if not isinstance(row.get("content_sha256"), str):
        return True
    return stored_verdict(blobs, row, guard, state_dir, kept).withholds(path)


def counts(verdicts: Iterable[Verdict]) -> Mapping[str, int]:
    """How many containers each verdict reached (the only trace a caller logs)."""
    out = {CLEAR: 0, EXCLUDED: 0, UNDECIDED: 0}
    for one in verdicts:
        out[one.state] += 1
    return out


__all__ = ["CLEAR", "EXCLUDED", "UNDECIDED", "CarriedMail", "Verdict", "carried_people",
           "code", "counts", "decide", "decided", "decode", "encode", "may_carry",
           "member_withheld", "stored_verdict", "verdict", "walk"]
