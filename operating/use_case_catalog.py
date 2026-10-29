"""The use cases the business delivered, and the source documents each one names.

A person asks "in the screening use case, ..." or "the expired material disposition
map says ...": the use case is named, not a file. ``Catalog.named`` reads the use cases
a ``plx.find`` or ``plx.documents`` query names, so the host
(``operating.case_tools_host.CaptureCaseHostTools``) can return that use case's own
documents in a separate block (never as search hits), each through its source's
gate for the reader, and name every document the use case needs that the Workbench does not hold. An agent then answers
from the right map, or abstains, and never from another use case's map.

The catalog is data (``examples/use_case_catalog.json``, schema
``plx-use-case-catalog/v1``), never code: each use case has an ``id`` (lower case,
hyphenated), a ``title``, ``phrases`` (two words or more) and its ``documents``, each
named by where it is held, a SharePoint capture item (``sharepoint``: ``drive_id``,
``item_id``; served as its current captured version) or a case original
(``original``: an ``original://`` entry id), with a ``role``. ``gaps`` name documents
no source the Workbench reads holds (``document``, ``detail``, ``registry_row``).

Matching is on words: the query is case-folded and every run of characters that is
not a letter or digit is one space ("R&D" reads "r d", "procure-to-GL" reads
"procure to gl"). A use case is named only when the query (a) holds its id as written
(``amazon-fba``; an id of one word only as the whole query) or its full title as whole
words, or (b) holds one of its phrases, or its id's words, directly followed or
preceded by a keyword (``KEYWORDS``: "use case", "process map(s)", "map(s)",
"process"), or a phrase that itself ends with one ("lost process map"). A phrase
alone in a transactional query ("PO 5280 expired material", "amazon fba shipment
123") names nothing. A query that says "use case" and names
none in the catalog is reported (``unknown_use_case``), never matched by its words to
some other use case. The documents found are never search hits: the host lists them
in the answer's ``use_case`` block alone.

A catalog that cannot be read or is malformed fails closed: ``load`` raises
``CatalogError`` and the host answers without use-case documents and with a
``use_case_catalog_unreadable`` gap, never with a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
from typing import Any, Mapping, Sequence
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "examples" / "use_case_catalog.json"
SCHEMA = "plx-use-case-catalog/v1"
ROLES = ("map", "readme", "brief", "evidence")
# Use cases one query resolves at most, and documents one use case lists at most.
MAX_NAMED = 2
MAX_DOCUMENTS = 12
_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){0,7}$")
# A use case's title is the catalog's own words (never a source's text): plain words and
# punctuation, the pattern the plx.find/plx.documents ``use_case`` contract pins, so a
# title that is not one fails the catalog closed instead of the answer's contract.
TITLE = r"^[A-Za-z0-9][A-Za-z0-9 &:,'()/.+-]{0,199}$"
_TITLE = re.compile(TITLE)
_ENTRY = re.compile(r"^(?:co|nr|rv)-[0-9a-f]{12}$")
_SP_ID = re.compile(r"^[A-Za-z0-9!_\-]{1,200}$")
_NOT_WORD = re.compile(r"[^0-9a-z]+")
_USE_CASE = ("use", "case")
# Words that, directly after or before a phrase (or an id's words), say the query is
# about the use case or its map rather than a record: "screening process map",
# "brand requirements use case".
KEYWORDS = (_USE_CASE, ("process", "maps"), ("process", "map"), ("maps",), ("map",),
            ("process",))


class CatalogError(ValueError):
    """The catalog cannot be read or does not have the expected shape."""


def words(text: str) -> tuple[str, ...]:
    """``text`` as matching words (module docstring)."""
    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    return tuple(w for w in _NOT_WORD.split(folded) if w)


@dataclass(frozen=True)
class UseCase:
    id: str
    title: str
    phrases: tuple[tuple[str, ...], ...]
    documents: tuple[Mapping[str, Any], ...]
    gaps: tuple[Mapping[str, Any], ...]


def _at(seq: Sequence[str], part: Sequence[str]) -> int | None:
    """The first index where ``part`` occurs in ``seq`` as consecutive items."""
    n = len(part)
    for i in range(len(seq) - n + 1):
        if tuple(seq[i:i + n]) == tuple(part):
            return i
    return None


class Catalog:
    def __init__(self, use_cases: Sequence[UseCase]) -> None:
        self.use_cases = tuple(use_cases)

    def named(self, query: Any) -> tuple[list[UseCase], bool]:
        """(use cases ``query`` names, earliest named first, at most ``MAX_NAMED``;
        whether it says "use case" and names none of them)."""
        if not isinstance(query, str):
            return [], False
        folded = unicodedata.normalize("NFKC", query).casefold()
        said = words(query)
        found: list[tuple[int, int, UseCase]] = []
        for case in self.use_cases:
            ident = tuple(case.id.split("-"))
            spots: list[int | None] = []
            if len(ident) > 1:
                literal = re.search(r"(?<![0-9a-z-])" + re.escape(case.id) + r"(?![0-9a-z-])",
                                    folded)
                if literal is not None:
                    spots.append(len(words(folded[:literal.start()])))
            elif said == ident:
                spots.append(0)
            title = words(case.title)
            if len(title) > 1:
                spots.append(_at(said, title))
            for phrase in (*case.phrases, ident):
                spots.append(_beside_keyword(said, phrase))
            hits = [spot for spot in spots if spot is not None]
            if hits:
                found.append((min(hits), len(found), case))
        found.sort(key=lambda hit: hit[:2])
        cases = [case for _at_, _n, case in found][:MAX_NAMED]
        unknown = not cases and _at(said, _USE_CASE) is not None
        return cases, unknown


def _beside_keyword(said: Sequence[str], phrase: Sequence[str]) -> int | None:
    """The first index of ``phrase`` in ``said`` directly followed or preceded by a
    keyword that names a use case or its map (``KEYWORDS``), else None."""
    n = len(phrase)
    for i in range(len(said) - n + 1):
        if tuple(said[i:i + n]) != tuple(phrase):
            continue
        after, before = tuple(said[i + n:]), tuple(said[:i])
        # A phrase that itself ends with a keyword names the map ("lost process map").
        if any(tuple(phrase[-len(k):]) == k or after[:len(k)] == k
               or (len(before) >= len(k) and before[-len(k):] == k) for k in KEYWORDS):
            return i
    return None


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CatalogError(f"{field} must be a non-empty string of at most {limit} characters")
    return value


def _document(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("role") not in ROLES:
        raise CatalogError(f"{where}: a document needs a role of {ROLES}")
    sharepoint, original = raw.get("sharepoint"), raw.get("original")
    if (sharepoint is None) == (original is None) or set(raw) - {"role", "sharepoint",
                                                                 "original"}:
        raise CatalogError(f"{where}: a document is exactly one of sharepoint or original")
    if original is not None:
        if not isinstance(original, str) or not _ENTRY.match(original):
            raise CatalogError(f"{where}: original must be a case original entry id")
        return {"role": raw["role"], "original": original}
    if not isinstance(sharepoint, Mapping) or set(sharepoint) != {"drive_id", "item_id"} \
            or not all(isinstance(sharepoint[k], str) and _SP_ID.match(sharepoint[k])
                       for k in ("drive_id", "item_id")):
        raise CatalogError(f"{where}: sharepoint must be {{drive_id, item_id}}")
    return {"role": raw["role"], "sharepoint": (sharepoint["drive_id"], sharepoint["item_id"])}


def parse(data: Any) -> Catalog:
    """A :class:`Catalog` from the decoded catalog file, or ``CatalogError``."""
    if not isinstance(data, Mapping) or data.get("schema") != SCHEMA \
            or not isinstance(data.get("use_cases"), list):
        raise CatalogError(f"not a {SCHEMA} catalog")
    seen: set[str] = set()
    out = []
    for raw in data["use_cases"]:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str) \
                or not _ID.match(raw["id"]) or raw["id"] in seen:
            raise CatalogError("every use case needs a unique lower-case hyphenated id")
        where = raw["id"]
        seen.add(where)
        phrases = []
        for phrase in raw.get("phrases") or ():
            said = words(_text(phrase, f"{where}: phrase", 120))
            if len(said) < 2:
                raise CatalogError(f"{where}: a phrase needs two words or more")
            phrases.append(said)
        documents = raw.get("documents")
        gaps = raw.get("gaps")
        if not isinstance(documents, list) or not isinstance(gaps, list) \
                or len(documents) > MAX_DOCUMENTS:
            raise CatalogError(f"{where}: documents (at most {MAX_DOCUMENTS}) and gaps "
                               "must be lists")
        parsed_gaps = []
        for gap in gaps:
            if not isinstance(gap, Mapping):
                raise CatalogError(f"{where}: a gap must be an object")
            parsed_gaps.append({"document": _text(gap.get("document"), f"{where}: gap", 200),
                                "detail": _text(gap.get("detail"), f"{where}: gap", 300)})
        title = _text(raw.get("title"), f"{where}: title", 200)
        if not _TITLE.match(title):
            raise CatalogError(f"{where}: a title is plain words and punctuation "
                               "(letters, digits, space, & : , ' ( ) / . + -)")
        out.append(UseCase(where, title,
                           tuple(phrases), tuple(_document(d, where) for d in documents),
                           tuple(parsed_gaps)))
    return Catalog(out)


_CACHE: dict[str, tuple[tuple[int, int], Catalog]] = {}
_LOCK = threading.Lock()


def load(path: str | Path | None = None) -> Catalog:
    """The catalog at ``path`` (default ``CATALOG``), re-read when the file changes;
    ``CatalogError`` when it cannot be read or is malformed."""
    path = Path(path or CATALOG)
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with _LOCK:
            held = _CACHE.get(str(path))
            if held is not None and held[0] == key:
                return held[1]
        catalog = parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        if isinstance(exc, CatalogError):
            raise
        raise CatalogError(f"the catalog could not be read ({type(exc).__name__})") from None
    with _LOCK:
        _CACHE[str(path)] = (key, catalog)
    return catalog


__all__ = ["CATALOG", "Catalog", "CatalogError", "UseCase", "load", "parse", "words"]
