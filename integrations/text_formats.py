"""Stdlib readers for the text-bearing files the extractor once called ``unsupported``:
the one place ``document_understanding.extract_document`` reads them from.

* Text that is not UTF-8 (:func:`decode_text`): a UTF-16 file with its byte-order mark
  (what Windows Notepad and PowerShell write), kept only when it reads as text (no
  control character, mostly Latin script: any even byte run decodes as UTF-16), an
  XML declaration's named encoding, or Windows-1252 (what Excel's CSV export writes),
  taken only when every byte decodes and no control character other than tab, line
  feed, form feed and carriage return appears, so a binary file is never read as text.
  Each non-UTF-8 reading is named in a warning (``text_decoded_utf16``,
  ``text_decoded_cp1252``, ...).
* Rich Text (:func:`rtf_document_text`): the visible text of the body by the one RTF
  reader (``operating.msg_reader.rtf_text``), skipping the font, colour and style
  tables, metadata, pictures, objects, field instructions, headers, footers, bookmarks
  and index entries (:data:`RTF_FILE_SKIP`); an encapsulated HTML body is returned as
  HTML for the caller's HTML reading.
* XML, SVG included (:func:`xml_text`): the character data of every element, CDATA
  included, and the values of the attributes that name or describe a thing
  (``XML_TEXT_ATTRIBUTES``: a Sage 300 object model's ``desc``, an SVG's ``title``);
  never a path, a style or a coordinate (script and style content dropped). No parser
  and no DTD: comments, processing instructions and the DOCTYPE are dropped, and no
  entity is expanded but the five predefined ones and character references, so no
  document can grow past its bytes.
* Label files (:func:`label_preview`): a BarTender ``.btw`` and a NiceLabel ``.lbl``
  store their label in a proprietary form, but each carries the label's own rendered
  preview (a PNG after the BarTender header; the ``NICE Preview Big`` device-independent
  bitmap of the NiceLabel compound file). The preview is handed to local OCR
  (``captured_text.capture_text``), so a label's printed text (product, item code,
  case pack, customer) becomes OCR text like a scanned page's. A NiceLabel ``.nlbl``
  (2017 and later) is an encrypted archive: ``encrypted_unreadable``, a named gap.

* What is no search text (:func:`terminal_class`): a file the extractor does not read gets
  a named terminal class instead of a bare ``unsupported``, so it stops counting as an
  unread gap. Plain-text files of the business's own (:data:`TEXT_FILE_SUFFIXES`: logs,
  SQL, scripts, configuration) are read as text and searched. Source code
  (:data:`SOURCE_CODE_SUFFIXES`), a font or glyph source, and a text file without any
  suffix (a licence, a ``.git`` file, a tool's launcher: what a code tree is made of)
  are ``code_not_indexed``: the bytes are kept and known to be text, never put in
  search, where one dependency tree's thousands of files would bury the documents. A
  type that holds no text by nature (:data:`BINARY_SUFFIXES`), or a text-type file whose
  bytes are not text, is ``binary_no_text``; an encrypted label archive is
  ``encrypted_unreadable``; a zip is ``container_members`` (its members
  are expanded when read: ``captured_text.container_members``).

Pure functions of the bytes: nothing here fetches, writes or runs anything.
"""

from __future__ import annotations

import codecs
import re
import struct

from operating.msg_reader import _RTF_SKIP, CFB_MAGIC, Cfb, rtf_text

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def _latin(text: str) -> bool:
    """Whether at least half of ``text``'s characters are Latin script or punctuation."""
    return sum(ord(c) < 0x250 for c in text) * 2 >= len(text)


def decode_text(content: bytes, *, encoding: str | None = None
                ) -> tuple[str, tuple[str, ...]] | None:
    """``(text, warnings)`` of bytes written as text: UTF-8 (a byte-order mark dropped;
    no warning), UTF-16 by its byte-order mark, ``encoding`` when given and known (an XML
    declaration's), else Windows-1252 (module docstring); None when none reads it."""
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            text = content.decode("utf-16")
        except UnicodeDecodeError:
            return None
        body = text.lstrip("\ufeff")
        if not body or _CONTROL.search(body) or not _latin(body):
            return None
        return text, ("text_decoded_utf16",)
    try:
        return content.decode("utf-8-sig"), ()
    except UnicodeDecodeError:
        pass
    for name in (encoding, "cp1252"):
        if not name:
            continue
        try:
            codec = codecs.lookup(name).name
            text = content.decode(codec)
        except (LookupError, UnicodeDecodeError):
            continue
        if not _CONTROL.search(text):
            return text, (f"text_decoded_{codec}",)
    return None


def _lines(text: str) -> str:
    """``text`` with each line's runs of whitespace folded, blank lines dropped."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


# ------------------------------------------------------------------------------- RTF

# The destinations of a Rich Text file that are not its text: the message reader's set
# and the page furniture, bookmarks, index/TOC entries and list-number runs a document
# file carries.
RTF_FILE_SKIP = _RTF_SKIP | frozenset({
    b"fldinst", b"filetbl", b"revtbl", b"headerl", b"headerr", b"headerf", b"footerl",
    b"footerr", b"footerf", b"bkmkstart", b"bkmkend", b"pgdsctbl", b"wgrffmtfilter",
    b"nonshppict", b"userprops", b"xe", b"tc", b"listtext", b"pntext", b"pntxta",
    b"pntxtb", b"objdata"})


def is_rtf_bytes(content: bytes) -> bool:
    """Whether ``content`` begins as Rich Text (``{\\rtf`` after any whitespace)."""
    return content[:64].lstrip()[:5] == b"{\\rtf"


def rtf_document_text(content: bytes) -> tuple[str, bool]:
    """``(text, is_html)`` of a Rich Text file: its visible text, line by line, or the
    HTML body it encapsulates (``\\fromhtml1``) when ``is_html``. Raises ``ValueError``
    for bytes that are not RTF."""
    body, is_html = rtf_text(content, skip=RTF_FILE_SKIP)
    return (body if is_html else _lines(body)), is_html


# ------------------------------------------------------------------------------- XML

# Attributes whose value is a name or a description a person would search for.
XML_TEXT_ATTRIBUTES = frozenset({"name", "desc", "description", "title", "label", "caption",
                                 "text", "value", "comment", "summary"})
_XML_DECLARED = re.compile(rb"""^\s*<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]{1,40})["']""")
_XML_NAME = re.compile(r"/?([A-Za-z_][\w:.-]*)")
# The lookbehind keeps a name from starting inside a longer run of name characters, so a
# failed match is never retried from each later character (that was quadratic).
_XML_ATTR = re.compile(r"""(?<![\w:.-])([A-Za-z_][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_XML_REF = re.compile(r"&(?:(lt|gt|amp|quot|apos)|#([0-9]{1,7})|#[xX]([0-9a-fA-F]{1,6}));")
_XML_NAMED = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
MAX_XML_TAG_SCAN = 65_536   # a longer tag contributes no attributes
_XML_SKIPPED = frozenset({"script", "style"})


def _xml_unescape(text: str) -> str:
    """``text`` with the predefined entities and character references read; any other
    entity reference is left as written (never expanded)."""
    def one(m: re.Match) -> str:
        if m[1]:
            return _XML_NAMED[m[1]]
        code = int(m[2]) if m[2] else int(m[3], 16)
        return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else m[0]
    return _XML_REF.sub(one, text)


def xml_text(content: bytes) -> tuple[str, tuple[str, ...]] | None:
    """``(text, warnings)``: the searchable text of an XML document (module docstring),
    each element's text on its own line, blanks folded; ``""`` when it holds none.
    None when its bytes are not text (``decode_text``) or it does not begin with a tag."""
    declared = _XML_DECLARED.match(content[:200].replace(b"\x00", b""))
    decoded = decode_text(content, encoding=declared[1].decode() if declared else None)
    if decoded is None:
        return None
    text, warnings = decoded
    if not text.lstrip("\ufeff \t\r\n").startswith("<"):
        return None
    parts: list[str] = []
    skipped, at, end, gt = 0, 0, len(text), -1
    # One forward scan (each search starts where the last stopped, so the cost is linear
    # in the bytes whatever the markup, a malformed one included).
    while at < end:
        lt = text.find("<", at)
        if lt < 0:
            lt = end
        if lt > at and not skipped:
            parts.append(_xml_unescape(text[at:lt]))
        if lt >= end:
            break
        for opener, closer, keep in (("<!--", "-->", False), ("<?", "?>", False),
                                     ("<![CDATA[", "]]>", True)):
            if text.startswith(opener, lt):
                stop = text.find(closer, lt + len(opener))
                stop = end if stop < 0 else stop
                if keep and not skipped:
                    parts.append(text[lt + len(opener):stop])
                at = stop + len(closer)
                break
        else:
            if gt < lt:
                gt = text.find(">", lt)
                if gt < 0:      # no tag closes again: the rest is no text
                    break
            if text.startswith("<!", lt):   # DOCTYPE (its internal subset too), others
                bracket = text.find("[", lt, gt)
                if bracket >= 0:
                    close = text.find("]", bracket)
                    gt = text.find(">", close) if close >= 0 else -1
                    if gt < 0:
                        break
                at = gt + 1
                continue
            inner = text.find("<", lt + 1, gt)
            if inner >= 0:      # a stray "<": text, scanned on from the next one
                if not skipped:
                    parts.append(text[lt:inner])
                at = inner
                continue
            body = text[lt + 1:gt]
            at = gt + 1
            name = _XML_NAME.match(body)
            if name is None:
                continue
            local = name[1].rsplit(":", 1)[-1].lower()
            if body.startswith("/"):
                if local in _XML_SKIPPED:
                    skipped = max(0, skipped - 1)
                continue
            if local in _XML_SKIPPED and not body.rstrip().endswith("/"):
                skipped += 1
            if skipped:
                continue
            if len(body) > MAX_XML_TAG_SCAN:
                continue
            for a in _XML_ATTR.finditer(body, name.end()):
                value = a[2] if a[2] is not None else a[3]
                if value and a[1].rsplit(":", 1)[-1].lower() in XML_TEXT_ATTRIBUTES:
                    parts.append(_xml_unescape(value))
    return _lines("\n".join(parts)), warnings


# ------------------------------------------------------------------- label previews

LABEL_SUFFIXES = (".btw", ".lbl")
BARTENDER_MAGIC = b"Bar Tender Format File"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_PREVIEW_BYTES = 8_000_000
MAX_PREVIEW_SIDE = 10_000
_NICE_PREVIEWS = ("NICE Preview Big", "NICE Preview")


def _png_at(content: bytes, start: int) -> bytes | None:
    """The one whole PNG beginning at ``start`` (chunk by chunk to its ``IEND``,
    a sane ``IHDR`` first), else None."""
    at = start + len(PNG_MAGIC)
    first = True
    while at + 12 <= len(content) and at - start <= MAX_PREVIEW_BYTES:
        length, kind = struct.unpack_from(">I4s", content, at)
        end = at + 12 + length
        if end > len(content):
            return None
        if first:
            if kind != b"IHDR" or length != 13:
                return None
            width, height = struct.unpack_from(">II", content, at + 8)
            if not (0 < width <= MAX_PREVIEW_SIDE and 0 < height <= MAX_PREVIEW_SIDE):
                return None
            first = False
        if kind == b"IEND":
            return content[start:end] if end - start <= MAX_PREVIEW_BYTES else None
        at = end
    return None


def _bmp_of(dib: bytes) -> bytes | None:
    """A BMP file of a device-independent bitmap (a BITMAPINFOHEADER and its pixels),
    else None when the header is not one an image reader can take."""
    if len(dib) < 40:
        return None
    size, width, height, planes, bits, compression = struct.unpack_from("<IiiHHI", dib, 0)
    used = struct.unpack_from("<I", dib, 32)[0]
    if size not in (40, 108, 124) or planes != 1 or bits not in (1, 4, 8, 16, 24, 32) \
            or compression not in (0, 3) or not 0 < width <= MAX_PREVIEW_SIDE \
            or not 0 < abs(height) <= MAX_PREVIEW_SIDE or used > 256:
        return None
    palette = 4 * (used or (1 << bits if bits <= 8 else 0))
    masks = 12 if compression == 3 and size == 40 else 0
    offset = size + masks + palette
    if offset + ((width * bits + 31) // 32) * 4 * abs(height) > len(dib):
        return None
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, 14 + offset) + dib


def label_preview(content: bytes, name: str) -> bytes | None:
    """The image a label file carries of its own label (module docstring): PNG bytes of
    a BarTender ``.btw``, BMP bytes of a NiceLabel ``.lbl``; None for any other file, or
    one whose preview is missing or malformed (it stays as unread as before)."""
    lower = name.lower()
    if lower.endswith(".btw"):
        if BARTENDER_MAGIC not in content[:64]:
            return None
        start = content.find(PNG_MAGIC, 0, 64 * 1024)
        return None if start < 0 else _png_at(content, start)
    if lower.endswith(".lbl") and content.startswith(CFB_MAGIC):
        try:
            cfb = Cfb(content)
            streams = {entry[0]: index for index, entry in enumerate(cfb.entries)
                       if entry[1] == 2}
            for stream in _NICE_PREVIEWS:
                if stream in streams:
                    data = cfb.stream(streams[stream], MAX_PREVIEW_BYTES)
                    # The bitmap follows its own length (4 bytes, little-endian).
                    if len(data) >= 4 and struct.unpack_from("<I", data)[0] == len(data) - 4:
                        data = data[4:]
                    found = _bmp_of(data)
                    if found is not None:
                        return found
        except (ValueError, OverflowError, struct.error):
            return None
    return None


# Text files read and searched as written (``document_understanding.PLAIN_TEXT_SUFFIXES``).
TEXT_FILE_SUFFIXES = (".log", ".sql", ".yml", ".yaml", ".ini", ".inf", ".cfg", ".conf",
                      ".toml", ".sh", ".bat", ".cmd", ".ps1", ".psm1", ".dat", ".meta",
                      ".odc", ".vbs", ".rdp", ".ics", ".url", ".srt", ".vtt", ".plist", ".psd1",
                      ".ps1xml", ".properties", ".mdc")
# Source code and font sources: text, but never search text (module docstring).
SOURCE_CODE_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts", ".py",
                        ".pyi", ".css", ".scss", ".less", ".java", ".c", ".h", ".cpp",
                        ".hpp", ".cs", ".go", ".rb", ".php", ".rs", ".swift", ".kt",
                        ".glif", ".glyph", ".map", ".vue", ".svelte", ".sample", ".qml",
                        ".pyw", ".example", ".template")
# Types that hold no text (an image, an archive, a database, a binary, a point cloud).
BINARY_SUFFIXES = (".pkl", ".pickle", ".db", ".sqlite", ".sqlite3", ".lnk", ".psd", ".ai",
                   ".da_", ".pp_", ".dl_", ".ch_", ".in_", ".dll", ".exe", ".msi", ".so",
                   ".ttf", ".otf", ".woff", ".woff2", ".cab", ".7z", ".rar", ".gz", ".tgz",
                   ".iso", ".mp3", ".mp4", ".mov", ".avi", ".wav", ".heic", ".dwg", ".dxf",
                   ".mpp", ".pbix", ".sst", ".fof", ".cdl", ".dfi", ".hcxs", ".ply",
                   ".pyc", ".bin", ".ds_store", ".idx", ".pack", ".cat", ".p7s", ".one",
                   ".onetoc2", ".lib")
# Text the extractor cannot read for want of a key (a NiceLabel 2017+ ``.nlbl`` archive): a
# known gap with a named cause, not a file that holds no text.
ENCRYPTED_SUFFIXES = (".nlbl",)
NAME_ONLY_SUFFIXES = (*BINARY_SUFFIXES, *ENCRYPTED_SUFFIXES)   # classed by name, no bytes
ZIP_MAGIC = b"PK\x03\x04"
_ZIP_EMPTY = b"PK\x05\x06"
# A suffix-less file is sniffed as text over its first bytes (no byte-order guessing).
MAX_SNIFF_BYTES = 8_000_000


def _suffix(name: str) -> str:
    """The file's type suffix, a browser's ``.download`` copy by the name it saved
    (``app.js.download`` is ``.js``); none for a name with no dot after its first
    character."""
    base = name.rsplit("/", 1)[-1].lower()
    if base.endswith(".download") and len(base) > len(".download"):
        base = base[:-len(".download")]
    dot = base.rfind(".")
    return "" if dot <= 0 else base[dot:]


def terminal_class(name: str, content: bytes) -> str | None:
    """The terminal class of a file the extractor called ``unsupported`` (module
    docstring), or None when it is not one of the named kinds (it stays ``unsupported``
    and counts as a gap). Pure and decided by the name's suffix and the bytes: source
    code or a suffix-less file is ``code_not_indexed`` only when its bytes read as text
    (:func:`decode_text`, no byte-order mark needed), otherwise ``binary_no_text``."""
    base = name.rsplit("/", 1)[-1]
    if base.startswith("~$") or (base.startswith("$I") and len(base) <= 24
                                 and len(content) < 1024):
        # an Office owner/lock file, a Recycle Bin record: a few hundred bytes of names
        return "binary_no_text"
    suffix = _suffix(name)
    if suffix == ".zip":
        return "container_members" if content.startswith((ZIP_MAGIC, _ZIP_EMPTY)) \
            else "binary_no_text"
    if suffix in ENCRYPTED_SUFFIXES:
        return "encrypted_unreadable"
    if suffix in BINARY_SUFFIXES:
        return "binary_no_text"
    if suffix in TEXT_FILE_SUFFIXES:
        return "binary_no_text"   # text by type but not text by bytes (the read refused it)
    if suffix in SOURCE_CODE_SUFFIXES or suffix == "":
        if len(content) > MAX_SNIFF_BYTES or b"\x00" in content[:4096] \
                or decode_text(content) is None:
            return "binary_no_text"
        return "code_not_indexed"
    return None


__all__ = ["BINARY_SUFFIXES", "ENCRYPTED_SUFFIXES", "NAME_ONLY_SUFFIXES", "SOURCE_CODE_SUFFIXES", "TEXT_FILE_SUFFIXES", "terminal_class",
           "LABEL_SUFFIXES", "RTF_FILE_SKIP", "XML_TEXT_ATTRIBUTES", "decode_text",
           "is_rtf_bytes", "label_preview", "rtf_document_text", "xml_text"]
