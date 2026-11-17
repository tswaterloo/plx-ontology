"""A case's claim items and the wording checks: typed claims, the wording verdict, and the
prose screen over schema names, fields and sentence problems."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from .cases import GAP_STATES

from .claim_checks_grammar import (
    _ABBREVIATIONS, ADD_DAYS_OP, _DIGIT_NEXT, _FIELD, _IE_EG, KNOWN_ID_PREFIXES, NUMBER,
    _PLAIN_NUMBER, QUANTITY_ROLES, _RECORD_NOUNS, _ROLE_ALIASES, ROLES, UNTYPED_CALCULATION,
)
from .claim_checks_parse import number_value, _plain, to_decimal
from .claim_checks_cells import _fold
from .claim_checks_text import (
    _figure_currencies, _negated, _page_locators, prose_numbers, same_currency, _spans,
    _stated_exactly, _strip,
)


def _claim_key(claim: Mapping[str, Any]) -> str:
    """What a claim reads, so one claim made in two items is one claim."""
    return json.dumps({key: claim[key] for key in ("citation", "field", "calculation", "op",
                                                   "of", "value") if key in claim},
                      sort_keys=True)


def _lower_bound_failures(texts: Iterable[str],
                          floors: Iterable[tuple[Decimal | None, frozenset[str]]],
                          exact: Mapping[Decimal | None, set[str]], pages: frozenset[int]
                          ) -> list[dict[str, Any]]:
    """``claim_count_lower_bound`` for each lower-bound count (``floors``: value and the
    words naming what it counts) its texts write as an exact number, occurrence by
    occurrence: each other passing claim of that number (``exact``) covers one occurrence,
    never every restatement of it."""
    out, seen = [], set()
    for floor, nouns in floors:
        if floor is None:
            continue
        written = [found for text in texts
                   for found in _stated_exactly(text or "", floor, pages, nouns)]
        for found in written[len(exact.get(floor, ())):]:
            if (floor, found) in seen:
                continue
            seen.add((floor, found))
            out.append({"value": found, "status": "failed", "code": "claim_count_lower_bound",
                        "detail": "no complete count entry a tool returned lists these "
                                  "records, so the count is a lower bound. Make it exact: "
                                  "count them with a tool (plx.aggregate with the set handle "
                                  "the walk returned and measure count, or the same "
                                  "plx.query walk with measures count) and claim that "
                                  "count with the records it cites; if no tool can count "
                                  f"them, state it as at least {_plain(floor)}"})
    return out


def _typed_claims(claims: Sequence[Mapping[str, Any]],
                  results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The claims as the wording check sees them: a passed claim's ``role`` is the quantity
    type the checker grounded (the source contract's role, the calculation's type by the
    type algebra, an identifier on an id column), never the name the caller chose; a
    passed calculation claim with no type is ``UNTYPED_CALCULATION``, which no word
    accepts and which blocks even an unbound word of its sentence. A failed claim keeps
    its declared role: its item fails already. A passed quote (``value_basis:
    text_span``) types none of the numbers it holds: each is an untyped number, as a page
    figure is, so a type word bound to it in the prose is never verified by the quote."""
    out = []
    for claim, result in zip(claims, results):
        if result["status"] != "passed":
            role = claim.get("role")
        elif result.get("role_basis") == "calculation" and result.get("quantity_type") is None:
            role = UNTYPED_CALCULATION
        else:
            role = result.get("quantity_type")
        out.append({"value": claim["value"], "kind": claim["kind"], "role": role})
        if result.get("value_basis") == "text_span":
            out += [{"value": _plain(number), "kind": "number", "role": None}
                    for _written, number in prose_numbers(claim["value"])]
    return out


def case_items(findings: Iterable[Mapping[str, Any]], gaps: Iterable[Mapping[str, Any]]
               ) -> tuple[list[dict[str, Any]], list[str]]:
    """(checkable items, ids of unreadable ones) from case-view findings and gaps."""
    items, unusable = [], []
    for finding in findings:
        if finding.get("statement") is None:
            unusable.append(str(finding.get("id")))
            continue
        claim = finding.get("claim") or {}
        items.append({"id": finding["id"], "texts": [finding["statement"]],
                      "citations": finding.get("citations") or [],
                      "value_claims": finding.get("value_claims") or [],
                      "workbench_statement": (claim.get("statement_by") == "workbench"
                                              or finding.get("statement_by") == "workbench")})
    for gap in gaps:
        if gap.get("citations_status") not in ("resolved", "uncited"):
            unusable.append(str(gap.get("id")))
            continue
        items.append({"id": gap["id"], "texts": [gap.get("missing") or "", gap.get("why") or ""],
                      "citations": gap.get("citations") or [],
                      "value_claims": gap.get("value_claims") or [],
                      "workbench_statement": gap.get("statement_by") == "workbench"})
    return items, unusable


def _id_key(ident: str) -> tuple[str, int]:
    match = re.match(r"^([A-Za-z]+)(\d+)$", ident)
    return (match.group(1), int(match.group(2))) if match else (ident, 0)


_TYPE_WORDS = {
    "planned": "planned", "plan": "planned", "estimated": "planned", "scheduled": "planned",
    "produced": "produced", "producing": "produced", "production": "produced",
    "manufactured": "produced", "made": "produced", "output": "produced", "yield": "produced",
    "filled": "filled", "fill": "filled", "filling": "filled",
    "inspected": "inspected", "inspection": "inspected", "accepted": "accepted",
    "ordered": "ordered", "damaged": "damaged", "received": "received",
    "shipped": "shipped", "delivered": "shipped", "rejected": "rejected",
    "retained": "retained", "returned": "returned", "qcpassed": "inspected"}
# "passed QC" / "QC passed" / "passed inspection": one inspected-family word.
_QC_PASSED = re.compile(r"\b(?:passed|passes|passing)\s+(?:QC|QA|inspection)\b"
                        r"|\b(?:QC|QA)[\s-]+(?:passed|passes|pass)\b", re.I)
# Clause separators for the binding: a type word never reaches across them.
_STOP_WORDS = frozenset({"and", "but", "or", "nor", "while", "whereas", "against", "than",
                         "vs", "versus", "compared", "exceeds", "exceeded", "exceeding",
                         "minus", "plus"})
_NEGATIONS = frozenset({"not", "never", "no"})
# A word between a negation and a type word that keeps it negated ("not fully received").
_NEGATED_THROUGH = frozenset({"longer", "yet", "fully", "completely", "entirely"})
_WORD_TOKEN = re.compile(
    r"(?P<num>" + NUMBER.pattern + r")|(?P<word>[A-Za-z_][A-Za-z0-9_']*)|(?P<open>\()"
    r"|(?P<stop>[\n;!?)\[\],]|\.(?=\s|$)|^\s*[-*]\s)", re.M)
_MAX_GAP = 3


def _wording_accepts(typed: str, role: str | None) -> bool:
    """A type word agrees with a claim only through the claim's grounded quantity type
    (a ``ROLES`` name); any other name is ungrounded and agrees with nothing."""
    if role is None or role not in ROLES:
        return False
    return role == typed or role in _ROLE_ALIASES.get(typed, frozenset())


def _wording_verdict(typed: str, roles: set[Any]) -> tuple[str, list[str]]:
    """("accepted" | "lead" | "claim_wording_role_mismatch" | "claim_wording_role_unverified",
    the quantity roles that disagree) of a type word over a number's claim roles."""
    if any(_wording_accepts(typed, role) for role in roles):
        return "accepted", []
    quantity = sorted(str(role) for role in roles
                      if role is not None and role != UNTYPED_CALCULATION
                      and (role in QUANTITY_ROLES or role not in ROLES))
    if quantity:
        return "claim_wording_role_mismatch", quantity
    if "identifier" in roles and UNTYPED_CALCULATION not in roles:
        return "lead", []
    return "claim_wording_role_unverified", []


def wording_check(text: str, claims: Sequence[Mapping[str, Any]],
                  pages: frozenset[int] = frozenset()
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(blocking failures, advisory leads) of the quantity-type words of ``text``.

    ``claims``' ``role`` is the quantity type the checker grounded (``check_items`` passes
    ``_typed_claims``); a name that is not in ``ROLES`` is ungrounded and agrees with no
    word, so a caller-chosen derived name never types prose.

    A type word (``produced``, ``planned``, ``filled`` ...; not after ``not``/``never``)
    binds the closest number of its clause within three words: commas, semicolons, ``and``,
    ``but``, ``than``, ``against``, line breaks and bullets separate clauses (a colon, an
    equals sign and a table cell bar do not: ``Produced: 1509``, ``| produced | 1509 |``);
    a word may bind forward into a parenthesis (``Produced (1509)``) but not back out of
    one (``1503 (produced finished goods)`` is a gloss). Closest pairs first; ties take
    the number after the word; each word and number once. For a bound number with claims:
    * ``claim_wording_role_mismatch`` (blocking): its claims carry another quantity role
      (``produced 1509`` over a claim that 1509 is planned);
    * ``claim_wording_role_unverified`` (blocking): its claims carry no quantity role
      (``produced 1`` over an untyped sample count);
    * a lead: its claims are identifiers (``order line 1140 was produced``).

    Sentence backstop: a type word bound to no number (``Produced, 1509``, ``produced the
    full batch of 1509``, ``Inspected and released 1509``) is checked against the claimed
    numbers of its sentence that no type word bound (``.``/``!``/``?`` or a blank line end
    a sentence; line breaks do not, so ``Produced\\n1509`` is one sentence). It blocks
    (``claim_wording_role_mismatch``) when none of them accepts the word and one carries
    another quantity type; when they are only identifiers or untyped it is a lead
    (``prose_type_word_without_a_bound_number``).
    A number with no claim is ``claim_unstructured`` elsewhere. ``pages`` are the pages the
    text's item cites: their locators (``_page_locators``) are blanked before binding."""
    # Page locators of cited pages are blanked first, exactly as for the number check, so a
    # type word never binds (and is never used up) on "page 3".
    claimed = [claim["value"] for claim in claims if claim["kind"] == "number"]
    clean = _QC_PASSED.sub(" qcpassed ", _strip(_page_locators(text, pages, claimed),
                                                lambda match: " " * len(match.group(0))))
    clean = _IE_EG.sub(lambda match: match.group(0).replace(".", " "), clean)
    tokens: list[dict[str, Any]] = []
    sentence = 0
    for match in _WORD_TOKEN.finditer(clean):
        if match.group("num") is not None:
            tokens.append({"t": "num", "text": match.group("num"), "sent": sentence,
                           "value": number_value(match)})
        elif match.group("word") is not None:
            word = match.group("word")
            lower = word.lower()
            if lower in _STOP_WORDS:
                tokens.append({"t": "stop", "sent": sentence})
                continue
            # "not produced", and "no longer" / "not yet" / "not fully received": no
            # type word.
            negated = bool(tokens) and (tokens[-1].get("lower") in _NEGATIONS or (
                tokens[-1].get("lower") in _NEGATED_THROUGH and len(tokens) > 1
                and tokens[-2].get("lower") in _NEGATIONS))
            tokens.append({"t": "word", "text": word, "lower": lower, "sent": sentence,
                           "typed": None if negated else _TYPE_WORDS.get(lower)})
        elif match.group("open") is not None:
            tokens.append({"t": "open", "sent": sentence})
        else:
            stop = match.group("stop")
            if (stop.startswith(".") and tokens and tokens[-1].get("lower") in _ABBREVIATIONS
                    and _DIGIT_NEXT.match(clean, match.start() + 1)):
                continue  # "p. 4", "No. 3": an abbreviation's period before a number
            blank_line = stop == "\n" and bool(tokens) and tokens[-1].get("stop") == "\n"
            tokens.append({"t": "stop", "stop": stop, "sent": sentence})
            if stop in ("!", "?") or stop.startswith(".") or blank_line:
                sentence += 1
    pairs = []
    for index, token in enumerate(tokens):
        if token["t"] != "word" or token["typed"] is None:
            continue
        for step in (1, -1):
            cursor, gap = index + step, 0
            while 0 <= cursor < len(tokens) and gap <= _MAX_GAP:
                other = tokens[cursor]
                if other["t"] == "stop" or (other["t"] == "open" and step == -1) or (
                        other["t"] == "word" and other["typed"] is not None):
                    break
                if other["t"] == "num":
                    pairs.append((gap, 0 if step == 1 else 1, index, cursor))
                    break
                if other["t"] == "word":
                    gap += 1
                cursor += step
    by_value: dict[Any, set[Any]] = {}
    for claim in claims:
        if claim["kind"] == "number":
            by_value.setdefault(to_decimal(claim["value"]), set()).add(claim.get("role"))
    used_words: set[int] = set()
    used_numbers: set[int] = set()
    failures, leads = [], []

    def report(verdict: str, quantity: list[str], word: str, typed: str, value: str,
               roles: set[Any], how: str) -> None:
        entry = {"word": word, "value": value,
                 "claim_roles": sorted(str(role) for role in roles)}
        if verdict == "lead":
            leads.append({**entry, "lead": "prose_type_word_next_to_an_identifier"})
        elif verdict == "claim_wording_role_mismatch":
            failures.append({**entry, "code": verdict,
                             "detail": f"the text calls {value} {typed} ('{word}'{how}), "
                                       "its claim says " + ", ".join(quantity)})
        else:
            why = ("its calculation has no quantity type by the type rules"
                   if UNTYPED_CALCULATION in roles else "no claim gives it a quantity role")
            failures.append({**entry, "code": verdict,
                             "detail": f"the text calls {value} {typed} ('{word}'{how}), "
                                       f"but {why}"})

    for _gap, _side, word, number in sorted(pairs):
        if word in used_words or number in used_numbers:
            continue
        used_words.add(word)
        used_numbers.add(number)
        typed = tokens[word]["typed"]
        roles = by_value.get(tokens[number]["value"])
        if roles is None:
            continue
        verdict, quantity = _wording_verdict(typed, roles)
        if verdict != "accepted":
            report(verdict, quantity, tokens[word]["text"], typed, tokens[number]["text"],
                   roles, "")
    seen: set[tuple[str, int]] = set()
    for index, token in enumerate(tokens):
        if token["t"] != "word" or token["typed"] is None or index in used_words:
            continue
        key = (token["typed"], token["sent"])
        numbers = [other for at, other in enumerate(tokens) if other["t"] == "num"
                   and other["sent"] == token["sent"] and other["value"] in by_value
                   and at not in used_numbers]
        if not numbers or key in seen:
            continue
        seen.add(key)
        roles = set().union(*(by_value[other["value"]] for other in numbers))
        verdict, quantity = _wording_verdict(token["typed"], roles)
        value = "/".join(dict.fromkeys(other["text"] for other in numbers))
        if verdict == "claim_wording_role_mismatch" or (
                verdict == "claim_wording_role_unverified" and UNTYPED_CALCULATION in roles):
            report(verdict, quantity, token["text"], token["typed"], value, roles,
                   " in the same sentence")
        elif verdict != "accepted":
            leads.append({"word": token["text"], "value": value,
                          "claim_roles": sorted(str(role) for role in roles),
                          "lead": "prose_type_word_without_a_bound_number"})
    return failures, sorted(leads, key=lambda lead: (lead["word"], lead["value"]))


# --------------------------------------------------------------------------- the prose screen
# The one prose screen (``screen_prose``) for an answer's text: the answer the gate renders
# from its templates (``operating.answer_gate.rendered_text``) and the answer text an eval
# scores (``evals.score.gate``). Words outside the claims' values state nothing no claim
# checked only if each is grammar, an ontology object-type noun (``frame_nouns``) or a
# record code (``KNOWN_ID_PREFIXES``), a quantity word framing a claim of its role, or a
# state word bound to a checked claim about that state.
PROSE_UNCHECKED = "claim_prose_unchecked"
# How the answer says a value rests on a lead-tier link (the gate's phrase writes it).
LEAD_MARK = "(lead: link not verified)"
# Function words, reporting verbs and the nouns of a field or value: the whole of the
# prose's own grammar (an allowlist: any other word is unchecked).
_PROSE_GRAMMAR = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "it", "its", "they", "their", "them",
    "is", "are", "was", "were", "be", "been", "has", "have", "had",
    "per", "on", "in", "for", "of", "by", "from", "at", "as", "to", "with", "under",
    "according", "and", "also", "least",
    "recorded", "reads", "shows", "holds", "lists", "listed", "states", "says", "gives",
    "comes", "carries", "carrying",
    "number", "numbers", "id", "ids", "value", "values", "field", "fields", "quantity",
    "quantities", "cell", "cells", "row", "rows", "date", "dates", "code", "codes", "name",
    "names", "unit", "units",
    *_RECORD_NOUNS, *(noun + "s" for noun in _RECORD_NOUNS), "entries"})
# A state word (``late``, ``received``) states a state of the record: it frames a value only
# beside a checked claim whose own field (its role, column or schema property, never the
# relation or type it sits on) states that state happened (``received_date``,
# ``fully_received``, ``shipped_at``; ``Receipt``, ``shipment_code`` or ``supplier`` do
# not). ``due`` is the one plan word: it binds to the planned date (``expected_date``).
_STATE_STEMS = {
    **dict.fromkeys(("late", "overdue"), frozenset({"late", "lateness", "overdue",
                                                    "delayed"})),
    "due": frozenset({"due", "expected", "promised", "requested"}),
    **dict.fromkeys(("confirmed", "confirms"), frozenset({"confirmed"})),
    **dict.fromkeys(("supplies", "supplied", "supply"), frozenset({"supplied"})),
    **dict.fromkeys(("received", "receives", "receive"), frozenset({"received"})),
    **dict.fromkeys(("shipped", "ships"), frozenset({"shipped"})),
    "delivered": frozenset({"delivered"}),
    "invoiced": frozenset({"invoiced"}),
    "paid": frozenset({"paid"}),
    "ordered": frozenset({"ordered"})}
# Words of a field that plans an event, not records it (``requestedReceiptDate``,
# ``expected_received_date``): such a field binds no event state, only ``due``.
_PLANNED_WORDS = frozenset({"expected", "requested", "planned", "promised", "scheduled",
                            "target", "estimated", "due", "projected", "forecast"})
# Values that deny a state: ``not received`` frames only a claim holding one of them.
_FALSE_VALUES = frozenset({"false", "no", "n", "f"})
# Words between ``not`` and the state word it denies (``not been received``).
_NOT_FILLER = frozenset({"be", "been"})
# Parts of a curated type name that are no record noun, or that state a fact or a state
# beside a value (``Aged``, ``ShipRelease``, ``AssemblyOrderDamage``, ``MCApproval``,
# ``ProductionTask``, ``BatchSchedule``): never frame nouns.
_NOT_FRAME_NOUNS = frozenset({"aged", "general", "ship", "release", "payable", "receivable",
                              "damage", "approval", "decision", "credit", "share", "point",
                              "measure", "schedule", "bill", "packing", "manufacturing",
                              "production"})
_PROSE_TOKEN = re.compile(r"(?P<word>[A-Za-z]+)|(?P<space>\s+)|(?P<punct>[.,;:()?!])")
_SENTENCE_END = frozenset(".?!;")
_NAME_PART = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")


def _name_words(name: Any) -> list[str]:
    """The lowercase words of an identifier (``po_number``, ``fullyReceived``,
    ``public.app_receipt``, ``PurchaseOrder``)."""
    return [part.lower() for part in _NAME_PART.findall(name)] if isinstance(name, str) else []


def _plural(noun: str) -> str:
    if noun.endswith(("s", "x", "ch", "sh")):
        return noun + "es"
    if noun.endswith("y") and noun[-2:-1] not in ("a", "e", "i", "o", "u"):
        return noun[:-1] + "ies"
    return noun + "s"


@lru_cache(maxsize=1)
def frame_nouns() -> frozenset[str]:
    """The nouns a prose may frame values with: each part of a curated ontology object
    type's name (``Lot``, ``Receipt``, ``SupplierConfirmation``, ``ItemLedgerEntry``),
    lowercase, of three letters or more, and its plural; none when the schema does not load
    (a template then falls back to lines: stricter, never looser)."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: no frame nouns
        return frozenset()
    out: set[str] = set()
    for type_id, origin in schema.origin_of_type.items():
        if origin != "curated":
            continue
        for word in _name_words(type_id):
            if len(word) >= 3 and word.isalpha() and word not in _NOT_FRAME_NOUNS:
                out.update((word, _plural(word)))
    return frozenset(out)


@lru_cache(maxsize=1)
def _schema_names() -> tuple[Mapping[tuple[str, str], frozenset[tuple[str, str]]],
                             Mapping[str, frozenset[str]]]:
    """((relation or BC entity set, column) -> {(type, property)} of each Portal mirror and
    BC column the schema binds, relation -> {type}); empty when the schema does not load."""
    try:
        from ontology.schema import load_schema
        schema = load_schema()
    except Exception:  # noqa: BLE001 - no schema: names from the citation alone
        return {}, {}
    columns: dict[tuple[str, str], set[tuple[str, str]]] = {}
    relations: dict[str, set[str]] = {}
    for type_id, spec in schema.object_types.items():
        for prop in spec.get("properties") or ():
            for source in prop.get("sources") or ():
                _type, binding = schema.bindings.get(source.get("binding"), (None, {}))
                path, relation = source.get("path"), binding.get("relation")
                if isinstance(path, str) and isinstance(relation, str):
                    columns.setdefault((relation, path), set()).add((type_id, prop["name"]))
                    relations.setdefault(relation, set()).add(type_id)
    return ({key: frozenset(value) for key, value in columns.items()},
            {key: frozenset(value) for key, value in relations.items()})


def _relation(handle: Any) -> str | None:
    """The relation (``public.app_receipt``) or BC entity set a cited handle names."""
    if not isinstance(handle, str) or "://" not in handle:
        return None
    scheme, rest = handle.split("://", 1)
    parts = rest.split("@", 1)[0].split("#", 1)[0].split("/")
    if scheme == "bc":
        return parts[2] if len(parts) == 4 else None
    return parts[0] or None


def _column(field: Any) -> str | None:
    """The row path a field reads (``po_number``, ``custom_fields.fmGeneratedPoId``)."""
    if not isinstance(field, str) or not _FIELD.match(field):
        return None
    return field.split(".", 2)[2] if field.startswith("rows.") else field.split(".", 1)[1]


def _cells(claim: Mapping[str, Any]) -> list[tuple[str | None, str | None]]:
    """(relation, column) of each cell a value claim reads (its citation, or each ``of``)."""
    given = [claim] if "citation" in claim else \
        [entry for entry in claim.get("of") or () if isinstance(entry, Mapping)]
    return [(_relation(entry.get("citation")), _column(entry.get("field"))) for entry in given]


def _concept(claim: Mapping[str, Any]) -> frozenset[str]:
    """The words of what a value claim is about: its role, and of each cell it reads its
    column, relation, schema property and that property's type."""
    columns, relations = _schema_names()
    words = set(_name_words(claim.get("role")))
    for relation, column in _cells(claim):
        words.update(_name_words(column), _name_words(relation))
        for type_id, prop in columns.get((relation, column), ()) if column else ():
            words.update(_name_words(type_id), _name_words(prop))
        for type_id in relations.get(relation, ()) if relation else ():
            words.update(_name_words(type_id))
    return frozenset(words)


def _field_words(claim: Mapping[str, Any]) -> frozenset[str]:
    """The words of the field a value claim states: its role, and of each cell it reads its
    column and schema property (never the relation or type the cell sits on)."""
    columns, _relations = _schema_names()
    words = set(_name_words(claim.get("role")))
    for relation, column in _cells(claim):
        words.update(_name_words(column))
        for _type, prop in columns.get((relation, column), ()) if column else ():
            words.update(_name_words(prop))
    return frozenset(words)


def field_label(claim: Mapping[str, Any]) -> str:
    """How an answer line names what a value claim states (``PO number: PO#5447``): its role,
    else the schema property of the cited column, else the column, as words with record
    codes capitalised; else its operation. Never a field path."""
    if isinstance(claim.get("role"), str) and claim["role"]:
        return " ".join(_name_words(claim["role"])) or claim["role"]
    field = claim.get("field")
    if isinstance(field, str) and "citation" in claim:
        relation, column = _cells(claim)[0]
        names = sorted({prop for _type, prop in _schema_names()[0].get((relation, column), ())}
                       ) if column else []
        name = names[0] if len(names) == 1 else (column or field).rsplit(".", 1)[-1]
        words = [word.upper() if len(word) > 1 and word.upper() in KNOWN_ID_PREFIXES
                 else word for word in _name_words(name)]
        if words:
            return " ".join(words)
    return str(claim.get("op") or "value")


def _number_claim(claim: Mapping[str, Any]) -> bool:
    if claim.get("op") == ADD_DAYS_OP:
        return False   # a calendar day (a date claim), never a figure
    return "op" in claim or claim.get("role") in QUANTITY_ROLES or \
        bool(_PLAIN_NUMBER.match(str(claim.get("value", ""))))


def _found_spans(text: str, claims: Sequence[Mapping[str, Any]]) -> list[tuple[int, int, int]]:
    """Where ``text`` writes each claim's value (start, end, claim index): its value on token
    boundaries, longest first, and for a number each figure of that value; each span taking
    the claim's unit or currency written right after it. No two spans overlap."""
    taken: list[tuple[int, int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e, _ in taken)

    order = sorted(range(len(claims)), key=lambda i: -len(str(claims[i].get("value", ""))))
    for index in order:
        value = str(claims[index].get("value", ""))
        found = [(at, at + len(value)) for at in _spans(value, text)] if value else []
        number = to_decimal(value) if _number_claim(claims[index]) else None
        if number is not None:
            found += [match.span() for match in NUMBER.finditer(text)
                      if not match.group("scale") and number_value(match) == number]
        for start, end in sorted(set(found)):
            for key in ("unit", "currency"):
                after = claims[index].get(key)
                if isinstance(after, str) and after and text.startswith(" " + after, end):
                    end += 1 + len(after)
            if free(start, end):
                taken.append((start, end, index))
    return sorted(taken)


def screen_prose(text: str, claims: Sequence[Mapping[str, Any]],
                 spans: Sequence[tuple[int, int, int]] | None = None,
                 gaps: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """The failures of an answer text against the checked value ``claims`` it writes (each
    ``{"value", "code", "detail"}``; none: the text states only what they state).
    ``spans`` are where it writes them (start, end, claim index: the gate's rendering knows);
    without them each claim's value is found in the text (an eval answer's prose).

    * ``claim_value_unstated``: a claim's value written negated (``is not OPEN``,
      ``passed? No``): the text denies what the claim states.
    * ``claim_currency_mismatch``: a claim's figure written beside a currency other than
      the one it checked (``1,260.50 EUR`` of a claim in USD; a bare ``$`` agrees with a
      dollar, ``same_currency``).
    * ``claim_prose_unchecked``, at most one per sentence: a word that is not grammar, an
      ontology object-type noun (``frame_nouns``) or a record code (``AO``, as written); a
      digit, a non-ASCII letter, a sign or a symbol outside a value; a value glued to a word
      or to another value with no word between them (``{p}{m}``, ``{p}k``); a quantity word
      (``planned``) beside a number claim of another role, or beside none; a state word
      (``late``, ``due``, ``confirmed``, ``supplies``, ``received``) no checked claim of the
      sentence is about (``late`` needs a lateness claim), or one a false claim value denies;
      ``not`` anywhere but before a state word a false claim value states (``not
      received``). A word of what a claim of the sentence is about (its role, column,
      schema property or type: ``order total``), or ``total`` beside a computed claim, is
      not unchecked.

    ``gaps`` are the answer's declared gaps (``{"state", "subject"}``): a sentence naming a
    gap's subject (``shipment``) may say its state as declared (``is not ingested``; ``is
    ingested`` inverts it), a ``GAP_STATES`` state only; any other gap adds nothing."""
    text = text if isinstance(text, str) else ""
    if spans is None:
        text = text.replace(LEAD_MARK, " " * len(LEAD_MARK))
        spans = _found_spans(text, claims)
    declared = _declared_gaps(gaps)
    # A currency written beside a claim's figure other than the one it checked (EUR beside
    # a value checked in USD) states what no claim checked: it fails, as a negation does.
    written_currencies = _figure_currencies(text)
    failures: list[dict[str, Any]] = []
    tokens: list[tuple[str, str, int | None]] = []   # (kind, text, claim index)
    at = 0
    for start, end, index in sorted(spans):
        if start < at:
            return [{"value": "", "code": PROSE_UNCHECKED, "detail": "overlapping values"}]
        _tokenize(text[at:start], tokens)
        tokens.append(("name", text[start:end], index))
        claim = claims[index]
        if "op" not in claim and _negated(str(claim.get("value", "")), text, start):
            failures.append({"value": str(claim.get("value", "")),
                             "code": "claim_value_unstated",
                             "detail": "the text writes this value negated; a value claim "
                                       "backs only what the text says"})
        checked = claim.get("currency")
        other = [currency for _, (first, last), _, _, currency in written_currencies
                 if start <= first and last <= end and isinstance(checked, str) and checked
                 and not same_currency(currency, checked)]
        if other:
            failures.append({"value": str(claim.get("value", "")),
                             "code": "claim_currency_mismatch",
                             "detail": f"the text writes this value in {other[0]}, the "
                                       f"claim checked {checked}"[:300]})
        at = end
    _tokenize(text[at:], tokens)
    sentence: list[tuple[str, str, int | None]] = []
    tokens.append(("punct", ".", None))
    for position, token in enumerate(tokens):
        sentence.append(token)
        # A sentence ends at its stop before a space or the end: ``{p}.{m}`` is one.
        ends = position + 1 == len(tokens) or tokens[position + 1][0] == "space"
        if token[0] == "punct" and token[1] in _SENTENCE_END and ends or \
                token[0] == "space" and "\n" in token[1]:
            problem = _sentence_problem(sentence, claims, declared)
            if problem is not None:
                failures.append({"value": problem[0], "code": PROSE_UNCHECKED,
                                 "detail": problem[1]})
            sentence = []
    return failures


def _declared_gaps(gaps: Any) -> tuple[tuple[frozenset[str], frozenset[str]], ...]:
    """(subject words, state words) of each declared gap of a ``GAP_STATES`` state and an
    object-type subject; anything else is dropped (it permits no word)."""
    out = []
    nouns = frame_nouns()
    for gap in gaps if isinstance(gaps, (list, tuple)) else ():
        if not isinstance(gap, Mapping) or gap.get("state") not in GAP_STATES or \
                not isinstance(gap.get("subject"), str):
            continue
        subject = frozenset(word for word in _name_words(gap["subject"]) if len(word) >= 3)
        if subject and subject <= nouns:
            out.append((subject, frozenset(gap["state"].split("_"))))
    return tuple(out)


def _tokenize(text: str, tokens: list[tuple[str, str, int | None]]) -> None:
    at = 0
    while at < len(text):
        match = _PROSE_TOKEN.match(text, at)
        if match is None:   # a digit run is one token (``99``, never ``9``)
            run = re.match(r"\d+", text[at:])
            other = run.group(0) if run else text[at]
            tokens.append(("other", other, None))
            at += len(other)
            continue
        tokens.append((match.lastgroup or "other", match.group(0), None))
        at = match.end()


def _sentence_problem(tokens: Sequence[tuple[str, str, int | None]],
                      claims: Sequence[Mapping[str, Any]],
                      declared: Sequence[tuple[frozenset[str], frozenset[str]]] = ()
                      ) -> tuple[str, str] | None:
    """(the offending token, why) of the first thing one sentence states that no claim of it
    checked, or None."""
    named = [index for kind, _, index in tokens if kind == "name"]
    said = {text.lower() for kind, text, _ in tokens if kind == "word"}
    # each state word of a gap the sentence names, and whether its state negates it
    # (``not_ingested``: ``ingested`` only after ``not``); two polarities permit neither
    polarity: dict[str, set[bool]] = {}
    for subject, state in declared:
        if all(word in said or _plural(word) in said for word in subject):
            for word in state - {"not"}:
                polarity.setdefault(word, set()).add("not" in state)
    gap_words = {word: next(iter(sides)) for word, sides in polarity.items() if len(sides) == 1}
    # the sentence may name what its claims are about (``order total``) and call a computed
    # claim a ``total``; no schema, no such word (stricter, never looser)
    nouns = frame_nouns()
    about = frozenset().union(*(_concept(claims[index]) for index in named)) | (
        {"total"} if any("op" in claims[index] or "calculation" in claims[index]
                         for index in named) else set()) if nouns else frozenset()

    def bound(word: str, denied: bool) -> bool:
        stems = _STATE_STEMS.get(word)
        plan = word == "due"
        return stems is not None and any(
            not _number_claim(claims[index]) and _field_words(claims[index]) & stems
            and (plan or not _field_words(claims[index]) & _PLANNED_WORDS)
            and (_fold(str(claims[index].get("value", ""))) in _FALSE_VALUES) == denied
            for index in named)

    def beside(index: int) -> list[int]:
        out = []
        if index >= 2 and tokens[index - 1][0] == "space" and tokens[index - 2][0] == "name":
            out.append(tokens[index - 2][2])
        rest = index + 1
        if rest < len(tokens) and tokens[rest][1] == ":":
            rest += 1
        if rest < len(tokens) and tokens[rest][0] == "space" and " " == tokens[rest][1]:
            rest += 1
        if rest > index + 1 and rest < len(tokens) and tokens[rest][0] == "name":
            out.append(tokens[rest][2])
        return out

    def next_word(index: int) -> tuple[int, str] | None:
        for at in range(index + 1, len(tokens)):
            kind, text, _ = tokens[at]
            if kind == "word" and text.lower() not in _NOT_FILLER:
                return at, text.lower()
            if kind != "space" and kind != "word":
                return None
        return None

    def denied_by_not(index: int) -> bool:
        for at in range(index - 1, -1, -1):
            kind, text, _ = tokens[at]
            if kind == "word" and text.lower() in _NOT_FILLER or kind == "space":
                continue
            return kind == "word" and text.lower() == "not"
        return False

    last_name = None
    for index, (kind, text, _claim) in enumerate(tokens):
        if kind == "other":
            return text, "a digit, letter, sign or symbol no claim states"
        if kind == "name":
            glued = [tokens[i] for i in (index - 1, index + 1) if 0 <= i < len(tokens)]
            if any(k in ("word", "name") for k, _, _ in glued) or last_name is not None and \
                    not any(k == "word" for k, _, _ in tokens[last_name + 1:index]):
                return text, "a value glued to a word or to another value"
            last_name = index
            continue
        if kind != "word":
            continue
        lower = text.lower()
        if lower == "not":
            following = next_word(index)
            if following is not None and gap_words.get(following[1]) is True:
                continue
            if following is None or following[1] not in _STATE_STEMS or \
                    not bound(following[1], True):
                return text, "a negation no false claim value states"
            continue
        if lower in QUANTITY_ROLES:
            numbers = [i for i in beside(index) if _number_claim(claims[i])]
            if numbers:
                if any(claims[i].get("role") != lower for i in numbers):
                    return text, "a quantity word beside a claim of another role"
                continue
        if lower in gap_words:
            if denied_by_not(index) != gap_words[lower]:
                return text, "a gap state the text writes inverted"
            continue
        if lower in _STATE_STEMS:
            if not bound(lower, denied_by_not(index)):
                return text, "a state word no checked claim of the sentence is about"
            continue
        if lower in QUANTITY_ROLES:
            return text, "a quantity word beside no claim of its role"
        if lower in _PROSE_GRAMMAR or lower in nouns or lower in about or \
                len(text) > 1 and text in KNOWN_ID_PREFIXES:
            continue
        return text, "a word that is not grammar, an object-type noun or a record code"
    return None
