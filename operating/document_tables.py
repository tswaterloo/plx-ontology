"""Hand-kept sheets and documents read as cited tables: one generic reader, no per-file,
per-sender or per-customer code.

* **Finding documents.** ``find_documents`` lists the mailed attachments, archive members
  and SharePoint files (``DOC_TYPES``) whose current name or path holds one of a family's
  words, through the store's value text index (``value_text``), at most ``MAX_DOCS`` per
  family (``Found.complete`` false when cut). Each document carries its record, the
  captured version it cites (``handle``), its audience and markings, its day (the message
  it came with: ``Attachment.attached_to`` an ``Email``'s ``sent_at``; an archive member
  through its attachment; a SharePoint file's ``last_modified``) and the customers and
  suppliers its message was sent by (``Email.sent_by_customer``, ``Email.sent_by_supplier``).
* **Text.** ``texts`` reads each version's captured text (``text_fields``/``text_chunks``,
  the chunks laid at their offsets so an overlap is never read twice); none captured:
  the document is ``text_not_captured`` (counted, never guessed).
* **Tables.** ``tables`` parses the workbook rendering of ``integrations.xlsx_extract``
  (``[Sheet: name]`` then ``A1=value | B1=value``; a formula cell's cached value) and plain
  delimited text (tab, semicolon or comma with three fields or more). The header row is the
  row of the first ``HEADER_ROWS`` whose cells name the most roles (``ROLES``: item,
  description, customer, supplier, quantity, price, currency, unit, date, order, priority,
  lot, status, question, answer); a column whose header reads as a month is a period column
  (``month_of``). Every value keeps its sheet and cell, so a figure is cited to the cell.
* **Records.** ``Resolver`` maps a cell value to the records whose identifier it is (the
  store's ``identifiers`` by normalized value, of the types asked); a value naming records
  of more than one identity is ambiguous and resolves to nothing.
* **Visibility.** ``Gate`` decides a record for one reader: the people exclusion first
  (``unit_clear`` of its audience; a guard that is missing or unavailable clears nothing),
  then the reader's audience grant and every marking the reader must hold. A row shows only
  when every record it cites is visible and its own markings (``finance`` on money) are held.

Fixture runs are ``isolated-fixture``; the live store read is live-unverified.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
import sqlite3
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

DOC_TYPES = ("Attachment", "ContainerMember", "SharePointFile")
NAME_PROPS = ("name", "path")
MAX_DOCS = 4000
HEADER_ROWS = 15
MAX_ROWS = 2000
TEXT_PATH = "row.content_text"
ATTACHED_TO = "Attachment.attached_to"
IN_ATTACHMENT = "ContainerMember.in_attachment"
SENT_BY = {"customer": "Email.sent_by_customer", "supplier": "Email.sent_by_supplier"}
RETIRED = re.compile(r"(?i)\b(do ?n[o']?t use|don'?t use|not in use|inactive|obsolete)\b")
PARTY_EMAIL_PROPS = ("bc_email", "email", "email_2", "email_3", "website")
PUBLIC_DOMAINS = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                            "live.com", "yahoo.com", "yahoo.ca", "icloud.com", "me.com",
                            "aol.com", "msn.com", "protonmail.com", "shaw.ca", "rogers.com",
                            "sympatico.ca", "bell.net"})

ROLES: dict[str, tuple[str, ...]] = {
    "item": ("item", "item no", "item #", "item number", "item code", "sku", "part",
             "part no", "part number", "product code", "material", "code", "upc", "style"),
    "description": ("description", "product", "product name", "item description", "name"),
    "customer": ("customer", "account", "ship to", "bill to", "client", "retailer"),
    "supplier": ("supplier", "vendor", "manufacturer"),
    "quantity": ("qty", "quantity", "units", "cases", "order qty", "forecast", "volume",
                 "pcs", "eaches", "forecast qty"),
    "price": ("price", "unit price", "net price", "cost", "unit cost", "price each",
              "list price", "sell price", "new price", "fob price", "rate"),
    "currency": ("currency", "cur", "ccy"),
    "unit": ("uom", "unit", "unit of measure", "pack"),
    "date": ("date", "ship date", "due date", "delivery date", "eta", "need by",
             "required date", "requested date", "effective date", "start date", "week"),
    "order": ("po", "po #", "po no", "po number", "purchase order", "order", "order #",
              "order no", "order number", "so", "so #", "sales order", "customer po"),
    "priority": ("priority", "rank", "ranking", "seq", "sequence", "fill order"),
    "lot": ("lot", "lot #", "lot no", "lot number", "batch", "batch #", "batch no"),
    "status": ("status", "state", "stage"),
    "question": ("question", "questions", "requirement", "query"),
    "answer": ("answer", "answers", "response", "reply", "comments"),
}
MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
CURRENCIES = frozenset({"CAD", "USD", "EUR", "GBP", "AUD", "MXN", "CNY", "INR", "JPY"})

_SHEET = re.compile(r"^\[Sheet: (.*)\]$")
_CELL = re.compile(r"^([A-Z]{1,3})(\d{1,7})=(.*)$", re.S)
_FORMULA = re.compile(r"^FORMULA\(.*\); cached=(.*)$", re.S)
_WORD = re.compile(r"[a-z0-9#]+")
_YEAR = re.compile(r"\b(20\d\d)\b")
_MONTH = re.compile(r"^(?:(?P<y1>20\d\d)[-/ ](?P<m1>\d{1,2})(?:[-/ ]\d{1,2})?(?:[ T].*)?"
                    r"|(?P<mn>[a-z]{3,9})\.?[ ,'/-]*(?P<y2>(?:20)?\d\d)?"
                    r"|(?P<m3>\d{1,2})[/-](?P<y3>20\d\d))$")
_MONEY = re.compile(r"^\(?-?\s*(?P<cur>[A-Z]{3}|US\$|C\$|CA\$|\$|€|£)?\s*"
                    r"(?P<num>-?\d[\d,]*(?:\.\d+)?)\s*(?P<cur2>[A-Z]{3})?\)?$")


# --------------------------------------------------------------------------- gate

class Gate:
    """One reader's view of the store's records (module doc, Visibility)."""

    def __init__(self, conn: sqlite3.Connection, *, may_read: Callable[[str], bool],
                 held: Iterable[str], guard: Any) -> None:
        self.conn = conn
        self.may_read = may_read
        self.held = frozenset(held)
        self.guard = guard
        self._seen: dict[str, bool] = {}
        self._info: dict[str, tuple[str, frozenset[str] | None]] = {}

    @property
    def unavailable(self) -> str | None:
        if self.guard is None:
            return "exclusion_not_loaded"
        return getattr(self.guard, "unavailable", None) or (
            None if callable(getattr(self.guard, "unit_clear", None))
            else "exclusion_not_loaded")

    def load(self, ids: Iterable[str]) -> None:
        want = sorted({i for i in ids if i not in self._info})
        for start in range(0, len(want), 500):
            part = json.dumps(want[start:start + 500])
            for oid, key, marks in self.conn.execute(
                    "SELECT o.object_id, o.audience_key, v.markings FROM objects o "
                    "LEFT JOIN object_versions v ON v.object_id = o.object_id "
                    "AND v.superseded_at IS NULL "
                    "WHERE o.object_id IN (SELECT value FROM json_each(?))", (part,)):
                self._info[oid] = (key, _marks(marks))

    def audience(self, oid: str) -> str | None:
        self.load([oid])
        info = self._info.get(oid)
        return info[0] if info else None

    def visible(self, oid: str) -> bool:
        if oid in self._seen:
            return self._seen[oid]
        self.load([oid])
        info = self._info.get(oid)
        ok = False
        if info is not None and self.unavailable is None:
            key, marks = info
            try:
                ok = (bool(self.guard.unit_clear(key)) and bool(self.may_read(key))
                      and marks is not None and marks <= self.held)
            except Exception:  # noqa: BLE001 - an undecidable record is withheld
                ok = False
        self._seen[oid] = ok
        return ok

    def shows(self, cited: Iterable[str], markings: Iterable[str] = ()) -> bool:
        return frozenset(markings) <= self.held and all(self.visible(o) for o in cited)


def _marks(raw: Any) -> frozenset[str] | None:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not all(isinstance(m, str) for m in parsed):
        return None
    return frozenset(parsed)


# --------------------------------------------------------------------------- documents

@dataclass
class Doc:
    object_id: str
    object_type: str
    handle: str
    name: str
    day: date | None = None
    message: str | None = None          # the Email it came with
    sender: str | None = None           # its sender address (lower case)
    recipients: list[str] = field(default_factory=list)   # its To addresses (lower case)
    parties: dict[str, list[str]] = field(default_factory=dict)   # customer/supplier ids
    text: str | None = None

    @property
    def cited(self) -> list[str]:
        return [self.object_id] + ([self.message] if self.message else [])


@dataclass
class Found:
    docs: list[Doc]
    complete: bool


def fts_query(words: Sequence[str]) -> str:
    """An FTS5 query any of ``words`` matches (a word ending in ``*`` a prefix); words are
    lower-case letters and digits only, so the query never carries a phrase or syntax."""
    terms = []
    for word in words:
        bare = re.sub(r"[^a-z0-9]", "", word.lower())
        if bare:
            terms.append(bare + ("*" if word.endswith("*") else ""))
    return " OR ".join(dict.fromkeys(terms))


def find_documents(conn: sqlite3.Connection, words: Sequence[str], *,
                   types: Sequence[str] = DOC_TYPES, props: Sequence[str] = NAME_PROPS,
                   limit: int = MAX_DOCS, require: Callable[[str], bool] | None = None
                   ) -> Found:
    """Documents of ``types`` whose current ``props`` value holds one of ``words``
    (module doc); ``require``: a further test of the matched name."""
    query = fts_query(words)
    if not query:
        return Found([], True)
    hits: dict[str, tuple[str, str, str]] = {}
    complete = True
    try:
        rows = conn.execute(
            "SELECT v.object_id, o.object_type, v.l1_handle, f.property, c.value "
            "FROM value_text vt "
            "JOIN value_cells c ON c.rowid = vt.rowid JOIN value_sources s ON s.src = c.src "
            "JOIN object_versions v ON v.rowid = s.ver JOIN value_fields f ON f.field = c.field "
            "JOIN objects o ON o.object_id = v.object_id "
            "WHERE value_text MATCH ? AND v.superseded_at IS NULL AND s.superseded_at IS NULL "
            "AND o.object_type IN (SELECT value FROM json_each(?)) "
            "AND f.property IN (SELECT value FROM json_each(?))",
            (query, json.dumps(list(types)), json.dumps(list(props))))
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc):
            raise   # the caller's time window is spent: never read as "no index"
        # no value word index here (a Postgres store without it, an old layout): the same
        # documents read whole, their names matched word by word as the index would
        rows = _scan_documents(conn, query, types, props)
    for oid, otype, handle, prop, value in rows:
        if oid in hits and prop != "name":
            continue
        text = str(value or "")
        if require is not None and not require(text):
            continue
        if oid not in hits and len(hits) >= limit:
            complete = False
            break
        hits[oid] = (otype, handle, text.rsplit("/", 1)[-1] if prop == "path" else text)
    removed = _removed(conn, list(hits))
    docs = [Doc(oid, t, h, n) for oid, (t, h, n) in sorted(hits.items()) if oid not in removed]
    _date_docs(conn, docs)
    return Found(docs, complete)


# The most word-matched cells a Postgres ``word_values`` read takes from the GIN index
# before its property and current-version tests, per row wanted (``word_values``).
WORD_SCAN_PER_ROW = 50


@dataclass
class WordValues:
    """``word_values``' rows: (object_id, property, value), and whether every match was
    read (False: cut at its cap; the caller says so, never "none")."""
    rows: list[tuple[str, str, str]]
    complete: bool


def word_values(conn: Any, query: str, properties: Sequence[str], *, prefix: str,
                limit: int) -> WordValues:
    """The current values of ``properties`` on objects whose id starts ``prefix``
    (``"Email:"``) whose words match ``query`` (an FTS5 query: ``fts_query``), at most
    ``limit``. On SQLite through the value word index (``value_text``); on Postgres
    through the value cells' GIN word index (``value_cells_words``, migration 0010) read
    first, then each cell's source and version, as native SQL: the translated FTS form
    joined the word rows to a computed row number and read every value cell (120 s and
    cancelled for a word matching 135 cells, 11-14). Raises ``sqlite3.OperationalError``
    when the index cannot be read (``"interrupted"``: the caller's time is spent); never
    an empty answer for a failed read."""
    props = sorted(set(properties))
    if not query or not props or limit <= 0:
        return WordValues([], True)
    if not getattr(conn, "postgres", False):
        rows = conn.execute(
            "SELECT v.object_id, f.property, c.value FROM value_text vt JOIN value_cells c "
            "ON c.rowid = vt.rowid JOIN value_sources s ON s.src = c.src JOIN "
            "object_versions v ON v.rowid = s.ver JOIN value_fields f ON f.field = c.field "
            "WHERE value_text MATCH ? AND v.superseded_at IS NULL AND f.property IN "
            "(SELECT value FROM json_each(?)) AND v.object_id LIKE ? ESCAPE '\\' LIMIT ?",
            (query, json.dumps(props), _like_prefix(prefix) + "%", limit + 1)).fetchall()
        out = [(str(o), str(p), str(v)) for o, p, v in rows]
        return WordValues(out[:limit], len(out) <= limit)
    return _pg_word_values(conn, query, props, prefix, limit)


def _like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# The word cells first, from their GIN index alone (the field test kept off its btree,
# ``+ 0``: the planner's BitmapAnd with it read up to 30x slower for a big field), then the
# kept cells' current sources and versions by their keys, a chunk of sources at a time. In
# one statement the planner sized its joins for the scan's cap and hashed every source and
# version (120 s and cancelled, 11-14).
_PG_WORD_CELLS = (
    "SELECT c.src, c.field, c.value FROM ontology.value_cells c "
    "WHERE c.value_num IS NULL AND c.value IS NOT NULL "
    "AND ontology.sq_words(c.value) @@ CAST(%(q)s AS tsquery) "
    "AND (c.field + 0) = ANY(%(fields)s::int[]) LIMIT %(scan)s")
_PG_CURRENT_OBJECTS = (
    "SELECT s.src, v.object_id FROM ontology.value_sources s "
    "JOIN ontology.object_versions v ON v.ver = s.ver "
    "WHERE s.src = ANY(%(srcs)s::bigint[]) AND s.superseded_at IS NULL "
    "AND v.superseded_at IS NULL AND v.object_id LIKE %(prefix)s ESCAPE '\\'")
_PG_SOURCE_CHUNK = 5000


def _pg_word_values(conn: Any, query: str, props: Sequence[str], prefix: str,
                    limit: int) -> WordValues:
    import psycopg
    from operating.pg.words import tsquery
    words = getattr(conn, "words", ())
    if words is not True and "value_text" not in (words or ()):
        raise sqlite3.OperationalError("no value word index on this Postgres store")
    q = tsquery(query)
    bounded = getattr(conn, "deadline_bounded", None)
    with bounded() if callable(bounded) else _no_deadline():
        named = {int(f): str(p) for f, p in conn.execute(
            "SELECT field, property FROM value_fields WHERE property IN (SELECT value FROM "
            "json_each(?))", (json.dumps(list(props)),))}
        if not named:
            return WordValues([], True)
        scan = max(1000, limit * WORD_SCAN_PER_ROW)
        with conn._lock:
            pg = conn._pg
            try:
                cells = pg.execute(_PG_WORD_CELLS, {"q": q, "fields": sorted(named),
                                                    "scan": scan}).fetchall()
                owner: dict[int, str] = {}
                srcs = sorted({int(src) for src, _f, _v in cells})
                for at in range(0, len(srcs), _PG_SOURCE_CHUNK):
                    owner.update(pg.execute(_PG_CURRENT_OBJECTS, {
                        "srcs": srcs[at:at + _PG_SOURCE_CHUNK],
                        "prefix": _like_prefix(prefix) + "%"}).fetchall())
            except psycopg.errors.QueryCanceled as exc:
                raise sqlite3.OperationalError("interrupted") from exc
            except psycopg.Error as exc:
                raise sqlite3.OperationalError(
                    f"postgres: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
                ) from exc
    out = [(owner[int(src)], named[int(field)], str(value)) for src, field, value in cells
           if int(src) in owner]
    out.sort()
    return WordValues(out[:limit], len(out) <= limit and len(cells) < scan)


def _scan_documents(conn: sqlite3.Connection, query: str, types: Sequence[str],
                    props: Sequence[str]) -> Iterable[tuple[Any, ...]]:
    """``find_documents``' rows without the word index: every current ``props`` value of a
    document of ``types`` whose words (lower-case letters and digits, as the index splits
    them) hold a term of ``query`` (an ``fts_query``: terms joined by OR, ``*`` a prefix)."""
    terms = [t for t in query.split(" OR ") if t]
    exact = {t for t in terms if not t.endswith("*")}
    prefixes = tuple(t[:-1] for t in terms if t.endswith("*"))
    if not terms:
        return
    if getattr(conn, "postgres", False):
        # Postgres keeps only the names holding a term (terms are [a-z0-9] only, so a LIKE
        # pattern carries no wildcard of its own; the words are still matched below, so the
        # rows are exactly a whole read's). The cells of the fields the name index holds
        # (``operating.pg.document_names``: a trigram index of the name and path cells) are
        # found through it, the fields named as literals so the planner proves the partial
        # index applies; a field added since it was built is read by its own btree. Without
        # it every name cell was read: 25-95 s a call on the served store (2026-11-13).
        # Each statement ends within the time the caller's SQL deadline left when the read
        # began (``deadline_bounded``): a tool call past its limit is interrupted, never
        # read on for minutes after nobody waits.
        from operating.pg import document_names as dn
        bounded = getattr(conn, "deadline_bounded", None)
        with bounded() if callable(bounded) else _no_deadline():
            fields = sorted(int(row[0]) for row in conn.execute(
                "SELECT field FROM value_fields WHERE property IN (SELECT value FROM "
                "json_each(?))", (json.dumps(list(props)),)))
            try:
                covered = dn.predicate_fields(next(iter(conn.execute(dn.COVERED_SQL)),
                                                   (None,))[0]) or frozenset()
            except sqlite3.OperationalError as exc:
                if "interrupt" in str(exc):
                    raise
                covered = frozenset()   # the catalog unreadable: every field by its btree
            parts = [part for part in ([f for f in fields if f in covered],
                                       [f for f in fields if f not in covered]) if part]
            rows = []
            for part in parts:
                sql = ("SELECT v.object_id, o.object_type, v.l1_handle, f.property, c.value "
                       "FROM objects o JOIN object_versions v ON v.object_id = o.object_id "
                       "JOIN value_sources s ON s.ver = v.rowid AND s.superseded_at IS NULL "
                       "JOIN value_cells c ON c.src = s.src "
                       "JOIN value_fields f ON f.field = c.field "
                       "WHERE o.object_type IN (SELECT value FROM json_each(?)) "
                       "AND c.field IN (" + ", ".join(str(f) for f in part) + ") AND ("
                       + " OR ".join("lower(c.value) LIKE ?" for _ in terms) + ")")
                rows.extend(conn.execute(sql, (json.dumps(list(types)),
                                               *(f"%{t.rstrip('*')}%" for t in terms))))
    else:
        rows = conn.execute(
            "SELECT v.object_id, o.object_type, v.l1_handle, v.property, v.value "
            "FROM object_values v JOIN objects o ON o.object_id = v.object_id "
            "WHERE v.superseded_at IS NULL "
            "AND o.object_type IN (SELECT value FROM json_each(?)) "
            "AND v.property IN (SELECT value FROM json_each(?))",
            (json.dumps(list(types)), json.dumps(list(props))))
    for oid, otype, handle, prop, value in rows:
        found = re.findall(r"[a-z0-9]+", str(value or "").lower())
        if any(w in exact or (prefixes and w.startswith(prefixes)) for w in found):
            yield oid, otype, handle, prop, value


@contextmanager
def _no_deadline() -> Iterator[None]:
    yield


def _removed(conn: sqlite3.Connection, ids: Sequence[str]) -> set[str]:
    from ontology import store as ostore
    out: set[str] = set()
    for start in range(0, len(ids), 500):
        part = json.dumps(sorted(ids)[start:start + 500])
        try:
            out.update(r[0] for r in conn.execute(
                "SELECT o.object_id FROM objects o WHERE o.object_id IN "
                "(SELECT value FROM json_each(?)) AND " + ostore.removed_at_sql("o.object_id"),
                (part,)))
        except sqlite3.Error:
            continue
    return out


def links_from(conn: sqlite3.Connection, ids: Iterable[str], link_type: str
               ) -> dict[str, list[str]]:
    """from_object -> its current, uncontradicted ``link_type`` targets (by the index)."""
    out: dict[str, list[str]] = defaultdict(list)
    for oid in sorted(set(ids)):
        for (to,) in conn.execute(
                "SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? "
                "AND superseded_at IS NULL AND tier <> 'contradicted'", (oid, link_type)):
            out[oid].append(to)
    return out


def links_to(conn: sqlite3.Connection, ids: Iterable[str], link_type: str
             ) -> dict[str, list[str]]:
    """to_object -> its current, uncontradicted ``link_type`` sources (by the index)."""
    out: dict[str, list[str]] = defaultdict(list)
    for oid in sorted(set(ids)):
        for (frm,) in conn.execute(
                "SELECT from_object FROM link_rows WHERE to_object = ? AND link_type = ? "
                "AND superseded_at IS NULL AND tier <> 'contradicted'", (oid, link_type)):
            out[oid].append(frm)
    return out


def values(conn: sqlite3.Connection, ids: Iterable[str], props: Sequence[str]
           ) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """object -> property -> its current value rows (value, unit, currency, markings)."""
    out: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    want = sorted(set(ids))
    for start in range(0, len(want), 400):
        for oid, prop, value, unit, cur, marks, handle in conn.execute(
                "SELECT object_id, property, value, unit, currency, markings, l1_handle "
                "FROM object_values WHERE superseded_at IS NULL AND object_id IN "
                "(SELECT value FROM json_each(?)) AND property IN (SELECT value FROM "
                "json_each(?))", (json.dumps(want[start:start + 400]), json.dumps(list(props)))):
            out[oid][prop].append({"value": value, "unit": unit, "currency": cur,
                                   "markings": marks, "handle": handle})
    return out


def first(rows: Mapping[str, list[dict[str, Any]]] | None, *props: str) -> Any:
    for prop in props:
        for row in (rows or {}).get(prop) or ():
            if row["value"] not in (None, "", "None", "null"):
                return row["value"]
    return None


def day_of(raw: Any) -> date | None:
    text = str(raw or "").strip()
    if not text or text in ("None", "null"):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")[:32]).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _date_docs(conn: sqlite3.Connection, docs: list[Doc]) -> None:
    members = [d for d in docs if d.object_type == "ContainerMember"]
    parent = links_from(conn, [d.object_id for d in members], IN_ATTACHMENT)
    att_of = {d.object_id: (parent.get(d.object_id) or [None])[0] for d in members}
    atts = [d.object_id for d in docs if d.object_type == "Attachment"] + \
        [a for a in att_of.values() if a]
    mail_of = links_from(conn, atts, ATTACHED_TO)
    messages = {a: ms[0] for a, ms in mail_of.items() if len(ms) == 1}
    sent = values(conn, set(messages.values()), ("sent_at", "received_at", "sender",
                                                      "to_addresses"))
    parties: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for kind, lt in SENT_BY.items():
        for m, who in links_from(conn, set(messages.values()), lt).items():
            parties[m][kind] = sorted(set(who))
    files = values(conn, [d.object_id for d in docs if d.object_type == "SharePointFile"],
                   ("last_modified", "created_at"))
    for d in docs:
        att = d.object_id if d.object_type == "Attachment" else att_of.get(d.object_id)
        msg = messages.get(att) if att else None
        if msg:
            d.message = msg
            d.day = day_of(first(sent.get(msg), "sent_at", "received_at"))
            d.parties = dict(parties.get(msg) or {})
            sender = str(first(sent.get(msg), "sender") or "").strip().lower()
            d.sender = sender if "@" in sender else None
            raw = str(first(sent.get(msg), "to_addresses") or "")
            d.recipients = sorted({a.lower() for a in re.findall(
                r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", raw)})
        elif d.object_type == "SharePointFile":
            d.day = day_of(first(files.get(d.object_id), "last_modified", "created_at"))


def _page_of(citation: Any, handle: str) -> int | None:
    """The page number of a page citation of the version ``handle``; None otherwise."""
    prefix = handle + "#page="
    if not isinstance(citation, str) or not citation.startswith(prefix):
        return None
    tail = citation[len(prefix):]
    return int(tail) if tail.isdigit() else None


def texts(conn: sqlite3.Connection, docs: Sequence[Doc], *, path: str | None = TEXT_PATH,
          max_chars: int = 400_000) -> None:
    """Each document's captured text (module doc), set on ``Doc.text`` (None: none);
    ``path`` None: every text field of the version, one after another."""
    from ontology.text_index import chunk_text

    for d in docs:
        # a version's text is cited to the version (its handle) or, read by OCR page by
        # page, to each page (``<handle>#page=<n>``, ``version_handle`` the version): the
        # whole-version fields when there are any, else the pages in page order
        found: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
        # two reads, each on the citation index: the version's own citation first, its
        # page citations only when it has none (an OR would lose the index)
        sql = ("SELECT f.field_path, f.citation, k.start, k.body FROM text_fields f "
               "JOIN text_chunks k ON k.field_id = f.field_id WHERE {cite} "
               "AND f.object_id = ? AND (? IS NULL OR f.field_path = ?) "
               "ORDER BY f.field_path, k.start")
        prefix = d.handle + "#page="
        try:
            for cite, params in (("f.citation = ?", (d.handle,)),
                                 ("f.citation >= ? AND f.citation < ?",
                                  (prefix, prefix + "\uffff"))):
                for fpath, citation, start, body in conn.execute(
                        sql.format(cite=cite), (*params, d.object_id, path, path)):
                    found[(fpath, citation)].append((start, chunk_text(body)))
                if found:
                    break
        except (sqlite3.Error, ValueError):
            found = defaultdict(list)
        whole = {k: v for k, v in found.items() if k[1] == d.handle}
        if whole:
            chosen = sorted(whole.items())
        else:
            paged = [(k, v) for k, v in found.items()
                     if _page_of(k[1], d.handle) is not None]
            chosen = sorted(paged, key=lambda kv: (kv[0][0], _page_of(kv[0][1], d.handle)))
        fields = {key: parts for key, parts in chosen}
        if not fields:
            d.text = None
            continue
        out, total = [], 0
        for parts in fields.values():
            end = 0
            for start, body in parts:
                if start + len(body) <= end:
                    continue
                piece = body[max(0, end - start):]
                out.append(piece)
                total += len(piece)
                end = start + len(body)
                if total >= max_chars:
                    break
            out.append("\n")
            if total >= max_chars:
                break
        d.text = "".join(out).rstrip("\n")


# --------------------------------------------------------------------------- tables

@dataclass
class Cell:
    value: str
    sheet: str
    address: str


@dataclass
class Table:
    sheet: str
    header_row: int
    roles: dict[str, int]                 # role -> column
    periods: dict[int, tuple[int, int]]   # column -> (year, month)
    headers: dict[int, str]
    rows: list[dict[int, Cell]]
    year_inferred: bool = False

    def cell(self, row: Mapping[int, Cell], role: str) -> Cell | None:
        col = self.roles.get(role)
        got = row.get(col) if col is not None else None
        return got if got is not None and got.value.strip() else None


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _letters(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def grids(text: str) -> list[tuple[str, dict[int, dict[int, str]]]]:
    """The text's sheets as (name, row -> column -> value)."""
    sheets: list[tuple[str, dict[int, dict[int, str]]]] = []
    current: dict[int, dict[int, str]] | None = None
    plain: list[list[str]] = []
    for line in (text or "").splitlines():
        line = line.rstrip()
        head = _SHEET.match(line)
        if head:
            current = {}
            sheets.append((head.group(1), current))
            continue
        cells = line.split(" | ")
        parsed = [_CELL.match(c.strip()) for c in cells]
        if line and all(parsed):
            if current is None:
                current = {}
                sheets.append(("", current))
            for m in parsed:
                value = m.group(3)
                f = _FORMULA.match(value)
                if f:
                    value = "" if f.group(1) == "(missing)" else f.group(1)
                current.setdefault(int(m.group(2)), {})[_col(m.group(1))] = value.strip()
            continue
        for sep in ("\t", ";", ","):
            fields = [p.strip().strip('"') for p in line.split(sep)]
            if len(fields) >= 3:
                plain.append(fields)
                break
    if plain and not sheets:
        sheets.append(("", {i + 1: {j + 1: v for j, v in enumerate(r)}
                            for i, r in enumerate(plain)}))
    return sheets


def _norm(text: str) -> str:
    return " ".join(_WORD.findall(str(text).lower().replace("_", " ")))


def role_of(header: str) -> str | None:
    h = _norm(header)
    if not h or len(h) > 40:
        return None
    for role, words in ROLES.items():
        if h in words:
            return role
    for role, words in ROLES.items():
        if any(len(w) > 3 and (h.startswith(w + " ") or h.endswith(" " + w)) for w in words):
            return role
    return None


def month_of(header: str, year_hint: int | None = None) -> tuple[int, int, bool] | None:
    """A period header as (year, month, year_inferred): ``Jan``, ``January 2026``,
    ``Jan-26``, ``2026-01``, ``2026-01-01``, ``01/2026``; None when it is none of these."""
    h = str(header).strip().lower()
    m = _MONTH.match(h)
    if not m:
        return None
    if m.group("y1"):
        year, month = int(m.group("y1")), int(m.group("m1"))
    elif m.group("m3"):
        year, month = int(m.group("y3")), int(m.group("m3"))
    else:
        name = m.group("mn")
        month = next((n for k, n in MONTHS.items() if name.startswith(k)
                      and (len(name) == 3 or name in _FULL_MONTHS or name == "sept")), None)
        if month is None:
            return None
        raw = m.group("y2")
        if raw:
            year = int(raw) + (2000 if len(raw) == 2 else 0)
        elif year_hint:
            return (year_hint, month, True) if 1 <= month <= 12 else None
        else:
            return None
    return (year, month, False) if 1 <= month <= 12 and 2000 <= year <= 2100 else None


_FULL_MONTHS = frozenset({"january", "february", "march", "april", "may", "june", "july",
                          "august", "september", "october", "november", "december"})


def tables(text: str, *, year_hint: int | None = None) -> list[Table]:
    """The tables of a document's text (module doc, Tables)."""
    out = []
    for sheet, grid in grids(text):
        if not grid:
            continue
        hint = year_hint
        found = _YEAR.search(sheet)
        if found:
            hint = int(found.group(1))
        best: tuple[int, int] | None = None
        for number in sorted(grid)[:HEADER_ROWS]:
            row = grid[number]
            roles = {role_of(v) for v in row.values()} - {None}
            periods = sum(1 for v in row.values() if month_of(v, hint))
            score = len(roles) + min(periods, 3)
            if score >= 2 and (best is None or score > best[1]):
                best = (number, score)
        if best is None:
            continue
        number = best[0]
        header = grid[number]
        roles: dict[str, int] = {}
        periods: dict[int, tuple[int, int]] = {}
        inferred = False
        for col in sorted(header):
            month = month_of(header[col], hint)
            if month is not None:
                periods[col] = month[:2]
                inferred = inferred or month[2]
                continue
            role = role_of(header[col])
            if role and role not in roles:
                roles[role] = col
        rows = []
        for n in sorted(grid):
            if n <= number or len(rows) >= MAX_ROWS:
                continue
            rows.append({c: Cell(v, sheet, f"{_letters(c)}{n}") for c, v in grid[n].items()
                         if v != ""})
        out.append(Table(sheet, number, roles, periods, {c: v for c, v in header.items()},
                         [r for r in rows if r], inferred))
    return out


def number(raw: Any) -> Decimal | None:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return -value if negative else value


def money(raw: Any) -> tuple[Decimal, str | None] | None:
    """A price cell as (amount, currency code or None); None when it is no amount."""
    m = _MONEY.match(str(raw or "").strip())
    if not m:
        return None
    amount = number(m.group("num"))
    if amount is None:
        return None
    sym = (m.group("cur") or m.group("cur2") or "").upper()
    code = {"US$": "USD", "C$": "CAD", "CA$": "CAD", "€": "EUR", "£": "GBP"}.get(sym, sym)
    return amount, (code if code in CURRENCIES else None)


def stated_currency(text: str) -> str | None:
    """The one currency code a document states (ISO code words); none or several: None."""
    found = {w for w in re.findall(r"\b([A-Z]{3})\b", text or "") if w in CURRENCIES}
    return next(iter(found)) if len(found) == 1 else None


# --------------------------------------------------------------------------- records

def domain_of(address: Any) -> str | None:
    text = str(address or "").strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    else:
        text = re.sub(r"^[a-z]+://", "", text).split("/", 1)[0]
        text = text[4:] if text.startswith("www.") else text
    text = text.strip(" .>")
    return text if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", text) else None


class Parties:
    """Sender address -> the one customer or supplier identity whose own record carries an
    address or web site of that domain (``PARTY_EMAIL_PROPS``); a public mail domain, one
    of the tenant's own, or a domain of several identities names nobody (conservative)."""

    def __init__(self, conn: sqlite3.Connection, object_type: str, twins: str | None,
                 internal: Iterable[str] = ()) -> None:
        self.conn, self.twins = conn, twins
        self.internal = frozenset(d.lower() for d in internal)
        self.by_domain: dict[str, set[str]] = defaultdict(set)
        ids = [r[0] for r in conn.execute(
            "SELECT object_id FROM objects WHERE object_type = ?", (object_type,))]
        names = values(conn, ids, ("name",))
        # a record its own name retires ("do not use") is no party anyone sends as
        self.retired = {oid for oid, props in names.items() if RETIRED.search(
            str(first(props, "name") or ""))}
        for oid, props in values(conn, ids, PARTY_EMAIL_PROPS).items():
            if oid in self.retired:
                continue
            for rows in props.values():
                for row in rows:
                    for part in re.split(r"[;, ]+", str(row["value"] or "")):
                        dom = domain_of(part)
                        if dom and not self._skip(dom):
                            self.by_domain[dom].add(oid)
        self._memo: dict[str, str | None] = {}
        self._ids = ids
        self._names: dict[str, set[str]] | None = None

    def members(self, oid: str) -> set[str]:
        """The records of one party: its settled twins, and the records of its type in
        other systems carrying exactly its normalized name (six characters or more)."""
        from ontology.identity_scoring import normalize_name
        if self._names is None:
            self._names = defaultdict(set)
            for rid, props in values(self.conn, self._ids, ("name",)).items():
                said = normalize_name(first(props, "name") or "")
                if len(said.replace(" ", "")) >= 6:
                    self._names[said].add(rid)
        out = cluster(self.conn, oid, self.twins)
        for rid in sorted(out):
            said = normalize_name(first(values(self.conn, [rid], ("name",)).get(rid), "name")
                                  or "")
            if len(said.replace(" ", "")) >= 6:
                out |= self._names.get(said, set())
        return out

    def _skip(self, dom: str) -> bool:
        return dom in PUBLIC_DOMAINS or any(dom == d or dom.endswith("." + d)
                                            for d in self.internal)

    def internal_address(self, address: str | None) -> bool:
        dom = domain_of(address)
        return bool(dom) and any(dom == d or dom.endswith("." + d) for d in self.internal)

    def of(self, address: str | None) -> str | None:
        dom = domain_of(address)
        if not dom or self._skip(dom):
            return None
        if dom not in self._memo:
            ids = sorted(self.by_domain.get(dom) or set())[:20]
            roots = sorted({root(self.conn, i, self.twins) for i in ids})
            if len(roots) > 1:
                # accounts of one party in several companies or ship-tos: the same name
                # (normalized) and the same domain are one party; any other name: nobody
                from ontology.identity_scoring import normalize_name
                names = values(self.conn, roots, ("name",))
                said = {normalize_name(first(names.get(r), "name") or "") for r in roots}
                roots = roots[:1] if len(said) == 1 and "" not in said else roots
            self._memo[dom] = roots[0] if len(roots) == 1 else None
        return self._memo[dom]


class Resolver:
    """Cell value -> the records it identifies (module doc, Records)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._memo: dict[tuple[str, tuple[str, ...]], list[str]] = {}

    def __call__(self, raw: Any, types: Sequence[str]) -> list[str]:
        token = re.sub(r"\s+", "", str(raw or "")).upper()
        if len(token) < 3 or len(token) > 60:
            return []
        key = (token, tuple(types))
        if key not in self._memo:
            ids = {oid for (oid,) in self.conn.execute(
                "SELECT DISTINCT object_id FROM identifiers WHERE normalized = ? "
                "AND superseded_at IS NULL", (token,))
                if oid.split(":", 1)[0] in types}
            self._memo[key] = sorted(ids)
        return self._memo[key]

    def group(self, raw: Any, types: Sequence[str], twins: str | None = None) -> list[str]:
        """Every record whose identifier the value is exactly (one item code in several
        systems or companies is one item), their settled twins joined; least id first."""
        ids = set(self(raw, types))
        for oid in sorted(ids):
            ids |= cluster(self.conn, oid, twins)
        return sorted(ids)

    def one(self, raw: Any, types: Sequence[str], twins: str | None = None) -> str | None:
        """The one identity a value names (twins joined by ``twins`` links), else None."""
        ids = self(raw, types)
        if not ids:
            return None
        roots = {root(self.conn, i, twins) if twins else i for i in ids}
        return next(iter(roots)) if len(roots) == 1 else None


def root(conn: sqlite3.Connection, oid: str, twins: str | None, cap: int = 50) -> str:
    """The least record id of ``oid``'s cluster over settled ``twins`` links."""
    if not twins:
        return oid
    seen, todo = {oid}, [oid]
    while todo and len(seen) < cap:
        cur = todo.pop()
        for sql in ("SELECT to_object FROM link_rows WHERE from_object = ? AND link_type = ? "
                    "AND superseded_at IS NULL AND tier IN ('verified', 'reviewed', 'corroborated')",
                    "SELECT from_object FROM link_rows WHERE to_object = ? AND link_type = ? "
                    "AND superseded_at IS NULL AND tier IN ('verified', 'reviewed', 'corroborated')"):
            for (other,) in conn.execute(sql, (cur, twins)):
                if other not in seen:
                    seen.add(other)
                    todo.append(other)
    return min(seen)


def cluster(conn: sqlite3.Connection, oid: str, twins: str | None, cap: int = 50
            ) -> set[str]:
    if not twins:
        return {oid}
    seen, todo = {oid}, [oid]
    while todo and len(seen) < cap:
        cur = todo.pop()
        for (a, b) in conn.execute(
                "SELECT from_object, to_object FROM link_rows WHERE (from_object = ? OR "
                "to_object = ?) AND link_type = ? AND superseded_at IS NULL AND tier IN "
                "('verified', 'reviewed', 'corroborated')", (cur, cur, twins)):
            for other in (a, b):
                if other not in seen:
                    seen.add(other)
                    todo.append(other)
    return seen


__all__ = ["Cell", "Doc", "Found", "Gate", "Resolver", "Table", "cluster", "day_of",
           "find_documents", "first", "fts_query", "grids", "links_from", "links_to",
           "money", "month_of", "number", "role_of", "root", "stated_currency", "tables",
           "texts", "values"]
