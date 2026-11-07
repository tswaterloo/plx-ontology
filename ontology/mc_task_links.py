"""Mission Control tasks linked to the business records their text names
(``MCTask.mentions_supplier`` / ``_customer`` / ``_product`` and the identifier mention
link types the schema generates for MCTask, ``ontology/schema.py`` MC_MENTION_LINK_RULES).

Two ``post_project`` steps (``operating/build_steps.py`` ``task_ids`` and
``task_names``; steps run in name order, so after ``mentions`` and ``reviews``), each
self-contained: it reads the
current store generation only, removes the rows it wrote before (its link id prefix) and
writes its rows again, so it can run in a full build or alone on a store in place.

``task_names`` (prefix ``NAME_PREFIX``): a **lead** from a task to each supplier,
customer or product its title or description names by name.

* Inputs: the current ``title`` and ``description`` of every ``MCTask``; the current
  ``name`` of every ``Supplier``, ``Customer`` and ``Product``; the own-party names
  (``ontology/schema/document_fields.json`` ``own_parties``); a person's rejections
  (``contradicted`` links of a pair).
* Mentions: the general resolver's name mentions of each field
  (``operating/entity_resolution.mentions``: runs of capitalised words that are not
  scaffolding or calendar words), scored by its name rules (``_name_score``: equal name
  0.95, the name beginning with the mention 0.9 for a party, a name differing only by a
  plural ``IN_ORDER_SCORE`` 0.86 and only when it is the one such name and nothing is
  named more closely; a likeness names nothing).
* A stored link needs more than a question-time lead, so a mention links only when:
  - it is a name, not a word of the corpus: at least one of its words (three letters or
    more, not an own-party word) is written capitalised more often than in lower case in
    the tasks' own text, and seldom in lower case (``_common``: ``Test``, ``Delete``,
    ``Bulk`` are words; ``Dandylion`` is a name); the whole mention is four letters or
    more;
  - the record's name is the mention (legal words aside), or the mention plus a
    qualifier (a parenthesis, ``#``, comma, slash, colon or a spaced dash:
    ``Acme (Packaging)``, ``Acme - Store #12``), or the one plural-only difference; a
    name that goes on with other words (``Acme Logistics`` for ``Acme``) is another
    party and never linked; when some record's name is the mention exactly, only those
    are linked;
  - it does not start with the first name of one of our own people: an MC principal
    (a task's ``assignee``, ``reporter`` or ``accountable_owner``) or the mailbox of an
    own-domain address the tasks write (``jesse@`` makes ``Jesse Vieira`` a colleague,
    whatever expense vendor or staff-purchase customer carries the name);
  - it names at most ``MAX_RECORDS`` records (a chain's hundreds of locations are no
    one record).

``task_ids`` (prefix ``FIRM_PREFIX``): a **corroborated** link beside an identifier
mention lead of the mentions step (``ontology/mentions.py``, an ``MCTask.mentions_<type>``
link) when the identifier is exact and unique:

* exact: the span the task wrote is the identifier in its normalised form (``AO-26222D``,
  ``PO100192``; never a number a label assembles, ``PO 352``) and a whole token (no letter,
  digit, ``+``, ``*`` or ellipsis touching it: ``PO100000+`` names a series, ``PO10…`` a
  prefix) holding at least ``MIN_DIGITS`` digits (``PO10`` is a filter or a count as
  often as an order);
* unique: the candidates of the mention of the identifier kind's own object type
  (``ontology/schema/identifiers.json`` ``object_type``) are one record: one object, or
  objects each two of which a confirming ``same_record`` link joins (a Portal order and
  its FileMaker twin). Candidates of other types carrying the number as a reference (an
  invoice's order number, a mapping row) neither block nor get a firm link.

Inputs: the current MCTask identifier mention leads, the current task text they cite,
the candidates' confirming ``same_record`` links. The lead stays; the firm link cites it.

Stdlib only; reads ``operating.entity_resolution`` for the name rules (as
``ontology/document_fields.py`` reads ``operating``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
import json
import re
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology import mentions

TASK_TYPE = "MCTask"
TEXT_PROPERTIES = ("title", "description")
# MC principals are our own people: their names, and the mailbox names of own-domain
# addresses the tasks write, are first names of our own staff.
PEOPLE_PROPERTIES = ("assignee", "reporter", "accountable_owner")
TARGET_TYPES = ("Supplier", "Customer", "Product")
NAME_PREFIX = "mcname:"
FIRM_PREFIX = "crb-mcid:"
LEAD = "lead"
FIRM = "corroborated"
CONFIRMING = ("verified", "reviewed", "corroborated")
BATCH = 2000
MIN_LETTERS = 4
# A word written in lower case more than this (plus a quarter of its capitalised uses)
# in the tasks' own text is a word of the corpus, not a name.
COMMON_LOWER = 3
_QUALIFIER = re.compile(r"\s*(?:[(#,/:]|-\s)")
_TOKEN_TOUCH = re.compile(r"[A-Za-z0-9+*…]")
# An identifier with fewer digits than this is a prefix, a filter or a count as often as a
# record (``PO10``), never firm.
MIN_DIGITS = 3


def _end(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _digest(*parts: Any) -> str:
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"))
                  .encode("utf-8")).hexdigest()[:32]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _q(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    return mentions._query(conn, sql, list(params)).fetchall()


_CELLS = ("SELECT v.object_id, v.l1_handle, v.markings AS vmarks, k.markings AS kmarks, "
          "coalesce(s.digest, v.digest) AS digest, coalesce(s.lineage, v.lineage) AS lineage, "
          "s.recorded_at, f.property, f.field_path, CASE WHEN c.field IS NULL THEN "
          "json_extract(c.value, lf.path) ELSE c.value END AS value FROM value_layout_fields lf "
          "JOIN value_cells c ON c.layout = lf.layout JOIN value_sources s ON s.src = c.src "
          "JOIN object_versions v ON v.rowid = s.ver JOIN value_kinds k ON k.kind = lf.kind "
          "JOIN value_fields f ON f.field = lf.field WHERE lf.field IN ({fields}) AND "
          "s.superseded_at IS NULL AND v.superseded_at IS NULL")


def current_values(conn: Any, props: Iterable[str], types: Sequence[str]) -> Iterator[Any]:
    """Current text value rows of ``props`` of objects of ``types``."""
    props = list(props)
    fields = [r[0] for r in _q(conn, "SELECT field FROM value_fields WHERE property IN "
                                     f"({','.join('?' * len(props))})", props)]
    if not fields:
        return
    prefixes = tuple(t + ":" for t in types)
    for row in mentions._query(conn, _CELLS.format(fields=",".join(map(str, fields))), []):
        if isinstance(row["value"], str) and row["value"].strip() \
                and row["object_id"].startswith(prefixes):
            yield row


def _cite(row: Any) -> dict[str, Any]:
    return {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
            "digest": row["digest"], "lineage": row["lineage"]}


def _rejected(conn: Any, task: str) -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in _q(conn, "SELECT to_object, link_type FROM link_rows WHERE "
                                           "from_object = ? AND tier = 'contradicted'", [task])}


def _versions(conn: Any, oids: Iterable[str]) -> dict[str, Any]:
    oids = sorted(set(oids))
    out: dict[str, Any] = {}
    for i in range(0, len(oids), 500):
        part = oids[i:i + 500]
        for r in _q(conn, "SELECT object_id, l1_handle, markings, recorded_at FROM "
                          "object_versions WHERE superseded_at IS NULL AND object_id IN "
                          f"({','.join('?' * len(part))})", part):
            out[r["object_id"]] = r
    return out


def link_type_of(target_type: str) -> str:
    from ontology.schema import _snake_case
    return f"{TASK_TYPE}.mentions_{_snake_case(target_type)}"


def _declared(links: Mapping[str, Any], types: Iterable[str]) -> dict[str, str]:
    return {t: link_type_of(t) for t in types
            if any(b.get("kind") == mentions.BINDING_KIND
                   for b in (links.get(link_type_of(t)) or {}).get("bindings", ()))}


# ------------------------------------------------------------------ names (leads)

class _Entry:
    """One name of the index, in the shape ``entity_resolution._name_score`` reads."""
    __slots__ = ("object_id", "type", "name", "normalized", "alias", "strict", "matched",
                 "row")

    def __init__(self, row: Any) -> None:
        self.object_id, self.name, self.row = row["object_id"], row["value"], row
        self.type = row["object_id"].split(":", 1)[0].lower()
        self.normalized, self.alias, self.strict, self.matched = None, (), 0.0, ""


class NameIndex:
    """The current names of the target types, by each word of their key."""

    def __init__(self, rows: Iterable[Any]) -> None:
        from operating import entity_resolution as er
        self.by_word: dict[str, list[_Entry]] = defaultdict(list)
        for row in rows:
            entry = _Entry(row)
            for word in set(er._name_key(entry.name).split()):
                if len(word) >= 3:
                    self.by_word[word].append(entry)

    def candidates(self, key: str) -> list[_Entry]:
        seen: dict[tuple[str, str], _Entry] = {}
        for word in key.split():
            for entry in self.by_word.get(word, ()):
                seen.setdefault((entry.object_id, entry.name), entry)
        return list(seen.values())


class Corpus:
    """How the tasks' own text writes each word: capitalised or in lower case."""

    def __init__(self, texts: Iterable[str], own_words: Iterable[str],
                 people: Iterable[str] = ()) -> None:
        self.people = frozenset(people)
        self.lower: Counter[str] = Counter()
        self.capital: Counter[str] = Counter()
        for text in texts:
            for token in re.findall(r"\b[A-Za-z][A-Za-z'-]{2,}\b", text or ""):
                (self.lower if token[0].islower() else self.capital)[token.lower()] += 1
        self.own = frozenset(own_words)

    def is_name(self, key: str) -> bool:
        """Whether a mention's key is a name (module docstring), not words of the text."""
        if len(key.replace(" ", "")) < MIN_LETTERS or key.split()[0] in self.people:
            return False
        return any(len(w) >= 3 and w not in self.own and self.lower[w] < self.capital[w]
                   and self.lower[w] < COMMON_LOWER + self.capital[w] // 4
                   for w in key.split())


def name_level(mention: str, name: str) -> str | None:
    """``exact`` when the name is the mention (legal words aside), ``qualified`` when it
    is the mention followed by a qualifier, else None."""
    from operating import entity_resolution as er
    if er._name_key(name) == er._name_key(mention):
        return "exact"
    folded, asked = er._fold(name), er._fold(mention)
    if not asked or not folded.startswith(asked):
        return None
    words = len(mention.split())
    head = re.match(r"\s*(?:\S+\s+){%d}\S+?(?=[\s(\-#,/_.:]|$)" % (words - 1), name.strip())
    rest = name.strip()[head.end():] if head else ""
    return "qualified" if _QUALIFIER.match(rest) else None


def judge_mention(index: NameIndex, corpus: Corpus, text: str
                  ) -> tuple[str, list[tuple[_Entry, str]]]:
    """(outcome, [(entry, level)]) of one name mention (module docstring)."""
    from operating import entity_resolution as er
    key = er._name_key(text)
    if not key or not corpus.is_name(key):
        return "not_a_name", []
    scored = []
    for entry in index.candidates(key):
        got = er._name_score(entry, key, text, loose=False)
        if got is not None:
            scored.append((entry, got[0]))
    plural = {er._name_key(e.name) for e, s in scored if s == er.IN_ORDER_SCORE}
    closer = any(s > er.IN_ORDER_SCORE for _e, s in scored)
    levels = []
    for entry, score in scored:
        if score == er.IN_ORDER_SCORE:
            if not closer and len(plural) == 1:
                levels.append((entry, "plural"))
            continue
        level = name_level(text, entry.name) if score >= er.MATCH_AT else None
        if level is not None:
            levels.append((entry, level))
    if any(level == "exact" for _e, level in levels):
        levels = [(e, level) for e, level in levels if level == "exact"]
    if not levels:
        return "no_record", []
    if len({e.object_id for e, _level in levels}) > _max_records():
        return "too_many_records", []
    return "linked", levels


def _max_records() -> int:
    from operating.entity_resolution import MAX_RECORDS
    return MAX_RECORDS


def own_words() -> set[str]:
    from operating import entity_resolution as er
    from ontology.document_fields import profiles
    return {w for n in profiles()["own_parties"]["names"] if "." not in n
            for w in er._name_key(n).split()}


def own_people(values: Iterable[str], domains: Sequence[str]) -> set[str]:
    """First names of our own people: MC principal names (a bare user name or an
    own-domain address) and the mailboxes of own-domain addresses written in task text."""
    own = "|".join(re.escape(d) for d in domains)
    found = re.compile(rf"([A-Za-z][A-Za-z0-9._%+-]*)@(?:{own})\b", re.I) if own else None
    out: set[str] = set()
    for value in values:
        if found is not None:
            out |= {m.lower().split(".")[0] for m in found.findall(value)}
        if re.fullmatch(r"[A-Za-z]{3,}", value.strip()):
            out.add(value.strip().lower())
    return out


def own_domains() -> list[str]:
    from ontology.document_fields import profiles
    return sorted(n.lower() for n in profiles()["own_parties"]["names"] if "." in n)


def name_links(conn: Any, declared: Mapping[str, str]) -> tuple[list[dict[str, Any]], Counter]:
    """The lead rows of every current task (module docstring) and the outcome counts."""
    from operating import entity_resolution as er
    counts: Counter[str] = Counter()
    tasks: dict[str, list[Any]] = defaultdict(list)
    for row in current_values(conn, TEXT_PROPERTIES, (TASK_TYPE,)):
        tasks[row["object_id"]].append(row)
    if not tasks or not declared:
        return [], counts
    index = NameIndex(current_values(conn, ["name"], tuple(declared)))
    people = own_people([r["value"] for rows in tasks.values() for r in rows]
                        + [r["value"] for r in current_values(conn, PEOPLE_PROPERTIES,
                                                              (TASK_TYPE,))], own_domains())
    corpus = Corpus((r["value"] for rows in tasks.values() for r in rows), own_words(), people)
    found: list[tuple[Any, str, int, _Entry, str]] = []
    for task in sorted(tasks):
        for row in sorted(tasks[task], key=lambda r: r["field_path"]):
            for mention in er.mentions(row["value"]):
                if mention["kind"] != "name":
                    continue
                outcome, levels = judge_mention(index, corpus, mention["text"])
                counts[outcome] += 1
                start = row["value"].find(mention["text"])
                found += [(row, mention["text"], start, e, level) for e, level in levels]
    versions = _versions(conn, {e.object_id for *_r, e, _l in found})
    out: dict[str, dict[str, Any]] = {}
    rejected: dict[str, set[tuple[str, str]]] = {}
    for row, text, start, entry, level in found:
        link_type = declared[entry.object_id.split(":", 1)[0]]
        version = versions.get(entry.object_id)
        if version is None:
            continue
        task = row["object_id"]
        if task not in rejected:
            rejected[task] = _rejected(conn, task)
        if (entry.object_id, link_type) in rejected[task]:
            counts["rejected"] += 1
            continue
        link_id = NAME_PREFIX + _digest(task, row["l1_handle"], link_type, entry.object_id)
        if link_id in out:
            continue
        reason = [{"feature": "name_mention", "agrees": True, "level": level,
                   "span": [start, start + len(text)] if start >= 0 else None,
                   "citations": [_cite(row)]},
                  {"feature": "record_name", "agrees": True, "citations": [_cite(entry.row)]}]
        why_not = [{"feature": "name_only", "agrees": None,
                    "detail": "the task names the record by name only: a lead"}]
        out[link_id] = {
            "link_id": link_id, "link_type": link_type, "from_object": task,
            "to_object": entry.object_id, "tier": LEAD, "binding_kind": mentions.BINDING_KIND,
            "link_contract": None, "reason": _canon(reason), "why_not": _canon(why_not),
            "evidence_handles": _canon(list(dict.fromkeys(
                [row["l1_handle"], entry.row["l1_handle"], version["l1_handle"]]))),
            "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
            "markings": mentions._marks(row["vmarks"], row["kmarks"], entry.row["vmarks"],
                                        entry.row["kmarks"], version["markings"]),
            "recorded_at": max(row["recorded_at"], entry.row["recorded_at"],
                               version["recorded_at"]),
            "superseded_at": None}
    counts["links"] = len(out)
    return [out[k] for k in sorted(out)], counts


# ------------------------------------------------------- identifiers (firm beside leads)

def kind_types() -> dict[str, str]:
    """identifier kind -> the object type it is the identifier of."""
    from ontology import identifiers as ids
    return {k: ids.kind_spec(k).get("object_type") for k in ids.kinds()}


def exact_token(text: str, span: Sequence[int], normalized: str) -> bool:
    """Whether ``text[span]`` is ``normalized`` as written and a whole token."""
    start, end = span
    if not (0 <= start < end <= len(text)):
        return False
    if text[start:end].strip().casefold() != str(normalized).casefold():
        return False
    if sum(ch.isdigit() for ch in str(normalized)) < MIN_DIGITS:
        return False
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if after == "." and text[end + 1:end + 2].isalnum():
        return False
    return not _TOKEN_TOUCH.match(before) and not _TOKEN_TOUCH.match(after)


def one_record(conn: Any, oids: Sequence[str]) -> bool:
    """Whether every two of ``oids`` are joined by a confirming same_record link."""
    oids = sorted(set(oids))
    if len(oids) < 2:
        return bool(oids)
    link = oids[0].split(":", 1)[0] + ".same_record_as"
    for i, a in enumerate(oids):
        joined = {r[0] for r in _q(
            conn, "SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? AND "
                  "tier IN ('verified', 'reviewed', 'corroborated') AND superseded_at IS NULL "
                  "UNION SELECT from_object FROM link_rows WHERE to_object = ? AND link_type = ? "
                  "AND tier IN ('verified', 'reviewed', 'corroborated') AND superseded_at IS NULL",
            [a, link, a, link])}
        if not set(oids[i + 1:]) <= joined:
            return False
    return True


def task_leads(conn: Any) -> list[Any]:
    """The current identifier mention leads from MC tasks (the mentions step's rows)."""
    return _q(conn, "SELECT * FROM links WHERE from_object >= ? AND from_object < ? AND "
                    "binding_kind = 'mention' AND tier = 'lead' AND superseded_at IS NULL AND "
                    "link_id >= ? AND link_id < ?",
              [TASK_TYPE + ":", _end(TASK_TYPE + ":"), "mention:", _end("mention:")])


def firm_links(conn: Any) -> tuple[list[dict[str, Any]], Counter]:
    """The corroborated rows beside the exact, unique identifier leads (module doc)."""
    counts: Counter[str] = Counter()
    owner = kind_types()
    texts: dict[tuple[str, str], str] = {}
    for row in current_values(conn, TEXT_PROPERTIES, (TASK_TYPE,)):
        texts[(row["l1_handle"], row["field_path"])] = row["value"]
    groups: dict[tuple[str, str], list[tuple[Any, Mapping[str, Any]]]] = defaultdict(list)
    for lead in task_leads(conn):
        reason = json.loads(lead["reason"] or "[]")
        mention = next((f for f in reason if isinstance(f, dict)
                        and f.get("feature") == "identifier_mention"), None)
        if mention is None:
            continue
        groups[(lead["from_object"], mention.get("mention_group"))].append((lead, mention))
    out = []
    for (_task, _group), members in sorted(groups.items()):
        lead, mention = members[0]
        cite = (mention.get("citations") or [{}])[0]
        text = texts.get((cite.get("l1_handle"), cite.get("field_path")))
        if text is None:
            counts["text_not_current"] += 1
            continue
        if not exact_token(text, mention.get("span") or (0, 0), mention.get("normalized", "")):
            counts["not_exact"] += 1
            continue
        own_type = owner.get(mention.get("identifier_kind"))
        own = [(l, m) for l, m in members if l["to_object"].split(":", 1)[0] == own_type]
        if not own:
            counts["no_record_of_kind"] += 1
            continue
        if not one_record(conn, [l["to_object"] for l, _m in own]):
            counts["several_records"] += 1
            continue
        counts["firm"] += 1
        for l, _m in own:
            out.append(_firm_row(l))
    return out, counts


def _firm_row(lead: Any) -> dict[str, Any]:
    reason = [*json.loads(lead["reason"] or "[]"),
              {"feature": "exact_identifier", "agrees": True},
              {"feature": "unique_record", "agrees": True},
              {"feature": "corroborates", "agrees": True, "binding_kind": "mention",
               "link_id": lead["link_id"]}]
    return {"link_id": FIRM_PREFIX + _digest(lead["link_id"]),
            "link_type": lead["link_type"], "from_object": lead["from_object"],
            "to_object": lead["to_object"], "tier": FIRM, "binding_kind": mentions.BINDING_KIND,
            "link_contract": None, "reason": _canon(reason), "why_not": "[]",
            "evidence_handles": lead["evidence_handles"], "reviewed_by": None,
            "reviewed_at": None, "review_action_id": None, "markings": lead["markings"],
            "recorded_at": lead["recorded_at"], "superseded_at": None}


# ------------------------------------------------------------------------ the steps

def _write(builder: Any, prefix: str, rows: list[dict[str, Any]]) -> None:
    builder.connection.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                               (prefix, _end(prefix)))
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])


def task_names_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``task_names`` step (module docstring)."""
    declared = _declared(getattr(projector, "links", {}) or {}, TARGET_TYPES)
    rows, counts = name_links(builder.connection, declared)
    _write(builder, NAME_PREFIX, rows)
    return {"counts": dict(counts), "link_tiers": {LEAD: len(rows)} if rows else {}}


def task_ids_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``task_ids`` step (module docstring); after ``mentions`` and ``reviews``."""
    rows, counts = firm_links(builder.connection)
    rejected = set()
    for task in sorted({r["from_object"] for r in rows}):
        rejected |= {(task, *pair) for pair in _rejected(builder.connection, task)}
    kept = [r for r in rows if (r["from_object"], r["to_object"], r["link_type"]) not in rejected]
    counts["rejected"] = len(rows) - len(kept)
    _write(builder, FIRM_PREFIX, kept)
    return {"counts": dict(counts), "link_tiers": {FIRM: len(kept)} if kept else {}}


__all__ = ["Corpus", "FIRM_PREFIX", "NAME_PREFIX", "NameIndex", "exact_token", "firm_links",
           "judge_mention", "name_level", "name_links", "one_record", "task_ids_step",
           "task_names_step"]
