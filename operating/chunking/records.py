"""Chunks of business records' text (``record_text.json``).

The rule: every text-valued property of every served or partial schema type is indexed
(names, titles, subjects, descriptions, notes, comments, line descriptions, company names
and any text a new type brings), except an explicit deny-list of ids, hashes and secrets
and the document types whose own chunkers index their text (mail, attachments,
SharePoint, Teams, Mission Control tasks, meetings). SharePoint list items' columns (a
JSON property) are one ``list_item`` chunk per item. A records read out of a mail or
SharePoint document (audience family ``mail`` / ``sharepoint``) is left to that
document's chunks, which carry its re-marked markings (a payroll file stays finance+pii).
Person-family units (Teams, SharePoint lists) pass the people exclusion first.
A figure in such a field is never truth
(the record's typed values answer; the gate checks). One chunk per record and field, cited
to the value's own L1 handle and field path. Keyed as ``plx.find`` reads a value: the
object's audience unit and the union of the value's markings and the object's current
and latest version markings.
"""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path
from typing import AbstractSet, Any, Iterator, Mapping, Sequence

from . import MIN_BODY, Chunk, make_chunk
from . import headers as hd
from .normalise import normalise
from .permissions import family, parse_markings, valid_key
from .projection import BATCH, ProjectionChunker
from .split import pack

CONFIG = Path(__file__).with_name("record_text.json")
SCHEMA_DIRS = ("domains", "generated")
_SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "ontology" / "schema"
DENY_CATEGORIES = ("ids", "hashes", "secrets")


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    """``record_text.json``. The raw Portal/Sage types' text is read by its ``rule`` (raw
    rows words-only); ``ontology/schema/overrides/business.json`` gives their readable
    names (:func:`type_title`), never a second list of their fields (a raw row chunked
    twice, and as human text, if it did)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "plx-record-text/v2" or not isinstance(data.get("rule"), dict) \
            or not isinstance(data.get("deny"), dict) or \
            set(data["deny"]) - set(DENY_CATEGORIES) or \
            not isinstance(data.get("documents"), dict):
        raise ValueError("record_text_config_invalid")
    return data


def schema_types(root: Path = _SCHEMA_ROOT) -> dict[str, Mapping[str, Any]]:
    """Every object type the schema declares (domains and generated), by id."""
    out: dict[str, Mapping[str, Any]] = {}
    for sub in SCHEMA_DIRS:
        for path in sorted((root / sub).glob("*.json")):
            for t in json.loads(path.read_text(encoding="utf-8")).get("object_types") or []:
                out[t["id"]] = t
    return out


def _matches(patterns: Sequence[str], name: str) -> bool:
    """Whether a property or column name (lower-cased: list columns are CamelCase)
    full-matches one pattern."""
    folded = name.lower()
    return any(re.fullmatch(p, folded) for p in patterns)


def denied(config: Mapping[str, Any], name: str) -> str | None:
    """The deny category of a property or column name, or None."""
    for category, patterns in config["deny"].items():
        if _matches(patterns, name):
            return category
    return None


# Contact details in a value, whatever its field is called: masked before chunking.
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
# A phone number only in a real phone shape, never inside a code (``CG-139-1000-677``,
# ``PO 4500-1234-5678``): after a phone cue (tel, phone, ph, mobile, cell, fax), in
# international form (+...), or the plain NANP 3-3-4 shape; bounded on both sides by
# neither a letter, digit nor hyphen.
_EDGE_L, _EDGE_R = r"(?<![\w-])", r"(?![\w-])"
_PHONE_CUE = re.compile(r"(?i)\b(tel|phone|ph|mobile|cell|fax)(\.?\s*[:#]?\s*)"
                        r"(\+?\(?\d[\d\s().-]{5,}\d)" + _EDGE_R)
_PHONE_PLUS = re.compile(_EDGE_L + r"\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}"
                         + _EDGE_R)
_PHONE_NANP = re.compile(_EDGE_L + r"(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}" + _EDGE_R)
_LINK = re.compile(r"(?i)\b(?:https?://|www\.)\S+")


def masked(text: str) -> str:
    """``text`` with every email address and phone number replaced by a placeholder."""
    out = _EMAIL.sub("[email]", text)
    out = _PHONE_CUE.sub(lambda m: f"{m.group(1)}{m.group(2)}[phone]", out)
    return _PHONE_NANP.sub("[phone]", _PHONE_PLUS.sub("[phone]", out))


_WORD = re.compile(r"[^\W\d_]{3,}")


def human(text: str, label: bool, words_only: Mapping[str, Any]) -> bool:
    """Whether a (masked) value is human language worth embedding (``words_only``):
    words of three letters or more that are not short upper-case codes, enough of them
    (one for a name or title), and mostly letters."""
    words = [w for w in _WORD.findall(text)
             if not (w.isupper() and len(w) <= 5) and w not in ("email", "phone")]
    if len(words) < (1 if label else int(words_only.get("min_words", 3))):
        return False
    stripped = "".join(text.split())
    share = sum(ch.isalpha() for ch in stripped) / max(1, len(stripped))
    return share >= (0.4 if label else float(words_only.get("min_letter_share", 0.6)))


def never_text(config: Mapping[str, Any], object_type: str, prop: str) -> bool:
    """Whether a ``never_text`` rule keeps this property out of search whatever a reader
    holds (types, columns and unless are full-match patterns; columns on the lower-cased
    name). Wins over the rule and the ``fields`` block."""
    name = prop.lower()
    for rule in config.get("never_text") or ():
        if object_type in (rule.get("except_types") or ()):
            continue
        if not re.fullmatch(rule["types"], object_type):
            continue
        if re.fullmatch(rule["columns"], name) and not (
                rule.get("unless") and re.fullmatch(rule["unless"], name)):
            return True
    return False


def classify(config: Mapping[str, Any], object_type: str, prop: str) -> str:
    """How search covers one text property: ``label`` / ``free`` (the record chunker, the
    rule), ``document:<kind>`` or ``fields_only`` (a document type's listed property),
    ``deny:<category>``, or ``unlisted`` (a document type's property nobody listed: the
    completeness test fails on it)."""
    if never_text(config, object_type, prop):
        return "deny:never_text"
    category = denied(config, prop)
    if category is not None:
        return f"deny:{category}"
    doc = config["documents"].get(object_type)
    if doc is not None:
        if prop in doc.get("text", ()):
            return f"document:{doc['kind']}"
        return "fields_only" if prop in doc.get("fields_only", ()) else "unlisted"
    return "label" if _matches(config.get("labels", ()), prop) else "free"


def text_plan(config: Mapping[str, Any], types: Mapping[str, Mapping[str, Any]]
              ) -> dict[str, dict[str, list[str]]]:
    """Object type -> {"label": [...], "free": [...], "json": [...]}: the properties the
    record chunker reads, by the rule over the schema's property kinds (module docstring)."""
    rule = config["rule"]
    value_types = set(rule.get("value_types", ("text", "string")))
    served = set(rule.get("availability", ("served", "partial")))
    json_fields = config.get("json_fields") or {}
    plan: dict[str, dict[str, list[str]]] = {}
    for type_id, t in sorted(types.items()):
        if (t.get("availability") or {}).get("state") not in served:
            continue
        if type_id in config["documents"]:
            continue
        entry: dict[str, list[str]] = {"label": [], "free": [], "json": []}
        for p in t.get("properties") or []:
            name = p.get("name")
            if not isinstance(name, str):
                continue
            if p.get("value_type") == "json" and name in json_fields.get(type_id, ()):
                entry["json"].append(name)
            elif p.get("value_type") in value_types:
                how = classify(config, type_id, name)
                if how in ("label", "free"):
                    entry[how].append(name)
        # the ``fields`` block: properties listed by hand (or merged in by a declaration)
        # are read too, unless a deny or never_text rule keeps them out
        for name in (config.get("fields") or {}).get(type_id, ()):
            how = classify(config, type_id, name)
            if how.startswith("deny:") or name in entry["label"] + entry["free"] + entry["json"]:
                continue
            entry["label" if how == "label" else "free"].append(name)
        if any(entry.values()):
            plan[type_id] = entry
    return plan


def type_title(object_type: str) -> str:
    """A record type's readable name in a chunk title: a raw type's declared business label
    (``ontology.raw_business``), else the type id."""
    from ontology.raw_business import label_of
    return label_of(object_type) or object_type


def record_kind(audience_key: str) -> str:
    if audience_key.startswith("api:public."):
        return "portal_text"
    if family(audience_key) == "bc":
        return "bc_text"
    if family(audience_key) == "mc":
        return "mc_task"
    if family(audience_key) == "sharepoint-list":
        return "list_item"
    return "record_text"


WORDS_KIND = "record_words"   # record text that is not human language: words only


def wordy(text: str, min_chars: int, share: float) -> bool:
    stripped = "".join(text.split())
    if len(stripped) < min_chars:
        return False
    return sum(ch.isalpha() for ch in stripped) / len(stripped) >= share


def json_lines(value: Any, min_chars: int, share: float,
               config: Mapping[str, Any] | None = None) -> list[str]:
    """``Column: value`` lines of a JSON object's wordy string leaves (one level; a nested
    object, such as a person or lookup column, is never read: it may name a person past
    the identity rules)."""
    try:
        data = json.loads(value) if isinstance(value, str) else value
        if isinstance(data, str):
            data = json.loads(data)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for key, leaf in sorted(data.items()):
        if not isinstance(key, str) or not isinstance(leaf, str):
            continue
        if config is not None and denied(config, key.replace("_x0020_", "_")) is not None:
            continue   # an id, address, link or secret column: never text-indexed
        text = _LINK.sub("[link]", masked(" ".join(leaf.split())))
        if wordy(text, min_chars, share):
            out.append(f"{key.replace('_x0020_', ' ').replace('_', ' ').strip()}: {text}")
    return out


class RecordChunker(ProjectionChunker):
    """Text of business records by the schema rule (module docstring)."""

    def __init__(self, *args: Any, config: Mapping[str, Any] | None = None,
                 types: Mapping[str, Mapping[str, Any]] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.config = dict(config or load_config())
        self.plan = text_plan(self.config, schema_types() if types is None else types)
        self.document_families = frozenset(self.config.get("document_families") or ())
        self.words_only = dict(self.config.get("words_only") or {})
        # raw relation rows (Rel*): found by their words, never embedded
        self.raw_types = re.compile(str(self.words_only.get("raw_types") or r"(?!)"))

    def record_chunks(self, kinds: Sequence[str] | None = None, limit: int | None = None,
                      shard: tuple[int, int] | None = None,
                      objects: AbstractSet[str] | None = None) -> Iterator[Chunk]:
        """Record chunks of ``kinds``. ``shard`` (i, n): only the records whose object id
        hashes to i of n; ``objects``: only these records are read (a delta layer's own),
        the others are passed over unread."""
        produced = 0
        min_chars = int(self.config.get("min_chars", 20))
        label_min = int(self.config.get("label_min_chars", 3))
        share = float(self.config.get("min_letter_share", 0.4))
        by_type: dict[str, list[Any]] | None = None
        if objects is not None:
            # a layer's slice: only its own objects are read (by key, in id order), never
            # every record of each type once per slice (a 700k-object layer read 35 times)
            by_type = {}
            wanted = sorted(objects)
            for start in range(0, len(wanted), 500):
                for row in self.conn.execute(
                        "SELECT object_id, object_type, audience_key, "
                        "missing_from_source_since AS missing FROM objects WHERE object_id "
                        "IN (SELECT value FROM json_each(?)) ORDER BY object_id",
                        (json.dumps(wanted[start:start + 500]),)):
                    by_type.setdefault(row["object_type"], []).append(row)
        for object_type, props in sorted(self.plan.items()):
            after = ""
            mine = None if by_type is None else by_type.get(object_type, [])
            while True:
                if mine is not None:
                    # a layer's slice: its own records of the type, read once by key
                    rows, mine = mine[:BATCH], mine[BATCH:]
                    if not rows:
                        break
                else:
                    rows = self.conn.execute(
                        "SELECT object_id, audience_key, missing_from_source_since AS missing "
                        "FROM objects WHERE object_type = ? AND object_id > ? ORDER BY "
                        "object_id LIMIT ?", (object_type, after, BATCH)).fetchall()
                    if not rows:
                        break
                    after = rows[-1]["object_id"]
                if shard is not None:
                    rows = [r for r in rows if zlib.crc32(r["object_id"].encode("utf-8"))
                            % shard[1] == shard[0]]
                if not rows:
                    continue
                for chunk in self._records(rows, props, (label_min, min_chars), share):
                    if kinds is not None and chunk.kind not in kinds:
                        continue
                    self.counts.chunks[chunk.kind] += 1
                    yield chunk
                    produced += 1
                    if limit is not None and produced >= limit:
                        return

    def _records(self, rows: Sequence[Any], props: Mapping[str, list[str]],
                 mins: tuple[int, int], share: float) -> Iterator[Chunk]:
        live = []
        for r in rows:
            if r["missing"] is not None:
                self.counts.skip("missing_from_source")
            elif family(r["audience_key"]) in self.document_families:
                # read out of a mail or SharePoint document: that document's chunks hold
                # the text, under the document's own (re-marked) markings
                self.counts.skip("document_family")
            elif not valid_key(r["audience_key"]):
                self.counts.skip("audience_key_unknown")
            elif not self.exclusion.unit_clear(r["audience_key"]):
                self.counts.skip("people_exclusion:record")
            else:
                live.append(r)
        if not live:
            return
        ids = [r["object_id"] for r in live]
        labels = set(props.get("label", ()))
        json_props = set(props.get("json", ()))
        wanted = sorted(labels | set(props.get("free", ())) | json_props)
        latest = self._latest_markings(ids)
        current = {row["object_id"]: row["markings"] for row in self.conn.execute(
            "SELECT object_id, markings FROM object_versions WHERE superseded_at IS NULL "
            "AND object_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))}
        texts: dict[str, list[tuple[Any, str]]] = {}
        for row in self.conn.execute(
                "SELECT object_id, property, value, markings, l1_handle, field_path, value_type "
                "FROM object_values WHERE object_id IN (SELECT value FROM json_each(?)) "
                "AND superseded_at IS NULL AND +property IN (SELECT value FROM json_each(?)) "
                "ORDER BY object_id, property", (json.dumps(ids), json.dumps(wanted))):
            if row["property"] in json_props:
                lines = json_lines(row["value"], mins[0], share, self.config)
                if lines:
                    texts.setdefault(row["object_id"], []).append((row, "\n".join(lines)))
            elif isinstance(row["value"], str) and wordy(
                    value := masked(row["value"]),
                    mins[0] if row["property"] in labels else mins[1], share):
                texts.setdefault(row["object_id"], []).append((row, value))
        if not texts:
            return
        idents = self._identifiers(list(texts))
        for record in live:
            oid = record["object_id"]
            if oid not in texts:
                continue
            audience = record["audience_key"]
            if oid not in current:
                self.counts.skip("no_current_version")
                continue
            own_kind = record_kind(audience)
            self.counts.documents[own_kind] += 1
            for row, value in texts[oid]:
                marks = parse_markings(row["markings"], current[oid], latest.get(oid, "[]"))
                if marks is None:
                    self.counts.skip("markings_unknown")
                    continue
                own = [(k, v) for k, v, m in idents.get(oid, ())
                       if (held := parse_markings(m)) is not None and set(held) <= set(marks)]
                id_labels = [hd.id_label(k, v) for k, v in own][:4]
                keys = [f"{k}:{v}" for k, v in own]
                type_id = oid.split(":", 1)[0]
                type_name = type_title(type_id)   # its readable name, for the title only
                title = f"{type_name} {own[0][1]}" if own else type_name
                prop = row["property"].replace("_", " ")
                about = f"the {prop} of {title}"
                short = row["property"] in labels or row["property"] in json_props
                kind = WORDS_KIND if self.raw_types.match(type_id) else (
                    own_kind if own_kind == "list_item" or human(
                        value, short, self.words_only) else WORDS_KIND)
                for index, piece in enumerate(pack(normalise(value))):
                    if len(piece) < MIN_BODY:
                        if not short:
                            self.counts.skip(f"too_short:{kind}")
                            continue
                        # a name or title: shorter than a passage, said with what it is
                        piece = f"{type_name} {prop}: {piece}"
                    yield make_chunk(
                        kind=kind, parts=(audience, row["l1_handle"], row["field_path"],
                                          str(index)),
                        source_handle=row["l1_handle"], object_id=oid, title=title,
                        author_role=None, occurred_at=None,
                        header=hd.build(kind=kind, title=title, date=None, author_role=None,
                                        ids=id_labels, about=about,
                                        where=f"field {row['field_path']}"),
                        body=piece, linked_ids=keys, audience_key=audience, markings=marks)


__all__ = ["CONFIG", "DENY_CATEGORIES", "WORDS_KIND", "denied", "human", "masked", "RecordChunker", "classify", "json_lines",
           "load_config", "record_kind", "schema_types", "text_plan", "wordy"]
