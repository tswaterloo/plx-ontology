"""Deterministic, stdlib-only decoding of Outlook ``.msg`` files (MS-CFB, MS-OXMSG).

``Cfb`` reads an OLE compound file from bytes (bounded and loop-safe: every
sector chain is checked against the file and against cycles). It and these
decoders are the repo's one ``.msg`` reader: ``use_case_parts`` accounts every
stream with them, ``integrations.office_text.read_msg`` (the ingestion path)
takes headers and body from them, and ``encrypted_ooxml`` opens an
encrypted OOXML container with ``Cfb``. The decoders
turn one ``.msg`` stream into text, or raise ``ValueError`` when its bytes do
not match the format, so a caller can account a stream it could not decode as
unread with a reason:

* ``fixed_properties``: the ``__properties_version1.0`` table (sent and
  received times, flags, sizes, recipient type, attach method, ...). Variable
  values are only listed there; their bytes live in their own streams. A
  property type nobody listed fails the whole table (closed world);
  ``property_table`` gives the same table raw, by tag, and ``filetime`` a time;
* ``string_value``, ``fixed_values``: single and multi-valued properties;
* ``rtf_decompress`` (MS-OXRTFCP, CRC checked) and ``rtf_text``: the
  compressed RTF body as plain text, or as the HTML it encapsulates;
* ``named_properties``: the ``__nameid_version1.0`` map (``0x8000 +`` index to
  a name), used to label named string properties;
* ``embedded_message``: a message attached as an item (a storage, no bytes of its
  own) as the standalone ``.msg`` Outlook saves it as, built in memory, so it is
  read like any attached ``.msg``.

``OPAQUE_BINARY`` maps the binary properties that are identifiers (the
``PidTag*EntryId`` family, record, instance, source and change keys, the
conversation index and id) or the attachment's icon rendering to their
MS-OXPROPS names: they carry no text, are parsed as such and say so.
``ADDRESS_BINARY`` maps the ones that hold an address (the address search keys,
``ADDRTYPE:ADDRESS``, and ``PidTagReplyRecipientEntries``, a list of entry ids):
``binary_text`` decodes them to that address text or raises ``ValueError``, so
they are never read with no text. An entry id that is a one-off (MS-OXCDATA
2.2.5.1: display name, its non-printable characters dropped, address type and
address inline) or an address book entry id (2.2.5.2: an X500 DN) is its address
text wherever it appears; any other entry id is a reference into a store and stays
an identifier, as does a reply-to list holding only such references. Any
other binary property is not decoded here; a property table entry of a type nobody
listed raises ``PropertyNotDecoded`` with every such tag, the raw entries and the
text of the entries that did decode (one bad entry never hides the rest).

Source text is data: it is transcribed, never interpreted. Nothing here
fetches, writes a file or runs anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
import struct
from typing import Callable
from uuid import UUID
import zlib


CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_NONE = 0xFFFFFFFF
STORAGE, STREAM, ROOT = 1, 2, 5
MAX_DIRECTORY_ENTRIES = 20_000


class Cfb:
    """Minimal read-only OLE compound file reader over bytes (bounded, loop-safe).

    The one compound-file reader of the repo: Outlook ``.msg`` here and in
    ``integrations.office_text``, and the container of an encrypted OOXML file.
    ``entries[i]`` is ``(name, kind, left, right, child, start, size)``;
    ``stream(i, limit)`` raises ``OverflowError`` for a stream over ``limit`` and
    anything malformed raises ``ValueError``. Every sector is read at most once
    per chain and every FAT sector once, so memory stays linear in the file."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or not data.startswith(CFB_MAGIC):
            raise ValueError("not a compound file")
        major, order, shift, mini_shift = struct.unpack_from("<HHHH", data, 0x1A)
        if order != 0xFFFE or (major, shift) not in ((3, 9), (4, 12)) or mini_shift != 6:
            raise ValueError("compound file header invalid")
        self.data, self.sector = data, 1 << shift
        (fat_sectors, first_dir, _tx, self.cutoff, first_minifat, _minifat_sectors,
         first_difat, difat_sectors) = struct.unpack_from("<IIIIIIII", data, 0x2C)
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        per_sector = self.sector // 4
        current, seen = first_difat, set()
        while current < 0xFFFFFFFA and len(seen) < difat_sectors:
            if current in seen:
                raise ValueError("compound file chain invalid")
            seen.add(current)
            values = struct.unpack(f"<{per_sector}I", self._sector(current))
            difat.extend(values[:-1])
            current = values[-1]
        fat = [sid for sid in difat if sid < 0xFFFFFFFA][:fat_sectors]
        # A FAT sector listed twice would multiply the table past the file's size.
        if len(fat) != fat_sectors or len(set(fat)) != len(fat):
            raise ValueError("compound file allocation table invalid")
        self.fat: list[int] = []
        for sid in fat:
            self.fat.extend(struct.unpack(f"<{per_sector}I", self._sector(sid)))
        directory = self._chain(first_dir, self.fat, self._sector, None)
        self.entries: list[tuple[str, int, int, int, int, int, int]] = []
        for offset in range(0, min(len(directory), 128 * MAX_DIRECTORY_ENTRIES) - 127, 128):
            name_len, kind = struct.unpack_from("<HB", directory, offset + 0x40)
            name = directory[offset:offset + max(0, min(name_len, 64) - 2)].decode(
                "utf-16-le", "replace")
            left, right, child = struct.unpack_from("<III", directory, offset + 0x44)
            start, size = struct.unpack_from("<IQ", directory, offset + 0x74)
            self.entries.append((name, kind, left, right, child, start,
                                 size & 0xFFFFFFFF if major == 3 else size))
        if not self.entries or self.entries[0][1] != ROOT:
            raise ValueError("compound file root missing")
        root = self.entries[0]
        self.mini = self._chain(root[5], self.fat, self._sector, root[6])
        raw = self._chain(first_minifat, self.fat, self._sector, None)
        self.minifat = list(struct.unpack(f"<{len(raw) // 4}I", raw[:len(raw) // 4 * 4]))

    def _sector(self, sid: int) -> bytes:
        offset = (sid + 1) * self.sector
        if offset + self.sector > len(self.data):
            raise ValueError("compound file sector out of range")
        return self.data[offset:offset + self.sector]

    def _mini_sector(self, sid: int) -> bytes:
        if (sid + 1) * 64 > len(self.mini):
            raise ValueError("compound file mini sector out of range")
        return self.mini[sid * 64:(sid + 1) * 64]

    @staticmethod
    def _chain(start: int, fat: list[int], read: Callable[[int], bytes],
               size: int | None) -> bytes:
        chunks, seen, sid = [], set(), start
        while sid < 0xFFFFFFFA:
            if sid in seen or sid >= len(fat):
                raise ValueError("compound file chain invalid")
            seen.add(sid)
            chunks.append(read(sid))
            sid = fat[sid]
        data = b"".join(chunks)
        if size is not None and len(data) < size:
            raise ValueError("compound file stream truncated")
        return data if size is None else data[:size]

    def stream(self, index: int, limit: int) -> bytes:
        _name, kind, _l, _r, _c, start, size = self.entries[index]
        if kind != STREAM:
            raise ValueError("compound file entry is not a stream")
        if size > limit:
            raise OverflowError("stream over the part bound")
        if size < self.cutoff:
            return self._chain(start, self.minifat, self._mini_sector, size)
        return self._chain(start, self.fat, self._sector, size)

    def children(self, index: int) -> list[int]:
        found, stack, seen = [], [self.entries[index][4]], set()
        while stack:
            current = stack.pop()
            if current == _NONE or current in seen or current >= len(self.entries):
                continue
            seen.add(current)
            found.append(current)
            stack.extend((self.entries[current][2], self.entries[current][3]))
        return sorted(found, key=lambda i: self.entries[i][0])

    def child(self, index: int, name: str) -> int | None:
        return next((i for i in self.children(index) if self.entries[i][0] == name), None)


# ------------------------------------------------------------------ properties

PROPERTIES = "__properties_version1.0"
NAMEID = "__nameid_version1.0"
ATTACHED_MESSAGE = "__substg1.0_3701000D"
RECIPIENT_PREFIX, ATTACHMENT_PREFIX = "__recip_version1.0_", "__attach_version1.0_"
# Header bytes before the property entries (MS-OXMSG 2.4.1): message, attached
# message, recipient or attachment storage.
HEADER_TOP, HEADER_EMBEDDED, HEADER_SUB = 32, 24, 8
PROPERTY_STREAM = re.compile(r"__substg1\.0_([0-9A-F]{4})([0-9A-F]{4})(?:-([0-9A-F]{8}))?")

LABELS = {
    "0037": "Subject", "0C1A": "From", "0C1F": "From address", "0042": "Sent on behalf of",
    "0065": "Sent on behalf of address", "0E04": "To", "0E03": "Cc", "0E02": "Bcc",
    "0070": "Conversation topic", "0E1D": "Normalized subject", "1035": "Message-ID",
    "1039": "References", "1042": "In-Reply-To", "007D": "Transport headers",
    "3001": "Name", "3003": "Address", "004F": "Reply to", "39FE": "SMTP address", "5FF6": "Recipient",
    "3707": "Attachment", "3704": "Attachment", "370E": "Attachment type",
    "3712": "Attachment content id",
    "0039": "Sent", "0E06": "Received", "3007": "Created", "3008": "Modified",
    "0E07": "Message flags", "0E08": "Size", "0017": "Importance", "0036": "Sensitivity",
    "0E1B": "Has attachments", "0C15": "Recipient type", "3705": "Attach method",
    "0E20": "Attach size", "0E21": "Attach number", "3FDE": "Internet codepage",
    "3FF1": "Locale"}
# Binary properties that are identifiers or a rendering, by MS-OXPROPS name: parsed,
# no text (an entry id among them that is a one-off or address book entry id is read as
# its address, binary_text). (0x3016 is PidTagConversationIndexTracking, a boolean: the
# conversation id is 0x3013. PidTagSearchKey 0x300B is a recipient's ADDRTYPE:ADDRESS,
# but a message's own search key is an opaque id.)
OPAQUE_BINARY = {
    "000B": "PidTagConversationKey", "0025": "PidTagParentKey",
    "003F": "PidTagReceivedByEntryId",
    "0041": "PidTagSentRepresentingEntryId", "0043": "PidTagReceivedRepresentingEntryId",
    "0045": "PidTagReportEntryId", "0046": "PidTagReadReceiptEntryId",
    "004C": "PidTagOriginalAuthorEntryId",
    "005B": "PidTagOriginalSenderEntryId",
    "005E": "PidTagOriginalSentRepresentingEntryId", "0071": "PidTagConversationIndex",
    "0C19": "PidTagSenderEntryId",
    "0E09": "PidTagParentEntryId", "0E0A": "PidTagSentMailEntryId",
    "0FF6": "PidTagInstanceKey", "0FF8": "PidTagMappingSignature",
    "0FF9": "PidTagRecordKey", "0FFA": "PidTagStoreRecordKey", "0FFB": "PidTagStoreEntryId",
    "0FFF": "PidTagEntryId", "300B": "PidTagSearchKey", "3010": "PidTagTargetEntryId",
    "3013": "PidTagConversationId", "3702": "PidTagAttachEncoding",
    "3709": "PidTagAttachRendering", "370A": "PidTagAttachTag",
    "3A12": "PidTagOriginalEntryId", "3FF9": "PidTagCreatorEntryId",
    "3FFB": "PidTagLastModifierEntryId", "5FF7": "PidTagRecipientEntryId",
    "65E0": "PidTagSourceKey", "65E1": "PidTagParentSourceKey", "65E2": "PidTagChangeKey",
    "65E3": "PidTagPredecessorChangeList", "6619": "PidTagUserEntryId"}
# Binary properties that hold an address, by MS-OXPROPS name: read as it or unread.
ADDRESS_BINARY = {
    "003B": "PidTagSentRepresentingSearchKey", "004F": "PidTagReplyRecipientEntries",
    "0051": "PidTagReceivedBySearchKey", "0052": "PidTagReceivedRepresentingSearchKey",
    "0053": "PidTagReadReceiptSearchKey", "0054": "PidTagReportSearchKey",
    "005C": "PidTagOriginalSenderSearchKey", "005F": "PidTagOriginalSentRepresentingSearchKey",
    "0C1D": "PidTagSenderSearchKey"}
_ENTRY_LISTS = frozenset({"004F"})

_EPOCH_1601 = datetime(1601, 1, 1, tzinfo=timezone.utc)
_EPOCH_1899 = datetime(1899, 12, 30, tzinfo=timezone.utc)
# type -> (size, decoder of the raw little-endian bytes)
_FIXED: dict[int, tuple[int, Callable[[bytes], str]]] = {}


def filetime(raw: bytes) -> datetime | None:
    """The moment of a ``PT_SYSTIME`` value; ``None`` past ``datetime``'s range."""
    try:
        return _EPOCH_1601 + timedelta(microseconds=struct.unpack("<Q", raw)[0] // 10)
    except OverflowError:
        return None


def _filetime(raw: bytes) -> str:
    moment = filetime(raw)
    if moment is None:  # Outlook writes far-future sentinels ("never")
        return f"filetime:{struct.unpack('<Q', raw)[0]}"
    return moment.isoformat().replace("+00:00", "Z")


def _apptime(raw: bytes) -> str:
    days = struct.unpack("<d", raw)[0]
    try:
        return (_EPOCH_1899 + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        return f"apptime:{days!r}"


_FIXED.update({
    0x0002: (2, lambda r: str(struct.unpack("<h", r)[0])),
    0x0003: (4, lambda r: str(struct.unpack("<i", r)[0])),
    0x0004: (4, lambda r: repr(struct.unpack("<f", r)[0])),
    0x0005: (8, lambda r: repr(struct.unpack("<d", r)[0])),
    0x0006: (8, lambda r: str(Decimal(struct.unpack("<q", r)[0]).scaleb(-4))),
    0x0007: (8, _apptime),
    0x000A: (4, lambda r: f"error:0x{struct.unpack('<I', r)[0]:08X}"),
    0x000B: (2, lambda r: "true" if struct.unpack("<H", r)[0] else "false"),
    0x0014: (8, lambda r: str(struct.unpack("<q", r)[0])),
    0x0040: (8, _filetime),
    0x0048: (16, lambda r: "{" + str(UUID(bytes_le=r)) + "}")})
# Types whose value lives in its own stream; the table only lists them.
_VARIABLE = frozenset({0x000D, 0x001E, 0x001F, 0x0048, 0x00FB, 0x00FD, 0x00FE, 0x0102})


class PropertyNotDecoded(ValueError):
    """Property table entries of a type nobody listed (closed world); ``tags`` are
    their 32-bit property tags in table order, each once, never a value; ``rows`` their
    16-byte table entries; ``text`` the table's entries that did decode."""

    def __init__(self, tags: list[int], rows: bytes, text: str) -> None:
        super().__init__("property type not decoded")
        self.tags, self.rows, self.text = tags, rows, text


def label(prop_id: str, names: dict[int, str]) -> str:
    """The display label of property ``prop_id`` (4 hex digits); a named property by its name."""
    named = names.get(int(prop_id, 16))
    return LABELS.get(prop_id) or (named and f"[{named}]") or f"0x{prop_id}"


def _table(data: bytes, header: int) -> list[tuple[int, bytes]]:
    body = data[header:]
    if len(data) < header or len(body) % 16:
        raise ValueError("property table size invalid")
    return [(struct.unpack_from("<I", body, offset)[0], body[offset + 8:offset + 16])
            for offset in range(0, len(body), 16)]


def property_table(data: bytes, header: int) -> dict[int, bytes]:
    """``tag -> 8 value bytes`` of one ``__properties_version1.0`` table (a
    variable-size property's entry holds its size, its bytes are a stream)."""
    return dict(_table(data, header))


def fixed_properties(data: bytes, header: int, names: dict[int, str] | None = None) -> str:
    """The fixed-size values of one property table, one ``Label: value`` line each."""
    lines, undecoded, rows = [], [], []
    for offset, (tag, value) in enumerate(_table(data, header)):
        prop_type, prop_id = tag & 0xFFFF, f"{tag >> 16:04X}"
        if prop_type in _VARIABLE or prop_type & 0x1000:
            continue  # listed here, its bytes are their own stream
        if prop_type not in _FIXED:
            if tag not in undecoded:
                undecoded.append(tag)
            rows.append(data[header + 16 * offset:header + 16 * offset + 16])
            continue
        size, decode = _FIXED[prop_type]
        lines.append(f"{label(prop_id, names or {})}: {decode(value[:size])}")
    if undecoded:
        raise PropertyNotDecoded(undecoded, b"".join(rows), "\n".join(lines))
    return "\n".join(lines)


def string_value(data: bytes, prop_type: str, codepage: str = "cp1252") -> str:
    """A ``PT_UNICODE`` (``...1F``) or 8-bit ``PT_STRING8`` value in ``codepage``."""
    return data.decode("utf-16-le" if prop_type.endswith("1F") else codepage,
                       "replace").rstrip("\x00")


def fixed_values(data: bytes, prop_type: int) -> list[str]:
    """The values of a fixed-size single (``0x0048``) or multi-valued property stream."""
    if prop_type & 0x0FFF not in _FIXED:
        raise ValueError("property values not decoded")
    size, decode = _FIXED[prop_type & 0x0FFF]
    if len(data) % size:
        raise ValueError("property values not decoded")
    return [decode(data[i:i + size]) for i in range(0, len(data), size)]


# ------------------------------------------------------------------ addresses

_ONE_OFF_UID = bytes.fromhex("812B1FA4BEA310199D6E00DD010F5402")
_ADDRESS_BOOK_UID = bytes.fromhex("DCA740C8C042101AB4B908002B2FE182")
_SEARCH_KEY = re.compile(r"([A-Za-z0-9][A-Za-z0-9_-]*):([\x20-\x7E]+)")


def _strings(data: bytes, count: int, unicode: bool) -> list[str]:
    """``count`` NUL-terminated strings filling ``data`` exactly."""
    width = 2 if unicode else 1
    found, offset = [], 0
    for _ in range(count):
        end = data.find(bytes(width), offset)
        while end != -1 and (end - offset) % width:  # a UTF-16 NUL starts on a code unit
            end = data.find(bytes(width), end + 1)
        if end == -1:
            raise ValueError("entry id string unterminated")
        found.append(data[offset:end].decode("utf-16-le" if unicode else "cp1252"))
        offset = end + width
    if offset != len(data):
        raise ValueError("entry id size invalid")
    return found


def _entry_id_text(data: bytes) -> str | None:
    """The address of a one-off (MS-OXCDATA 2.2.5.1) or address book (2.2.5.2) entry id,
    ``None`` for any other entry id; ``ValueError`` when one of those two is malformed."""
    if len(data) < 24 or data[:4] != bytes(4):
        return None
    if data[4:20] == _ONE_OFF_UID:
        version, flags = struct.unpack_from("<HH", data, 20)
        if version:
            raise ValueError("one-off entry id version invalid")
        name, kind, address = _strings(data[24:], 3, bool(flags & 0x8000))
        name = "".join(c for c in name if c.isprintable())
        if not kind or not address:
            raise ValueError("one-off entry id has no address")
        return f"{name} <{kind}:{address}>" if name else f"{kind}:{address}"
    if data[4:20] == _ADDRESS_BOOK_UID:
        if len(data) < 29 or struct.unpack_from("<I", data, 20)[0] != 1:
            raise ValueError("address book entry id invalid")
        (dn,) = _strings(data[28:], 1, False)
        if not dn.isprintable() or not dn:
            raise ValueError("address book entry id has no address")
        return f"EX:{dn}"
    return None


def _search_key_text(data: bytes) -> str:
    """An ``ADDRTYPE:ADDRESS`` search key (NUL-terminated, MS-OXPROPS)."""
    match = _SEARCH_KEY.fullmatch(data.removesuffix(b"\0").decode("ascii", "replace"))
    if not data.endswith(b"\0") or match is None:
        raise ValueError("search key is no address")
    return match[0]


def _entry_list_text(data: bytes) -> str | None:
    """The addresses of a ``FlatEntryList`` (MS-OXCDATA 2.3.3): one per one-off or
    address book entry id, a reference into a store adding none; ``None`` (identifiers)
    when every entry is such a reference."""
    if len(data) < 8:
        raise ValueError("entry list size invalid")
    count, size = struct.unpack_from("<II", data, 0)
    if size != len(data) - 8:
        raise ValueError("entry list size invalid")
    found, offset = [], 8
    for _ in range(count):
        if offset + 4 > len(data):
            raise ValueError("entry list truncated")
        (length,) = struct.unpack_from("<I", data, offset)
        if offset + 4 + length > len(data):
            raise ValueError("entry list truncated")
        text = _entry_id_text(data[offset + 4:offset + 4 + length])
        if text is not None:
            found.append(text)
        offset += 4 + length + (-length % 4)
    if offset < len(data) - 3 or any(data[offset:]):
        raise ValueError("entry list size invalid")
    return "; ".join(found) if found or not count else None


def binary_text(prop_id: str, data: bytes) -> str | None:
    """The address text of binary property ``prop_id`` (``OPAQUE_BINARY`` or
    ``ADDRESS_BINARY``), ``None`` for an identifier with none. Raises ``ValueError``
    when an ``ADDRESS_BINARY`` property, or an entry id that says it is a one-off or
    address book entry id, does not decode."""
    if prop_id in _ENTRY_LISTS:
        return _entry_list_text(data)
    if prop_id in ADDRESS_BINARY:
        return _search_key_text(data)
    if prop_id == "300B":
        try:
            return _search_key_text(data)
        except ValueError:
            return None  # a message's own search key: an id
    if prop_id not in OPAQUE_BINARY:
        raise ValueError("binary property not decoded")
    return _entry_id_text(data) if OPAQUE_BINARY[prop_id].endswith("EntryId") else None


# ------------------------------------------------------------------ named properties

def named_properties(guids: bytes, entries: bytes, strings: bytes) -> dict[int, str]:
    """``0x8000 + index -> name`` from the three ``__nameid_version1.0`` streams.

    A numeric name is ``{guid}:0xLID`` (``PS_MAPI``/``PS_PUBLIC_STRINGS`` for
    GUID index 1/2). Raises ``ValueError`` on a malformed map."""
    if len(guids) % 16 or len(entries) % 8:
        raise ValueError("named property map size invalid")
    table = ["PS_MAPI", "PS_PUBLIC_STRINGS"] + [
        "{" + str(UUID(bytes_le=guids[i:i + 16])) + "}" for i in range(0, len(guids), 16)]
    names: dict[int, str] = {}
    for offset in range(0, len(entries), 8):
        ident, packed = struct.unpack_from("<II", entries, offset)
        is_string, guid_index, index = packed & 1, (packed >> 1) & 0x7FFF, packed >> 16
        if not 1 <= guid_index <= len(table):
            raise ValueError("named property guid invalid")
        if is_string:
            if ident + 4 > len(strings):
                raise ValueError("named property string out of range")
            length = struct.unpack_from("<I", strings, ident)[0]
            if ident + 4 + length > len(strings) or length % 2:
                raise ValueError("named property string out of range")
            name = strings[ident + 4:ident + 4 + length].decode("utf-16-le", "replace")
        else:
            name = f"{table[guid_index - 1]}:0x{ident:04X}"
        names[0x8000 + index] = name
    return names


# ------------------------------------------------------------------ embedded messages

_SECTOR, _MINI, _END, _FAT_SECTOR = 4096, 64, 0xFFFFFFFE, 0xFFFFFFFD


def embedded_message(cfb: Cfb, index: int, limit: int) -> bytes:
    """The message attached as an item (the ``ATTACHED_MESSAGE`` storage at ``index``)
    as a standalone ``.msg``: its storages and streams as they are, its property header
    widened to a top-level message's (MS-OXMSG 2.4.1.1: 8 reserved bytes more) and the
    file's named-property map, which an embedded message shares. Deterministic: the same
    storage gives the same bytes. Raises ``OverflowError`` past ``limit`` stream bytes
    and ``ValueError`` when the storage is malformed (a cycle, no property table)."""
    budget, seen = limit, {index}

    def tree(storage: int) -> dict[str, bytes | dict]:
        nonlocal budget
        out: dict[str, bytes | dict] = {}
        for child in cfb.children(storage):
            name, kind = cfb.entries[child][:2]
            if kind == STORAGE:
                if child in seen:
                    raise ValueError("compound file storage cycle")
                seen.add(child)
                out[name] = tree(child)
            elif kind == STREAM:
                out[name] = cfb.stream(child, budget)
                budget -= len(out[name])
        return out

    root = tree(index)
    props = root.get(PROPERTIES)
    if not isinstance(props, bytes) or len(props) < HEADER_EMBEDDED:
        raise ValueError("embedded message has no property table")
    root[PROPERTIES] = (props[:HEADER_EMBEDDED] + bytes(HEADER_TOP - HEADER_EMBEDDED) +
                        props[HEADER_EMBEDDED:])
    nameid = cfb.child(0, NAMEID)
    if NAMEID not in root and nameid is not None and cfb.entries[nameid][1] == STORAGE:
        seen.add(nameid)
        root[NAMEID] = tree(nameid)
    return _compound_file(root)


def _compound_file(tree: dict[str, bytes | dict]) -> bytes:
    """A version-4 compound file of ``{name: bytes | {...}}``: streams under 4096 bytes
    in the mini stream, siblings a right-leaning binary tree in MS-CFB name order."""
    entries: list[dict] = []

    def add(name: str, node: bytes | dict) -> int:
        at = len(entries)
        entries.append({"name": name, "kind": STORAGE if isinstance(node, dict) else STREAM,
                        "data": b"" if isinstance(node, dict) else node, "kids": []})
        if isinstance(node, dict):
            entries[at]["kids"] = [add(key, node[key]) for key in sorted(
                node, key=lambda key: (len(key), key.upper()))]
        return at

    add("Root Entry", tree)
    entries[0]["kind"] = ROOT
    mini, mini_fat, big = bytearray(), [], []
    for entry in entries:
        data = entry["data"]
        if entry["kind"] == STREAM and len(data) < _SECTOR:
            count = -(-len(data) // _MINI)
            entry["start"] = len(mini_fat) if count else _END
            mini_fat += [len(mini_fat) + i + 1 for i in range(count - 1)] + [_END] * bool(count)
            mini += data + bytes(-len(data) % _MINI)
        elif entry["kind"] == STREAM:
            big.append(entry)
    entries[0]["data"] = bytes(mini)
    regions = [(-(-len(entries) * 128 // _SECTOR), None),
               (-(-len(mini_fat) * 4 // _SECTOR), None), (-(-len(mini) // _SECTOR), entries[0]),
               *((-(-len(e["data"]) // _SECTOR), e) for e in big)]
    others = sum(count for count, _ in regions)
    fat_count = -(-others // (_SECTOR // 4 - 1))
    if fat_count > 109:   # past the header's allocation list (no DIFAT written)
        raise OverflowError("embedded message over the compound file bound")
    fat, starts, cursor = [_FAT_SECTOR] * fat_count, [], fat_count
    for count, entry in regions:
        starts.append(cursor if count else _END)
        fat += [cursor + i + 1 for i in range(count - 1)] + [_END] * bool(count)
        if entry is not None:
            entry["start"] = starts[-1]
        cursor += count
    fat += [_NONE] * (fat_count * (_SECTOR // 4) - len(fat))
    directory = bytearray()
    for entry in entries:
        kids = entry["kids"]
        for left, right in zip(kids, kids[1:]):
            entries[left]["right"] = right
        name = entry["name"].encode("utf-16-le") + b"\0\0"
        directory += (name.ljust(64, b"\0") + struct.pack(
            "<HBB3I", len(name), entry["kind"], 1, _NONE, entry.get("right", _NONE),
            kids[0] if kids else _NONE) + bytes(36) +
            struct.pack("<IQ", entry.get("start", _END), len(entry["data"])))
    directory += (bytes(64) + struct.pack("<HBB3I", 0, 0, 0, _NONE, _NONE, _NONE) +
                  bytes(48)) * (-len(entries) % (_SECTOR // 128))
    pad = lambda raw: bytes(raw) + bytes(-len(raw) % _SECTOR)  # noqa: E731
    header = (CFB_MAGIC + bytes(16) + struct.pack(
        "<HHHHH6xIIIIIIIII", 0x3E, 4, 0xFFFE, 12, 6, regions[0][0], fat_count, starts[0], 0,
        _SECTOR, starts[1], regions[1][0], _END, 0) +
        struct.pack("<109I", *range(fat_count), *[_NONE] * (109 - fat_count)))
    return b"".join((pad(header), struct.pack(f"<{len(fat)}I", *fat), pad(directory),
                     pad(struct.pack(f"<{len(mini_fat)}I", *mini_fat)), pad(mini),
                     *(pad(entry["data"]) for entry in big)))


# ------------------------------------------------------------------ compressed RTF

_RTF_PREBUF = (b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman "
               b"\\fswiss \\fmodern \\fscript \\fdecor MS Sans SerifSymbolArialTimes New Roman"
               b"Courier{\\colortbl\\red0\\green0\\blue0\r\n\\par \\pard\\plain\\f0\\fs20\\b\\i"
               b"\\u\\tab\\tx")
_LZFU, _MELA = 0x75465A4C, 0x414C454D


def rtf_decompress(data: bytes, limit: int) -> bytes:
    """The RTF of a ``PidTagRtfCompressed`` stream (MS-OXRTFCP), CRC and sizes checked."""
    if len(data) < 16:
        raise ValueError("compressed rtf header missing")
    comp_size, raw_size, kind, crc = struct.unpack_from("<IIII", data, 0)
    if comp_size != len(data) - 4 or raw_size > limit:
        raise ValueError("compressed rtf size invalid")
    if kind == _MELA:
        return data[16:16 + raw_size]
    if kind != _LZFU or zlib.crc32(data[16:], 0xFFFFFFFF) ^ 0xFFFFFFFF != crc:
        raise ValueError("compressed rtf invalid")
    window = bytearray(4096)
    window[:len(_RTF_PREBUF)] = _RTF_PREBUF
    position, out, index = len(_RTF_PREBUF), bytearray(), 16
    while index < len(data):
        control = data[index]
        index += 1
        for bit in range(8):
            if index >= len(data):
                break
            if not control >> bit & 1:
                byte = data[index]
                index += 1
                out.append(byte)
                window[position] = byte
                position = (position + 1) % 4096
                continue
            if index + 2 > len(data):
                raise ValueError("compressed rtf truncated")
            token = data[index] << 8 | data[index + 1]
            index += 2
            offset, length = token >> 4, (token & 0xF) + 2
            if offset == position:
                if len(out) > raw_size:
                    raise ValueError("compressed rtf size invalid")
                return bytes(out)
            for k in range(length):
                byte = window[(offset + k) % 4096]
                out.append(byte)
                window[position] = byte
                position = (position + 1) % 4096
            if len(out) > raw_size:
                raise ValueError("compressed rtf size invalid")
    raise ValueError("compressed rtf has no end")


_RTF_TOKEN = re.compile(rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|"
                        rb"([^\\{}\r\n]+)|[\r\n]+", re.S)
# Destinations whose text is never body text.
_RTF_SKIP = frozenset({b"fonttbl", b"colortbl", b"stylesheet", b"info", b"pict", b"object",
                       b"listtable", b"listoverridetable", b"rsidtbl", b"generator",
                       b"themedata", b"colorschememapping", b"latentstyles", b"datastore",
                       b"xmlnstbl", b"mmathPr", b"header", b"footer"})
_RTF_BREAKS = {b"par": "\n", b"line": "\n", b"row": "\n", b"page": "\n", b"sect": "\n",
               b"tab": "\t", b"cell": "\t", b"emdash": "\u2014", b"endash": "\u2013",
               b"bullet": "\u2022", b"lquote": "\u2018", b"rquote": "\u2019",
               b"ldblquote": "\u201c", b"rdblquote": "\u201d"}


def rtf_text(rtf: bytes) -> tuple[str, bool]:
    """``(text, is_html)``: the visible text of an RTF body, or the HTML an Outlook
    ``\\fromhtml1`` body encapsulates (``\\*\\htmltag`` groups kept, ``\\htmlrtf``
    runs dropped)."""
    if not rtf.lstrip().startswith(b"{\\rtf"):
        raise ValueError("not rtf")
    html = b"\\fromhtml1" in rtf[:1024]
    codepage = "cp1252"
    match = re.search(rb"\\ansicpg(\d{3,5})", rtf[:1024])
    if match:
        try:
            "".encode(f"cp{int(match[1])}")
            codepage = f"cp{int(match[1])}"
        except LookupError:
            pass
    out: list[str] = []
    pending = bytearray()
    # Per group: [skip, htmltag, uc count]; htmlrtf toggles suppression.
    stack: list[list] = [[False, False, 1]]
    suppress, star, skip_chars = False, False, 0

    def flush() -> None:
        if pending:
            out.append(pending.decode(codepage, "replace"))
            pending.clear()

    def emit(text: str | bytes) -> None:
        skip, htmltag, _uc = stack[-1]
        if skip or (suppress and not htmltag):
            return
        if isinstance(text, bytes):
            pending.extend(text)
        else:
            flush()
            out.append(text)

    for token in _RTF_TOKEN.finditer(rtf):
        word, number, hexbyte, symbol, brace, plain = token.groups()
        if brace is not None:
            if brace == b"{":
                stack.append(list(stack[-1]))
            elif len(stack) > 1:
                stack.pop()
            star = False
            continue
        if skip_chars and (hexbyte is not None or plain):  # the fallback text after a u word
            dropped = 1 if hexbyte is not None else min(skip_chars, len(plain))
            skip_chars -= dropped
            if hexbyte is not None or not plain[dropped:]:
                continue
            plain = plain[dropped:]
        if word is not None:
            if star and word == b"htmltag" and html:
                stack[-1][1] = True
            elif star or word in _RTF_SKIP:
                stack[-1][0] = True
            star = False
            if word == b"htmlrtf":
                suppress = number != b"0"
            elif word == b"uc" and number is not None:
                stack[-1][2] = int(number)
            elif word == b"u" and number is not None:
                emit(chr(int(number) % 0x10000))
                skip_chars = stack[-1][2]
            elif word in _RTF_BREAKS:
                emit(_RTF_BREAKS[word])
        elif hexbyte is not None:
            emit(bytes([int(hexbyte, 16)]))
        elif symbol is not None:
            if symbol == b"*":
                star = True
            elif symbol in (b"\\", b"{", b"}"):
                emit(symbol)
            elif symbol == b"~":
                emit("\u00a0")
            elif symbol == b"_":
                emit("-")
            elif symbol in (b"\n", b"\r"):
                emit("\n")
        elif plain:
            emit(plain)
    flush()
    return "".join(out), html


__all__ = ["ADDRESS_BINARY", "ATTACHED_MESSAGE", "ATTACHMENT_PREFIX", "CFB_MAGIC", "Cfb",
           "HEADER_EMBEDDED", "HEADER_SUB", "HEADER_TOP", "LABELS", "NAMEID", "OPAQUE_BINARY",
           "PROPERTIES", "PROPERTY_STREAM", "PropertyNotDecoded", "RECIPIENT_PREFIX",
           "binary_text", "embedded_message",
           "filetime", "fixed_properties", "fixed_values", "label", "named_properties",
           "property_table", "rtf_decompress", "rtf_text", "string_value"]
