"""What customers and suppliers send in sheets and documents, read for one reader:
``plx.aggregate`` mode ``business_documents`` with a ``view`` (all read only; nothing is
sent, written or changed; every row cites the records and sheet cells it comes from):

* ``forecasts``: customer forecasts (documents named with ``FORECAST_WORDS``) as dated lines
  per customer, item and month (a period column of the sheet, or a date column with a
  quantity), the customer being the one the message came from (``Email.sent_by_customer``);
  a sheet from no one known, or an item code naming no one product, is counted, never
  guessed. ``forecast_lines`` is the reusable reader (the demand forecast can take them).
* ``forecast_accuracy``: per customer and item, each forecast month that has ended scored
  against the customer's own orders of the item in that month (``SalesOrderLine`` ordered
  quantity of orders ``ordered_by`` the customer, its identity cluster joined, dated by the
  order date): weighted absolute percentage error ``wape`` (sum |forecast - actual| over
  sum actual) and ``bias``; the newest forecast for a month counts. Order lines in more
  than one unit are never summed (``mixed_units``); the sheet's unit is stated or not
  (``unit_basis``).
* ``priority_fill``: priority / fill lists (``PRIORITY_WORDS``) as ranked order numbers (a
  cell with a digit, five characters or more when digits only), each matched to its order
  (``identifiers`` of ``SalesOrder`` or ``CustomerPurchaseOrder``, a PO's orders by
  ``SalesOrder.references_customer_po``), its customer the order's (``ordered_by``) and its
  fill status: ``shipped`` (a ``Shipment.ships`` the order, or a done status), ``late`` (its
  ship or delivery date before the day asked), ``not_scheduled`` (no such date),
  ``scheduled``. ``alert`` is set on ``late`` and ``not_scheduled``. An order's newest
  listing is its current one; ``subject`` (a customer or order) narrows it.
* ``price_sheets``: the newest price sheet per customer and item (``PRICE_WORDS``; a price
  column), and every invoice line of that customer and item since the sheet's day compared
  with it (``SalesInvoiceLine`` net amount over invoiced quantity, one currency). A line
  more than ``PRICE_TOLERANCE`` off is a ``mismatch``. Finance-marked: a reader without
  ``finance`` gets no row (``withheld_finance``). A sheet stating no currency is compared
  only when the invoices are in one currency (``currency_basis: assumed_invoice``).
* ``subject`` on ``forecasts``, ``forecast_accuracy`` and ``price_sheets`` narrows them to
  one customer's rows (its identity cluster) or one item's (its code group); a subject of
  any other kind keeps no row (``subject_not_customer_or_item``). On ``supplier_issues`` a
  supplier or item subject takes its same-record twins too.
* ``compliance_pack`` (``subject``: an item or a lot): its certificates of analysis, specs,
  safety data sheets, allergen and other statements and certificates: documents whose name
  holds one of its codes (item code, lot number) and a document kind word (``PACK_KINDS``),
  newest first per kind, plus the ``CertificateOfAnalysis`` records naming the code.
* ``answer_bank`` (``question`` optional): past questionnaire answers (``QUESTIONNAIRE_WORDS``
  documents; a question/answer table, a ``label: value`` line, or a question line and the
  line after it), each cited; with a question, the answers ranked by word overlap as a
  draft to pre-fill (``draft: true``; never sent).
* ``supplier_issues``: raw-material and supplier quality issues (``ISSUE_WORDS``; a reject
  or defect only beside a material or quality word) from Planner tasks, mail subjects and
  Teams messages, linked to the item and lot codes they name and to the supplier (a
  supplier link, a named lot's receipt ``Receipt.supplied_by``, or the mail's sender when it
  supplies a named item through ``SupplierItem``). A mail from a supplier's domain that
  supplies none of the named items ``reported_by`` it (a lab, a broker), kept apart. Mail
  from no supplier's domain is not read. Per-supplier and per-reporter monthly trends.
* ``formula_lines`` (``subject``: a product): what its formula sheets say it is made of.
  Sheets are documents whose name holds a formula number or a formula word
  (``FORMULA_WORDS``) and at least ``FORMULA_NAME_WORDS`` of the product's name words; a
  sheet counts only when its stated ``CODE`` is one of the product's codes (``basis:
  code``) or, stating none, its ``NAME`` is the product's name (``basis: name``); a sheet
  stating another code is another product's (counted). Each sheet's lines are read by
  ``operating.composition_lines`` (part code, share %, quantity, the share x size check,
  the sheet ``complete`` or ``partial``, never filled in); a part code resolves to the one
  item its identifier names (``resolved``) or none (``unresolved``, counted), and the
  item's supplier items (``SupplierItem.for_item``) the reader may read are listed by id
  (prices stay on those records, under their own markings). The formula lines the Portal
  holds (FileMaker ``formula_item``, ``app_formulation_ingredient``) are not served, so
  every answer counts ``portal_formula_lines_not_served``; sheets are the only source.
* ``item_on_hand_sheets`` (``subject``: a product): the on-hand figure a hand-kept sheet
  states for it (``operating.proactive_sheets.item_readings``: the sheet's item number is
  one of the product's codes and no other item's), a separate source from the records' own
  stock and never added to it: one row per sheet at its latest dated reading with its
  quantity, unit, day, cell and ``fresh`` (within ``ON_HAND_FRESH_DAYS``), citing the sheet's
  record and the product. People first, as every view.
* ``item_po_prices`` (``items``: up to ``MAX_PRICE_ITEMS`` item codes, each with the cost a
  sheet states for it, or item records; ``currency``: the stated costs' currency): per item
  what its purchase orders paid: the last price (the newest priced line by its order's
  date) and the quantity-weighted average per currency and unit, exact Decimal, never
  across currencies or units; one order's copies in several systems
  (``PurchaseOrder.same_record_as``) counted once. A stated cost gets a verdict
  (``_cost_verdict``): a $0 cost a PO paid for is ``zero_but_bought``, a $0 cost no PO
  paid for ``zero_never_bought`` (as a customer-supplied item reads), else within
  ``COST_TOLERANCE`` of the last or the average price or ``differs``; a PO line the reader
  may not read makes it ``lines_withheld`` (that line could be the last price or the
  purchase), never a verdict. Each row cites the item and the last price's line and order
  and carries the prices' own markings. A code naming no item and one naming an item the
  reader may not read are both ``codes_not_found`` (no oracle); a code several items carry
  is ``codes_ambiguous``, never one item's prices.

People first, fail closed: without a people exclusion that is loaded and bound to the
tenant directory (``unit_clear``) every view is ``withheld: people_exclusion_unavailable``;
a record of a mailbox, chat, list or drive the exclusion does not clear is never read into
a row. A row shows only when the reader may read every record it cites and holds its
markings. No view names a customer, a supplier, an item or a sender in code. Fixture
runs are ``isolated-fixture``; live runs on the store are live-unverified.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from . import document_memo
from . import document_tables as dt
from .document_memo import StillReading, reader_key

TOOL_NAME = "plx.aggregate"
MODE = "business_documents"
FORMAT = "plx-business-documents/v1"
VIEWS = ("forecasts", "forecast_accuracy", "priority_fill", "price_sheets",
         "compliance_pack", "answer_bank", "supplier_issues",
         "formula_lines", "item_on_hand_sheets", "item_po_prices")
MAX_ROWS = 200
FINANCE = "finance"

FORECAST_WORDS = ("forecast*",)
PRIORITY_WORDS = ("priority", "priorities", "fill", "hotlist", "expedite*")
PRICE_WORDS = ("price*", "pricing")
QUESTIONNAIRE_WORDS = ("questionnaire*",)
ISSUE_WORDS = ("reject*", "deviation*", "complaint*", "ncr", "nonconform*", "quarantine*",
               "oos", "contaminat*", "defect*")
FORMULA_WORDS = ("formula*", "sample")
FORMULA_BUDGET_SECONDS = 35.0   # inside plx.aggregate's 45 s: past it, a labelled partial
FORMULA_NAME_WORDS = 3   # product name words a sheet's name must hold (all, when fewer)
_FORMULA_NUMBER = re.compile(r"(?i)\bF-\d{2}-\d{4}\b")
_NAME_STOP = frozenset({"the", "and", "for", "with", "of", "a", "an", "on", "in", "to"})
# a reject or a defect is a quality issue only beside a word of material or quality (a
# rejected bill or an approve/reject request is none)
GENERIC_ISSUE = ("reject*", "defect*")
QUALITY_CONTEXT = ("lot", "lots", "batch", "material", "materials", "raw", "receipt",
                   "received", "receiving", "pallet", "pallets", "quality", "qc", "qa",
                   "spec", "specs", "coa", "sample", "samples", "drum", "drums", "tote",
                   "totes", "bottle", "bottles", "pump", "pumps", "cap", "caps", "label",
                   "labels", "carton", "cartons", "container", "shipment", "product",
                   "ingredient", "component", "components", "packaging")
PACK_KINDS = {
    "coa": ("coa", "coas", "certificate of analysis", "c of a"),
    "spec": ("spec", "specs", "specification", "specifications", "tds"),
    "sds": ("sds", "msds", "safety data"),
    "allergen": ("allergen", "allergens", "gluten", "nut free"),
    "certificate": ("certificate", "cert", "kosher", "halal", "organic", "vegan", "ifra",
                    "gmp", "iso", "coc", "chain of custody", "statement", "declaration"),
}
PRICE_TOLERANCE = Decimal("0.02")
PRICE_WINDOW_DAYS = 365   # a sheet governs the invoices of the year after it
ORDER_LINE_ITEM = "SalesOrderLine.for_item"
ORDER_LINE_OF = "SalesOrderLine.line_of"
ORDERED_BY = "SalesOrder.ordered_by"
CUSTOMER_TWINS = "Customer.same_record_as"
INVOICE_LINE_ITEM = "SalesInvoiceLine.for_item"
INVOICE_LINE_OF = "SalesInvoiceLine.line_of"
BILLED_TO = "SalesInvoice.billed_to"
ITEM_TYPES = ("Product", "FgItem")
ITEM_TWINS = "Product.same_record_as"
PO_LINE_ITEM = "PurchaseOrderLine.for_item"
PO_LINE_OF = "PurchaseOrderLine.line_of"
PO_TWINS = "PurchaseOrder.same_record_as"
# A PO line's unit price, in the order its systems state it (Portal, BC, FileMaker).
PO_PRICE_PROPS = ("unit_price", "bc_direct_unit_cost", "fm_item_cost")
# A stated cost differs from a PO price past this share of the PO price.
COST_TOLERANCE = Decimal("0.05")
# Units a stated per-each cost can be compared with (a PO line stating none is compared
# and says so: ``uom_unstated``).
EACH_UNITS = frozenset({"ea", "each", "pc", "pcs", "piece", "pieces", "unit", "units", "u"})
MAX_PRICE_ITEMS = MAX_ROWS
ORDER_TYPES = ("SalesOrder", "CustomerPurchaseOrder")
_QUESTION = re.compile(r"\?\s*$")
_LABEL = re.compile(r"^([A-Za-z][A-Za-z0-9 /()._-]{1,65}): (.{1,150}) \[Sheet .*\]$")


class Answer:
    """Rows shown to one reader, with what was left out and why (counts only)."""

    def __init__(self, gate: dt.Gate, view: str) -> None:
        self.gate = gate
        self.view = view
        self.rows: list[dict[str, Any]] = []
        self.counts: Counter = Counter()
        self.withheld: Counter = Counter()
        self.complete = True
        self.reader: str | None = None   # the memo's reader key (``reader_key``)

    def add(self, row: dict[str, Any], cited: Sequence[str], markings: Sequence[str] = ()
            ) -> bool:
        if not self.gate.shows(cited, markings):
            reason = ("withheld_finance" if FINANCE in markings
                      and FINANCE not in self.gate.held else "withheld_not_readable")
            self.withheld[reason] += 1
            return False
        if len(self.rows) >= MAX_ROWS:
            self.counts["more_rows"] += 1
            self.complete = False
            return False
        row["cites"] = [_cite(self.gate, o) for o in dict.fromkeys(cited)]
        if markings:
            row["markings"] = sorted(set(markings))
        self.rows.append(row)
        return True

    def done(self, **extra: Any) -> dict[str, Any]:
        out = {"mode": MODE, "format": FORMAT, "view": self.view, "rows": self.rows,
               "counts": dict(sorted(self.counts.items())),
               "withheld_counts": dict(sorted(self.withheld.items())),
               "complete": self.complete, "draft": False}
        out.update(extra)
        return out


# ------------------------------------------------------------------- the reader's memo
# (``operating.document_memo``): each view's whole-company step kept per reader

MEMO = document_memo.Memo(lambda gate, view: Answer(gate, view))
# the views whose whole-company steps are kept (``MEMO``): a customer's or item's lists
KEPT_VIEWS = ("forecasts", "forecast_accuracy", "price_sheets", "answer_bank")


STILL_READING_NOTE = ("This list is still being read and is incomplete; do not say none were "
                      "found. Ask again in a minute.")


def _cite(gate: dt.Gate, oid: str) -> dict[str, Any]:
    return {"object_id": oid}


def _cell(c: dt.Cell | None) -> dict[str, str] | None:
    return None if c is None else {"sheet": c.sheet, "cell": c.address}


def _docs(conn, gate: dt.Gate, words: Sequence[str], ans: Answer, *,
          require: Callable[[str], bool] | None = None) -> list[dt.Doc]:
    """Documents of a family the reader may see, their text read (counted otherwise)."""
    found = dt.find_documents(conn, words, require=require)
    if not found.complete:
        ans.complete = False
        ans.counts["documents_cut"] += 1
    gate.load(o for d in found.docs for o in d.cited)
    kept = []
    for d in found.docs:
        if not all(gate.visible(o) for o in d.cited):
            ans.withheld["document_not_readable"] += 1
            continue
        kept.append(d)
    dt.texts(conn, kept)
    out = []
    for d in kept:
        if d.text is None:
            ans.counts["text_not_captured"] += 1
            continue
        out.append(d)
    ans.counts["documents"] += len(out)
    return out


def _words_in(name: str, words: Sequence[str]) -> bool:
    toks = set(re.findall(r"[a-z0-9]+", name.lower()))
    for w in words:
        if w.endswith("*"):
            if any(t.startswith(w[:-1]) for t in toks):
                return True
        elif w in toks:
            return True
    return False


# --------------------------------------------------------------------------- forecasts

def _customers(conn, gate: dt.Gate, ans: Answer | None = None) -> dt.Parties:
    def read(_scratch: Answer) -> dt.Parties:
        return dt.Parties(conn, "Customer", CUSTOMER_TWINS,
                          getattr(gate.guard, "internal", ())).unbound()
    # the kept value holds data only: each request reads through its own connection
    return MEMO.step(ans.reader, "customers", ans, read, conn).bound(conn) if ans is not None \
        else read(ans).bound(conn)


_FROM = re.compile(r"(?im)^\s*\*?From:\*?\s*(?:[^<\n]*<)?([\w.+'-]+@[\w-]+(?:\.[\w-]+)+)")
THREAD_MAIL = 300        # messages read per sender domain for thread evidence
COA_SCAN = 1000          # certificate lot / code cells read per code (more: the answer says so)
ISSUE_SCAN = 50000       # issue-word cells read per source (more: the answer says so)
THREAD_SHARE = 0.8       # the share of a domain's tied messages one customer must hold


class CustomerOf:
    """The customer a document comes from or was sent to, with its basis and evidence, in
    this order (the first that names exactly one customer wins; none: nobody):

    1. ``sender_link``: the message's own customer link (``Email.sent_by_customer``);
    2. the sender (for a message of ours forwarding one, the original sender of the
       forwarded header, ``forwarded_sender``; for ours sent out, its one external
       recipient, ``recipient``): the customer whose record carries the address domain
       (``*_domain``), else the customer the domain's other messages are tied to through
       their links to orders, customer POs, invoices or customers (``*_threads``: at least
       two messages and ``THREAD_SHARE`` of the tied ones, those messages cited);
    3. ``named_in_document``: the one customer whose full name (normalized, two words or
       more) the file name or the sheet's first lines carry.
    """

    def __init__(self, conn, gate: dt.Gate, ans: "Answer | None" = None) -> None:
        self.conn, self.gate, self.ans = conn, gate, ans
        self.parties = dt.Parties(conn, "Customer", CUSTOMER_TWINS,
                                  getattr(gate.guard, "internal", ()))
        self._threads: dict[str, tuple[str | None, list[str]]] = {}
        self._names: dict[str, set[str]] | None = None
        self._fwd: dict[str, str | None] = {}   # message -> its forwarded sender
        self._all_words: frozenset[str] | None = None
        self._people: frozenset[str] | None = None
        self._product_pairs: frozenset[str] = frozenset()
        self._roots: dict[str, str] = {}   # customer -> its cluster's root (this read)

    def _root(self, oid: str) -> str:
        """``dt.root`` over the customer twins, read once per customer per step."""
        got = self._roots.get(oid)
        if got is None:
            got = self._roots[oid] = dt.root(self.conn, oid, CUSTOMER_TWINS)
        return got

    def __call__(self, d: dt.Doc) -> tuple[str | None, str | None, list[str]]:
        conn, parties = self.conn, self.parties
        linked = sorted(set(d.parties.get("customer") or []))
        if linked:
            roots = {self._root(c) for c in linked}
            return (next(iter(roots)), "sender_link", []) if len(roots) == 1 else \
                (None, None, [])
        tries: list[tuple[str, str]] = []
        if d.sender and not parties.internal_address(d.sender):
            tries.append((d.sender, "sender"))
        elif d.sender:
            forwarded = self._forwarded(d)
            if forwarded:
                tries.append((forwarded, "forwarded_sender"))
            outside = sorted({a for a in d.recipients if not parties.internal_address(a)})
            if len({dt.domain_of(a) for a in outside}) == 1:
                tries.append((outside[0], "recipient"))
        for address, basis in tries:
            who = parties.of(address)
            if who:
                return who, basis + "_domain", []
            who, evidence = self._by_threads(dt.domain_of(address))
            if who:
                return who, basis + "_threads", evidence
        who = self._named(d)
        return (who, "named_in_document", []) if who else (None, None, [])

    def _forwarded(self, d: dt.Doc) -> str | None:
        """The first outside address of a From: line in the message's own text."""
        if not d.message:
            return None
        if d.message not in self._fwd:
            self._fwd[d.message] = self._read_forwarded(d.message)
        return self._fwd[d.message]

    def _read_forwarded(self, message: str) -> str | None:
        d = dt.Doc(message, "Email", "", "", message=message)
        row = self.conn.execute("SELECT l1_handle FROM object_versions WHERE object_id = ? "
                                "AND superseded_at IS NULL", (d.message,)).fetchone()
        text = str(dt.first(dt.values(self.conn, [d.message], ("body_preview",)).get(
            d.message), "body_preview") or "")
        if row:
            msg = dt.Doc(d.message, "Email", row[0], "")
            dt.texts(self.conn, [msg], path=None, max_chars=8_000)
            text += "\n" + (msg.text or "")
        for found in _FROM.findall(text):
            if not self.parties.internal_address(found.lower()):
                return found.lower()
        return None

    def _by_threads(self, domain: str | None) -> tuple[str | None, list[str]]:
        if not domain or self.parties._skip(domain):
            return None, []
        if domain not in self._threads:
            self._threads[domain] = self._read_threads(domain)
        return self._threads[domain]

    def _read_threads(self, domain: str) -> tuple[str | None, list[str]]:
        conn = self.conn
        token = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
        if len(token) < 3:
            return None, []
        mails = []
        try:
            senders = dt.word_values(conn, token, ["sender"], prefix="Email:",
                                     limit=THREAD_MAIL * 4).rows
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc):
                raise
            # the value word index unreadable: no thread evidence, so the threads name
            # nobody (never a guess), and the answer says it was not read
            if self.ans is not None:
                self.ans.counts["sender_threads_not_read"] += 1
                self.ans.complete = False
            return None, []
        for oid, _prop, value in senders:
            if dt.domain_of(value) == domain and self.gate.visible(oid):
                mails.append(oid)
            if len(mails) >= THREAD_MAIL:
                break
        votes: dict[str, list[str]] = defaultdict(list)
        links: dict[str, list[str]] = defaultdict(list)   # the mails' links, one read
        for start in range(0, len(mails), 500):
            part = mails[start:start + 500]
            for frm, to in conn.execute(
                    "SELECT from_object, to_object FROM link_rows WHERE from_object IN (%s) "
                    "AND superseded_at IS NULL AND tier <> 'contradicted'"
                    % ",".join("?" * len(part)), tuple(part)):
                links[frm].append(to)
        for mail in mails:
            whose = set()
            for to in links.get(mail, ()):
                kind = to.split(":", 1)[0]
                if kind == "Customer":
                    whose.add(to)
                elif kind == "SalesOrder":
                    whose.update(dt.links_from(conn, [to], ORDERED_BY).get(to, []))
                elif kind == "SalesInvoice":
                    whose.update(dt.links_from(conn, [to], BILLED_TO).get(to, []))
                elif kind == "CustomerPurchaseOrder":
                    for so in dt.links_to(conn, [to], "SalesOrder.references_customer_po"
                                          ).get(to, []):
                        whose.update(dt.links_from(conn, [so], ORDERED_BY).get(so, []))
            for c in {self._root(c) for c in whose}:
                if c not in self.parties.retired:
                    votes[c].append(mail)
        if not votes:
            return None, []
        best = max(votes, key=lambda c: (len(votes[c]), c))
        tied = sum(len(v) for v in votes.values())
        if len(votes[best]) >= 2 and len(votes[best]) >= THREAD_SHARE * tied:
            return best, sorted(votes[best])
        return None, []

    def names_in(self, text: str) -> set[str]:
        """The customers whose full normalized name (two words or more) ``text`` carries."""
        from ontology.identity_scoring import normalize_name
        self._load_names()
        words = normalize_name(text).split()
        found = set()
        for size in range(2, 7):
            for i in range(len(words) - size + 1):
                ids = self._names.get(" ".join(words[i:i + size]))
                if ids:
                    found.add(min(self._root(x) for x in ids))
        return found

    def name_words(self, oid: str, every: bool = False) -> set[str]:
        """The words of a customer's own name, lower case (``every``: legal forms too)."""
        name = str(dt.first(dt.values(self.conn, [oid], ("name",)).get(oid), "name") or "")
        words = set(re.findall(r"[a-z0-9]+", name.lower()))
        return words if every else words - _NAME_NOISE

    def all_name_words(self) -> set[str]:
        """Every word of every customer's name (not retired), legal forms aside."""
        if self._all_words is None:
            ids = [r[0] for r in self.conn.execute(
                "SELECT object_id FROM objects WHERE object_type = 'Customer'")]
            words: set[str] = set()
            for oid, props in dt.values(self.conn, ids, ("name",)).items():
                if oid not in self.parties.retired:
                    words |= set(re.findall(r"[a-z0-9]+", str(dt.first(props, "name")
                                                               or "").lower()))
            self._all_words = frozenset(w for w in words - _NAME_NOISE if len(w) > 1)
        return set(self._all_words)

    def names_a_person(self, text: str) -> bool:
        """True when ``text`` carries a person's name: a contact's or person's full name
        from the store (case-insensitive), or a capitalised First Last pair that is no
        product's name words and no standard term (``_STANDARD_PAIRS``)."""
        if self._people is None:
            people: set[str] = set()
            products: set[str] = set()
            for kind, into in (("Contact", people), ("Person", people),
                               ("Product", products)):
                ids = [r[0] for r in self.conn.execute(
                    "SELECT object_id FROM objects WHERE object_type = ?", (kind,))]
                for props in dt.values(self.conn, ids, ("name", "display_name",
                                                        "full_name")).values():
                    for prop in ("name", "display_name", "full_name"):
                        words = re.findall(r"[a-z]+", str(dt.first(props, prop) or "").lower())
                        if kind == "Product":
                            into.update(f"{a} {b}" for a, b in zip(words, words[1:]))
                        elif len(words) >= 2:
                            into.add(" ".join(words))
            self._people, self._product_pairs = frozenset(people), frozenset(products)
        words = re.findall(r"[a-z]+", text.lower())
        for size in (2, 3, 4):
            if any(" ".join(words[i:i + size]) in self._people
                   for i in range(len(words) - size + 1)):
                return True
        for first, last in _PAIR.findall(text):
            pair = f"{first} {last}".lower()
            if pair not in _STANDARD_PAIRS and pair not in self._product_pairs:
                return True
        return False

    def _load_names(self) -> None:
        from ontology.identity_scoring import normalize_name
        if self._names is None:
            self._names = defaultdict(set)
            ids = [r[0] for r in self.conn.execute(
                "SELECT object_id FROM objects WHERE object_type = 'Customer'")]
            for oid, props in dt.values(self.conn, ids, ("name",)).items():
                if oid in self.parties.retired:
                    continue
                said = normalize_name(dt.first(props, "name") or "")
                if len(said.split()) >= 2 and len(said) >= 8:
                    self._names[said].add(oid)

    def _named(self, d: dt.Doc) -> str | None:
        found = self.names_in(re.sub(r"[._-]+", " ", d.name) + " \n " +
                              "\n".join((d.text or "").splitlines()))
        return next(iter(found)) if len(found) == 1 else None


def forecast_lines(conn, gate: dt.Gate, ans: Answer) -> list[dict[str, Any]]:
    """Dated forecast lines (module doc): customer, item, (year, month), quantity, unit,
    the document's day and the cited records and cell; kept per reader (``MEMO``)."""
    return MEMO.step(ans.reader, "forecast_lines", ans,
                     lambda scratch: _forecast_lines(conn, gate, scratch), conn)


def _forecast_lines(conn, gate: dt.Gate, ans: Answer) -> list[dict[str, Any]]:
    resolve = dt.Resolver(conn)
    out = []
    docs = _docs(conn, gate, FORECAST_WORDS, ans,
                 require=lambda n: _words_in(n, FORECAST_WORDS))
    customer_of = CustomerOf(conn, gate, ans)
    for d in docs:
        customer, basis, evidence = customer_of(d)
        if customer is None:
            ans.counts["forecast_sender_not_one_customer"] += 1
            continue
        ans.counts["customer_basis_" + basis] += 1
        hint = d.day.year if d.day else None
        found_any = False
        for t in dt.tables(d.text or "", year_hint=hint):
            if "item" not in t.roles:
                continue
            unit_col = t.roles.get("unit")
            if t.periods:
                cols = sorted(t.periods.items())
            elif "date" in t.roles and "quantity" in t.roles:
                cols = []
            else:
                continue
            for row in t.rows:
                item_cell = t.cell(row, "item")
                if item_cell is None:
                    continue
                members = resolve.group(item_cell.value, ITEM_TYPES, ITEM_TWINS)
                if not members:
                    ans.counts["forecast_item_unresolved"] += 1
                    continue
                item = members[0]
                unit = row[unit_col].value.strip().lower() if unit_col in row else None
                points = []
                for col, (y, m) in cols:
                    if col in row:
                        points.append((y, m, row[col]))
                if not cols:
                    when = dt.day_of(t.cell(row, "date").value) if t.cell(row, "date") else None
                    q = t.cell(row, "quantity")
                    if when and q:
                        points.append((when.year, when.month, q))
                for y, m, c in points:
                    qty = dt.number(c.value)
                    if qty is None or qty < 0:
                        continue
                    found_any = True
                    out.append({"customer": customer, "customer_basis": basis,
                                "evidence": evidence, "item": item, "members": members,
                                "period": f"{y:04d}-{m:02d}",
                                "quantity": qty, "unit": unit, "doc": d, "cell": c,
                                "year_inferred": t.year_inferred})
        if not found_any:
            ans.counts["forecast_no_table"] += 1
    ans.counts["forecast_lines"] += len(out)
    return out


def _newest(lines: Sequence[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    best: dict[tuple, dict[str, Any]] = {}
    for line in lines:
        key = (line["customer"], line["item"], line["period"])
        cur = best.get(key)
        if cur is None or (line["doc"].day or date.min, line["doc"].object_id) > \
                (cur["doc"].day or date.min, cur["doc"].object_id):
            best[key] = line
    return best


def _subject_filter(conn, ans: Answer, subject: Any
                    ) -> Callable[[str, Sequence[str]], bool] | None:
    """``subject`` narrows a per-customer-and-item view (forecasts, their accuracy, price
    sheets) to one record's rows: a customer's (its identity cluster: every same-record
    twin) or an item's (any record of its code group). None: no subject, every row. Any
    other kind of record keeps no row (counted ``subject_not_customer_or_item``), never
    every row."""
    if not subject:
        return None
    kind = str(subject).split(":", 1)[0]
    if kind == "Customer":
        members = dt.cluster(conn, subject, CUSTOMER_TWINS) | {subject}
        return lambda customer, _items: customer in members
    if kind in ITEM_TYPES:
        members = dt.cluster(conn, subject, ITEM_TWINS) | {subject}
        return lambda _customer, items: bool(members.intersection(items))
    ans.counts["subject_not_customer_or_item"] += 1
    return lambda _customer, _items: False


def view_forecasts(conn, gate, ans, args, as_of) -> None:
    keep = _subject_filter(conn, ans, args.get("subject"))
    for (customer, item, period), line in sorted(_newest(forecast_lines(conn, gate, ans)).items()):
        if keep is not None and not keep(customer, line["members"]):
            continue
        d = line["doc"]
        ans.add({"customer": customer, "customer_basis": line["customer_basis"],
                 "item": item, "period": period,
                 "quantity": str(line["quantity"]), "unit": line["unit"],
                 "unit_basis": "stated" if line["unit"] else "unstated",
                 "received": d.day.isoformat() if d.day else None,
                 "document": d.object_id, "cell": _cell(line["cell"]),
                 "year_inferred": line["year_inferred"]},
                [*d.cited, customer, item, *line["evidence"]])


def _orders(conn, customer: str, items: Sequence[str], parties: dt.Parties
            ) -> dict[str, list[tuple[Decimal, str | None, str]]]:
    """period -> (quantity, unit, line) of the customer's order lines of the item (every
    record of its code; the customer's records by ``Parties.members``)."""
    members = parties.members(customer)
    lines = sorted({l for ls in dt.links_to(conn, items, ORDER_LINE_ITEM).values() for l in ls})
    head = {l: (hs[0] if len(hs) == 1 else None)
            for l, hs in dt.links_from(conn, lines, ORDER_LINE_OF).items()}
    orders = sorted({h for h in head.values() if h})
    who = dt.links_from(conn, orders, ORDERED_BY)
    mine = {o for o in orders if set(who.get(o, [])) & members}
    vals = dt.values(conn, mine, ("order_date", "document_date", "created_at"))
    qty = dt.values(conn, [l for l, h in head.items() if h in mine], ("ordered_quantity",
                                                                      "quantity"))
    out: dict[str, list[tuple[Decimal, str | None, str]]] = defaultdict(list)
    for line, order in head.items():
        if order not in mine:
            continue
        when = dt.day_of(dt.first(vals.get(order), "order_date", "document_date", "created_at"))
        rows = (qty.get(line) or {}).get("ordered_quantity") or \
            (qty.get(line) or {}).get("quantity") or []
        q = dt.number(rows[0]["value"]) if rows else None
        if when is None or q is None:
            continue
        out[f"{when.year:04d}-{when.month:02d}"].append((q, rows[0]["unit"], line))
    return out


def view_forecast_accuracy(conn, gate, ans, args, as_of) -> None:
    keep = _subject_filter(conn, ans, args.get("subject"))
    newest = _newest(forecast_lines(conn, gate, ans))
    if keep is not None:
        newest = {k: v for k, v in newest.items() if keep(k[0], v["members"])}
    parties = _customers(conn, gate, ans)
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    current = f"{as_of.year:04d}-{as_of.month:02d}"
    for (customer, item, period), line in newest.items():
        if period < current:
            pairs[(customer, item)].append(line)
    for (customer, item), lines in sorted(pairs.items()):
        actual = _orders(conn, customer, lines[0]["members"], parties)
        f_sum = a_sum = err = Decimal(0)
        cited: list[str] = [customer, item]
        months = []
        units = set()
        for line in sorted(lines, key=lambda l: l["period"]):
            got = actual.get(line["period"], [])
            units.update(u for _, u, _ in got)
            a = sum((q for q, _, _ in got), Decimal(0))
            f = line["quantity"]
            f_sum += f
            a_sum += a
            err += abs(f - a)
            cited += [*line["doc"].cited, *(l for _, _, l in got)]
            months.append({"period": line["period"], "forecast": str(f), "actual": str(a),
                           "cell": _cell(line["cell"]), "document": line["doc"].object_id})
        if len(units) > 1:
            ans.counts["mixed_units"] += 1
            continue
        ans.counts["scored_pairs"] += 1
        wape = (err / a_sum).quantize(Decimal("0.001")) if a_sum > 0 else None
        bias = ((f_sum - a_sum) / a_sum).quantize(Decimal("0.001")) if a_sum > 0 else None
        ans.add({"customer": customer, "item": item, "months": months[-24:],
                 "forecast_total": str(f_sum), "actual_total": str(a_sum),
                 "wape": None if wape is None else str(wape),
                 "bias": None if bias is None else str(bias),
                 "order_unit": next(iter(units)) if units else None,
                 "unit_basis": "stated" if any(l["unit"] for l in lines) else "unstated"},
                cited)


# --------------------------------------------------------------------------- priority fill

def _order_state(conn, order: str, as_of: date) -> tuple[str, str | None]:
    if dt.links_to(conn, [order], "Shipment.ships").get(order):
        return "shipped", None
    vals = dt.values(conn, [order], ("status", "promised_delivery_date",
                                     "requested_delivery_date", "shipment_date", "ship_date",
                                     "requested_ship_date", "due_date"))
    status = str(dt.first(vals.get(order), "status") or "").strip().lower()
    if status in ("completed", "closed", "shipped", "invoiced", "fully shipped"):
        return "shipped", None
    when = dt.day_of(dt.first(vals.get(order), "promised_delivery_date", "shipment_date",
                              "ship_date", "requested_ship_date", "requested_delivery_date",
                              "due_date"))
    if when is None:
        return "not_scheduled", None
    return ("late" if when < as_of else "scheduled"), when.isoformat()


def view_priority_fill(conn, gate, ans, args, as_of) -> None:
    resolve = dt.Resolver(conn)
    subject = args.get("subject")
    docs = _docs(conn, gate, PRIORITY_WORDS, ans,
                 require=lambda n: _words_in(n, PRIORITY_WORDS))
    # each order's newest listing is its current one (a later list replaces an earlier)
    listed: dict[str, tuple[tuple, dict[str, Any], list[str]]] = {}
    customer_of = CustomerOf(conn, gate, ans)
    for d in sorted(docs, key=lambda d: (d.day or date.min, d.object_id)):
        rank = 0
        of_list: tuple | None = None
        members: set[str] = set()
        has_order = False
        for t in dt.tables(d.text or ""):
            if "order" not in t.roles:
                continue
            has_order = True
            for row in t.rows:
                c = t.cell(row, "order")
                if c is None:
                    continue
                rank += 1
                if not _document_number(c.value):
                    ans.counts["listed_not_a_document_number"] += 1
                    continue
                given = t.cell(row, "priority")
                hit = resolve(c.value, ORDER_TYPES)
                candidates = sorted({o for o in hit if o.startswith("SalesOrder:")} |
                                    {o for po in hit if po.startswith("CustomerPurchaseOrder:")
                                     for o in dt.links_to(conn, [po],
                                                          "SalesOrder.references_customer_po"
                                                          ).get(po, [])})
                if not candidates:
                    ans.counts["listed_not_found"] += 1
                    continue
                # the list's customer first: a number is matched only among that
                # customer's orders (its settled duplicate records joined); a list whose
                # customer is not known attaches nothing and alerts nothing
                if of_list is None:
                    of_list = customer_of(d)
                    members = (dt.cluster(conn, of_list[0], CUSTOMER_TWINS)
                               if of_list[0] else set())
                if not of_list[0]:
                    ans.counts["list_customer_unresolved"] += 1
                    continue
                orders = [o for o in candidates
                          if set(dt.links_from(conn, [o], ORDERED_BY).get(o, [])) & members]
                if not orders:
                    ans.counts["listed_not_this_customers_order"] += 1
                    continue
                customers = [of_list[0]]
                basis, evidence = of_list[1], of_list[2]
                row_out = {"customer": customers[0] if len(customers) == 1 else None,
                           "customer_basis": basis if customers else None,
                           "rank": rank, "priority": given.value if given else None,
                           "listed": c.value, "orders": orders,
                           "list_received": d.day.isoformat() if d.day else None,
                           "document": d.object_id, "cell": _cell(c)}
                key = orders[0]
                listed[key] = ((d.day or date.min, d.object_id), row_out,
                               [*d.cited, *customers, *orders, *evidence])
        if not has_order:
            ans.counts["list_no_order_column"] += 1
    order_rank = ("late", "not_scheduled", "scheduled", "shipped")
    for key, (_, row_out, cited) in sorted(listed.items(), key=lambda kv: (
            kv[1][1]["customer"] or "", kv[1][1]["list_received"] or "", kv[1][1]["rank"])):
        if subject and subject != row_out["customer"] and subject not in row_out["orders"]:
            continue
        states = [_order_state(conn, o, as_of) for o in row_out["orders"]]
        state, when = min(states, key=lambda s: order_rank.index(s[0]))
        ans.counts[state] += 1
        row_out.update(status=state, date=when, alert=state in ("late", "not_scheduled"))
        ans.add(row_out, cited)


def _document_number(raw: str) -> bool:
    """A cell that can be an order or PO number: a digit in it, and five characters or
    more when it is digits only (a row number, a rank or a quantity is not)."""
    v = re.sub(r"\s+", "", str(raw or ""))
    if not re.search(r"\d", v) or len(v) > 40:
        return False
    return len(v) >= 5 if v.isdigit() else len(v) >= 4


# --------------------------------------------------------------------------- prices

def _billed_lines(conn, customer: str, parties: dt.Parties) -> dict[str, list[tuple[str, str]]]:
    """item record -> (line, invoice) of every invoice line billed to the customer (any
    record of it: ``Parties.members``) whose one invoice it is; read once per customer,
    from the customer's invoices (an item's lines over every customer: 1,600 and more)."""
    members = parties.members(customer)
    invoices = {i for got in dt.links_to(conn, members, BILLED_TO).values() for i in got}
    lines = sorted({l for got in dt.links_to(conn, invoices, INVOICE_LINE_OF).values()
                    for l in got})
    head = {l: hs[0] for l, hs in dt.links_from(conn, lines, INVOICE_LINE_OF).items()
            if len(hs) == 1 and hs[0] in invoices}
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for line, items in dt.links_from(conn, sorted(head), INVOICE_LINE_ITEM).items():
        for item in dict.fromkeys(items):
            out[item].append((line, head[line]))
    return out


def _invoice_prices(conn, billed: Mapping[str, Sequence[tuple[str, str]]],
                    items: Sequence[str], since: date | None, until: date
                    ) -> list[tuple[date, Decimal, str | None, str | None, str, str]]:
    """The net unit prices of the customer's invoice lines (``billed``: ``_billed_lines``)
    of the item (``items``: every record of its code) between ``since`` and ``until``."""
    head = dict(sorted({line: inv for item in dict.fromkeys(items)
                        for line, inv in billed.get(item, ())}.items()))
    mine = set(head.values())
    days = dt.values(conn, mine, ("posting_date", "invoice_date", "status"))
    amounts = dt.values(conn, [l for l, h in head.items() if h in mine],
                        ("invoiced_quantity", "amount_excluding_tax", "extended_amount",
                         "invoice_discount_share", "unit_price"))
    out = []
    for line, inv in head.items():
        if inv not in mine:
            continue
        if str(dt.first(days.get(inv), "status") or "").strip().lower() in ("draft",
                                                                          "in review"):
            continue
        when = dt.day_of(dt.first(days.get(inv), "posting_date", "invoice_date"))
        if when is None or when > until or (since and when < since):
            continue
        a = amounts.get(line) or {}
        q = dt.number(dt.first(a, "invoiced_quantity"))
        if not q or q <= 0:
            continue
        # the net unit price: amount excluding tax, else the extended amount less its
        # invoice discount share (Sage), over the invoiced quantity; else the unit price
        amt_rows = a.get("amount_excluding_tax") or a.get("extended_amount") or []
        amt = dt.number(amt_rows[0]["value"]) if amt_rows else None
        if amt is not None and not a.get("amount_excluding_tax"):
            cut = dt.number(dt.first(a, "invoice_discount_share"))
            amt = amt - cut if cut is not None else amt
        if amt is not None and amt > 0:
            price, currency = amt / q, amt_rows[0].get("currency")
        else:
            unit_rows = a.get("unit_price") or []
            price = dt.number(unit_rows[0]["value"]) if unit_rows else None
            currency = unit_rows[0].get("currency") if unit_rows else None
            if price is None or price <= 0 or amt == 0:
                continue   # a free or sample line: no price paid, never a price of 0
        unit = (a.get("invoiced_quantity") or [{}])[0].get("unit")
        out.append((when, price, currency, unit, line, inv))
    return out


def view_price_sheets(conn, gate, ans, args, as_of) -> None:
    if FINANCE not in gate.held:
        ans.withheld["withheld_finance"] += 1
        return
    newest, groups = MEMO.step(ans.reader, "sheet_prices", ans,
                               lambda scratch: _sheet_prices(conn, gate, scratch), conn)
    parties = _customers(conn, gate, ans)
    keep = _subject_filter(conn, ans, args.get("subject"))
    if keep is not None:
        newest = {k: v for k, v in newest.items() if keep(k[0], groups[k[1]])}
    else:
        newest = dict(newest)
    _compare_prices(conn, gate, ans, newest, groups, parties, as_of)


def _sheet_prices(conn, gate: dt.Gate, ans: Answer
                  ) -> tuple[dict[tuple[str, str], tuple[dt.Doc, dt.Cell, Decimal, str | None]],
                             dict[str, list[str]]]:
    """Every price sheet's newest price per (customer, item) and each item's code group."""
    resolve = dt.Resolver(conn)
    docs = _docs(conn, gate, PRICE_WORDS, ans, require=lambda n: _words_in(n, PRICE_WORDS))
    newest: dict[tuple[str, str], tuple[dt.Doc, dt.Cell, Decimal, str | None]] = {}
    customer_of = CustomerOf(conn, gate, ans)
    groups: dict[str, list[str]] = {}
    for d in docs:
        customer, _basis, _evidence = customer_of(d)
        if customer is None:
            ans.counts["sheet_sender_not_one_customer"] += 1
            continue
        stated = dt.stated_currency(d.text or "")
        for t in dt.tables(d.text or ""):
            if "item" not in t.roles or "price" not in t.roles:
                continue
            for row in t.rows:
                ic, pc = t.cell(row, "item"), t.cell(row, "price")
                if ic is None or pc is None:
                    continue
                m = dt.money(pc.value)
                if m is None or m[0] <= 0:
                    continue
                group = resolve.group(ic.value, ITEM_TYPES, ITEM_TWINS)
                if not group:
                    ans.counts["price_item_unresolved"] += 1
                    continue
                item = group[0]
                groups[item] = group
                cur_cell = t.cell(row, "currency")
                currency = m[1] or (cur_cell.value.strip().upper() if cur_cell and
                                    cur_cell.value.strip().upper() in dt.CURRENCIES else stated)
                key = (customer, item)
                prev = newest.get(key)
                if prev is None or (d.day or date.min, d.object_id) > \
                        (prev[0].day or date.min, prev[0].object_id):
                    newest[key] = (d, pc, m[0], currency)
    return newest, groups


def _compare_prices(conn, gate: dt.Gate, ans: Answer, newest, groups, parties: dt.Parties,
                    as_of: date) -> None:
    ans.counts["sheet_prices"] = len(newest)
    billed_to: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for (customer, item), (d, pc, price, currency) in sorted(newest.items()):
        until = min(as_of, d.day + timedelta(days=PRICE_WINDOW_DAYS)) if d.day else as_of
        if customer not in billed_to:
            billed_to[customer] = _billed_lines(conn, customer, parties)
        billed = _invoice_prices(conn, billed_to[customer], groups[item], d.day, until)
        # only lines this reader may open (the line and its invoice) give any figure,
        # count or worst price; the rest are counted as withheld, never used
        gate.load(o for b in billed for o in b[4:6])
        readable = [b for b in billed if gate.visible(b[4]) and gate.visible(b[5])]
        if len(readable) < len(billed):
            ans.withheld["invoice_line_not_readable"] += len(billed) - len(readable)
        billed = readable
        currencies = {b[2] for b in billed}
        if not billed:
            ans.counts["no_invoice_since"] += 1
            continue
        if currency is None:
            if len(currencies) != 1 or None in currencies:
                ans.counts["currency_unknown"] += 1
                continue
            basis, currency = "assumed_invoice", next(iter(currencies))
        else:
            basis = "stated"
        same = [b for b in billed if b[2] == currency]
        if not same:
            ans.counts["currency_differs"] += 1
            continue
        off = [b for b in same if abs(b[1] - price) > price * PRICE_TOLERANCE]
        ans.counts["compared"] += 1
        ans.counts["mismatch" if off else "match"] += 1
        worst = max(off or same, key=lambda b: abs(b[1] - price))
        ans.add({"customer": customer, "item": item, "sheet_price": str(price),
                 "currency": currency, "currency_basis": basis,
                 "sheet_day": d.day.isoformat() if d.day else None,
                 "document": d.object_id, "cell": _cell(pc),
                 "invoice_lines": len(same), "mismatched_lines": len(off),
                 "status": "mismatch" if off else "match",
                 "worst_invoiced_price": str(worst[1].quantize(Decimal("0.0001"))),
                 "worst_invoice_day": worst[0].isoformat(),
                 "invoiced_unit": worst[3]},
                [*d.cited, customer, item, *(o for b in same for o in b[4:6])], [FINANCE])


# --------------------------------------------------------------------------- compliance

def _kind_of(name: str) -> str | None:
    n = " " + " ".join(re.findall(r"[a-z0-9]+", name.lower())) + " "
    for kind, words in PACK_KINDS.items():
        if any(f" {w} " in n for w in words):
            return kind
    return None


def view_compliance_pack(conn, gate, ans, args, as_of) -> None:
    subject = args.get("subject")
    if not isinstance(subject, str) or not subject.startswith(("Product:", "FgItem:", "Lot:")):
        ans.counts["subject_required"] += 1
        return
    if not gate.visible(subject):
        ans.withheld["withheld_not_readable"] += 1
        return
    codes = _codes(conn, subject)
    if not codes:
        ans.counts["subject_has_no_code"] += 1
        return
    found: dict[str, dt.Doc] = {}
    for code in codes:
        token = re.sub(r"[^A-Za-z0-9]", " ", code).split()
        if not token:
            continue
        hits = dt.find_documents(conn, [max(token, key=len)],
                                 require=lambda n, c=code: _norm_code(c) in _norm_code(n))
        if not hits.complete:
            ans.complete = False
        for d in hits.docs:
            found[d.object_id] = d
    gate.load(o for d in found.values() for o in d.cited)
    by_kind: dict[str, list[dt.Doc]] = defaultdict(list)
    for d in found.values():
        kind = _kind_of(d.name)
        if kind is None:
            ans.counts["named_not_compliance"] += 1
            continue
        by_kind[kind].append(d)
    for kind in PACK_KINDS:
        for d in sorted(by_kind.get(kind, []), key=lambda d: (d.day or date.min, d.object_id),
                        reverse=True):
            ans.counts[kind] += 1
            ans.add({"kind": kind, "name": d.name, "document": d.object_id,
                     "dated": d.day.isoformat() if d.day else None, "subject": subject},
                    [*d.cited, subject])
    for code in codes:
        for coa in _coa_records(conn, code, ans):
            ans.counts["coa_record"] += 1
            ans.add({"kind": "coa_record", "name": code, "document": coa,
                     "dated": None, "subject": subject}, [coa, subject])


def _norm_code(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _codes(conn, subject: str) -> list[str]:
    props = ("code", "erp_code", "item_code", "no", "number", "lot_number", "lot_text")
    ids = [subject]
    if subject.startswith("Lot:"):
        ids += dt.links_from(conn, [subject], "Lot.of_item").get(subject, [])
    vals = dt.values(conn, ids, props)
    out = []
    for oid in ids:
        for p in props:
            for r in (vals.get(oid) or {}).get(p, []):
                v = str(r["value"] or "").strip()
                if len(_norm_code(v)) >= 4 and v.upper() not in ("NA", "N/A", "NONE"):
                    out.append(v)
    return sorted(set(out))


def _coa_records(conn, code: str, ans: "Answer | None" = None) -> list[str]:
    """The certificates of analysis whose lot or product code is ``code`` (at most 20).
    The value word index unreadable: none, with ``coa_not_read`` counted and the answer
    marked incomplete (never a silent "no certificate")."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", code) if w]
    q = " AND ".join(dt.fts_query([w]) for w in words if dt.fts_query([w]))
    if not q:
        return []
    out = set()
    try:
        found = dt.word_values(conn, q, ["lot", "product_code"],
                               prefix="CertificateOfAnalysis:", limit=COA_SCAN)
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc):
            raise
        if ans is not None:
            ans.counts["coa_not_read"] += 1
            ans.complete = False
        return []
    for oid, _prop, value in found.rows:
        if _norm_code(value) == _norm_code(code):
            out.add(oid)
    if not found.complete and ans is not None:
        ans.counts["coa_more_matches"] += 1
        ans.complete = False
    return sorted(out)


# --------------------------------------------------------------------------- answer bank

_BOOLEAN = frozenset({"true", "false", "yes", "no", "x", "n/a", "na", ""})


def _is_question(v: str) -> bool:
    v = v.strip()
    return 12 <= len(v) <= 300 and v.lower() not in _BOOLEAN and (
        v.endswith("?") or re.match(r"(?i)^(do|does|is|are|has|have|can|will|please "
                                    r"(provide|list|describe|state)|list|describe)\b", v)
        is not None)


def qa_pairs(text: str) -> list[tuple[str, str, str | None]]:
    """(question, answer, cell or None) pairs of one questionnaire's text: a question and
    answer column; in a sheet, a question cell and the next filled cell of its row that is
    no further question; in prose, a question line and the line after it."""
    out: list[tuple[str, str, str | None]] = []
    sheets = dt.grids(text)
    for t in dt.tables(text):
        if "question" in t.roles and "answer" in t.roles:
            for row in t.rows:
                q, a = t.cell(row, "question"), t.cell(row, "answer")
                if q and a:
                    out.append((q.value, a.value, a.address))
    if out:
        return out
    if sheets and any(name for name, _ in sheets):
        for name, grid in sheets:
            for number, row in sorted(grid.items()):
                cols = sorted(row)
                for i, col in enumerate(cols):
                    if not _is_question(row[col]):
                        continue
                    answer = next((row[c] for c in cols[i + 1:] if row[c].strip()), None)
                    if answer and not _is_question(answer):
                        nxt = cols[cols.index(col) + 1:]
                        cell = next(f"{dt._letters(c)}{number}" for c in nxt if row[c].strip())
                        out.append((row[col].strip(), answer.strip(), cell))
                    break
        return out
    lines = [l.strip() for l in (text or "").splitlines()]
    for i, line in enumerate(lines[:-1]):
        nxt = lines[i + 1]
        if _is_question(line) and line.endswith("?") and nxt and not _is_question(nxt) \
                and len(nxt) <= 500:
            out.append((line, nxt, None))
    return out


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", str(text).lower())
            if t not in ("the", "and", "you", "your", "are", "for", "with", "does", "this")}


# commercial and terms words, each matched as a word prefix (``stem*``: contract matches
# contractually, discount discounted); the short abbreviations (po, moq) as whole words,
# since as prefixes they would match ordinary words (policy, post)
_SPECIFIC_WORDS = ("pric*", "cost*", "rebat*", "discount*", "contract*", "agreement*",
                   "exclusiv*", "moq", "minimum*", "volum*", "forecast*", "allocat*",
                   "term*", "invoic*", "payment*", "account*", "po", "purchas*", "order*",
                   "commit*")
_FIGURE = re.compile(
    r"\d[.,]\d"                                  # a decimal (a price, a rate)
    r"|[$€£¥]\s?\d|\b(?:USD|CAD|EUR|GBP)\b"       # money
    r"|\bper\b|\d\s*/\s*[a-z]"                    # per unit
    r"|\d\s*%"                                     # a share
    r"|\d\s*(?!(?:and|or|to|years?|months?|days?|hours?)\b)[a-z]{1,12}\b"   # 500 cases
    r"|\bnet\s*\d|\buntil\s+(?:\w+\s+)?(?:19|20)\d\d\b", re.I)
# a way to reach someone: an email, a web address or domain, a phone number, a street
# address, a postal or ZIP code
_CONTACT = re.compile(
    r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b(?:https?://|www\.)\S+|\b[a-z0-9-]+\.(?:com|ca|net|org|io|co|us|biz|info|example)\b"
    r"|\+?\d[\d ().-]{7,}\d"
    r"|\b\d{1,6}\s+(?:[A-Za-z]+\s+){0,3}(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|"
    r"dr|lane|ln|way|court|ct|crescent|cres|highway|hwy|parkway|pkwy)\b"
    r"|\b(?:suite|unit|p\.?\s?o\.?\s?box)\s*#?\s*\d+"
    r"|\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b|\b[A-Z]{2}\s\d{5}(?:-\d{4})?\b", re.I)
_PAIR = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b")
# capitalised pairs that are standards, regulators or roles, never a person
_STANDARD_PAIRS = frozenset({"good manufacturing", "health canada", "food safety",
                             "quality assurance", "quality control", "safety data",
                             "certificate of", "united states", "north america",
                             "customer service", "purchase order", "supply chain"})
# words of a party's name that name no one (legal forms, joiners)
_NAME_NOISE = frozenset({"inc", "ltd", "llc", "lp", "llp", "corp", "corporation", "co",
                         "company", "limited", "the", "and", "of", "de", "la", "le", "et",
                         "group", "holdings", "international", "intl", "na", "usa",
                         "canada", "dba"})


# The company-fact questions an answer may be shared across customers for (the declared
# list; a question is in a category when one of its words is, ``*`` a prefix). Every other
# question's answer belongs to the customer that asked it.
COMPANY_FACT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "certifications": ("certif*", "gmp", "iso", "organic", "kosher", "halal", "sqf", "brc",
                       "accredit*"),
    "facility_capabilities": ("facility", "facilities", "plant", "plants", "capabilit*",
                              "equipment", "manufactur*", "warehous*"),
    "quality_systems": ("quality", "haccp", "sop", "sops", "traceab*", "recall*"),
    "allergen_contamination": ("allergen*", "contamina*", "gluten", "peanut*"),
    "insurance": ("insurance", "insured", "liability"),
    "sustainability": ("sustainab*", "environment*", "recycl*", "esg", "carbon", "pcr"),
    "regulatory": ("regulat*", "registration*", "registered", "fda", "licen*", "npn"),
    "lead_time_policy": ("leadtime", "lead"),
}
# all-caps pairs that are standards or regulators, never a name
_ACRONYMS = frozenset({"GMP", "ISO", "HACCP", "FDA", "USDA", "SQF", "BRC", "NSF", "IFS",
                       "FSSC", "CFIA", "NPN", "DIN", "OTC", "SDS", "COA", "PCR", "ESG",
                       "EU", "USA", "US", "CA", "UK", "LLC", "INC", "LTD", "NA", "N/A"})
_DOMAIN = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z][a-z0-9-]{1,23}\b", re.I)
# an answer spoken to the customer (you, your, us, them, for you ...) is theirs
_DIRECT_WORDS = ("you", "your", "yours", "yourself", "us", "them", "their", "theirs")
# commercial promises (consignment, free, charges, guarantees, penalties, waivers, credits,
# chargebacks, exclusivity, dedicated capacity, SLAs, priority, reserved stock)
_COMMERCIAL_WORDS = ("consign*", "free*", "charg*", "guarant*", "penalt*", "waiv*",
                     "credit*", "chargeback*", "exclusiv*", "dedicat*", "sla", "slas",
                     "priorit*", "reserv*")
_HOLD_FOR = re.compile(r"(?i)\bhold\s+(?:\w+\s+)?for\b|\b(?:for|to)\s+you\b")
_CAPS_PAIR = re.compile(r"\b([A-Z][A-Z'-]+)\s+([A-Z][A-Z'-]+)\b")


def fact_category(question: str) -> str | None:
    """The declared company-fact category a question asks about, else None."""
    for category, words in COMPANY_FACT_CATEGORIES.items():
        if category == "lead_time_policy":
            if re.search(r"(?i)\blead\s*-?\s*times?\b", question):
                return category
            continue
        if _words_in(question, words):
            return category
    return None


def _specific(text: str, customer_of: "CustomerOf", source: str | None = None) -> bool:
    """Specific by default. An answer is generic only when it carries no way to reach
    someone (email, web address, phone, street or postal address), no person's name, no
    word spoken to the customer (you, your, us, them), no commercial promise, no figure (a decimal,
    money, a per-unit or a number next to a unit, a share, net N, until a year), no word
    of the source customer's name (any length), no word of any customer's name (one-word
    names too, case-insensitive; legal forms aside) and no commercial or terms word."""
    if _FIGURE.search(text) or _words_in(text, _SPECIFIC_WORDS) or _CONTACT.search(text) \
            or _DOMAIN.search(text) or _words_in(text, _DIRECT_WORDS + _COMMERCIAL_WORDS) \
            or _HOLD_FOR.search(text):
        return True
    if any(a not in _ACRONYMS or b not in _ACRONYMS for a, b in _CAPS_PAIR.findall(text)):
        return True
    if customer_of.names_a_person(text):
        return True
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    if source and words & customer_of.name_words(source, every=True):
        return True
    return bool(words & customer_of.all_name_words())


def _answer_pairs(conn, gate: dt.Gate, ans: Answer) -> list[tuple]:
    """Every answer we gave a customer in a questionnaire the reader may read: (document,
    question, answer, cell, customer, basis, evidence, specific), whoever the draft is for
    (``view_answer_bank`` narrows it); a supplier's or an unknown party's counted. Read
    per reader (``answer_bank_store``: its reader-independent part collected through this
    reader's gate, then decided for them)."""
    from . import answer_bank_store
    return answer_bank_store.pairs(answer_bank_store.collect(conn, gate), gate, ans)


def _bank_pairs(conn, gate: dt.Gate, ans: Answer) -> list[tuple]:
    """``_answer_pairs`` from the company's kept bank (``answer_bank_store``) when it may
    answer for this reader, decided with the reader's own gate; else read per reader and
    kept in ``MEMO`` as before."""
    from . import answer_bank_store
    bank = getattr(ans, "bank", None)
    if bank is not None:
        scratch = Answer(gate, ans.view)
        try:
            got = answer_bank_store.pairs(bank, gate, scratch)
        except answer_bank_store.BankMismatch:
            ans.counts["answer_bank_read_per_reader"] += 1
        else:
            ans.counts.update(scratch.counts)
            ans.withheld.update(scratch.withheld)
            ans.complete = ans.complete and scratch.complete
            return got
    return MEMO.step(ans.reader, "answer_pairs", ans,
                     lambda scratch: _answer_pairs(conn, gate, scratch), conn)


def view_answer_bank(conn, gate, ans, args, as_of) -> None:
    """Past questionnaire answers as a cited draft (module doc). Each answer is marked with
    whose questionnaire it came from (``party``: a customer's, a supplier's, or unknown) and
    whether it is generic or specific to that customer. A draft reuses only answers we gave
    a customer: generic ones for anyone, specific ones only for the customer they were
    given to (``subject``/``customer``: the customer the draft is for; none named: no
    specific answer). A supplier's questionnaire, or one from no known party, never feeds
    a draft (counted)."""
    question = args.get("question")
    target = args.get("subject")
    target_members = (dt.cluster(conn, target, CUSTOMER_TWINS)
                      if isinstance(target, str) and target.startswith("Customer:") else set())
    if target and not target_members:
        ans.counts["subject_not_a_customer"] += 1
        return
    if target and not gate.visible(target):
        ans.withheld["withheld_not_readable"] += 1
        return
    want = _tokens(question) if isinstance(question, str) else set()
    entries = []
    for d, q, a, cell, customer, basis, evidence, specific in _bank_pairs(conn, gate, ans):
        if specific and customer not in target_members:
            ans.counts["specific_to_another_customer" if target_members
                       else "specific_needs_customer"] += 1
            continue
        score = len(want & _tokens(q)) / len(want) if want else 0.0
        if want and score == 0:
            continue
        entries.append((score, d.day or date.min, d, q, a, cell, customer, basis,
                        evidence, specific))
    ans.counts["answers"] = len(entries)
    entries.sort(key=lambda e: (-e[0], -e[1].toordinal(), e[2].object_id))
    for score, day, d, q, a, cell, customer, basis, evidence, specific in entries:
        ans.add({"question": q, "answer": a, "match": round(score, 2),
                 "answered": d.day.isoformat() if d.day else None, "document": d.object_id,
                 "cell": cell, "party": "customer", "party_id": customer,
                 "party_basis": basis,
                 "scope": "customer_specific" if specific else "generic",
                 "category": fact_category(q)},
                [*d.cited, customer, *evidence])


# --------------------------------------------------------------------------- issues

ISSUE_SOURCES = (("PlannerTask", ("title", "description"), ("created_at",)),
                 ("Email", ("subject",), ("sent_at", "received_at")))
SUPPLIER_LINKS = ("Email.sent_by_supplier", "Email.mentions_supplier",
                  "TeamsChatMessage.mentions_supplier", "TeamsChannelMessage.mentions_supplier")


def _issue_records(conn, ans: Answer) -> dict[str, tuple[str, str]]:
    """record -> (type, its matched text): Planner tasks and mail subjects by the value
    index, Teams messages by the text index (their chunk text read here, never kept);
    the three read side by side, each on its own read-only connection of the store."""
    from concurrent.futures import ThreadPoolExecutor
    from ontology.text_index import chunk_text

    q = dt.fts_query(ISSUE_WORDS)
    not_read: list[str] = []   # sources not (wholly) read, appended from the threads
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row and row[2] else None

    def connect():
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                               check_same_thread=False) if path else conn

    def values_of(otype: str, props: Sequence[str]) -> list[tuple[str, str, str]]:
        c = connect()
        try:
            found = dt.word_values(c, q, props, prefix=f"{otype}:", limit=ISSUE_SCAN)
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc):
                raise
            # the value word index unreadable: this source's issues are not in the answer,
            # and it says so (never "no issues")
            not_read.append(f"{otype.lower()}_not_read")
            return []
        finally:
            if c is not conn:
                c.close()
        if not found.complete:
            not_read.append(f"{otype.lower()}_more_matches")
        return [(oid, otype, value) for oid, _prop, value in found.rows]

    def teams() -> list[tuple[str, str, str]]:
        c = connect()
        out = []
        try:
            for oid, body in c.execute(
                    "SELECT f.object_id, k.body FROM chunk_text ct JOIN text_chunks k ON "
                    "k.chunk_id = ct.rowid JOIN text_fields f ON f.field_id = k.field_id WHERE "
                    "chunk_text MATCH ? AND (f.object_id LIKE 'TeamsChatMessage:%' OR "
                    "f.object_id LIKE 'TeamsChannelMessage:%')", (q,)):
                try:
                    out.append((oid, oid.split(":", 1)[0], chunk_text(body)))
                except Exception:  # noqa: BLE001 - an unreadable chunk says nothing
                    continue
        except sqlite3.Error:
            not_read.append("teams_not_read")
        finally:
            if c is not conn:
                c.close()
        return out

    jobs = [lambda t=t, p=p: values_of(t, p) for t, p, _ in ISSUE_SOURCES] + [teams]
    if path:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            parts = list(pool.map(lambda job: job(), jobs))
    else:
        parts = [job() for job in jobs]
    for reason in not_read:
        ans.counts[reason] += 1
        ans.complete = False
    out: dict[str, tuple[str, str]] = {}
    for part in parts:
        for oid, otype, text in part:
            if _is_issue(text):
                prev = out.get(oid)
                out[oid] = (otype, (prev[1] + " " if prev else "") + text)
    return out


def _is_issue(text: str) -> bool:
    specific = [w for w in ISSUE_WORDS if w not in GENERIC_ISSUE]
    if _words_in(text, specific):
        return True
    return _words_in(text, GENERIC_ISSUE) and _words_in(text, QUALITY_CONTEXT)


def _codes_in(resolve: dt.Resolver, text: str) -> tuple[set[str], set[str]]:
    lots, items = set(), set()
    for tok in sorted(set(re.findall(r"\b[A-Z0-9][A-Z0-9-]{3,30}\b", text.upper()))):
        if not re.search(r"\d", tok):
            continue
        lots.update(resolve(tok, ("Lot",)))
        items.update(resolve(tok, ITEM_TYPES))
    return lots, items


def view_supplier_issues(conn, gate, ans, args, as_of) -> None:
    """Raw-material and supplier quality issues (module doc): a mail subject counts only
    when its sender's domain is one supplier's; a Planner task or Teams message only when
    it names a received lot (its supplier through the receipt) or an item, or links a
    supplier."""
    resolve = dt.Resolver(conn)
    found = _issue_records(conn, ans)
    gate.load(found)
    seen = [o for o in sorted(found) if gate.visible(o)]
    ans.withheld["withheld_not_readable"] += len(found) - len(seen)
    suppliers_by = dt.Parties(conn, "Supplier", "Supplier.same_record_as",
                              getattr(gate.guard, "internal", ()))
    mails = [o for o in seen if found[o][0] == "Email"]
    senders = dt.values(conn, mails, ("sender",))
    from_supplier = {o for o in mails if suppliers_by.of(dt.first(senders.get(o), "sender"))}
    ans.counts["mail_not_from_supplier"] += len(mails) - len(from_supplier)
    seen = [o for o in seen if found[o][0] != "Email" or o in from_supplier]
    vals = dt.values(conn, seen, ("title", "subject", "description", "created_at", "sent_at",
                                  "received_at", "sender"))
    trend: dict[str, Counter] = defaultdict(Counter)
    reported: dict[str, Counter] = defaultdict(Counter)
    subject = args.get("subject")
    # a supplier's or an item's page holds the issues of every same-record twin of it
    kind = str(subject).split(":", 1)[0] if subject else ""
    twins = ("Supplier.same_record_as" if kind == "Supplier" else
             ITEM_TWINS if kind in ITEM_TYPES else None)
    wanted = (dt.cluster(conn, subject, twins) | {subject}) if subject else set()
    for oid in seen:
        otype, matched = found[oid]
        v = vals.get(oid) or {}
        title = str(dt.first(v, "title", "subject") or matched or "")
        text = " ".join([title, str(dt.first(v, "description") or ""), matched])
        when = dt.day_of(dt.first(v, "created_at", "sent_at", "received_at"))
        suppliers: set[str] = set()
        for lt in SUPPLIER_LINKS:
            if lt.startswith(otype + "."):
                suppliers.update(dt.links_from(conn, [oid], lt).get(oid, []))
        reporter = suppliers_by.of(dt.first(v, "sender")) if otype == "Email" else None
        lots, items = _codes_in(resolve, text)
        if reporter:
            # the sender is the material's supplier only when it supplies an item the
            # message names (``SupplierItem``); else it reported the issue (a lab, a broker)
            supplied = {dt.root(conn, s2, "Supplier.same_record_as")
                        for i in sorted(items)
                        for si in dt.links_to(conn, [i], "SupplierItem.for_item").get(i, [])
                        for s2 in dt.links_from(conn, [si], "SupplierItem.offered_by"
                                                ).get(si, [])}
            if reporter in supplied:
                suppliers.add(reporter)
        receipts: set[str] = set()
        for lot in sorted(lots):
            for rl in dt.links_to(conn, [lot], "ReceiptLine.received_lot").get(lot, []):
                for rc in dt.links_from(conn, [rl], "ReceiptLine.line_of").get(rl, []):
                    receipts.add(rc)
                    suppliers.update(dt.links_from(conn, [rc], "Receipt.supplied_by").get(rc, []))
        # the receipts that took the lot in are shown and cited only where the reader may
        # read them (a withheld receipt still names its supplier, never itself)
        gate.load(receipts)
        shown_receipts = sorted(r for r in receipts if gate.visible(r))
        ans.withheld["receipt_not_readable"] += len(receipts) - len(
            [r for r in receipts if gate.visible(r)])
        if not (suppliers or lots or items or reporter):
            ans.counts["names_no_supplier_item_or_lot"] += 1
            continue
        kind = next((w.rstrip("*") for w in ISSUE_WORDS if _words_in(text, (w,))), "issue")
        ans.counts["issues"] += 1
        ans.counts["by_source_" + otype] += 1
        ans.counts["linked_supplier" if suppliers else "supplier_unlinked"] += 1
        month = f"{when.year:04d}-{when.month:02d}" if when else "undated"
        for s in suppliers:
            trend[s][month] += 1
        if subject and not wanted & (suppliers | items | lots | {reporter}):
            continue
        if reporter:
            reported[reporter][month] += 1
        ans.add({"record": oid, "source": otype, "kind": kind, "title": title,
                 "dated": when.isoformat() if when else None,
                 "suppliers": sorted(suppliers), "reported_by": reporter,
                 "items": sorted(items), "lots": sorted(lots),
                 "receipts": shown_receipts},
                [oid, *sorted(suppliers), *([reporter] if reporter else []),
                 *shown_receipts])
    def ranked(by: Mapping[str, Counter]) -> list[dict[str, Any]]:
        return [{"supplier": s, "issues": sum(m.values()), "by_month": dict(sorted(
            m.items())[-12:])} for s, m in sorted(by.items(), key=lambda kv: (
                -sum(kv[1].values()), kv[0])) if gate.visible(s)]
    ans.extra = {"trend": ranked(trend), "reported_by_trend": ranked(reported)}


# --------------------------------------------------------------------------- formula lines

def _name_words(text: Any) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if w not in _NAME_STOP and len(w) > 1]


def view_formula_lines(conn, gate, ans, args, as_of, *,
                       budget: float | None = None, clock: Callable[[], float] = time.monotonic
                       ) -> None:
    from .composition_lines import read_sheet
    deadline = clock() + (FORMULA_BUDGET_SECONDS if budget is None else budget)

    def spent(left: int) -> bool:
        """Past the budget: the rest (``left`` sheets) is counted, never read."""
        if clock() < deadline:
            return False
        ans.complete = False
        ans.counts["budget_exhausted"] = 1
        ans.counts["sheets_not_read"] += left
        return True
    ans.counts["portal_formula_lines_not_served"] = 1
    subject = args.get("subject")
    if not isinstance(subject, str) or not subject.startswith(("Product:", "FgItem:")):
        ans.counts["subject_required"] += 1
        return
    if not gate.visible(subject):
        ans.withheld["withheld_not_readable"] += 1
        return
    codes = {_norm_code(c) for c in _codes(conn, subject)}
    name = dt.first(dt.values(conn, [subject], ("name",)).get(subject), "name")
    words = list(dict.fromkeys(_name_words(name)))
    if not words and not codes:
        ans.counts["subject_has_no_name_or_code"] += 1
        return
    need = min(FORMULA_NAME_WORDS, len(words))

    def named(n: str) -> bool:
        toks = set(re.findall(r"[a-z0-9]+", n.lower()))
        return (bool(_FORMULA_NUMBER.search(n)) or _words_in(n, FORMULA_WORDS)) and \
            sum(w in toks for w in words) >= need
    # the product's longest name words find its sheets' names (any one of them)
    probe = sorted(words, key=len, reverse=True)[:3]  # guard-ok: retrieval heuristic — the longest name words find candidate sheets, every word still scores the match
    docs = _docs(conn, gate, probe, ans, require=named) if probe else []
    resolve = dt.Resolver(conn)
    for i, d in enumerate(docs):
        if spent(len(docs) - i):
            return
        sheet = read_sheet(d.text or "")
        if not sheet["lines"]:
            ans.counts["sheet_no_lines_read"] += 1
            continue
        stated = (sheet["header"].get("product_code") or {}).get("value")
        if stated is not None:
            if _norm_code(stated) not in codes:
                ans.counts["sheet_other_product"] += 1
                continue
            basis = "code"
        else:
            said = (sheet["header"].get("name") or {}).get("value")
            if not said or _name_words(said) != _name_words(name):
                ans.counts["sheet_product_not_stated"] += 1
                continue
            basis = "name"
        formula = (sheet["header"].get("formula_code") or {}).get("value")
        ans.counts["sheet_" + sheet["status"]] += 1
        ans.add({"kind": "sheet", "document": d.object_id, "name": d.name,
                 "dated": d.day.isoformat() if d.day else None, "subject": subject,
                 "basis": basis, "formula_code": formula, "status": sheet["status"],
                 "share_total": sheet["share_total"], "lines": len(sheet["lines"]),
                 "missing_lines": sheet["missing_lines"]}, [*d.cited, subject])
        for line in sheet["lines"]:
            members = resolve.group(line["part"], ITEM_TYPES, ITEM_TWINS)
            item = members[0] if members else None
            ans.counts["part_resolved" if item else "part_unresolved"] += 1
            supplier_items: list[str] = []
            if item is not None:
                found = sorted({si for m in members for si in dt.links_to(
                    conn, [m], "SupplierItem.for_item").get(m, [])})
                gate.load(found)
                supplier_items = [si for si in found if gate.visible(si)]
                if len(supplier_items) < len(found):
                    ans.withheld["supplier_item_not_readable"] += \
                        len(found) - len(supplier_items)
            ans.add({"kind": "component", "document": d.object_id, "subject": subject,
                     "formula_code": formula, "line": line["line"], "no": line["no"],
                     "part": line["part"], "description": line["description"],
                     "share_percent": line["share"], "quantity": line["quantity"],
                     "unit": line["unit"], "check": line["check"], "item": item,
                     "supplier_items": supplier_items},
                    [*d.cited, subject] + ([item] if item else []))

def view_item_on_hand_sheets(conn, gate, ans, args, as_of) -> None:
    from . import proactive_sheets as sheets
    subject = args.get("subject")
    if not isinstance(subject, str) or not subject.startswith(("Product:", "FgItem:")):
        ans.counts["subject_required"] += 1
        return
    if not gate.visible(subject):
        ans.withheld["withheld_not_readable"] += 1
        return
    path = getattr(ans, "readings", None)
    if path is None or not path.is_file():
        ans.counts["sheet_readings_not_set_up"] = 1
        return
    props = sheets.PRODUCT_CODES
    codes = [str(r["value"]) for p in props
             for r in (dt.values(conn, [subject], props).get(subject) or {}).get(p, [])
             if r["value"] not in (None, "")]
    twins = {subject, *dt.links_from(conn, [subject], ITEM_TWINS).get(subject, []),
             *dt.links_to(conn, [subject], ITEM_TWINS).get(subject, [])}
    wanted = {sheets._norm(c) for c in codes if len(sheets._norm(c)) >= 2}
    rows = conn.execute(
        "SELECT object_id, value FROM object_values WHERE superseded_at IS NULL AND "
        "property IN ('code', 'erp_code') AND object_id LIKE 'Product:%'").fetchall()
    other = {o for o, v in rows if o not in twins and sheets._norm(v) in wanted}
    if other:   # another item holds the same number: not decided for either
        ans.counts["item_code_ambiguous"] += 1
        return
    skipped: Counter = Counter()
    found = sheets.item_readings(path, gate.guard, codes, as_of, skipped)
    for reason, n in skipped.items():
        ans.counts[reason] += n
    for f in found:
        ans.add({"kind": "sheet_on_hand", "subject": subject, "quantity": format(f["qty"], "f"),
                 "unit": f["unit"], "dated": f["day"].isoformat(), "cell": f["cell"],
                 "fresh": f["fresh"], "source": "hand-kept sheet",
                 "not_added_to_records_stock": True}, [f["object_id"], subject])


def _decimal(raw: Any) -> Decimal | None:
    """A stated number as an exact Decimal (never a float); None for none or text."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _price_line(conn, gate, line: str, po: str, vals, po_vals) -> dict[str, Any] | None:
    """One PO line's price fact (price, quantity, unit, currency, order day, markings)."""
    rows = vals.get(line) or {}
    price, marks = None, set()
    for prop in PO_PRICE_PROPS:
        for row in rows.get(prop) or ():
            got = _decimal(row["value"])
            if got is not None:
                price, read = got, dt._marks(row["markings"])
                if read is None:
                    return None
                marks |= read
                break
        if price is not None:
            break
    head = po_vals.get(po) or {}
    for row in head.get("currency") or ():
        read = dt._marks(row["markings"])
        if read is None:
            return None
        marks |= read
    return {"line": line, "po": po, "price": price,
            "quantity": _decimal(dt.first(rows, "ordered_quantity")),
            "received": _decimal(dt.first(rows, "received_quantity")),
            "uom": (str(dt.first(rows, "uom") or "").strip().lower() or None),
            "currency": (str(dt.first(head, "currency") or "").strip().upper() or None),
            "day": dt.day_of(dt.first(head, "order_date")),
            "po_number": dt.first(head, "po_number", "fm_po_number", "sage_po_number"),
            "markings": sorted(marks)}


def view_item_po_prices(conn, gate, ans, args, as_of) -> None:
    """Per item: what its purchase orders paid (``item_po_prices``, module doc)."""
    items = args.get("items")
    if items is None and isinstance(args.get("subject"), str):
        items = [{"item": args["subject"]}]
    if not isinstance(items, list) or not items:
        ans.counts["items_required"] += 1
        return
    currency = args.get("currency")
    resolve = dt.Resolver(conn)
    for i, entry in enumerate(items):
        if i >= MAX_PRICE_ITEMS:
            ans.counts["items_not_read"] += len(items) - i
            ans.complete = False
            break
        code = entry.get("code") if isinstance(entry, Mapping) else None
        stated = _decimal(entry.get("cost")) if isinstance(entry, Mapping) else None
        if isinstance(entry, Mapping) and entry.get("item"):
            subject = entry["item"]
            if not isinstance(subject, str) or not subject.startswith(
                    tuple(t + ":" for t in ITEM_TYPES)):
                ans.counts["item_not_an_item"] += 1
                continue
            members = sorted(dt.cluster(conn, subject, ITEM_TWINS))
        else:
            members = resolve.group(code, ITEM_TYPES, ITEM_TWINS) \
                if isinstance(code, str) else []
        gate.load(members)
        visible = [m for m in members if gate.visible(m)]
        if not visible:
            # a code naming no item and one naming an item this reader may not read read
            # the same (no oracle)
            ans.counts["code_not_found"] += 1
            if isinstance(code, str):
                ans.extra.setdefault("codes_not_found", []).append(code)
            continue
        if len({dt.root(conn, m, ITEM_TWINS) for m in visible}) > 1:
            # the code is several readable items' (no settled link makes them one): never
            # one's prices for the other's
            ans.counts["code_ambiguous"] += 1
            ans.extra.setdefault("codes_ambiguous", []).append(
                (code if isinstance(code, str) else str(entry.get("item"))))
            continue
        item = visible[0]
        same = sorted(dt.cluster(conn, item, ITEM_TWINS))   # this item's own records only
        lines = sorted({ln for m in same for ln in dt.links_to(conn, [m], PO_LINE_ITEM)
                        .get(m, [])})
        heads = dt.links_from(conn, lines, PO_LINE_OF)
        gate.load([*lines, *(h for hs in heads.values() for h in hs)])
        orphan = [ln for ln in lines if len(heads.get(ln) or ()) != 1 and gate.visible(ln)]
        if orphan:
            ans.counts["po_line_without_one_order"] += len(orphan)
        seen = [ln for ln in lines if ln not in orphan and gate.visible(ln)
                and len(heads.get(ln) or ()) == 1 and gate.visible(heads[ln][0])]
        hidden = len(lines) - len(seen) - len(orphan)
        if hidden:
            ans.withheld["po_line_not_readable"] += hidden
        vals = dt.values(conn, seen, (*PO_PRICE_PROPS, "ordered_quantity",
                                      "received_quantity", "uom"))
        pos = sorted({heads[ln][0] for ln in seen})
        po_vals = dt.values(conn, pos, ("currency", "order_date", "po_number",
                                        "fm_po_number", "sage_po_number"))
        # one purchase order's copies in several systems are one order: the lines of its
        # least-id copy count, the copies' lines never added again
        facts, by_root, copies, unreadable = [], {}, 0, 0
        for ln in seen:
            po = heads[ln][0]
            by_root.setdefault(dt.root(conn, po, PO_TWINS), {}).setdefault(po, []).append(ln)
        for root_po, members_po in sorted(by_root.items()):
            keep = min(members_po)
            copies += sum(len(v) for k, v in members_po.items() if k != keep)
            for ln in members_po[keep]:
                fact = _price_line(conn, gate, ln, keep, vals, po_vals)
                if fact is None:   # its markings could not be read: never shown
                    ans.withheld["po_line_markings_unreadable"] += 1
                    unreadable += 1
                elif not frozenset(fact["markings"]) <= gate.held:
                    # real builds mark the price VALUES (purchasing_prices on unit_price),
                    # not the line: a line whose price this reader may not read is a hidden
                    # line, no price, average or verdict from it (no oracle)
                    ans.withheld["po_line_price_not_readable"] += 1
                    hidden += 1
                else:
                    facts.append(fact)
        if copies:
            ans.counts["po_copy_lines_not_added"] += copies
        priced = [f for f in facts if f["price"] is not None and f["price"] > 0]
        zero = [f for f in facts if f["price"] is not None and f["price"] == 0]
        # the last price: the priced lines of the newest order day; two different prices
        # that day name no last price (never one picked)
        newest = max((f["day"] or date.min for f in priced), default=None)
        that_day = [f for f in priced if (f["day"] or date.min) == newest]
        last = that_day[0] if len({(f["price"], f["currency"], f["uom"])
                                   for f in that_day}) == 1 else None
        two_on_one_day = bool(priced and last is None)
        averages: dict[tuple, list[Decimal]] = {}
        for f in priced:
            if f["quantity"] is None or f["quantity"] <= 0:
                continue
            key = (f["currency"], f["uom"])
            total = averages.setdefault(key, [Decimal(0), Decimal(0), Decimal(0)])
            total[0] += f["price"] * f["quantity"]
            total[1] += f["quantity"]
            total[2] += 1
        average = [{"currency": cur, "uom": uom, "lines": int(n),
                    "quantity": format(q.normalize(), "f"),
                    "price": format((amount / q).quantize(Decimal("0.0001")), "f")}
                   for (cur, uom), (amount, q, n) in sorted(
                       averages.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))]
        verdict = _cost_verdict(stated, currency, last, average, priced)
        if hidden or unreadable:
            # a PO line this reader may not read (or whose markings could not be read)
            # could be the last price, or the purchase a $0 cost lacks: never judged
            verdict = "lines_withheld"
        elif orphan and verdict in ("zero_never_bought", "never_bought"):
            verdict = "lines_unlinked"   # a line with no one order may be the purchase
        cited = [item] + ([last["line"], last["po"]] if last else [])
        # every stated price read (a $0 line's too: its existence is a price fact)
        marks = sorted({m for f in facts if f["price"] is not None for m in f["markings"]})
        row = {"kind": "item_prices", "item": item, "code": str(code) if code else None,
               "po_lines": len(facts), "priced_lines": len(priced), "zero_price_lines": len(zero),
               "received_lines": sum(1 for f in priced if f["received"] and f["received"] > 0),
               "last": {"price": format(last["price"], "f"), "currency": last["currency"],
                        "uom": last["uom"], "po": last["po"], "po_number": last["po_number"],
                        "line": last["line"],
                        "dated": last["day"].isoformat() if last["day"] else None}
               if last else None,
               "average": average, "verdict": verdict, "lines_withheld": hidden,
               "lines_unlinked": len(orphan),
               "stated_cost": format(stated, "f") if stated is not None else None}
        if ans.add(row, cited, marks):
            # counted only for a row shown: a count of a withheld row's verdict would tell
            # what its hidden prices are
            ans.counts["verdict_" + verdict] += 1
            if two_on_one_day:
                ans.counts["last_price_two_on_one_day"] += 1


def _cost_verdict(stated: Decimal | None, currency: Any, last, average, priced) -> str:
    """A stated cost against what the item's POs paid: ``zero_but_bought`` (stated 0, a PO
    paid more than 0), ``zero_never_bought`` (stated 0 and no priced PO line: as a
    customer-supplied item reads), ``matches_last`` / ``matches_average``, ``differs``
    (past ``COST_TOLERANCE`` of both), or why no comparison was made (``no_cost_stated``,
    ``never_bought``, ``currency_not_stated``, ``currency_differs``, ``units_differ``)."""
    if stated is None:
        return "no_cost_stated" if priced else "never_bought"
    if stated == 0:
        return "zero_but_bought" if priced else "zero_never_bought"
    if not priced:
        return "never_bought"
    if not isinstance(currency, str) or not currency:
        return "currency_not_stated"
    want = currency.upper()

    def close(price: Decimal) -> bool:
        return abs(stated - price) <= COST_TOLERANCE * price

    comparable = False
    if last is not None and last["currency"] == want and (
            last["uom"] is None or last["uom"] in EACH_UNITS):
        comparable = True
        if close(last["price"]):
            return "matches_last"
    for avg in average:
        if avg["currency"] == want and (avg["uom"] is None or avg["uom"] in EACH_UNITS):
            comparable = True
            if close(Decimal(avg["price"])):
                return "matches_average"
    if comparable:
        return "differs"
    if (last is None or last["currency"] != want) and all(
            a["currency"] != want for a in average):
        return "currency_differs"
    return "units_differ"


# --------------------------------------------------------------------------- serving

VIEW_FUNCS: dict[str, Callable] = {
    "forecasts": view_forecasts, "forecast_accuracy": view_forecast_accuracy,
    "priority_fill": view_priority_fill, "price_sheets": view_price_sheets,
    "compliance_pack": view_compliance_pack, "answer_bank": view_answer_bank,
    "supplier_issues": view_supplier_issues,
    "formula_lines": view_formula_lines, "item_on_hand_sheets": view_item_on_hand_sheets,
    "item_po_prices": view_item_po_prices}


def answer(conn, *, view: str, may_read: Callable[[str], bool], held: frozenset[str],
           guard: Any, as_of: date, args: Mapping[str, Any] | None = None,
           readings: Any = None, reader: str | None = None,
           bank: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One view for one reader (module doc). ``readings``: the sheet readings file;
    ``reader``: the reader's memo key (``reader_key``; None: nothing kept); ``bank``: the
    company's kept answer bank (``answer_bank_store.load``; None: read per reader)."""
    gate = dt.Gate(conn, may_read=may_read, held=held, guard=guard)
    ans = Answer(gate, view)
    ans.reader = reader
    ans.readings = readings
    ans.bank = bank
    ans.extra = {}
    if view not in VIEW_FUNCS:
        return _error("invalid_input", "view must be one of " + ", ".join(VIEWS))
    if gate.unavailable:
        out = ans.done(withheld="people_exclusion_unavailable")
        out["complete"] = False
        return out
    started = time.monotonic()
    if reader and view in KEPT_VIEWS and (args or {}).get("subject"):
        # a record page reads these lists one after another: their steps start together
        # (the Templates page's whole answer bank, no subject, reads its own step only)
        steps = [("forecast_lines", lambda scratch: _forecast_lines(conn, gate, scratch))]
        if bank is None:
            steps.append(("answer_pairs", lambda scratch: _answer_pairs(conn, gate, scratch)))
        if FINANCE in gate.held:
            steps.append(("sheet_prices", lambda scratch: _sheet_prices(conn, gate, scratch)))
        MEMO.prefetch(reader, conn, gate, view, steps)
    try:
        VIEW_FUNCS[view](conn, gate, ans, dict(args or {}), as_of)
    except StillReading as reading:
        # read in the background for the next request; this one says so, never "none"
        ans.rows, ans.complete = [], False
        ans.counts["still_reading"] = 1
        ans.extra["note"] = STILL_READING_NOTE
        token = reading.token
    else:
        token = None
    out = ans.done(**ans.extra)
    if token:
        out["still_reading_token"] = token   # the read going on (``MEMO.running``)
    out["draft"] = view == "answer_bank"
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


class BusinessDocumentsTool:
    """``plx.aggregate`` mode ``business_documents`` over a host's ``OntologyTools``."""

    def __init__(self, base: Any, guard: Callable[[], Any] | None = None) -> None:
        self.base = base
        self.guard = guard or (lambda: _store_guard(base))

    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict) or arguments.get("mode") != MODE:
            return _error("invalid_input", "mode must be business_documents")
        view = arguments.get("view")
        if view not in VIEWS:
            return _error("invalid_input", "view must be one of " + ", ".join(VIEWS))
        args = {}
        # ``customer`` (the mode's shared input) names the view's subject: a customer,
        # order, supplier, item or lot record
        if "subject" not in arguments and arguments.get("customer") is not None:
            arguments = dict(arguments, subject=arguments.get("customer"))
        for key in ("subject", "question", "status"):
            v = arguments.get(key)
            if v is not None and (not isinstance(v, str) or len(v) > 700):
                return _error("invalid_input", f"{key} must be a string")
            if v is not None:
                args[key] = v
        items = arguments.get("items")
        if items is not None:
            # item_po_prices: the items to price, each a code (with the cost a sheet
            # states for it) or an item record; anything else is refused, never guessed
            if view != "item_po_prices" or not isinstance(items, list) or not items \
                    or len(items) > MAX_PRICE_ITEMS or not all(
                        isinstance(e, dict) and set(e) <= {"code", "cost", "item"}
                        and (isinstance(e.get("code"), str) and 0 < len(e["code"]) <= 60
                             or isinstance(e.get("item"), str) and 0 < len(e["item"]) <= 700)
                        and (e.get("cost") is None or (not isinstance(e["cost"], bool)
                             and isinstance(e["cost"], (str, int))
                             and _decimal(e["cost"]) is not None))
                        for e in items):
                return _error("invalid_input", "items must be 1-%d objects {code (or item), "
                              "cost as decimal text}" % MAX_PRICE_ITEMS)
            args["items"] = items
        currency = arguments.get("currency")
        if currency is not None:
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency):
                return _error("invalid_input", "currency must be a 3-letter code")
            args["currency"] = currency.upper()
        as_of = arguments.get("as_of")
        try:
            day = date.fromisoformat(as_of) if as_of else date.today()
        except (TypeError, ValueError):
            return _error("invalid_input", "as_of must be a date (YYYY-MM-DD)")
        policy = self.base.policy
        held = frozenset(policy.held_markings(actor))
        guard = self.guard()
        return _checked(answer(self.base.store.conn, view=view,
                               may_read=lambda key: policy.may_read(actor, key),
                               held=held, guard=guard, as_of=day, args=args,
                               readings=_readings_file(self.base),
                               reader=reader_key(actor, held, guard),
                               bank=_kept_bank(self.base, guard)
                               if view in KEPT_VIEWS else None))


def _checked(result: dict[str, Any]) -> dict[str, Any]:
    from ontology.json_contract import validate
    from .ontology_tools_policy import load_contracts

    contracts = load_contracts()
    schema = contracts["$defs"]["business_documents_answer" if "error" not in result
                                else "error_result"]
    problems = validate(result, schema, root=contracts)
    if problems:
        return _error("contract_violation", ("output failed the contract: "
                                             + str(problems[0])))
    return result


def _readings_file(base: Any) -> Any:
    from pathlib import Path
    from . import proactive_sheets
    from .cash_forecast import store_file
    path = store_file(base)
    return proactive_sheets.readings_path(Path(path)) if path else None


def _kept_bank(base: Any, guard: Any) -> Mapping[str, Any] | None:
    """The company's kept answer bank when it may answer under ``guard`` (``answer_bank_store``)."""
    from . import answer_bank_store
    from .cash_forecast import store_file
    try:
        return answer_bank_store.load(answer_bank_store.bank_path(store_file(base)), guard)
    except Exception:  # noqa: BLE001 - no kept bank: read per reader
        return None


def _store_guard(base: Any) -> Any:
    """The people exclusion bound to the tenant directory of the store's state (the
    proactive engine's loader); None (every view withheld) when it cannot be loaded. One
    loaded is kept ``GUARD_SECONDS`` per store (loading it read the mail capture's
    directory again on every call: 1-3 s of each list on the served store); one that could
    not be loaded is never kept."""
    from pathlib import Path
    from .proactive_engine import _people
    from .cash_forecast import store_file
    path = store_file(base)
    if not path:
        return None
    now = time.monotonic()
    with _GUARDS_LOCK:
        kept = _GUARDS.get(path)
    if kept is not None and now - kept[0] < GUARD_SECONDS:
        return kept[1]
    guard = _people(Path(path))
    if guard is not None and not getattr(guard, "unavailable", None):
        with _GUARDS_LOCK:
            _GUARDS[path] = (now, guard)
    return guard


GUARD_SECONDS = 60.0
_GUARDS: dict[str, tuple[float, Any]] = {}
_GUARDS_LOCK = threading.Lock()


def register(table: MutableMapping[str, dict[str, Any]], base: Any
             ) -> BusinessDocumentsTool | None:
    """Serve the mode in a host tool table: a call naming it goes here, every other call
    to the handler registered before."""
    entry = table.get(TOOL_NAME)
    if entry is None:
        return None
    tool = BusinessDocumentsTool(base)
    before = entry["handler"]

    def handler(arguments: Any, actor: Any) -> dict[str, Any]:
        if isinstance(arguments, dict) and arguments.get("mode") == MODE:
            return tool.call(arguments, actor)
        return before(arguments, actor)
    table[TOOL_NAME] = dict(entry, handler=handler)
    return tool


# --------------------------------------------------------------------------- alert rule

RULE = "priority_fill_unmet"
KIND = "priority_order_unfilled"


def priority_fill_unmet(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                        result: Callable, settled: Sequence[str], skips: Callable
                        ) -> dict[str, Any]:
    """``priority_order_unfilled``: an order a priority / fill list names (its newest
    listing) that is late or has no ship or delivery date (``view_priority_fill``), one
    alert per order, citing the list, its message and the order. Run by the proactive
    engine; people first, fail closed (``blocked`` without a bound exclusion). Each alert
    shows only to a reader of every cited record."""
    from pathlib import Path
    from .proactive_engine import _people

    guard = _people(Path(st.path))
    gate = dt.Gate(st.conn, may_read=lambda _k: True,
                   held=frozenset({"finance", "l5", "pii", "purchasing_prices",
                                   "quality_controlled"}), guard=guard)
    if gate.unavailable:
        return result([], status="blocked", reasons=["people_exclusion_unavailable"])
    ans = Answer(gate, "priority_fill")
    ans.extra = {}
    view_priority_fill(st.conn, gate, ans, {}, as_of)
    gaps = []
    for row in ans.rows:
        if not row.get("alert"):
            continue
        ids = [c["object_id"] for c in row["cites"]]
        versions = [v for v in st.versions(objects=ids)]
        order = row["orders"][0]
        late = row["status"] == "late"
        gaps.append(gap(
            KIND, RULE, order, order.split(":", 1)[0], row["document"],
            summary=(f"Priority-listed order {row['listed']} is "
                     + (f"late (due {row['date']})" if late else "not scheduled")),
            missing="When will this order the customer put on its priority list ship?",
            why=("A priority or fill list the customer's mail carried names this order "
                 "(its newest listing, rank " + str(row["rank"]) + "), and the order is "
                 + ("past its ship or delivery date and not shipped." if late else
                    "open with no ship or delivery date.")),
            priority="high" if late else "medium",
            about=(order.split(":", 1)[0], "status"), groups=[cites(versions)],
            event={"rank": row["rank"], "status": row["status"]}))
    out = result(gaps, not_evaluated=skips())
    out["counts"] = {k: ans.counts.get(k, 0) for k in ("late", "not_scheduled", "scheduled",
                                                       "shipped", "listed_not_found")}
    return out


__all__ = ["FORMAT", "KIND", "MODE", "RULE", "VIEWS", "priority_fill_unmet", "BusinessDocumentsTool", "answer", "forecast_lines",
           "qa_pairs", "register"]
