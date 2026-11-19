"""What an Autopilot message may carry to a reader outside the company.

A reply to a customer or supplier, an option's reply on a question card, or a message the
assistant or a job proposes to an outside address is read by that party. Two rules hold it
to the party's own business:

* reads: the assistant's session for a conversation with an outside reader gets no record
  read tools; it writes from the message, the thread and the lines the store gives about
  the outside party's own records (``autopilot_drafter.material_of``: their open orders,
  invoices, purchase orders and the records the message names that are theirs) and our own
  catalog's lines for products the message names (``product_lines``: no price, no cost,
  never a customer's own private-label product).
* the outbound check (``held``) on every text that goes out: it is held when it names
  another customer's or supplier's record (an order, PO, invoice or shipment number that is
  not the recipient's own and not written in what they sent), another customer or supplier
  by name (or by the distinctive word that leads its name), a price that the recipient's
  own material does not state, or any number of four or more digits the recipient's
  message and own records do not carry (a date, a year beside a month and a quantity with
  its unit excepted). A number is the same however it is written ("SO 9931", "SO-9931",
  "order 9931": its digits; record lookups also try every identifier kind's normal form).
  Each outside recipient is checked on their own: the conversation's party lines count for
  a recipient only when every record they name is that recipient's own. A held reply
  becomes a question card that says why; a held proposed message is dropped and the item
  says what was held back.

Undecidable (no store to check against): held (fail closed).
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

# the record types a number in an outgoing text can name that belong to one party
PARTY_TYPES = ("SalesOrder", "SalesInvoice", "Shipment", "CustomerPurchaseOrder",
               "PurchaseOrder")
SUPPLIER_LINK = "PurchaseOrder.ordered_from"
# words that, before or after a figure, make it a price
_PRICE_WORD = re.compile(r"(?i)\b(?:price[ds]?|pricing|cost[s]?|rate[s]?|quoted?|charge[ds]?|"
                         r"fee[s]?|per\s+(?:kg|kilo|lb|pound|unit|case|piece|each|litre|liter|"
                         r"gallon|pallet|box|bottle|ea)|each|/\s?(?:kg|lb|unit|ea|case|pc|l)\b)")
_SUFFIX = re.compile(r"\b(?:inc|incorporated|ltd|limited|llc|corp|corporation|co|company|the|"
                     r"gmbh|plc|ulc)\b")
NAME_MIN = 6                 # a one-word party name shorter than this is not matched
NGRAM = 5
WORD_NAMES = 2               # a name's first word in more party names is no one party's
NUMBER_DIGITS = 4            # a number this long the reader has not seen is held
# identifier kinds that name no party's business (a product code, a system's internal id)
RECORD_KINDS_SKIPPED = ("item_code", "erp_item_code", "fm_id", "fm_external_id",
                        "fm_supplier_id", "fm_bulk_product_id", "fm_finished_product_id",
                        "bc_system_id", "internet_message_id", "content_sha256", "gl_account",
                        "formula_code")
# words that lead many company names and plain sentences alike: never one party's
GENERIC_WORDS = frozenset("""
american canadian national international global united general north south east west
northern southern eastern western pacific atlantic central continental premier quality
natural nature pure green golden royal modern advanced active prime superior standard
special supply supplies service services product products industries industrial
chemical chemicals packaging solutions systems trading company group canada america
monday tuesday wednesday thursday friday saturday sunday january february march april
june july august september october november december
""".split())
_RUN = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)|\d+")
_SEP = re.compile(r"[\s#.\-_/:]+")
_LABEL_BEFORE = re.compile(r"(?i)\b([a-z]{1,8})\.?\s*(?:#|no\.?|number)?\s*[#:\-]?\s*$")
_CAPITALISED = re.compile(r"(?<![\w@.])[A-Z][A-Za-z]{%d,}(?![\w@])" % (NAME_MIN - 1))
_YEAR_NEAR = re.compile(r"(?i)(?:\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
                        r"[a-z]*\.?,?\s+|\b(?:in|since|year|fy|of|q[1-4])\s+)((?:19|20)\d{2})\b")
REASONS = {
    "another_partys_record": "it names an order, PO, invoice or shipment that is not "
                             "{who}'s own",
    "another_partys_name": "it names another customer or supplier",
    "price_not_on_their_records": "it states a price that {who}'s own records and "
                                  "message do not",
    "number_not_on_their_records": "it carries a number that {who}'s own records and "
                                   "message do not",
    "outbound_unchecked": "it goes outside the company and could not be checked",
}


def norm_name(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
    return " ".join(_SUFFIX.sub(" ", words).split())


def outside(addresses: Iterable[Any], domains: Sequence[str]) -> list[str]:
    """The addresses of ``addresses`` that are not exactly one of our domains' (a
    display-name form, a guest UPN or anything unreadable counts as outside)."""
    from .autopilot_handlers import _ours
    out = []
    for a in addresses:
        a = str(a or "").strip().lower()
        if a and not _ours(a, domains):
            out.append(a)
    return list(dict.fromkeys(out))


def recipients(proposal: Mapping[str, Any]) -> list[str]:
    """Everyone a proposed mail or Teams message reaches (to, cc, bcc, members); a Teams
    chat named only by id is a reader nobody can check: ``"chat:<id>"``."""
    args = proposal.get("arguments") if isinstance(proposal.get("arguments"), Mapping) else {}
    change = args.get("change") if isinstance(args.get("change"), Mapping) else {}
    target = args.get("target") if isinstance(args.get("target"), Mapping) else {}
    out: list[str] = []
    for k in ("to", "cc", "bcc", "members"):
        v = change.get(k)
        out += [str(x) for x in (v if isinstance(v, (list, tuple)) else [v] if v else [])]
    if not out and target.get("chat_id"):
        out.append(f"chat:{target['chat_id']}")
    return out


def is_message(proposal: Mapping[str, Any]) -> bool:
    args = proposal.get("arguments") if isinstance(proposal.get("arguments"), Mapping) else {}
    return str(args.get("system") or "").lower() in ("mail", "teams")


def body_of(proposal: Mapping[str, Any]) -> str:
    args = proposal.get("arguments") if isinstance(proposal.get("arguments"), Mapping) else {}
    change = args.get("change") if isinstance(args.get("change"), Mapping) else {}
    return f"{change.get('subject') or ''}\n{change.get('body') or ''}"


def _party(ctx: Any, readers: Sequence[str]) -> tuple[set[str], set[str], set[str]]:
    """(customer numbers, supplier records, party records) the outside ``readers`` are held
    for (their exact address, else their company domain)."""
    from .autopilot_events import _parties
    from .autopilot_records import _v
    numbers: set[str] = set()
    suppliers: set[str] = set()
    party: set[str] = set()
    for a in readers:
        if "@" not in a:
            continue
        for side, oid in _parties(ctx.st, a):
            party.add(oid)
            if side == "supplier":
                suppliers.add(oid)
    customers = sorted(o for o in party if o.startswith("Customer:"))
    if customers:
        twins = [o for o in ctx.linked_many(customers, ("Customer.same_record_as",))
                 if o.startswith("Customer:")]
        party.update(twins)
        for r in ctx.read(sorted(set(customers) | set(twins)), ("erp_code",)).values():
            if _v(r, "erp_code"):
                numbers.add(_v(r, "erp_code").strip().upper())
    return numbers, suppliers, party


def _theirs(ctx: Any, oid: str, numbers: set[str], suppliers: set[str]) -> bool:
    from .autopilot_handlers import customers_own
    if oid.startswith("PurchaseOrder:"):
        return bool(suppliers & set(ctx.linked(oid, (SUPPLIER_LINK,))))
    return any(customers_own(ctx, [oid], n) for n in numbers)


def _names(ctx: Any) -> dict[str, set[str]]:
    """normalized name -> the customer and supplier records of that name (read once per
    store)."""
    from .autopilot_events import PARTY_ADDRESSES, _read
    from .autopilot_records import _v

    def make() -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for otype in PARTY_ADDRESSES:
            ids = [r[0] for r in ctx.st.conn.execute(
                "SELECT object_id FROM objects WHERE object_type = ?", (otype,))]
            for start in range(0, len(ids), 5000):
                for oid, row in _read(ctx.st, ids[start:start + 5000], ("name",)).items():
                    n = norm_name(_v(row, "name"))
                    if n and (" " in n or len(n) >= NAME_MIN):
                        out.setdefault(n, set()).add(oid)
        return out
    st = ctx.st
    return st.memo("autopilot:party_names", make) if hasattr(st, "memo") else make()


def _ours_parties(ctx: Any) -> set[str]:
    """The customer/supplier records held for one of our own addresses (the company as its
    own customer or supplier): naming it is never another party."""
    from .autopilot_events import _party_addresses
    out: set[str] = set()
    for key, found in _party_addresses(ctx.st).items():
        address = "x" + key if key.startswith("@") else key    # a domain key: any address
        if not outside([address], ctx.domains):
            out.update(oid for _s, oid in found)
    return out


def _written(token: str, text: str) -> bool:
    return bool(token) and re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", text,
                                     re.I) is not None


def _prices(text: str) -> list[tuple[Any, str]]:
    """The figures of ``text`` that are prices: a currency beside them, or a price word
    within a few words of them."""
    from .autopilot_drafter import _NUMBER, _unit
    from decimal import Decimal, InvalidOperation
    out = []
    for m in _NUMBER.finditer(text):
        raw = m.group(1).rstrip(",").rstrip(".")
        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        unit = _unit(text[:m.start()], text[m.start() + len(raw):])
        near = text[max(0, m.start() - 25):m.start() + len(raw) + 25]
        if unit in ("usd", "cad", "eur", "gbp", "$") or _PRICE_WORD.search(near):
            out.append((value, unit))
    return out


def _name_words(ctx: Any) -> dict[str, set[str]]:
    """distinctive word -> the parties whose name it leads: the first word of a name of two
    or more words, at least ``NAME_MIN`` letters, leading or inside at most ``WORD_NAMES``
    party names, no generic word, and written in that party's own mail domain ("Northwind"
    for "Northwind Soapworks Inc" at northwind.example): a plain word or a first name that
    happens to lead a name ("Current", "Christine") is not one party's."""
    from .autopilot_events import _party_addresses

    def make() -> dict[str, set[str]]:
        names = _names(ctx)
        labels: dict[str, set[str]] = {}
        for key, found in _party_addresses(ctx.st).items():
            if key.startswith("@"):
                label = key[1:].split(".", 1)[0].lower()
                for _side, oid in found:
                    labels.setdefault(oid, set()).add(label)
        inside: dict[str, int] = {}
        for n in names:
            for w in set(n.split()):
                inside[w] = inside.get(w, 0) + 1
        out: dict[str, set[str]] = {}
        for n, oids in names.items():
            words = n.split()
            w = words[0]
            if len(words) < 2 or len(w) < NAME_MIN or not w.isalpha() \
                    or w in GENERIC_WORDS or inside.get(w, 0) > WORD_NAMES:
                continue
            mine = {o for o in oids if any(w in label for label in labels.get(o, ()))}
            if mine:
                out.setdefault(w, set()).update(oids)
        return out
    st = ctx.st
    return st.memo("autopilot:party_name_words", make) if hasattr(st, "memo") else make()


def _masked(text: str) -> str:
    """``text`` with its dates blanked (a date's digits are no record number)."""
    from .autopilot_drafter import _DATES
    flat = text or ""
    for rx, _order in _DATES:
        flat = rx.sub(lambda m: " " * (m.end() - m.start()), flat)
    return _YEAR_NEAR.sub(lambda m: m.group(0)[:m.start(1) - m.start()] + " " * 4, flat)


def _runs(text: str) -> list[tuple[str, int, int]]:
    """(digits without leading zeros, start, end) of each number of ``text``: a thousands
    grouping is one number (``12,500`` -> ``12500``), any other separator splits
    (``SO-9931`` -> ``9931``; ``PO100109`` -> ``100109``)."""
    out = []
    for m in _RUN.finditer(text or ""):
        digits = m.group(0).replace(",", "")
        out.append((digits.lstrip("0") or "0", m.start(), m.end()))
    return out


def _cores(text: str) -> set[str]:
    return {d for d, _s, _e in _runs(text)}


def _number_unseen(text: str, seen: set[str]) -> bool:
    """Whether ``text`` carries a number of ``NUMBER_DIGITS`` or more digits that ``seen``
    (the reader's message and own records, as digit cores) does not: a date, a year beside
    a month or a quantity with its unit excepted. Written as "SO 9931", "SO-9931",
    "order 9931" or "9931", the number is the same."""
    from ontology.identifiers import is_quantity
    flat = _masked(text)
    for core, start, end in _runs(flat):
        if len(core) < NUMBER_DIGITS or core in seen:
            continue
        if flat[start - 1:start] in ("$", "€", "£"):
            continue                       # a price: the price rule decides it
        if is_quantity(flat, flat[start:end].replace(",", ""), end):
            continue
        return True
    return False


def _variants(text: str, start: int, end: int, token: str) -> set[str]:
    """The stored forms ``token`` (at ``start``..``end`` of ``text``) may have: as written,
    every kind's normal form (``ontology.identifiers.normalize``), separators dropped, with
    the label word before a bare number ("SO 9931", "PO-100109", "PO#5447")."""
    from ontology import identifiers as idf
    forms = {token, token.upper(), _SEP.sub("", token).upper()}
    label = _LABEL_BEFORE.search(text[max(0, start - 16):start]) if token.isdigit() else None
    head = re.match(r"([A-Za-z]{1,8})[\s#.\-_/:]*(\d.*)$", token)
    if label or head:
        word, digits = (label.group(1), token) if label else (head.group(1), head.group(2))
        forms.update(word.upper() + sep + digits for sep in ("", "-", " ", "#"))
    for form in list(forms):
        for kind in idf.kinds():
            if kind in RECORD_KINDS_SKIPPED:
                continue
            try:
                n = idf.normalize(kind, form)
            except (TypeError, ValueError):
                n = None
            if n:
                forms.add(n)
    return {f for f in forms if f}


def _named(ctx: Any, text: str) -> list[tuple[str, str]]:
    """(digit core, record) of each party record ``text`` names in any written form of its
    number (a customer PO resolved to its orders); every record a form names counts (an
    ambiguous number is checked against all of them: fail closed)."""
    import json as _json
    from .autopilot_handlers import TOKEN
    from ontology.identifiers import is_quantity
    found: list[tuple[str, set[str]]] = []
    text = _masked(text)                   # a date's digits name no record
    for m in TOKEN.finditer(text):
        tok = m.group(1).strip("-")
        core = re.sub(r"\D", "", tok).lstrip("0")
        if tok.isdigit() and is_quantity(text, tok, m.end(1)):
            continue                       # 1200 kg: a quantity, no record
        if 3 <= len(core) and len(tok) <= 25:
            found.append((core, _variants(text, m.start(1), m.end(1), tok)))
    found = found[:40]
    if not found:
        return []
    ctx.check()
    wanted = sorted({f for _c, fs in found for f in fs})
    rows = ctx.st.conn.execute(
        "SELECT normalized, identifier_kind, object_id FROM identifiers WHERE normalized IN "
        "(SELECT value FROM json_each(?))", (_json.dumps(wanted),)).fetchall()
    by_form: dict[str, set[str]] = {}
    for norm, kind, oid in rows:
        if kind not in RECORD_KINDS_SKIPPED and oid.split(":", 1)[0] in PARTY_TYPES + (
                "CustomerPurchaseOrder",):
            by_form.setdefault(str(norm).upper(), set()).add(oid)
    out: list[tuple[str, str]] = []
    for core, forms in found:
        oids = {o for f in forms for o in by_form.get(f.upper(), ())}
        for oid in sorted(oids):
            if oid.startswith("CustomerPurchaseOrder:"):
                out += [(core, o) for o in ctx.linked(oid, ("SalesOrder.references_customer_po",))
                        if o.startswith("SalesOrder:")]
            else:
                out.append((core, oid))
    return out


def _records_of(ctx: Any, line: str, cache: dict[str, list[str]]) -> list[str]:
    if line not in cache:
        cache[line] = [o for _c, o in _named(ctx, line)]
    return cache[line]


def held(ctx: Any, text: str, readers: Sequence[str], allowed: str,
         lines: Sequence[str] = ()) -> str:
    """Why ``text`` may not go to ``readers`` ("" : it may): ``REASONS``' keys. Each outside
    reader is checked on its own: ``allowed`` is what every reader already has (the message
    and thread they read), ``lines`` the party lines of the conversation, each counting for
    a reader only when every record it names is that reader's own (a thread with two outside
    parties never hands one's records to the other). Readers all ours: "" (the outbound
    rules are for outside readers only)."""
    if ctx is None:
        return "outbound_unchecked" if any(str(r).startswith("chat:") or "@" in str(r)
                                           for r in readers) else ""
    out_readers = [r for r in readers if str(r).startswith("chat:")] + \
        outside([r for r in readers if not str(r).startswith("chat:")], ctx.domains)
    if not out_readers or not str(text or "").strip():
        return ""
    named = _named(ctx, text)
    cache: dict[str, list[str]] = {}
    for reader in out_readers:
        why = _held_for(ctx, text, reader, allowed, lines, named, cache)
        if why:
            return why
    return ""


def _held_for(ctx: Any, text: str, reader: str, allowed: str, lines: Sequence[str],
              named: Sequence[tuple[str, str]], cache: dict[str, list[str]]) -> str:
    from .autopilot_drafter import _agree
    numbers, suppliers, party = _party(ctx, [reader])
    own = [line for line in lines
           if all(_theirs(ctx, o, numbers, suppliers) for o in _records_of(ctx, line, cache))]
    seen_text = "\n".join([allowed, *own])
    seen = _cores(seen_text)
    for core, oid in named:
        if core in seen:
            continue
        if not _theirs(ctx, oid, numbers, suppliers):
            return "another_partys_record"
    names = _names(ctx)
    ours = _ours_parties(ctx)
    seen_names = norm_name(seen_text)
    words = norm_name(text).split()
    for n in range(1, NGRAM + 1):
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i:i + n])
            found = names.get(phrase)
            if found and not (found & party) and not (found & ours) \
                    and not _written(phrase, seen_names):
                return "another_partys_name"
    lead = _name_words(ctx)
    seen_words = set(seen_names.split())
    for m in _CAPITALISED.finditer(text):
        w = m.group(0).lower()
        found = lead.get(w)
        if found and not (found & party) and not (found & ours) and w not in seen_words:
            return "another_partys_name"
    have = _prices(seen_text) + [(v, u) for v, u in _numbers(seen_text)]
    for value, unit in _prices(text):
        if not any(value == v and _agree(unit, u) for v, u in have):
            return "price_not_on_their_records"
    if _number_unseen(text, seen):
        return "number_not_on_their_records"
    return ""


def _numbers(text: str) -> list[tuple[Any, str]]:
    from .autopilot_drafter import _tokens
    return [(v, u) for v, u, _d in _tokens(text)[1]]


def reason_text(why: str, who: str) -> str:
    return REASONS.get(why, REASONS["outbound_unchecked"]).format(who=who or "the recipient")


def product_lines(ctx: Any, text: str) -> list[str]:
    """Our catalog's line for each product the message names by its item number (code,
    name, unit, size): never a price or cost, never a product a customer owns (a private
    label is that customer's business)."""
    from .autopilot_records import _v
    from .autopilot_work import resolve_items
    tokens = [m.group(0) for m in re.finditer(r"\b[A-Za-z0-9][A-Za-z0-9-]{2,24}\b", text[:20000])
              if any(c.isdigit() for c in m.group(0))]
    found = resolve_items(ctx, list(dict.fromkeys(tokens))[:40]) if tokens else {}
    oids = sorted({o for v in found.values() for o in v})[:10]
    out = []
    for oid, r in (ctx.read(oids, ("code", "name", "uom", "size_text",
                                   "customer_owner_fm_id")) if oids else {}).items():
        if _v(r, "customer_owner_fm_id") or not _v(r, "code"):
            continue
        said = [x for x in (_v(r, "name"), _v(r, "size_text"),
                            f"unit {_v(r, 'uom')}" if _v(r, "uom") else "") if x]
        out.append(f"Product {_v(r, 'code')}" + (": " + ", ".join(said) if said else "") + ".")
    return out
