"""Read-time guard on the same-person links the people step got wrong.

The served store holds two kinds of identity link that say more than the evidence does:

* ``Contact.same_person_as`` from a Business Central contact whose ``contact_type`` is not
  ``Person`` (a Company card, or a missing, blank or unknown type): a company's address
  book entry is not one person, yet the step tied it to the Person holding its address.
* ``Person.same_person_as`` firm (``corroborated``, ``verified``) on a plus-address match
  (reason feature ``plus_address``): ``a+tag@d`` and ``a@d`` reach one mailbox, which is a
  lead at most, never the same person.

The corrected step stops writing
them and the person-match migration rewrites the stored rows. Until that runs, every
reader of the served store is kept from seeing them by rewriting its SQL: each read of the
relation ``links`` or ``link_rows`` (after ``FROM`` or ``JOIN``) reads the same relation
less the rows above (:func:`guard`). One rewrite at the connection
(``sqlite_compat.PgCompatConnection(link_guard=True)``, which ``PgStoreReader`` and the
proactive and watcher readers open) covers every consumer: record pages (``plx.around``
link groups, "Same person as", "Bc contacts"), ``plx.identity`` and ``plx.investigate``,
process, aggregate and traverse, the search joins, Ask facts and the autopilots.

Fail closed: a ``Contact.same_person_as`` row is shown only when its contact end is a
Contact whose current ``contact_type`` is ``Person`` and no end is any other Contact; a
row of an unexpected shape is hidden. Writers never open a guarded connection: a step
that could not see its own rows would write them again. Every statement a guarded
connection runs whose head is SELECT, WITH or VALUES is rewritten, an item-gated read
(``CASE ... END``) too (``PgCompatConnection._guarded``). No stored row changes, so no digest moves and the guard is
removed once the migration has run.
"""

from __future__ import annotations

import functools
import re

CONTACT_LINK = "Contact.same_person_as"
PERSON_LINK = "Person.same_person_as"
CONTACT_PREFIX = "Contact:"
PERSON_TYPE = "person"
# the tiers that read as one person (a lead or a contradicted row says it is not settled)
FIRM_TIERS = ("corroborated", "verified")
# the reason feature of a plus-address match (no other reason of the type names it)
PLUS_FEATURE = '"plus_address"'
RELATIONS = ("links", "link_rows")
_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<str>'(?:[^']|'')*')
  | (?P<qid>"(?:[^"]|"")*")
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?)
  | (?P<other>.)
""", re.VERBOSE | re.DOTALL)
# a word after the relation that is not its alias (the alias is then the relation's name)
_CLAUSE = frozenset("""
    where join inner left right full cross natural on using group order limit offset union
    except intersect having window indexed not as
""".split())
_ALIAS = "plx_pg"


def _person_contact(column: str) -> str:
    """SQL: the object in ``column`` is a Contact whose current ``contact_type`` is Person."""
    return (f"EXISTS (SELECT 1 FROM object_values plx_pv WHERE plx_pv.object_id = {column} "
            f"AND plx_pv.property = 'contact_type' AND plx_pv.superseded_at IS NULL "
            f"AND lower(trim(plx_pv.value)) = '{PERSON_TYPE}')")


def hidden(alias: str, relation: str) -> str:
    """SQL: the row ``alias`` of ``relation`` is a link the guard hides. ``CASE`` keeps the
    lookups to the two link types (every other row is decided by its type alone)."""
    if relation == "links":
        plus = f"instr({alias}.reason, '{PLUS_FEATURE}') > 0"
    else:   # link_rows: the reason may be packed; read it from the links view
        plus = (f"EXISTS (SELECT 1 FROM links plx_pr WHERE plx_pr.link_id = {alias}.link_id "
                f"AND instr(plx_pr.reason, '{PLUS_FEATURE}') > 0)")
    firm = ", ".join(f"'{t}'" for t in FIRM_TIERS)
    return (f"(CASE WHEN {alias}.link_type = '{CONTACT_LINK}' THEN "
            f"(substr({alias}.from_object, 1, {len(CONTACT_PREFIX)}) <> '{CONTACT_PREFIX}' "
            f"OR NOT {_person_contact(alias + '.from_object')} "
            f"OR (substr({alias}.to_object, 1, {len(CONTACT_PREFIX)}) = '{CONTACT_PREFIX}' "
            f"AND NOT {_person_contact(alias + '.to_object')})) "
            f"WHEN {alias}.link_type = '{PERSON_LINK}' AND {alias}.tier IN ({firm}) "
            f"THEN {plus} ELSE 1 = 0 END)")


@functools.lru_cache(maxsize=2)
def _relation(relation: str) -> str:
    """The relation less the hidden links, as two branches of a ``UNION ALL``: every
    other link type read as stored (Postgres flattens a simple ``UNION ALL`` and pushes
    the caller's conditions into each branch, so a scan of another type keeps its
    index-only scan; one filtered relation read every column and every type's rows
    through the CASE, a sequential scan of the largest partition, 11-16), then the two
    same-person types less the hidden rows. ``link_type`` is NOT NULL, so the branches
    split every row."""
    types = f"('{CONTACT_LINK}', '{PERSON_LINK}')"
    return (f"(SELECT * FROM {relation} {_ALIAS} WHERE {_ALIAS}.link_type NOT IN {types} "
            f"UNION ALL SELECT * FROM {relation} {_ALIAS} WHERE {_ALIAS}.link_type IN "
            f"{types} AND NOT {hidden(_ALIAS, relation)})")


def _significant(tokens: list[tuple[str, str]], i: int, step: int) -> int:
    while 0 <= i < len(tokens) and tokens[i][0] in ("ws", "comment"):
        i += step
    return i


# the words that end a FROM list at its depth (``ON`` / ``USING`` do not: a list may go
# on after a join's condition)
_FROM_ENDS = frozenset("""
    where group order limit offset having window union except intersect select returning
    set values
""".split())


@functools.lru_cache(maxsize=4096)
def guard(sql: str) -> str:
    """``sql`` with each read of ``links`` / ``link_rows`` (a bare name after ``FROM``,
    ``JOIN`` or a comma of a FROM list) replaced by that relation less the hidden links,
    under its own alias (or its name when it had none). Anything else is left as
    written."""
    lowered = sql.lower()
    if "links" not in lowered and "link_rows" not in lowered:
        return sql
    tokens = [(m.lastgroup or "other", m.group()) for m in _TOKEN.finditer(sql)]
    out: list[str] = []
    changed = False
    numbered: set[str] = set()   # the names a guarded link_rows is read by
    in_from = [False]   # per parenthesis depth: inside a FROM list
    for i, (kind, text) in enumerate(tokens):
        name = text.lower()
        if text == "(":
            in_from.append(False)
        elif text == ")" and len(in_from) > 1:
            in_from.pop()
        elif kind == "word" and name in ("from", "join"):
            in_from[-1] = True
        elif kind == "word" and name in _FROM_ENDS:
            in_from[-1] = False
        if kind != "word" or name not in RELATIONS:
            out.append(text)
            continue
        before = _significant(tokens, i - 1, -1)
        after = _significant(tokens, i + 1, 1)
        follows = tokens[after] if after < len(tokens) else ("", "")
        lead = tokens[before][1].lower() if before >= 0 else ""
        if not (lead in ("from", "join") or lead == "," and in_from[-1]) \
                or follows[1] == "(":
            out.append(text)
            continue
        piece = _relation(name)
        named = follows[0] == "word" and follows[1].lower() not in _CLAUSE
        if follows[1].lower() == "as":
            alias = _significant(tokens, after + 1, 1)
            named = alias < len(tokens) and tokens[alias][0] == "word"
            follows = tokens[alias] if named else follows
        if not named and follows[1].lower() != "as":
            piece += f" AS {name}"
        out.append(piece)   # one entry per token: ``out[i]`` is token i
        if name == "link_rows":
            numbered.add((follows[1] if named else name).lower())
        changed = True
    if not changed:
        return sql
    # ``x.rowid`` of a guarded link_rows: its row number column, which the subquery keeps
    # (``sqlite_compat.ROWID``; the translation no longer sees the table behind ``x``)
    for i, (kind, text) in enumerate(tokens):
        if kind == "word" and "." in text:
            head, tail = text.split(".", 1)
            if tail.lower() == "rowid" and head.lower() in numbered:
                out[i] = f"{head}.row_no"
    return "".join(out)
